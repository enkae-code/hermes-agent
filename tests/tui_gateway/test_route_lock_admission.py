import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from gateway.session_policy import LocalSessionPolicy
from gateway.session_route_lock import FIELDS
from hermes_cli import kanban_resume_guard
from hermes_state import SessionDB
from hermes_state_keys import local_identity
from hermes_state_local import POLICY_PREFIX, commit_local_session, local_receipt
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway import methods_session, server
from tui_gateway.method_ctx import rebind


@pytest.fixture
def stores(tmp_path):
    with SessionDB(tmp_path / 'donor.db') as donor, SessionDB(tmp_path / 'selected.db') as selected:
        yield donor, selected


def _record(db, tmp_path, case):
    if case == 'legacy':
        return db.create_session('ordinary', source='tui')
    profile, principal, request = str(tmp_path), 'local:test', 'create-test'
    owner = local_identity(profile, principal, request)
    if case == 'detached':
        return db.create_session(owner, source='tui')
    route = f'agent:main:local:dm:{owner}'
    origin = dict(platform='local', chat_id=owner, chat_type='dm', user_id=principal)
    lock = None if case == 'unlocked' else json.dumps(dict.fromkeys((*FIELDS, 'salt', 'credential_id'), 'test'))
    request_json = '{}' if lock is None else '{"fallback":"none"}'
    policy = LocalSessionPolicy('tui', 'tui', str(tmp_path), 'test-model', (), '{}', request_json,
                                json.dumps({'TERMINAL_CWD': str(tmp_path)}), route_lock_json=lock)
    receipt = dict(profile_id=profile, principal_id=principal, request_id=request, session_id=owner, route=route,
                   policy=asdict(policy), entry=dict(session_id=owner, session_key=route, origin=origin,
                                                   created_at='2026-11-21T00:00:00+00:00',
                                                   updated_at='2026-11-21T00:00:00+00:00'))
    commit_local_session(db, epoch=begin_runtime_epoch(db, instance_id='test'), receipt=receipt)
    if case == 'descendant':
        db.end_session(owner, 'compression')
        return db.create_session('compressed-tip', source='tui', parent_session_id=owner,
                                 session_key=route, chat_id=owner, user_id=principal)
    if case == 'missing':
        db._write_sql('DELETE FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,))
    if case == 'malformed':
        db._write_sql('UPDATE state_meta SET value=? WHERE key=?', ('{', POLICY_PREFIX + owner))
    if case == 'unverified':
        receipt['principal_id'] = 'unverified'
        db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + owner))
    if case == 'missing-lock':
        del receipt['policy']['route_lock_json']
        db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + owner))
    return owner


@pytest.mark.parametrize(('case', 'denial'), [
    ('locked', 'route_lock_mismatch'),
    ('descendant', 'route_lock_mismatch'),
    ('missing', 'storage_unavailable'),
    ('malformed', 'storage_unavailable'),
    ('unverified', 'storage_unavailable'),
    ('missing-lock', 'storage_unavailable'),
    ('unreadable', 'storage_unavailable'),
    ('unlocked', None),
    ('legacy', None),
    ('detached', None),
])
@pytest.mark.parametrize('boundary', ['resume', 'donor'])
def test_persisted_policy_at_legacy_admission(stores, tmp_path, monkeypatch, case, denial, boundary):
    donor, selected = stores
    db = donor if boundary == 'donor' else selected
    session_id = _record(db, tmp_path, case)
    found = db.get_session(session_id)
    if case == 'unreadable':
        read = db._read_ctx

        @contextmanager
        def unreadable_receipt(**kwargs):
            with read(**kwargs) as conn:
                def authorize(action, table, *_):
                    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'state_meta' else sqlite3.SQLITE_OK

                conn.set_authorizer(authorize)
                try:
                    yield conn
                finally:
                    conn.set_authorizer(None)

        monkeypatch.setattr(db, '_read_ctx', unreadable_receipt)

    effects = []

    def stop_adoption(*_):
        effects.append('adopt')
        return {'adopted': False}

    monkeypatch.setattr(methods_session, '_err', lambda rid, code, text: {'error': text}, raising=False)
    monkeypatch.setattr(methods_session, 'logger', logging.getLogger(__name__), raising=False)
    if hasattr(methods_session, '_legacy_execution_refusal'):
        guard = rebind(methods_session._legacy_execution_refusal,
                       {'_err': methods_session._err, 'logger': methods_session.logger})
        monkeypatch.setattr(methods_session, '_legacy_execution_refusal', guard)
    monkeypatch.setattr(methods_session, '_get_db', lambda: donor, raising=False)
    monkeypatch.setattr(selected, 'adopt_session_lineage_from', stop_adoption)
    monkeypatch.setattr(kanban_resume_guard, 'kanban_resume_refusal', lambda *_: effects.append('resume'))
    monkeypatch.setattr(db, 'assert_resume_safe', lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(db=selected, target=session_id, found=found, rid=None, lazy=True,
                          omit_messages=True, defer_history=True, eager_build=False)
    if boundary == 'donor':
        ctx.found = None
        result = methods_session._resume_adopt_stranded(ctx)
    else:
        result = methods_session._resume_guard(ctx)

    if denial:
        assert result is not None and denial in result['error'] and effects == [], (result, effects)
    else:
        assert result is None
        assert effects == [('adopt' if boundary == 'donor' else 'resume')]


def test_resume_guard_admits_store_without_route_lock_api(monkeypatch):
    # Lightweight adaptor DBs cannot hold a route lock, so the R14 check must not refuse their resumes.
    monkeypatch.setattr(methods_session, '_err', lambda rid, code, text: {'error': text}, raising=False)
    monkeypatch.setattr(methods_session, 'logger', logging.getLogger(__name__), raising=False)
    monkeypatch.setattr(kanban_resume_guard, 'kanban_resume_refusal', lambda *_: None)
    db = SimpleNamespace(assert_resume_safe=lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(db=db, target='s1', found={'id': 's1', 'message_count': 0}, rid=None, lazy=True,
                          omit_messages=True, defer_history=True, eager_build=False)
    assert methods_session._resume_guard(ctx) is None


def _wire(monkeypatch, store, method, params, live=None):
    monkeypatch.setattr(server, '_get_db', lambda: store)
    monkeypatch.setattr(server, '_sessions', dict(live or {}))
    monkeypatch.setattr(server, '_schedule_agent_build', lambda *_args, **_kwargs: None)
    return server.handle_request({'id': 'rl', 'method': method, 'params': params})


def _children(db, parent):
    with db._read_ctx() as conn:
        return conn.execute('SELECT COUNT(*) FROM sessions WHERE parent_session_id=?', (parent,)).fetchone()[0]


@pytest.mark.parametrize('case', ['locked', 'unlocked'])
def test_branch_stored_parent_transcript_respects_route_lock(stores, tmp_path, monkeypatch, case):
    _, selected = stores
    parent = _record(selected, tmp_path, case)
    selected.append_message(parent, 'user', 'transcript secret')
    resp = _wire(monkeypatch, selected, 'session.branch_stored',
                 {'cols': 96, 'parent_session_id': parent, 'source': 'desktop'})
    if case == 'locked':
        assert resp['error']['code'] == 4133, resp
        assert _children(selected, parent) == 0
    else:
        assert 'result' in resp, resp
        assert _children(selected, parent) == 1


def test_resume_follow_tip_rechecks_locked_tip(stores, tmp_path, monkeypatch):
    _, selected = stores
    original = selected.create_session('plain-original', source='tui')
    selected.append_message(original, 'user', 'hi')
    tip = _record(selected, tmp_path, 'locked')
    selected.append_message(tip, 'user', 'transcript secret')
    selected._write_sql('UPDATE sessions SET parent_session_id=? WHERE id=?', (original, tip))
    resp = _wire(monkeypatch, selected, 'session.resume', {'session_id': original, 'lazy': False, 'cols': 96})
    assert 'error' in resp and resp['error']['code'] == 4133, resp


def test_resume_live_unpersisted_locked_key_fails_closed(stores, tmp_path, monkeypatch):
    # A locked id whose row is gone is storage_unavailable (5000), never a reattach.
    _, selected = stores
    owner = _record(selected, tmp_path, 'locked')
    selected._write_sql('DELETE FROM sessions WHERE id=?', (owner,))
    live = {'live1': {'profile_home': None, 'session_key': owner, 'pending_title': None, 'history': []}}
    resp = _wire(monkeypatch, selected, 'session.resume', {'session_id': owner, 'cols': 96}, live=live)
    assert 'error' in resp and resp['error']['code'] == 5000, resp


def test_resume_live_unpersisted_route_key_refused(stores, tmp_path, monkeypatch):
    _, selected = stores
    owner = _record(selected, tmp_path, 'locked')
    route = f'agent:main:local:dm:{owner}'
    live = {'live1': {'profile_home': None, 'session_key': route, 'pending_title': None, 'history': []}}
    resp = _wire(monkeypatch, selected, 'session.resume', {'session_id': route, 'cols': 96}, live=live)
    # A row-less route key's identity can't be verified: refused, either as a lock (4133) or fail-closed (5000).
    assert 'error' in resp and resp['error']['code'] in (4133, 5000), resp


def test_resume_minted_orphan_policy_fails_closed(stores, tmp_path, monkeypatch):
    # An orphaned policy marker for a minted key with no row: refuse (5000) and never materialize the row.
    _, selected = stores
    key = '20261121_120000_abcdef'
    owner = _record(selected, tmp_path, 'locked')
    selected._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)',
                        (POLICY_PREFIX + key, json.dumps(local_receipt(selected, owner))))
    resp = _wire(monkeypatch, selected, 'session.resume', {'session_id': key, 'lazy': False, 'cols': 96})
    assert 'error' in resp and resp['error']['code'] == 5000, resp
    assert selected.get_session(key) is None


@pytest.mark.parametrize('case', ['locked', 'unlocked'])
def test_seeded_create_with_parent_respects_route_lock(stores, tmp_path, monkeypatch, case):
    _, selected = stores
    parent = _record(selected, tmp_path, case)
    seed = [{'role': 'user', 'content': 'seed'}, {'role': 'assistant', 'content': 'reply'}]
    resp = _wire(monkeypatch, selected, 'session.create',
                 {'cols': 96, 'parent_session_id': parent, 'source': 'desktop', 'messages': seed})
    if case == 'locked':
        assert resp['error']['code'] == 4133, resp
        assert _children(selected, parent) == 0
    else:
        assert 'result' in resp, resp
        assert _children(selected, parent) == 1


def test_locked_live_legacy_branch_refuses_before_copy(stores, tmp_path, monkeypatch):
    import threading

    _, db = stores
    parent = _record(db, tmp_path, 'locked')
    history = [{'role': 'user', 'content': 'live history'}]
    db.append_message(parent, 'user', 'stored history')
    saved = [tuple(row) for row in db._read_all('SELECT * FROM messages WHERE session_id=?', (parent,))]
    built = []

    def build(*args, **kwargs):
        built.append(args)
        raise AssertionError('unexpected branch agent construction')

    monkeypatch.setattr(server, '_make_agent_in_context', build)
    result = _wire(monkeypatch, db, 'session.branch', {'session_id': 'live1'}, live={
        'live1': {'session_key': parent, 'history': history, 'history_lock': threading.Lock(),
                  'profile_home': None, 'agent': SimpleNamespace(_session_db=db)},
    })
    assert result['error']['code'] == 4133, result
    assert _children(db, parent) == 0
    assert local_receipt(db, parent)
    assert not built
    assert saved == [tuple(row) for row in db._read_all('SELECT * FROM messages WHERE session_id=?', (parent,))]


@pytest.mark.parametrize(('method', 'params'), [
    ('session.compress', {}),
    ('session.undo', {}),
    ('slash.exec', {'command': '/refine'}),
    ('prompt.background', {'text': 'side task'}),
    ('prompt.btw', {'text': 'side question'}),
])
def test_locked_legacy_binding_blocks_side_operations(stores, tmp_path, monkeypatch, method, params):
    from hermes_state_runtime import RuntimeStoreError

    _, db = stores
    sid = _record(db, tmp_path, 'locked')
    db.append_message(sid, 'user', 'stored history')
    before = '\n'.join(db._conn.iterdump())
    monkeypatch.setattr(server, '_sessions', {})

    with pytest.raises(RuntimeStoreError) as denied:
        server._init_session('live1', sid, None, [], session_db=db, cwd=str(tmp_path))
    assert denied.value.reason == 'route_lock_mismatch'
    assert not server._sessions
    result = server.handle_request({'id': 'rl', 'method': method, 'params': {'session_id': 'live1', **params}})
    assert result['error']['code'] == 4001, result  # No usable runtime id was ever issued.
    assert '\n'.join(db._conn.iterdump()) == before


def test_unlocked_legacy_binding_keeps_undo_available(stores, tmp_path, monkeypatch):
    _, db = stores
    sid = _record(db, tmp_path, 'unlocked')
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_start_session_services', lambda *_: None)
    server._init_session('live1', sid, None, [], session_db=db, cwd=str(tmp_path))
    result = server.handle_request({'id': 'rl', 'method': 'session.undo', 'params': {'session_id': 'live1'}})
    assert 'result' in result, result


@pytest.mark.parametrize('case', ['locked', 'unlocked', 'locked_during_build'])
def test_compute_host_legacy_binding(stores, tmp_path, monkeypatch, case):
    from io import StringIO

    from hermes_state_runtime import RuntimeStoreError
    from tui_gateway.compute_host import ComputeHost

    donor, db = stores
    key = _record(db, tmp_path, 'locked' if case == 'locked' else 'unlocked')
    calls = []
    agent = SimpleNamespace()
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_start_session_services', lambda *_: None)

    def make_agent(*args, **kwargs):
        calls.append(args)
        if case == 'locked_during_build':
            # Change the real receipt between preflight and registry admission.
            _record(donor, tmp_path, 'locked')
            db._write_sql('UPDATE state_meta SET value=? WHERE key=?',
                          (json.dumps(local_receipt(donor, key)), POLICY_PREFIX + key))
        return agent

    monkeypatch.setattr(server, '_make_agent', make_agent)
    host = ComputeHost(stdout=StringIO(), heartbeat_secs=0)
    frame = {'sid': 'live1', 'session_key': key, 'cwd': str(tmp_path)}
    try:
        if case == 'unlocked':
            live = host._ensure_server_session(server, frame)
            assert live is server._sessions['live1']
            assert live['agent'] is agent
        else:
            with pytest.raises(RuntimeStoreError) as denied:
                host._ensure_server_session(server, frame)
            assert denied.value.reason == 'route_lock_mismatch'
            assert not server._sessions
        assert len(calls) == (0 if case == 'locked' else 1)
    finally:
        host.close()
