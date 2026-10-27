import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer, type Server } from "node:http";
import { createServer as createHttpsServer, type Server as HttpsServer } from "node:https";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import type { Socket } from "node:net";
import { once } from "node:events";
import { promisify } from "node:util";
import { test, expect } from "@playwright/test";
import { isPublicIPv4 } from "../src/egress-proxy.js";
import { readJob, runJob, validateJob, JobValidationError } from "../src/job.js";

const testRoots: string[] = [];
const execFileAsync = promisify(execFile);

async function privateOptions(options: Record<string, unknown> = {}) {
  const artifactRoot = await mkdtemp(path.join(tmpdir(), "titan-browser-test-"));
  testRoots.push(artifactRoot);
  return { ...options, artifactRoot };
}

function testRuntime(runtime: Record<string, unknown>) {
  return { ...runtime, testOnlySkipNetworkPolicy: true };
}

test.afterEach(async () => {
  await Promise.all(testRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

test("job schema rejects unapproved origins and browser attachment overrides", () => {
  const base = {
    version: 1,
    jobId: "fixture",
    startUrl: "http://127.0.0.1:4173/",
    allowedOrigins: ["http://127.0.0.1:4173"],
    timeoutMs: 10_000,
    steps: [{ type: "goto", url: "https://example.com/" }],
  };
  expect(() => validateJob(base)).toThrow(JobValidationError);
  expect(() => validateJob({ ...base, cdpEndpoint: "http://127.0.0.1:9333" })).toThrow(JobValidationError);
  expect(() => validateJob({ ...base, testOnlySkipNetworkPolicy: true })).toThrow(JobValidationError);
  expect(() => validateJob({ ...base, steps: [{ type: "fill", label: "Password", value: "secret" }] })).toThrow(JobValidationError);
  expect(() => validateJob({ ...base, steps: [{ type: "snapshot" }] })).toThrow(JobValidationError);
  expect(() => validateJob({ ...base, steps: [{ type: "snapshot", selector: "main", mode: "raw" }] }))
    .toThrow(JobValidationError);
});

test("network policy rejects private and reserved IPv4 destinations", async () => {
  for (const address of ["0.1.2.3", "10.1.2.3", "100.64.0.1", "127.0.0.1", "169.254.169.254", "172.16.0.1", "192.168.1.1", "198.18.0.1", "203.0.113.10", "224.0.0.1"]) {
    expect(isPublicIPv4(address)).toBe(false);
  }
  expect(isPublicIPv4("8.8.8.8")).toBe(true);
});

test("job files are size checked before bounded streaming and JSON parsing", async () => {
  const pathToJob = path.join(await privateOptions().then((value) => value.artifactRoot), "oversized.json");
  await import("node:fs/promises").then(({ writeFile }) => writeFile(pathToJob, Buffer.alloc(1_048_577)));
  await expect(readJob(pathToJob)).rejects.toMatchObject({ code: "job_file_too_large" });
});

test("job starts a fresh headless browser and runs only the local fixture origin", async () => {
  const server: Server = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end("<main><button>Continue</button><p>Fixture loaded</p></main>");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fixture server did not bind TCP");
  const origin = `http://127.0.0.1:${address.port}`;
  try {
    const result = await runJob({
      version: 1,
      jobId: "local-fixture",
      startUrl: `${origin}/`,
      allowedOrigins: [origin],
      timeoutMs: 10_000,
      steps: [{ type: "click", role: "button", name: "Continue" }, { type: "snapshot", selector: "main" }],
    }, await privateOptions(), { testOnlyAllowLoopback: true } as never);
    expect(result.status, JSON.stringify(result)).toBe("ok");
    expect(result.steps[0].result).toBe("clicked");
    expect(result.steps[1].result).toContain("Fixture loaded");
    expect(result.steps[1]).toMatchObject({ selector: "main", mode: "redacted", truncated: false });
  } finally {
    server.close();
    await once(server, "close");
  }
});

test("redirect to an unapproved localhost port is blocked by the egress proxy", async () => {
  let destinationHits = 0;
  let observedProxy: { wasBlocked?: () => boolean } | undefined;
  const destination: Server = createServer((_request, response) => {
    destinationHits += 1;
    response.end("must not be reached");
  });
  destination.listen(0, "127.0.0.1");
  await once(destination, "listening");
  const destinationAddress = destination.address();
  if (!destinationAddress || typeof destinationAddress === "string") throw new Error("fixture server failed");
  const source: Server = createServer((_request, response) => {
    response.writeHead(302, { location: `http://127.0.0.1:${destinationAddress.port}/private` });
    response.end();
  });
  source.listen(0, "127.0.0.1");
  await once(source, "listening");
  const sourceAddress = source.address();
  if (!sourceAddress || typeof sourceAddress === "string") throw new Error("fixture server failed");
  const origin = `http://127.0.0.1:${sourceAddress.port}`;
  try {
    const result = await runJob({
      version: 1,
      jobId: "redirect-blocked",
      startUrl: `${origin}/`,
      allowedOrigins: [origin],
      timeoutMs: 10_000,
      steps: [{ type: "snapshot", selector: "body" }],
    }, await privateOptions(), {
      testOnlyAllowLoopback: true,
      testOnlyCaptureProxy: (proxy: { wasBlocked?: () => boolean }) => { observedProxy = proxy; },
    } as never);
    expect(result).toMatchObject({ status: "failed", error: "network_destination_blocked", stepIndex: null });
    expect(destinationHits).toBe(0);
    expect(observedProxy?.wasBlocked?.()).toBe(true);
  } finally {
    source.close();
    destination.close();
    await Promise.all([once(source, "close"), once(destination, "close")]);
  }
});

test("real headless Chromium can use an explicitly allowed plain WebSocket origin", async () => {
  let upgradeHits = 0;
  const websocketSockets = new Set<Socket>();
  const server: Server = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(`<!doctype html><html><head><script>
      window.socket = new WebSocket("ws://127.0.0.1:${(server.address() as { port: number }).port}/socket");
      window.socket.onopen = () => { document.documentElement.dataset.socket = "open"; };
      window.socket.onerror = () => { document.documentElement.dataset.socket = "error"; };
    </script></head><body><main>WebSocket fixture</main></body></html>`);
  });
  server.on("upgrade", (request, socket) => {
    upgradeHits += 1;
    websocketSockets.add(socket);
    socket.on("close", () => websocketSockets.delete(socket));
    const key = request.headers["sec-websocket-key"];
    if (!key) { socket.destroy(); return; }
    const accept = createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "\r\n",
    ].join("\r\n"));
    socket.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fixture server did not bind TCP");
  const origin = `http://127.0.0.1:${address.port}`;
  let observedProxy: { wasBlocked?: () => boolean } | undefined;
  try {
    const result = await runJob({
      version: 1,
      jobId: "allowed-websocket",
      startUrl: `${origin}/`,
      allowedOrigins: [origin],
      timeoutMs: 10_000,
      steps: Array.from({ length: 15 }, () => ({ type: "snapshot", selector: "main" })),
    }, await privateOptions(), {
      testOnlyAllowLoopback: true,
      testOnlyCaptureProxy: (proxy: { wasBlocked?: () => boolean }) => { observedProxy = proxy; },
    } as never);
    expect(result.status, JSON.stringify(result)).toBe("ok");
    expect(upgradeHits).toBe(1);
    expect(observedProxy?.wasBlocked?.()).toBe(false);
  } finally {
    for (const socket of websocketSockets) socket.destroy();
    server.close();
    await once(server, "close");
  }
});

test("real headless Chromium can use an explicitly allowed secure WebSocket origin", async () => {
  test.skip(process.platform === "win32", "runner artifacts fail closed on Windows");
  const certificateRoot = await mkdtemp(path.join(tmpdir(), "titan-browser-wss-cert-"));
  testRoots.push(certificateRoot);
  const keyPath = path.join(certificateRoot, "key.pem");
  const certPath = path.join(certificateRoot, "cert.pem");
  await execFileAsync("openssl", [
    "req", "-x509", "-newkey", "rsa:2048", "-nodes",
    "-keyout", keyPath, "-out", certPath,
    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-days", "2",
  ], { timeout: 10_000 });
  let upgradeHits = 0;
  const websocketSockets = new Set<Socket>();
  let server!: HttpsServer;
  server = createHttpsServer({ key: await readFile(keyPath), cert: await readFile(certPath) }, (_request, response) => {
    const address = server.address();
    if (!address || typeof address === "string") { response.writeHead(500).end(); return; }
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(`<!doctype html><html><head><script>
      window.socket = new WebSocket("wss://127.0.0.1:${address.port}/socket");
      window.socket.onopen = () => { document.documentElement.dataset.socket = "open"; };
    </script></head><body><main>Secure WebSocket fixture</main></body></html>`);
  });
  server.on("upgrade", (request, socket) => {
    upgradeHits += 1;
    websocketSockets.add(socket);
    socket.on("close", () => websocketSockets.delete(socket));
    const key = request.headers["sec-websocket-key"];
    if (!key) { socket.destroy(); return; }
    const accept = createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "\r\n",
    ].join("\r\n"));
    socket.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fixture server did not bind TCP");
  const origin = `https://127.0.0.1:${address.port}`;
  let observedProxy: { wasBlocked?: () => boolean } | undefined;
  try {
    const result = await runJob({
      version: 1,
      jobId: "allowed-secure-websocket",
      startUrl: `${origin}/`,
      allowedOrigins: [origin],
      timeoutMs: 10_000,
      steps: Array.from({ length: 15 }, () => ({ type: "snapshot", selector: "main" })),
    }, await privateOptions(), {
      testOnlyAllowLoopback: true,
      testOnlyIgnoreHTTPSErrors: true,
      testOnlyCaptureProxy: (proxy: { wasBlocked?: () => boolean }) => { observedProxy = proxy; },
    } as never);
    expect(result.status, JSON.stringify(result)).toBe("ok");
    expect(upgradeHits).toBe(1);
    expect(observedProxy?.wasBlocked?.()).toBe(false);
  } finally {
    for (const socket of websocketSockets) socket.destroy();
    server.close();
    await once(server, "close");
  }
});

test("runner launches its own headless process and a new isolated context", async () => {
  const launchOptions: unknown[] = [];
  const contextOptions: unknown[] = [];
  let currentUrl = "about:blank";
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async (url: string) => { currentUrl = url; },
    url: () => currentUrl,
    getByRole: () => ({ click: async () => undefined }),
    locator: () => ({ innerText: async () => "fixture text" }),
  };
  const context = {
    route: async () => undefined,
    routeWebSocket: async () => undefined,
    tracing: { start: async () => undefined, stop: async () => undefined },
    newPage: async () => page,
    close: async () => undefined,
  };
  const browser = {
    newContext: async (options: unknown) => { contextOptions.push(options); return context; },
    close: async () => undefined,
  };
  const runtime = {
    chromium: {
      launch: async (options: unknown) => { launchOptions.push(options); return browser; },
      connectOverCDP: () => { throw new Error("must not attach to an existing browser"); },
    },
  };
  const testOnlyProxyFactory = async () => ({ url: "http://127.0.0.1:19999", close: async () => undefined });
  const result = await runJob({
    version: 1,
    jobId: "isolated-context",
    startUrl: "https://public.example/",
    allowedOrigins: ["https://public.example"],
    timeoutMs: 10_000,
    steps: [{ type: "snapshot", selector: "body" }],
  }, await privateOptions(), {
    ...runtime,
    testOnlyProxyFactory,
    testOnlyLookup: async () => [{ address: "8.8.8.8" }],
  } as never);

  expect(result.status).toBe("ok");
  expect(launchOptions).toEqual([{
    headless: true,
    proxy: { server: expect.stringMatching(/^http:\/\/127\.0\.0\.1:/), bypass: "<-loopback>" },
  }]);
  expect(contextOptions).toHaveLength(1);
  expect(contextOptions[0]).toMatchObject({ serviceWorkers: "block", acceptDownloads: false });
  expect(contextOptions[0]).toHaveProperty("recordVideo.dir");
});

test("a browser that launches after timeout is closed", async () => {
  let closeCalls = 0;
  const runtime = {
    chromium: {
      launch: async () => new Promise((resolve) => setTimeout(() => resolve({
        close: async () => { closeCalls += 1; },
      }), 1_100)),
    },
  };
  const result = await runJob({
    version: 1,
    jobId: "late-launch",
    startUrl: "http://fixture.test/",
    allowedOrigins: ["http://fixture.test"],
    timeoutMs: 1_000,
    steps: [{ type: "snapshot", selector: "body" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "timeout" });
  await new Promise((resolve) => setTimeout(resolve, 150));
  expect(closeCalls).toBe(1);
});

test("deadline cancellation preserves timeout when tracing cannot stop after context close", async () => {
  let contextClosed = false;
  let contextCloseCalls = 0;
  let browserCloseCalls = 0;
  let tracingStopCalls = 0;
  let rejectSnapshot!: (error: Error) => void;
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async () => null,
    url: () => "http://fixture.test/",
    locator: () => ({
      innerText: async () => new Promise((_resolve, reject) => { rejectSnapshot = reject; }),
    }),
  };
  const context = {
    route: async () => undefined,
    tracing: {
      start: async () => undefined,
      stop: async () => {
        tracingStopCalls += 1;
        if (contextClosed) throw new Error("Target page, context or browser has been closed");
      },
    },
    newPage: async () => page,
    close: async () => {
      contextCloseCalls += 1;
      contextClosed = true;
      rejectSnapshot(new Error("Target page, context or browser has been closed"));
    },
  };
  const runtime = {
    chromium: {
      launch: async () => ({
        newContext: async () => context,
        close: async () => { browserCloseCalls += 1; },
      }),
    },
  };
  const result = await runJob({
    version: 1,
    jobId: "timeout-cleanup-order",
    startUrl: "http://fixture.test/",
    allowedOrigins: ["http://fixture.test"],
    timeoutMs: 1_000,
    steps: [{ type: "snapshot", selector: "h1" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "timeout", stepIndex: 0 });
  expect(result.artifacts?.trace).toBeNull();
  expect(contextCloseCalls).toBe(1);
  expect(browserCloseCalls).toBe(1);
  expect(tracingStopCalls).toBe(0);
});

test("deadline cleanup is successful when a slow context close completes on retry", async () => {
  let contextCloseCalls = 0;
  let rejectSnapshot!: (error: Error) => void;
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async () => null,
    url: () => "http://fixture.test/",
    locator: () => ({
      innerText: async () => new Promise((_resolve, reject) => { rejectSnapshot = reject; }),
    }),
  };
  const context = {
    route: async () => undefined,
    tracing: { start: async () => undefined, stop: async () => { throw new Error("closed"); } },
    newPage: async () => page,
    close: async () => {
      contextCloseCalls += 1;
      if (contextCloseCalls === 1) {
        rejectSnapshot(new Error("closed"));
        await new Promise((resolve) => setTimeout(resolve, 1_100));
      }
    },
  };
  const runtime = {
    chromium: {
      launch: async () => ({ newContext: async () => context, close: async () => undefined }),
    },
  };
  const result = await runJob({
    version: 1,
    jobId: "slow-context-close",
    startUrl: "http://fixture.test/",
    allowedOrigins: ["http://fixture.test"],
    timeoutMs: 1_000,
    steps: [{ type: "snapshot", selector: "h1" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "timeout", stepIndex: 0 });
  expect(contextCloseCalls).toBe(2);
});

test("a navigation finishing after deadline cannot trigger another action", async () => {
  let clickCalls = 0;
  let contextCloseCalls = 0;
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async () => new Promise((resolve) => setTimeout(resolve, 1_100)),
    getByRole: () => ({ click: async () => { clickCalls += 1; } }),
    locator: () => ({ innerText: async () => "fixture" }),
  };
  const context = {
    route: async () => undefined,
    routeWebSocket: async () => undefined,
    tracing: { start: async () => undefined, stop: async () => undefined },
    newPage: async () => page,
    close: async () => { contextCloseCalls += 1; },
  };
  const runtime = {
    chromium: {
      launch: async () => ({ newContext: async () => context, close: async () => undefined }),
    },
  };
  const result = await runJob({
    ...cleanupJob,
    jobId: "late-navigation",
    steps: [{ type: "click", role: "button", name: "Continue" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "timeout" });
  await new Promise((resolve) => setTimeout(resolve, 150));
  expect(contextCloseCalls).toBeGreaterThan(0);
  expect(clickCalls).toBe(0);
});

test("context startup failures are not reported as browser launch failures", async () => {
  const runtime = {
    chromium: {
      launch: async () => ({ newContext: async () => { throw new Error("context failed"); }, close: async () => undefined }),
    },
  };
  const result = await runJob({
    version: 1,
    jobId: "context-failure",
    startUrl: "http://fixture.test/",
    allowedOrigins: ["http://fixture.test"],
    timeoutMs: 1_000,
    steps: [{ type: "snapshot", selector: "body" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "context_failed" });
});

test("password input is blocked with a distinct error and never filled", async () => {
  let fillCalls = 0;
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async () => null,
    url: () => "http://fixture.test/",
    getByLabel: () => ({
      getAttribute: async () => "password",
      fill: async () => { fillCalls += 1; },
    }),
  };
  const context = {
    route: async () => undefined,
    routeWebSocket: async () => undefined,
    tracing: { start: async () => undefined, stop: async () => undefined },
    newPage: async () => page,
    close: async () => undefined,
  };
  const runtime = {
    chromium: {
      launch: async () => ({ newContext: async () => context, close: async () => undefined }),
    },
  };
  const result = await runJob({
    version: 1,
    jobId: "password-field-blocked",
    startUrl: "http://fixture.test/",
    allowedOrigins: ["http://fixture.test"],
    timeoutMs: 1_000,
    steps: [{ type: "fill", label: "Contact name", value: "ordinary text" }],
  }, await privateOptions(), testRuntime(runtime) as never);

  expect(result).toMatchObject({ status: "failed", error: "password_field_blocked", stepIndex: 0 });
  expect(fillCalls).toBe(0);
});

function cleanupRuntime({ browserClose = async () => undefined } = {}) {
  const page = {
    video: () => null,
    setDefaultTimeout: () => undefined,
    setDefaultNavigationTimeout: () => undefined,
    goto: async () => undefined,
    locator: () => ({ innerText: async () => "fixture" }),
  };
  const context = {
    route: async () => undefined,
    routeWebSocket: async () => undefined,
    tracing: { start: async () => undefined, stop: async () => undefined },
    newPage: async () => page,
    close: async () => undefined,
  };
  return { chromium: { launch: async () => ({ newContext: async () => context, close: browserClose }) } };
}

const cleanupJob = {
  version: 1,
  jobId: "cleanup-check",
  startUrl: "http://fixture.test/",
  allowedOrigins: ["http://fixture.test"],
  timeoutMs: 1_000,
  steps: [{ type: "snapshot", selector: "body" }],
};

test("browser close failure cannot produce success", async () => {
  const result = await runJob(cleanupJob, await privateOptions(), testRuntime(cleanupRuntime({
    browserClose: async () => { throw new Error("close failed"); },
  })) as never);

  expect(result).toMatchObject({ status: "failed", error: "cleanup_failed" });
  expect(result.artifacts).toHaveProperty("directory");
});

test("artifact deletion failure cannot produce success", async () => {
  const result = await runJob(cleanupJob, await privateOptions({
    removeArtifacts: async () => { throw new Error("delete failed"); },
  }), testRuntime(cleanupRuntime()) as never);

  expect(result).toMatchObject({ status: "failed", error: "cleanup_failed" });
  expect(result.artifacts).toHaveProperty("directory");
});
