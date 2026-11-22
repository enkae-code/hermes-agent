"""B2 operation fences: the ACP adapter and the classic CLI refuse a route-locked stored session.

Real ``SessionDB`` + ``commit_local_session`` fixture (``_record``), real ``SessionManager`` and real
``HermesCLI`` resume/branch commands. Only agent construction is replaced, to prove it never ran.
Classic CLI tests live here too: this file is the only test path this slice owns.
"""
import io
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from acp_adapter.session import SessionManager
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, require_legacy_execution
from hermes_state_runtime import RuntimeStoreError
from tests.tui_gateway.test_route_lock_admission import _record

SECRET = 'transcript secret'


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        yield db


def _seed(db, tmp_path, case, source=None):
    sid = _record(db, tmp_path, case)
    db.append_message(sid, 'user', SECRET)
    db.append_message(sid, 'assistant', 'reply')
    if source:
        db._write_sql('UPDATE sessions SET source=? WHERE id=?', (source, sid))
    return sid


def _snapshot(db, sid):
    """Everything a refused operation must leave alone: row count, this session's rows, its receipt."""
    with db._read_ctx() as conn:
        sessions = conn.execute('SELECT id,ended_at,end_reason FROM sessions ORDER BY id').fetchall()
        messages = conn.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id', (sid,)).fetchall()
        receipt = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone()
    return [tuple(r) for r in sessions], [tuple(r) for r in messages], receipt and receipt[0]


def _assert_still_locked(db, sid, before):
    assert _snapshot(db, sid) == before
    assert before[2] is not None  # the receipt exists ...
    with pytest.raises(RuntimeStoreError) as refused:  # ... and still refuses
        require_legacy_execution(db, sid)
    assert refused.value.reason == 'route_lock_mismatch'


# ── ACP SessionManager ──────────────────────────────────────────────────────────────────────────

def _manager(db):
    built = []

    def factory():
        built.append(1)
        return SimpleNamespace(model='fake', session_id=None)

    return SessionManager(agent_factory=factory, db=db), built


# source=None keeps the TUI fixture source: the refusal must not depend on the incidental 'acp' gate.
@pytest.mark.parametrize('source', ['acp', None])
@pytest.mark.parametrize('op', ['get_session', 'fork_session', 'display_history'])
def test_acp_locked_session_is_refused(db, tmp_path, source, op):
    sid = _seed(db, tmp_path, 'locked', source)
    before = _snapshot(db, sid)
    manager, built = _manager(db)
    with pytest.raises(RuntimeStoreError) as refused:
        getattr(manager, op)(sid)
    assert refused.value.reason == 'route_lock_mismatch'
    assert built == [] and manager._sessions == {}
    _assert_still_locked(db, sid, before)


@pytest.mark.parametrize('case', ['missing', 'malformed', 'unverified', 'missing-lock'])
def test_acp_unverifiable_receipt_fails_closed(db, tmp_path, case):
    sid = _seed(db, tmp_path, case, 'acp')
    manager, built = _manager(db)
    for op in (manager.get_session, manager.display_history):
        with pytest.raises(RuntimeStoreError) as refused:  # never an empty history or a fresh agent
            op(sid)
        assert refused.value.reason == 'storage_unavailable'
    assert built == []


def test_acp_unlocked_control_restores_history(db, tmp_path):
    sid = _seed(db, tmp_path, 'unlocked', 'acp')
    manager, built = _manager(db)
    state = manager.get_session(sid)
    assert state is not None and built == [1]
    assert [m['content'] for m in state.history] == [SECRET, 'reply']
    assert [m['content'] for m in manager.display_history(sid)] == [SECRET, 'reply']
    assert manager.fork_session(sid) is not None


def test_acp_unknown_and_foreign_source_stay_none(db, tmp_path):
    manager, built = _manager(db)
    assert manager.get_session('nope') is None
    sid = _seed(db, tmp_path, 'legacy')  # ordinary tui row: not an ACP session, not locked
    assert manager.get_session(sid) is None and built == []


# ── classic CLI ─────────────────────────────────────────────────────────────────────────────────

def _make_cli():
    import cli as cli_mod
    from cli import HermesCLI
    config = {'model': {'default': 'anthropic/claude-opus-4.6', 'base_url': 'https://openrouter.ai/api/v1',
                        'provider': 'auto'},
              'display': {'compact': False, 'tool_progress': 'all', 'resume_display': 'full'},
              'agent': {}, 'terminal': {'env_type': 'local'}}
    with (patch('cli.get_tool_definitions', return_value=[]),
          patch.dict('os.environ', {'LLM_MODEL': '', 'HERMES_MAX_ITERATIONS': ''}, clear=False),
          patch.dict(cli_mod.__dict__, {'CLI_CONFIG': config})):
        return HermesCLI(resume=None)


def _resuming_cli(db, sid):
    cli = _make_cli()
    cli._session_db, cli.session_id, cli._resumed = db, sid, True
    cli.console.file = io.StringIO()
    cli._restore_session_state = lambda *a, **k: None
    cli._restore_session_cwd = cli._restore_session_yolo = cli._restore_session_model = lambda *a, **k: None
    return cli


def _spy_reads(db):
    reads = []
    for name in ('get_resume_conversations', 'get_messages_as_conversation', 'reopen_session'):
        real = getattr(db, name)
        setattr(db, name, lambda *a, _real=real, _name=name, **k: reads.append(_name) or _real(*a, **k))
    return reads


@pytest.mark.parametrize('path', ['_preload_resumed_session', '_load_resumed_history_late'])
@pytest.mark.parametrize('case', ['locked', 'descendant', 'missing', 'malformed', 'unverified', 'missing-lock'])
def test_cli_startup_resume_refuses_locked(db, tmp_path, capsys, path, case):
    sid = _seed(db, tmp_path, case)
    before = _snapshot(db, sid)
    reads = _spy_reads(db)
    cli = _resuming_cli(db, sid)
    assert getattr(cli, path)() is False
    assert cli.conversation_history == [] and cli._resumed is True and cli.session_id == sid
    assert getattr(cli, '_resume_history_error', None)  # the existing abort channel: _init_agent stops, no turn
    seen = capsys.readouterr()
    assert ('route-locked' if case in ('locked', 'descendant') else 'storage_unavailable') in (
        cli.console.file.getvalue() + seen.out + seen.err)
    assert reads == [], f'content/lifecycle touched before the refusal: {reads}'
    assert _snapshot(db, sid)[:2] == before[:2]


def test_cli_init_agent_does_not_build_an_agent_for_a_refused_resume(db, tmp_path):
    sid = _seed(db, tmp_path, 'locked')
    cli = _resuming_cli(db, sid)
    cli._ensure_gateway_session = lambda: None
    cli._ensure_runtime_credentials = lambda: True
    cli._install_tool_callbacks = lambda: None
    cli.finalize_preloaded_skills = lambda: None
    with patch('hermes_cli.gateway_agent.GatewayAgent') as agent:
        assert cli._init_agent() is False
    agent.assert_not_called()
    assert cli.agent is None


@pytest.mark.parametrize('path', ['_preload_resumed_session', '_load_resumed_history_late'])
def test_cli_startup_resume_unlocked_control(db, tmp_path, path):
    sid = _seed(db, tmp_path, 'unlocked')
    cli = _resuming_cli(db, sid)
    assert getattr(cli, path)() is True
    assert [m['content'] for m in cli.conversation_history] == [SECRET, 'reply']
    assert not getattr(cli, '_resume_history_error', None)


@pytest.mark.parametrize('path', ['_preload_resumed_session', '_load_resumed_history_late'])
def test_cli_gateway_store_refuses_before_any_gateway_call(db, tmp_path, path):
    """The classic CLI's real store is a ``GatewaySessionStore`` over a read-only reader."""
    from hermes_cli.gateway_session_store import GatewaySessionStore
    sid = _seed(db, tmp_path, 'locked')
    calls = []
    store = GatewaySessionStore(SimpleNamespace(final=False, call=lambda *a, **k: calls.append(a)))
    store.call = lambda *a, **k: calls.append(a)
    store._reader = SessionDB(db_path=db.db_path, read_only=True)
    cli = _resuming_cli(store, sid)
    assert getattr(cli, path)() is False
    assert calls == [] and cli.conversation_history == []


def test_cli_fence_reads_the_gateway_store_reader_not_the_refusing_facade(db, tmp_path):
    """Unlocked ids must pass and locked ones name the lock, through the real ``GatewaySessionStore`` facade
    (its unknown attributes raise): an unwrapped facade would refuse everything as unverifiable."""
    from hermes_cli.cli_agent_setup_mixin import _route_lock_refusal
    from hermes_cli.gateway_session_store import GatewaySessionStore
    open_sid, locked_sid = _seed(db, tmp_path, 'legacy'), _seed(db, tmp_path, 'locked')
    store = GatewaySessionStore(SimpleNamespace(final=False))
    store._reader = SessionDB(db_path=db.db_path, read_only=True)
    assert _route_lock_refusal(store, open_sid) is None
    assert 'route-locked' in _route_lock_refusal(store, locked_sid)
    store._reader = None  # no state.db yet: nothing stored, nothing to refuse
    with patch.object(GatewaySessionStore, '_db', lambda self: None):
        assert _route_lock_refusal(store, locked_sid) is None


def _midchat_cli(db, current):
    cli = _make_cli()
    cli._session_db, cli.session_id = db, current
    cli.conversation_history = [{'role': 'user', 'content': 'mine'}]
    return cli


def test_cli_midchat_resume_refuses_locked_and_stays(db, tmp_path):
    current = db.create_session('plain-current', source='cli')
    sid = _seed(db, tmp_path, 'locked')
    before = _snapshot(db, sid)
    cli = _midchat_cli(db, current)
    printed = []
    with patch('cli._cprint', lambda *a: printed.append(' '.join(map(str, a)))):
        cli._handle_resume_command(f'/resume {sid}')
    assert 'route-locked' in '\n'.join(printed)
    assert cli.session_id == current and cli.conversation_history == [{'role': 'user', 'content': 'mine'}]
    assert db.get_session(current)['ended_at'] is None  # the session the user is on was not ended
    _assert_still_locked(db, sid, before)


def test_cli_midchat_resume_unlocked_control(db, tmp_path):
    current = db.create_session('plain-current', source='cli')
    sid = _seed(db, tmp_path, 'unlocked')
    cli = _midchat_cli(db, current)
    cli._sync_agent_to_session = MagicMock()
    with patch('cli._cprint'), patch('hermes_cli.cli_commands_mixin._sync_agent_to_session'):
        cli._handle_resume_command(f'/resume {sid}')
    assert cli.session_id == sid and [m['content'] for m in cli.conversation_history] == [SECRET, 'reply']


def test_cli_branch_of_a_locked_session_creates_nothing(db, tmp_path):
    sid = _seed(db, tmp_path, 'locked')
    before = _snapshot(db, sid)
    cli = _midchat_cli(db, sid)
    cli.model, cli.max_turns, cli.reasoning_config = 'm', 5, None
    cli.session_start, cli._pending_title, cli.agent, cli._agent_running = datetime.now(), None, None, False
    printed = []
    with patch('cli._cprint', lambda *a: printed.append(' '.join(map(str, a)))):
        cli._handle_branch_command('/branch')
    assert 'route-locked' in '\n'.join(printed)
    assert cli.session_id == sid
    _assert_still_locked(db, sid, before)  # no child row, parent not ended
