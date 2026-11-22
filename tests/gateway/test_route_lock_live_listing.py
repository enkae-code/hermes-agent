"""Route-lock B4 N2: ``session.list`` (live) and ``session.active_list`` blank a locked session's title.

An operator who is not the owner reads the row (profile-local access) but a locked title is content:
only the owner, or ``route_content_readable``, sees it. One gate in ``live_entries`` serves both verbs.
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tests.test_route_lock_content_boundary import _record


@pytest_asyncio.fixture
async def feed(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=None, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id='live', instance_id='owner')
    for tag, lock in (('lk', True), ('open', False)):
        sid, _ = _record(store._db, tmp_path, tag, lock=lock)
        store._db.set_session_title(sid, f'Title-{tag}')
        authority.sessions[sid] = LiveSession(
            SessionSource(platform=Platform.LOCAL, chat_id=sid, user_id='local:test'), sid)

    async def read(method, identity, operator):
        native = dict(identity, provider='local', profile_id='live', instance_id='owner',
                      capabilities=['session:read'])  # a native grant keeps its subject
        connection = AuthorityConnection(authority, object(), native, operator=operator)
        reply = await connection.dispatch({'id': 1, 'method': method, 'params': {}})
        assert 'result' in reply, reply
        return sorted(row['title'] for row in reply['result']['sessions'])

    yield read


VERBS = ('session.list', 'session.active_list')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', VERBS)
async def test_operator_non_owner_gets_blank_locked_title(feed, method):
    # The locked row is still listed, with a blank title; the unlocked control keeps its title.
    assert await feed(method, {'user_id': 'local:other'}, True) == ['', 'Title-open']


@pytest.mark.asyncio
@pytest.mark.parametrize('method', VERBS)
async def test_owner_keeps_locked_title(feed, method):
    assert await feed(method, {'user_id': 'local:test'}, False) == ['Title-lk', 'Title-open']
