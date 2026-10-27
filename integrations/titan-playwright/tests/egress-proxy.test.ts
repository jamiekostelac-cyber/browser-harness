import { createServer, request as httpRequest, type Server } from "node:http";
import net from "node:net";
import { once } from "node:events";
import { test, expect } from "@playwright/test";
import { createEgressProxy } from "../src/egress-proxy.js";

async function listen(server: Server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fixture server failed to bind");
  return address.port;
}

function proxyGet(proxyUrl: string, destination: string) {
  const proxy = new URL(proxyUrl);
  return new Promise<{ status: number; headers: Record<string, string | string[] | undefined>; body: string }>((resolve, reject) => {
    const request = httpRequest({
      hostname: proxy.hostname,
      port: Number(proxy.port),
      path: destination,
      headers: { host: new URL(destination).host },
    }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => resolve({
        status: response.statusCode ?? 0,
        headers: response.headers,
        body,
      }));
    });
    request.on("error", reject);
    request.end();
  });
}

test("HTTP proxy enforces exact scheme, hostname, and port on each request", async () => {
  const target: Server = createServer((_request, response) => response.end("allowed"));
  const port = await listen(target);
  const origin = `http://127.0.0.1:${port}`;
  const proxy = await createEgressProxy({
    allowedOrigins: [origin],
    testOnlyLoopbackOrigins: [origin],
  });
  try {
    const allowed = await proxyGet(proxy.url, `${origin}/path`);
    expect(allowed).toMatchObject({ status: 200, body: "allowed" });
    const wrongPort = await proxyGet(proxy.url, `http://127.0.0.1:${port + 1}/path`);
    expect(wrongPort.status).toBe(403);
    expect(wrongPort.headers["x-titan-browser-blocked"]).toBe("destination-policy");
  } finally {
    const closing = once(target, "close");
    target.close();
    await Promise.all([closing, proxy.close()]);
  }
});

test("proxy rejects mixed public and private DNS answers without connecting", async () => {
  let connectCalls = 0;
  const proxy = await createEgressProxy({
    allowedOrigins: ["http://public.example"],
    lookupAddresses: async () => [{ address: "8.8.8.8" }, { address: "169.254.169.254" }],
    connect: (...args: Parameters<typeof net.createConnection>) => {
      connectCalls += 1;
      return net.createConnection(...args);
    },
  });
  try {
    const result = await proxyGet(proxy.url, "http://public.example/path");
    expect(result.status).toBe(403);
    expect(connectCalls).toBe(0);
  } finally {
    await proxy.close();
  }
});

test("proxy close prevents HTTP, CONNECT, and WebSocket DNS lookups from opening late connections", async () => {
  for (const mode of ["http", "connect", "websocket"] as const) {
    let lookupStarted!: () => void;
    let releaseLookup!: (records: Array<{ address: string }>) => void;
    let connectCalls = 0;
    const started = new Promise<void>((resolve) => { lookupStarted = resolve; });
    const pendingAnswers = new Promise<Array<{ address: string }>>((resolve) => { releaseLookup = resolve; });
    const origin = mode === "connect" ? "https://delayed.example" : "http://delayed.example";
    const proxy = await createEgressProxy({
      allowedOrigins: [origin],
      lookupAddresses: async () => { lookupStarted(); return pendingAnswers; },
      connect: (...args: Parameters<typeof net.createConnection>) => {
        connectCalls += 1;
        return net.createConnection(...args);
      },
    });
    let pendingTraffic: Promise<unknown> = Promise.resolve();
    if (mode === "http") {
      pendingTraffic = proxyGet(proxy.url, "http://delayed.example/path").catch(() => null);
    } else if (mode === "connect") {
      const proxyAddress = new URL(proxy.url);
      pendingTraffic = new Promise<void>((resolve, reject) => {
        const request = httpRequest({
          hostname: proxyAddress.hostname,
          port: Number(proxyAddress.port),
          method: "CONNECT",
          path: "delayed.example:443",
        });
        request.on("connect", (_response, socket) => { socket.write(Buffer.from([0x16, 0x03, 0x01, 0x00, 0x00])); resolve(); });
        request.on("error", reject);
        request.end();
      }).catch(() => null);
      await pendingTraffic;
    } else {
      const proxyAddress = new URL(proxy.url);
      const socket = net.createConnection({ host: proxyAddress.hostname, port: Number(proxyAddress.port) }, () => {
        socket.write([
          "GET ws://delayed.example/socket HTTP/1.1",
          "Host: delayed.example",
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
          "\r\n",
        ].join("\r\n"));
      });
      socket.on("error", () => undefined);
      pendingTraffic = new Promise<void>((resolve) => socket.once("close", resolve));
    }
    await started;
    await proxy.close();
    releaseLookup([{ address: "8.8.8.8" }]);
    await pendingTraffic;
    await new Promise((resolve) => setImmediate(resolve));
    expect(connectCalls, mode).toBe(0);
    await proxy.close();
  }
});

test("HTTPS CONNECT is restricted to the exact approved host and port", async () => {
  let tlsDataResolve!: () => void;
  const tlsData = new Promise<void>((resolve) => { tlsDataResolve = resolve; });
  const target = net.createServer((socket) => {
    socket.once("data", () => tlsDataResolve());
    socket.on("error", () => undefined);
  });
  const localPort = await new Promise<number>((resolve) => target.listen(0, "127.0.0.1", () => {
    const address = target.address();
    if (!address || typeof address === "string") throw new Error("fixture TCP server failed to bind");
    resolve(address.port);
  }));
  const connectCalls: Array<{ host: string; port: number }> = [];
  const proxy = await createEgressProxy({
    allowedOrigins: ["https://public.example"],
    lookupAddresses: async () => [{ address: "8.8.8.8" }],
    connect: (options) => {
      connectCalls.push({ host: String(options.host), port: Number(options.port) });
      return net.createConnection({ host: "127.0.0.1", port: localPort });
    },
  });
  try {
    const proxyAddress = new URL(proxy.url);
    const result = await new Promise<{ status: number; socket: net.Socket }>((resolve, reject) => {
      const request = httpRequest({
        hostname: proxyAddress.hostname,
        port: Number(proxyAddress.port),
        method: "CONNECT",
        path: "public.example:443",
      });
      request.on("connect", (response, socket) => resolve({ status: response.statusCode ?? 0, socket }));
      request.on("error", reject);
      request.end();
    });
    expect(result.status).toBe(200);
    result.socket.write(Buffer.from([0x16, 0x03, 0x01, 0x00, 0x00]));
    await tlsData;
    expect(connectCalls).toEqual([{ host: "8.8.8.8", port: 443 }]);
    result.socket.destroy();

    const denied = await new Promise<number>((resolve, reject) => {
      const request = httpRequest({
        hostname: proxyAddress.hostname,
        port: Number(proxyAddress.port),
        method: "CONNECT",
        path: "public.example:8443",
      });
      request.on("connect", (response, socket) => { socket.destroy(); resolve(response.statusCode ?? 0); });
      request.on("response", (response) => { response.resume(); resolve(response.statusCode ?? 0); });
      request.on("error", reject);
      request.end();
    });
    expect(denied).toBe(403);
    expect(connectCalls).toHaveLength(1);
  } finally {
    const closing = once(target, "close");
    target.close();
    await Promise.all([closing, proxy.close()]);
  }
});

test("CONNECT preserves ClientHello bytes received while DNS is pending", async () => {
  const expected = Buffer.from([0x16, 0x03, 0x01, 0x00, 0x0a, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a]);
  let releaseLookup!: (records: Array<{ address: string }>) => void;
  let lookupStartedResolve!: () => void;
  let payloadResolve!: () => void;
  const pendingLookup = new Promise<Array<{ address: string }>>((resolve) => { releaseLookup = resolve; });
  const lookupStarted = new Promise<void>((resolve) => { lookupStartedResolve = resolve; });
  const payloadReceived = new Promise<void>((resolve) => { payloadResolve = resolve; });
  let delivered = Buffer.alloc(0);
  const target = net.createServer((socket) => {
    socket.on("data", (chunk) => {
      delivered = Buffer.concat([delivered, chunk]);
      if (delivered.length >= expected.length) payloadResolve();
    });
  });
  const localPort = await listen(target);
  const proxy = await createEgressProxy({
    allowedOrigins: ["https://public.example"],
    lookupAddresses: async () => { lookupStartedResolve(); return pendingLookup; },
    connect: () => net.createConnection({ host: "127.0.0.1", port: localPort }),
  });
  let clientSocket: net.Socket | undefined;
  try {
    const proxyAddress = new URL(proxy.url);
    clientSocket = await new Promise<net.Socket>((resolve, reject) => {
      const request = httpRequest({
        hostname: proxyAddress.hostname,
        port: Number(proxyAddress.port),
        method: "CONNECT",
        path: "public.example:443",
      });
      request.on("connect", (response, socket) => {
        if (response.statusCode !== 200) {
          socket.destroy();
          reject(new Error(`unexpected CONNECT status ${response.statusCode}`));
          return;
        }
        resolve(socket);
      });
      request.on("error", reject);
      request.end();
    });

    clientSocket.write(expected.subarray(0, 3));
    await lookupStarted;
    await new Promise<void>((resolve, reject) => {
      clientSocket?.write(expected.subarray(3), (error) => error ? reject(error) : resolve());
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    releaseLookup([{ address: "8.8.8.8" }]);
    let payloadTimer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        payloadReceived,
        new Promise<never>((_resolve, reject) => {
          payloadTimer = setTimeout(() => reject(new Error("ClientHello was not forwarded")), 1_000);
        }),
      ]);
    } finally {
      if (payloadTimer) clearTimeout(payloadTimer);
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 25));
    expect(delivered).toEqual(expected);
  } finally {
    clientSocket?.destroy();
    const closing = once(target, "close");
    target.close();
    await Promise.all([closing, proxy.close()]);
  }
});

test("CONNECT rejects HTTPS on an HTTP allowlist and malformed cleartext on HTTPS", async () => {
  const attempts: Array<{ allowed: string; authority: string; payload: Buffer }> = [
    {
      allowed: "http://public.example",
      authority: "public.example:80",
      payload: Buffer.from([0x16, 0x03, 0x01, 0x00, 0x00]),
    },
    {
      allowed: "https://public.example",
      authority: "public.example:443",
      payload: Buffer.from("GET / HTTP/1.1\r\nHost: public.example\r\n\r\n"),
    },
  ];
  for (const attempt of attempts) {
    let connectCalls = 0;
    const proxy = await createEgressProxy({
      allowedOrigins: [attempt.allowed],
      lookupAddresses: async () => [{ address: "8.8.8.8" }],
      connect: () => { connectCalls += 1; return new net.Socket(); },
    });
    try {
      const proxyAddress = new URL(proxy.url);
      await new Promise<void>((resolve, reject) => {
        const request = httpRequest({
          hostname: proxyAddress.hostname,
          port: Number(proxyAddress.port),
          method: "CONNECT",
          path: attempt.authority,
        });
        const timer = setTimeout(() => reject(new Error("CONNECT did not close")), 1_000);
        request.on("connect", (_response, socket) => {
          socket.once("close", () => { clearTimeout(timer); resolve(); });
          socket.write(attempt.payload);
        });
        request.on("response", (response) => {
          response.resume();
          clearTimeout(timer);
          reject(new Error(`unexpected status ${response.statusCode}`));
        });
        request.on("error", (error) => { clearTimeout(timer); reject(error); });
        request.end();
      });
      expect(connectCalls).toBe(0);
      expect(proxy.wasBlocked()).toBe(true);
    } finally {
      await proxy.close();
    }
  }
});

test("WebSocket HTTP upgrade is checked and tunneled through the proxy", async () => {
  let received = "";
  const target = net.createServer((socket) => {
    socket.once("data", (data) => {
      received = data.toString("utf8");
      socket.end("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n");
    });
  });
  const localPort = await new Promise<number>((resolve) => target.listen(0, "127.0.0.1", () => {
    const address = target.address();
    if (!address || typeof address === "string") throw new Error("fixture TCP server failed to bind");
    resolve(address.port);
  }));
  const proxy = await createEgressProxy({
    allowedOrigins: ["http://public.example"],
    lookupAddresses: async () => [{ address: "8.8.8.8" }],
    connect: () => net.createConnection({ host: "127.0.0.1", port: localPort }),
  });
  try {
    const proxyAddress = new URL(proxy.url);
    const result = await new Promise<string>((resolve, reject) => {
      const client = net.createConnection({ host: proxyAddress.hostname, port: Number(proxyAddress.port) }, () => {
        client.write([
          "GET ws://public.example/socket HTTP/1.1",
          "Host: public.example",
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
          "\r\n",
        ].join("\r\n"));
      });
      let response = "";
      client.on("data", (chunk) => { response += chunk.toString("utf8"); });
      client.on("end", () => resolve(response));
      client.on("error", reject);
      setTimeout(() => client.destroy(), 500);
    });
    expect(received).toContain("GET /socket HTTP/1.1");
    expect(result).toContain("101 Switching Protocols");
  } finally {
    const closing = once(target, "close");
    target.close();
    await Promise.all([closing, proxy.close()]);
  }
});
