# Debugging Hermes Plugin Runtime Hooks

Use this when a Hermes plugin or hook appears enabled but does not fire in an actual session.

## Procedure

1. **Confirm the installation and effective config for the active profile.** Use the profile-aware plugin CLI to inspect/list the plugin and its capabilities. For blocking a tool call, verify the required tool-override capability is explicitly granted; a registration message alone does not prove permission to block.
2. **Trace the real execution path.** Search the Hermes source for the hook dispatcher and its callers. Identify whether the session dispatches tools in the CLI process, a gateway, desktop backend, or another long-lived server. Do not infer the executor from the command the user typed.
3. **Instrument the dispatcher in the process that owns it.** Record a minimal event name, tool name, and returned verdict/error to a private local diagnostic log; avoid recording arguments, credentials, or user content. Ensure the instrumentation actually loads in that process (a direct import in a separate Python process proves only the import path).
4. **Run a minimal end-to-end probe through the same interface and profile as the reported failure.** Use a harmless tool action first. If possible, compare a direct dispatcher call with a real session call to separate plugin logic from runtime wiring.
5. **Check process age and reload behavior.** Plugin changes may be read only at startup. If the active long-lived process predates the installation/config change, arrange a safe restart through the owning app/service, then repeat the same probe. Do not restart the current interactive backend if that would terminate the session without the operator's consent.
6. **Report evidence and confidence separately.** Distinguish observed facts (registration, permission, direct hook result, runtime trace) from hypotheses about why the production path skipped the hook. Do not call the hook fixed until the real session path demonstrates the expected block/allow behavior.
7. **Clean up instrumentation and fixtures.** Restore the plugin source, remove test repositories/logs, and verify the active installed copies are clean before closeout.

## Unexpected queued or system-generated session messages

1. Inspect the exact visible items in the affected session and determine whether they are queued user input, background-task status, gateway notifications, tool output, or ordinary transcript messages. Do not infer queue state from an empty composer or a screenshot summary alone.
2. Correlate message text, session/chat ID, and timestamps with the active session transcript and gateway/agent logs. Search for the exact text or a distinctive fragment before attributing a source.
3. Trace the matched event to its producer (for example, a background process completion, cron delivery, plugin hook, or user queue command). Verify the producer's actual delivery path; a plugin's presence or an agent's interpretation of an image is not proof it sent the item.
4. Report what is confirmed separately from what remains unknown. If a delegated or background investigation is still running, keep the result explicitly provisional and do not present it as the completed explanation.

## Pitfalls

- Test hooks through the same runtime that will execute the tool call; a separate CLI process may load the plugin while a long-lived backend dispatches tools without it.
- Locate the actual hook call sites before monkeypatching a dispatcher; patching the imported function in the wrong process or module namespace produces no evidence about production execution.
- Avoid editing vendor runtime source to instrument it; prefer supported logging, plugin-level diagnostics, or a temporary external probe, and keep any diagnostic edits reversible and isolated.
- Treat enablement, registration, and permission as separate gates: each can succeed while a later runtime path still omits the hook.
- Do not label every visible system card as a queued message or attribute messages to the first plausible plugin; identify the exact item and correlate it to its producer before concluding.
