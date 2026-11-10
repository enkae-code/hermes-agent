---
name: codex
description: "Delegate coding to OpenAI Codex CLI (features, PRs)."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI

Delegate coding tasks to [Codex](https://github.com/openai/codex) via the Hermes terminal. Codex is OpenAI's autonomous coding agent CLI.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing

Requires the codex CLI. `codex exec` runs inside a git repository, or anywhere with `--skip-git-repo-check`.

## Prerequisites

- Codex installed: `npm install -g @openai/codex`
- OpenAI auth configured: either `OPENAI_API_KEY` or Codex OAuth credentials
  from the Codex CLI login flow
- Prefer a git repository; only use `--skip-git-repo-check` for an authorized non-repository task.
- `codex exec` is noninteractive and does not require a PTY. Reserve `pty=true` for the interactive CLI.

For Hermes itself, `model.provider: openai-codex` uses Hermes-managed Codex
OAuth from `~/.hermes/auth.json` after `hermes auth add openai-codex`. For the
standalone Codex CLI, a valid CLI OAuth session may live under
`~/.codex/auth.json`; do not treat a missing `OPENAI_API_KEY` alone as proof
that Codex auth is missing.

## One-Shot Tasks

Keep agent-authored writes inside the native writable roots. For a receipt outside the checkout, use the outer CLI's `--output-last-message` capture and ask the worker for a final response, not a file write. Do not broaden sandbox permissions merely to fix reporting.

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project")
```

For a bounded read-only source investigation, feed a reviewed work order on stdin, pin the model and effort, write the final answer to a dedicated artifact, and keep the native sandbox read-only:

```bash
codex exec --model <approved-model> --config 'model_reasoning_effort="high"' \
  --sandbox read-only --output-last-message /absolute/path/result.md - \
  < /absolute/path/work-order.md > /absolute/path/run.log 2>&1
```

Bound the run with the process timeout. If it runs in the background, capture its process/session ID and verify the startup log reports the requested model, effort, and read-only sandbox before relying on it. Startup metadata proves routing only: wait for normal completion, inspect the terminal status and actual result artifact, then independently verify any source claims against the frozen candidate. A dispatched or still-running investigation has no findings or verdict yet.

For scratch work, run inside a fresh repo (or pass `--skip-git-repo-check` for an authorized non-repository task):
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'")
```

## Background Mode (Long Tasks)

```
# Start in background
terminal(command="codex exec --sandbox workspace-write 'Refactor the auth module'", workdir="~/project", background=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Key Flags

| Flag | Effect |
|------|--------|
| `exec "prompt"` | One-shot execution, exits when done |
| `--sandbox workspace-write` (`-s`) | Sandboxed but auto-approves file changes in the workspace (the recommended auto-build mode) |
| `--dangerously-bypass-approvals-and-sandbox` | Disables safety boundaries; never select as a default or sandbox-failure workaround. Requires separate explicit operator authorization. |
| `--sandbox danger-full-access` | No Codex sandbox; not a recovery for service/user-namespace failures. Stop and diagnose the supported boundary. |

> **Deprecated:** `--full-auto` still works but the live CLI warns to use `--sandbox workspace-write` instead.

## Hermes Gateway Caveat

When invoking the Codex CLI from a Hermes gateway/service context (for example,
Telegram-driven agent sessions), Codex `workspace-write` sandboxing may fail even
when the same command works in the user's interactive shell. A typical symptom is
bubblewrap/user-namespace errors such as `setting up uid map: Permission denied`
or `loopback: Failed RTM_NEWADDR: Operation not permitted`.

Concrete check, from the same service context: `sysctl kernel.apparmor_restrict_unprivileged_userns` and `unshare --user --map-root-user true`. A value of `1` or an `unshare` failure such as `write failed /proc/self/uid_map: Operation not permitted` means the host blocks unprivileged user namespaces for that process, so bubblewrap cannot start.

Treat sandbox failure as a blocker. Do not respond by enabling `danger-full-access` or bypassing permissions. Diagnose the supported invocation or report the blocked boundary. A path allowlist, workdir, narrow prompt and later diff review are not a sandbox.

## PR Reviews

Clone to a temp directory for safe review:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && codex review --base origin/main")
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 ~/.hermes/cache/scratch/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 ~/.hermes/cache/scratch/issue-99 main", workdir="~/project")

# Launch Codex in each
terminal(command="codex --sandbox workspace-write exec 'Fix issue #78: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-78", background=true)
terminal(command="codex --sandbox workspace-write exec 'Fix issue #99: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-99", background=true)

# Monitor
process(action="list")

# After completion, push and create PRs
terminal(command="cd ~/.hermes/cache/scratch/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove ~/.hermes/cache/scratch/issue-78", workdir="~/project")
```

## Batch PR Reviews

```
# Fetch all PR refs
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# Review multiple PRs in parallel
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true)

# Post results
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## Rules

1. **Noninteractive exec needs no PTY** — use `pty=true` only for the interactive CLI.
2. **Run inside a git repo** — outside one, `codex exec` needs `--skip-git-repo-check`. Use `mktemp -d && git init` for scratch
3. **Use `exec` for one-shots** — `codex exec "prompt"` runs and exits cleanly
4. **`--sandbox workspace-write` for building** — auto-approves changes within the sandbox (`--full-auto` is deprecated for this)
5. **Background for long tasks** — use `background=true` and monitor with `process` tool
6. **Don't interfere** — monitor with `poll`/`log`, be patient with long-running tasks
7. **Parallel is fine** — run multiple Codex processes at once for batch work
