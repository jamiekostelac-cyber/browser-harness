#!/usr/bin/env node
import { open } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { createEgressProxy } from "./egress-proxy.js";
import { failureReport, redactText, safeUrl, successReport } from "./report.ts";
import { createManagedJobDir, markJobComplete, readPrivateStorageState, removeManagedJobDir } from "./private-files.js";

const MAX_JOB_BYTES = 1024 * 1024;
const MAX_STEPS = 25;
const MAX_ORIGINS = 20;
const MAX_TIMEOUT_MS = 120_000;
const MAX_TEXT_CHARS = 4_000;
const ROLES = new Set([
  "alert", "button", "checkbox", "combobox", "dialog", "heading", "link",
  "list", "listbox", "menu", "menuitem", "option", "radio", "searchbox",
  "slider", "switch", "tab", "table", "textbox", "tree", "treeitem",
]);
const SENSITIVE_LABEL = /password|passcode|secret|credential|token|api[ _-]?key/i;
const CLEANUP_TIMEOUT_MS = 1_000;
const BROWSER_CLOSE_TIMEOUT_MS = 5_000;
const MAX_JOB_READ_CHUNK = 64 * 1024;
export const CHROMIUM_LAUNCH_ARGS = Object.freeze([
  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]);

async function boundedCleanup(operation, timeoutMs = CLEANUP_TIMEOUT_MS) {
  const outcome = Promise.resolve().then(operation);
  let timer;
  return Promise.race([
    outcome,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new JobValidationError("cleanup_timeout")), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function closeBrowserServer(server, timeoutMs) {
  try {
    await boundedCleanup(() => server.close(), timeoutMs);
  } catch {
    // BrowserServer.kill() terminates the owned process if graceful shutdown stalls.
    await boundedCleanup(() => server.kill(), timeoutMs);
  }
}

export class JobValidationError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function exactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).every((key) => keys.includes(key));
}

function validOrigin(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
        || url.pathname !== "/" || url.search || url.hash) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function validateJob(job) {
  if (!exactKeys(job, ["version", "jobId", "startUrl", "allowedOrigins", "timeoutMs", "steps"])) {
    throw new JobValidationError("invalid_job_fields");
  }
  if (job.version !== 1) throw new JobValidationError("unsupported_job_version");
  if (typeof job.jobId !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(job.jobId)) {
    throw new JobValidationError("invalid_job_id");
  }
  if (!Number.isInteger(job.timeoutMs) || job.timeoutMs < 1_000 || job.timeoutMs > MAX_TIMEOUT_MS) {
    throw new JobValidationError("invalid_timeout");
  }
  if (!Array.isArray(job.allowedOrigins) || job.allowedOrigins.length < 1 || job.allowedOrigins.length > MAX_ORIGINS) {
    throw new JobValidationError("invalid_origin_policy");
  }
  const origins = new Set(job.allowedOrigins.map(validOrigin));
  if (origins.has(null) || origins.size !== job.allowedOrigins.length) {
    throw new JobValidationError("invalid_origin_policy");
  }
  let start;
  try { start = new URL(job.startUrl); } catch { throw new JobValidationError("invalid_start_url"); }
  if (!["http:", "https:"].includes(start.protocol) || start.username || start.password) {
    throw new JobValidationError("invalid_start_url");
  }
  const startOrigin = validOrigin(start.origin);
  if (!startOrigin || !origins.has(startOrigin)) throw new JobValidationError("start_origin_not_allowed");
  if (!Array.isArray(job.steps) || job.steps.length < 1 || job.steps.length > MAX_STEPS) {
    throw new JobValidationError("invalid_steps");
  }
  for (const step of job.steps) {
    if (!step || typeof step !== "object" || Array.isArray(step) || typeof step.type !== "string") {
      throw new JobValidationError("invalid_step");
    }
    if (step.type === "goto") {
      if (!exactKeys(step, ["type", "url"]) || typeof step.url !== "string") throw new JobValidationError("invalid_step");
      let origin;
      try {
        const target = new URL(step.url);
        if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) {
          throw new Error("unsupported URL");
        }
        origin = target.origin;
      } catch { throw new JobValidationError("invalid_step_url"); }
      if (!origins.has(origin)) throw new JobValidationError("step_origin_not_allowed");
    } else if (step.type === "click") {
      if (!exactKeys(step, ["type", "role", "name"]) || !ROLES.has(step.role)
          || typeof step.name !== "string" || !step.name.trim() || step.name.length > 200) {
        throw new JobValidationError("invalid_step");
      }
    } else if (step.type === "fill") {
      if (!exactKeys(step, ["type", "label", "value"]) || typeof step.label !== "string"
          || typeof step.value !== "string" || !step.label.trim() || step.label.length > 200
          || step.value.length > 2_000 || SENSITIVE_LABEL.test(step.label)) {
        throw new JobValidationError("invalid_step");
      }
    } else if (step.type === "snapshot") {
      if (!exactKeys(step, ["type", "selector", "mode"]) || typeof step.selector !== "string"
          || !step.selector.trim() || step.selector.length > 300
          || (step.mode !== undefined && step.mode !== "redacted")) {
        throw new JobValidationError("invalid_step");
      }
    } else {
      throw new JobValidationError("unsupported_step");
    }
  }
  return { origins, startUrl: job.startUrl, timeoutMs: job.timeoutMs };
}

function parseArgs(argv) {
  const opts = { jobFile: null, storageState: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--storage-state") {
      if (!argv[index + 1]) throw new JobValidationError("missing_option_value");
      opts.storageState = argv[++index];
    } else if (arg === "--help" || arg === "-h") {
      opts.help = true;
    } else if (arg.startsWith("-")) {
      throw new JobValidationError("unknown_option");
    } else if (opts.jobFile) {
      throw new JobValidationError("unexpected_argument");
    } else {
      opts.jobFile = arg;
    }
  }
  if (!opts.help && !opts.jobFile) throw new JobValidationError("job_file_required");
  return opts;
}

export async function readJob(jobFile) {
  let handle;
  try {
    handle = await open(jobFile, "r");
    const metadata = await handle.stat();
    if (!metadata.isFile()) throw new JobValidationError("invalid_job_file");
    if (metadata.size > MAX_JOB_BYTES) throw new JobValidationError("job_file_too_large");
    const chunks = [];
    const chunk = Buffer.alloc(MAX_JOB_READ_CHUNK);
    let total = 0;
    while (true) {
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (!bytesRead) break;
      total += bytesRead;
      if (total > MAX_JOB_BYTES) throw new JobValidationError("job_file_too_large");
      chunks.push(Buffer.from(chunk.subarray(0, bytesRead)));
    }
    try { return JSON.parse(Buffer.concat(chunks, total).toString("utf8")); }
    catch { throw new JobValidationError("invalid_json"); }
  } catch (error) {
    if (error instanceof JobValidationError) throw error;
    throw new JobValidationError("invalid_job_file");
  } finally {
    if (handle) await handle.close();
  }
}

async function readStorageState(file) {
  if (!file) return undefined;
  try { return await readPrivateStorageState(file); }
  catch (error) {
    throw new JobValidationError(error?.code === "secure_storage_state_unsupported"
      ? "secure_storage_state_unsupported" : "invalid_storage_state");
  }
}

async function executeStep(page, step, index, origins, assertBeforeDeadline) {
  if (step.type === "goto") {
    const response = await page.goto(step.url, { waitUntil: "domcontentloaded" });
    assertBeforeDeadline();
    if (response?.headers()["x-titan-browser-blocked"] === "destination-policy"
        || (response && !origins.has(new URL(response.url()).origin))) {
      throw new JobValidationError("network_destination_blocked");
    }
    return { index, type: step.type, result: safeUrl(page.url()) };
  }
  if (step.type === "click") {
    await page.getByRole(step.role, { name: step.name, exact: true }).click();
    assertBeforeDeadline();
    return { index, type: step.type, result: "clicked" };
  }
  if (step.type === "fill") {
    const field = page.getByLabel(step.label, { exact: true });
    const type = await field.getAttribute("type");
    assertBeforeDeadline();
    if (type === "password") throw new JobValidationError("password_field_blocked");
    await field.fill(step.value);
    assertBeforeDeadline();
    return { index, type: step.type, result: "filled" };
  }
  if (step.type === "snapshot") {
    const rawText = await page.locator(step.selector).innerText();
    assertBeforeDeadline();
    const truncated = rawText.length > MAX_TEXT_CHARS;
    const mode = "redacted";
    const safeText = redactText(rawText);
    return {
      index,
      type: step.type,
      result: safeText.slice(0, MAX_TEXT_CHARS),
      selector: step.selector,
      mode,
      truncated,
    };
  }
  throw new JobValidationError("unsupported_step");
}

export async function runJob(job, options = {}, runtime = null) {
  const { origins, timeoutMs } = validateJob(job);
  const jobId = job.jobId;
  const storageState = options.storageStateData;
  let artifactDir;
  try {
    ({ directory: artifactDir } = await createManagedJobDir({ jobId, root: options.artifactRoot }));
  } catch (error) {
    throw new JobValidationError(error?.code || "private_storage_unavailable");
  }
  let browser;
  let browserServer;
  let context;
  let tracePath = null;
  let videoRef = null;
  let activeStep = null;
  let timer;
  let stage = "startup";
  let launchPromise = null;
  let launchKind = "browser";
  let contextPromise = null;
  let deadlineExpired = false;
  let deadlineCleanupFailed = false;
  let deadlineContextClosed = false;
  let deadlineBrowserClosed = false;
  let deadlineCancellation;
  let networkProxy;
  let page;
  let networkDestinationBlocked = false;
  const results = [];
  const browserCloseTimeoutMs = Math.min(
    BROWSER_CLOSE_TIMEOUT_MS,
    Number.isInteger(runtime?.testOnlyBrowserCloseTimeoutMs)
      ? Math.max(1, runtime.testOnlyBrowserCloseTimeoutMs)
      : BROWSER_CLOSE_TIMEOUT_MS,
  );
  try {
    const beforeDeadline = (operation) => Promise.race([operation, deadline]);
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        deadlineExpired = true;
        reject(new JobValidationError("timeout"));
        deadlineCancellation = (async () => {
          try {
            if (context) {
              await boundedCleanup(() => context.close());
              deadlineContextClosed = true;
            }
          }
          catch { deadlineCleanupFailed = true; }
          try {
            if (browserServer) {
              await closeBrowserServer(browserServer, browserCloseTimeoutMs);
              browserServer = null;
              browser = null;
              deadlineBrowserClosed = true;
            } else if (browser) {
              await boundedCleanup(() => browser.close(), browserCloseTimeoutMs);
              browser = null;
              deadlineBrowserClosed = true;
            }
          }
          catch { deadlineCleanupFailed = true; }
          try { if (networkProxy) await boundedCleanup(() => networkProxy.close()); }
          catch { deadlineCleanupFailed = true; }
        })();
      }, timeoutMs);
    });
    stage = "import";
    const browserApi = runtime?.chromium ? runtime : await beforeDeadline(import("playwright"));
    stage = "policy";
    if (runtime?.testOnlySkipNetworkPolicy !== true) {
      const testOnlyLoopbackOrigins = runtime?.testOnlyAllowLoopback === true
        ? [...origins].filter((origin) => new URL(origin).hostname === "127.0.0.1")
        : [];
      const proxyFactory = runtime?.testOnlyProxyFactory ?? createEgressProxy;
      networkProxy = await beforeDeadline(proxyFactory({
        allowedOrigins: [...origins],
        testOnlyLoopbackOrigins,
        lookupAddresses: runtime?.testOnlyLookup,
        connect: runtime?.testOnlyConnect,
      }));
      runtime?.testOnlyCaptureProxy?.(networkProxy);
    }
    stage = "launch";
    const browserProxy = networkProxy ? {
      server: networkProxy.url,
      bypass: "<-loopback>",
    } : null;
    const launchOptions = {
      headless: true,
      args: [...CHROMIUM_LAUNCH_ARGS],
      ...(browserProxy ? { proxy: browserProxy } : {}),
    };
    if (typeof browserApi.chromium.launchServer === "function") {
      launchKind = "server";
      launchPromise = browserApi.chromium.launchServer(launchOptions);
      browserServer = await beforeDeadline(launchPromise);
      launchPromise = null;
      browser = await beforeDeadline(browserApi.chromium.connect(browserServer.wsEndpoint()));
    } else {
      launchPromise = browserApi.chromium.launch(launchOptions);
      browser = await beforeDeadline(launchPromise);
    }
    launchPromise = null;
    stage = "context";
    contextPromise = browser.newContext({
      ...(storageState ? { storageState } : {}),
      ...(runtime?.testOnlyIgnoreHTTPSErrors === true ? { ignoreHTTPSErrors: true } : {}),
      serviceWorkers: "block",
      acceptDownloads: false,
      recordVideo: { dir: artifactDir, size: { width: 1280, height: 720 } },
    });
    context = await beforeDeadline(contextPromise);
    contextPromise = null;
    stage = "setup";
    await beforeDeadline(context.route("**/*", async (route) => {
      const url = route.request().url();
      if (/^(about|blob|data):/.test(url)) return route.continue();
      let origin;
      try { origin = new URL(url).origin; } catch { return route.abort("blockedbyclient"); }
      if (!origins.has(origin)) {
        networkDestinationBlocked = true;
        return route.abort("blockedbyclient");
      }
      return route.continue();
    }));
    await beforeDeadline(context.tracing.start({ screenshots: true, snapshots: true, sources: false }));
    page = await beforeDeadline(context.newPage());
    page.on?.("request", (request) => {
      try {
        if (!origins.has(new URL(request.url()).origin)) networkDestinationBlocked = true;
      } catch { networkDestinationBlocked = true; }
    });
    page.on?.("requestfailed", (request) => {
      try {
        if (!origins.has(new URL(request.url()).origin)) networkDestinationBlocked = true;
      } catch { networkDestinationBlocked = true; }
    });
    videoRef = page.video();
    page.setDefaultTimeout(timeoutMs);
    page.setDefaultNavigationTimeout(timeoutMs);
    stage = "steps";
    const work = (async () => {
      const response = await page.goto(job.startUrl, { waitUntil: "domcontentloaded" });
      if (response?.headers()["x-titan-browser-blocked"] === "destination-policy"
          || (response && !origins.has(new URL(response.url()).origin))) {
        throw new JobValidationError("network_destination_blocked");
      }
      if (deadlineExpired) throw new JobValidationError("timeout");
      if (runtime?.testOnlyAfterNavigation) {
        await runtime.testOnlyAfterNavigation(page);
        if (deadlineExpired) throw new JobValidationError("timeout");
      }
      for (let index = 0; index < job.steps.length; index += 1) {
        if (deadlineExpired) throw new JobValidationError("timeout");
        activeStep = index;
        results.push(await executeStep(page, job.steps[index], index, origins, () => {
          if (deadlineExpired) throw new JobValidationError("timeout");
        }));
      }
    })();
    await Promise.race([work, deadline]);
    clearTimeout(timer);
    stage = "cleanup";
    await boundedCleanup(() => context.tracing.stop());
    await boundedCleanup(() => context.close());
    context = null;
    if (browserServer) {
      await closeBrowserServer(browserServer, browserCloseTimeoutMs);
      browserServer = null;
    } else {
      await boundedCleanup(() => browser.close(), browserCloseTimeoutMs);
    }
    browser = null;
    if (networkProxy) await boundedCleanup(() => networkProxy.close());
    networkProxy = null;
    await boundedCleanup(() => markJobComplete(artifactDir, "ok"));
    await boundedCleanup(() => (options.removeArtifacts ?? removeManagedJobDir)(artifactDir, options.artifactRoot));
    return successReport(jobId, results);
  } catch (error) {
    clearTimeout(timer);
    let pageOriginBlocked = false;
    try { pageOriginBlocked = page ? !origins.has(new URL(page.url()).origin) : false; } catch { /* no page URL */ }
    const proxyBlocked = networkProxy?.wasBlocked?.() === true;
    const code = error instanceof JobValidationError
      ? error.code
      : networkDestinationBlocked || pageOriginBlocked || proxyBlocked ? "network_destination_blocked"
      : stage === "import" || stage === "launch" ? "browser_unavailable"
        : stage === "policy" ? "network_destination_blocked"
        : stage === "context" ? "context_failed"
          : stage === "setup" ? "setup_failed"
            : stage === "cleanup" ? "cleanup_failed" : "step_failed";
    let cleanupFailed = code === "cleanup_failed" || code === "cleanup_timeout";
    if (deadlineCancellation) await deadlineCancellation;
    if (stage === "context" && contextPromise && deadlineExpired) {
      let graceTimer;
      const lateContext = await Promise.race([
        contextPromise.then((value) => value, () => null),
        new Promise((resolve) => { graceTimer = setTimeout(() => resolve(null), CLEANUP_TIMEOUT_MS); }),
      ]).finally(() => clearTimeout(graceTimer));
      if (lateContext) context = lateContext;
      else {
        cleanupFailed = true;
        contextPromise.then(
          (lateContextValue) => { void boundedCleanup(() => lateContextValue.close()).catch(() => undefined); },
          () => undefined,
        );
      }
    }
    if (stage === "launch" && launchPromise && deadlineExpired) {
      let graceTimer;
      const lateLaunch = await Promise.race([
        launchPromise.then((value) => ({ value }), () => ({ value: null })),
        new Promise((resolve) => { graceTimer = setTimeout(() => resolve(null), CLEANUP_TIMEOUT_MS); }),
      ]).finally(() => clearTimeout(graceTimer));
      if (lateLaunch?.value && launchKind === "server") {
        browserServer = lateLaunch.value;
        try {
          await closeBrowserServer(browserServer, browserCloseTimeoutMs);
          browserServer = null;
          deadlineBrowserClosed = true;
        } catch { cleanupFailed = true; }
      } else if (lateLaunch?.value) {
        browser = lateLaunch.value;
      } else if (!lateLaunch) {
        cleanupFailed = true;
        launchPromise.then((value) => {
          const cleanup = launchKind === "server"
            ? closeBrowserServer(value, browserCloseTimeoutMs)
            : boundedCleanup(() => value.close(), browserCloseTimeoutMs);
          void cleanup.catch(() => undefined);
        }, () => undefined);
      }
    }
    let videoPath = null;
    if (context) {
      if (!deadlineContextClosed) {
        if (!deadlineExpired) {
          try {
            tracePath = path.join(artifactDir, "trace.zip");
            await boundedCleanup(() => context.tracing.stop({ path: tracePath }));
          } catch { tracePath = null; cleanupFailed = true; }
        }
        try {
          await boundedCleanup(() => context.close());
          deadlineContextClosed = true;
        } catch { cleanupFailed = true; }
      }
      context = null;
    }
    if (videoRef) {
      videoPath = path.join(artifactDir, "video.webm");
      try { await boundedCleanup(() => videoRef.saveAs(videoPath)); }
      catch { videoPath = null; cleanupFailed = true; }
    }
    if (browserServer) {
      if (!deadlineBrowserClosed) {
        try {
          await closeBrowserServer(browserServer, browserCloseTimeoutMs);
          deadlineBrowserClosed = true;
        } catch { cleanupFailed = true; }
      }
      browserServer = null;
      browser = null;
    } else if (browser) {
      if (!deadlineBrowserClosed) {
        try {
          await boundedCleanup(() => browser.close(), browserCloseTimeoutMs);
          deadlineBrowserClosed = true;
        } catch { cleanupFailed = true; }
      }
      browser = null;
    }
    if (networkProxy) {
      try { await boundedCleanup(() => networkProxy.close()); } catch { cleanupFailed = true; }
      networkProxy = null;
    }
    if (deadlineExpired && (deadlineContextClosed || !context)
        && (deadlineBrowserClosed || !browser) && !networkProxy) {
      deadlineCleanupFailed = false;
    }
    cleanupFailed ||= deadlineCleanupFailed;
    try { await boundedCleanup(() => markJobComplete(artifactDir, "failed")); }
    catch { cleanupFailed = true; }
    const artifacts = { trace: tracePath, video: videoPath, directory: artifactDir };
    const failureCode = cleanupFailed ? "cleanup_failed"
      : ["timeout", "browser_unavailable", "context_failed", "setup_failed", "network_destination_blocked", "password_field_blocked"].includes(code)
      ? code : "step_failed";
    return failureReport(jobId, failureCode, activeStep, artifacts);
  }
}

export function helpText() {
  return "Usage: titan-browser-job <job.json> [--storage-state <file>]\n";
}

export async function main(argv = process.argv.slice(2)) {
  let opts;
  try { opts = parseArgs(argv); } catch (error) {
    process.stdout.write(`${JSON.stringify(failureReport("unknown", "invalid_job", null, null))}\n`);
    return 2;
  }
  if (opts.help) {
    process.stdout.write(helpText());
    return 0;
  }
  let job;
  try {
    job = await readJob(opts.jobFile);
    const storageStateData = await readStorageState(opts.storageState);
    const result = await runJob(job, { storageStateData });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result.status === "ok" ? 0 : 1;
  } catch (error) {
    const id = typeof job?.jobId === "string" && /^[A-Za-z0-9._-]{1,64}$/.test(job.jobId) ? job.jobId : "unknown";
    const code = error instanceof JobValidationError ? error.code : "invalid_job";
    const result = failureReport(id, "invalid_job", null, null);
    result.error = code;
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return 2;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().then((code) => { process.exitCode = code; });
}
