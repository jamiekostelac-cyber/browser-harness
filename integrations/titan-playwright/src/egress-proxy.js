import { lookup } from "node:dns/promises";
import { createServer, request as httpRequest } from "node:http";
import net from "node:net";
import { once } from "node:events";

function isIPv4(address) {
  return net.isIPv4(address);
}

function ipv4Number(address) {
  return address.split(".").reduce((value, part) => (value * 256) + Number(part), 0) >>> 0;
}

function inIPv4Range(address, network, prefix) {
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (ipv4Number(address) & mask) === (ipv4Number(network) & mask);
}

export function isPublicIPv4(address) {
  if (!isIPv4(address)) return false;
  const blocked = [
    ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
    ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.0.2.0", 24],
    ["192.88.99.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
    ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
  ];
  return !blocked.some(([network, prefix]) => inIPv4Range(address, network, prefix));
}

function stripHopHeaders(headers) {
  const result = { ...headers };
  const connectionHeaders = String(result.connection ?? "").split(",").map((part) => part.trim().toLowerCase());
  for (const header of [
    "connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
    "keep-alive", "transfer-encoding", ...connectionHeaders,
  ]) delete result[header];
  return result;
}

function parseConnectAuthority(authority) {
  try {
    const url = new URL(`https://${authority}`);
    if (!url.hostname || url.pathname !== "/" || url.search || url.hash || url.username || url.password) return null;
    if (net.isIP(url.hostname) === 6) return null;
    const hostname = url.hostname.toLowerCase();
    const port = Number(url.port || 443);
    return {
      hostname,
      port,
      httpsOrigin: new URL(`https://${hostname}:${port}`).origin,
      httpOrigin: new URL(`http://${hostname}:${port}`).origin,
    };
  } catch {
    return null;
  }
}

function connectPayloadProtocol(data) {
  if (data.length >= 3 && data[0] === 0x16 && data[1] === 0x03 && data[2] >= 0x01 && data[2] <= 0x04) {
    return "https";
  }
  if (data.indexOf("\r\n\r\n") !== -1) return "http";
  return null;
}

function readConnectPrelude(client, initial) {
  return new Promise((resolve, reject) => {
    let data = Buffer.from(initial);
    let timer;
    const finish = (error, result) => {
      clearTimeout(timer);
      client.pause();
      client.off("data", onData);
      client.off("close", onClose);
      if (error) reject(error);
      else resolve(result);
    };
    const onClose = () => finish(new Error("connect_closed"));
    const onData = (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (data.length > 16 * 1024) return finish(new Error("connect_prelude_too_large"));
      const protocol = connectPayloadProtocol(data);
      if (protocol) finish(null, { data, protocol });
    };
    timer = setTimeout(() => finish(new Error("connect_prelude_timeout")), 2_000);
    client.on("data", onData);
    client.once("close", onClose);
    if (data.length > 16 * 1024) return finish(new Error("connect_prelude_too_large"));
    const protocol = connectPayloadProtocol(data);
    if (protocol) finish(null, { data, protocol });
    else client.resume();
  });
}

function validWebSocketPrelude(data, expectedOrigin) {
  const end = data.indexOf("\r\n\r\n");
  if (end < 0) return false;
  const lines = data.subarray(0, end).toString("latin1").split("\r\n");
  const requestLine = /^GET (\S+) HTTP\/1\.[01]$/.exec(lines[0] ?? "");
  if (!requestLine || !requestLine[1].startsWith("/") || requestLine[1].startsWith("//")) return false;
  const headers = new Map();
  for (const line of lines.slice(1)) {
    const separator = line.indexOf(":");
    if (separator <= 0) return false;
    const name = line.slice(0, separator).trim().toLowerCase();
    const value = line.slice(separator + 1).trim();
    headers.set(name, headers.has(name) ? `${headers.get(name)},${value}` : value);
  }
  let hostOrigin;
  let hostUrl;
  try { hostUrl = new URL(`http://${headers.get("host")}`); } catch { return false; }
  if (hostUrl.username || hostUrl.password || hostUrl.pathname !== "/" || hostUrl.search || hostUrl.hash) return false;
  hostOrigin = hostUrl.origin;
  const connection = (headers.get("connection") ?? "").toLowerCase().split(",").map((item) => item.trim());
  return hostOrigin === expectedOrigin && connection.includes("upgrade")
    && (headers.get("upgrade") ?? "").toLowerCase() === "websocket"
    && headers.get("sec-websocket-version") === "13"
    && /^[A-Za-z0-9+/]{22}==$/.test(headers.get("sec-websocket-key") ?? "");
}

export async function createEgressProxy({
  allowedOrigins,
  testOnlyLoopbackOrigins = [],
  lookupAddresses = lookup,
  connect = net.createConnection,
} = {}) {
  const allowed = new Set(allowedOrigins ?? []);
  const testLoopback = new Set(testOnlyLoopbackOrigins);
  const sockets = new Set();
  let blockedRequests = false;
  let closing = false;
  let closePromise;

  async function resolvePublic(hostname, origin) {
    if (testLoopback.has(origin) && hostname === "127.0.0.1") return "127.0.0.1";
    // Literal IP destinations are accepted only when public. Never let a
    // hostname resolve to a mix of public and private addresses.
    if (net.isIP(hostname)) {
      if (isPublicIPv4(hostname)) return hostname;
      throw new Error("blocked");
    }
    if (hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local")
        || hostname.endsWith(".internal") || hostname.endsWith(".test") || hostname.endsWith(".invalid")) {
      throw new Error("blocked");
    }
    const records = await lookupAddresses(hostname, { family: 4, all: true, verbatim: true });
    if (!records.length || records.some(({ address }) => !isPublicIPv4(address))) throw new Error("blocked");
    return records[0].address;
  }

  function forbidden(response) {
    blockedRequests = true;
    response.writeHead(403, {
      "content-length": "0",
      connection: "close",
      "x-titan-browser-blocked": "destination-policy",
    });
    response.end();
  }

  const server = createServer(async (incoming, outgoing) => {
    let url;
    try { url = new URL(incoming.url); } catch { forbidden(outgoing); return; }
    if (url.protocol !== "http:" || !allowed.has(url.origin)) {
      forbidden(outgoing);
      return;
    }
    let address;
    try { address = await resolvePublic(url.hostname.toLowerCase(), url.origin); }
    catch { if (!closing && !outgoing.destroyed) forbidden(outgoing); return; }
    if (closing || incoming.destroyed || outgoing.destroyed) return;
    const headers = stripHopHeaders(incoming.headers);
    headers.host = url.host;
    const upstream = httpRequest({
      hostname: address,
      port: Number(url.port || 80),
      method: incoming.method,
      path: `${url.pathname}${url.search}`,
      headers,
      agent: false,
    }, (response) => {
      const responseHeaders = stripHopHeaders(response.headers);
      outgoing.writeHead(response.statusCode ?? 502, responseHeaders);
      response.pipe(outgoing);
    });
    sockets.add(upstream);
    upstream.on("close", () => sockets.delete(upstream));
    upstream.on("error", () => {
      if (!outgoing.headersSent) forbidden(outgoing);
      else outgoing.destroy();
    });
    incoming.on("aborted", () => upstream.destroy());
    incoming.pipe(upstream);
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });

  server.on("connect", async (incoming, client, head) => {
    const destination = parseConnectAuthority(incoming.url);
    if (!destination || !destination.hostname
        || (!allowed.has(destination.httpsOrigin) && !allowed.has(destination.httpOrigin))) {
      blockedRequests = true;
      client.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      return;
    }
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    let prelude;
    try { prelude = await readConnectPrelude(client, head); }
    catch {
      if (!closing && !client.destroyed) blockedRequests = true;
      client.destroy();
      return;
    }
    const origin = prelude.protocol === "https" ? destination.httpsOrigin : destination.httpOrigin;
    if (!allowed.has(origin)
        || (prelude.protocol === "http" && !validWebSocketPrelude(prelude.data, destination.httpOrigin))) {
      blockedRequests = true;
      client.destroy();
      return;
    }
    let address;
    try { address = await resolvePublic(destination.hostname, origin); }
    catch {
      if (closing || client.destroyed) return;
      blockedRequests = true;
      client.destroy();
      return;
    }
    if (closing || incoming.destroyed || client.destroyed) return;
    const upstream = connect({ host: address, port: destination.port });
    sockets.add(upstream);
    upstream.on("close", () => sockets.delete(upstream));
    upstream.once("error", () => { if (!client.destroyed) client.destroy(); });
    upstream.once("connect", () => {
      if (client.destroyed) { upstream.destroy(); return; }
      upstream.write(prelude.data);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    client.on("error", () => upstream.destroy());
  });

  server.on("upgrade", async (incoming, client, head) => {
    let url;
    try { url = new URL(incoming.url); } catch { blockedRequests = true; client.destroy(); return; }
    const scheme = url.protocol === "ws:" ? "http:" : url.protocol === "wss:" ? "https:" : "";
    const origin = scheme ? `${scheme}//${url.host}` : "";
    if (!origin || !allowed.has(origin) || url.protocol !== "ws:") {
      blockedRequests = true;
      client.destroy();
      return;
    }
    let address;
    try { address = await resolvePublic(url.hostname.toLowerCase(), origin); }
    catch { blockedRequests = true; client.destroy(); return; }
    if (closing || incoming.destroyed || client.destroyed) return;
    const upstream = connect({ host: address, port: Number(url.port || 80) });
    sockets.add(upstream);
    upstream.on("close", () => sockets.delete(upstream));
    upstream.once("error", () => client.destroy());
    upstream.once("connect", () => {
      const headers = stripHopHeaders(incoming.headers);
      headers.host = url.host;
      headers.connection = "Upgrade";
      headers.upgrade = "websocket";
      const requestLines = [`${incoming.method} ${url.pathname}${url.search} HTTP/${incoming.httpVersion}`];
      for (const [name, value] of Object.entries(headers)) {
        if (Array.isArray(value)) value.forEach((item) => requestLines.push(`${name}: ${item}`));
        else if (value !== undefined) requestLines.push(`${name}: ${value}`);
      }
      upstream.write(`${requestLines.join("\r\n")}\r\n\r\n`);
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    client.on("error", () => upstream.destroy());
  });

  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("proxy_listen_failed");
  return {
    server,
    url: `http://127.0.0.1:${address.port}`,
    async close() {
      if (closePromise) return closePromise;
      closing = true;
      closePromise = new Promise((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        for (const socket of sockets) socket.destroy();
      });
      return closePromise;
    },
    wasBlocked() { return blockedRequests; },
  };
}
