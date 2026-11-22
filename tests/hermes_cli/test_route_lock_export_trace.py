"""B2: dashboard GET, portable export and trace refuse a route-locked session (P1/P3/P4).

Real entry points over a real SessionDB with the real locked fixture: the dashboard FastAPI app, the
``hermes sessions export`` command, and ``agent.trace_upload``. Only the HF upload edge is captured.
"""
import io
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

import hermes_state
from agent import trace_upload
from hermes_state import SessionDB
from hermes_state_local import RouteLockedContent
from tests.tui_gateway.test_route_lock_admission import _record

SECRET = 'SECRET-LOCKED-BODY'


def _seed(db, tmp_path, case):
    sid = _record(db, tmp_path, case)
    db.append_message(sid, role='user', content=SECRET + ' question')
    db.append_message(sid, role='assistant', content=SECRET + ' answer')
    return sid


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'state.db'
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', path)
    with SessionDB(path) as db:
        locked = _seed(db, tmp_path, 'locked')
        control = db.create_session('plain-control', source='cli')
        db.append_message(control, role='user', content='PLAIN-OPEN hello')
        db.append_message(control, role='assistant', content='PLAIN-OPEN world')
        db.set_session_title(control, 'Plain title')
        for sid in (locked, control):  # prune-style filters (bulk export) only match ended sessions
            db.end_session(sid, 'done')
        yield SimpleNamespace(db=db, path=path, locked=locked, control=control)


# -- dashboard GET (P1) -------------------------------------------------------

@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _refused(resp):
    assert resp.status_code == 409, (resp.status_code, resp.text)
    assert 'route_lock_mismatch' in resp.text and SECRET not in resp.text, resp.text


@pytest.mark.parametrize('suffix', ['/messages', '/timeline', '/messages/around?row_id=1', '/export'])
def test_dashboard_content_get_refuses_locked(client, stores, suffix):
    _refused(client.get(f'/api/sessions/{stores.locked}{suffix}'))


def test_dashboard_detail_is_metadata_only(client, stores):
    stores.db.set_session_title(stores.locked, SECRET + ' title')
    resp = client.get(f'/api/sessions/{stores.locked}')
    assert resp.status_code == 200 and SECRET not in resp.text, resp.text


def test_dashboard_unlocked_control_unchanged(client, stores):
    assert 'PLAIN-OPEN hello' in client.get(f'/api/sessions/{stores.control}/messages').text
    export = client.get(f'/api/sessions/{stores.control}/export')
    assert export.status_code == 200 and 'PLAIN-OPEN world' in export.text
    assert client.get(f'/api/sessions/{stores.control}/timeline').status_code == 200


# -- CLI export (P3) ----------------------------------------------------------

_FILTERS = dict.fromkeys(('older_than', 'newer_than', 'before', 'after', 'source', 'title', 'end_reason', 'cwd',
                          'min_messages', 'max_messages', 'model', 'provider', 'user', 'chat_id', 'chat_type',
                          'branch', 'min_tokens', 'max_tokens', 'min_cost', 'max_cost', 'min_tool_calls',
                          'max_tool_calls'))


def _export(stores, tmp_path, **kw):
    from hermes_cli.sessions_cmd import _cmd_export
    args = SimpleNamespace(**{**_FILTERS, 'session_id': None, 'format': 'jsonl', 'output': str(tmp_path / 'out.jsonl'),
                              'redact': False, 'dry_run': False, 'only': None, 'no_redact': False, 'upload': False,
                              'public': False, 'force': False, 'delete_after_verified': False, 'yes': False,
                              'lineage': 'single', **kw})
    out = io.StringIO()
    with redirect_stdout(out):
        code = _cmd_export(stores.db, args)
    return code, out.getvalue()


def _written(tmp_path):
    return [p for p in tmp_path.rglob('*') if p.is_file() and p.suffix in ('.jsonl', '.md', '.qmd', '.html')
            and p.name != 'state.db']


@pytest.mark.parametrize('fmt', ['jsonl', 'html', 'md', 'trace'])
def test_cli_single_export_refuses_locked(stores, tmp_path, fmt):
    out_dir = tmp_path / 'o'
    out_dir.mkdir()
    code, text = _export(stores, tmp_path, session_id=stores.locked, format=fmt, output=str(out_dir / 'x'))
    assert code == 1 and 'route_lock_mismatch' in text and SECRET not in text, (code, text)
    assert not list(out_dir.iterdir())


def test_cli_delete_after_verified_refuses_and_keeps_session(stores, tmp_path):
    code, text = _export(stores, tmp_path, session_id=stores.locked, format='md', output=str(tmp_path / 'md'),
                         delete_after_verified=True, yes=True)
    assert code == 1 and 'route_lock_mismatch' in text
    assert stores.db.get_session(stores.locked) is not None


def test_cli_single_export_unlocked_control(stores, tmp_path):
    code, text = _export(stores, tmp_path, session_id=stores.control)
    assert code in (None, 0), text
    assert 'PLAIN-OPEN world' in (tmp_path / 'out.jsonl').read_text()


@pytest.mark.parametrize('how', ['bare', 'filter'])
def test_cli_bulk_export_skips_and_reports_locked(stores, tmp_path, how):
    kw = dict(min_messages=0) if how == 'filter' else {}
    _, text = _export(stores, tmp_path, **kw)
    body = (tmp_path / 'out.jsonl').read_text()
    assert 'PLAIN-OPEN world' in body and SECRET not in body, text
    assert 'skipped 1 route-locked' in text.lower() and stores.locked in text, text


def test_cli_bulk_markdown_skips_locked(stores, tmp_path):
    _, text = _export(stores, tmp_path, format='md', output=str(tmp_path / 'md'), min_messages=0)
    assert 'skipped 1 route-locked' in text.lower(), text
    assert not any(SECRET in p.read_text() for p in _written(tmp_path)), text


def test_cli_trace_default_latest_session_refused(stores, tmp_path):
    # "the last thing I did" must not pick a locked session
    stores.db._write_sql('UPDATE messages SET timestamp=timestamp+1000 WHERE session_id=?', (stores.locked,))
    code, text = _export(stores, tmp_path, format='trace', output=str(tmp_path / 'tr.jsonl'))
    assert code == 1 and 'is route-locked' in text and not (tmp_path / 'tr.jsonl').exists(), (code, text)


# -- trace upload / render (P4) ----------------------------------------------

@pytest.fixture
def uploads(monkeypatch):
    sent = []
    monkeypatch.setattr(trace_upload, '_do_upload', lambda jsonl, **kw: sent.append(jsonl) or 'uploaded')
    return sent


def test_trace_upload_refuses_locked(stores, uploads):
    msg = trace_upload.upload_session_trace(stores.locked, db_path=stores.path, token='hf_fake')
    assert 'refused (route_lock_mismatch)' in msg and SECRET not in msg and uploads == []


def test_trace_loader_raises_typed_for_locked(stores):
    with pytest.raises(RouteLockedContent):
        trace_upload.load_session_messages(stores.locked, db_path=stores.path)


def test_trace_upload_unlocked_control(stores, uploads):
    assert trace_upload.upload_session_trace(stores.control, db_path=stores.path, token='hf_fake') == 'uploaded'
    assert 'PLAIN-OPEN world' in uploads[0]


def test_cli_export_race_lock_is_clean_refusal(stores, tmp_path, monkeypatch):
    # a lock minted between the preflight and the read: B1 raises, the command stops cleanly (no traceback)
    def raced(*a, **k):
        raise RouteLockedContent('route_lock_mismatch')
    monkeypatch.setattr(stores.db, 'export_session', raced)
    code, text = _export(stores, tmp_path, session_id=stores.control)
    assert code == 1 and 'route_lock_mismatch' in text and not (tmp_path / 'out.jsonl').exists()


def test_cli_trace_upload_refuses_locked(stores, tmp_path, uploads, monkeypatch):
    monkeypatch.setenv('HF_TOKEN', 'hf_fake')
    code, text = _export(stores, tmp_path, session_id=stores.locked, format='trace', upload=True)
    assert code == 1 and 'route_lock_mismatch' in text and uploads == []


def test_trace_refused_inside_owner_grant(stores):
    """An owner grant authorizes the owner's own replay read, never an operator-initiated trace (P4)."""
    from hermes_state_local import owner_content_grant
    with owner_content_grant(stores.db, stores.locked):
        assert stores.db.get_messages_as_conversation(stores.locked)  # the grant itself is live
        with pytest.raises(RouteLockedContent):
            trace_upload.read_session_messages(stores.db, stores.locked)


def test_cli_trace_render_uses_no_grant_loader_even_inside_owner_grant(stores, tmp_path, monkeypatch):
    """The CLI trace render must go through the shared no-grant loader (P4), not read the store directly.

    The preflight is disabled to simulate a lock minted after it; an owner grant would let a direct read succeed.
    """
    from hermes_cli import sessions_cmd
    from hermes_state_local import owner_content_grant
    monkeypatch.setattr(sessions_cmd, '_refuse_locked', lambda *a, **k: None)
    with owner_content_grant(stores.db, stores.locked):
        code, text = _export(stores, tmp_path, session_id=stores.locked, format='trace',
                             output=str(tmp_path / 'tr.jsonl'))
    assert code == 1 and 'route_lock_mismatch' in text and SECRET not in text, (code, text)
    assert not (tmp_path / 'tr.jsonl').exists()
