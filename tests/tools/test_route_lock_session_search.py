"""Route-lock projections at the real session_search entry point."""
import json

import pytest

from hermes_state import SessionDB
from tests.test_route_lock_content_boundary import _record
from tools.session_search_tool import session_search


@pytest.mark.parametrize('cross_profile', [False, True])
def test_search_omits_locked_but_keeps_unlocked(tmp_path, monkeypatch, cross_profile):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    home = tmp_path / 'profiles' / 'isolated' if cross_profile else tmp_path
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text('{}')
    with SessionDB(home / 'state.db') as db:
        locked, _ = _record(db, tmp_path, 'locked')
        opened, _ = _record(db, tmp_path, 'open', lock=False)
        db.set_session_title(locked, 'Secret locked title')
        anchor = db._read_one('SELECT MIN(id) AS i FROM messages WHERE session_id=?', (locked,))['i']
        scope = {'profile': 'isolated'} if cross_profile else {'db': db}
        for query in ({}, {'query': 'secret'}, {'query': 'Secret locked title OR secret'}):
            result = json.loads(session_search(**scope, **query))
            assert [hit['session_id'] for hit in result['results']] == [opened]
        assert locked not in session_search(**scope, query='Secret locked title')
        for params in ({}, {'around_message_id': anchor}):
            result = json.loads(session_search(**scope, session_id=locked, **params))
            assert result['success'] is False
            assert 'not available' in result['error']
        assert 'secret-open' in session_search(**scope, session_id=opened)
