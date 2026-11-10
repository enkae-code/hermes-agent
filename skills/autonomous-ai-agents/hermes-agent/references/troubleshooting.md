# Troubleshooting

### Voice not working
1. Check `stt.enabled: true` in config.yaml
2. Verify provider: `python -c "import pm; pm.sync_venv(['stt-whisper'], explicit=True)"` or set API key
3. In gateway: `/restart`. In CLI: exit and relaunch.

### Tool not available
1. `hermes tools` — check if toolset is enabled for your platform
2. Some tools need env vars (check `.env`)
3. `/reset` after enabling tools

### Desktop `stream_drop` error cards (provider-side)

Symptom: desktop pops `layer: streaming, code: stream_drop` error cards that auto-report as a new session. Provider logs show the real cause — check `~/.hermes/logs/agent.log` for `Stream drop ... retrying` (agent.stream_diag): typically `RemoteProtocolError: peer closed connection without sending complete message body`, HTTP 200 via Cloudflare, few bytes, long TTFB (20-55s) on large task prompts.

Diagnosis: that signature is an upstream provider dropping the stream mid-response — provider-side, not local config. Confirm with a tiny streaming probe (small max_tokens) — if tiny prompts succeed, local network/auth are fine.

Mitigation: `HERMES_STREAM_RETRIES` (default 2) in `~/.hermes/.env` — bump to 4 so transient drops retry before surfacing a card. Stale-stream detector: `HERMES_STREAM_STALE_TIMEOUT` (default 180s). Restart the Desktop app (or `/reload`) after editing .env. Only a model/provider switch removes the root cause; that's an operator call.

### Model/provider issues
1. `hermes doctor` — check config and dependencies
2. `hermes auth` — re-authenticate OAuth providers (or `hermes auth add <provider>`)
3. Check `.env` has the right API key
4. **GitHub Copilot** (`copilot` / `copilot-acp`) lives in the `github-copilot` labs plugin (`hermes plugins install github-copilot`); sign in with `hermes auth add copilot` — `gh` CLI logins are not read.

### Changes not taking effect
- **Tools/skills:** `/reset` starts a new session with updated toolset
- **Config changes:** In gateway: `/restart`. In CLI: exit and relaunch.
- **Code changes:** Restart the CLI or gateway process
- **Plugins/hooks:** Follow `references/plugin-runtime-debugging.md` before concluding a hook is broken; verify the process that actually dispatches tool calls, not only the short-lived CLI that starts or attaches to it.

### web_extract shows a stale page (result caching)
`web_search`/`web_extract` cache results for 20 minutes (PR #94618) — a
repeat fetch of the same URL within the TTL is served from cache, which
can look like "my website changes aren't showing up."

Automatic carveouts (always fetched live, never cached):
- localhost / 127.0.0.1 / `*.local` / `*.localhost` / single-label LAN
  hostnames / private + link-local IP ranges (dev servers, hot-reload
  builds, chat-GUI artifact previews)
- URLs matched by `security.website_blocklist`
- failed responses and keyless-rescue-served responses

Developing a site tested over the PUBLIC internet (Vercel/Netlify
preview, ngrok/cloudflared tunnel, staging domain)? Public DNS isn't
auto-carved-out — list the host in config.yaml:

```yaml
web:
  cache_exempt_hosts:      # always fetched live; effective immediately
    - mysite.vercel.app
    - "*.ngrok-free.app"
    - mysite.dev           # suffix match: also covers preview.mysite.dev
```

Blunt instruments: `web.cache_ttl_minutes: 1` (min) or
`web.cache_enabled: false` disables both caches entirely.

### Skills not showing
1. `hermes skills list` — verify installed
2. `hermes skills config` — check platform enablement
3. Load explicitly: `hermes -s name` (or the skill's own `/<name>` slash command)

### Desktop composer queues, drafts, and notifications

When text appears above or beside the composer, identify the actual UI element before explaining it: a server-queued prompt, an unsent local draft, a stale pending-submission card, and a gateway completion notification are distinct states. A `user` role in session history does not prove a human typed the message; the gateway can inject internal notifications as user turns.

1. Inspect the user's screenshot/current visible UI and identify the card's labels and controls before interpreting it.
2. Read the relevant Desktop source and corroborate with supported read-only state, matching session IDs, queue status, and timestamps. Do not infer current queue contents from historical notifications or a session title.
3. If live DOM access is unavailable, report that boundary and label storage-derived explanations provisional; never present an inference as verified UI state.
4. Do not click Send/Delete/Stop, clear or edit persisted state, or restart the app just to diagnose. Queued prompts may be legitimate follow-ups waiting behind a long-running turn.

### Gateway issues
For session-resume stalls, compare a small session with a large idle history, and test a large idle history while another session is actively running; sequential small-session success does not rule out lock contention. Exercise the production client/protocol against the live gateway, record bounded response times, and detach each probe session when finished. After a gateway code change, restart from a separate shell outside the gateway process, then verify the service and repeat the same live test. Finally reload the actual client UI and inspect its rendered status/errors: a successful RPC probe alone does not prove the UI opened the chat.

Check logs first:
```bash
grep -i "failed to send\|error" ~/.hermes/logs/gateway.log | tail -20
```

Common gateway problems:
- **Gateway dies on SSH logout**: Enable linger: `sudo loginctl enable-linger $USER`
- **Gateway dies on WSL2 close**: WSL2 requires `systemd=true` in `/etc/wsl.conf` for systemd services to work. Without it, gateway falls back to `nohup` (dies when session closes).
- **Gateway crash loop**: Reset the failed state: `systemctl --user reset-failed hermes-gateway`

### Platform-specific issues
- **Discord bot silent**: Must enable **Message Content Intent** in Bot → Privileged Gateway Intents.
- **Slack bot only works in DMs**: Must subscribe to `message.channels` event. Without it, the bot ignores public channels.
- **Windows-specific issues** (`Alt+Enter` newline, WinError 10106, UTF-8 BOM config, line endings): see `references/windows-quirks.md`.

### Auxiliary models not working
If `auxiliary` tasks (vision, compression, session_search) fail silently, the `auto` provider can't find a backend. Either set `OPENROUTER_API_KEY` or `GOOGLE_API_KEY`, or explicitly configure each auxiliary task's provider:
```bash
hermes config set auxiliary.vision.provider <your_provider>
hermes config set auxiliary.vision.model <model_name>
```

### "Reset permissions" / auto-approving everything
See `references/security-privacy.md` — wipe the "Always allow" stores, don't touch yolo mode.

