from types import SimpleNamespace

import pytest

from browser_harness import paths


def _set_windows_identity(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "WORKSTATION")


def test_ensure_private_dir_hardens_new_windows_directory(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)
    target = tmp_path / "private"

    assert paths.ensure_private_dir(target) == target
    assert calls == [
        (
            [
                "icacls",
                str(target),
                "/inheritance:r",
                "/grant:r",
                "WORKSTATION\\alice:(OI)(CI)F",
            ],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_ensure_private_dir_hardens_existing_windows_directory(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "private"
    target.mkdir()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    paths.ensure_private_dir(target)

    assert len(calls) == 1
    assert calls[0][-1] == "WORKSTATION\\alice:(OI)(CI)F"


def test_harden_private_path_uses_file_acl_on_windows(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)
    target = tmp_path / "auth.json"

    paths.harden_private_path(target)

    assert calls[0][-1] == "WORKSTATION\\alice:F"


def test_harden_private_path_warns_when_icacls_fails(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=5, stdout="", stderr="Access is denied.")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.warns(RuntimeWarning, match="icacls exited with 5"):
        paths.harden_private_path(tmp_path / "private", directory=True)
