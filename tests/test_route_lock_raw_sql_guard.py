"""Route-lock B3 item 6: no new raw SQL may read message content or a system prompt unnoticed.

A locked session's stored content is default-denied at the SessionDB readers and the ``content_sql_filter``
gate. A raw ``SELECT ... content FROM messages`` elsewhere skips both. This scans repo source (not ``tests/``,
not ``hermes_state_*.py``, which own the gate) for those statements and fails on any site not in ALLOWED.

Deliberately not a SQL parser: per function scope, the string literals are joined and a regex looks for
``SELECT <content-ish columns> FROM messages`` or a ``system_prompt`` / ``SELECT *`` read of sessions.
ponytail: SQL assembled across scopes or via a variable table name is invisible to this; the allowlist is a
tripwire for the usual shape, not proof. Upgrade path: route every raw reader through SessionDB.
"""
import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (repo-relative path, dotted function scope) -> "<kind>: reason". Kind is one of KINDS.
ALLOWED = {
    # --- gated: the statement or its connection carries the route-lock content filter ---
    ('agent/insights.py', 'InsightsEngine'):
        'gated: every query runs via _query() inside content_sql_filter with route_content_readable(s.id)',
    ('gateway/session_group_controls.py', 'latest_message_preview'):
        'gated: route_content_readable(session_id) in the one shared roster preview query',
    ('hermes_cli/approvals_suggest.py', '_iter_terminal_calls'):
        'gated: scan_approval_history runs inside content_sql_filter and appends route_content_readable',
    ('hermes_cli/approvals_suggest.py', '_blocked_tool_call_ids'):
        'gated: same visible clause as _iter_terminal_calls, inside content_sql_filter',
    # --- owner-only: reads the row the caller already owns or just created, or never leaves the process ---
    ('gateway/platforms/api_server.py', 'APIServerAdapter._handle_create_session._atomic'):
        'owner-only: SELECT * of the session this request just INSERTed in the same write txn',
    ('gateway/session_group_controls.py', '_read_bot_sessions'):
        'owner-only: SELECT * of session rows to build the roster; fields are picked by name (no system_prompt), '
        'preview/titles go through the gate',
    ('gateway/session_group_controls.py', '_read_bot_sessions.summary'):
        'owner-only: SELECT * of the compression tip; fields are picked by name (no system_prompt)',
    ('gateway/session_local_migration.py', 'require_history_claim'):
        'owner-only: reads routing/owner columns of the claimed row to authorize it; nothing is returned',
    # --- integrity-only: reads rows for repair, retention or ordering, never hands content to a caller ---
    ('agent/transcript_repair.py', 'stamp_inserted_row_snapshots'):
        'integrity-only: digests rows just inserted in the caller write txn',
    ('agent/transcript_repair.py', '_active_logical_message_row'):
        'integrity-only: CAS/dedupe row inside the session writer transaction',
    ('agent/transcript_repair.py', '_active_uid_dedupe'):
        'integrity-only: re-insert guard inside the session writer transaction',
    ('agent/transcript_repair.py', 'resolve_and_repair_transcript_batch'):
        'integrity-only: CAS row read inside the session writer transaction',
    ('agent/transcript_repair.py', '_active_message_row'):
        'integrity-only: CAS row read inside the session writer transaction',
    ('gateway/session_ingress_media.py', '_history_mentions'):
        'integrity-only: media retention; returns only which attachment paths a note mentions, never text',
    ('gateway/session_results.py', 'close_discarded_turn'):
        'integrity-only: reads the tail role/ids to decide on a failed-turn boundary; no content selected',
    ('tools/tool_result_storage.py', 'referenced_spillover_names'):
        'integrity-only: spillover GC; returns only archive basenames, fails closed (None) when unknown',
    # --- offline: eval/forensics scripts run by hand against a copied or throwaway state.db, never in the product ---
    ('evals/acp_empty_session_wire.py', 'main'):
        'offline: eval probe over its own throwaway home',
    ('evals/compaction/scripts/reconstruct_lineage.py', '<module>'):
        'offline: forensics script over a state.db path given on the command line',
    ('evals/desktop_bug_campaign/persistence_live.py', 'main'):
        'offline: live-campaign probe over its own throwaway home',
    ('evals/gateway_failure_ownership/probe.py', 'rows'):
        'offline: probe over its own throwaway home',
    ('evals/postmortem/forensics/common.py', 'Run.open'):
        'offline: postmortem loader, opens a copy of a state.db read-only',
    ('evals/postmortem/forensics/common.py', 'Run.messages'):
        'offline: postmortem loader, opens a copy of a state.db read-only',
    ('evals/postmortem/forensics/common.py', 'Run.system_prompt_len'):
        'offline: postmortem loader, opens a copy of a state.db read-only',
    ('evals/postmortem/review_probes/goal_repaste_probe.py', '<module>'):
        'offline: one-off review probe over a copied state.db',
}
KINDS = ('gated', 'owner-only', 'integrity-only', 'offline')

SELECT = re.compile(r'\bselect\b(?P<cols>(?:(?!\bfrom\b).)*)\bfrom\s+(?P<table>\w+)', re.IGNORECASE | re.DOTALL)
STAR = r'(?:^|[\s,.])\*'
CONTENT_COLS = re.compile(rf'\b(?:content|api_content|tool_calls|reasoning\w*)\b|{STAR}|^\s*\{{\}}\s*$', re.IGNORECASE)
PROMPT_COLS = re.compile(rf'\bsystem_prompt\b|\bprompt\b|{STAR}', re.IGNORECASE)
SKIP_DIRS = {'tests', 'node_modules', 'website', '__pycache__', 'dist', 'build'}


def _scopes(tree):
    """(dotted function/class scope, [string literals directly in it]); f-string holes become ``{}``."""
    out = []

    def walk(node, name):
        own, stack = [], list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, f'{name}.{child.name}' if name else child.name)
            elif isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue  # docstrings and bare strings are prose, not SQL
            elif isinstance(child, ast.JoinedStr):
                own.append((child.lineno, ''.join(str(p.value) if isinstance(p, ast.Constant) else '{}' for p in child.values)))
            else:
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    own.append((child.lineno, child.value))
                stack.extend(ast.iter_child_nodes(child))
        out.append((name or '<module>', [text for _, text in sorted(own)]))

    walk(tree, '')
    return out


def raw_content_sites(source):
    """Dotted scopes in *source* that SELECT message content or a system prompt with raw SQL."""
    sites = set()
    for scope, strings in _scopes(ast.parse(source)):
        for m in SELECT.finditer(' '.join(strings)):
            table, cols = m['table'].lower(), m['cols']
            if (table == 'messages' and CONTENT_COLS.search(cols)) or (
                    table in ('sessions', 'system_prompts') and PROMPT_COLS.search(cols)):
                sites.add(scope)
    return sites


def scan(root):
    found = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS
                   and not (Path(base, d) / 'pyvenv.cfg').exists()]
        for name in files:
            if not name.endswith('.py') or name.startswith(('test_', 'hermes_state_')) or name == 'hermes_state.py':
                continue
            path = Path(base, name)
            try:
                scopes = raw_content_sites(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            found |= {(path.relative_to(root).as_posix(), scope) for scope in scopes}
    return found


def check(root, allowed=ALLOWED):
    """Failure text for unlisted and stale sites, or '' when the tree matches the allowlist."""
    found, errors = scan(root), []
    if unlisted := sorted(found - set(allowed)):
        errors.append('Raw SQL reads message content or a system prompt outside the audited allowlist: '
                      + '; '.join(f'{p}::{s}' for p, s in unlisted)
                      + '. Route the read through SessionDB or the content gate (content_sql_filter + '
                        'route_content_readable); only if it is gated, owner-only, integrity-only or offline, '
                        'audit it and add it to ALLOWED with a reason.')
    if stale := sorted(set(allowed) - found):
        errors.append('Allowlist entries no longer match any raw reader (renamed or deleted): '
                      + '; '.join(f'{p}::{s}' for p, s in stale))
    return '\n'.join(errors)


def test_repo_has_no_unlisted_raw_content_sql():
    assert check(ROOT) == ''


def test_every_allowlist_entry_states_its_kind_and_a_reason():
    for (path, scope), reason in ALLOWED.items():
        kind, _, why = reason.partition(': ')
        assert kind in KINDS and len(why) > 10, (path, scope)
        assert path.startswith(('evals/', 'scripts/')) == (kind == 'offline'), (path, scope)


# One synthetic tree: each file is raw SQL the guard must catch, or a read it must leave alone.
SYNTHETIC = {
    'newmod/leak.py': "def peek(conn, sid):\n    return conn.execute('SELECT content FROM messages WHERE session_id=?', (sid,))\n",
    'newmod/fleak.py': "def peek(conn, cols):\n    return conn.execute(f'SELECT {cols} FROM messages')\n",
    'newmod/star.py': "class A:\n    def peek(self, c):\n        return c.execute('select * from messages')\n",
    'newmod/prompt.py': "def peek(c):\n    return c.execute('SELECT system_prompt FROM sessions')\n",
    'newmod/subq.py': "def peek(c):\n    return c.execute('SELECT n FROM (SELECT role, content FROM messages)')\n",
    'newmod/gate_missing.py': "def peek(c):\n    return c.execute('SELECT tool_calls FROM messages WHERE role=?', ('assistant',))\n",
}
CLEAN = {
    'newmod/count.py': "def n(c):\n    return c.execute('SELECT COUNT(*) FROM messages').fetchone()\n",
    'newmod/ids.py': "def ids(c):\n    return c.execute('SELECT id, role, timestamp FROM messages').fetchall()\n",
    'newmod/sessions.py': "def t(c):\n    return c.execute('SELECT id, title FROM sessions').fetchall()\n",
    'newmod/doc.py': "def f():\n    '''Reads SELECT content FROM messages in prose only.'''\n",
    'tests/test_x.py': "def test():\n    c.execute('SELECT content FROM messages')\n",
    'hermes_state_new.py': "def f(c):\n    c.execute('SELECT content FROM messages')\n",
    'hermes_state.py': "def f(c):\n    c.execute('SELECT content FROM messages')\n",
}


def _tree(tmp_path, files):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


@pytest.mark.parametrize('rel', SYNTHETIC)
def test_guard_fails_an_unlisted_raw_reader(tmp_path, rel):
    message = check(_tree(tmp_path, {rel: SYNTHETIC[rel]}), allowed={})
    assert rel in message and 'route the read through SessionDB' in message.replace('Route', 'route')


def test_guard_accepts_listed_site_and_ignores_non_content_reads(tmp_path):
    root = _tree(tmp_path, {**CLEAN, 'newmod/leak.py': SYNTHETIC['newmod/leak.py']})
    assert check(root, allowed={('newmod/leak.py', 'peek'): 'gated: audited'}) == ''


def test_guard_fails_a_stale_entry(tmp_path):
    root = _tree(tmp_path, CLEAN)
    assert 'no longer match' in check(root, allowed={('newmod/gone.py', 'peek'): 'gated: audited'})
