"""Read-only, bounded diagnostics for Titan's dedicated local Chrome endpoint."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:9333"
HTTP_TIMEOUT_SECONDS = 2.0
WS_TIMEOUT_SECONDS = 2.0
MAX_VERSION_BYTES = 32 * 1024
MAX_CDP_BYTES = 32 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_version_endpoint(request):
    parsed = urllib.parse.urlsplit(request.full_url)
    host = parsed.hostname
    port = parsed.port or 80
    if host == "localhost":
        host = "127.0.0.1"
    deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS
    with socket.create_connection((host, port), timeout=HTTP_TIMEOUT_SECONDS) as connection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("version probe deadline exceeded")
        connection.settimeout(remaining)
        connection.sendall(
            f"GET /json/version HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
            "Accept: application/json\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        if time.monotonic() > deadline:
            raise TimeoutError("version probe deadline exceeded")
        buffer = bytearray()
        eof = False

        def receive_more(buffer_limit: int = MAX_VERSION_BYTES + 16 * 1024 + 1) -> None:
            nonlocal eof
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("version probe deadline exceeded")
            connection.settimeout(remaining)
            recv_size = min(4096, max(1, buffer_limit + 1 - len(buffer)))
            chunk = connection.recv(recv_size)
            if time.monotonic() > deadline:
                raise TimeoutError("version probe deadline exceeded")
            if chunk:
                buffer.extend(chunk)
            else:
                eof = True

        def read_line(limit: int) -> bytes:
            while b"\n" not in buffer and not eof:
                if len(buffer) > limit:
                    raise ValueError("HTTP line too large")
                receive_more(limit)
                if b"\n" not in buffer and len(buffer) > limit:
                    raise ValueError("HTTP line too large")
            newline = buffer.find(b"\n")
            if newline < 0:
                line = bytes(buffer)
                buffer.clear()
            else:
                line = bytes(buffer[:newline + 1])
                del buffer[:newline + 1]
            if len(line) > limit:
                raise ValueError("HTTP line too large")
            return line

        def read_bytes(size: int) -> bytes:
            while len(buffer) < size and not eof:
                receive_more()
            chunk = bytes(buffer[:size])
            del buffer[:size]
            return chunk

        status_line = read_line(4096)
        parts = status_line.decode("ascii", "strict").strip().split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("invalid HTTP status")
        status = int(parts[1])
        headers = {}
        header_bytes = len(status_line)
        while True:
            line = read_line(8192)
            header_bytes += len(line)
            if header_bytes > 16 * 1024:
                raise ValueError("HTTP headers too large")
            if line in (b"\r\n", b"\n", b""):
                break
            name, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                raise ValueError("invalid HTTP header")
            headers[name.strip().lower()] = value.strip().lower()

        if status < 200 or status >= 300:
            raise urllib.error.HTTPError(request.full_url, status, "HTTP endpoint rejected request", headers, None)

        length = headers.get("content-length")
        if "chunked" in headers.get("transfer-encoding", ""):
            chunks = []
            total = 0
            while True:
                size_line = read_line(128).split(b";", 1)[0].strip()
                try:
                    chunk_size = int(size_line, 16)
                except ValueError as exc:
                    raise ValueError("invalid chunked body") from exc
                if chunk_size == 0:
                    while read_line(8192) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += chunk_size
                if total > MAX_VERSION_BYTES:
                    raise ValueError("version response too large")
                chunk = read_bytes(chunk_size)
                if len(chunk) != chunk_size or read_bytes(2) != b"\r\n":
                    raise ValueError("truncated chunked body")
                chunks.append(chunk)
            body = b"".join(chunks)
        elif length is not None:
            if not length.isdigit() or int(length) > MAX_VERSION_BYTES:
                raise ValueError("version response too large")
            remaining_bytes = int(length)
            chunks = []
            while remaining_bytes:
                chunk = read_bytes(min(4096, remaining_bytes))
                if not chunk:
                    raise ValueError("truncated HTTP body")
                chunks.append(chunk)
                remaining_bytes -= len(chunk)
            body = b"".join(chunks)
        else:
            chunks = []
            total = 0
            while total <= MAX_VERSION_BYTES:
                if not buffer and not eof:
                    receive_more()
                if not buffer:
                    break
                chunk = read_bytes(min(4096, MAX_VERSION_BYTES + 1 - total))
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
        return _MemoryResponse(body)


class _MemoryResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        body, self._body = self._body[:size], self._body[size:]
        return body


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError("invalid endpoint URL") from exc
    if parsed.scheme != "http" or not parsed.hostname or not _loopback_host(parsed.hostname):
        raise ValueError("endpoint must be an http loopback URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint credentials are not allowed")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("endpoint must contain only scheme, host, and port")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ValueError("invalid endpoint port") from exc
    if port < 1:
        raise ValueError("invalid endpoint port")
    host = parsed.hostname.lower()
    authority_host = f"[{host}]" if ":" in host else host
    return f"http://{authority_host}:{port}", port


def _valid_debugger_url(value: object, port: int) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "ws" or not parsed.hostname or not _loopback_host(parsed.hostname):
        return None
    try:
        debugger_port = parsed.port or 80
    except ValueError:
        return None
    if debugger_port != port or parsed.username is not None or parsed.password is not None:
        return None
    if not parsed.path.startswith("/devtools/browser/") or parsed.query or parsed.fragment:
        return None
    return value


def _result(status: str, endpoint: str | None, **values) -> dict:
    return {
        "status": status,
        "endpoint": endpoint,
        "auth": "AUTH_NOT_CHECKED",
        **values,
    }


def diagnose(endpoint: str = DEFAULT_ENDPOINT) -> dict:
    """Probe Chrome's version endpoint and one bounded Browser.getVersion call."""
    try:
        normalized, port = _parse_endpoint(endpoint)
    except ValueError:
        return _result("INVALID_ENDPOINT", None)

    request = urllib.request.Request(
        normalized + "/json/version",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with _open_version_endpoint(request) as response:
            body = response.read(MAX_VERSION_BYTES + 1)
        if len(body) > MAX_VERSION_BYTES:
            return _result("INVALID_CDP_RESPONSE", normalized, error="version_response_too_large")
        version_info = json.loads(body)
    except urllib.error.HTTPError:
        # A response proves the endpoint was reached, but it rejected the
        # version request. Do not probe any additional paths to diagnose it.
        return _result("ENDPOINT_REJECTED", normalized, error="version_endpoint_http_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        return _result("TRANSPORT_ERROR", normalized, error="version_endpoint_unreachable")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _result("INVALID_CDP_RESPONSE", normalized, error="version_response_invalid")

    if not isinstance(version_info, dict):
        return _result("INVALID_CDP_RESPONSE", normalized, error="version_response_invalid")
    websocket_url = _valid_debugger_url(version_info.get("webSocketDebuggerUrl"), port)
    if websocket_url is None:
        return _result("INVALID_CDP_RESPONSE", normalized, error="debugger_endpoint_invalid")

    try:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        return _result("CDP_ERROR", normalized, error="cdp_client_unavailable")

    try:
        with connect(
            websocket_url,
            open_timeout=WS_TIMEOUT_SECONDS,
            close_timeout=1,
            max_size=MAX_CDP_BYTES,
            ping_interval=None,
            proxy=None,
        ) as websocket:
            websocket.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            raw = websocket.recv(timeout=WS_TIMEOUT_SECONDS)
        cdp_result = json.loads(raw)
    except (WebSocketException, OSError, TimeoutError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
            return _result("CDP_ERROR", normalized, error="cdp_response_invalid")
        error = "cdp_timeout" if "Timeout" in type(exc).__name__ else "cdp_connection_failed"
        return _result("CDP_ERROR", normalized, error=error)

    if not isinstance(cdp_result, dict) or cdp_result.get("id") != 1:
        return _result("CDP_ERROR", normalized, error="cdp_response_invalid")
    version = cdp_result.get("result")
    if not isinstance(version, dict):
        return _result("CDP_ERROR", normalized, error="cdp_method_failed")
    product = version.get("product")
    if not isinstance(product, str) or not re.fullmatch(
        r"(?:Chrome|Chromium|HeadlessChrome)/[A-Za-z0-9.]{1,40}", product
    ):
        return _result("CDP_ERROR", normalized, error="browser_metadata_invalid")
    protocol = version.get("protocolVersion")
    if not isinstance(protocol, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}", protocol):
        return _result("CDP_ERROR", normalized, error="browser_metadata_invalid")
    return _result(
        "READY",
        normalized,
        browser=product,
        protocol=protocol,
    )


def run_titan_doctor_cli(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="browser-harness titan-doctor")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args)
    result = diagnose(parsed.endpoint)
    if parsed.json:
        print(json.dumps(result, sort_keys=True))
    elif result["status"] == "READY":
        print(f"TITAN_CHROME_READY {result['browser']} AUTH_NOT_CHECKED")
    else:
        print(f"TITAN_CHROME_{result['status']} AUTH_NOT_CHECKED: {result.get('error', 'endpoint rejected')}")
    return 0 if result["status"] == "READY" else 1
