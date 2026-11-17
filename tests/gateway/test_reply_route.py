"""A reply to a message a session sent continues that session (reply routing).

Real SessionDB rows and the real rich_sent_store index; only the session store (route table) and
the platform sender are stubbed.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import rich_sent_store
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key

CHAT = "67890"
OWNER = "12345"
KEY = build_session_key(SessionSource(platform=Platform.TELEGRAM, user_id=OWNER, chat_id=CHAT))


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(rich_sent_store, "_store_path", lambda: str(tmp_path / "rich.json"))
    return rich_sent_store


def _source(user_id=OWNER, is_bot=False):
    return SessionSource(platform=Platform.TELEGRAM, user_id=user_id, chat_id=CHAT,
                         chat_type="dm", user_name="op", is_bot=is_bot)


def _runner(tmp_path, admin=True):
    from hermes_state import AsyncSessionDB, SessionDB
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(platforms={})
    runner._voice_mode = {}
    runner._session_db = AsyncSessionDB(db)
    runner._running_agents = {}
    runner._is_user_authorized = lambda _source: True
    runner._resume_caller_is_admin = lambda _source: admin
    current = MagicMock(session_id="current", session_key=KEY)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = current
    runner.session_store.switch_session = MagicMock(return_value=MagicMock(session_id="x"))
    return runner, db


def _session(db, sid, user_id=OWNER, parent=None):
    db.create_session(sid, "telegram", session_key=KEY, user_id=user_id, chat_id=CHAT,
                      chat_type="dm", **({"parent_session_id": parent} if parent else {}))
    db.append_message(sid, "user", f"turn in {sid}")


def _reply(reply_to, source):
    return MessageEvent(text="yes", source=source, reply_to_message_id=reply_to)


def _switched_to(runner):
    return [c.args[1] for c in runner.session_store.switch_session.call_args_list]


@pytest.mark.asyncio
async def test_admin_reply_routes_to_origin_session(tmp_path, index):
    runner, db = _runner(tmp_path)
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")
    event = _reply("m1", _source(user_id="99"))

    await runner._route_reply_to_origin_session(event, event.source, KEY)

    runner.session_store.switch_session.assert_called_once_with(KEY, "origin", preserve_prompt_pin=False)


@pytest.mark.asyncio
async def test_non_admin_reply_to_another_users_session_is_not_routed(tmp_path, index):
    runner, db = _runner(tmp_path, admin=False)
    _session(db, "theirs", user_id="someone-else")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "theirs")

    event = _reply("m1", _source())
    await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_bot_reply_is_not_routed(tmp_path, index):
    runner, db = _runner(tmp_path)
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99", is_bot=True))
    await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_unrecorded_reply_id_is_not_routed(tmp_path, index):
    runner, _ = _runner(tmp_path)

    event = _reply("never-sent", _source(user_id="99"))
    await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_compressed_origin_routes_to_live_continuation(tmp_path, index):
    runner, db = _runner(tmp_path)
    _session(db, "origin")
    db.end_session("origin", "compression")
    _session(db, "tip", parent="origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99"))
    await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert _switched_to(runner) == ["tip"]


@pytest.mark.asyncio
async def test_reply_while_this_chat_is_mid_turn_is_held_not_routed(tmp_path, index):
    runner, db = _runner(tmp_path)
    runner._is_session_running = lambda key: key == KEY
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99"))
    notice = await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert "still running" in notice
    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_reply_to_a_conversation_still_running_elsewhere_is_held(tmp_path, index, monkeypatch):
    import gateway.session_route_recovery as recovery

    async def unsettled(_runner, _authority, _session_id):
        raise RuntimeError("wait for the current turn to finish")

    monkeypatch.setattr(recovery, "settled_handoff_db", unsettled)
    runner, db = _runner(tmp_path)
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99"))
    notice = await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert "still running" in notice
    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_a_turn_admitted_during_the_settled_check_is_not_released(tmp_path, index, monkeypatch):
    import gateway.session_route_recovery as recovery

    runner, db = _runner(tmp_path)
    running = set()
    runner._is_session_running = lambda key: key in running

    async def turn_starts_meanwhile(_runner, _authority, _session_id):
        running.add(KEY)

    monkeypatch.setattr(recovery, "settled_handoff_db", turn_starts_meanwhile)
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99"))
    notice = await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert "still running" in notice
    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_a_command_sent_as_a_reply_is_left_to_command_dispatch(tmp_path, index):
    runner, db = _runner(tmp_path)
    runner._is_session_running = lambda key: True  # /stop must reach its handler, not a busy notice
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = MessageEvent(text="/stop", source=_source(user_id="99"), reply_to_message_id="m1")
    assert await runner._route_reply_to_origin_session(event, event.source, KEY) is None
    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_a_reply_in_a_shared_group_lane_is_not_routed(tmp_path, index):
    runner, db = _runner(tmp_path)
    runner._is_shared_session_source = lambda _source: True
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    event = _reply("m1", _source(user_id="99"))
    await runner._route_reply_to_origin_session(event, event.source, KEY)

    assert _switched_to(runner) == []


@pytest.mark.asyncio
async def test_a_routed_reply_id_is_spent_so_a_later_new_sticks(tmp_path, index):
    runner, db = _runner(tmp_path)
    _session(db, "origin")
    index.record_marker(f"telegram:{CHAT}", ["m1"], "session", "origin")

    first = _reply("m1", _source(user_id="99"))
    await runner._route_reply_to_origin_session(first, first.source, KEY)
    again = _reply("m1", _source(user_id="99"))  # e.g. a Slack thread stamping every message
    await runner._route_reply_to_origin_session(again, again.source, KEY)

    assert _switched_to(runner) == ["origin"]


@pytest.mark.asyncio
async def test_resume_of_an_untitled_session_still_renames_the_topic_to_its_name(tmp_path, index):
    runner, db = _runner(tmp_path)
    runner._rename_telegram_topic_after_switch = AsyncMock()
    _session(db, "untitled-id")

    await runner._handle_resume_command(MessageEvent(text="/resume untitled-id", source=_source()))

    assert runner._rename_telegram_topic_after_switch.await_args.args[2] == "untitled-id"


@pytest.mark.asyncio
async def test_resume_still_switches_on_its_own_path(tmp_path, index):
    runner, db = _runner(tmp_path)
    _session(db, "named")
    db.set_session_title("named", "Research")

    reply = await runner._handle_resume_command(MessageEvent(text="/resume named", source=_source()))

    assert "Research" in reply
    runner.session_store.switch_session.assert_called_once_with(KEY, "named", preserve_prompt_pin=False)


def test_send_records_origin_only_with_session_env(index, monkeypatch):
    from gateway.outbound.send import send_message_tool

    tg_cfg = SimpleNamespace(enabled=True, token="t", extra={})
    config = SimpleNamespace(platforms={Platform.TELEGRAM: tg_cfg}, get_home_channel=lambda _p: None)
    def send(env, message_id, source="desktop"):
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        monkeypatch.setenv("HERMES_SESSION_SOURCE", source)
        if env:
            monkeypatch.setenv("HERMES_SESSION_ID", env)
        sent = {"success": True, "message_id": message_id}
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=lambda coro: asyncio.run(coro)), \
             patch("gateway.outbound.send._send_to_platform", new=AsyncMock(return_value=sent)), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            return json.loads(send_message_tool({"action": "send", "target": "telegram:67890", "message": "done"}))

    first = send("sess_sent", "77")
    assert first.get("success") is True, first
    assert index.lookup_marker("telegram:67890", "77", "session") == "sess_sent"

    send(None, "78")
    assert index.lookup_marker("telegram:67890", "78", "session") is None

    send("sess_cron", "79", source="cron")  # cron/messaging turns are not a conversation to continue
    assert index.lookup_marker("telegram:67890", "79", "session") is None
