# Hermes CLI Reference

Live sources when anything looks stale: `hermes --help`, `hermes <command> --help`,
https://hermes-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
hermes [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
hermes chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
hermes setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
hermes model                Interactive model/provider picker
hermes fallback [add|remove|list]  Fallback provider chain
hermes config [show|edit|get|set|unset|path|env-path|check|migrate]
hermes login / logout       OAuth sign-in / clear stored auth
hermes doctor [--fix]       Check dependencies and config
hermes status [--full]      Component summary (--full: every section)
```

### Tools & Skills

```
hermes tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

hermes skills list|browse|search QUERY|inspect ID
hermes skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
hermes skills config        Enable/disable skills per platform
hermes skills check|update|uninstall|publish PATH
hermes skills tap add REPO  Add a GitHub repo as a skill source
hermes bundles              Skill bundles (one /<name> alias loads several skills)
```

### Updating a plugin

1. Identify the active profile and exact installed plugin name; run `hermes plugins check-updates` and confirm that this plugin has an update before changing anything.
2. Inspect the installed plugin for local edits and record its current version/SHA. Preserve a recoverable copy if it is locally modified or rollback would otherwise be difficult; do not overwrite unreconciled local work.
3. Update only the named plugin with `hermes plugins update <name>`, not the whole catalog. Then read the installed metadata and inspect the expected changed capability or code path; a success message alone is not verification.
4. Run the plugin's relevant tests. If tests fail, compare the same failing test against the pre-update version before attributing the failure to the update. Report baseline failures separately; do not call a skipped or failed test passing.
5. If the changed plugin is loaded by a long-lived gateway, establish whether reload or restart is required. Verify the real runtime after an authorized restart; on shared or interruptive services, do not restart without the operator's consent.

### MCP Servers

```
hermes mcp add NAME (--url or --command) | remove | list | test NAME
hermes mcp catalog | install NAME     Curated catalog install
hermes mcp configure NAME             Toggle tool selection
hermes mcp-serve                      Run Hermes as an MCP server (mcp-serve plugin)
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
hermes gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `hermes photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

##### Moving a gateway to another host

1. Inventory the source and destination before copying: active profile, gateway configuration and platform credentials, cron jobs, plugins/MCP servers, skills, memory/session data, auxiliary services and their databases. Preserve a rollback copy and keep secrets out of command output and logs.
2. Copy persistent user data while both gateway owners are stopped or otherwise quiesced. Do not transfer live runtime ownership artifacts (PID files, sockets, or gateway state files); they describe the source process, not the destination. Start the destination once, then verify service health and each real messaging/integration path there.
3. Before disabling the source, inspect every launch path. Disabling its systemd service is not sufficient when a client such as Hermes Desktop can run `hermes gateway ensure` and spawn a local gateway on demand. Check the actual client backend/connection mode and verify which host owns the live gateway after opening the client.
4. Prevent duplicate scheduled work and messages during transition: establish which host owns cron execution and platform credentials, and ensure the other copy cannot run the same jobs or connect as the same bot. Preserve the source configuration for rollback rather than deleting it.
5. Treat a cutover as incomplete until the destination has been exercised through the actual client and external integration, and the source is confirmed not to be processing. Keep rollback bounded and restore the source's services, credentials and scheduled jobs together.

Concrete recipe (source-fact capture, venv rebuilds, disabled units, detached cutover with health poll and rollback, bandwidth-safe pre-copy, desktop switcher semantics, tunnel-only backend→frontend SSH, post-move capability audit): `references/gateway-host-migration.md`.

Do not claim that a cutover script, copied login state, or a green service status proves the migration worked; verify the live client connection and external behavior. When the destination setup or rollback path has not been exercised, state that the cutover remains pending rather than handing over an unverified command as completed work.

#### Discovering third-party platform adapters

Before saying Hermes lacks an adapter or recommending a platform switch, check in this order: (1) the live Hermes official plugin catalog/docs, (2) the installed Hermes source's `plugin-catalog/` and plugin registry, then (3) repository search for adapter implementations. Search both platform and project terms (for example, `Fluxer Hermes Agent`); an integration can exist as a community plugin even when absent from the core supported-platform list. Read the candidate's README, manifest and setup instructions; verify required Hermes hooks/API compatibility against the current adapter/plugin docs before recommending installation. Distinguish catalog-listed, repository-found, installed, and runtime-tested states—discovery is not proof the plugin works in this environment. Compare a bundled platform (such as Slack) with a community adapter on concrete capabilities, maintenance signals, permission model and setup burden; do not equate "more integrated" with "better fit" or recommend switching without explaining the trade-off. Treat star counts and repository activity as weak maturity signals, not security or reliability evidence; review code and scopes before handling credentials or granting permissions.

### Sessions

```
hermes sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
hermes cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
hermes webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
hermes profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
hermes profile rename A B | alias NAME | export NAME | import FILE
hermes profile migrate-identity A B   Retry a completed rename's session/routing identity migration
```

### Credentials & Pools

```
hermes auth                 Interactive credential manager
hermes auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
hermes auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
hermes desktop / gui        Native desktop app
hermes dashboard            Web admin panel + embedded chat (--stop / --status)
hermes proxy                OpenAI-compatible local proxy backed by an OAuth provider
hermes portal               Quick setup / sign in via Nous Portal
hermes kanban <verb>        Multi-agent work-queue board
hermes project              Named multi-folder workspaces (see references/projects-and-folders.md)
hermes skin list|use|set    Switch/tweak skins (see references/themes.md)
hermes pets <verb>          Pet mascots (see references/petdex.md)
hermes memory setup|status|off|reset   Memory provider
hermes secrets bitwarden|onepassword   External secret stores
hermes moa                  Mixture-of-Agents slots
hermes hooks / security / backup / import / checkpoints / console
hermes logs [-f] [errors]   View agent/error logs
hermes send                 One-off message through a gateway platform
hermes pairing / plugins / insights / journey / computer-use
hermes acp                  ACP server (IDE integration)
hermes completion bash|zsh|fish
hermes update / uninstall
```

Plugin- and provider-supplied subcommands (e.g. `hermes photon setup`) only appear once their plugin is installed/active. Importing from OpenClaw, Claude Code, Codex CLI or Cursor (`hermes import-agent <agent>`, plus the older `hermes claw migrate|cleanup`) comes from the `agent-importers` plugin; a fresh `hermes setup` that finds one of those agents offers to install it.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `hermes config edit` · [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `hermes tools list` · [Tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `hermes skills browse` · [Skills catalog](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `hermes model` · [Providers guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `hermes config env-path` · [Env vars reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.hermes/logs/gateway.log` (or `hermes logs`) |
| Sessions | `hermes sessions browse` (reads state.db) |
