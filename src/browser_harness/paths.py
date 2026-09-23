"""browser-harness filesystem layout."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def home_dir() -> Path:
    raw = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return (Path(base).expanduser() / "browser-harness").resolve()
    return (Path.home() / ".config" / "browser-harness").resolve()


def _windows_user_sid() -> str:
    if sys.platform != "win32":
        raise PermissionError("Windows user SID is only available on Windows")

    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = [wintypes.LPVOID]
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.LocalFree.restype = wintypes.LPVOID
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise PermissionError(f"could not open process token: {ctypes.get_last_error()}")

    try:
        token_user = 1
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        if not size.value:
            raise PermissionError(f"could not query process token: {ctypes.get_last_error()}")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, token_user, buffer, size, ctypes.byref(size)
        ):
            raise PermissionError(f"could not read process token: {ctypes.get_last_error()}")

        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
        if not advapi32.IsValidSid(sid):
            raise PermissionError("process token contains an invalid user SID")
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
            raise PermissionError(f"could not convert user SID: {ctypes.get_last_error()}")
        try:
            return string_sid.value
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)


def _windows_principal(resolve_sid=None) -> str:
    sid = (resolve_sid or _windows_user_sid)()
    if not sid:
        raise PermissionError("could not resolve the effective Windows user SID")
    return f"*{sid.lstrip('*')}"


def _run_icacls(path: Path, *args: str) -> bool:
    try:
        result = subprocess.run(
            ["icacls", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PermissionError(f"could not restrict permissions for {path}: {exc}") from exc

    if result.returncode == 0:
        return True

    detail = (result.stderr or result.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    raise PermissionError(
        f"could not restrict permissions for {path}: icacls exited with {result.returncode}{suffix}"
    )


def _read_acl_snapshot(path: Path, *, directory: bool) -> bytes:
    recursive = ("/T",) if directory else ()
    fd, snapshot_name = tempfile.mkstemp(prefix="browser-harness-acl-read-", suffix=".txt")
    os.close(fd)
    snapshot_path = Path(snapshot_name)
    snapshot_path.unlink(missing_ok=True)
    try:
        _run_icacls(path, "/save", str(snapshot_path), *recursive)
        return snapshot_path.read_bytes()
    finally:
        snapshot_path.unlink(missing_ok=True)


def _acl_principals(snapshot: bytes) -> set[str]:
    if snapshot.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = snapshot.decode("utf-16")
    else:
        text = snapshot.decode("utf-8")
    aliases = {
        "AU": "S-1-5-11",
        "BA": "S-1-5-32-544",
        "BU": "S-1-5-32-545",
        "SY": "S-1-5-18",
        "WD": "S-1-1-0",
    }
    return {
        aliases.get(match.upper().lstrip("*"), match.upper().lstrip("*"))
        for match in re.findall(r"\([^)]*;;;([^;)]+)\)", text)
    }


def _read_acl_principals(path: Path, *, directory: bool) -> set[str]:
    return _acl_principals(_read_acl_snapshot(path, directory=directory))


def _restore_acl(path: Path, backup_path: Path, *, directory: bool) -> None:
    restore_root = path.parent if path.parent != Path("") else Path(".")
    _run_icacls(restore_root, "/restore", str(backup_path))
    if _read_acl_snapshot(path, directory=directory) != backup_path.read_bytes():
        raise PermissionError(f"could not verify restored permissions for {path}")


def _harden_windows_acl(path: Path, *, directory: bool, resolve_sid=None) -> None:
    principal = _windows_principal(resolve_sid)
    approved = {principal.lstrip("*").upper()}

    recursive = ("/T",) if directory else ()
    inheritance = "(OI)(CI)" if directory else ""
    grant = f"{principal}:{inheritance}F"

    fd, backup_name = tempfile.mkstemp(prefix="browser-harness-acl-", suffix=".txt")
    os.close(fd)
    backup_path = Path(backup_name)
    backup_path.unlink(missing_ok=True)

    try:
        _run_icacls(path, "/save", str(backup_path), *recursive)
        backup = backup_path.read_bytes()
        original_principals = _acl_principals(backup)

        try:
            _run_icacls(path, "/inheritance:r", *recursive)
            for unapproved in sorted(original_principals - approved):
                _run_icacls(path, "/remove:g", f"*{unapproved}", *recursive)
                _run_icacls(path, "/remove:d", f"*{unapproved}", *recursive)
            _run_icacls(path, "/grant:r", grant, *recursive)
            remaining = _read_acl_principals(path, directory=directory)
            if remaining != approved:
                names = ", ".join(sorted(remaining - approved)) or "the approved SID is missing"
                raise PermissionError(f"ACL readback failed for {path}: {names}")
        except Exception:
            _restore_acl(path, backup_path, directory=directory)
            raise
    finally:
        backup_path.unlink(missing_ok=True)


def harden_private_path(path: Path, *, directory: bool = False, resolve_sid=None) -> None:
    if sys.platform == "win32":
        _harden_windows_acl(path, directory=directory, resolve_sid=resolve_sid)
        return
    os.chmod(path, 0o700 if directory else 0o600)


def ensure_private_dir(path: Path) -> Path:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32" or not existed:
        harden_private_path(path, directory=True)
    return path


def config_dir() -> Path:
    raw = os.environ.get("BH_CONFIG_DIR")
    return ensure_private_dir(Path(raw).expanduser().resolve() if raw else home_dir())


def inspect_marker() -> Path:
    """Marker recording that the harness opened a chrome://inspect tab"""
    return config_dir() / "inspect-opened"


def runtime_dir() -> Path:
    raw = os.environ.get("BH_RUNTIME_DIR")
    return ensure_private_dir(Path(raw).expanduser().resolve() if raw else home_dir() / "runtime")


def tmp_dir() -> Path:
    raw = os.environ.get("BH_TMP_DIR")
    return ensure_private_dir(Path(raw).expanduser().resolve() if raw else home_dir() / "tmp")


def workspace_dir() -> Path:
    raw = os.environ.get("BH_AGENT_WORKSPACE")
    return ensure_private_dir(Path(raw).expanduser().resolve() if raw else home_dir() / "agent-workspace")
