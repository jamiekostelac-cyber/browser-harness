"""macOS TCC blocks reading the browser profile dir (EPERM), where
DevToolsActivePort lives. get_ws_url() must not crash on that: it falls back to a
dedicated automation Chrome, and fails with actionable guidance when it can't.
No real browser is launched."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from browser_harness import daemon


class _BlockedProfile:
    """A profile dir the OS refuses to read (macOS TCC / EPERM)."""

    def __truediv__(self, _name):
        return self

    def read_text(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    def exists(self):
        return True

    def __str__(self):
        return "/Library/Application Support/Google/Chrome"


@pytest.fixture
def blocked_profile(monkeypatch):
    monkeypatch.setattr(daemon, "PROFILES", [_BlockedProfile()])
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.setattr(daemon, "REMOTE_ID", None)

    def refused(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(daemon.urllib.request, "urlopen", refused)
    monkeypatch.setattr(daemon.platform, "system", lambda: "Darwin")


def test_blocked_profile_falls_back_to_automation_chrome(blocked_profile, monkeypatch):
    endpoint = "ws://127.0.0.1:49231/devtools/browser/auto"
    called = False

    def launch_and_record():
        nonlocal called
        called = True
        return endpoint

    monkeypatch.setattr(daemon, "launch_automation_chrome", launch_and_record)
    assert daemon.get_ws_url() == endpoint
    assert called


def test_blocked_profile_without_browser_raises_actionable_error(blocked_profile, monkeypatch):
    monkeypatch.setattr(daemon, "launch_automation_chrome", lambda: None)
    monkeypatch.setattr(daemon, "remote_debugging_user_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="Full Disk Access"):
        daemon.get_ws_url()


def test_blocked_profile_with_missing_profile_falls_back(blocked_profile, monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROFILES", [_BlockedProfile(), tmp_path / "missing-profile"])
    endpoint = "ws://127.0.0.1:49231/devtools/browser/auto"
    monkeypatch.setattr(daemon, "supported_browser_running", lambda: True)
    monkeypatch.setattr(daemon, "NO_TOGGLE_GRACE", -1)
    launch = []
    monkeypatch.setattr(daemon, "launch_automation_chrome", lambda: launch.append(True) or endpoint)

    assert daemon.get_ws_url() == endpoint
    assert launch == [True]


def test_one_readable_profile_prevents_tcc_fallback(blocked_profile, monkeypatch, tmp_path):
    readable = tmp_path / "readable-profile"
    readable.mkdir()
    monkeypatch.setattr(daemon, "PROFILES", [_BlockedProfile(), readable])
    monkeypatch.setattr(daemon, "NO_TOGGLE_GRACE", -1)
    monkeypatch.setattr(daemon, "supported_browser_running", lambda: False)
    launch = []
    monkeypatch.setattr(daemon, "launch_automation_chrome", lambda: launch.append(True))

    with pytest.raises(RuntimeError, match="chrome-not-running"):
        daemon.get_ws_url()
    assert launch == []


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_permission_error_on_non_macos_uses_normal_profile_error(
    blocked_profile, monkeypatch, system
):
    monkeypatch.setattr(daemon.platform, "system", lambda: system)
    monkeypatch.setattr(daemon, "supported_browser_running", lambda: True)
    monkeypatch.setattr(daemon, "NO_TOGGLE_GRACE", -1)
    monkeypatch.setattr(daemon, "remote_debugging_user_enabled", lambda: None)
    launch = []
    monkeypatch.setattr(daemon, "launch_automation_chrome", lambda: launch.append(True))

    with pytest.raises(RuntimeError, match="DevToolsActivePort not found") as exc:
        daemon.get_ws_url()

    assert "Full Disk Access" not in str(exc.value)
    assert launch == []


def test_automation_profile_rediscovers_selected_port_after_restart(monkeypatch, tmp_path):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("49231\n/devtools/browser/persisted\n")
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "browser_running_for_profile", lambda path: path == profile)
    monkeypatch.setattr(
        daemon,
        "_json_version_ws",
        lambda port: f"ws://127.0.0.1:{port}/devtools/browser/persisted" if port == 49231 else None,
    )

    def unexpected_launch():
        pytest.fail("a live automation profile should be reused")

    monkeypatch.setattr(daemon, "_automation_chrome_binary", unexpected_launch)
    monkeypatch.setattr(daemon, "_profile_process_owns", lambda _profile: True)
    assert daemon.launch_automation_chrome() == "ws://127.0.0.1:49231/devtools/browser/persisted"


@pytest.mark.parametrize(
    ("response_path", "accepted"),
    [
        ("/devtools/browser/profile-owner", True),
        ("/devtools/browser/unrelated-owner", False),
    ],
)
def test_json_version_reuse_requires_profile_endpoint_identity(
    monkeypatch, tmp_path, response_path, accepted
):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "49231\n/devtools/browser/profile-owner\n"
    )
    monkeypatch.setattr(daemon, "PROFILES", [profile])
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "supported_browser_running", lambda: True)
    monkeypatch.setattr(daemon, "NO_TOGGLE_GRACE", -1)
    monkeypatch.setattr(daemon, "remote_debugging_user_enabled", lambda: None)
    response = MagicMock()
    response.read.return_value = (
        '{"webSocketDebuggerUrl": '
        f'"ws://127.0.0.1:49231{response_path}"}}'
    ).encode()
    response.__enter__.return_value = response
    monkeypatch.setattr(daemon.urllib.request, "urlopen", lambda *_a, **_k: response)

    if accepted:
        assert daemon.get_ws_url() == f"ws://127.0.0.1:49231{response_path}"
    else:
        with pytest.raises(RuntimeError, match="DevToolsActivePort not found"):
            daemon.get_ws_url()


@pytest.mark.parametrize("process_owns_profile", [True, False])
def test_json_version_404_fallback_requires_profile_process_identity(
    monkeypatch, tmp_path, process_owns_profile
):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "49231\n/devtools/browser/profile-owner\n"
    )
    monkeypatch.setattr(daemon, "PROFILES", [profile])
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "supported_browser_running", lambda: True)
    monkeypatch.setattr(daemon, "NO_TOGGLE_GRACE", -1)
    monkeypatch.setattr(daemon, "remote_debugging_user_enabled", lambda: None)
    monkeypatch.setattr(
        daemon, "_profile_process_owns", lambda base: base == profile and process_owns_profile
    )

    def not_found(*_args, **_kwargs):
        raise daemon.urllib.error.HTTPError(
            "http://127.0.0.1:49231/json/version", 404, "Not Found", {}, None
        )

    monkeypatch.setattr(daemon.urllib.request, "urlopen", not_found)

    if process_owns_profile:
        assert daemon.get_ws_url() == (
            "ws://127.0.0.1:49231/devtools/browser/profile-owner"
        )
    else:
        with pytest.raises(RuntimeError, match="DevToolsActivePort not found"):
            daemon.get_ws_url()


def test_stale_automation_port_does_not_attach_to_unrelated_listener(monkeypatch, tmp_path):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("49231\n/devtools/browser/stale\n")
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_profile_process_owns", lambda _path: False)
    monkeypatch.setattr(daemon, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: None)

    assert daemon.launch_automation_chrome() is None


def test_live_automation_profile_with_unverifiable_endpoint_is_not_reused(
    monkeypatch, tmp_path
):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("49231\n/devtools/browser/stale\n")
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_profile_process_owns", lambda _path: True)
    monkeypatch.setattr(daemon, "_json_version_ws", lambda _port: None)
    monkeypatch.setattr(daemon, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: None)

    assert daemon.launch_automation_chrome() is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://example.com:49231/devtools/browser/profile-owner",
        "ws://192.168.1.2:49231/devtools/browser/profile-owner",
        "ws://127.0.0.1:49232/devtools/browser/profile-owner",
        "ws://127.0.0.1:49231/devtools/browser/foreign-profile",
        "wss://127.0.0.1:49231/devtools/browser/profile-owner",
    ],
)
def test_automation_reuse_rejects_foreign_endpoint_identity(
    monkeypatch, tmp_path, endpoint
):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "49231\n/devtools/browser/profile-owner\n"
    )
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_profile_process_owns", lambda *_args: True)
    monkeypatch.setattr(daemon, "_json_version_ws", lambda _port: endpoint)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: None)

    assert daemon.launch_automation_chrome() is None


def test_automation_launch_rejects_unrelated_listener(monkeypatch, tmp_path):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(daemon, "_free_port", lambda: 49231)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: "/mock/chrome")
    monkeypatch.setattr(daemon, "_profile_process_owns", lambda *_args: True)
    monkeypatch.setattr(
        daemon, "_json_version_ws", lambda _port: "ws://127.0.0.1:49231/devtools/browser/other"
    )
    clock_values = iter([0, 0, 21])
    monkeypatch.setattr(
        daemon, "time", SimpleNamespace(time=lambda: next(clock_values), sleep=lambda _seconds: None)
    )

    class RunningChild:
        pid = 1234

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: RunningChild())

    assert daemon.launch_automation_chrome() is None


def test_automation_launch_stops_polling_when_child_exits(monkeypatch, tmp_path):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(daemon, "_free_port", lambda: 49231)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: "/mock/chrome")
    discovery = []
    monkeypatch.setattr(daemon, "_json_version_ws", lambda _port: discovery.append(True))

    class FailedChild:
        pid = 1234

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: FailedChild())

    assert daemon.launch_automation_chrome() is None
    assert discovery == []


def test_automation_launch_requires_launched_pid_and_profile_endpoint(
    monkeypatch, tmp_path
):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_port_in_use", lambda _port: True)
    monkeypatch.setattr(daemon, "_free_port", lambda: 49231)
    monkeypatch.setattr(daemon, "_automation_chrome_binary", lambda: "/mock/chrome")
    clock_values = iter([0, 0])
    monkeypatch.setattr(
        daemon, "time", SimpleNamespace(time=lambda: next(clock_values), sleep=lambda _seconds: None)
    )

    class RunningChild:
        pid = 1234

        @staticmethod
        def poll():
            return None

    def spawn(*_args, **_kwargs):
        (profile / "DevToolsActivePort").write_text(
            "49231\n/devtools/browser/launched-profile\n"
        )
        return RunningChild()

    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    owners = []
    monkeypatch.setattr(
        daemon,
        "_profile_process_owns",
        lambda _profile, expected_pid=None: owners.append(expected_pid) or expected_pid == 1234,
    )
    monkeypatch.setattr(
        daemon,
        "_json_version_ws",
        lambda _port: "ws://127.0.0.1:49231/devtools/browser/launched-profile",
    )

    assert daemon.launch_automation_chrome() == (
        "ws://127.0.0.1:49231/devtools/browser/launched-profile"
    )
    assert owners == [1234]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('/Applications/Chrome --user-data-dir=/tmp/automation-profile', True),
        ('/Applications/Chrome --user-data-dir=/tmp/unrelated-profile', False),
    ],
)
def test_profile_process_identity_matches_user_data_dir(monkeypatch, tmp_path, command, expected):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("host-1234")
    monkeypatch.setattr(daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(daemon.os, "readlink", lambda _path: "host-1234")
    observed = command.replace("/tmp/automation-profile", str(profile))
    monkeypatch.setattr(daemon.subprocess, "check_output", lambda *_args, **_kwargs: observed)
    assert daemon._profile_process_owns(profile) is expected


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("BH_HOME", "/custom/bh", "/custom/bh/chrome-profile"),
        (
            "BROWSER_HARNESS_HOME",
            "/custom/browser-harness",
            "/custom/browser-harness/chrome-profile",
        ),
        ("XDG_CONFIG_HOME", "/custom/config", "/custom/config/browser-harness/chrome-profile"),
    ],
)
def test_default_automation_profile_uses_harness_home(
    monkeypatch, variable, value, expected
):
    from browser_harness import paths

    for key in ("BH_HOME", "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME", "BH_AUTOMATION_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(variable, value)

    assert daemon.automation_profile() == paths.home_dir() / "chrome-profile"
    assert str(daemon.automation_profile()) == expected
