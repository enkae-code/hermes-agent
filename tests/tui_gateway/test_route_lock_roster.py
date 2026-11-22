"""Route-lock B3 roster: a locked session's newest message and titles never reach a Bots roster row.

``profiles.list`` (every profile's own store, cross-profile included) and the unified roster's
``_read_bot_sessions`` share ONE gated preview query. Titles are blanked for a row that is not
content-readable unless the caller is that row's owner.
"""
import json

import pytest

import tui_gateway.server as srv
from gateway.session_group_controls import _read_bot_sessions
from hermes_state import SessionDB
from tests.test_route_lock_content_boundary import _record


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / '.hermes'
    (h / 'profiles' / 'ops').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(h))
    return h


def _profile_db(home, name):
    return SessionDB(db_path=(home if name == 'default' else home / 'profiles' / name) / 'state.db')


def _seed(db, tmp_path, tag, *, lock):
    """A titled canonical "Bot Chat" row, then a newer conversation (``last_session``)."""
    canon, _ = _record(db, tmp_path, f'{tag}-canon', lock=lock)
    db.set_session_title(canon, 'Bot Chat')
    last, _ = _record(db, tmp_path, f'{tag}-last', lock=lock)
    db.set_session_title(last, f'Title-{tag}-last')
    db._write_sql('UPDATE sessions SET last_activity_at=? WHERE id=?', (9e9, last))
    return last, canon


def _roster(name):
    rows = srv._methods['profiles.list'](1, {})['result']['profiles']
    return next(p for p in rows if p['name'] == name)


@pytest.mark.parametrize('profile', ['default', 'ops'])
def test_profiles_list_blanks_locked_preview_incl_cross_profile(home, tmp_path, profile):
    db = _profile_db(home, profile)
    last, _ = _seed(db, tmp_path, 'lk', lock=True)
    db.close()
    row = _roster(profile)
    # The locked "Bot Chat" title is redacted, so the registry lookup finds no canonical row at all.
    assert row['last_session']['id'] == last and (row['canonical_session'] or {}).get('preview', '') == ''
    assert row['last_session']['preview'] == '' and row['last_session']['title'] == ''
    assert 'reply-lk' not in json.dumps(row) and 'secret-lk' not in json.dumps(row)


@pytest.mark.parametrize('profile', ['default', 'ops'])
def test_profiles_list_unlocked_control_keeps_preview(home, tmp_path, profile):
    db = _profile_db(home, profile)
    _seed(db, tmp_path, 'open', lock=False)
    db.close()
    row = _roster(profile)
    assert row['last_session']['preview'] == 'reply-open-last'
    assert row['canonical_session']['preview'] == 'reply-open-canon'


def test_read_bot_sessions_blanks_locked_titles_unless_owner(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        last, canon = _seed(db, tmp_path, 'lk', lock=True)
        # An operator who is not the owner sees neither title nor root title.
        view = _read_bot_sessions(db, 'local:other', True)
        for field in ('last_session', 'canonical_session'):
            assert view[field] and view[field]['title'] == '' and view[field]['root_title'] == ''
        assert view['last_session']['id'] == last and view['canonical_session']['id'] == canon
        # The owner keeps the titles; the preview stays gated (no grant on a roster read).
        mine = _read_bot_sessions(db, 'local:test', False)
        assert mine['last_session']['title'] == 'Title-lk-last'
        assert mine['canonical_session']['title'] == mine['canonical_session']['root_title'] == 'Bot Chat'
        assert mine['canonical_session']['preview'] == ''


def test_read_bot_sessions_unlocked_control_operator_sees_titles(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        _seed(db, tmp_path, 'open', lock=False)
        view = _read_bot_sessions(db, 'local:other', True)
        assert view['last_session']['title'] == 'Title-open-last'
        assert view['canonical_session']['title'] == 'Bot Chat'
