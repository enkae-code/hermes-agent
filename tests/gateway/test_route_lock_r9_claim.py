"""Route-lock WO item 6: (a) authorization runs before the ``expected_policy_revision`` fence, and
(b) a locked admission's expected revision is rechecked when the work is CLAIMED, not only when submitted.
Fixtures: the loopback provider and real owner of ``test_session_route_lock``."""

import pytest
import pytest_asyncio

from gateway.session_controls import AuthorityConnection
from tests.gateway import test_session_route_lock as _lock_tests
from tests.gateway.test_session_route_lock import _chats
from tests.waiting import async_wait_until

owner = _lock_tests.owner  # the loopback-provider fixture, aliased so the tests below do not shadow an import

WRONG = "0" * 16


def _reason(reply):
    return reply["error"]["data"]["reason"]


async def _revision(owner, sid):
    got = await owner.call("config.get", session_id=sid, key="runtime_policy")
    return got["result"]["runtime_policy"]["revision"]


# --- (a) authorization before the fence -----------------------------------------------------------------------
@pytest_asyncio.fixture
async def stranger(owner):
    """A connection of another subject that never attached to the session."""
    connection = AuthorityConnection(owner.authority, object(), {"user_id": "stranger"})
    yield connection
    await connection.close()


async def _stranger_submit(stranger, sid, revision):
    return await stranger.dispatch(
        {"id": "x", "method": "prompt.submit",
         "params": {"session_id": sid, "submission_id": "sx", "text": "x", "expected_policy_revision": revision}}
    )


@pytest.mark.asyncio
async def test_unauthorized_submit_with_the_correct_revision_is_permission_denied_control(owner, stranger):
    ref = owner.create(fallback="none")
    sid = ref.session_id
    await owner.call("session.resume", session_id=sid)
    reply = await _stranger_submit(stranger, sid, await _revision(owner, sid))
    assert _reason(reply) == "permission_denied", reply
    assert owner.rows(sid) == []


@pytest.mark.asyncio
async def test_unauthorized_submit_with_a_wrong_revision_is_permission_denied_not_a_policy_mismatch(owner, stranger):
    ref = owner.create(fallback="none")
    sid = ref.session_id
    await owner.call("session.resume", session_id=sid)
    reply = await _stranger_submit(stranger, sid, WRONG)
    assert _reason(reply) == "permission_denied", reply  # a mismatch here tells a stranger the session is locked
    assert owner.rows(sid) == []


# --- (b) the expected revision is rechecked at claim ----------------------------------------------------------
async def _queue_behind_a_held_turn(owner, sid, *, move_policy):
    """Turn A stalls at the provider; locked turn B queues with the then-current revision; optionally the
    runtime policy moves (config ``compression.checkpoint_required``: same route, new revision); A is released."""
    await owner.call("session.resume", session_id=sid)
    owner.primary.gate.clear()
    assert "result" in await owner.call("prompt.submit", session_id=sid, submission_id="a", text="A")
    await async_wait_until(lambda: len(_chats(owner.primary)) == 1, timeout=30, what="A at the provider")
    before = await _revision(owner, sid)
    sent = await owner.call(
        "prompt.submit", session_id=sid, submission_id="b", text="B", expected_policy_revision=before
    )
    assert sent["result"]["status"] == "queued", sent
    if move_policy:
        owner.config(compression={"checkpoint_required": True})
        assert await _revision(owner, sid) != before  # the premise: the revision moved, the route did not
    owner.primary.gate.set()
    await async_wait_until(
        lambda: len(owner.rows(sid)) == 2 and all(r["status"] == "terminal" for r in owner.rows(sid)),
        timeout=60,
        what="both settled",
    )


@pytest.mark.asyncio
async def test_a_queued_locked_turn_with_an_unchanged_revision_runs_control(owner):
    ref = owner.create(fallback="none")
    await _queue_behind_a_held_turn(owner, ref.session_id, move_policy=False)
    assert len(_chats(owner.primary)) == 2  # A and B both reached the provider
    assert [r["outcome"] for r in owner.rows(ref.session_id)] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_a_queued_locked_turn_whose_revision_went_stale_is_refused_at_claim(owner):
    ref = owner.create(fallback="none")
    sid = ref.session_id
    await _queue_behind_a_held_turn(owner, sid, move_policy=True)
    prompts = [auth for auth, _, _ in _chats(owner.primary)]
    assert len(prompts) == 1, "B reached the provider although the policy it was admitted under changed"
    b = owner.rows(sid)[-1]
    assert b["outcome"] != "completed" and owner.backup.seen == []
