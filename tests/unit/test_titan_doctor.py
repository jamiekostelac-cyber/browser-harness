import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from browser_harness import titan_doctor


def _version_response(monkeypatch, *, websocket_url="ws://127.0.0.1:9333/devtools/browser/secret-id"):
    response = MagicMock()
    response.read.return_value = json.dumps(
        {"webSocketDebuggerUrl": websocket_url, "Browser": "Chrome/155.0.0.0"}
    ).encode()
    response.__enter__.return_value = response
    monkeypatch.setattr(titan_doctor, "_open_version_endpoint", lambda *_a, **_kw: response)
    return response


def _fake_cdp(monkeypatch, reply):
    websocket = MagicMock()
    websocket.recv.return_value = json.dumps(reply)
    manager = MagicMock()
    manager.__enter__.return_value = websocket
    monkeypatch.setattr("websockets.sync.client.connect", lambda *_a, **_kw: manager)
    return websocket


def test_diagnose_requires_loopback_before_network(monkeypatch):
    open_url = MagicMock()
    monkeypatch.setattr(titan_doctor, "_open_version_endpoint", open_url)

    result = titan_doctor.diagnose("http://example.com:9333")

    assert result["status"] == "INVALID_ENDPOINT"
    assert result["auth"] == "AUTH_NOT_CHECKED"
    open_url.assert_not_called()


def test_diagnose_rejects_credentials_and_url_paths():
    assert titan_doctor.diagnose("http://user:pass@127.0.0.1:9333")["status"] == "INVALID_ENDPOINT"
    assert titan_doctor.diagnose("http://127.0.0.1:9333/json")["status"] == "INVALID_ENDPOINT"
    assert titan_doctor.diagnose("http://[::1")["status"] == "INVALID_ENDPOINT"


def test_diagnose_validates_json_version_and_probes_browser_get_version(monkeypatch):
    response = _version_response(monkeypatch)
    websocket = _fake_cdp(
        monkeypatch,
        {"id": 1, "result": {"product": "Chrome/155.0.0.0", "protocolVersion": "1.3"}},
    )

    result = titan_doctor.diagnose()

    assert result == {
        "status": "READY",
        "endpoint": "http://127.0.0.1:9333",
        "auth": "AUTH_NOT_CHECKED",
        "browser": "Chrome/155.0.0.0",
        "protocol": "1.3",
    }
    response.read.assert_called_once_with(titan_doctor.MAX_VERSION_BYTES + 1)
    websocket.send.assert_called_once_with('{"id": 1, "method": "Browser.getVersion"}')
    websocket.recv.assert_called_once_with(timeout=titan_doctor.WS_TIMEOUT_SECONDS)


def test_diagnose_redacts_debugger_identifier(monkeypatch):
    _version_response(monkeypatch, websocket_url="ws://127.0.0.1:9333/devtools/browser/private-id")
    _fake_cdp(monkeypatch, {"id": 2, "result": {}})

    result = titan_doctor.diagnose()

    assert result["status"] == "CDP_ERROR"
    assert "private-id" not in json.dumps(result)
    assert "webSocketDebuggerUrl" not in json.dumps(result)


def test_diagnose_rejects_untrusted_browser_metadata(monkeypatch):
    _version_response(monkeypatch)
    _fake_cdp(monkeypatch, {"id": 1, "result": {"product": "Chrome/1\nInjected", "protocolVersion": "1.3"}})

    result = titan_doctor.diagnose()

    assert result["status"] == "CDP_ERROR"
    assert result["error"] == "browser_metadata_invalid"
    assert "Injected" not in json.dumps(result)


def test_diagnose_rejects_non_loopback_debugger_endpoint(monkeypatch):
    _version_response(monkeypatch, websocket_url="ws://192.168.1.4:9333/devtools/browser/private-id")
    connect = MagicMock()
    monkeypatch.setattr("websockets.sync.client.connect", connect)

    result = titan_doctor.diagnose()

    assert result["status"] == "INVALID_CDP_RESPONSE"
    assert "private-id" not in json.dumps(result)
    connect.assert_not_called()


def test_diagnose_rejects_malformed_debugger_url(monkeypatch):
    _version_response(monkeypatch, websocket_url="ws://[::1/devtools/browser/id")
    connect = MagicMock()
    monkeypatch.setattr("websockets.sync.client.connect", connect)

    result = titan_doctor.diagnose()

    assert result["status"] == "INVALID_CDP_RESPONSE"
    assert result["auth"] == "AUTH_NOT_CHECKED"
    connect.assert_not_called()


def test_diagnose_classifies_unreachable_endpoint(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("Connection refused")

    monkeypatch.setattr(titan_doctor, "_open_version_endpoint", fail)
    result = titan_doctor.diagnose()
    assert result["status"] == "TRANSPORT_ERROR"
    assert result["error"] == "version_endpoint_unreachable"


def test_diagnose_classifies_http_rejection_as_reachable_endpoint(monkeypatch):
    def reject(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1:9333/json/version", 404, "Not Found", {}, None)

    monkeypatch.setattr(titan_doctor, "_open_version_endpoint", reject)

    result = titan_doctor.diagnose()

    assert result["status"] == "ENDPOINT_REJECTED"
    assert result["auth"] == "AUTH_NOT_CHECKED"
    assert result["error"] == "version_endpoint_http_error"


def test_http_redirects_are_never_followed():
    handler = titan_doctor._NoRedirect()
    request = object()
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://example.com") is None


def test_version_probe_enforces_total_deadline_across_trickling_body(monkeypatch):
    clock = {"now": 0.0}

    class FakeSocket:
        def __init__(self): self.calls = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def sendall(self, _data): pass
        def settimeout(self, timeout): self.timeout = timeout
        def recv(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n"
            clock["now"] = 2.1
            return b"x"

    monkeypatch.setattr(titan_doctor.socket, "create_connection", lambda *_a, **_kw: FakeSocket())
    monkeypatch.setattr(titan_doctor.time, "monotonic", lambda: clock["now"])

    with pytest.raises(TimeoutError):
        titan_doctor._open_version_endpoint(
            titan_doctor.urllib.request.Request("http://127.0.0.1:9333/json/version")
        )


def test_version_probe_caps_unterminated_http_lines(monkeypatch):
    class UnterminatedSocket:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def sendall(self, _data): pass
        def settimeout(self, _timeout): pass
        def recv(self, size): return b"x" * size

    monkeypatch.setattr(titan_doctor.socket, "create_connection", lambda *_a, **_kw: UnterminatedSocket())

    with pytest.raises(ValueError, match="HTTP line too large"):
        titan_doctor._open_version_endpoint(
            titan_doctor.urllib.request.Request("http://127.0.0.1:9333/json/version")
        )


def test_cli_json_always_marks_auth_unchecked(monkeypatch, capsys):
    monkeypatch.setattr(
        titan_doctor,
        "diagnose",
        lambda endpoint: titan_doctor._result("READY", endpoint, browser="Chrome/1", protocol="1.3"),
    )

    assert titan_doctor.run_titan_doctor_cli(["--endpoint", "http://127.0.0.1:9333", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["auth"] == "AUTH_NOT_CHECKED"
    assert output["status"] == "READY"
