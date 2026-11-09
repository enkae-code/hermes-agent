"""Peer-UID gate: a loopback TCP client owned by another Unix user is refused before any route runs.

``hermes serve`` on a shared host: any local account can GET ``/`` and read the session token. The
gate asks the kernel which uid owns the client socket (``/proc/net/tcp{,6}``) and must equal ours.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os

import pytest

_TABLE_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
)


def _hex_addr(ip: str) -> str:
    """Kernel /proc encoding: IPv4 one little-endian word; IPv6 four little-endian words."""
    addr = ipaddress.ip_address(ip)
    packed = addr.packed
    if addr.version == 4:
        return packed[::-1].hex().upper()
    return "".join(packed[i:i + 4][::-1].hex().upper() for i in range(0, 16, 4))


def _row(local: tuple[str, int], remote: tuple[str, int], uid: int) -> str:
    return (
        f"   0: {_hex_addr(local[0])}:{local[1]:04X} {_hex_addr(remote[0])}:{remote[1]:04X} "
        f"01 00000000:00000000 00:00000000 00000000 {uid:5d}        0 12345 1 "
        "0000000000000000 100 0 0 10 0"
    )


def _table(*rows: str) -> str:
    return "\n".join([_TABLE_HEADER, *rows]) + "\n"


def test_hex_encoding_matches_kernel_layout():
    """Pin the byte order literally, so the helper cannot silently mirror a parser bug."""
    assert _hex_addr("127.0.0.1") == "0100007F"
    assert _hex_addr("::1") == "00000000000000000000000001000000"
    assert _hex_addr("::ffff:127.0.0.1") == "0000000000000000FFFF00000100007F"


def test_parser_finds_owner_uid_in_tcp_and_tcp6():
    from hermes_cli.web_server_boundary import proc_net_socket_uid

    server = ("127.0.0.1", 9119)
    v4 = _table(
        _row(("127.0.0.1", 40000), server, 1002),  # client socket, owned by uid 1002
        _row(server, ("127.0.0.1", 40000), 1001),  # the server's own end: must not match
    )
    assert proc_net_socket_uid(("127.0.0.1", 40000), server, [v4]) == 1002
    # Kernel reports the mapped client in tcp6 with the same port: still matched.
    v6_mapped = _table(_row(("::ffff:127.0.0.1", 40001), ("::ffff:127.0.0.1", 9119), 1003))
    assert proc_net_socket_uid(("::ffff:127.0.0.1", 40001), ("::ffff:127.0.0.1", 9119), [v6_mapped]) == 1003
    # ::1 client over tcp6.
    v6 = _table(_row(("::1", 40002), ("::1", 9119), 1004))
    assert proc_net_socket_uid(("::1", 40002), ("::1", 9119), [v6]) == 1004
    # Client IP as a mapped string against a plain tcp6 row, and the reverse.
    assert proc_net_socket_uid(("::ffff:127.0.0.1", 40002), ("::1", 9119), [v6]) is None


def test_parser_fails_closed_without_a_matching_row():
    from hermes_cli.web_server_boundary import proc_net_socket_uid

    table = _table(_row(("127.0.0.1", 40000), ("127.0.0.1", 9119), 1002))
    assert proc_net_socket_uid(("127.0.0.1", 40009), ("127.0.0.1", 9119), [table]) is None
    assert proc_net_socket_uid(("127.0.0.1", 40000), ("127.0.0.1", 9120), [table]) is None
    assert proc_net_socket_uid(("127.0.0.1", 40000), None, [table]) is None
    assert proc_net_socket_uid(("127.0.0.1", 40000), ("127.0.0.1", 9119), []) is None


def _uvicorn_server_scope(app):
    """Starlette's WebSocket test transport reports the server as ``testserver``; uvicorn reports the
    bound address. Without this the gate fails closed and the foreign-uid test passes for the wrong reason."""

    async def shim(scope, receive, send):
        if scope["type"] == "websocket":
            scope = {**scope, "server": ("127.0.0.1", 9119)}
        await app(scope, receive, send)

    return shim


@pytest.fixture
def serve_app(monkeypatch):
    import hermes_cli.web_server as ws
    import hermes_cli.web_server_boundary as boundary

    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(boundary.sys, "platform", "linux")
    return ws


def _set_owner(monkeypatch, boundary, uid: int) -> None:
    """Make /proc report the client socket (127.0.0.1:40000 -> server 127.0.0.1:9119) as owned by *uid*."""
    table = _table(_row(("127.0.0.1", 40000), ("127.0.0.1", 9119), uid))
    monkeypatch.setattr(boundary, "_proc_net_tables", lambda: [table])


def test_http_request_from_foreign_uid_is_refused_before_route(serve_app, monkeypatch):
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    _set_owner(monkeypatch, boundary, os.getuid() + 1)
    client = TestClient(serve_app.app, client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    resp = client.get("/api/status", headers={serve_app._SESSION_HEADER_NAME: serve_app._SESSION_TOKEN})
    assert resp.status_code == 403


def test_http_request_from_own_uid_is_served(serve_app, monkeypatch):
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    _set_owner(monkeypatch, boundary, os.getuid())
    client = TestClient(serve_app.app, client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    resp = client.get("/api/status", headers={serve_app._SESSION_HEADER_NAME: serve_app._SESSION_TOKEN})
    assert resp.status_code == 200


def test_websocket_from_foreign_uid_is_closed_before_accept(serve_app, monkeypatch):
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _set_owner(monkeypatch, boundary, os.getuid() + 1)
    client = TestClient(_uvicorn_server_scope(serve_app.app), client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    url = f"/api/events?token={serve_app._SESSION_TOKEN}&channel=peer-uid-test"
    headers = {"Host": "127.0.0.1:9119", "Origin": "http://127.0.0.1:9119"}
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url, headers=headers):
            pass
    assert exc.value.code == 4403


def test_websocket_from_own_uid_is_accepted(serve_app, monkeypatch):
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    _set_owner(monkeypatch, boundary, os.getuid())
    client = TestClient(_uvicorn_server_scope(serve_app.app), client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    url = f"/api/events?token={serve_app._SESSION_TOKEN}&channel=peer-uid-test"
    headers = {"Host": "127.0.0.1:9119", "Origin": "http://127.0.0.1:9119"}
    with client.websocket_connect(url, headers=headers):
        pass


def test_non_loopback_peer_is_not_checked(serve_app, monkeypatch):
    """Only loopback peers are owned by a local account; other peers keep their existing gates."""
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    def _boom():
        raise AssertionError("/proc must not be read for a non-loopback peer")

    monkeypatch.setattr(boundary, "_proc_net_tables", _boom)
    client = TestClient(serve_app.app, client=("192.0.2.7", 40000), base_url="http://127.0.0.1:9119")
    resp = client.get("/api/status", headers={serve_app._SESSION_HEADER_NAME: serve_app._SESSION_TOKEN})
    assert resp.status_code == 200


def test_in_process_selftest_is_not_refused(serve_app, monkeypatch):
    """The dashboard self-test calls the app in-process from a fake loopback client: no socket to
    check, so the gate must not refuse it (it would mark the dashboard as failing every minute)."""
    import hermes_cli.web_server_boundary as boundary
    from hermes_cli.web_server import DASHBOARD_HEALTH

    def _boom():
        raise AssertionError("/proc must not be read for an in-process request")

    monkeypatch.setattr(boundary, "_proc_net_tables", _boom)
    asyncio.run(serve_app._dashboard_selftest_once())
    # This sandbox has no session store, so the route answers 503; the gate's refusal would be 403.
    assert DASHBOARD_HEALTH.selftest_http_status != 403


def test_non_linux_is_unchanged(serve_app, monkeypatch):
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    _set_owner(monkeypatch, boundary, os.getuid() + 1)
    monkeypatch.setattr(boundary.sys, "platform", "darwin")
    client = TestClient(serve_app.app, client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    resp = client.get("/api/status", headers={serve_app._SESSION_HEADER_NAME: serve_app._SESSION_TOKEN})
    assert resp.status_code == 200


def test_gated_mode_is_not_checked(serve_app, monkeypatch):
    """Gated mode withholds the token and a loopback reverse proxy may run as another account."""
    import hermes_cli.web_server_boundary as boundary
    from fastapi.testclient import TestClient

    _set_owner(monkeypatch, boundary, os.getuid() + 1)
    monkeypatch.setattr(serve_app.app.state, "auth_required", True, raising=False)
    client = TestClient(serve_app.app, client=("127.0.0.1", 40000), base_url="http://127.0.0.1:9119")
    resp = client.get("/api/status")
    assert resp.status_code != 403
