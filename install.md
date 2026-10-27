---
name: browser-install
description: Install Titan's isolated headless runner and troubleshoot approved legacy browser-harness callers.
---

# Titan browser runner installation

New navigation, extraction, and verification jobs use `titan-browser run`. It starts a new headless Playwright process and fresh context. It never attaches to shared Chrome, 9333, or a user profile. Use `titan-browser doctor` only to diagnose the local 9333 transport. The doctor does not test login state or extension/in-app-browser permissions.

## Dry-run install

From the repository checkout, write the dry-run plan to a file, then inspect every destination, SHA-256 value, conflict, receipt, and rollback action:

```bash
python3 scripts/install-titan-browser.py --home "$HOME" --codex-home "${CODEX_HOME:-$HOME/.codex}" > /tmp/titan-browser-install-plan.json
python3 -c 'import json; print(json.load(open("/tmp/titan-browser-install-plan.json"))["manifest_sha256"])'
```

The installer defaults to read-only dry-run. It registers Claude Code at `~/.claude/skills/browser-harness/` and Codex at `${CODEX_HOME:-~/.codex}/skills/browser-harness/`. It places the launcher on Unix-like systems at `~/.local/bin/titan-browser` and installs the pinned runner under `~/.local/share/titan-browser/`. On Windows it uses `%LOCALAPPDATA%\Titan\bin` and `%LOCALAPPDATA%\Titan\share\browser-harness`.

Review the manifest and its hash. Apply requires both exact values and rejects changed sources or destinations:

```bash
python3 scripts/install-titan-browser.py --home "$HOME" --codex-home "${CODEX_HOME:-$HOME/.codex}" --apply --manifest /tmp/titan-browser-install-plan.json --manifest-sha256 <reviewed-hash>
```

The installer does not download Node, npm dependencies, or a browser. It writes a private receipt with hashes and backups for managed updates. `--rollback` restores/removes only files whose installed hash still matches the receipt; edited files are reported as conflicts and left alone. On Windows the launcher and doctor can be installed, but `run` fails closed because this version does not verify private artifact-directory ACLs. Isolated jobs are supported on macOS and Linux only.

If a process stops during an apply, the next dry-run reports `recovery_required`. Do not apply a new plan until you run `--rollback` against that receipt and resolve any reported conflicts. Then generate and review a fresh plan. Rollback restores the prior receipt metadata exactly, or removes the receipt if the interrupted operation was the first install.

## Provision the doctor runtime once

The installer places `doctor-requirements.txt` in the install root and includes its hash in the reviewed manifest. It does not create a Python environment or install packages. `titan-browser doctor` uses only the dedicated `doctor-venv` below that root. It checks for `websockets==15.0.1` and fails closed if it is missing or mismatched. It never falls back to ambient Python or installs dependencies at runtime.

On macOS and Linux, use the install root shown in the plan (normally `~/.local/share/titan-browser`):

```bash
TITAN_BROWSER_HOME="$HOME/.local/share/titan-browser"
python3 -m venv "$TITAN_BROWSER_HOME/doctor-venv"
"$TITAN_BROWSER_HOME/doctor-venv/bin/python" -m pip install --no-input --disable-pip-version-check -r "$TITAN_BROWSER_HOME/doctor-requirements.txt"
```

On Windows PowerShell, use the install root shown in the plan (normally `$env:LOCALAPPDATA\Titan\share\browser-harness`):

```powershell
$TitanBrowserHome = "$env:LOCALAPPDATA\Titan\share\browser-harness"
py -3 -m venv "$TitanBrowserHome\doctor-venv"
& "$TitanBrowserHome\doctor-venv\Scripts\python.exe" -m pip install --no-input --disable-pip-version-check -r "$TitanBrowserHome\doctor-requirements.txt"
```

## Provision the isolated runner once

Read the install plan to find the installed `integrations/titan-playwright/` directory. From that directory, provision the pinned Node dependency and Chromium browser explicitly:

```bash
npm ci
npx playwright install chromium
```

These commands may download package and browser files. The `titan-browser run` launcher never downloads dependencies or browsers. It checks Node 22.18+, Playwright 1.64.1, and the installed Chromium executable before running.

Run a job from any current directory:

```bash
titan-browser run /absolute/path/to/job.json
```

An explicitly supplied `--storage-state` file must be absolute, a regular non-symlink owned by the current user, private to that user, and within the 5 MiB limit. The runner reads it without modifying or deleting it. On platforms where `O_NOFOLLOW` and owner/mode verification are unavailable, secure storage-state import is rejected.

The job schema, retention policy, and troubleshooting steps are in `docs/titan-browser-workflow.md`.

## Permission troubleshooting

Inspect the effective permission context of the current runtime or task. Use only its documented permission request flow. If local access is refused, stop and report the refusal; do not claim that changing a global setting changes an already-running task. The shell doctor cannot diagnose an extension/native host or Codex in-app browser.

For a read-only 9333 transport check:

```bash
titan-browser doctor --endpoint http://127.0.0.1:9333 --json
```

`READY` means only that `/json/version` and one `Browser.getVersion` request worked. The doctor always reports `AUTH_NOT_CHECKED`. It never starts Chrome, lists targets, opens UI, navigates, or reads cookies.

Existing approved Ads and GA4 workflows that use shared 9333 remain on their scoped legacy path. Do not extend that path to new general-purpose work. The older `browser-harness` CLI, cloud-daemon commands, and recording controls remain available only to their existing authorized callers.

## Legacy browser-harness CLI installation

Use only for existing authorized legacy Ads and GA4 workflows. New jobs use the isolated Titan runner above.

### Fast Path

```bash
uv tool install --python 3.12 --upgrade --force browser-harness
```

This installs the legacy CLI only. It does not install Titan's headless runner. Do not overwrite the Titan skill with `browser-harness skill`; the skill is registered by the Titan installer above.

`--python 3.12` prevents uv from selecting old releases that support older Python versions. `--upgrade --force` replaces any previous `browser-harness` tool install with the latest stable release. It does not uninstall unrelated commands such as `browser-use-Browser` or `browser-use-Terminal`.

For an existing authorized legacy Ads or GA4 caller, use this scoped trigger for shared-browser steps:

```text
Use browser-harness shared-CDP steps only for the explicitly approved legacy Ads and GA4 workflows. Use `titan-browser run` for new general-purpose navigation, extraction, and verification jobs.
```

If an old user-installed `browser` or `browser-use` skill is being picked instead, remove that stale skill directory manually. Do not edit bundled/vendor plugin caches.

### Recording Consent (Legacy Browser-Harness Only)

Run `browser-harness recordings`. If it reports `(default)`, ask the user once:

> Enable local browser recordings? This saves screenshots and action traces on
> this machine, which may include sensitive page content, so you can later ask
> “show me what you did” or request a video. Videos are never generated
> automatically. [y/N]

Default to no. Run `browser-harness recordings enable` only after yes; otherwise
run `browser-harness recordings disable`. Preserve an existing `(config)` or
`(BH_RECORD)` preference during upgrades instead of asking again.

### If Chrome Blocks a Legacy Shared-Browser Connection

In Chrome:

1. Open `chrome://inspect/#remote-debugging`.
2. Tick "Allow remote debugging for this browser instance".
3. Retry `page_info()`.

If that reports `permission-blocked` on macOS, handle the per-connection Allow
sheet without bringing Chrome to the foreground:

```bash
browser-harness mac-approve
```

Continue browser work when the helper returns `ready`; otherwise follow its
printed instruction. The first checkbox is intentionally a one-time manual
Chrome setup step; it is not exposed to the harness until CDP is available.

The helper requires Accessibility permission for the app launching the CLI
(for example Terminal, iTerm, Codex, or an IDE) in System Settings.

### Cloud Browsers (Existing Authorized Callers Only)

Cloud is optional. Local Chrome does not need a Browser Use API key.

Use any short made-up name; `r7k2` below is just a placeholder.

```bash
browser-harness auth login
browser-harness <<'PY'
start_remote_daemon("r7k2")
PY
```

Then use it by name:

```bash
BU_NAME=r7k2 browser-harness <<'PY'
print(page_info())
PY
```

### Legacy Browser-Harness Diagnostics

```bash
browser-harness --doctor
```

Use the output:

- `chrome running` FAIL: ask the user to open Chrome, or use isolated/cloud browser.
- `daemon alive` FAIL: Chrome remote debugging permission is missing, Chrome is closed, or the CDP endpoint is not reachable.
- update available: run `browser-harness --update -y` when you decide to upgrade.

For a machine-readable health check, an orchestrator can set `BU_NAME` to an
already-provisioned daemon and run:

```bash
browser-harness doctor --json --require-existing-daemon
```

This prints a versioned JSON report and exits nonzero unless that exact daemon
has a live browser connection. It never starts or discovers another browser.

If this still fails, inspect `src/browser_harness/admin.py`, `src/browser_harness/daemon.py`, and `src/browser_harness/_ipc.py`.

Useful:

```bash
browser-harness --update -y
browser-harness telemetry disable
```

State lives under `${XDG_CONFIG_HOME:-~/.config}/browser-harness` by default: auth, telemetry id, agent workspace, runtime sockets, logs, screenshots, and temp files. Override with `BH_HOME` or `BROWSER_HARNESS_HOME`.
