"""Round 12: locked sessions skip automatic pre-turn, inbound, post-turn, and viewer-automation model helpers."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource
from gateway.session_local import LocalSessionAdapter


def _make_runner_with_source(*, locked: bool):
    from gateway.run import GatewayRunner

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="sess-r12",
        user_id="human",
        profile="default",
        chat_type="dm",
    )
    authority = SimpleNamespace(sessions={"sess-r12": SimpleNamespace(source=source)})
    adapter = LocalSessionAdapter(authority)
    adapter.register_source(source)
    adapter.policies["sess-r12"] = SimpleNamespace(
        route_lock_json="{}" if locked else None,
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._adapter_for_source = lambda src: adapter if src.platform == Platform.LOCAL else None
    runner._delivery_adapter_for = lambda src: adapter
    runner._session_key_for_source = lambda src: "local:sess-r12"
    runner._peek_session_state = lambda key: None
    runner._session_state = lambda key: SimpleNamespace(
        persistent=SimpleNamespace(native_image_paths=[]),
        conversation=SimpleNamespace(),
    )

    async def _run_sync(fn, *a, **kw):
        return fn(*a, **kw)

    runner._run_in_executor_with_context = _run_sync
    return runner, source


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [True, False])
async def test_hygiene_method_skips_locked_sessions(locked):
    runner, source = _make_runner_with_source(locked=locked)
    plan_calls = []
    runner._hmwa_hygiene_settings = AsyncMock(
        return_value=SimpleNamespace(compression_enabled=True, hard_msg_limit=5000)
    )

    async def spy_plan(*a, **kw):
        plan_calls.append(a)
        return SimpleNamespace(needs_compress=False, needs_reset=False)

    runner._hmwa_hygiene_plan = spy_plan
    history = [{"role": "user", "content": "hi"}] * 6
    event = SimpleNamespace()
    entry = SimpleNamespace(session_id="s1")
    out = await runner._hmwa_run_session_hygiene(
        event, source, entry, "local:sess-r12", history, "local:sess-r12", 1
    )
    assert out == history
    assert bool(plan_calls) is (not locked)


@pytest.mark.asyncio
@pytest.mark.parametrize("locked,mode", [(True, "text"), (False, "text"), (True, "native")])
async def test_inbound_image_description_skipped_under_lock_while_retaining_attachment(
    tmp_path, monkeypatch, locked, mode
):
    runner, source = _make_runner_with_source(locked=locked)
    vision_calls = []

    async def spy_vision(user_text, image_paths):
        vision_calls.append((user_text, list(image_paths)))
        return f"[described]\n\n{user_text}"

    runner._decide_image_input_mode = lambda **kw: mode
    runner._resolve_session_agent_runtime = lambda **kw: ("m", {"provider": "p", "api_key": "k"})
    runner._enrich_message_with_vision = spy_vision
    monkeypatch.setattr("agent.auxiliary_client.scoped_runtime_main", lambda *a, **kw: nullcontext())

    img_path = str(tmp_path / "pic.png")
    out = await runner._enrich_inbound_images(source, "local:sess-r12", "look at this", [img_path])

    if mode == "native":
        assert vision_calls == []
        assert out == "look at this"
    elif locked:
        assert vision_calls == []
        assert img_path in out
        assert "look at this" in out
    else:
        assert len(vision_calls) == 1
        assert out.startswith("[described]")


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [True, False])
async def test_post_turn_goal_and_loop_judges_skipped_for_locked_sessions(monkeypatch, locked):
    runner, source = _make_runner_with_source(locked=locked)
    goal_eval_calls = []
    loop_complete_calls = []

    class FakeGoalMgr:
        state = SimpleNamespace(active_wait=None)

        def is_active(self):
            return True

        def evaluate_after_turn(self, *a, **kw):
            goal_eval_calls.append((a, kw))
            return {"message": "", "should_continue": False}

    class FakeLoopMgr:
        state = SimpleNamespace(awaiting_response=True, until="tests pass")

        def complete_tick(self, final_response, *a, **kw):
            loop_complete_calls.append(final_response)
            return {"message": ""}

    async def fake_post_turn_mgr(session_entry, label, module, load):
        return FakeGoalMgr() if module == "goals" else FakeLoopMgr()

    runner._post_turn_manager = fake_post_turn_mgr
    runner._publish_local_control = AsyncMock()
    store_stub = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="s1"))
    )
    monkeypatch.setattr(type(runner), "async_session_store", property(lambda _self: store_stub))
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda r: None)
    monkeypatch.setattr("gateway.session_viewer_automation.viewer_driven", lambda *a, **kw: False)
    monkeypatch.setattr("hermes_cli.goals.gather_background_processes", lambda **kw: None)
    monkeypatch.setattr("hermes_cli.goals.count_active_delegations", lambda sid: 0)

    await runner._run_post_turn_hooks(
        agent_result={"final_response": "done"},
        source=source,
        is_internal=False,
    )

    assert bool(goal_eval_calls) is (not locked)
    # Non-model loop bookkeeping still runs under lock, but with empty final_response so --until judge is never fed.
    assert loop_complete_calls == ([""] if locked else ["done"])


@pytest_asyncio.fixture
async def rpc_owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from hermes_cli import goals
    from hermes_state_local import local_receipt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(
        session_store=store, _session_db=store._db, adapters={}, _draining=False,
        _cached_agent_for=lambda route: None,
    )
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path / "fixture"), instance_id="owner")
    monkeypatch.setattr(goals, "_get_session_db", lambda: authority.db)
    viewer = AuthorityConnection(authority, object(), {"user_id": "human", "profile_id": str(tmp_path / "fixture")})
    ref = create_local_session(authority, viewer.actor, {
        "request_id": "r", "source": "cli", "cwd": str(tmp_path), "model": "m", "toolsets": [],
    })
    target = local_receipt(authority.db, ref.session_id)["entry"]["session_id"]

    async def call(kind, name, *args, **kwargs):
        return await viewer.dispatch({"id": 1, "method": "session.automation", "params": {
            "session_id": ref.session_id, "kind": kind, "call": name, "args": list(args), "kwargs": kwargs,
        }})

    try:
        yield SimpleNamespace(call=call, db=authority.db, target=target, authority=authority, ref=ref, runner=runner)
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [True, False])
async def test_viewer_automation_rpc_gates_model_evaluations_under_lock(rpc_owner, monkeypatch, locked):
    from dataclasses import replace
    from gateway.session_viewer_automation import viewer_driven
    from hermes_cli import goals

    judge_calls = []
    draft_calls = []

    def spy_judge(goal, response, **kw):
        judge_calls.append((goal, response))
        return "continue", "still going", False, None, False

    def spy_draft(text, **kw):
        draft_calls.append(text)

    monkeypatch.setattr(goals, "judge_goal", spy_judge)
    monkeypatch.setattr(goals, "draft_contract", spy_draft)

    adapter = rpc_owner.runner._adapter_for_source(rpc_owner.authority.sessions[rpc_owner.ref.session_id].source)
    sid = rpc_owner.ref.session_id
    if locked:
        adapter.policies[sid] = replace(adapter.policies[sid], route_lock_json=json.dumps({"provider": "p"}))

    # Refused model-backed call must NOT mutate viewer registration
    if locked:
        early_draft = await rpc_owner.call("goal", "command", "draft a contract")
        assert early_draft["error"]["data"]["reason"] == "route_lock_mismatch"
        assert viewer_driven(rpc_owner.authority, sid) is False

    # Non-model controls always succeed (including non-model goal.command and loop.complete_tick without until)
    set_res = await rpc_owner.call("goal", "set", "ship it")
    assert "state" in set_res["result"]
    pause_res = await rpc_owner.call("goal", "pause", reason="check")
    assert json.loads(pause_res["result"]["state"])["status"] == "paused"
    await rpc_owner.call("goal", "resume")
    status_res = await rpc_owner.call("goal", "command", "status")
    assert "value" in status_res["result"]

    await rpc_owner.call("loop", "set", "watch", interval_seconds=60)
    await rpc_owner.call("loop", "fire_tick")
    plain_tick = await rpc_owner.call("loop", "complete_tick", "still working")
    assert "value" in plain_tick["result"]

    # Now test model-backed calls
    eval_res = await rpc_owner.call("goal", "evaluate_after_turn", "drafted")
    draft_res = await rpc_owner.call("goal", "command", "draft a contract")
    await rpc_owner.call("loop", "set", "watch", interval_seconds=60, until="done")
    await rpc_owner.call("loop", "fire_tick")
    loop_res = await rpc_owner.call("loop", "complete_tick", "still working")

    if locked:
        assert eval_res["error"]["data"]["reason"] == "route_lock_mismatch"
        assert draft_res["error"]["data"]["reason"] == "route_lock_mismatch"
        assert loop_res["error"]["data"]["reason"] == "route_lock_mismatch"
        assert judge_calls == []
        assert draft_calls == []
    else:
        assert "value" in eval_res["result"]
        assert "value" in draft_res["result"]
        assert "value" in loop_res["result"]
        assert len(judge_calls) >= 2
        assert draft_calls == ["a contract"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arg_mode", ["positional", "keyword"])
async def test_viewer_automation_loop_complete_tick_keyword_and_marker_under_lock(rpc_owner, monkeypatch, arg_mode):
    from dataclasses import replace
    from gateway.session_viewer_automation import viewer_driven
    from hermes_cli import goals

    eval_calls = []

    def spy_judge(until, text):
        eval_calls.append((until, text))
        return "continue", "still going", False, None, False

    monkeypatch.setattr(goals, "judge_goal", spy_judge)

    adapter = rpc_owner.runner._adapter_for_source(rpc_owner.authority.sessions[rpc_owner.ref.session_id].source)
    sid = rpc_owner.ref.session_id
    adapter.policies[sid] = replace(adapter.policies[sid], route_lock_json=json.dumps({"provider": "p"}))

    await rpc_owner.call("loop", "set", "watch", interval_seconds=60, until="done")
    await rpc_owner.call("loop", "fire_tick")
    # Clear viewer registration from the setup calls so we can verify refusal does not register.
    rpc_owner.authority.viewer_automation.pop(sid, None)

    # 1. Model-backed complete_tick (both positional and keyword last_response) must be refused under lock
    args = ["still working"] if arg_mode == "positional" else []
    kwargs = {} if arg_mode == "positional" else {"last_response": "still working"}
    refused = await rpc_owner.call("loop", "complete_tick", *args, **kwargs)
    assert refused["error"]["data"]["reason"] == "route_lock_mismatch"
    assert eval_calls == []
    assert viewer_driven(rpc_owner.authority, sid) is False
    assert json.loads(rpc_owner.db.get_meta(f"loop:{rpc_owner.target}"))["awaiting_response"] is True

    # 2. Non-model LOOP_COMPLETE marker completion must succeed under lock even with until set
    m_args = ["all done\nLOOP_COMPLETE"] if arg_mode == "positional" else []
    m_kwargs = {} if arg_mode == "positional" else {"last_response": "all done\nLOOP_COMPLETE"}
    marker_res = await rpc_owner.call("loop", "complete_tick", *m_args, **m_kwargs)
    assert marker_res["result"]["value"]["stopped"] is True
    assert eval_calls == []
    assert json.loads(rpc_owner.db.get_meta(f"loop:{rpc_owner.target}"))["status"] == "done"

    # 3. Non-model no-tick-in-flight complete_tick must succeed under lock
    rpc_owner.authority.viewer_automation.pop(sid, None)
    idle_res = await rpc_owner.call("loop", "complete_tick", *args, **kwargs)
    assert idle_res["result"]["value"]["reason"] == "no tick in flight"
    assert eval_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locked, text, refused",
    [
        (True, "/goal draft ship it", True),
        (True, "/goal   DRAFT ship it", True),
        (True, "/goal set ship it", False),
        (True, "/goal status", False),
        (False, "/goal draft ship it", False),
    ],
)
async def test_goal_draft_verb_is_refused_under_lock_other_verbs_run(monkeypatch, locked, text, refused):
    from gateway.platforms.event import MessageEvent
    from hermes_cli import goal_command, goals
    from hermes_cli.goal_command import GoalCommandResult
    from gateway.session_route_lock import LOCKED_NOTICE

    runner, source = _make_runner_with_source(locked=locked)
    runner._get_goal_manager_for_event = AsyncMock(return_value=(SimpleNamespace(session_id="s"), None))
    monkeypatch.setattr(goals, "last_user_message_from_db", lambda _sid: None)
    dispatched = []

    def spy_dispatch(mgr, arg, **kw):
        dispatched.append(arg)
        return GoalCommandResult("ran")

    monkeypatch.setattr(goal_command, "dispatch_goal_command", spy_dispatch)
    out = await runner._handle_goal_command(MessageEvent(text=text, source=source))
    if refused:
        assert (out, dispatched) == (LOCKED_NOTICE, [])
    else:
        assert (out, len(dispatched)) == ("ran", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [True, False])
async def test_locked_hygiene_bounds_the_model_payload_without_resolving_a_runtime(monkeypatch, locked):
    from gateway import run
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"compression": {"hygiene_hard_message_limit": 50}})
    runner, source = _make_runner_with_source(locked=locked)
    resolved = []
    runner._resolve_session_agent_runtime = lambda **kw: (resolved.append(kw), ("m", {}))[1]
    runner._hmwa_hygiene_plan = AsyncMock(return_value=SimpleNamespace(needs_compress=False, needs_reset=False))
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(60)]
    out = await runner._hmwa_run_session_hygiene(
        SimpleNamespace(), source, SimpleNamespace(session_id="s1"), "local:sess-r12", history, "local:sess-r12", 1
    )
    assert len(out) <= 50
    assert bool(resolved) is (not locked)
