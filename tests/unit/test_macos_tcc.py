"""macOS TCC blocks reading the browser profile dir (EPERM), where
DevToolsActivePort lives. get_ws_url() must not crash on that: it falls back to a
dedicated automation Chrome, and fails with actionable guidance when it can't.
No real browser is launched."""
import pytest

from browser_harness import daemon


class _BlockedProfile:
    """A profile dir the OS refuses to read (macOS TCC / EPERM)."""

    def __truediv__(self, _name):
        return self

    def read_text(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

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


def test_automation_profile_rediscovers_selected_port_after_restart(monkeypatch, tmp_path):
    profile = tmp_path / "automation-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("49231\n/devtools/browser/persisted\n")
    monkeypatch.setattr(daemon, "AUTOMATION_PROFILE", profile)
    monkeypatch.setattr(daemon, "_json_version_ws", lambda port: f"ws://127.0.0.1:{port}/json" if port == 49231 else None)

    def unexpected_launch():
        pytest.fail("a live automation profile should be reused")

    monkeypatch.setattr(daemon, "_automation_chrome_binary", unexpected_launch)
    assert daemon.launch_automation_chrome() == "ws://127.0.0.1:49231/json"
