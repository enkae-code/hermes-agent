"""Bot-sourced turns collect no plugin ``pre_llm_call`` context (human turns unchanged)."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.turn_context import _collect_pre_llm_call_context


def _agent(bot_turn):
    return SimpleNamespace(session_id="s", model="m", platform="fluxer", _user_id="u",
                           _persist_disabled=False, bot_turn=bot_turn)


def _collect(agent, blocks=None):
    return _collect_pre_llm_call_context(
        agent, effective_task_id="t", turn_id="turn", original_user_message="hi",
        messages=[], conversation_history=[], blocks=blocks)


def test_init_path_keeps_bot_turn_on_the_agent():
    from run_agent import AIAgent
    agent = AIAgent(model="gpt-4o", api_key="k", base_url="http://127.0.0.1:9/v1", quiet_mode=True, bot_turn=True)
    assert agent.bot_turn is True


def test_bot_turn_does_not_invoke_pre_llm_call_hook():
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"context": "PRIVATE"}]) as hook:
        assert _collect(_agent(bot_turn=True)) == ""
    hook.assert_not_called()


def test_bot_turn_block_verdict_is_not_collected():
    verdicts = [{"action": "block", "message": "VETO"}]
    blocks = []
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=verdicts):
        assert _collect(_agent(bot_turn=True), blocks) == ""
    assert blocks == []


def test_human_turn_still_invokes_pre_llm_call_hook():
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"context": "CTX"}]) as hook:
        assert _collect(_agent(bot_turn=False)) == "CTX"
    hook.assert_called_once()
