"""Route lock, auxiliary calls: a LOCKED session's side calls (title generation, compression, the manual
compress mutation, the session-bound one-shot) never leave the frozen route.

Real owner authority + ``create_local_session {fallback: none}`` + ``prompt.submit``; only the provider's
HTTP edge is stubbed (loopback servers, counted). Title generation is LEFT ENABLED at its default: the
feature each test guards is on. Keys are fake sentinels in a disposable HERMES_HOME.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from tests.waiting import async_wait_until

KEY = "sk-SEN...ck-1"
ALT_KEY = "sk-ALT...ck-9"
BACKUP_KEY = "sk-BAK...ck-8"
QUOTA = {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota",
                   "code": "insufficient_quota"}}


def _kind(body):
    text = json.dumps(body.get("messages") or [])
    if "You name chat sessions" in text:
        return "title"
    if "summarization agent" in text:
        return "summary"
    return "turn"


class _Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("messages"):
            return self._send(200, {"choices": [], "usage": {}})
        kind = _kind(body)
        self.server.seen.append((kind, self.headers.get("Authorization"), body.get("model"), self.path))
        if kind in self.server.refuse:
            return self._send(429, QUOTA)
        frame = {"id": "c", "model": body.get("model"),
                 "usage": {"prompt_tokens": self.server.prompt_tokens, "completion_tokens": 1,
                           "total_tokens": self.server.prompt_tokens + 1},
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": self.server.name},
                              "finish_reason": "stop"}]}
        if body.get("stream"):
            return self._send(200, "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n", "text/event-stream")
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
    server.seen, server.refuse, server.name, server.prompt_tokens = [], set(), name, 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server):
    return f"http://127.0.0.1:{server.server_port}/v1"


def _kinds(server):
    return [seen[0] for seen in server.seen]


class _Transport:
    def __init__(self):
        self.frames = []

    async def write_async(self, frame):
        self.frames.append(frame)
        return True


class Owner:
    def __init__(self, home, primary, alt, backup, authority, connection):
        self.home, self.primary, self.alt, self.backup = home, primary, alt, backup
        self.authority, self.connection = authority, connection

    @property
    def elsewhere(self):
        """Everything any server other than the frozen primary received."""
        return {"alt": self.alt.seen, "backup": self.backup.seen}

    async def call(self, method, **params):
        return await self.connection.dispatch({"id": method, "method": method, "params": params})

    def create(self):
        from gateway.session_local import create_local_session

        return create_local_session(
            self.authority, self.connection.actor,
            {"request_id": "lock", "source": "cli", "cwd": str(self.home), "toolsets": [], "fallback": "none"})

    def rows(self, sid):
        from hermes_state_runtime import list_session_admissions

        return list_session_admissions(self.authority.db, session_id=sid, pending_only=False)

    async def turn(self, ref, text="hello"):
        sid = ref.session_id
        reply = await self.call("prompt.submit", session_id=sid, submission_id=f"s{len(self.rows(sid)) + 1}", text=text)
        assert "result" in reply, reply
        await async_wait_until(
            lambda: self.rows(sid) and all(r["status"] == "terminal" for r in self.rows(sid)),
            timeout=60, what="settled admission")

    def seed(self, ref, pairs=40, chars=4000):
        """Long prior history written straight to the transcript (cheaper than 40 provider turns)."""
        for i in range(pairs):
            self.authority.db.append_message(ref.session_id, "user", f"question {i} " + "x " * (chars // 2))
            self.authority.db.append_message(ref.session_id, "assistant", f"answer {i} " + "y " * (chars // 2))

    async def settle(self, seen_by, timeout=15):
        """Wait until the side call being guarded has been observed somewhere (or time runs out), then
        a short grace for a late stray request."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not seen_by():
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.0)


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    """``await rig(config_fn)``: a real owner whose profile config is ``config_fn(alt_url, backup_url)``
    merged over a deepseek primary on a loopback server with a static sentinel key."""
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB

    servers = [_serve("PRIMARY"), _serve("ALT"), _serve("BACKUP")]
    cleanup = []

    async def build(config_fn=lambda alt, backup: {}):
        primary, alt, backup = servers
        monkeypatch.setattr(run, "_hermes_home", tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = {"model": {"provider": "deepseek", "default": "deepseek-flash", "context_length": 64000},
                  "compression": {"threshold_tokens": 5000, "protect_first_n": 1, "protect_last_n": 2, "min_tail_user_messages": 2}}
        config.update(config_fn(_url(alt), _url(backup)))
        (tmp_path / "config.yaml").write_text(json.dumps(config))
        (tmp_path / ".env").write_text(f"DEEPSEEK_API_KEY={KEY}\nDEEPSEEK_BASE_URL={_url(primary)}\n")
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        db = SessionDB(tmp_path / "state.db")
        runner.session_store._db = runner._session_db = db
        authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id="owner", db=db)
        connection = AuthorityConnection(authority, _Transport(), {"user_id": "owner", "native_bootstrap": True})
        cleanup.append((connection, db))
        return Owner(tmp_path, primary, alt, backup, authority, connection)

    try:
        yield build
    finally:
        for connection, db in cleanup:
            await connection.close()
            db.close()
        for server in servers:
            server.shutdown()
            server.server_close()


def _backup_entry(url):
    return {"provider": "custom", "model": "backup-model", "base_url": url, "api_key": BACKUP_KEY}


def _assert_frozen_route_only(owner):
    """No request left the frozen route: nothing reached another server, and whatever the primary got
    carried the sentinel key and the frozen model."""
    assert owner.elsewhere == {"alt": [], "backup": []}, owner.elsewhere
    assert {(auth, model) for _, auth, model, _ in owner.primary.seen} == {(f"Bearer {KEY}", "deepseek-flash")}


@pytest.mark.asyncio
async def test_title_generation_with_an_alternate_route_never_leaves_the_frozen_route(rig):
    owner = await rig(lambda alt, backup: {  # title generation stays enabled (default)
        "auxiliary": {"title_generation": {
            "provider": "custom", "base_url": alt, "model": "alt-title-model", "api_key": ALT_KEY}}})
    ref = owner.create()
    await owner.call("session.resume", session_id=ref.session_id)
    await owner.turn(ref, "name this conversation please")
    await owner.settle(lambda: owner.alt.seen or "title" in _kinds(owner.primary))
    assert owner.alt.seen == [], f"title call left the frozen route for the alternate server: {owner.alt.seen}"
    _assert_frozen_route_only(owner)
    assert "turn" in _kinds(owner.primary)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["profile_fallback_providers", "aux_fallback_chain"])
async def test_a_title_call_refused_by_the_primary_does_not_fall_back_to_the_backup(rig, where):
    def config(alt, backup):
        if where == "profile_fallback_providers":
            return {"fallback_providers": [_backup_entry(backup)]}
        return {"auxiliary": {"title_generation": {"fallback_chain": [_backup_entry(backup)]}}}

    owner = await rig(config)
    owner.primary.refuse = {"title"}
    ref = owner.create()
    await owner.call("session.resume", session_id=ref.session_id)
    await owner.turn(ref, "name this conversation please")
    await owner.settle(lambda: owner.backup.seen or "title" in _kinds(owner.primary))
    assert owner.backup.seen == [], f"429 title call fell back to the backup server: {owner.backup.seen}"
    _assert_frozen_route_only(owner)


@pytest.mark.asyncio
async def test_a_compression_summary_refused_by_the_primary_does_not_fall_back_to_the_backup(rig):
    owner = await rig(lambda alt, backup: {"fallback_providers": [_backup_entry(backup)]})
    owner.primary.refuse = {"summary"}
    owner.primary.prompt_tokens = 100_000  # real usage anchors the next turn's preflight estimate
    ref = owner.create()
    await owner.call("session.resume", session_id=ref.session_id)
    owner.seed(ref)
    await owner.turn(ref, "one more question")
    await owner.turn(ref, "and another")
    # Guard against a vacuous pass: compression must really have been attempted on the frozen route.
    assert "summary" in _kinds(owner.primary), f"compression never ran: {_kinds(owner.primary)}"
    await owner.settle(lambda: owner.backup.seen)
    assert owner.backup.seen == [], f"429 compression call fell back to the backup server: {owner.backup.seen}"
    _assert_frozen_route_only(owner)


def _reason(reply):
    return ((reply or {}).get("error") or {}).get("data", {}).get("reason")


@pytest.mark.asyncio
async def test_the_manual_compress_mutation_is_refused_on_a_locked_session(rig):
    owner = await rig(lambda alt, backup: {"auxiliary": {"compression": {
        "provider": "custom", "base_url": alt, "model": "alt-summary-model", "api_key": ALT_KEY}}})
    ref = owner.create()
    resumed = (await owner.call("session.resume", session_id=ref.session_id))["result"]
    owner.seed(ref)
    resumed = (await owner.call("session.resume", session_id=ref.session_id))["result"]
    reply = await owner.call(
        "session.mutate", session_id=ref.session_id, request_id="compress", operation="compress", payload={},
        expected_revision=resumed["revision"], expected_generation=resumed["execution_generation"])
    await owner.settle(lambda: owner.alt.seen or owner.primary.seen, timeout=5)
    assert _reason(reply) == "route_lock_mismatch", (reply, owner.elsewhere, owner.primary.seen)
    assert owner.primary.seen == [] and owner.elsewhere == {"alt": [], "backup": []}


@pytest.mark.asyncio
async def test_the_session_bound_oneshot_is_refused_on_a_locked_session(rig):
    owner = await rig()
    ref = owner.create()
    await owner.call("session.resume", session_id=ref.session_id)
    transport = owner.connection.transport
    reply = await owner.call("llm.oneshot", session_id=ref.session_id, input="say hi")
    if reply is None:  # admitted as a deferred reply: its answer is a frame on the transport
        await owner.settle(lambda: transport.frames and owner.primary.seen, timeout=10)
        reply = transport.frames[-1] if transport.frames else None
    assert _reason(reply) == "route_lock_mismatch", (reply, transport.frames, owner.primary.seen)
    assert owner.primary.seen == [] and owner.elsewhere == {"alt": [], "backup": []}
