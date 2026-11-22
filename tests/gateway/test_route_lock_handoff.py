"""B2: handoff and hosted history/rewind fences for a route-locked local session (L7, item 10).

A handoff or platform ``/resume`` rewrites the row's routing identity and retires its local receipt, so a read
check cannot see the lock afterwards. Every repoint is refused ``route_lock_mismatch`` BEFORE any write; a refused
one leaves the receipt, the rows and the lane as they were. Real ``SessionStore``/``SessionDB``/authority; the
lock is the receipt's own ``route_lock_json`` field written into the real creation receipt (substituted fixture
state: a real ``fallback:"none"`` create needs a resolved provider route).
"""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_route_lock import FIELDS
from hermes_state_local import POLICY_PREFIX, local_receipt, require_legacy_execution
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_authority_cold_route_recovery import _Runner, _source
from tests.gateway.test_owner_loop_writes_class import _local, _owner
from tests.gateway.test_session_handoff_owner import _call, _telegram_home

pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    from gateway import run
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *_a: {'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})


def _lock(db, sid, authority=None):
    receipt = local_receipt(db, sid)
    receipt['policy']['route_lock_json'] = json.dumps(dict.fromkeys((*FIELDS, 'salt', 'credential_id'), 'test'))
    db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + sid))
    if authority is not None:  # the resident policy is the receipt's (a restored locked session has both)
        from dataclasses import replace
        from gateway.session_local_recovery import local_adapter
        adapter = local_adapter(authority)
        chat = authority.sessions[sid].source.chat_id
        adapter.policies[chat] = replace(adapter.policies[chat], route_lock_json=receipt['policy']['route_lock_json'])


def _state(db):
    """Every durable fact a refused repoint must leave alone."""
    with db._read_ctx() as conn:
        return {name: [tuple(row) for row in conn.execute(sql)] for name, sql in dict(
            sessions='SELECT id,source,session_key,chat_id,user_id,thread_id,display_name,origin_json,ended_at,end_reason '
                     'FROM sessions ORDER BY id',
            receipts="SELECT key,value FROM state_meta WHERE key LIKE 'gateway.%' ORDER BY key",
            routes='SELECT scope,session_key,entry_json FROM gateway_routing ORDER BY session_key',
            handoffs='SELECT * FROM session_handoffs' if _has(conn, 'session_handoffs') else 'SELECT 1 WHERE 0').items()}


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _gateway(runner, authority):
    from gateway.session import AsyncSessionStore
    from hermes_state import AsyncSessionDB
    return SimpleNamespace(_session_db=AsyncSessionDB(authority.db), session_authority=authority,
                           async_session_store=AsyncSessionStore(runner.session_store))


async def _authority(tmp_path, locked, request_id='b2'):
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    runner = _Runner(tmp_path)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='b2')
    owner = SessionActor('owner', 'fixture', frozenset({'session:create', 'session:read', 'session:control'}), 'socket')
    ref = create_local_session(authority, owner, {'request_id': request_id, 'source': 'gui', 'cwd': str(tmp_path),
                                                   'model': 'frozen', 'toolsets': []})
    if locked:
        _lock(authority.db, ref.session_id, authority)
    return runner, authority, ref


def _refused(excinfo):
    assert excinfo.value.reason == 'route_lock_mismatch', excinfo.value


@pytest.mark.asyncio
@pytest.mark.parametrize('locked', [True, False])
async def test_the_handoff_watcher_refuses_a_locked_conversation_before_any_write(tmp_path, locked):
    from gateway.run_startup import GatewayStartupMixin
    runner, authority, ref = await _authority(tmp_path, locked)
    gateway, telegram = _gateway(runner, authority), _source()
    route = runner.session_store._generate_session_key(telegram)
    before = _state(authority.db)
    call = GatewayStartupMixin._handoff_repoint(gateway, SimpleNamespace(source=telegram), route, ref.session_id)
    if not locked:  # control: the receipt is retired and the lane takes the conversation
        entry = await call
        assert authority.logical_owner(entry.session_id) == ref.session_id
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (POLICY_PREFIX + ref.session_id,)).fetchone() is None
        return
    with pytest.raises(RuntimeStoreError) as excinfo:
        await call
    _refused(excinfo)
    assert _state(authority.db) == before
    assert runner.session_store.lookup_by_session_key(route) is None
    with pytest.raises(RuntimeStoreError) as still:
        require_legacy_execution(authority.db, ref.session_id)
    _refused(still)


@pytest.mark.asyncio
async def test_a_locked_conversation_in_a_later_segment_is_refused_by_its_head_and_its_first_segment(tmp_path):
    from gateway.session_route_recovery import conversation_head
    runner, authority, ref = await _authority(tmp_path, True)
    runner.session_store.reset_session(authority.sessions[ref.session_id].route)  # /new: the head is a later segment
    head = local_receipt(authority.db, ref.session_id)['entry']['session_id']
    assert head != ref.session_id
    gateway = _gateway(runner, authority)
    for named in (ref.session_id, head):
        with pytest.raises(RuntimeStoreError) as excinfo:
            await conversation_head(gateway, named)
        _refused(excinfo)


@pytest.mark.asyncio
async def test_switch_session_refuses_to_move_a_lane_onto_a_locked_conversation(tmp_path):
    """The one chokepoint every repoint passes (``/resume``, admin ``/resume --all``, reply routing, handoff)."""
    runner, authority, ref = await _authority(tmp_path, True)
    store, telegram = runner.session_store, _source()
    lane = store.get_or_create_session(telegram)
    before = _state(authority.db)
    with pytest.raises(RuntimeStoreError) as excinfo:
        store.switch_session(lane.session_key, ref.session_id, preserve_prompt_pin=False)
    _refused(excinfo)
    assert _state(authority.db) == before
    assert store.lookup_by_session_key(lane.session_key).session_id == lane.session_id
    # Control: the conversation's own local route is not a lane it was moved onto.
    own = authority.sessions[ref.session_id].route
    assert store.switch_session(own, ref.session_id).session_id == ref.session_id


@pytest.mark.asyncio
async def test_a_refused_lane_move_never_retires_the_receipt_inside_the_transaction(tmp_path):
    """The swallowed-error path: ``_record_gateway_session_peer`` logs a failure, so the write itself must refuse."""
    runner, authority, ref = await _authority(tmp_path, True)
    telegram, db = _source(), authority.db
    route = runner.session_store._generate_session_key(telegram)
    before = _state(db)
    with pytest.raises(RuntimeStoreError) as excinfo:
        db.record_gateway_session_peer(ref.session_id, source='telegram', user_id='u', session_key=route,
                                       chat_id='4242', chat_type='dm', include_compression_ancestors=True)
    _refused(excinfo)
    assert _state(db) == before


@pytest.mark.asyncio
async def test_boot_heal_never_retires_a_locked_receipt_and_still_releases_an_unlocked_one(tmp_path):
    from gateway.session_local_recovery import release_handed_off_receipts
    from hermes_state_runtime import _epoch
    runner, authority, locked = await _authority(tmp_path, True, 'locked')
    unlocked = _local(authority, SimpleNamespace(actor=authority_actor(authority)), tmp_path, source='gui', request_id='free')
    telegram = _source()
    route = runner.session_store._generate_session_key(telegram)
    for ref in (locked, unlocked):  # an older build moved both rows onto the lane and left their receipts
        authority.db._write_sql('UPDATE sessions SET session_key=?, source=?, chat_id=?, user_id=? WHERE id=?',
                                (route, 'telegram', '4242', 'u-4242', ref.session_id))
    release_handed_off_receipts(authority.db, epoch=authority.epoch)  # must not raise at boot
    with authority.db._read_ctx() as conn:
        kept = {sid: conn.execute('SELECT 1 FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone() is not None
                for sid in (locked.session_id, unlocked.session_id)}
    assert kept == {locked.session_id: True, unlocked.session_id: False}
    assert _epoch  # imported for the epoch-guard contract of the boot write


def authority_actor(authority):
    from gateway.session_contract import SessionActor
    return SessionActor('owner', 'fixture', frozenset({'session:create', 'session:read', 'session:control'}), 'socket')


@pytest.mark.asyncio
@pytest.mark.parametrize('locked', [True, False])
async def test_platform_resume_gives_a_refusal_not_a_moved_lane(tmp_path, locked):
    """``_repoint_route`` (``/resume``, ``/resume --all``, reply routing) answers text and moves nothing."""
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    runner, authority, ref = await _authority(tmp_path, locked)
    store, telegram = runner.session_store, _source()
    lane = store.get_or_create_session(telegram)
    gateway = _gateway(runner, authority)
    released = []
    gateway._release_running_agent_state = released.append
    gateway._telegram_topic_switch_conflict = AsyncMock(return_value=None)
    gateway._telegram_topic_publication = lambda source: nullcontext()
    gateway._follow_telegram_topic_switch = AsyncMock()
    gateway._clear_conversation_scope = lambda *a, **k: None
    gateway._evict_cached_agent = lambda key: None
    gateway._rename_telegram_topic_after_switch = AsyncMock()
    gateway.async_session_store.set_model_override = AsyncMock()
    gateway._session_db = SimpleNamespace(get_session_title=AsyncMock(return_value='t'))
    from unittest.mock import patch
    before = _state(authority.db)
    with patch('gateway.slash_commands_session.paused_followers', AsyncMock(return_value=0)):
        refused, _left, _title, moved = await GatewaySessionCommandsMixin._repoint_route(
            gateway, telegram, lane.session_key, lane, ref.session_id, ref.session_id, authority)
    if not locked:
        assert refused is None and moved
        return
    assert 'route_lock_mismatch' in refused and not moved
    assert released == [] and _state(authority.db) == before
    assert store.lookup_by_session_key(lane.session_key).session_id == lane.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize('locked', [True, False])
async def test_the_owner_handoff_request_is_refused_before_the_row_is_queued(tmp_path, monkeypatch, locked):
    _telegram_home(tmp_path, monkeypatch)
    _store, authority, owner = _owner(tmp_path, monkeypatch, handle=None)
    ref = _local(authority, owner, tmp_path)
    if locked:
        _lock(authority.db, ref.session_id, authority)
    reply = await _call(owner, 'handoff.request', session_id=ref.session_id, platform='telegram')
    if not locked:
        assert reply['result']['queued'] is True
    else:
        assert reply['error']['data']['reason'] == 'route_lock_mismatch', reply
        assert 'owning gateway' in reply['error']['message'], reply  # the human reason Desktop shows
        assert authority.db.list_pending_handoffs() == []
        with pytest.raises(RuntimeStoreError):
            require_legacy_execution(authority.db, ref.session_id)
    authority.db.close()


@pytest.mark.asyncio
async def test_the_watchers_settled_recheck_refuses_a_conversation_locked_since_the_request(tmp_path):
    """``check_settled`` re-runs the fence on a write transaction right before the repoint."""
    from gateway.session_handoff import check_settled
    _runner, authority, ref = await _authority(tmp_path, True)
    with pytest.raises(RuntimeError, match='owning gateway'):
        await check_settled(authority.db, ref.session_id)
    with authority.db._read_ctx() as conn:
        assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (POLICY_PREFIX + ref.session_id,)).fetchone()


@pytest.mark.parametrize('locked', [True, False])
def test_hosted_room_history_of_a_locked_session_is_a_typed_refusal(hosted_owner, locked):
    from gateway.session_hosted_rpc import HostedRoomAuthorityRPC
    authority, loop, principal, _agent = hosted_owner
    rpc = HostedRoomAuthorityRPC(authority, loop, room_id='room', member_id='one', profile='default',
                                 principal=principal, authorize=lambda *_a: True)
    coords = {'profile': 'default', 'source': 'bot_room'}
    sid = rpc.create(**coords, title='Group: room')['session_id']
    if locked:
        _lock(authority.db, sid, authority)
    if not locked:
        assert rpc.history(**coords, session_id=sid) == []
        return
    with pytest.raises(RuntimeStoreError) as excinfo:
        rpc.history(**coords, session_id=sid)
    _refused(excinfo)
    assert type(excinfo.value) is RuntimeStoreError or excinfo.value.reason == 'route_lock_mismatch'


class _Checkpoints:
    """Captures the consequential edge: the file restore."""
    restored = []

    def __init__(self, **_kwargs):
        self.enabled = True

    def unsupported_backend_reason(self, _task):
        return None

    def list_checkpoints(self, _cwd):
        return [{'hash': 'abc123', 'timestamp': '2026-11-21T00:00:00', 'reason': 'fixture'}]

    def restore(self, _cwd, target, **_kwargs):
        self.restored.append(target)
        return {'success': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('locked', [True, False])
async def test_a_whole_tree_rewind_of_a_locked_session_restores_nothing(tmp_path, monkeypatch, locked):
    """The history rewind half of ``rollback.restore`` is refused for a locked session, so the file half must be
    refused first: a typed refusal after the working tree was already restored is still a mutation."""
    from tools import checkpoint_manager, checkpoint_undo_hint
    _Checkpoints.restored = []
    monkeypatch.setattr(checkpoint_manager, 'CheckpointManager', _Checkpoints)
    monkeypatch.setattr(checkpoint_undo_hint, 'checkpoint_taken_at', lambda cwd, target: 0)
    _store, authority, owner = _owner(tmp_path, monkeypatch, handle=None)
    ref = _local(authority, owner, tmp_path)
    if locked:
        _lock(authority.db, ref.session_id, authority)
    before = _state(authority.db)
    reply = await owner.dispatch({'id': 1, 'method': 'rollback.restore',
                                  'params': {'session_id': ref.session_id, 'hash': 'abc123'}})
    if not locked:
        assert reply['result']['success'] is True and _Checkpoints.restored == ['abc123'], reply
    else:
        assert reply['error']['data']['reason'] == 'route_lock_mismatch', reply
        assert _Checkpoints.restored == [] and _state(authority.db) == before
    authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['session.history', 'session.save', 'session.export'])
async def test_tui_history_and_portable_export_of_a_locked_session_are_typed_refusals(tmp_path, monkeypatch, method):
    _store, authority, owner = _owner(tmp_path, monkeypatch, handle=None)
    ref = _local(authority, owner, tmp_path)
    saved = tmp_path / 'sessions' / 'saved'
    reply = await owner.dispatch({'id': 1, 'method': method, 'params': {'session_id': ref.session_id}})
    assert 'result' in reply, reply  # control: the unlocked session answers
    _lock(authority.db, ref.session_id, authority)
    reply = await owner.dispatch({'id': 2, 'method': method, 'params': {'session_id': ref.session_id}})
    assert reply['error']['data']['reason'] == 'route_lock_mismatch', reply
    assert not saved.exists() or not any(saved.iterdir()) or method != 'session.save' or len(list(saved.iterdir())) == 1
    authority.db.close()


@pytest.mark.asyncio
async def test_the_resume_command_answers_a_locked_conversation_with_the_refusal_not_an_error(tmp_path):
    """``/resume <name>`` resolves the conversation's head first; that read is where a lock is seen."""
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    runner, authority, ref = await _authority(tmp_path, True)
    telegram = _source()
    gateway = _gateway(runner, authority)
    gateway._normalize_source_for_session_key = lambda source: source
    gateway._session_key_for_source = runner.session_store._generate_session_key
    gateway._resolve_resume_target = AsyncMock(return_value=(ref.session_id, 'locked one'))
    before = _state(authority.db)
    reply = await GatewaySessionCommandsMixin._handle_resume_command(
        gateway, SimpleNamespace(source=telegram, get_command_args=lambda: 'locked one'))
    assert 'route_lock_mismatch' in reply
    assert _state(authority.db) == before
