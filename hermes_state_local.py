"""Private local creation receipts in the canonical runtime transaction owner."""
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_state_runtime import RuntimeStoreError, _epoch, _json

POLICY_PREFIX = 'gateway.local_policy.v1:'
API_BINDING_PREFIX = 'gateway.api.binding.v1.'
API_DECLARED_PREFIX = 'gateway.api.conversation.v1.'


def commit_local_session(db, *, epoch, receipt):
    """Reserve the row, route and immutable policy together, before publication. A staged receipt's
    ``on_create`` (owner memory, never stored) is published by the transaction that creates the row
    and withdrawn if that transaction fails: an exact retry, a refused or a losing attempt adds none."""
    on_create = getattr(receipt, 'on_create', None)
    request = getattr(receipt, 'client_request', None)  # the staged request's own client_env, never stored
    if request is None:
        if receipt['policy'].get('client_env_json') is not None:
            # Lost (a copy): never judge a retry by this attempt's own filtered map instead.
            raise RuntimeStoreError('invalid_params')
        request = {}
    if on_create is not None:
        on_create.read()  # its file read: in the caller's thread, outside the write transaction
    encoded = _json(receipt)
    receipt = json.loads(encoded)
    sid, route = receipt['session_id'], receipt['route']
    key = POLICY_PREFIX + sid
    def write(conn):
        _epoch(conn, epoch)
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if old is not None:
            try:
                saved = json.loads(old[0])
                same = all(saved[k] == receipt[k] for k in (
                    'profile_id', 'principal_id', 'request_id', 'session_id', 'route'))
                same = same and same_create_retry(saved['policy'], receipt['policy']['request_json'], request)
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeStoreError('storage_unavailable') from exc
            if not same:
                raise RuntimeStoreError('invalid_params')
            return saved
        from hermes_state_mutation_retirement import RETIRED_PREFIX
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (RETIRED_PREFIX + sid,)).fetchone():
            # Deleted: its receipt was retired with the transcript; a create/cron retry must not
            # resurrect the row (and re-fire its prompt) under the same deterministic id.
            raise RuntimeStoreError('not_found')
        if ('legacy_session_id' not in receipt and
                conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone()):
            raise RuntimeStoreError('storage_unavailable')
        if conn.execute("SELECT 1 FROM gateway_routing WHERE scope='' AND session_key=?", (route,)).fetchone():
            raise RuntimeStoreError('admission_conflict')
        if 'legacy_session_id' in receipt:
            from hermes_state_local_migration import bind_legacy_target
            bind_legacy_target(db, conn, receipt)
        policy, entry = receipt['policy'], receipt['entry']
        from datetime import datetime
        started = datetime.fromisoformat(entry['created_at']).timestamp()
        # Auxiliary usage inserts created_source='unknown' when it creates the row
        # (record_auxiliary_usage carries no surface). The creator upsert repairs
        # 'unknown', but this DO NOTHING insert does not, so the surface is stamped here.
        # The agent's own accounting passes its surface (turn_usage._agent_session_source)
        # and is not that writer. gui's in-process agent is desktop; a managed worker
        # creates with the policy label gui.
        from gateway.session_policy import created_surface
        created = created_surface(policy['source'])
        conn.execute('''INSERT INTO sessions(id,source,created_source,user_id,session_key,chat_id,chat_type,
            model,cwd,profile_name,origin_json,started_at) VALUES(?,?,?,?,?,?,'dm',?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING''',
            (sid, policy['source'], created, receipt['principal_id'], route,
             entry['origin']['chat_id'], policy['model'], _row_cwd(policy), db._own_profile_name(),
             _json(entry['origin']), started))
        conn.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES('',?,?,?)",
                     (route, _json(entry), started))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, encoded))
        if on_create is not None:
            on_create.publish()  # before the row is visible, so no reader lazy-loads around it
        return receipt
    try:
        return db._execute_write(write)
    except BaseException:
        if on_create is not None:
            on_create.withdraw()  # only this attempt's own snapshot, never another winner's
        raise


def client_request(request_json, derived=None):
    """A frozen create request as the client sent it: an owner-resolved ``-m`` route (``model_requested``)
    compares by the name the client typed, not by the model and the *derived* fields (a receipt from
    before ``route_derived``: the endpoint) its ``${VAR}`` refs expanded to at the time. ``desktop_local``
    (``gateway.session_policy.LOCAL_DESKTOP_CHAT``), which an earlier owner stamped into the request, was
    never sent."""
    request = json.loads(request_json)
    request.pop('desktop_local', None)
    if derived is None:
        request.pop('route_derived', None)
    if 'model_requested' in request:
        request.pop('model', None)
        for key in ('base_url',) if derived is None else derived:
            request.pop(key, None)
    return request


def same_create_request(saved_json, retry_json):
    """Whether a create retry carries the frozen request it resumes: one check for a retry that finds the
    session committed and one staged before it was. The saved receipt's provenance names the owner's
    fields; one from before a same-name alias got ``model_requested`` compares exactly, as it did."""
    derived = json.loads(saved_json).get('route_derived')
    if client_request(saved_json, derived) == client_request(retry_json, derived):
        return True
    retry = json.loads(retry_json)
    if retry.get('model_requested') == retry.get('model'):
        retry.pop('model_requested', None)
        retry.pop('route_derived', None)
    return json.loads(saved_json) == retry


def same_create_retry(saved_policy, retry_json, client_request):
    """The one exact-retry identity of a create: ``same_create_request`` on what the client sent, and its
    ``client_env`` (``client_request``: the retry's own, never stored) judged by the rules frozen with
    *saved_policy*. Used by a retry that finds the session committed, at commit, and by one overlapping
    the attempt that holds the session's key slot."""
    from agent.session_client_env import retry_matches
    return same_create_request(saved_policy['request_json'], retry_json) and retry_matches(saved_policy, client_request)


def _row_cwd(policy):
    """The sessions-row ``cwd``: the create request's ``row_cwd`` (``""`` = none) when it sent one,
    else the execution cwd. Row metadata only (workspace grouping, ``-c``, cwd restore); the
    frozen policy's ``cwd`` / ``TERMINAL_CWD`` stay where the session's tools run."""
    try:
        request = json.loads(policy.get('request_json') or '{}')
    except (TypeError, ValueError):
        request = {}
    value = request.get('row_cwd') if isinstance(request, dict) else None
    return policy['cwd'] if not isinstance(value, str) else (value or None)


def _policy_target(raw):
    """Current physical target a creation receipt routes to; None for a malformed receipt, which
    then stays (its own reads already refuse it as ``storage_unavailable``) and cannot abort
    the delete of an unrelated session."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    entry = value.get('entry') if isinstance(value, dict) else None
    return entry.get('session_id') if isinstance(entry, dict) else None


def retire_local_receipts(conn, session_ids):
    """Deletion fence half for owner receipts. A local creation policy carries the launch request
    (``request_json``: a cron fire's job and raw prompt), so it goes exactly when its route does:
    when the receipt's current physical target is deleted (a pruned reset ancestor whose logical
    session lives on in a child keeps it). An API binding and the declared conversation key that
    resolves to it go with their id, so that key starts a fresh session."""
    targets = set(session_ids)
    if not targets:
        return
    # Prefix scans (GLOB uses the key index), not one LIKE per id: a prune retires thousands.
    gone = [key for key, value in conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ?',
                                               (POLICY_PREFIX + '*',))
            if _policy_target(value) in targets]
    gone += [key for key, value in conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ?',
                                                (API_DECLARED_PREFIX + '*',)) if value in targets]
    gone += [API_BINDING_PREFIX + sid for sid in targets]
    conn.executemany('DELETE FROM state_meta WHERE key=?', [(key,) for key in gone])


def moved_receipts(conn, target_ids=None):
    """``{owner id: (moved row id, its source)}`` of local receipts whose current physical target row
    now carries another lane's route. An explicit repoint (``/handoff``, a platform ``/resume``)
    rewrote that row's routing identity, so the receipt can never restore the conversation again
    (the stored identity check in ``gateway.session_local_recovery._checked_entry``). Only the
    current target counts: an earlier ``/new`` segment that moved leaves the conversation the
    receipt guards local, and releasing it would strand that transcript. *target_ids* narrows the
    scan to the rows one repoint rewrote, and those are returned locked or not, so the repoint's own
    ``hand_off_conversations`` can refuse (B2). The boot scan (no *target_ids*) is a heal, never a
    refusal: it leaves a route-locked receipt where it is."""
    rows = conn.execute(
        "SELECT substr(m.key, ?), s.id, s.source, m.value FROM state_meta m JOIN sessions s "
        "ON s.id = CASE WHEN json_valid(m.value) THEN json_extract(m.value, '$.entry.session_id') END "
        "WHERE m.key GLOB ? AND s.session_key IS NOT NULL "
        "AND s.session_key IS NOT CASE WHEN json_valid(m.value) THEN json_extract(m.value, '$.route') END",
        (len(POLICY_PREFIX) + 1, POLICY_PREFIX + '*')).fetchall()
    return {owner: (target, source) for owner, target, source, raw in rows
            if (target in target_ids if target_ids is not None else not _receipt_locked(raw))}


def release_receipts(conn, owners):
    """Retire *owners*' local receipts (creation policy and legacy binding): the row they guarded now
    belongs to a messaging lane, which restores it from its route like any other platform session."""
    from hermes_state_local_migration import LEGACY_PREFIX
    conn.executemany('DELETE FROM state_meta WHERE key IN (?,?)',
                     [(POLICY_PREFIX + owner, LEGACY_PREFIX + owner) for owner in owners])


def handoff_head(db, session_id):
    """``(local owner or None, row)`` a ``/handoff`` of *session_id* moves: the conversation the user
    is looking at. A local conversation continues from its receipt's current target (its latest
    ``/new`` segment, then that segment's compression tip), never from an earlier segment; any other
    row from its compression tip."""
    with db._read_ctx() as conn:
        owner = local_lineage_owner(conn, session_id)
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
    target = _policy_target(saved[0]) if saved else None
    require_movable(db, (session_id, owner, target))  # before any caller writes: a handoff has no own-route case
    start = target or session_id
    return (owner if target else None), db.get_compression_tip(start) or start


_LANE_IDENTITY = 'session_key, source, user_id, chat_id, chat_type, thread_id, display_name, origin_json, transport_profile'


def hand_off_conversations(conn, moved):
    """Retire the receipts in *moved* (``moved_receipts``: a messaging lane took over each one's
    current target) and move the rest of each conversation onto that lane with it. Its earlier
    ``/new`` segments take the lane's identity, so they open there as read-only history (a lane
    moved past them) instead of staying local rows no receipt restores; the conversation's name
    follows its head, which the lane continues as its own conversation. Returns the receipts it
    handed off: one whose lineage is malformed stays as it is (its own reads refuse it, logged).

    A route-locked receipt is never retired: the whole write is refused ``route_lock_mismatch`` before
    its first statement, so the caller's transaction (which already rewrote the row's lane identity)
    rolls back with the receipt, the rows and the lane as they were (B2)."""
    from gateway.session_route_lock import refusal
    for owner in moved:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
        if saved and _receipt_locked(saved[0]):
            raise refusal('this session is route-locked: it must run through its owning gateway')
    handed = {}
    for owner, (target, source) in moved.items():
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
        lineage = (_receipt(saved[0]) or {}).get('lineage', [owner]) if saved else [owner]
        if not isinstance(lineage, list) or not all(isinstance(sid, str) for sid in lineage):
            continue
        segments = list(dict.fromkeys([owner, *lineage]))
        conn.execute(f'UPDATE sessions SET ({_LANE_IDENTITY}) = (SELECT {_LANE_IDENTITY} FROM sessions WHERE id=?) '
                     'WHERE id IN (SELECT value FROM json_each(?)) AND id != ?', (target, json.dumps(segments), target))
        _title_follows_head(conn, owner, _segment_root(conn, segments, target))
        handed[owner] = (target, source)
    release_receipts(conn, handed)
    return handed


def _receipt(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _receipt_locked(raw):
    """Whether the stored receipt *raw* freezes a route, read from the receipt itself: a repointed row no
    longer matches the identity ``_locked_owner`` verifies, so that cannot classify it. The same
    predicate (``is_locked``, and ``_verified_legacy_policy``'s missing-lock case); it does not re-validate
    the whole policy (an older receipt must still be movable). Anything it cannot read counts as locked."""
    from types import SimpleNamespace

    from gateway.session_route_lock import is_locked
    try:
        policy = _receipt(raw)['policy']
        return (is_locked(SimpleNamespace(route_lock_json=policy.get('route_lock_json')))
                or json.loads(policy['request_json']).get('fallback') == 'none')
    except Exception:
        return True


def require_movable(db, session_ids, route=None):
    """Refuse ``route_lock_mismatch`` unless every local conversation owning one of *session_ids* is
    unlocked, or *route* is its own (a local session's own route follows its compression tip). Before any
    write of a repoint onto a messaging lane, for the resolved owner and its lineage (B2)."""
    if not hasattr(db, 'transcript_read_snapshot'):  # only a real SessionDB persists route locks
        return
    from gateway.session_route_lock import refusal
    with db._read_ctx() as conn, content_snapshot(conn):
        for sid in dict.fromkeys(filter(None, session_ids)):
            owner, locked = _locked_owner(conn, sid)
            if not locked:
                continue
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone() if owner else None
            if route is None or not saved or (_receipt(saved[0]) or {}).get('route') != route:
                raise refusal('this session is route-locked: it must run through its owning gateway')


def _segment_root(conn, segments, target):
    """The first row of *target*'s ``/new`` segment: back through the compressions behind it."""
    if target not in segments:
        return target
    index = segments.index(target)
    while index > 0 and (row := conn.execute('SELECT end_reason FROM sessions WHERE id=?',
                                               (segments[index - 1],)).fetchone()) and row[0] == 'compression':
        index -= 1
    return segments[index]


def _title_follows_head(conn, owner, head):
    """The conversation's name (a Bot Chat's registry title included) moves to its head, over a
    title of lower provenance there (a generated one); a name of equal rank the head carries stays
    (``SessionDB._title_rank``: a pre-provenance title ranks as the user's)."""
    from hermes_state import SessionDB
    row = conn.execute('SELECT title, title_source, hidden FROM sessions WHERE id=?', (owner,)).fetchone()
    if head == owner or row is None or not row[0]:
        return
    kept = conn.execute('SELECT title, title_source FROM sessions WHERE id=?', (head,)).fetchone()
    if kept is None or (kept[0] and SessionDB._title_rank(kept[1]) >= SessionDB._title_rank(row[1])):
        return
    conn.execute('UPDATE sessions SET title=NULL WHERE id IN (?,?)', (owner, head))
    conn.execute('UPDATE sessions SET title=?, title_source=?, hidden=? WHERE id=?', (*row, head))


def local_lineage_owner(conn, session_id):
    """The creation id whose receipt lineage holds physical segment *session_id* (a reset or
    compression child, which carries the owner as ``chat_id``); *session_id* itself otherwise."""
    row = conn.execute('SELECT chat_id FROM sessions WHERE id=?', (session_id,)).fetchone()
    owner = row[0] if row else None
    if not owner or owner == session_id:
        return session_id
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
    try:
        lineage = json.loads(saved[0]).get('lineage') if saved else None
    except (TypeError, ValueError, AttributeError):
        return session_id
    return owner if isinstance(lineage, list) and session_id in lineage else session_id


def owned_lineage_ids(conn, session_id):
    """[session_id] for any row outside a local reset/compression lineage, else the whole
    conversation (*session_id* first). Any segment deletes as all of it: the owner's policy goes
    with its current target, and an earlier segment's gap breaks the lineage, so a lone segment
    delete strands the rest (and the owner's queued work) readable but never continuable."""
    return list(dict.fromkeys([session_id, *local_conversation_ids(conn, session_id)]))


def local_conversation_ids(conn, session_id):
    """Every row of the local conversation *session_id* belongs to: its creation id first, then
    each segment of the receipt lineage. A canonical reset never forks the logical session (one
    policy, FIFO and generation), so listing, delete and retirement treat these ids as one."""
    owner = local_lineage_owner(conn, session_id)
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
    try:
        lineage = json.loads(saved[0]).get('lineage') if saved else None
    except (TypeError, ValueError, AttributeError):
        lineage = None
    if not isinstance(lineage, list):
        return [session_id]
    return list(dict.fromkeys([owner, *(sid for sid in lineage if isinstance(sid, str))]))


def local_lineage_index(conn):
    """``(superseded, heads)`` for listing a local conversation as one row, as
    ``acp_adapter.catalog`` does: a canonical reset keeps the creation id as the owner and moves the
    transcript to a child, so every segment is one conversation. ``superseded`` holds the listable
    roots of earlier reset segments (filtered in SQL, before paging); ``heads`` maps the latest
    reset segment to ``(owner, lineage)`` for the row that represents the conversation. A delete
    of that row removes the whole lineage (``hermes_state_mutation_guards.delete_targets``)."""
    superseded, heads = set(), {}
    rows = conn.execute("SELECT value FROM state_meta WHERE key GLOB ? AND json_valid(value) "
                        "AND json_array_length(value, '$.lineage') > 1", (POLICY_PREFIX + '*',)).fetchall()
    for (raw,) in rows:
        receipt = json.loads(raw)
        owner, lineage = receipt.get('session_id'), receipt.get('lineage')
        if not isinstance(owner, str) or not all(isinstance(sid, str) for sid in lineage):
            continue
        marks = ','.join('?' * len(lineage))
        ended = dict(conn.execute(f'SELECT id,end_reason FROM sessions WHERE id IN ({marks})', lineage).fetchall())
        starts = [sid for i, sid in enumerate(lineage) if i == 0 or ended.get(lineage[i - 1]) == 'session_reset']
        superseded.update(starts[:-1])
        heads[starts[-1]] = (owner, lineage)
    return superseded, heads


def _lineage_version(conn):
    # data_version sees OTHER connections' commits; total_changes sees this connection's writes.
    return conn.total_changes, conn.execute('PRAGMA main.data_version').fetchone()[0]


def _copy_lineage_index(value):
    superseded, heads = value
    return set(superseded), {key: (owner, list(lineage)) for key, (owner, lineage) in heads.items()}


def _cached_local_lineage_index(db, conn):
    # Owned by this SessionDB, not a process-global path/id cache. Bound retained connections
    # across read-pool churn and close/reopen; a closed/replaced connection cannot match a new one.
    cache, lock = db.__dict__.setdefault('_local_lineage_cache', (OrderedDict(), threading.Lock()))
    if conn.in_transaction:
        # A snapshot or uncommitted answer must never outlive its transaction (including rollback).
        with lock:
            cache.pop(conn, None)
        return local_lineage_index(conn)
    version = _lineage_version(conn)
    with lock:
        saved = cache.get(conn)
        if saved is not None and saved[0] == version:
            cache.move_to_end(conn)
            return _copy_lineage_index(saved[1])
    value = local_lineage_index(conn)
    # A concurrent writer may commit between the receipt scan and the end_reason reads. Never
    # publish that in-flight projection as a stable cache entry; the next call must rebuild it.
    if not conn.in_transaction and _lineage_version(conn) == version:
        with lock:
            cache[conn] = (version, value)
            cache.move_to_end(conn)
            if len(cache) > 8:
                cache.popitem(last=False)
    return _copy_lineage_index(value)


def exclude_superseded_segments(db, where_clauses, params):
    """Drop earlier reset segments from a listing or count query (one conversation, one row),
    in SQL before paging; returns ``heads`` for :func:`annotate_local_lineages`."""
    with db._read_ctx() as conn:
        superseded, heads = _cached_local_lineage_index(db, conn)
    if superseded:
        where_clauses.append("s.id NOT IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(sorted(superseded)))
    return heads


def annotate_local_lineages(sessions, heads):
    """Name every segment on the conversation's representative row (``_lineage_ids``, which the
    Desktop matches deletes, tombstones and stored ids against). ``_lineage_root_id`` stays the
    compression root: pins and archive flags live on the listed segment's own chain."""
    for row in sessions:
        found = heads.get(row.get('_lineage_root_id') or row['id'])
        if found is not None:
            row['_lineage_ids'] = list(dict.fromkeys([*found[1], *(row.get('_lineage_ids') or ())]))
    return sessions


def local_receipt(db, session_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + session_id,)).fetchone()
    if row is None:
        raise RuntimeStoreError('storage_unavailable')
    try:
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError('invalid receipt')
        return value
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc


def _legacy_policy_owner(conn, row):
    from hermes_state_local_migration import LEGACY_PREFIX

    session_id, chat_id, route = row['id'], row['chat_id'], row['session_key']
    parts = (route or '').split(':', 4)
    local_route = len(parts) >= 3 and parts[0] == 'agent' and parts[2] == 'local'
    route_owner = parts[4] if local_route and len(parts) == 5 else None
    candidates = tuple(dict.fromkeys(filter(None, (session_id, chat_id, route_owner))))
    keys = {prefix + candidate: (candidate, prefix == POLICY_PREFIX)
            for candidate in candidates for prefix in (POLICY_PREFIX, LEGACY_PREFIX)}
    found = {}
    for key, value in conn.execute(
            f"SELECT key,value FROM state_meta WHERE key IN ({','.join('?' for _ in keys)})", tuple(keys)):
        candidate, policy = keys[key]
        if policy or candidate not in found:
            found[candidate] = (value,) if policy else None
    owners = list(found.items())
    origin = _receipt(row['origin_json']) or {}
    canonical = local_route or origin.get('platform') == 'local'
    if not owners and not canonical:
        return None
    if len(owners) != 1 or owners[0][1] is None:
        raise RuntimeStoreError('storage_unavailable')
    owner, saved = owners[0]
    return owner, json.loads(saved[0])


def _verified_legacy_policy(conn, row, owner, receipt):
    from gateway.session import SessionEntry, build_session_key
    from gateway.session_policy import restore_policy
    from hermes_state_keys import profile_from_session_key
    from hermes_state_local_lineage import adoptable_compression, validate_local_lineage, with_adopted_compression

    route, entry = receipt['route'], SessionEntry.from_dict(receipt['entry'])
    origin = entry.origin
    profile = profile_from_session_key(route)
    identity = (route, owner, receipt['principal_id'])
    if (receipt['session_id'] != owner or entry.session_key != route or origin is None or origin.platform.value != 'local'
            or (origin.chat_id, origin.user_id) != identity[1:] or origin.chat_type != 'dm'
            or (origin.profile is not None and origin.profile != profile)
            or route != build_session_key(origin, profile=profile)
            or (row['session_key'], row['chat_id'], row['user_id']) != identity):
        raise RuntimeStoreError('storage_unavailable')
    routed = conn.execute("SELECT entry_json FROM gateway_routing WHERE scope='' AND session_key=?", (route,)).fetchone()
    if routed is None:
        raise RuntimeStoreError('storage_unavailable')
    routed_entry = SessionEntry.from_dict(json.loads(routed[0]))
    if (routed_entry.session_id != entry.session_id or routed_entry.session_key != route
            or routed_entry.origin != origin):
        raise RuntimeStoreError('storage_unavailable')
    if (adoptable := adoptable_compression(conn, receipt)) is not None:
        receipt = with_adopted_compression(receipt, adoptable[0])
    tip = validate_local_lineage(conn, receipt)
    target = conn.execute('SELECT session_key,chat_id,user_id FROM sessions WHERE id=?', (tip,)).fetchone()
    if target is None or tuple(target) != identity or row['id'] not in receipt.get('lineage', [owner]):
        raise RuntimeStoreError('storage_unavailable')
    policy = restore_policy(receipt['policy'])
    if policy.route_lock_json is None and json.loads(policy.request_json).get('fallback') == 'none':
        raise RuntimeStoreError('storage_unavailable')
    return policy


def require_legacy_execution(db, session_id, *, allow_unpersisted=False):
    """Check the selected store's persisted policy, including the compression segments a donor would move.

    Only legacy live/minted keys may have no row; a surviving canonical marker still requires a receipt.
    """
    from gateway.session_route_lock import is_locked, refusal

    if not hasattr(db, 'transcript_read_snapshot'):  # only a real SessionDB persists route locks; adaptors cannot hold one
        return
    with db.transcript_read_snapshot() as consistent:
        lineage = db.get_compression_lineage(session_id) or [session_id]
        with db._read_ctx() as conn:
            for segment in lineage:
                row = conn.execute('SELECT id,chat_id,session_key,user_id,origin_json FROM sessions WHERE id=?',
                                   (segment,)).fetchone()
                if row is None:
                    if not allow_unpersisted or segment.startswith('local-'):
                        raise RuntimeStoreError('storage_unavailable')
                    row = dict(id=segment, chat_id=None, session_key=segment, user_id=None, origin_json=None)
                owned = _legacy_policy_owner(conn, row)
                if owned is not None and is_locked(_verified_legacy_policy(conn, row, *owned)):
                    raise refusal('this session must run through its owning gateway')
        if not consistent():
            raise RuntimeStoreError('storage_unavailable')


class RouteLockedContent(RuntimeStoreError):
    """A stored-content read of a route-locked session without an owner grant (phase B1)."""


# (db, owner id) of the owner read in progress on this thread or task. Set only by owner_content_grant.
_OWNER_GRANT: ContextVar[tuple | None] = ContextVar('route_lock_owner_grant', default=None)


@contextmanager
def content_snapshot(conn):
    """Reuse an outer transaction or hold one SQLite generation for authorization and payload."""
    began = not conn.in_transaction
    if began:
        conn.execute('BEGIN')
    try:
        yield conn
    finally:
        if began and conn.in_transaction:
            conn.execute('ROLLBACK')


@contextmanager
def content_sql_filter(db, conn, *, allow_grant=False):
    """The same policy in SQL WHERE/CASE, before ranking, pagination and hydration."""
    answers = {}

    def readable(sid):
        if sid not in answers:
            answers[sid] = content_readable(db, sid, conn=conn, allow_grant=allow_grant)
        return answers[sid]

    with content_snapshot(conn):
        conn.create_function('route_content_readable', 1, readable)
        try:
            yield conn
        finally:
            conn.create_function('route_content_readable', 1, None)


def _locked_owner(conn, session_id):
    """``(owner, locked)`` of physical segment *session_id*: the creation id whose receipt it belongs to,
    and whether that receipt is route-locked. A canonical marker that cannot be verified is locked with
    no owner, so no grant matches it (fail closed). A session with no local receipt is ``(None, False)``.

    An ordinary row costs this one indexed statement: its own row left-joined to the receipt keys its
    id and ``chat_id`` could name. Anything that matched, or looks local, takes the full owner lookup.
    The receipt is verified by what it owns (identity plus ``validate_local_lineage``), not by the
    legacy-binding shape ``_verified_legacy_policy`` demands of an execution: a canonical ``local:<id>``
    route is a valid owner here and must not read as unverifiable (it locked every reset child out)."""
    from gateway.session_route_lock import is_locked

    rows = conn.execute(
        'SELECT s.id,s.chat_id,s.session_key,s.user_id,s.origin_json,m.key FROM sessions s '
        'LEFT JOIN state_meta m ON m.key IN (?1||s.id,?2||s.id,?1||s.chat_id,?2||s.chat_id) WHERE s.id=?3',
        (POLICY_PREFIX, _legacy_prefix(), session_id)).fetchall()
    if not rows:
        return None, False
    row = rows[0]
    try:
        if len(rows) == 1 and rows[0]['key'] is None and not _canonical(row):
            return None, False
        owned = _legacy_policy_owner(conn, row)
        if owned is None:
            return None, False
        return owned[0], is_locked(_owned_policy(conn, row, *owned))
    except sqlite3.Error:
        raise
    except Exception:
        return None, True


def _legacy_prefix():
    from hermes_state_local_migration import LEGACY_PREFIX
    return LEGACY_PREFIX


def _canonical(row):
    parts = (row['session_key'] or '').split(':', 4)
    return (len(parts) >= 3 and parts[0] == 'agent' and parts[2] == 'local'
            or (_receipt(row['origin_json']) or {}).get('platform') == 'local')


def _owned_policy(conn, row, owner, receipt):
    """*receipt*'s frozen policy once *row* is verified to be one of *owner*'s lineage segments."""
    from gateway.session_policy import restore_policy
    from hermes_state_local_lineage import adoptable_compression, validate_local_lineage, with_adopted_compression

    if (adoptable := adoptable_compression(conn, receipt)) is not None:
        receipt = with_adopted_compression(receipt, adoptable[0])
    validate_local_lineage(conn, receipt)
    if (receipt['session_id'] != owner or row['id'] not in receipt.get('lineage', [owner])
            or (row['session_key'], row['chat_id'], row['user_id']) != (receipt['route'], owner, receipt['principal_id'])):
        raise RuntimeStoreError('storage_unavailable')
    return restore_policy(receipt['policy'])


def content_readable(db, session_id, *, conn=None, allow_grant=True):
    """Whether *session_id*'s stored content may be read here: it is unlocked, or this thread holds an
    owner grant on this same ``db`` for its owner."""
    if conn is None:
        with db._read_ctx() as reader, content_snapshot(reader):
            return content_readable(db, session_id, conn=reader, allow_grant=allow_grant)
    owner, locked = _locked_owner(conn, session_id)
    if not locked:
        return True
    grant = _OWNER_GRANT.get() if allow_grant else None
    return grant is not None and grant[0] is db and owner is not None and owner == grant[1]


def require_content(db, session_id, *, conn=None, allow_grant=True):
    """Raise ``RouteLockedContent`` unless :func:`content_readable`."""
    if not content_readable(db, session_id, conn=conn, allow_grant=allow_grant):
        raise RouteLockedContent('route_lock_mismatch', field='content', detail='this session is route-locked')


@contextmanager
def owner_content_grant(db, session_id):
    """Grant this block the stored content of *session_id*'s local owner, on *db* only. Use it at an owner
    replay or resume call site, after the turn or view was admitted for that session. The grant covers
    the owner's compression segments and nothing else: another locked session, or the same id on another
    SessionDB instance, stays denied."""
    with db._read_ctx() as conn:
        owner, _ = _locked_owner(conn, session_id)
    token = _OWNER_GRANT.set((db, owner) if owner is not None else None)
    try:
        yield
    finally:
        _OWNER_GRANT.reset(token)


def end_idle_local_session(db, *, epoch, session_id, target_id, reason):
    """Stamp ``ended_at`` on *target_id* only while the logical *session_id*'s FIFO is idle.

    The idle check and the stamp share one owner write txn, so an admission committed first keeps
    the row open; one committed after is reopened by the drain (``reopen_local_session``)."""
    def write(conn):
        _epoch(conn, epoch)
        if conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status IN ('queued','started','unknown')",
                        (session_id,)).fetchone():
            return 0
        if conn.execute("SELECT 1 FROM worker_executions WHERE session_id IN (?,?) AND status IN ('registered','running','unknown')",
                        (session_id, target_id)).fetchone():
            return 0
        return db._end_and_bump(conn, 'UPDATE sessions SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL',
                                (time.time(), reason, target_id), target_id, reason)
    return db._execute_write(write)
