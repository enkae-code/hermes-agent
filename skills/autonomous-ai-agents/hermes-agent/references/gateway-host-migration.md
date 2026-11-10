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
Copy source units verbatim, drop PATH entries that don't exist there, add a memory cap drop-in, and resolve port clashes with an env drop-in (webhook adapter: `Environment=WEBHOOK_PORT=<free>`). Verify with `systemd-analyze --user verify` and `is-enabled`=disabled until cutover. Check bind addresses after start: the webhook adapter listens on 0.0.0.0, so confirm the firewall covers it.

## 4. Cutover script shape
Run detached: the operator launches it from a separate terminal with `systemd-run --user --unit=<name> --collect bash -c '<script> >> <log> 2>&1'`. The gateway's safety guard refuses any command that references a script able to stop the gateway (even `bash -n script`); syntax-check it by piping the source to `bash -n` via execute_code.
Order: preflight (dest units exist, dest ports free) -> stop source gateway + memory service -> final rsync (`--no-specials`, exclude `gateway.pid`, `gateway.sock`, `gateway_state.json`; delete those on dest) -> start dest services -> poll dest `~/.hermes/gateway_state.json` until `gateway_state == running` and each platform `state == connected` with `updated_at` >= start time -> disable source autostart -> strip bot tokens from source `.env` and move `cron/jobs.json` aside (Desktop `gateway ensure` can still spawn a local gateway) -> report via a messaging API post, because the chat that launched it is gone. Send that report BEFORE stripping the source `.env` (or read the token from the `.pre-*` backup): a notifier that reads the bot token from the just-stripped file fails silently and the operator never hears the result. Rollback restores `.env`, cron jobs, units and tunnels together. Offer `--rollback`.

## 5. Protect the operator's bandwidth
Measure the pending delta with `rsync -an --stats` first. If the operator is streaming or on a call, don't run the cutover; run a warm pre-copy instead (`nice -n 19 ionice -c3 rsync --partial --bwlimit=<KB/s>` under `systemd-run --user`) while the source keeps running, so the final in-cutover copy is small. Tell them how to stop it (`systemctl --user stop <unit>`).

## 6. Verify where you are running
From inside the chat: `hostname`, `/etc/os-release`, the gateway unit's start time, `gateway_state.json` platform states, and reverse-tunnel listeners. Desktop's Settings -> Gateways -> "This window" can still show **Local gateway** while Sessions is on the remote: sessions are switched from the Sessions sidebar gateway selector. Recommend making the remote **Primary** and turning on "return to Sessions on the last-used gateway"; switching to "This PC" starts a separate local gateway with its own diverging chats/memory.

A few minutes after cutover, check the source again: with "This window" left on **Local gateway**, Desktop re-starts the disabled source `hermes-gateway` unit on its own (multi-GB RAM, bots disconnected because tokens were stripped). The fix is the client setting, not more unit disabling: set "This window" to **Connect via SSH** to the destination, then stop the source unit and re-check it stays inactive.

## 7. Let the backend reach the frontend again
The backend host usually can't SSH into the desktop (no sshd / firewall). Give it a tunnel-only path instead of opening a port:
- On the destination: a dedicated key (`ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_pc -N ''`) and a `Host pc` alias (`HostName 127.0.0.1`, `Port 2201`, `IdentitiesOnly yes`).
- On the frontend (operator pastes; needs sudo): sshd drop-in `ListenAddress 127.0.0.1` + `PasswordAuthentication no` + `KbdInteractiveAuthentication no`, `systemctl enable --now sshd`, key appended as `from="127.0.0.1",no-agent-forwarding ssh-ed25519 ...`, and `-R 127.0.0.1:2201:127.0.0.1:22` added to the existing tunnel unit, then restart it.
- Verify from the destination: `ss -ltn | grep 2201` and `ssh pc hostname`.
Tell the operator plainly that anyone with root on the backend host can use that key while the tunnel is up.

## 8. Audit what behaves differently after the move
Answer "what won't work the same" from checks, not memory:
- Vault sync: write a probe note on the backend, wait for it on the frontend, edit it there, wait for it back, delete it and confirm the delete propagates; report the measured seconds. Check Syncthing folder type is sendreceive on both sides, `needFiles` 0, errors 0. Concurrent edits while one side is offline become `.sync-conflict` files, not data loss.
- Obsidian REST/MCP over the reverse tunnel: `curl -sk https://127.0.0.1:27124/` (200) works only while the frontend and Obsidian are up.
- Logged-in browser work: `browser.use_real_profile` + `headed` launches Chrome on the gateway host; a headless backend with no Chrome or display can't do it until a CDP tunnel to the frontend Chrome exists. Check `ss -ltn` for a forwarded CDP port before claiming browser tasks work. Pattern that works: on the frontend, a user unit runs Chrome on Hermes's profile copy (`~/.hermes/browser-profile/chrome`; Chrome >=136 refuses CDP on the default dir) with `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9222`, a second unit `ssh -N -R 127.0.0.1:9222:127.0.0.1:9222 <gateway>`, then `browser.cdp_url: http://127.0.0.1:9222` on the gateway. On a shared gateway host, restrict the port with an nft `meta skuid` rule (CDP has no auth). Pitfalls: a copy with `hardware_acceleration_mode.enabled=false` in Local State has no WebGL, so Vercel checkpoints fail with "Code 11"; a re-snapshot copies that setting back. A fresh snapshot is needed when the copy is stale (cookie count is a quick check).
- Screen control and GUI launches act on the gateway host's display, so they need the frontend path (SSH alias or a future remote-desktop route).
List what still works unchanged (chat, bots, cron, memory, code, vault edits) separately from the gaps, and offer the fix for the main gap.
