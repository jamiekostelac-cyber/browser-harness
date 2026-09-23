import json
from pathlib import Path

import pytest

from browser_harness import auth


@pytest.mark.parametrize("raw", ["[]", "null", '"token"', "123"])
def test_load_auth_file_rejects_non_object_json(tmp_path, raw):
    path = tmp_path / "auth.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(auth.AuthError):
        auth.load_auth_file(path)


def test_load_auth_file_accepts_object_and_missing(tmp_path):
    path = tmp_path / "auth.json"
    assert auth.load_auth_file(path) == {}
    path.write_text(json.dumps({"browser_use": {"api_key": "k"}}), encoding="utf-8")
    assert auth.load_auth_file(path) == {"browser_use": {"api_key": "k"}}


def test_stored_auth_record_rejects_non_object_json(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(auth.AuthError):
        auth.stored_auth_record(path)


def test_clear_auth_rejects_non_object_json(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("null", encoding="utf-8")
    with pytest.raises(auth.AuthError):
        auth.clear_auth(path)


def test_save_auth_record_does_not_harden_relative_parent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(auth, "_chmod_private", lambda path, directory=False: calls.append((path, directory)))

    auth.save_auth_record(auth.AuthRecord(api_key="x" * 24), path=Path("auth.json"))

    assert Path("auth.json").exists()
    assert any(path.name.startswith("auth.json.") and path.name.endswith(".tmp") for path, _ in calls)
    assert (Path("."), True) not in calls


def test_write_private_json_hardens_before_writing_and_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "auth.json.tmp"
    key = "secret-api-key-that-must-not-be-written"

    def fail_hardening(path, *, directory=False):
        assert path.exists()
        assert path.read_bytes() == b""
        raise PermissionError("ACL hardening failed")

    monkeypatch.setattr(auth, "_chmod_private", fail_hardening)

    with pytest.raises(PermissionError, match="ACL hardening failed"):
        auth._write_private_json(path, {"api_key": key})

    assert not path.exists()


def test_write_private_json_keeps_creation_handle_through_hardening(monkeypatch, tmp_path):
    final_path = tmp_path / "auth.json"
    path, creation_fd = auth._new_auth_temp(final_path)
    ordering = []
    original_ftruncate = auth.os.ftruncate

    def harden(private_path, *, directory=False):
        assert auth.os.fstat(creation_fd)
        assert private_path.stat().st_ino == auth.os.fstat(creation_fd).st_ino
        assert private_path.read_bytes() == b""
        ordering.append(("harden", creation_fd))

    def tracking_ftruncate(fd, size):
        ordering.append(("write", fd))
        return original_ftruncate(fd, size)

    monkeypatch.setattr(auth, "_chmod_private", harden)
    monkeypatch.setattr(auth.os, "ftruncate", tracking_ftruncate)

    auth._write_private_json(path, {"api_key": "secret"}, fd=creation_fd)

    assert ordering == [("harden", creation_fd), ("write", creation_fd)]
    with pytest.raises(OSError):
        auth.os.fstat(creation_fd)
    assert json.loads(path.read_text(encoding="utf-8")) == {"api_key": "secret"}


def test_save_auth_record_rejects_existing_symlink_before_parent_hardening(monkeypatch, tmp_path):
    target = tmp_path / "auth.json"
    secret_target = tmp_path / "outside.json"
    secret_target.write_text("{}", encoding="utf-8")
    target.symlink_to(secret_target)
    calls = []
    monkeypatch.setattr(auth, "_chmod_private", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(PermissionError, match="reparse point"):
        auth.save_auth_record(auth.AuthRecord(api_key="x" * 24), path=target)

    assert calls == []
    assert secret_target.read_text(encoding="utf-8") == "{}"
