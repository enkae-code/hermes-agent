"""Route lock (``session.create {fallback: "none"}``): creation refuses any primary route whose
selected credential is not a static API key, judged by the resolver that execution uses."""

from types import SimpleNamespace

import pytest

from gateway import session_local
from gateway.session_policy import build_policy
from hermes_state_runtime import RuntimeStoreError

_CFG = {"platform_toolsets": {"cli": ["terminal"]}}


def test_fallback_none_is_the_only_accepted_fallback_value(tmp_path):
    with pytest.raises(RuntimeStoreError) as caught:
        build_policy({"cwd": str(tmp_path), "fallback": "any"}, dict(_CFG))
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    build_policy({"cwd": str(tmp_path), "fallback": "none"}, dict(_CFG))


@pytest.mark.parametrize(
    "kind, refused", [("api_key", False), ("unknown", True), ("oauth", True)]
)
def test_create_refuses_unless_the_resolver_selected_a_static_key(
    monkeypatch, kind, refused
):
    seen = {}

    def fake_route_runtime(policy, frozen, model, key, *, fallback=True):
        seen.update(key=key, fallback=fallback)
        return {"credential_kind": kind, "provider": "openrouter"}, None

    import gateway.session_worker_construct as worker

    monkeypatch.setattr(worker, "route_runtime", fake_route_runtime)
    policy = SimpleNamespace(
        effective=lambda authority=None: ({"model": {"provider": "openrouter"}}, "m")
    )
    import gateway.session_policy as sp

    monkeypatch.setattr(
        sp, "route_launch_key", lambda authority, policy, frozen: "LAUNCH-KEY"
    )

    if not refused:
        session_local._refuse_unless_static_key(None, policy)
        assert seen == {"key": "LAUNCH-KEY", "fallback": False}
        return
    with pytest.raises(RuntimeStoreError) as caught:
        session_local._refuse_unless_static_key(None, policy)
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    assert seen["fallback"] is False


# --- the lock end to end: real owner, session.create -> prompt.submit -> FIFO drain -> turn runner ------------
# Only the provider's HTTP edge is stubbed (a loopback server); keys are fake sentinels in a disposable home.
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest_asyncio

from tests.waiting import async_wait_until

KEY = "sk-SENTINEL-route-lock-1"
OTHER_KEY = "sk-SENTINEL-route-lock-2"


class _Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("messages"):
            return self._send(200, {"choices": [], "usage": {}})
        self.server.seen.append(
            (self.headers.get("Authorization"), body.get("model"), self.path)
        )
        self.server.gate.wait(30)
        if self.server.refuse:
            return self._send(
                429,
                {
                    "error": {
                        "message": "You exceeded your current quota",
                        "type": "insufficient_quota",
                        "code": "insufficient_quota",
                    }
                },
            )
        frame = {
            "id": "c",
            "model": body.get("model"),
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": self.server.name},
                    "finish_reason": "stop",
                }
            ],
        }
        if body.get("stream"):
            return self._send(
                200,
                "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n",
                "text/event-stream",
            )
        frame["choices"][0]["message"] = frame["choices"][0].pop("delta")
        self._send(200, frame)

    def _send(self, status, body, kind="application/json"):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(name):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    server.seen, server.refuse, server.name, server.gate = (
        [],
        False,
        name,
        threading.Event(),
    )
    server.gate.set()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _chats(server):
    return [seen for seen in server.seen if seen[2].endswith("/chat/completions")]


class Owner:
    def __init__(self, home, primary, backup, authority, connection, monkeypatch):
        self.home, self.primary, self.backup = home, primary, backup
        self.authority, self.connection, self.monkeypatch = (
            authority,
            connection,
            monkeypatch,
        )

    def remove_credential(self):
        """Every source the resolver reads: the profile .env, the process env it was loaded into, the pool."""
        self.env(DEEPSEEK_API_KEY=None)
        self.monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        (self.home / "auth.json").unlink(missing_ok=True)

    def env(self, **values):
        path = self.home / ".env"
        lines = {
            k: v
            for k, v in (
                line.split("=", 1)
                for line in path.read_text().splitlines()
                if "=" in line
            )
        }
        lines.update(values)
        path.write_text(
            "".join(f"{k}={v}\n" for k, v in lines.items() if v is not None)
        )

    def config(self, **sections):
        path = self.home / "config.yaml"
        path.write_text(json.dumps({**json.loads(path.read_text()), **sections}))

    async def call(self, method, **params):
        return await self.connection.dispatch(
            {"id": method, "method": method, "params": params}
        )

    def create(self, request_id="lock", **extra):
        from gateway.session_local import create_local_session

        return create_local_session(
            self.authority,
            self.connection.actor,
            {
                "request_id": request_id,
                "source": "cli",
                "cwd": str(self.home),
                "toolsets": [],
                **extra,
            },
        )

    async def turn(self, ref, text="hello", **extra):
        sid = ref.session_id
        reply = await self.call(
            "prompt.submit",
            session_id=sid,
            submission_id=f"s{len(self.rows(sid)) + 1}",
            text=text,
            **extra,
        )
        assert "result" in reply, reply
        count = len(self.rows(sid))
        await async_wait_until(
            lambda: (
                len(self.rows(sid)) >= count
                and all(r["status"] == "terminal" for r in self.rows(sid))
            ),
            timeout=60,
            what="settled admission",
        )
        return self.rows(sid)[-1]

    def rows(self, sid):
        from hermes_state_runtime import list_session_admissions

        return list_session_admissions(
            self.authority.db, session_id=sid, pending_only=False
        )

    def policy(self, ref):
        from gateway.session_policy import restore_policy
        from hermes_state_local import local_receipt

        return restore_policy(
            local_receipt(self.authority.db, ref.session_id)["policy"]
        )


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB

    primary, backup = _serve("PRIMARY"), _serve("BACKUP")
    url = lambda server: f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        json.dumps(
            {
                "model": {"provider": "deepseek", "default": "deepseek-flash"},
                "auxiliary": {"title_generation": {"enabled": False}},
            }
        )
    )
    (tmp_path / ".env").write_text(
        f"DEEPSEEK_API_KEY={KEY}\nDEEPSEEK_BASE_URL={url(primary)}\n"
    )
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(
        runner, profile_id=str(tmp_path), instance_id="owner", db=db
    )
    connection = AuthorityConnection(
        authority, object(), {"user_id": "owner", "native_bootstrap": True}
    )
    handle = Owner(tmp_path, primary, backup, authority, connection, monkeypatch)
    handle.backup_url = url(backup)
    try:
        yield handle
    finally:
        await connection.close()
        db.close()
        for server in (primary, backup):
            server.shutdown()
            server.server_close()


@pytest.mark.asyncio
async def test_locked_turns_reach_exactly_the_frozen_route_fresh_and_cached(owner):
    ref = owner.create(fallback="none")
    lock = json.loads(owner.policy(ref).route_lock_json)
    assert (lock["provider"], lock["model"], lock["credential_kind"]) == (
        "deepseek",
        "deepseek-flash",
        "api_key",
    )
    assert lock["base_url"].startswith("http://127.0.0.1:") and KEY not in json.dumps(
        lock
    )
    await owner.call("session.resume", session_id=ref.session_id)
    await owner.turn(ref, "one")  # fresh agent
    await owner.turn(ref, "two")  # the cached agent
    assert [(auth, model) for auth, model, _ in _chats(owner.primary)] == [
        (f"Bearer {KEY}", "deepseek-flash")
    ] * 2
    assert owner.backup.seen == []


@pytest.mark.asyncio
async def test_the_frozen_route_is_private_to_the_policy(owner):
    ref = owner.create(fallback="none")
    policy = owner.policy(ref)
    lock = json.loads(policy.route_lock_json)
    assert (
        "route_lock" not in policy.request_json
        and lock["credential_id"] not in policy.request_json
    )
    assert lock["credential_id"] != KEY and len(lock["credential_id"]) == 64
    from gateway.session_policy_credentials import policy_identity
    from dataclasses import replace

    assert policy_identity(policy) == policy_identity(
        replace(policy, route_lock_json=None)
    )  # not hashed into a binding


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env, config",
    [
        ({"DEEPSEEK_API_KEY": None}, {}),  # no credential: an AuthError
        (
            {"ANTHROPIC_TOKEN": "sk-ant-oat-SENTINEL"},  # a subscription token
            {"model": {"provider": "anthropic", "default": "claude-sonnet-4"}},
        ),
        (
            {
                "DEEPSEEK_API_KEY": None,
                "OPENAI_API_KEY": "sk-or-SENTINEL-legacy",
            },  # OpenRouter's legacy key path
            {
                "model": {
                    "provider": "openrouter",
                    "default": "anthropic/claude-sonnet-4",
                }
            },
        ),
    ],
    ids=["no-credential", "subscription-token", "alternate-openrouter-credential"],
)
async def test_create_through_the_owner_refuses_a_non_static_key_and_makes_no_session(
    owner, env, config
):
    from hermes_state_runtime import RuntimeStoreError

    owner.env(**env)
    owner.config(**config)
    with pytest.raises(RuntimeStoreError) as caught:
        owner.create(fallback="none")
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    assert owner.authority.sessions == {}


@pytest.mark.asyncio
async def test_create_through_the_real_resolver_refuses_a_jwt_and_a_cc_oauth_token(owner):
    """A JWT-shaped ANTHROPIC_TOKEN beside a cc- CLAUDE_CODE_OAUTH_TOKEN: neither is a static key, whichever
    the real ``resolve_runtime_provider`` picks (no resolver stub)."""
    from hermes_state_runtime import RuntimeStoreError

    owner.env(ANTHROPIC_TOKEN="eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", CLAUDE_CODE_OAUTH_TOKEN="cc-SENTINEL-oauth")
    owner.config(model={"provider": "anthropic", "default": "claude-sonnet-4"})
    with pytest.raises(RuntimeStoreError) as caught:
        owner.create(fallback="none")
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    assert caught.value.detail == "the primary credential is not a static API key"  # resolved, then refused (not an AuthError)
    assert owner.authority.sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["key", "base_url", "credential_removed"])
async def test_a_route_that_moved_after_create_refuses_the_turn_without_dispatch(
    owner, change, caplog
):
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    await owner.turn(ref, "ok")
    sent = len(owner.primary.seen)
    if change == "credential_removed":
        owner.remove_credential()
    else:
        owner.env(
            **{
                "key": {"DEEPSEEK_API_KEY": OTHER_KEY},
                "base_url": {"DEEPSEEK_BASE_URL": owner.backup_url},
            }[change]
        )
    row = await owner.turn(ref, "again")
    assert len(owner.primary.seen) == sent and owner.backup.seen == []
    assert (row["status"], row["outcome"]) == ("terminal", "failed")
    assert (
        "Model resolution failed" in caplog.text
        and "route_lock_mismatch" in caplog.text
    )  # the typed refusal


@pytest.mark.parametrize(
    "name, value",
    [
        ("provider", "openrouter"),
        ("model", "other-model"),
        ("base_url", "http://127.0.0.1:1/v1"),
        ("api_mode", "anthropic_messages"),
        ("credential_kind", "unknown"),
    ],
)
def test_every_frozen_field_is_compared(name, value):
    from gateway import session_route_lock as lock

    runtime = {
        "provider": "deepseek",
        "base_url": "http://127.0.0.1:2/v1",
        "api_mode": "chat_completions",
        "credential_kind": "api_key",
        "api_key": KEY,
    }
    policy = SimpleNamespace(route_lock_json=lock.freeze(runtime, "deepseek-flash"))
    lock.verify(policy, runtime, "deepseek-flash")
    now, model = dict(runtime), "deepseek-flash"
    if name == "model":
        model = value
    else:
        now[name] = value
    with pytest.raises(RuntimeStoreError):
        lock.verify(policy, now, model)
    with pytest.raises(RuntimeStoreError):
        lock.verify(policy, {**runtime, "api_key": OTHER_KEY}, "deepseek-flash")


@pytest.mark.asyncio
async def test_a_locked_session_never_falls_back_on_a_fresh_agent(owner):
    owner.config(
        fallback_providers=[
            {
                "provider": "custom",
                "model": "backup-model",
                "base_url": owner.backup_url,
                "api_key": "loopback-only",
            }
        ]
    )
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    owner.primary.refuse = True
    await owner.turn(ref, "need fallback")
    assert _chats(owner.primary) and owner.backup.seen == []


@pytest.mark.asyncio
async def test_a_locked_session_never_falls_back_on_a_cached_agent(owner):
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    await owner.turn(ref, "prime the cache")
    owner.config(
        fallback_providers=[
            {
                "provider": "custom",
                "model": "backup-model",
                "base_url": owner.backup_url,
                "api_key": "loopback-only",
            }
        ]
    )
    cached = next(iter(owner.authority.runner._agent_cache.values()))[0]
    cached._fallback_activated = True  # as a primary failure would leave it
    owner.primary.refuse = True
    await owner.turn(ref, "need fallback")
    assert owner.backup.seen == []


@pytest.mark.asyncio
async def test_a_locked_session_in_a_managed_worker_is_refused_before_dispatch(
    owner, monkeypatch
):
    from gateway import session_managed_worker

    started = []
    monkeypatch.setattr(
        session_managed_worker, "execute_managed", lambda *a, **k: started.append(a)
    )
    ref = owner.create(
        fallback="none",
        ignore_user_config=True,
        model="deepseek-flash",
        provider="deepseek",
    )
    await owner.call("session.resume", session_id=ref.session_id)
    row = await owner.turn(ref, "hi")
    assert started == [] and owner.primary.seen == [] and row["status"] == "terminal"


@pytest.mark.asyncio
async def test_expected_policy_revision_fences_a_locked_session(owner):
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    got = await owner.call(
        "config.get", session_id=ref.session_id, key="runtime_policy"
    )
    revision = got["result"]["runtime_policy"]["revision"]
    stale = await owner.call(
        "prompt.submit",
        session_id=ref.session_id,
        submission_id="stale",
        text="x",
        expected_policy_revision="0" * 16,
    )
    assert "error" in stale and owner.rows(ref.session_id) == []
    fresh = await owner.call(
        "prompt.submit",
        session_id=ref.session_id,
        submission_id="fresh",
        text="x",
        expected_policy_revision=revision,
    )
    assert fresh["result"]["status"] == "queued"


@pytest.mark.asyncio
async def test_the_getter_reports_the_frozen_route_and_reads_nothing_else(
    owner, monkeypatch
):
    import sys
    from hermes_cli import auth, auth_plugin_providers

    def forbidden(*args, **kwargs):
        raise AssertionError("the getter reached the provider layer")

    ref = owner.create(fallback="none")
    plain = owner.create("plain")
    for name in ("read_credential_pool", "_save_auth_store", "_load_auth_store"):
        monkeypatch.setattr(auth, name, forbidden)
    monkeypatch.setattr(
        auth_plugin_providers, "sync_plugin_provider_registry", forbidden
    )
    loaded = {m for m in sys.modules if m.startswith("plugins.model_providers")}
    locked = (
        await owner.call("config.get", session_id=ref.session_id, key="runtime_policy")
    )["result"]["runtime_policy"]
    unlocked = (
        await owner.call(
            "config.get", session_id=plain.session_id, key="runtime_policy"
        )
    )["result"]["runtime_policy"]
    bare = (await owner.call("config.get", key="runtime_policy"))["result"][
        "runtime_policy"
    ]
    assert {m for m in sys.modules if m.startswith("plugins.model_providers")} == loaded
    assert locked["primary"] == {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "credential_kind": "api_key",
        "endpoint_host": "127.0.0.1",
    }
    for view in (unlocked, bare):
        assert (
            view["primary"]["credential_kind"] == "unknown"
            and view["complete"] is False
        )
    assert locked["complete"] is True
    lock = json.loads(owner.policy(ref).route_lock_json)
    for text in (json.dumps(locked), json.dumps(unlocked), json.dumps(bare)):
        assert (
            KEY not in text
            and lock["credential_id"] not in text
            and lock["salt"] not in text
        )


@pytest.mark.asyncio
async def test_no_exported_surface_carries_the_key_or_its_identity(owner):
    ref = owner.create(fallback="none")
    sid = ref.session_id
    lock = json.loads(owner.policy(ref).route_lock_json)
    await owner.call("session.resume", session_id=sid)
    await owner.turn(ref, "hello")
    surfaces = [
        await owner.call("session.describe")
        if False
        else await owner.call("runtime.describe"),
        await owner.call("session.info", session_id=sid),
        await owner.call("session.resume", session_id=sid),
    ]
    for key in ("runtime_policy", "full", "mtime", "model", "project"):
        surfaces.append(await owner.call("config.get", session_id=sid, key=key))
    surfaces.append(await owner.call("session.list"))
    surfaces.append(owner.authority.db.export_session(sid))
    surfaces.append(
        await owner.call("session.events.since", session_id=sid, sequence=0)
    )
    text = json.dumps(surfaces, default=str)
    for secret in (KEY, lock["credential_id"], lock["salt"], "route_lock_json"):
        assert secret not in text, secret


@pytest.mark.parametrize("locked, typed", [(True, True), (False, False)])
def test_a_resolver_error_is_the_typed_refusal_only_for_a_locked_session(
    monkeypatch, locked, typed
):
    from gateway import session_route_lock, run_turn_prepare
    from gateway.session_authorities import active_authority
    from hermes_cli.auth import AuthError
    import gateway.session_worker_construct as worker

    def unresolvable(*args, **kwargs):
        raise AuthError(
            "No usable credentials", provider="deepseek", code="missing_api_key"
        )

    monkeypatch.setattr(worker, "route_runtime", unresolvable)
    monkeypatch.setattr(
        "gateway.session_authorities.active_authority", lambda runner: None
    )
    monkeypatch.setattr("gateway.session_policy.route_launch_key", lambda *a: None)
    policy = SimpleNamespace(
        effective=lambda authority=None: ({"model": {"provider": "deepseek"}}, "m"),
        route_lock_json="{}" if locked else None,
    )
    expected = RuntimeStoreError if typed else AuthError
    with pytest.raises(expected) as caught:
        run_turn_prepare._resolve_policy_agent_runtime(SimpleNamespace(), policy)
    assert (getattr(caught.value, "reason", None) == "route_lock_mismatch") is typed
    assert session_route_lock.is_locked(policy) is locked


@pytest.mark.parametrize("locked", [True, False])
def test_the_turn_resolver_walks_no_fallback_chain_for_a_locked_session(
    monkeypatch, locked
):
    from gateway import run_turn_prepare, session_route_lock
    import gateway.session_worker_construct as worker

    seen = {}

    def resolver(policy, frozen, model, key, *, fallback=True):
        seen["fallback"] = fallback
        return {
            "provider": "deepseek",
            "credential_kind": "api_key",
            "api_key": KEY,
            "base_url": "u",
            "api_mode": "chat_completions",
        }, None

    monkeypatch.setattr(worker, "route_runtime", resolver)
    monkeypatch.setattr(
        "gateway.session_authorities.active_authority", lambda runner: None
    )
    monkeypatch.setattr("gateway.session_policy.route_launch_key", lambda *a: KEY)
    monkeypatch.setattr(session_route_lock, "verify", lambda *a: None)
    policy = SimpleNamespace(
        effective=lambda authority=None: ({"model": {"provider": "deepseek"}}, "m"),
        route_lock_json="{}" if locked else None,
    )
    run_turn_prepare._resolve_policy_agent_runtime(SimpleNamespace(), policy)
    assert seen["fallback"] is (not locked)


@pytest.mark.asyncio
async def test_queued_work_is_checked_when_it_is_claimed_not_when_it_was_submitted(
    owner,
):
    ref = owner.create(fallback="none")
    sid = ref.session_id
    await owner.call("session.resume", session_id=sid)
    owner.primary.gate.clear()  # the first turn stalls at the provider, so the second queues behind it
    for name in ("first", "second"):
        assert "result" in await owner.call(
            "prompt.submit", session_id=sid, submission_id=name, text=name
        )
    await async_wait_until(
        lambda: len(_chats(owner.primary)) == 1,
        timeout=30,
        what="first turn at the provider",
    )
    owner.env(
        DEEPSEEK_API_KEY=OTHER_KEY
    )  # the route moves while the second is still queued
    owner.monkeypatch.setenv("DEEPSEEK_API_KEY", OTHER_KEY)
    owner.primary.gate.set()
    await async_wait_until(
        lambda: (
            len(owner.rows(sid)) == 2
            and all(r["status"] == "terminal" for r in owner.rows(sid))
        ),
        timeout=60,
        what="both settled",
    )
    assert [auth for auth, _, _ in _chats(owner.primary)] == [
        f"Bearer {KEY}"
    ]  # only the first was ever sent
    assert [r["outcome"] for r in owner.rows(sid)][
        -1
    ] == "failed" and owner.backup.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"model": "deepseek-pro"},
    {"model": "deepseek-pro", "provider": "deepseek"},
    {"model": "alt"},
], ids=["same-provider-other-model", "explicit-provider", "alias-to-backup-endpoint"])
async def test_a_one_turn_model_override_on_a_locked_session_sends_nothing(owner, override):
    owner.config(model_aliases={"alt": {"model": "other-model", "provider": "custom", "base_url": owner.backup_url,
                                        "api_key": "sk-backup-sentinel"}})
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    row = await owner.turn(ref, "hi", model_override=override)
    assert _chats(owner.primary) == [] and owner.backup.seen == []
    assert row["outcome"] != "completed"


@pytest.mark.asyncio
async def test_a_profile_default_change_between_resolve_and_run_is_refused_before_dispatch(owner, monkeypatch):
    ref = owner.create(fallback="none")
    await owner.call("session.resume", session_id=ref.session_id)
    from gateway.session_policy import LocalSessionPolicy
    monkeypatch.setattr(LocalSessionPolicy, "current_model", lambda self, authority: "deepseek-pro")
    await owner.turn(ref, "hi")
    assert _chats(owner.primary) == [] and owner.backup.seen == []


@pytest.mark.parametrize("extra", [{"api_mode": "codex_app_server"}, {"command": "codex"}], ids=["app-server", "external-process"])
def test_the_lock_never_accepts_an_app_server_or_process_route_whatever_its_label(extra):
    from gateway.session_route_lock import static_key

    assert static_key({"credential_kind": "api_key", "api_mode": "chat_completions"})
    assert not static_key({"credential_kind": "api_key", "api_mode": "chat_completions", **extra})
