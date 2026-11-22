"""Route-lock B2: the API server refuses to run, copy or project a route-locked session.

Real aiohttp handlers over a real SessionDB with a real committed LOCAL receipt (hand-built receipt via
``commit_local_session``, as in ``test_route_lock_content_boundary``). Only the provider edge
(``_run_agent`` / ``_create_agent``) is replaced, and only to record that it was reached.
"""
import json
import sqlite3
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from gateway.session_policy import LocalSessionPolicy
from gateway.session_route_lock import FIELDS
from hermes_state import SessionDB
from hermes_state_keys import local_identity
from hermes_state_local import commit_local_session
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": "Bearer sk-secret"}
OK = ({"final_response": "ok", "messages": [], "api_calls": 1},
      {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


def _record(db, tmp_path, name, *, lock):
    profile, principal, request = str(tmp_path / name), "local:test", f"create-{name}"
    owner = local_identity(profile, principal, request)
    route = f"agent:main:local:dm:{owner}"
    origin = dict(platform="local", chat_id=owner, chat_type="dm", user_id=principal)
    policy = LocalSessionPolicy(
        "tui", "tui", str(tmp_path), "test-model", (), "{}", '{"fallback":"none"}' if lock else "{}",
        json.dumps({"TERMINAL_CWD": str(tmp_path)}),
        route_lock_json=json.dumps(dict.fromkeys((*FIELDS, "salt", "credential_id"), "test")) if lock else None)
    receipt = dict(profile_id=profile, principal_id=principal, request_id=request, session_id=owner, route=route,
                   policy=asdict(policy), entry=dict(session_id=owner, session_key=route, origin=origin,
                                                     created_at="2026-11-21T00:00:00+00:00",
                                                     updated_at="2026-11-21T00:00:00+00:00"))
    commit_local_session(db, epoch=begin_runtime_epoch(db, instance_id="test"), receipt=receipt)
    db.append_message(owner, role="user", content=f"secret-{name}")
    db.append_message(owner, role="assistant", content=f"reply-{name}")
    return owner


def _snapshot(db):
    """Every row the API could have written: sessions, messages and the receipts."""
    with db._read_ctx() as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()]
                for t in ("sessions", "messages", "state_meta")}


@pytest.fixture
def env(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-secret"}))
        adapter._session_db = db
        adapter._run_agent = AsyncMock(return_value=OK)
        adapter._create_agent = MagicMock(side_effect=AssertionError("agent built"))
        app = web.Application()
        app["api_server_adapter"] = adapter
        for method, path, handler in (
                ("POST", "/api/sessions/{session_id}/fork", adapter._handle_fork_session),
                ("POST", "/api/sessions/{session_id}/chat", adapter._handle_session_chat),
                ("POST", "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream),
                ("GET", "/api/sessions/{session_id}", adapter._handle_get_session),
                ("GET", "/api/sessions/{session_id}/messages", adapter._handle_session_messages),
                ("POST", "/v1/chat/completions", adapter._handle_chat_completions),
                ("POST", "/v1/responses", adapter._handle_responses),
                ("POST", "/v1/runs", adapter._handle_runs)):
            app.router.add_route(method, path, handler)
        yield adapter, db, app, _record(db, tmp_path, "locked", lock=True), _record(db, tmp_path, "open", lock=False)


async def _refused(resp, db, before, adapter):
    body = await resp.text()
    assert resp.status == 409, body
    assert json.loads(body)["error"]["code"] == "route_lock_mismatch"
    assert "secret-locked" not in body
    assert _snapshot(db) == before  # no row written, nothing ended, receipt kept
    adapter._run_agent.assert_not_called()
    adapter._create_agent.assert_not_called()


def _request(kind, sid):
    return {
        "fork": ("POST", f"/api/sessions/{sid}/fork", {"id": "fork-child"}, {}),
        "chat": ("POST", f"/api/sessions/{sid}/chat", {"message": "hi"}, {}),
        "stream": ("POST", f"/api/sessions/{sid}/chat/stream", {"message": "hi"}, {}),
        "completions": ("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]},
                        {"X-Hermes-Session-Id": sid}),
        "runs": ("POST", "/v1/runs", {"input": "hi", "session_id": sid}, {}),
        "responses": ("POST", "/v1/responses", {"input": "hi", "previous_response_id": "resp_prev"}, {}),
    }[kind]


@pytest.mark.parametrize("kind", ["fork", "chat", "stream", "completions", "runs", "responses"])
async def test_locked_session_refused_before_any_write_or_provider_work(env, kind):
    adapter, db, app, locked, _ = env
    # a stored response chain that points at the locked session (the only way /v1/responses names one)
    adapter._current_response_store().put("resp_prev", {"session_id": locked, "conversation_history": []})
    before = _snapshot(db)
    method, path, body, headers = _request(kind, locked)
    async with TestClient(TestServer(app)) as cli:
        await _refused(await cli.request(method, path, json=body, headers={**AUTH, **headers}), db, before, adapter)
    assert db.get_session("fork-child") is None


async def test_locked_wake_refused_before_provider_work(env):
    adapter, db, _, locked, _ = env
    before = _snapshot(db)
    token = _api_request_profile.set("default")
    try:
        with pytest.raises(RuntimeStoreError) as refused:
            await adapter.run_internal_session_turn(session_id=locked, text="wake", profile="default")
    finally:
        _api_request_profile.reset(token)
    assert refused.value.reason == "route_lock_mismatch"
    assert _snapshot(db) == before
    adapter._run_agent.assert_not_called()


@pytest.mark.parametrize("kind", ["chat", "stream", "completions"])
async def test_unverifiable_policy_fails_closed_not_to_empty_history(env, kind):
    adapter, db, app, _, open_ = env
    before = _snapshot(db)
    method, path, body, headers = _request(kind, open_)
    broken = RuntimeStoreError("storage_unavailable")
    with patch("hermes_state_local.require_legacy_execution", side_effect=broken):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.request(method, path, json=body, headers={**AUTH, **headers})
            text = await resp.text()
    assert resp.status == 503, text
    assert json.loads(text)["error"]["code"] == "storage_unavailable"
    assert _snapshot(db) == before
    adapter._run_agent.assert_not_called()


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("locked"), OSError("io")])
async def test_unexpected_verifier_error_fails_closed(env, failure):
    adapter, _, app, _, open_ = env
    with patch("hermes_state_local.require_legacy_execution", side_effect=failure):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{open_}/chat", json={"message": "hi"}, headers=AUTH)
            assert resp.status == 503, await resp.text()
    adapter._run_agent.assert_not_called()


async def test_content_read_refusal_is_not_swallowed_into_empty_history(env):
    """Second layer, independent of the execution fence: B1's content refusal must not become ``[]``."""
    adapter, _, app, locked, _ = env
    with patch("hermes_state_local.require_legacy_execution"):  # fence gone: the read guard alone
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{locked}/chat", json={"message": "hi"}, headers=AUTH)
            assert resp.status == 409, await resp.text()
    adapter._run_agent.assert_not_called()


@pytest.mark.parametrize("kind", ["chat", "completions"])
async def test_unlocked_control_runs_with_its_own_history(env, kind):
    adapter, _, app, _, open_ = env
    method, path, body, headers = _request(kind, open_)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.request(method, path, json=body, headers={**AUTH, **headers})
        assert resp.status == 200, await resp.text()
    history = adapter._run_agent.await_args.kwargs["conversation_history"]
    assert [m["content"] for m in history] == ["secret-open", "reply-open"]


async def test_unlocked_control_forks_and_runs(env):
    _, db, app, _, open_ = env
    async with TestClient(TestServer(app)) as cli:
        fork = await cli.post(f"/api/sessions/{open_}/fork", json={"id": "fork-child"}, headers=AUTH)
        assert fork.status == 201, await fork.text()
        run = await cli.post("/v1/runs", json={"input": "hi", "session_id": open_}, headers=AUTH)
        assert run.status == 202, await run.text()
    assert [m["content"] for m in db.get_messages("fork-child")] == ["secret-open", "reply-open"]


async def test_get_projections_refuse_or_withhold_locked_content(env):
    _, _, app, locked, open_ = env
    async with TestClient(TestServer(app)) as cli:
        messages = await cli.get(f"/api/sessions/{locked}/messages", headers=AUTH)
        text = await messages.text()
        assert messages.status == 409, text
        assert json.loads(text)["error"]["code"] == "route_lock_mismatch"
        assert "secret-locked" not in text
        meta = await cli.get(f"/api/sessions/{locked}", headers=AUTH)
        assert meta.status == 200
        assert (await meta.json())["session"]["title"] is None
        ok = await cli.get(f"/api/sessions/{open_}/messages", headers=AUTH)
        assert ok.status == 200
        assert [m["content"] for m in (await ok.json())["data"]] == ["secret-open", "reply-open"]
