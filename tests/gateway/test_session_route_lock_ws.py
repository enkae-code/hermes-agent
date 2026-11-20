"""Route lock over Pagestone's transport: the real ``tui_gateway.ws.handle_ws`` route (scripted socket),
the real authority, ``session.create {source: acp, fallback: none}`` -> ``runtime_policy`` revision ->
``prompt.submit {expected_policy_revision}``. Only the provider's HTTP edge is stubbed (loopback servers);
keys are fake sentinels in a disposable HERMES_HOME."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.waiting import async_wait_until
from tui_gateway import ws as ws_mod

KEY = "sk-sentinel-route-lock-ws-0000"


class _Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Tolerates extra calls (title generation etc.); the test counts only /chat/completions.
        self.server.seen.append((self.headers.get("Authorization"), body.get("model"), self.path))
        self.server.bodies.append(body)
        frame = {
            "id": "c",
            "model": body.get("model"),
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": self.server.name}, "finish_reason": "stop"}],
        }
        if body.get("stream"):
            data, kind = "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n", "text/event-stream"
        else:
            frame["choices"][0]["message"] = frame["choices"][0].pop("delta")
            data, kind = json.dumps(frame), "application/json"
        data = data.encode()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(name):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    server.seen, server.bodies, server.name = [], [], name
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _chats(server):
    return [s for s in server.seen if s[2].endswith("/chat/completions")]


def _turns(server):
    """Agent-turn requests: the chat completions that are not the default title generator's."""
    return [
        s
        for s, b in zip(server.seen, server.bodies)
        if s[2].endswith("/chat/completions") and not str(b["messages"][0]["content"]).startswith("You name chat sessions")
    ]


class _Socket:
    """The ASGI WebSocket surface ``handle_ws`` uses, scripted: a client queue in, a frame list out."""

    def __init__(self, authority):
        self.scope = {"hermes.session_authority": authority}
        self.inbound, self.frames = asyncio.Queue(), []

    async def accept(self, *a, **kw):
        pass

    async def close(self, *a, **kw):
        pass

    async def send_text(self, line):
        self.frames.append(json.loads(line))

    async def receive_text(self):
        frame = await self.inbound.get()
        if frame is None:
            raise ws_mod._WebSocketDisconnect()
        return json.dumps(frame)

    async def call(self, method, **params):
        rid = f"{method}-{len(self.frames)}-{self.inbound.qsize()}"
        self.inbound.put_nowait({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        box = []
        await async_wait_until(
            lambda: box.append(next((f for f in self.frames if f.get("id") == rid), None)) or box[-1] is not None,
            timeout=60,
            what=f"reply to {method}",
        )
        return box[-1]


@pytest.mark.asyncio
async def test_route_lock_holds_over_the_websocket_route(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_state import SessionDB
    from hermes_state_runtime import list_session_admissions

    primary, backup = _serve("PRIMARY"), _serve("BACKUP")
    url = lambda s: f"http://127.0.0.1:{s.server_port}/v1"
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        json.dumps(
            {
                "model": {"provider": "deepseek", "default": "deepseek-flash"},
                "fallback_providers": [
                    {"provider": "custom", "model": "backup-model", "base_url": url(backup), "api_key": "sk-backup-sentinel"}
                ],
            }
        )
    )
    (tmp_path / ".env").write_text(f"DEEPSEEK_API_KEY={KEY}\nDEEPSEEK_BASE_URL={url(primary)}\n")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id="owner", db=db)
    sock = _Socket(authority)
    handle = asyncio.create_task(
        ws_mod.handle_ws(sock, auth_identity={"user_id": "owner", "native_bootstrap": True})
    )
    rows = lambda sid: list_session_admissions(db, session_id=sid, pending_only=False)
    try:
        await async_wait_until(
            lambda: any((f.get("params") or {}).get("type") == "gateway.ready" for f in sock.frames),
            timeout=30,
            what="gateway.ready",
        )

        # (1) create over the socket
        created = await sock.call(
            "session.create", request_id="ws-lock", source="acp", fallback="none", cwd=str(tmp_path), toolsets=[]
        )
        assert "result" in created, created
        sid = created["result"]["session_id"]

        # (2) the revision the getter reports for this session
        got = await sock.call("config.get", session_id=sid, key="runtime_policy")
        assert "result" in got, got
        revision = got["result"]["runtime_policy"]["revision"]

        # (3) a turn carrying it, (4) settles with exactly one frozen-route request
        sent = await sock.call(
            "prompt.submit", session_id=sid, submission_id="s1", text="hello", expected_policy_revision=revision
        )
        assert "result" in sent, sent
        await async_wait_until(
            lambda: rows(sid) and all(r["status"] == "terminal" for r in rows(sid)),
            timeout=60,
            what="settled admission",
        )
        assert [(a, m) for a, m, _ in _turns(primary)] == [(f"Bearer {KEY}", "deepseek-flash")]
        # Anything else the provider saw (title generation) rode the same frozen route.
        assert {(a, m) for a, m, _ in _chats(primary)} == {(f"Bearer {KEY}", "deepseek-flash")}
        assert backup.seen == []

        # (5) a stale revision is refused, nothing more is sent
        stale = await sock.call(
            "prompt.submit", session_id=sid, submission_id="s2", text="again", expected_policy_revision="0" * 16
        )
        assert stale["error"]["data"]["reason"] == "route_lock_mismatch", stale
        assert len(rows(sid)) == 1 and len(_turns(primary)) == 1 and backup.seen == []
    finally:
        sock.inbound.put_nowait(None)
        await asyncio.wait_for(handle, 30)
        db.close()
        for server in (primary, backup):
            server.shutdown()
            server.server_close()
