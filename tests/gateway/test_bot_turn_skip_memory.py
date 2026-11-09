"""Bot-sourced turns build their agent with skip_memory=True; human turns are unchanged."""

from types import SimpleNamespace

import pytest

from gateway import session_policy
from gateway.session_execution import GatewaySessionAgentMixin


def _build_kwargs(monkeypatch, *, is_bot):
    monkeypatch.setattr(session_policy, "policy_for_source", lambda runner, source: None)
    built = {}
    turn = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
    turn._runner = SimpleNamespace(session_authority=None, _prefill_messages=None, _service_tier=None,
                                   _session_db=None, _refresh_fallback_model=lambda: None)
    source = SimpleNamespace(chat_id="c", user_id="u", user_id_alt=None, user_name=None, chat_name=None,
                             chat_type="group", thread_id=None, is_bot=is_bot)
    turn._ctx = SimpleNamespace(source=source, user_config={}, enabled_toolsets=["bot_room"],
                                disabled_toolsets=None, session_id="s", session_key="k",
                                AIAgent=lambda **kw: built.update(kw))
    turn._build_fresh_agent({"model": "m", "runtime": {}}, "fluxer", None, 1, None, {}, False)
    return built


def test_bot_source_builds_with_skip_memory(monkeypatch):
    assert _build_kwargs(monkeypatch, is_bot=True)["skip_memory"] is True


def test_human_source_keeps_skip_memory_false(monkeypatch):
    assert _build_kwargs(monkeypatch, is_bot=False)["skip_memory"] is False
