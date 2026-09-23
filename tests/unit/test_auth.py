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
