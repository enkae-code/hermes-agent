"""Route lock, WO item 5: a LOCKED session (``fallback: none``) refuses every side / review / background
entry point before any transcript is read or any provider (primary or backup) is contacted.

Real owner + real ``create_local_session`` + real ``prompt.submit`` (fixtures of ``test_session_route_lock``);
only the provider's HTTP edge is a loopback server. Locked cases assert behaviour -- the typed refusal and
ZERO new requests to either server -- not a guard name. Each entry point has an UNLOCKED control that must
reach the provider, so a locked pass can never be a dead path. Auto-review has its trigger forced on
(``memory.nudge_interval: 1`` + the memory toolset) and a control proving the fork fires when unlocked.
"""

import asyncio
import json

import pytest

from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_session_route_lock import _chats, owner
from tests.waiting import async_wait_until

REFUSAL = "route_lock_mismatch"


def _n(o):
    """Chat requests seen by ANY loopback server (primary + the configured backup)."""
    return len(_chats(o.primary)) + len(_chats(o.backup))


async def _new(o, base, wait=4.0):
    """Requests since *base*, waiting up to *wait* s for a stray one (the side agents run on threads)."""
    end = asyncio.get_running_loop().time() + wait
    while _n(o) == base and asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.1)
    return _n(o) - base


async def _reached(o, base, what):
    await async_wait_until(lambda: _n(o) > base, timeout=45, what=what)


def _reads(o):
    """Count transcript reads from now on."""
    seen, real = [], o.authority.db.get_messages_as_conversation
    o.authority.db.get_messages_as_conversation = lambda *a, **k: (seen.append(a), real(*a, **k))[1]
    return seen


def _agents(monkeypatch):
    """Every AIAgent constructed from now on (kwargs)."""
    import run_agent

    built, real = [], run_agent.AIAgent

    class Spy(real):
        def __init__(self, *a, **k):
            built.append(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(run_agent, "AIAgent", Spy)
    return built


async def _session(o, locked, *, turn=True, **extra):
    """A session attached on the connection (side verbs need the subscription), with one settled turn."""
    ref = o.create(f"r9-{'lock' if locked else 'open'}", **({"fallback": "none"} if locked else {}), **extra)
    await o.call("session.resume", session_id=ref.session_id)
    if turn:
        await o.turn(ref, "first")
    return ref


def _refused(reply=None, exc=None):
    text = str(reply or "")
    return (isinstance(exc, RuntimeStoreError) and exc.reason == REFUSAL) or REFUSAL in text or "locked" in text.lower()


# --- (1) /btw, (2) /bg over the tui side route ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["prompt.btw", "prompt.background"])
async def test_locked_side_verb_is_refused_and_sends_nothing(owner, verb):
    ref = await _session(owner, True)
    base, reads = _n(owner), _reads(owner)
    reply = await owner.call(verb, session_id=ref.session_id, text="side question")
    assert (reply.get("error") or {}).get("data", {}).get("reason") == REFUSAL, reply
    assert await _new(owner, base) == 0
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["prompt.btw", "prompt.background"])
async def test_unlocked_side_verb_still_reaches_the_provider(owner, verb):
    ref = await _session(owner, False)
    base = _n(owner)
    reply = await owner.call(verb, session_id=ref.session_id, text="side question")
    assert (reply.get("result") or {}).get("task_id"), reply
    await _reached(owner, base, verb)


# --- (3) /refine and /review: warm (live resident session) and cold (stored transcript) ----------------------


async def _warm_refine(o, ref):
    from gateway.session_refine import refine_local_session

    live = o.authority.sessions[ref.session_id]
    try:
        return await refine_local_session(o.authority.runner, live.source, "focus"), None
    except Exception as exc:  # the refusal may be typed or text; the assertions below judge it
        return None, exc


@pytest.mark.asyncio
async def test_locked_warm_refine_is_refused_and_sends_nothing(owner):
    ref = await _session(owner, True)
    base, reads = _n(owner), _reads(owner)
    reply, exc = await _warm_refine(owner, ref)
    assert _refused(reply, exc), (reply, exc)
    assert await _new(owner, base) == 0
    assert reads == []


@pytest.mark.asyncio
async def test_unlocked_warm_refine_still_reaches_the_provider(owner):
    ref = await _session(owner, False)
    base = _n(owner)
    reply, exc = await _warm_refine(owner, ref)
    assert exc is None and reply, (reply, exc)
    await _reached(owner, base, "warm refine review fork")


async def _cold(o, ref, verb):
    """The cold slash path: stored transcript first, then the fresh parent (as the handlers do)."""
    from agent.review_engine import start_review
    from gateway.session_cold_review import cold_review_history, cold_review_parent, start_cold_refine

    runner, source = o.authority.runner, o.authority.sessions[ref.session_id].source
    key = runner._session_key_for_source(source)
    try:
        session_id, history = await cold_review_history(runner, source)
        if verb == "refine":
            await start_cold_refine(runner, source, key, session_id, history, "focus")
        else:

            def dispatch():
                with cold_review_parent(runner, source, key, session_id) as parent:
                    return start_review(parent, history, "focus")

            await asyncio.get_running_loop().run_in_executor(None, dispatch)
    except Exception as exc:
        return exc
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["refine", "review"])
async def test_locked_cold_review_is_refused_and_sends_nothing(owner, verb):
    ref = await _session(owner, True)
    base, reads = _n(owner), _reads(owner)
    exc = await _cold(owner, ref, verb)
    assert _refused(None, exc), exc
    assert await _new(owner, base) == 0
    assert reads == []


@pytest.mark.asyncio
async def test_unlocked_cold_refine_still_reaches_the_provider(owner):
    ref = await _session(owner, False)
    base = _n(owner)
    assert await _cold(owner, ref, "refine") is None
    await _reached(owner, base, "cold refine review fork")


# --- (4) gateway slash /bg (run_background._run_background_task_inner) ---------------------------------------


async def _gateway_bg(o, ref):
    live = o.authority.sessions[ref.session_id]
    await o.authority.runner._run_background_task_inner("do the thing", live.source, "bg_r9")


@pytest.mark.asyncio
async def test_locked_gateway_bg_builds_no_agent_and_sends_nothing(owner, monkeypatch):
    ref = await _session(owner, True, turn=False)
    base, built = _n(owner), _agents(monkeypatch)
    await _gateway_bg(owner, ref)
    assert built == []
    assert await _new(owner, base) == 0


@pytest.mark.asyncio
async def test_unlocked_gateway_bg_still_builds_an_agent_and_reaches_the_provider(owner, monkeypatch):
    ref = await _session(owner, False, turn=False)
    base, built = _n(owner), _agents(monkeypatch)
    await _gateway_bg(owner, ref)
    assert len(built) == 1
    assert await _new(owner, base) >= 1


# --- (5) automatic background memory/skill review after a turn ----------------------------------------------


async def _review_turn(o, monkeypatch, locked):
    """One turn with the memory review forced due (nudge_interval 1, memory toolset); returns
    ``(agent, requests_after_the_turn, external_memory_syncs)``."""
    import run_agent

    o.config(memory={"memory_enabled": True, "user_profile_enabled": False, "nudge_interval": 1})
    o.config(fallback_providers=[{"provider": "custom", "model": "backup-model", "base_url": o.backup_url,
                                  "api_key": "backup-key"}])
    syncs, real = [], run_agent.AIAgent._sync_external_memory_for_turn

    def spy(self, **kw):
        if self._memory_manager:  # only a sync that would reach a provider counts
            syncs.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(run_agent.AIAgent, "_sync_external_memory_for_turn", spy)
    ref = await _session(o, locked, turn=False, toolsets=["memory"])
    await o.turn(ref, "remember this")
    agent = o.authority.agent(ref)
    assert agent is not None and agent._memory_nudge_interval == 1 and "memory" in agent.valid_tool_names
    return ref, agent, syncs


@pytest.mark.asyncio
async def test_locked_turn_forks_no_review_and_syncs_no_memory(owner, monkeypatch):
    _ref, agent, syncs = await _review_turn(owner, monkeypatch, True)
    await asyncio.sleep(6)  # the fork runs after delivery, on a thread
    assert _n(owner) == 1, "exactly the turn request: no review fork on primary or backup"
    assert owner.backup.seen == [] and syncs == []
    assert agent.skip_background_review is True


@pytest.mark.asyncio
async def test_unlocked_turn_still_forks_the_review(owner, monkeypatch):
    _ref, agent, _ = await _review_turn(owner, monkeypatch, False)
    assert agent.skip_background_review is False
    await _reached(owner, 1, "background review fork")
