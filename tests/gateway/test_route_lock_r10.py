"""Route lock, round 10: a LOCKED session never refreshes or swaps its credential, exposes no model-calling
tools, and its auxiliary getters do no live discovery.

The credential case drives the real owner + ``prompt.submit`` against a loopback Anthropic Messages server;
the profile holds a pool ``api_key`` row (the frozen key) AND a valid Claude Code login in ``CLAUDE_CONFIG_DIR``
(the OAuth token the send-time refresh used to adopt).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from tests.gateway.test_session_route_lock import Owner
from tests.waiting import async_wait_until

FROZEN = "sk-ant-api03-FROZEN-EKEY"
OAUTH = "sk-ant-oat01-CLAUDE-CODE-ROBE"
SSE = "".join(
    f"event: {name}\ndata: {json.dumps(data)}\n\n"
    for name, data in [
        ("message_start", {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-4", "content": [],
            "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
)


class _Anthropic(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("messages"):
            return self._send(200, "{}", "application/json")
        self.server.seen.append((self.headers.get("x-api-key") or self.headers.get("Authorization"), self.path))
        if self.server.unauthorized:
            return self._send(401, json.dumps({"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}}), "application/json")
        self._send(200, SSE, "text/event-stream")

    def _send(self, status, text, kind):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest_asyncio.fixture
async def anthropic(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Anthropic)
    server.seen, server.unauthorized = [], False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    login = tmp_path / "claude"
    login.mkdir()
    (login / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": OAUTH, "refreshToken": "r-1", "expiresAt": int((time.time() + 86400) * 1000)}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(login))
    (tmp_path / "config.yaml").write_text(json.dumps({
        "model": {"provider": "anthropic", "default": "claude-sonnet-4",
                  "base_url": f"http://127.0.0.1:{server.server_port}"},
        "auxiliary": {"title_generation": {"enabled": False}}}))
    (tmp_path / ".env").write_text("")
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [{
        "id": "c1", "label": "static", "auth_type": "api_key", "priority": 0, "source": "manual",
        "access_token": FROZEN, "base_url": f"http://127.0.0.1:{server.server_port}"}]}}))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id="owner", db=db)
    connection = AuthorityConnection(authority, object(), {"user_id": "owner", "native_bootstrap": True})
    handle = Owner(tmp_path, server, server, authority, connection, monkeypatch)
    try:
        yield handle
    finally:
        await connection.close()
        db.close()
        server.shutdown()
        server.server_close()


def _keys(owner):
    return [key for key, _ in owner.primary.seen]


@pytest.mark.asyncio
async def test_locked_anthropic_session_sends_its_frozen_key_on_every_request_never_the_claude_code_token(anthropic):
    ref = anthropic.create(fallback="none")
    await anthropic.call("session.resume", session_id=ref.session_id)
    await anthropic.turn(ref, "one")
    await anthropic.turn(ref, "two")
    assert _keys(anthropic) and set(_keys(anthropic)) == {FROZEN}, _keys(anthropic)


@pytest.mark.asyncio
async def test_a_401_on_a_locked_anthropic_turn_fails_it_and_tries_no_second_key(anthropic):
    ref = anthropic.create(fallback="none")
    await anthropic.call("session.resume", session_id=ref.session_id)
    anthropic.primary.unauthorized = True
    await anthropic.turn(ref, "one")
    assert _keys(anthropic) and set(_keys(anthropic)) == {FROZEN}, _keys(anthropic)
    assert OAUTH not in json.dumps(anthropic.rows(ref.session_id), default=str)


@pytest.mark.asyncio
async def test_unlocked_anthropic_session_still_adopts_the_refreshed_login(anthropic):
    """Control: the refresh path is live when unlocked, so the locked pass above is not a dead path."""
    ref = anthropic.create()
    await anthropic.call("session.resume", session_id=ref.session_id)
    await anthropic.turn(ref, "one")
    assert OAUTH in _keys(anthropic) or f"Bearer {OAUTH}" in _keys(anthropic), _keys(anthropic)


def test_every_credential_refresh_and_rotation_hook_is_inert_on_a_route_locked_agent():
    from unittest.mock import MagicMock

    from agent.agent_runtime_helpers import recover_with_credential_pool
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.route_locked = True
    agent.provider, agent.api_mode, agent.model = "anthropic", "anthropic_messages", "claude-sonnet-4"
    agent._anthropic_api_key, agent._anthropic_base_url = FROZEN, "https://api.anthropic.com"
    agent._credential_pool = MagicMock()
    entry = MagicMock()
    for name, args in [
        ("_try_refresh_anthropic_client_credentials", ()), ("_try_refresh_codex_client_credentials", ()),
        ("_try_refresh_nous_client_credentials", ()), ("_try_refresh_env_client_credentials", ()),
        ("_try_refresh_vertex_client_credentials", ()), ("_swap_credential", (entry,)),
    ]:
        assert getattr(agent, name)(*args) is False, name
    assert agent._try_refresh_provider_runtime_credentials(reason="unauthorized") is False
    assert recover_with_credential_pool(agent, status_code=401, has_retried_429=False) == (False, False)
    assert agent._anthropic_api_key == FROZEN
    agent._credential_pool.assert_not_called()
    assert agent._credential_pool.method_calls == []


def test_a_locked_auxiliary_401_does_not_refresh_provider_credentials(monkeypatch):
    from agent import auxiliary_client as aux
    from agent.auxiliary_route_lock import locked_route

    called = []
    monkeypatch.setitem(aux._CREDENTIAL_REFRESHERS, "anthropic", lambda *_a: called.append(1) or True)
    assert aux._refresh_provider_credentials("anthropic") is True and called == [1]  # unlocked control
    with locked_route(lambda *_a: True):
        assert aux._refresh_provider_credentials("anthropic") is False
    assert called == [1]


# --- (2) default-deny tools ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"toolsets": ["delegation"]}, {"toolsets": ["cronjob", "kanban"]}, {"toolsets": ["vision"]}, {}])
async def test_a_locked_create_without_an_empty_toolset_is_refused(anthropic, extra):
    from hermes_state_runtime import RuntimeStoreError

    from gateway.session_local import create_local_session

    with pytest.raises(RuntimeStoreError) as caught:
        create_local_session(anthropic.authority, anthropic.connection.actor, {
            "request_id": "r10", "source": "cli", "cwd": str(anthropic.home), "fallback": "none", **extra})
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "toolsets")
    assert anthropic.authority.sessions == {}


@pytest.mark.asyncio
async def test_a_locked_turn_exposes_no_tools_whatever_the_profile_enables(anthropic):
    anthropic.config(platform_toolsets={"cli": ["delegation", "cronjob", "kanban", "vision", "moa"]},
                     agent={"disabled_toolsets": []})
    ref = anthropic.create(fallback="none")
    await anthropic.call("session.resume", session_id=ref.session_id)
    await anthropic.turn(ref, "one")
    agent = anthropic.authority.agent(ref)
    assert agent.route_locked is True and agent.valid_tool_names == set() and not agent.tools


@pytest.mark.asyncio
async def test_a_locked_agent_is_built_with_its_frozen_toolset_whatever_the_turn_resolves(anthropic, monkeypatch):
    monkeypatch.setattr("gateway.session_policy.policy_turn_toolsets", lambda *_a: ["delegation", "cronjob", "vision"])
    ref = anthropic.create(fallback="none")
    await anthropic.call("session.resume", session_id=ref.session_id)
    await anthropic.turn(ref, "one")
    agent = anthropic.authority.agent(ref)
    assert agent.enabled_toolsets == [] and agent.valid_tool_names == set()


@pytest.mark.asyncio
async def test_a_toolset_mutation_on_a_locked_session_is_refused(anthropic):
    ref = anthropic.create(fallback="none")
    await anthropic.call("session.resume", session_id=ref.session_id)
    reply = await anthropic.call("session.mutate", session_id=ref.session_id, request_id="m1",
                                 operation="toolsets", payload={"toolsets": ["delegation"]})
    assert "error" in reply, reply
    assert anthropic.policy(ref).toolsets == ()


@pytest.mark.asyncio
async def test_a_profile_toolset_edit_never_reaches_a_locked_sessions_policy(anthropic):
    from gateway.session_policy_refresh import refreshed_policy

    ref = anthropic.create(fallback="none")
    policy = anthropic.policy(ref)
    live = {"platform_toolsets": {"cli": ["delegation", "cronjob"]}, "mcp_servers": {}}
    moved, _secrets, _replaced = refreshed_policy(policy, live, {})
    assert moved.toolsets == policy.toolsets == ()


# --- (3) no live discovery under a lock ---------------------------------------------------------------------


def test_auxiliary_getters_do_no_live_discovery_under_a_lock(monkeypatch):
    import hermes_cli.models_pricing as pricing
    from agent import auxiliary_client as aux
    from agent.auxiliary_route_lock import locked_route
    import providers

    calls = []
    monkeypatch.setattr(pricing, "fetch_models_with_pricing", lambda **kw: calls.append("catalog") or {"x-mini": 1})

    class Profile:
        base_url = "http://127.0.0.1:9/v1"
        default_aux_model = "static-aux"

        def resolve_aux_model(self, **kw):
            calls.append("resolve_aux_model")
            return "live-aux"

        def default_vision_model(self):
            calls.append("default_vision_model")
            return "live-vision"

    monkeypatch.setattr(providers, "get_provider_profile", lambda _name: Profile())
    assert aux._get_aux_model_for_provider("someprov", prefer_fast=True) in {"x-mini", "live-aux"}
    assert aux._resolve_provider_vision_default("someprov") == "live-vision"
    assert calls  # unlocked control: discovery is live
    calls.clear()
    with locked_route(lambda *_a: True):
        assert aux._get_aux_model_for_provider("someprov", prefer_fast=True) == "static-aux"
        assert aux._resolve_provider_vision_default("someprov") is None
    assert calls == []
