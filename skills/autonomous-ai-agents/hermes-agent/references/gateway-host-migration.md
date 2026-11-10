# Moving a live gateway to another host (recipe)

## 1. Capture source-side facts first
The destination often cannot SSH back to the source (desktop sshd closed). Before the switch, dump everything you will need from the source into one local dir and read it from there: user unit files + drop-ins (`systemctl --user cat <u>`), exact running command lines of helpers (`ps -o args= -p <pid>`), venv package metadata, ports (`ss -ltnp`). Demoted tool output is gone after compaction; files are not.

## 2. Rebuild environments, don't copy them
- A venv copied across distros carries the source interpreter path and breaks. Recreate it on the destination with uv and the same Python minor version as the source (`<venv>/bin/python --version`).
- Read versions from the installed dist-info, not from memory or a mixed `pip list`: `grep '^Requires-Dist' <pkg>.dist-info/METADATA` shows what the top-level package actually pins (e.g. a meta-package pinning its siblings).
- Editable installs (`direct_url.json` has `"editable": true`): rsync the source checkout INCLUDING uncommitted edits, confirm `git diff --stat` matches, then `uv pip install -e '<checkout>[extras]'`. Pick extras by comparing `Provides-Extra` requirements against what is installed on the source.
- Do not install from a full `pip freeze` of a long-lived venv: it contains local-only packages (e.g. `hermes-agent==0.0.0`) and drifted pins that no longer co-resolve. Let the resolver pick from the package + extras.
- No GPU: `--extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match`, then assert `uv pip list | grep -ci nvidia` is 0.
- Acceptance per service: `--help` exits 0 AND a short real run hits its health endpoint (`timeout 25 <ExecStart> & curl /health`).

## 3. Units on the destination, written but disabled
Copy source units verbatim, drop PATH entries that don't exist there, add a memory cap drop-in, and resolve port clashes with an env drop-in (webhook adapter: `Environment=WEBHOOK_PORT=<free>`). Verify with `systemd-analyze --user verify` and `is-enabled`=disabled until cutover. Check bind addresses after start: by default the webhook adapter binds all IPv4 and IPv6 interfaces (pin one with `platforms.webhook.extra.host`), so confirm the firewall covers it.

## 4. Cutover script shape
Run detached: the operator launches it from a separate terminal with `systemd-run --user --unit=<name> --collect bash -c '<script> >> <log> 2>&1'`. The gateway's safety guard refuses any command that references a script able to stop the gateway (even `bash -n script`).
Order: preflight (dest units exist, dest ports free) -> stop source gateway + memory service -> final rsync (`--no-specials`, exclude `gateway.pid`, `gateway.sock`, `gateway_state.json`; delete those on dest) -> start dest services -> poll dest `~/.hermes/gateway_state.json` until `gateway_state == running` and each platform `state == connected` with `updated_at` >= start time -> disable source autostart -> strip bot tokens from source `.env` and move `cron/jobs.json` aside (Desktop `gateway ensure` can still spawn a local gateway) -> report via a messaging API post, because the chat that launched it is gone. Send that report BEFORE stripping the source `.env` (or read the token from the `.pre-*` backup): a notifier that reads the bot token from the just-stripped file fails silently and the operator never hears the result. Rollback restores `.env`, cron jobs, units and tunnels together. Offer `--rollback`.

## 5. Protect the operator's bandwidth
Measure the pending delta with `rsync -an --stats` first. If the operator is streaming or on a call, don't run the cutover; run a warm pre-copy instead (`nice -n 19 ionice -c3 rsync --partial --bwlimit=<KB/s>` under `systemd-run --user`) while the source keeps running, so the final in-cutover copy is small. Tell them how to stop it (`systemctl --user stop <unit>`).

## 6. Verify where you are running
From inside the chat: `hostname`, `/etc/os-release`, the gateway unit's start time, `gateway_state.json` platform states, and reverse-tunnel listeners. Desktop's Settings -> Gateways -> "This window" can still show **Local gateway** while Sessions is on the remote: sessions are switched from the Sessions sidebar gateway selector. Recommend making the remote **Primary** and turning on "return to Sessions on the last-used gateway"; switching to "This PC" starts a separate local gateway with its own diverging chats/memory.

A few minutes after cutover, check the source again: with "This window" left on **Local gateway**, Desktop re-starts the disabled source `hermes-gateway` unit on its own (multi-GB RAM, bots disconnected because tokens were stripped). The fix is the client setting, not more unit disabling: set "This window" to **Connect via SSH** to the destination, then stop the source unit and re-check it stays inactive.
