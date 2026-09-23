from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_harness import paths

APPROVED_SID = "S-1-5-21-111"
FOREIGN_SID = "S-1-5-21-999"


def _set_windows_identity(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(paths, "_windows_user_sid", lambda: APPROVED_SID)
    monkeypatch.setattr(
        paths, "_read_sddl",
        lambda path: f"O:{APPROVED_SID}G:{APPROVED_SID}D:P(A;;FA;;;{APPROVED_SID})",
    )


def _successful_icacls(calls):
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(
                f"D:(A;;FA;;;{APPROVED_SID})".encode()
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_ensure_private_dir_replaces_windows_acl_recursively(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    calls = []
    monkeypatch.setattr(paths.subprocess, "run", _successful_icacls(calls))
    target = tmp_path / "private"

    assert paths.ensure_private_dir(target) == target

    argv = [call[0] for call in calls]
    assert argv[0][:3] == ["icacls", str(target), "/save"]
    assert argv[0][-1] == "/T"
    assert argv[1:-1] == [
        ["icacls", str(target), "/inheritance:r", "/T"],
        ["icacls", str(target), "/grant:r", f"*{APPROVED_SID}:(OI)(CI)F", "/T"],
    ]
    assert argv[-1][:3] == ["icacls", str(target), "/save"]
    assert argv[-1][-1] == "/T"
    assert all(call[1] == {"capture_output": True, "text": True, "check": False} for call in calls)


def test_ensure_private_dir_rehardens_existing_windows_tree(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "private"
    target.mkdir()
    (target / "existing.txt").write_text("secret", encoding="utf-8")
    calls = []
    monkeypatch.setattr(paths.subprocess, "run", _successful_icacls(calls))

    paths.ensure_private_dir(target)

    argv = [call[0] for call in calls]
    assert len(argv[0]) == 5
    backup = argv[0][3]
    assert argv[:3] == [
        ["icacls", str(target), "/save", backup, "/T"],
        ["icacls", str(target), "/inheritance:r", "/T"],
        ["icacls", str(target), "/grant:r", f"*{APPROVED_SID}:(OI)(CI)F", "/T"],
    ]
    assert argv[3][:3] == ["icacls", str(target), "/save"]
    assert argv[3][-1] == "/T"


def test_harden_private_path_replaces_file_acl_on_windows(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    calls = []
    monkeypatch.setattr(paths.subprocess, "run", _successful_icacls(calls))
    target = tmp_path / "auth.json"

    paths.harden_private_path(target)

    argv = [call[0] for call in calls]
    assert len(argv[0]) == 4
    backup = argv[0][3]
    assert argv[1:3] == [
        ["icacls", str(target), "/inheritance:r"],
        ["icacls", str(target), "/grant:r", f"*{APPROVED_SID}:F"],
    ]
    assert argv[3][:3] == ["icacls", str(target), "/save"]
    assert argv[0] == ["icacls", str(target), "/save", backup]


def test_harden_private_path_restores_acl_after_partial_failure(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "private"
    target.mkdir()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(
                f"D:(A;;FA;;;{APPROVED_SID})".encode()
            )
        if "/grant:r" in args:
            return SimpleNamespace(returncode=5, stdout="", stderr="Access is denied.")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="icacls exited with 5"):
        paths.harden_private_path(target, directory=True)

    backup = calls[0][3]
    assert calls[:4] == [
        ["icacls", str(target), "/save", backup, "/T"],
        ["icacls", str(target), "/inheritance:r", "/T"],
        ["icacls", str(target), "/grant:r", f"*{APPROVED_SID}:(OI)(CI)F", "/T"],
        ["icacls", str(target.parent), "/restore", backup],
    ]
    assert calls[4][:3] == ["icacls", str(target), "/save"]
    assert calls[4][-1] == "/T"
    assert len(calls) == 5
    assert all("/reset" not in call for call in calls)


def test_harden_private_path_raises_when_icacls_fails(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=5, stdout="", stderr="Access is denied.")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="icacls exited with 5"):
        paths.harden_private_path(tmp_path / "private", directory=True)


def test_harden_private_path_uses_token_sid_instead_of_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "untrusted-name")
    monkeypatch.setenv("USERDOMAIN", "untrusted-domain")
    monkeypatch.setattr(paths, "_windows_user_sid", lambda: APPROVED_SID)

    assert paths._windows_principal() == f"*{APPROVED_SID}"


def test_harden_private_path_raises_when_icacls_is_unavailable(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)

    def fake_run(args, **kwargs):
        raise FileNotFoundError("icacls")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="icacls"):
        paths.harden_private_path(tmp_path / "private", directory=True)


def test_harden_private_path_propagates_acl_restore_failure(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "auth.json"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(
                f"D:(A;;FA;;;{APPROVED_SID})".encode()
            )
        if "/grant:r" in args:
            return SimpleNamespace(returncode=5, stdout="", stderr="Access denied")
        if "/restore" in args:
            return SimpleNamespace(returncode=5, stdout="", stderr="Restore denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="Restore denied"):
        paths.harden_private_path(target)

    assert calls[-1][0:3] == ["icacls", str(target.parent), "/restore"]


def test_harden_private_path_removes_foreign_explicit_grants(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "auth.json"
    calls = []
    saves = iter(
        [
            f"D:(A;;FA;;;{APPROVED_SID})(A;;FA;;;{FOREIGN_SID})(A;;FA;;;S-1-1-0)".encode(),
            f"D:(A;;FA;;;{APPROVED_SID})".encode(),
        ]
    )

    def fake_run(args, **kwargs):
        calls.append(args)
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(next(saves))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    paths.harden_private_path(target)

    assert [call[2] for call in calls] == [
        "/save",
        "/inheritance:r",
        "/remove:g",
        "/remove:d",
        "/remove:g",
        "/remove:d",
        "/grant:r",
        "/save",
    ]
    removed = {call[3] for call in calls if call[2] in {"/remove:g", "/remove:d"}}
    assert removed == {f"*{FOREIGN_SID}", "*S-1-1-0"}


def test_harden_private_path_rolls_back_when_acl_readback_has_foreign_principal(
    monkeypatch, tmp_path
):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "auth.json"
    calls = []
    snapshots = iter([
        f"O:{APPROVED_SID}G:{APPROVED_SID}D:P(A;;FA;;;{APPROVED_SID})",
        f"O:{APPROVED_SID}G:{APPROVED_SID}D:P(A;;FA;;;{APPROVED_SID})(A;;FA;;;{FOREIGN_SID})",
    ])
    monkeypatch.setattr(paths, "_read_sddl", lambda path: next(snapshots))

    def fake_run(args, **kwargs):
        calls.append(args)
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(
                f"D:(A;;FA;;;{APPROVED_SID})".encode()
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="unapproved principal"):
        paths.harden_private_path(target)

    assert any("/restore" in call for call in calls)


def test_posix_private_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(paths.os, "chmod", lambda path, mode: calls.append((path, mode)))

    new_dir = tmp_path / "new"
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    private_file = tmp_path / "auth.json"
    private_file.touch()

    paths.ensure_private_dir(new_dir)
    paths.ensure_private_dir(existing_dir)
    paths.harden_private_path(private_file)

    assert calls == [
        (new_dir, 0o700),
        (private_file, 0o600),
    ]


@pytest.mark.parametrize("ace", [
    f"(XA;;FA;;;{APPROVED_SID};(@User.x==1))",
        f"(A;;FA;;;{APPROVED_SID};(@User.x==1))",
        f"(A;;FR;;;{APPROVED_SID})",
        f"(A;;FA;;;{FOREIGN_SID})",
])
def test_acl_parser_rejects_conditional_and_insufficient_aces(ace):
    with pytest.raises(PermissionError):
        paths._parse_acl_snapshot(
            f"O:{APPROVED_SID}G:{APPROVED_SID}D:P{ace}".encode(),
            approved_sid=APPROVED_SID,
        )


@pytest.mark.parametrize("flags", ["IO", "OIIO", "ID", "ZZ", "NP", "OIOI"])
def test_acl_parser_rejects_non_applicable_or_invalid_approved_ace_flags(flags):
    with pytest.raises(PermissionError):
        paths._parse_acl_snapshot(
            f"O:{APPROVED_SID}G:{APPROVED_SID}D:P"
            f"(A;{flags};FA;;;{APPROVED_SID})".encode(),
            approved_sid=APPROVED_SID,
        )


def test_acl_parser_accepts_applicable_directory_inheritance_flags():
    assert paths._parse_acl_snapshot(
        f"O:{APPROVED_SID}G:{APPROVED_SID}D:P"
        f"(A;OICI;FA;;;{APPROVED_SID})".encode(),
        approved_sid=APPROVED_SID,
    ) == {APPROVED_SID}


def test_acl_parser_requires_protected_dacl_trusted_owner_and_no_deny():
    valid_prefix = f"O:{APPROVED_SID}G:{APPROVED_SID}D:"
    for sddl in (
        valid_prefix + f"(A;;FA;;;{APPROVED_SID})",
        f"O:{FOREIGN_SID}G:{APPROVED_SID}D:P(A;;FA;;;{APPROVED_SID})",
        f"O:{APPROVED_SID}G:{APPROVED_SID}D:P(D;;FA;;;{FOREIGN_SID})(A;;FA;;;{APPROVED_SID})",
        f"O:{APPROVED_SID}G:{APPROVED_SID}D:P",
    ):
        with pytest.raises(PermissionError):
            paths._parse_acl_snapshot(sddl.encode(), approved_sid=APPROVED_SID)


def test_hardening_rejects_reparse_points_before_acl_commands(monkeypatch, tmp_path):
    _set_windows_identity(monkeypatch)
    target = tmp_path / "link"
    target.symlink_to(tmp_path, target_is_directory=True)
    calls = []
    monkeypatch.setattr(paths.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(PermissionError, match="reparse point"):
        paths.harden_private_path(target)
    assert calls == []
