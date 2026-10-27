# Titan browser workflow

## Default browser route

Use `titan-browser run <job.json>` for new navigation, extraction, and verification work. Each job starts a new headless Playwright process and fresh isolated context. The runner never attaches to an existing visible browser, the shared 9333 endpoint, or a copied profile. It ignores inherited `BU_*`, `BH_*`, and `BROWSER_HARNESS_*` environment settings. It does not download Node dependencies or a browser at invocation.

The launcher works from any current directory. In a repository checkout, provision dependencies from `integrations/titan-playwright/`; after installation, use the exact runner package directory shown in the installer plan:

```bash
npm ci
npx playwright install chromium
```

The launcher verifies Node 22.18 or newer, Playwright 1.64.1, and the installed Chromium executable. If any is missing, it exits with an installation instruction. It does not run `npm`, `npx`, or a download on behalf of a job.

## Job contract

Jobs are JSON with `version: 1`, a bounded `jobId`, `startUrl`, an exact `allowedOrigins` list, `timeoutMs` from 1,000 through 120,000, and at most 25 `goto`, `click`, `fill`, or `snapshot` steps. At most 20 origins are accepted. Every snapshot requires a selector and is redacted before output truncation. `mode` is accepted only as `redacted`; raw snapshot output is not supported. Results report selector, redaction mode, and truncation. Incidental navigation URLs are reduced to origin.

HTTP, HTTPS CONNECT, and WebSocket requests pass through a per-job loopback egress proxy. The proxy checks the exact scheme, hostname, and port against `allowedOrigins` on every hop, including redirects. For CONNECT tunnels it distinguishes TLS from cleartext before opening an upstream socket: TLS requires an approved HTTPS origin, and cleartext requires an approved HTTP origin plus a valid WebSocket upgrade for that same host. It resolves DNS hostnames for each connection, rejects the whole hostname if any IPv4 answer is private or reserved, and connects directly to a vetted public IPv4 address so the connection cannot re-resolve to a private address. Literal private/reserved destinations, DNS names with private/reserved answers, and IPv6 destinations are rejected. The browser's implicit loopback proxy bypass is disabled. Service workers and downloads are blocked. Job JSON has no local-network bypass. Jobs cannot provide JavaScript, shell commands, password fields, a CDP endpoint, headed mode, an executable, profile, or channel. Do not put passwords or secrets in job input.

The localhost exception exists only as an internal test dependency injection used by the fixture suite. The CLI and job schema cannot enable it.

An optional `--storage-state <absolute-file>` can seed a fresh context when the task is authorized to use that state. The runner rejects relative paths, symlinks, non-regular files, files not owned by the current user, group/other-readable files, and files over 5 MiB. It uses no-follow open and a bounded streaming read. It never modifies or deletes the caller's file. On platforms that do not expose `O_NOFOLLOW` and current-owner/mode checks, storage-state import is rejected.

## Artifact privacy and cleanup

Artifacts are supported only on Linux and macOS, beneath a private per-user state directory: `$XDG_STATE_HOME/titan-browser/artifacts` or `~/.local/state/titan-browser/artifacts` on Linux, and `~/Library/Application Support/Titan/Browser Harness/artifacts` on macOS. The runtime creates each job directory privately and writes a manifest containing owner, job ID, status, creation time, and 24-hour expiry. On Windows, runner artifact creation fails closed because private ACL verification is not implemented; the launcher and doctor can still be installed.

Successful jobs confirm tracing stop, context/browser close, and artifact-directory deletion before returning `ok`. Failure traces and video may contain raw page content and remain in the private managed root. They become eligible for cleanup after 24 hours and remain until an explicit `titan-browser cleanup` succeeds. Handle them as sensitive. Cleanup removes only expired, complete, manifest-owned job directories. It preserves active jobs, symlinks, and unrecognized directories and reports cleanup failures. It does not clean arbitrary temporary directories.

A `cleanup_failed` job result means teardown, manifest update, or artifact removal was not confirmed. Its report includes the local managed directory path for follow-up. Do not treat that result as success. The job file is streamed in bounded chunks and rejected above 1 MiB before JSON parsing.

## 9333 transport diagnosis

Run `titan-browser doctor --endpoint http://127.0.0.1:9333 --json` only when diagnosing that endpoint. The doctor performs a single bounded HTTP `GET /json/version` followed by one `Browser.getVersion` call. It has a two-second total HTTP deadline and a 32 KiB response cap. It does not start Chrome, read task/browser configuration, list or modify tabs, navigate, read cookies, or open UI. Every result says `AUTH_NOT_CHECKED`.

An installed doctor uses only the dedicated `doctor-venv` under the install root and requires `websockets==15.0.1`. Provision it once using the platform-specific commands in `install.md`; the launcher fails before probing 9333 if the pinned dependency is missing or mismatched. It does not use ambient Python or download packages at runtime.

The isolated runner disables non-proxied WebRTC UDP with a fixed Chromium launch argument. Direct WebRTC UDP and TURN destinations outside the job's authorized network policy may be blocked, so calls, media, and data channels that depend on them may not connect. The Chromium flag is a browser control, not an operating-system network sandbox; the runner's egress proxy remains the destination boundary.

`READY` confirms transport and one Browser-domain response only. It says nothing about site login. An HTTP rejection means the endpoint was reached but rejected the version request. A transport error means the probe could not complete. The doctor is not a permission probe for a browser extension/native host or the Codex in-app browser.

Inspect the current runtime's effective permission context and use only its supported request flow. If task-local access is refused, stop and report the actual result. Do not assume a global setting updates a running task, do not invent a broker, and do not fall back to a shared/cloud browser.

## Legacy caller boundary

Existing approved Ads and GA4 workflows that use shared 9333 remain on their scoped legacy path. Do not extend that route to new general-purpose work.

The shared `browser-harness` daemon, cloud sessions, recordings, and raw CDP interaction skills remain available only to their existing explicitly authorized callers.
