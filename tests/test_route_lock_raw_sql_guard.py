"""Route-lock B3 item 6: no new raw SQL may read message content or a system prompt unnoticed.

A locked session's stored content is default-denied at the SessionDB readers and the ``content_sql_filter``
gate. A raw ``SELECT ... content FROM messages`` elsewhere skips both. This scans repo source (not ``tests/``,
not ``hermes_state_*.py``, which own the gate) for those statements and fails on any site not in ALLOWED.

Deliberately not a SQL parser: per function scope, the string literals are joined (``'a' + 'b'`` chains fold
without a separator) and a tokenizer finds each ``SELECT <cols> FROM <clause>`` (subqueries and CTEs are their
own SELECTs) and ``INSERT/UPDATE/DELETE ... RETURNING <cols>``. A statement is a content read when its FROM/JOIN
clause names ``messages\\w*`` anywhere (comma joins, ``main.`` schema, quotes; ``messages_fts*`` counts, ``snippet()``
and ``highlight()`` are content) with a content column or ``*``, or names ``sessions`` / ``system_prompts`` with a
prompt column, ``*`` or ``title``.
ponytail: f-string table names, a module-level constant joined across scopes (``Q = 'SELECT ...'`` defined at
module level and executed in a function; only a same-scope name resolves), ``%`` / ``.format`` / ``''.join``
assembly and SQL built in another file are invisible to this; a bare ``{}`` column hole is flagged, a partial
one (``SELECT id, {cols} FROM messages``) is not; a content column used only in a predicate (``WHERE content
LIKE ?``) and ``tool_name`` / ``display_metadata`` / ``model_config`` reads are not content here. The allowlist
is a tripwire for the usual shape, not proof. Upgrade path: route every raw reader through SessionDB.
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
    ('gateway/session_live_listing.py', '_store_facts'):
        'gated: runs inside content_sql_filter and selects route_content_readable(s.id) and user_id; '
        'live_entries blanks the title unless route_content_readable or the actor is the owner (N2)',
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
        'owner-only: SELECT * of session rows to build the roster; fields are picked by name (no system_prompt); '
        'every returned title (summary and worker_session) is blank unless owner or route_content_readable, '
        'and the preview is gated',
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
        'integrity-only: SELECTs the tail role/ids and tool_calls to decide on a failed-turn boundary; '
        'returns only a bool, nothing is handed to a caller',
    ('gateway/platforms/api_server_room_dispatch.py', '_ensure_hosted_member_session.atomic'):
        'integrity-only: SELECTs a room session title to compare with the requested one and to detect a duplicate; '
        'raises or returns the session id, never the title',
    ('gateway/session_api.py', 'bind_api_steps.write'):
        'integrity-only: SELECTs the title to detect a bot_room admission conflict; raises an error code, '
        'never returns the title',
    ('gateway/session_bot.py', '_bot_chat_creation_id.spent'):
        'integrity-only: SELECTs the title only to test whether it is set; returns a bool',
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

STAR = r'(?:^|[\s,.])\*'
CONTENT_COLS = re.compile(rf'\b(?:content|api_content|tool_calls|reasoning\w*|codex_\w+)\b|{STAR}|^\s*\{{\}}\s*$',
                          re.IGNORECASE)
FTS_COLS = re.compile(rf'{CONTENT_COLS.pattern}|\b(?:snippet|highlight)\s*\(', re.IGNORECASE)
PROMPT_COLS = re.compile(rf'\bsystem_prompt\b|\bprompt\b|\btitle\b|{STAR}', re.IGNORECASE)
SKIP_DIRS = {'tests', 'node_modules', 'website', '__pycache__', 'dist', 'build'}
# One SQL token stream: parens, ';', and the keywords that end a column list or a FROM clause.
TOKEN = re.compile(r'[();]|\b(?:select|from|where|group|order|limit|having|union|intersect|except|window|returning)\b',
                   re.IGNORECASE)
WRITE = re.compile(r'\b(?:delete\s+from|insert\s+(?:or\s+\w+\s+)?into|replace\s+into|update(?:\s+or\s+\w+)?)\s+'
                   r'(?P<table>[\w."`\[\]]+)[^;]*?\breturning\b(?P<cols>[^;]*)', re.IGNORECASE | re.DOTALL)
NAME = re.compile(r'(?:\w+\.)?(\w+)')


def _selects(sql):
    """(cols, from-clause text) of every SELECT in *sql*, nested ones included; a clause ends at the first
    clause keyword or unbalanced ``)`` at its own paren depth."""
    out = []
    for start in re.finditer(r'\bselect\b', sql, re.IGNORECASE):
        depth, cols_at, cols, clause_at = 0, start.end(), None, None
        for t in TOKEN.finditer(sql, start.end()):
            tok = t[0].lower()
            if tok == '(':
                depth += 1
            elif tok == ')' and depth:
                depth -= 1
            elif depth:
                continue
            elif tok == 'from' and cols is None:
                cols, clause_at = sql[cols_at:t.start()], t.end()
            else:
                break
        else:
            t = None
        if cols is not None:
            out.append((cols, sql[clause_at:t.start() if t else len(sql)]))
    return out


def _tables(clause):
    return {n.lower() for n in NAME.findall(clause)}  # quotes/brackets/``main.`` fall out of \w+ on their own


def _reads_content(tables, cols):
    return (any(t.startswith('messages_') for t in tables) and FTS_COLS.search(cols)) or (
            'messages' in tables and CONTENT_COLS.search(cols)) or (
            bool(tables & {'sessions', 'system_prompts'}) and PROMPT_COLS.search(cols))


def _chain(node):
    """Leaves of a ``+`` chain, left to right."""
    return _chain(node.left) + _chain(node.right) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) else [node]


def _text(node):
    """Literal text of a str constant or f-string (holes ``{}``), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(str(p.value) if isinstance(p, ast.Constant) else '{}' for p in node.values)


def _scopes(tree):
    """(dotted function/class scope, [string literals directly in it]); f-string holes become ``{}``,
    a ``+`` chain with a string leaf folds into one literal (other leaves ``{}``)."""
    out = []

    def walk(node, name):
        own, stack = [], list(ast.iter_child_nodes(node))
        names = {t.id: _text(c.value) for c in getattr(node, 'body', ()) if isinstance(c, ast.Assign)
                 for t in c.targets if isinstance(t, ast.Name) and _text(c.value) is not None}  # same-scope str names
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, f'{name}.{child.name}' if name else child.name)
            elif isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue  # docstrings and bare strings are prose, not SQL
            elif isinstance(child, ast.BinOp) and any(_text(x) is not None for x in _chain(child)):
                leaves = _chain(child)
                texts = [_text(x) if _text(x) is not None else names.get(getattr(x, 'id', None), '{}') for x in leaves]
                own.append((child.lineno, ''.join(texts)))
                stack.extend(x for x in leaves if _text(x) is None)  # strings inside a spliced call still count
            elif (text := _text(child)) is not None:
                own.append((child.lineno, text))
            else:
                stack.extend(ast.iter_child_nodes(child))
        out.append((name or '<module>', [text for _, text in sorted(own)]))

    walk(tree, '')
    return out


def raw_content_sites(source):
    """Dotted scopes in *source* that SELECT (or RETURNING) message content, a system prompt or a title with raw SQL."""
    sites = set()
    for scope, strings in _scopes(ast.parse(source)):
        sql = ' '.join(strings)
        found = [(_tables(clause), cols) for cols, clause in _selects(sql)]
        found += [(_tables(m['table']), m['cols']) for m in WRITE.finditer(sql)]
        if any(_reads_content(tables, cols) for tables, cols in found):
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
    'newmod/title.py': "def peek(c):\n    return c.execute('SELECT id, title FROM sessions')\n",
    'newmod/gate_missing.py': "def peek(c):\n    return c.execute('SELECT tool_calls FROM messages WHERE role=?', ('assistant',))\n",
}
CLEAN = {
    'newmod/count.py': "def n(c):\n    return c.execute('SELECT COUNT(*) FROM messages').fetchone()\n",
    'newmod/ids.py': "def ids(c):\n    return c.execute('SELECT id, role, timestamp FROM messages').fetchall()\n",
    'newmod/sessions.py': "def t(c):\n    return c.execute('SELECT id, source, model FROM sessions').fetchall()\n",
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


def _fn(body):
    return 'def f(conn, cols):\n' + ''.join(f'    {line}\n' for line in body.splitlines())


# Bypass shapes (Fable's B3 probes plus neighbours): inert source text fed to the parser, never executed.
MUST_FLAG = {
    'join_sessions_first': "conn.execute('SELECT s.id, m.content FROM sessions s JOIN messages m ON m.session_id=s.id')",
    'comma_join': "conn.execute('SELECT m.content FROM sessions s, messages m WHERE m.session_id=s.id')",
    'fts_content': "conn.execute('SELECT content FROM messages_fts WHERE messages_fts MATCH ?')",
    'fts_snippet': "conn.execute(\"SELECT snippet(messages_fts, 0, '', '', '', 20) FROM messages_fts WHERE messages_fts MATCH ?\")",
    'fts_trigram_highlight': "conn.execute('SELECT highlight(messages_fts_trigram, 0, 1, 2) FROM messages_fts_trigram WHERE x MATCH ?')",
    'schema_qualified': "conn.execute('SELECT content FROM main.messages')",
    'double_quoted': "conn.execute('SELECT content FROM \"messages\"')",
    'bracket_backtick': "conn.execute('SELECT content FROM [main].`messages`')",
    'codex_cols': "conn.execute('SELECT codex_reasoning_items, codex_message_items FROM messages')",
    'title': "conn.execute('SELECT id, title FROM sessions')",
    'title_alias': "conn.execute('SELECT s.title FROM sessions AS s WHERE s.id=?')",
    'returning_delete': "conn.execute('DELETE FROM messages WHERE session_id=? RETURNING content')",
    'returning_update': "conn.execute('UPDATE messages SET active=0 WHERE id=? RETURNING tool_calls')",
    'returning_title': "conn.execute('UPDATE sessions SET archived=1 WHERE id=? RETURNING title')",
    'scalar_subquery': "conn.execute('SELECT id, (SELECT content FROM messages m WHERE m.session_id=s.id) FROM sessions s')",
    'cte': "conn.execute('WITH x AS (SELECT content FROM messages) SELECT * FROM x')",
    'concat_plus': "conn.execute('SELECT content' + ' FROM messages')",
    'concat_many': "conn.execute('SELECT ' + 'content' + ' FROM ' + 'messages')",
    'bare_column_hole': "conn.execute(f'SELECT {cols} FROM messages')",
    'same_scope_name': "tail = ' FROM messages'\nconn.execute('SELECT content' + tail)",
    'paren_adjacent': "conn.execute(('SELECT content '\n 'FROM messages'))",
}
MUST_NOT_FLAG = {
    'count_only': "conn.execute('SELECT COUNT(*) FROM messages')",
    'ids_only': "conn.execute('SELECT id, role, timestamp FROM messages')",
    'predicate_only': "conn.execute('SELECT id FROM sessions WHERE id IN (SELECT session_id FROM messages WHERE content LIKE ?)')",
    'sessions_metadata': "conn.execute('SELECT id, source, model FROM sessions')",
    'title_in_where': "conn.execute('SELECT id FROM sessions WHERE title = ?')",
    'title_other_table': "conn.execute('SELECT title FROM profiles')",
    'write_no_returning': "conn.execute('UPDATE sessions SET title=? WHERE id=?')",
    'returning_id': "conn.execute('DELETE FROM messages WHERE session_id=? RETURNING id')",
    'fts_ids_only': "conn.execute('SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?')",
    'prose_docstring': '"""SELECT content FROM messages"""',
}


@pytest.mark.parametrize('name', MUST_FLAG)
def test_scanner_flags_each_bypass_shape(name):
    assert raw_content_sites(_fn(MUST_FLAG[name])) == {'f'}, name


@pytest.mark.parametrize('name', MUST_NOT_FLAG)
def test_scanner_leaves_non_content_shapes_alone(name):
    assert raw_content_sites(_fn(MUST_NOT_FLAG[name])) == set(), name
