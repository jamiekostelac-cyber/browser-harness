# Codex browser access

Browser connectivity has three separate paths:

- A task shell or direct CDP client uses the execution context's effective task permissions.
- A browser extension or native host uses its own app and permission boundary.
- The Codex in-app browser is a separate browser surface.

Success on one path does not prove access on another. Inspect the effective permissions for the current task through the runtime's supported interface. If a supported request flow exists, use it for the named task. If access is refused or unavailable, stop and report the observed refusal. Do not infer that a global setting changes an already-running task, and do not invent a local broker or alternate browser route.

For new navigation, extraction, and verification work, use `titan-browser run <job.json>`. It launches its own headless process and fresh context, and never attaches to 9333. The launcher removes inherited `BU_*`, `BH_*`, and `BROWSER_HARNESS_*` settings from the job process.

Use `titan-browser doctor --endpoint http://127.0.0.1:9333 --json` only to diagnose local 9333 transport. `READY` proves that the version endpoint and one Browser-domain CDP request worked. `AUTH_NOT_CHECKED` is always reported. The doctor cannot validate extension/native-host or in-app-browser access.

Do not disable task sandboxing globally to repair one local-browser failure. Do not expose CDP beyond loopback, copy a live profile, or silently switch to a shared or cloud browser.
