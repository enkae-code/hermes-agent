"""Route-lock phase B1: a locked session's stored content is default-denied at the SessionDB readers.

The owner grant is the one way through, and it is bound to one SessionDB and one owner.
"""
import json
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import hermes_state_timeline
from gateway.session_policy import LocalSessionPolicy
from gateway.session_route_lock import FIELDS
from gateway.session_transcript import SessionTranscriptMixin
from hermes_state import SessionDB
from hermes_state_keys import local_identity
from hermes_state_local import (
    POLICY_PREFIX,
    RouteLockedContent,
    commit_local_session,
    content_readable,
    owner_content_grant,
)
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch


def _record(db, tmp_path, name, *, lock=True, descendant=False):
    profile, principal, request = str(tmp_path / name), 'local:test', f'create-{name}'
    owner = local_identity(profile, principal, request)
    route = f'agent:main:local:dm:{owner}'
    origin = dict(platform='local', chat_id=owner, chat_type='dm', user_id=principal)
    policy_lock = json.dumps(dict.fromkeys((*FIELDS, 'salt', 'credential_id'), 'test')) if lock else None
    request_json = '{"fallback":"none"}' if lock else '{}'
    policy = LocalSessionPolicy('tui', 'tui', str(tmp_path), 'test-model', (), '{}', request_json,
                                json.dumps({'TERMINAL_CWD': str(tmp_path)}), route_lock_json=policy_lock)
    receipt = dict(profile_id=profile, principal_id=principal, request_id=request, session_id=owner, route=route,
                   policy=asdict(policy), entry=dict(session_id=owner, session_key=route, origin=origin,
                                                     created_at='2026-11-21T00:00:00+00:00',
                                                     updated_at='2026-11-21T00:00:00+00:00'))
    db._b1_epoch = begin_runtime_epoch(db, instance_id='test')
    commit_local_session(db, epoch=db._b1_epoch, receipt=receipt)
    db.append_message(owner, role='user', content=f'secret-{name}')
    db.append_message(owner, role='assistant', content=f'reply-{name}')
    if descendant:
        db.end_session(owner, 'compression')
        child = db.create_session(f'{name}-tip', source='tui', parent_session_id=owner,
                                  session_key=route, chat_id=owner, user_id=principal)
        db.append_message(child, role='user', content=f'tip-{name}')
        return owner, child
    return owner, owner


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / 'state.db') as store:
        yield store


def _first_id(db, sid):
    return db._read_one('SELECT MIN(id) AS i FROM messages WHERE session_id=?', (sid,))['i']


def _titled(db, sid, title):
    assert db.set_session_title(sid, title)
    return sid


# Every named point reader: name -> read(db, sid). Includes move and the timeline windows.
POINT_READERS = {
    'get_messages': lambda db, sid: db.get_messages(sid),
    'get_messages_as_conversation': lambda db, sid: db.get_messages_as_conversation(sid),
    'get_resume_conversations': lambda db, sid: db.get_resume_conversations(sid),
    'get_messages_around': lambda db, sid: db.get_messages_around(sid, _first_id(db, sid)),
    'get_anchored_view': lambda db, sid: db.get_anchored_view(sid, _first_id(db, sid)),
    'timeline_around': lambda db, sid: hermes_state_timeline.get_session_messages_around(db, sid, _first_id(db, sid)),
    'timeline': lambda db, sid: hermes_state_timeline.get_session_timeline(db, sid),
    'export_session': lambda db, sid: db.export_session(sid),
    'export_session_lineage': lambda db, sid: db.export_session_lineage(sid),
    'export_session_for_move': lambda db, sid: db.export_session_for_move(sid),
}


# 1. Locked session, no grant: every point reader raises, never returns [] or None.
@pytest.mark.parametrize('reader', POINT_READERS)
def test_locked_point_readers_raise_without_grant(db, tmp_path, reader):
    owner, _ = _record(db, tmp_path, 'a')
    with pytest.raises(RouteLockedContent):
        POINT_READERS[reader](db, owner)


# 2. Unlocked control: the same readers return the content.
@pytest.mark.parametrize('reader', POINT_READERS)
def test_unlocked_point_readers_return_content(db, tmp_path, reader):
    owner, _ = _record(db, tmp_path, 'open', lock=False)
    assert 'secret-open' in json.dumps(POINT_READERS[reader](db, owner), default=str)


# The owner grant reads every point reader, so a legitimate owner path is not collateral damage.
@pytest.mark.parametrize('reader', POINT_READERS)
def test_granted_owner_reads_every_point_reader(db, tmp_path, reader):
    owner, _ = _record(db, tmp_path, 'g')
    with owner_content_grant(db, owner):
        assert 'secret-g' in json.dumps(POINT_READERS[reader](db, owner), default=str)


# 3 (part): export_all leaves a locked session out and still exports the unlocked one.
def test_export_all_excludes_locked_keeps_unlocked(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'locked-x')
    open_, _ = _record(db, tmp_path, 'open-x', lock=False)
    exported = {s['id']: s for s in db.export_all()}
    assert locked not in exported
    assert [m['content'] for m in exported[open_]['messages']] == ['secret-open-x', 'reply-open-x']
    assert 'secret-locked-x' not in json.dumps(list(exported.values()))


# 4. get_session keeps identity and lifecycle, redacts the prompt of a locked row.
def test_get_session_redacts_prompt_of_locked_row_only(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'pl')
    open_, _ = _record(db, tmp_path, 'po', lock=False)
    db._write_sql('UPDATE sessions SET system_prompt=? WHERE id IN (?,?)', ('PROMPT', locked, open_))
    row = db.get_session(locked)
    assert row['id'] == locked and row['source'] == 'tui' and row['started_at'] is not None
    assert row['system_prompt'] is None and row['_system_prompt_resolved'] is None
    assert db.get_session(open_)['system_prompt'] == 'PROMPT'


# 5. Grant for session A reads A and A's compressed child; not another locked session; not another store.
def test_grant_covers_owner_lineage_only(db, tmp_path, monkeypatch):
    owner_a, child_a = _record(db, tmp_path, 'ga', descendant=True)
    owner_b, _ = _record(db, tmp_path, 'gb')
    with owner_content_grant(db, owner_a):
        assert db.get_messages(owner_a)[0]['content'] == 'secret-ga'
        assert db.get_messages(child_a)[0]['content'] == 'tip-ga'
        assert content_readable(db, child_a)
        with pytest.raises(RouteLockedContent):
            db.get_messages(owner_b)
    with SessionDB(tmp_path / 'other.db') as other:
        same_owner, _ = _record(other, tmp_path, 'ga', descendant=True)  # the same locked owner id, another store
        assert same_owner == owner_a
        with owner_content_grant(db, owner_a):
            with pytest.raises(RouteLockedContent):
                other.get_messages(owner_a)
    with pytest.raises(RouteLockedContent):
        db.get_messages(owner_a)


# 6. Fail closed: a local marker whose receipt is corrupt or missing counts as locked.
@pytest.mark.parametrize('damage', ['malformed', 'missing'])
def test_corrupt_or_missing_receipt_is_locked(db, tmp_path, damage):
    owner, _ = _record(db, tmp_path, damage, lock=False)
    if damage == 'malformed':
        db._write_sql('UPDATE state_meta SET value=? WHERE key=?', ('{', POLICY_PREFIX + owner))
    else:
        db._write_sql('DELETE FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,))
    assert not content_readable(db, owner)
    with pytest.raises(RouteLockedContent):
        db.get_messages(owner)


def test_route_locked_content_is_a_runtime_store_error():
    assert issubclass(RouteLockedContent, RuntimeStoreError)


# 7. Owner path end to end: the gateway's owner history load gets the transcript, not [] and not an error.
class _OwnerStore(SessionTranscriptMixin):
    def __init__(self, db):
        self._db = db

    def _db_for_session_id(self, session_id):
        return self._db

    def _follow_reroutes(self, session_id):
        return session_id


def test_owner_transcript_load_is_granted_and_plain_load_is_refused(db, tmp_path):
    owner, _ = _record(db, tmp_path, 'own')
    store = _OwnerStore(db)
    history = store.load_owner_transcript(owner)
    assert [m['content'] for m in history] == ['secret-own', 'reply-own']
    with pytest.raises(Exception) as refused:
        store.load_transcript(owner)
    assert type(refused.value).__name__ == 'TranscriptReadError'


# 8. Search filters locked rows BEFORE ranking and LIMIT/OFFSET: locked hits never push an unlocked one out.
def test_search_filters_locked_before_limit(db, tmp_path):
    open_, _ = _record(db, tmp_path, 'so', lock=False)
    db.append_message(open_, role='user', content='needle open-hit')
    locked, _ = _record(db, tmp_path, 'sl')  # newer than the unlocked one, so sort=newest ranks it first
    for i in range(3):
        db.append_message(locked, role='user', content=f'needle locked-hit-{i}')
    for kwargs in ({}, {'sort': 'newest'}, {'sort': 'oldest'}):
        hits = db.search_messages('needle', limit=1, **kwargs)
        assert [h['session_id'] for h in hits] == [open_], kwargs
    everything = db.search_messages('needle', limit=50, sort='newest')
    assert {h['session_id'] for h in everything} == {open_}
    assert 'locked-hit' not in json.dumps(everything, default=str)
    assert db.search_messages('needle', limit=1, offset=1) == []  # offset counts only readable hits


def test_search_returns_nothing_when_only_locked_matches(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'sx')
    db.append_message(locked, role='user', content='needle only-locked')
    assert db.search_messages('needle') == []
    assert db.search_messages('only-locked') == []
    assert db.search_messages('needle', fields=['snippet']) == []


# 9. Titles: a locked, ungranted row's title is not readable by id, exact title, numbered title or listing.
def test_locked_title_is_redacted_by_id_and_exact_lookup(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'tl')
    _titled(db, locked, 'Locked Plan')
    assert db.get_session_title(locked) is None
    assert db.get_session_by_title('Locked Plan') is None
    assert db.resolve_session_by_title('Locked Plan') is None
    assert (db.get_session(locked) or {}).get('title') is None
    assert db.get_session_title_source(locked) is None


def test_unlocked_title_control_resolves(db, tmp_path):
    open_, _ = _record(db, tmp_path, 'to', lock=False)
    _titled(db, open_, 'Open Plan')
    assert db.get_session_title(open_) == 'Open Plan'
    assert db.get_session_by_title('Open Plan')['id'] == open_
    assert db.resolve_session_by_title('Open Plan') == open_
    assert db.get_session(open_)['title'] == 'Open Plan'


def test_numbered_title_resolution_skips_locked_rows(db, tmp_path):
    base, _ = _record(db, tmp_path, 'nb', lock=False)
    _titled(db, base, 'Series')
    locked, _ = _record(db, tmp_path, 'nl')
    _titled(db, locked, 'Series #2')  # newest numbered continuation, but locked
    assert db.resolve_session_by_title('Series') == base  # never the locked row, never its id
    assert db.get_session_by_title('Series #2') is None
    open_n, _ = _record(db, tmp_path, 'nu', lock=False)
    _titled(db, open_n, 'Series #3')
    assert db.resolve_session_by_title('Series') == open_n  # the unlocked numbered row still wins
    only_locked, _ = _record(db, tmp_path, 'ol')
    _titled(db, only_locked, 'Solo #2')
    assert db.resolve_session_by_title('Solo') is None


def test_granted_owner_reads_its_own_title(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'tg', descendant=True)
    _titled(db, locked, 'Owner Plan')
    with owner_content_grant(db, locked):
        assert db.get_session_title(locked) == 'Owner Plan'
        assert db.get_session_by_title('Owner Plan')['id'] == locked
        assert db.resolve_session_by_title('Owner Plan') == locked
    assert db.get_session_title(locked) is None


def test_listing_never_carries_locked_title_or_preview(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'll')
    _titled(db, locked, 'Locked Listing')
    open_, _ = _record(db, tmp_path, 'lo', lock=False)
    _titled(db, open_, 'Open Listing')
    for listing in (db.list_sessions_rich(limit=50), db.list_sessions_rich(limit=50, include_children=True),
                    db.search_sessions(limit=50)):
        blob = json.dumps(listing, default=str)
        assert 'Locked Listing' not in blob and 'secret-ll' not in blob
        row = next(r for r in listing if r['id'] == open_)  # the unlocked row keeps title and preview
        assert row['title'] == 'Open Listing'
    assert next(r for r in db.list_sessions_rich(limit=50) if r['id'] == open_)['preview'] == 'secret-lo'


# 10. Grant rotation scope.
def test_grant_survives_rotation_inside_the_block_and_stays_scoped(db, tmp_path):
    owner_a, _ = _record(db, tmp_path, 'ra')
    owner_b, child_b = _record(db, tmp_path, 'rb', descendant=True)
    db._write_sql('UPDATE sessions SET system_prompt=? WHERE id IN (?,?)', ('PROMPT', owner_b, child_b))
    with owner_content_grant(db, owner_a):
        db.end_session(owner_a, 'compression')  # a legitimate rotation after the grant was taken
        tip = db.create_session('ra-tip', source='tui', parent_session_id=owner_a, session_key=db.get_session(owner_a)['session_key'],
                                chat_id=owner_a, user_id='local:test')
        db.append_message(tip, role='user', content='tip-ra')
        assert db.get_messages(tip)[0]['content'] == 'tip-ra'
        for sid in (owner_b, child_b):
            for name in ('get_messages', 'get_messages_around', 'get_anchored_view', 'timeline', 'export_session'):
                with pytest.raises(RouteLockedContent):
                    POINT_READERS[name](db, sid)
            assert db.get_session(sid)['system_prompt'] is None
    with pytest.raises(RouteLockedContent):
        db.get_messages(tip)


def test_grant_for_unlocked_session_opens_nothing(db, tmp_path):
    open_, _ = _record(db, tmp_path, 'gu', lock=False)
    locked, _ = _record(db, tmp_path, 'gl')
    with owner_content_grant(db, open_):
        with pytest.raises(RouteLockedContent):
            db.get_messages(locked)


# 11. Move: a locked session never leaves its store, even through the owner-less move path.
def test_locked_session_is_not_exported_for_move_but_unlocked_is(db, tmp_path):
    locked, _ = _record(db, tmp_path, 'mv')
    with pytest.raises(RouteLockedContent):
        db.export_session_for_move(locked)
    open_, _ = _record(db, tmp_path, 'mo', lock=False)
    payload = db.export_session_for_move(open_)
    assert payload['session']['id'] == open_ and len(payload['messages']) == 2


# 12. Cost budget: classifying an ordinary session (no local receipt) is ONE indexed statement.
def _statements(db, fn):
    seen = []
    with db._read_ctx() as conn:
        conn.set_trace_callback(seen.append)
        try:
            return fn(conn), seen
        finally:
            conn.set_trace_callback(None)


@pytest.mark.parametrize('chat_id', [None, 'some-chat'])
def test_ordinary_session_classification_is_one_indexed_statement(db, chat_id):
    from hermes_state_local import _locked_owner
    db.create_session('plain', source='cli', **({'chat_id': chat_id} if chat_id else {}))
    owner_locked, seen = _statements(db, lambda conn: _locked_owner(conn, 'plain'))
    assert owner_locked == (None, False)
    assert len(seen) == 1, seen
    with db._read_ctx() as conn:
        plan = [r['detail'] for r in conn.execute('EXPLAIN QUERY PLAN ' + seen[0])]
    assert plan and all(d.startswith('SEARCH ') and 'INDEX' in d for d in plan), plan  # no table scan


# 13. Reset beside compression: a grant on the owner reads its own reset and compression children, never another's.
def _reset_child(db, owner):
    from gateway.session import SessionEntry
    from hermes_state_local import local_receipt
    from hermes_state_local_lineage import reset_local_target
    old = SessionEntry.from_dict(local_receipt(db, owner)['entry'])
    child = f'{owner}-reset'
    entry = SessionEntry(old.session_key, child, old.created_at, old.updated_at, origin=old.origin,
                         platform=old.platform, chat_type=old.chat_type, is_fresh_reset=True)
    reset_local_target(db, epoch=db._b1_epoch, parent_session_id=owner, entry=entry.to_dict())
    db.append_message(child, role='user', content=f'reset-{owner[:8]}')
    return child


def test_grant_reads_own_reset_child_and_not_anothers(db, tmp_path):
    owner_a, _ = _record(db, tmp_path, 'xa')
    owner_b, _ = _record(db, tmp_path, 'xb')
    reset_a, reset_b = _reset_child(db, owner_a), _reset_child(db, owner_b)
    with owner_content_grant(db, owner_a):
        assert db.get_messages(reset_a)[0]['content'] == f'reset-{owner_a[:8]}'
        with pytest.raises(RouteLockedContent):
            db.get_messages(reset_b)
    with pytest.raises(RouteLockedContent):
        db.get_messages(reset_a)


def test_grant_reads_reset_then_compression_chain_and_stays_scoped(db, tmp_path):
    owner_a, _ = _record(db, tmp_path, 'ya')
    owner_b, child_b = _record(db, tmp_path, 'yb', descendant=True)
    reset_a = _reset_child(db, owner_a)
    db.end_session(reset_a, 'compression')
    tip_a = db.create_session('ya-tip', source='tui', parent_session_id=reset_a,
                              session_key=db.get_session(reset_a)['session_key'], chat_id=owner_a, user_id='local:test')
    db.append_message(tip_a, role='user', content='tip-ya')
    with owner_content_grant(db, owner_a):
        assert db.get_messages(tip_a)[0]['content'] == 'tip-ya'
        for sid in (owner_b, child_b):
            with pytest.raises(RouteLockedContent):
                db.get_messages(sid)
