#!/usr/bin/env python3
"""Plan or install Titan browser launcher and separately registered skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

RECEIPT_SCHEMA = 1
MAX_RECEIPT_BYTES = 64 * 1024
SOURCE_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_WRAPPER = b'@echo off\r\npy -3 "%~dp0titan-browser.py" %*\r\n'


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_hash(plan: dict) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "manifest_sha256"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(canonical)


def _safe_parent_chain(target: Path, boundary: Path) -> bool:
    try:
        relative = target.parent.relative_to(boundary)
    except ValueError:
        return False
    current = boundary
    components = (Path("."), *relative.parts)
    for component in components:
        if component != Path("."):
            current = current / component
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
        if os.name != "nt" and metadata.st_uid not in {0, os.getuid()}:
            return False
    return True


def _ensure_boundary(boundary: Path) -> None:
    current = Path(boundary.anchor)
    for component in boundary.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("boundary_conflict")
        if os.name != "nt" and metadata.st_uid not in {0, os.getuid()}:
            raise ValueError("boundary_conflict")


def _boundary_is_safe(boundary: Path) -> bool:
    current = Path(boundary.anchor)
    for component in boundary.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
        if os.name != "nt" and metadata.st_uid not in {0, os.getuid()}:
            return False
    return True


def _ensure_directories(directory: Path, boundary: Path, *, private: bool = False) -> None:
    _ensure_boundary(boundary)
    if not _safe_parent_chain(directory / ".placeholder", boundary):
        raise ValueError("directory_conflict")
    relative = directory.relative_to(boundary)
    current = boundary
    for component in relative.parts:
        current = current / component
        if not current.exists():
            current.mkdir(mode=0o700)
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("directory_conflict")
        if os.name != "nt" and metadata.st_uid != os.getuid():
            raise ValueError("directory_conflict")
    if private and os.name != "nt" and current.stat().st_mode & 0o077:
        raise ValueError("directory_conflict")


def _no_symlink_components(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        if current.is_symlink():
            return False
    return True


def _receipt_path(home: Path, platform: str, local_app_data: Path | None) -> Path:
    if platform == "win32":
        base = local_app_data or home / "AppData" / "Local"
        return base / "Titan" / "Browser Harness" / "install-receipt.json"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Titan" / "Browser Harness" / "install-receipt.json"
    return home / ".config" / "titan-browser" / "install-receipt.json"


def installation_targets(
    *, home: Path, codex_home: Path, platform: str, local_app_data: Path | None = None,
    source_root: Path = SOURCE_ROOT,
) -> list[tuple[str, Path, bytes, str | None]]:
    def source(relative: str) -> bytes:
        return (source_root / relative).read_bytes()

    launcher = source("scripts/titan-browser")
    if platform == "win32":
        bin_dir = (local_app_data or home / "AppData" / "Local") / "Titan" / "bin"
        share_dir = (local_app_data or home / "AppData" / "Local") / "Titan" / "share" / "browser-harness"
        launcher_targets = [
            ("launcher", bin_dir / "titan-browser.py", launcher, "scripts/titan-browser"),
            ("launcher_wrapper", bin_dir / "titan-browser.cmd", WINDOWS_WRAPPER, None),
        ]
    else:
        share_dir = home / ".local" / "share" / "titan-browser"
        launcher_targets = [("launcher", home / ".local" / "bin" / "titan-browser", launcher, "scripts/titan-browser")]
    share_sources = [
        ("runner_package_json", "integrations/titan-playwright/package.json"),
        ("runner_lock", "integrations/titan-playwright/package-lock.json"),
        ("runner_job", "integrations/titan-playwright/src/job.js"),
        ("runner_proxy", "integrations/titan-playwright/src/egress-proxy.js"),
        ("runner_report", "integrations/titan-playwright/src/report.ts"),
        ("runner_private_files", "integrations/titan-playwright/src/private-files.js"),
    ]
    share_targets = [
        (role, share_dir / "integrations" / "titan-playwright" / Path(relative).relative_to("integrations/titan-playwright"), source(relative), relative)
        for role, relative in share_sources
    ]
    share_targets.append((
        "doctor_requirements",
        share_dir / "doctor-requirements.txt",
        source("scripts/titan-doctor-requirements.txt"),
        "scripts/titan-doctor-requirements.txt",
    ))
    python_sources = [
        ("doctor_init", "src/browser_harness/__init__.py"),
        ("doctor_cli", "src/browser_harness/titan_doctor.py"),
    ]
    share_targets.extend(
        (role, share_dir / "src" / "browser_harness" / Path(relative).name, source(relative), relative)
        for role, relative in python_sources
    )
    skill_targets = []
    for registration, skill_dir in (
        ("claude", home / ".claude" / "skills" / "browser-harness"),
        ("codex", codex_home / "skills" / "browser-harness"),
    ):
        for role, relative in (
            (f"{registration}_skill", "SKILL.md"),
            (f"{registration}_install_guide", "install.md"),
            (f"{registration}_workflow_guide", "docs/titan-browser-workflow.md"),
            (f"{registration}_codex_access_guide", "docs/codex-browser-access.md"),
        ):
            skill_targets.append((role, skill_dir / relative, source(relative), relative))
    return launcher_targets + share_targets + skill_targets


def _read_prior_receipt(path: Path) -> dict | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("receipt_conflict")
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise ValueError("receipt_conflict")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("receipt_conflict")
    if metadata.st_size > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_conflict")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("receipt_conflict")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(body) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt_conflict")
        receipt = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("receipt_conflict") from error
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA or not isinstance(receipt.get("files"), list):
        raise ValueError("receipt_conflict")
    return receipt


def build_plan(
    *, repo_root: Path | None = None, home: Path, codex_home: Path, platform: str,
    local_app_data: Path | None = None,
) -> dict:
    home = Path(os.path.abspath(home.expanduser()))
    codex_home = Path(os.path.abspath(codex_home.expanduser()))
    local_app_data = Path(os.path.abspath(local_app_data.expanduser())) if local_app_data else None
    receipt = _receipt_path(home, platform, local_app_data)
    try:
        prior = _read_prior_receipt(receipt)
    except ValueError:
        prior = {"_invalid": True}
    recovery_required = (prior or {}).get("status") == "applying"
    managed = {
        item.get("target"): item for item in (prior or {}).get("files", [])
        if isinstance(item, dict) and isinstance(item.get("target"), str)
    }
    source_root = repo_root or SOURCE_ROOT
    targets = installation_targets(home=home, codex_home=codex_home, platform=platform,
                                   local_app_data=local_app_data, source_root=source_root)
    records = []
    conflicts = []
    target_counts: dict[str, int] = {}
    for _role, target, _content, _source_relative in targets:
        target_counts[str(target)] = target_counts.get(str(target), 0) + 1
    receipt_boundary = (local_app_data or home) if platform == "win32" else home
    if not _boundary_is_safe(receipt_boundary):
        conflicts.append(str(receipt))
    if recovery_required:
        conflicts.append(str(receipt))
    for role, target, content, source_relative in targets:
        target_text = str(target)
        source_hash = _sha256(content)
        previous = managed.get(target_text)
        target_hash = None
        exists = target.exists() or target.is_symlink()
        boundary = codex_home if role.startswith("codex_") else (local_app_data or home) if platform == "win32" else home
        if not _safe_parent_chain(target, boundary) or target_counts[target_text] > 1:
            conflicts.append(target_text)
        if not _boundary_is_safe(boundary):
            conflicts.append(str(boundary))
        if exists:
            try:
                metadata = target.lstat()
                if target.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise OSError("not regular")
                if os.name != "nt" and metadata.st_uid != os.getuid():
                    raise OSError("foreign owner")
                target_hash = _sha256(target.read_bytes())
            except OSError:
                conflicts.append(target_text)
        if (prior or {}).get("_invalid"):
            conflicts.append(str(receipt))
            action = "conflict"
        elif not exists:
            action = "install"
        elif target_hash == source_hash:
            action = "unchanged"
        elif previous and target_hash == previous.get("installed_sha256"):
            action = "update"
        else:
            action = "conflict"
            conflicts.append(target_text)
        records.append({
            "role": role,
            "target": target_text,
            "source": source_relative,
            "boundary": str(boundary),
            "source_sha256": source_hash,
            "target_sha256": target_hash,
            "action": action,
            "managed": bool(previous and target_hash == previous.get("installed_sha256")),
        })
    planned_files = []
    rollback = []
    for record in records:
        if record["action"] in {"install", "update"} or (record["action"] == "unchanged" and record["managed"]):
            planned_files.append({
                "role": record["role"],
                "target": record["target"],
                "installed_sha256": record["source_sha256"],
                "prior_sha256": record["target_sha256"] if record["action"] == "update" else None,
                "backup": "created-during-apply" if record["action"] == "update" else None,
            })
        if record["action"] in {"install", "update"}:
            rollback.append({
                "target": record["target"],
                "action": "remove_if_hash_matches" if record["action"] == "install" else "restore_private_backup_if_hash_matches",
                "installed_sha256": record["source_sha256"],
            })
    plan = {
        "schema": 1,
        "dry_run_default": True,
        "receipt_path": str(receipt),
        "recovery_required": recovery_required,
        "receipt_boundary": str(receipt_boundary),
        "home": str(home),
        "codex_home": str(codex_home),
        "local_app_data": str(local_app_data) if local_app_data else None,
        "platform": platform,
        "files": records,
        "receipt_preview": {"schema": RECEIPT_SCHEMA, "files": planned_files},
        "rollback_plan": rollback,
        "conflicts": sorted(set(conflicts)),
        "writable": not conflicts,
        "source_root": str(Path(source_root).absolute()),
    }
    plan["manifest_sha256"] = _manifest_hash(plan)
    return plan


def read_reviewed_manifest(path: Path, expected_hash: str) -> dict:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError("manifest_rejected")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("manifest_rejected")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("manifest_rejected")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                body = stream.read(1024 * 1024 + 1)
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
        if len(body) > 1024 * 1024:
            raise ValueError("manifest_rejected")
        plan = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest_rejected") from error
    if not isinstance(plan, dict) or plan.get("manifest_sha256") != expected_hash:
        raise ValueError("manifest_hash_mismatch")
    if _manifest_hash(plan) != expected_hash:
        raise ValueError("manifest_hash_mismatch")
    return plan


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".titan-browser-install-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _encode_receipt(receipt: dict) -> bytes:
    payload = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_too_large")
    return payload


def _read_private_receipt_backup(path: Path, expected_sha256: str) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RECEIPT_BYTES:
            raise ValueError("receipt_backup_conflict")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("receipt_backup_conflict")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise ValueError("receipt_backup_conflict")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("receipt_backup_conflict")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_RECEIPT_BYTES + 1)
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
    except OSError as error:
        raise ValueError("receipt_backup_conflict") from error
    if len(content) > MAX_RECEIPT_BYTES or _sha256(content) != expected_sha256:
        raise ValueError("receipt_backup_conflict")
    try:
        previous = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("receipt_backup_conflict") from error
    if not isinstance(previous, dict) or previous.get("schema") != RECEIPT_SCHEMA or not isinstance(previous.get("files"), list):
        raise ValueError("receipt_backup_conflict")
    return content


def _prepare_previous_receipt_backup(receipt_dir: Path, prior: dict | None) -> tuple[dict | None, Path | None, bytes | None]:
    if prior is None:
        return None, None, None
    content = _encode_receipt(prior)
    digest = _sha256(content)
    path = receipt_dir / "backups" / f"receipt-{digest}.bak"
    if path.exists() or path.is_symlink():
        _read_private_receipt_backup(path, digest)
    return {"sha256": digest}, path, content


def apply_plan(plan: dict, *, repo_root: Path | None = None) -> dict:
    if plan.get("manifest_sha256") != _manifest_hash(plan):
        raise ValueError("manifest_hash_mismatch")
    if plan["conflicts"]:
        raise ValueError("install_conflict")
    source_root = Path(repo_root or plan["source_root"]).absolute()
    if str(source_root) != plan.get("source_root"):
        raise ValueError("source_root_changed")
    receipt_path = Path(plan["receipt_path"])
    receipt_dir = receipt_path.parent
    receipt_boundary = Path(plan["receipt_boundary"])
    prior = _read_prior_receipt(receipt_path)
    if (prior or {}).get("status") == "applying":
        raise ValueError("recovery_required")
    # Create/validate all boundary roots and parent directories before touching
    # any target file. A missing custom CODEX_HOME is a supported boundary.
    boundaries = {Path(record["boundary"]) for record in plan["files"]}
    boundaries.add(receipt_boundary)
    if any(not _boundary_is_safe(boundary) for boundary in boundaries):
        raise ValueError("boundary_conflict")
    for boundary in sorted(boundaries, key=lambda value: len(value.parts)):
        _ensure_boundary(boundary)
    _ensure_directories(receipt_dir, receipt_boundary, private=True)
    backup_dir = receipt_dir / "backups"
    _ensure_directories(backup_dir, receipt_boundary, private=True)

    old_files = {item.get("target"): item for item in (prior or {}).get("files", []) if isinstance(item, dict)}
    actions = []
    preflight_records = []
    for record in plan["files"]:
        if record["action"] not in {"install", "update", "unchanged"}:
            raise ValueError("install_conflict")
        target = Path(record["target"])
        boundary = Path(record["boundary"])
        if not _safe_parent_chain(target, boundary):
            raise ValueError("directory_conflict")
        if record["action"] in {"install", "update"}:
            _ensure_directories(target.parent, boundary)
        current_exists = target.exists() or target.is_symlink()
        if record["action"] == "install" and current_exists:
            raise ValueError("install_conflict")
        if record["action"] in {"update", "unchanged"} and (
            not current_exists or target.is_symlink() or not target.is_file()
            or _sha256(target.read_bytes()) != record["target_sha256"]
        ):
            raise ValueError("target_changed")
        role = record["role"]
        if record.get("source"):
            content = (source_root / record["source"]).read_bytes()
        elif role == "launcher_wrapper":
            content = WINDOWS_WRAPPER
        else:
            raise ValueError("installer_source_missing")
        if _sha256(content) != record["source_sha256"]:
            raise ValueError("source_changed")
        if record["action"] == "unchanged" and _sha256(content) != record["target_sha256"]:
            raise ValueError("source_changed")
        preflight_records.append((record, content))
        if record["action"] == "unchanged":
            continue
        backup_path = None
        prior_bytes = target.read_bytes() if record["action"] == "update" else None
        if prior_bytes is not None:
            backup_path = backup_dir / f"{hashlib.sha256(str(target).encode()).hexdigest()}-{record['target_sha256']}.bak"
            if backup_path.exists() or backup_path.is_symlink():
                metadata = backup_path.lstat()
                if backup_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("backup_conflict")
                if os.name != "nt" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
                    raise ValueError("backup_conflict")
                if _sha256(backup_path.read_bytes()) != record["target_sha256"]:
                    raise ValueError("backup_conflict")
        actions.append({
            "role": role,
            "target": str(target),
            "source": record.get("source"),
            "boundary": str(boundary),
            "action": record["action"],
            "installed_sha256": _sha256(content),
            "prior_sha256": record["target_sha256"],
            "backup": str(backup_path) if backup_path else None,
            "content": content,
            "prior_bytes": prior_bytes,
        })

    # Recheck all planned source and target bytes after safe parent creation
    # and immediately before the journal/first target mutation.
    for record, content in preflight_records:
        target = Path(record["target"])
        if not _safe_parent_chain(target, Path(record["boundary"])):
            raise ValueError("directory_conflict")
        if record.get("source"):
            current_source = (source_root / record["source"]).read_bytes()
        elif record["role"] == "launcher_wrapper":
            current_source = WINDOWS_WRAPPER
        else:
            raise ValueError("installer_source_missing")
        if _sha256(current_source) != record["source_sha256"]:
            raise ValueError("source_changed")
        current_exists = target.exists() or target.is_symlink()
        if record["target_sha256"] is None:
            if current_exists:
                raise ValueError("target_changed")
        elif (
            not current_exists or target.is_symlink() or not target.is_file()
            or _sha256(target.read_bytes()) != record["target_sha256"]
        ):
            raise ValueError("target_changed")
    # Revalidate unchanged records too, immediately before the journal and
    # first mutation. This prevents a dry-run reviewed against stale source or
    # destination bytes from authorizing a different apply.
    for record, _content in preflight_records:
        if record["action"] != "unchanged":
            continue
        target = Path(record["target"])
        source_path = source_root / record["source"] if record.get("source") else None
        if source_path is not None and _sha256(source_path.read_bytes()) != record["source_sha256"]:
            raise ValueError("source_changed")
        if target.is_symlink() or not target.is_file() or _sha256(target.read_bytes()) != record["target_sha256"]:
            raise ValueError("target_changed")

    previous_files = list((prior or {}).get("files", []))
    previous_receipt_backup, previous_receipt_backup_path, previous_receipt_backup_bytes = (
        _prepare_previous_receipt_backup(receipt_dir, prior)
    )
    pending = {
        "schema": RECEIPT_SCHEMA,
        "status": "applying",
        "previous_receipt_backup": previous_receipt_backup,
        "files": previous_files,
        "transaction": [
            {key: value for key, value in action.items() if key not in {"content", "prior_bytes", "source", "boundary"}}
            for action in actions
        ],
    }
    pending_payload = _encode_receipt(pending)
    final_files = [
        old_files[record["target"]]
        for record in plan["files"]
        if record["action"] == "unchanged" and record["managed"] and record["target"] in old_files
    ]
    final_files.extend(
        {key: value for key, value in item.items() if key != "action"}
        for item in pending["transaction"]
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "complete",
        "previous_receipt_backup": previous_receipt_backup,
        "files": final_files,
        "transaction": pending["transaction"],
    }
    receipt_payload = _encode_receipt(receipt)
    if previous_receipt_backup_path and not previous_receipt_backup_path.exists():
        _atomic_write(previous_receipt_backup_path, previous_receipt_backup_bytes)
    # This durable journal precedes backups and target writes, so a crash or
    # partial write always leaves enough metadata for explicit rollback.
    _atomic_write(receipt_path, pending_payload)

    for action in actions:
        backup_path = Path(action["backup"]) if action["backup"] else None
        if backup_path and not backup_path.exists():
            _atomic_write(backup_path, action["prior_bytes"])
    for action in actions:
        target = Path(action["target"])
        boundary = Path(action["boundary"])
        if not _safe_parent_chain(target, boundary):
            raise ValueError("directory_conflict")
        current_exists = target.exists() or target.is_symlink()
        if action["action"] == "install" and current_exists:
            raise ValueError("install_conflict")
        if action["action"] == "update" and (
            not current_exists or target.is_symlink() or _sha256(target.read_bytes()) != action["prior_sha256"]
        ):
            raise ValueError("install_conflict")
        _atomic_write(target, action["content"], mode=0o700 if action["role"] in {"launcher", "launcher_wrapper"} else 0o600)

    for record, _content in preflight_records:
        if record["action"] != "unchanged":
            continue
        target = Path(record["target"])
        if (
            not _safe_parent_chain(target, Path(record["boundary"]))
            or target.is_symlink() or not target.is_file()
            or _sha256(target.read_bytes()) != record["target_sha256"]
        ):
            raise ValueError("target_changed")

    _atomic_write(receipt_path, receipt_payload)
    return receipt


def rollback_receipt(
    receipt_path: Path, allowed_targets: set[str], *, boundary: Path | None = None,
) -> dict:
    if boundary is not None and not _safe_parent_chain(receipt_path, boundary):
        raise ValueError("receipt_conflict")
    try:
        receipt_parent = receipt_path.parent.lstat()
    except FileNotFoundError:
        receipt_parent = None
    if receipt_parent is not None and (
        not stat.S_ISDIR(receipt_parent.st_mode)
        or (os.name != "nt" and (receipt_parent.st_uid != os.getuid() or receipt_parent.st_mode & 0o077))
    ):
        raise ValueError("receipt_conflict")
    receipt = _read_prior_receipt(receipt_path)
    if not receipt:
        raise ValueError("receipt_missing")
    previous_receipt_bytes = None
    if "previous_receipt_backup" in receipt:
        backup_ref = receipt["previous_receipt_backup"]
        if backup_ref is not None:
            if not isinstance(backup_ref, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(backup_ref.get("sha256", ""))):
                raise ValueError("receipt_backup_conflict")
            backup_path = receipt_path.parent / "backups" / f"receipt-{backup_ref['sha256']}.bak"
            if backup_path.parent != receipt_path.parent / "backups" or not _no_symlink_components(backup_path):
                raise ValueError("receipt_backup_conflict")
            previous_receipt_bytes = _read_private_receipt_backup(backup_path, backup_ref["sha256"])
    elif isinstance(receipt.get("previous_receipt"), dict):
        previous_receipt_bytes = _encode_receipt(receipt["previous_receipt"])
    elif "previous_receipt" not in receipt:
        legacy_previous = receipt.get("previous_files")
        if legacy_previous is not None:
            previous_receipt_bytes = _encode_receipt({
                "schema": RECEIPT_SCHEMA,
                "status": "complete",
                "files": legacy_previous,
                "transaction": [],
            })
    outcomes = []
    transaction = receipt.get("transaction", receipt["files"])
    for item in transaction:
        target = Path(item["target"])
        if str(target) not in allowed_targets:
            outcomes.append({"target": str(target), "status": "conflict"})
            continue
        try:
            if not _no_symlink_components(target):
                raise OSError("symlink parent")
            metadata = target.lstat()
            if target.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise OSError("not regular")
            if os.name != "nt" and metadata.st_uid != os.getuid():
                raise OSError("foreign owner")
            current_hash = _sha256(target.read_bytes())
        except FileNotFoundError:
            if item.get("action") == "install" or item.get("prior_sha256") is None:
                outcomes.append({"target": str(target), "status": "already_absent"})
                continue
            outcomes.append({"target": str(target), "status": "conflict"})
            continue
        except OSError:
            outcomes.append({"target": str(target), "status": "conflict"})
            continue
        prior_hash = item.get("prior_sha256")
        if prior_hash is not None and current_hash == prior_hash:
            outcomes.append({"target": str(target), "status": "unchanged"})
            continue
        if current_hash != item.get("installed_sha256"):
            outcomes.append({"target": str(target), "status": "conflict"})
            continue
        backup = item.get("backup")
        if backup:
            backup_path = Path(backup)
            if backup_path.parent != receipt_path.parent / "backups" or not _no_symlink_components(backup_path):
                outcomes.append({"target": str(target), "status": "conflict"})
                continue
            backup_metadata = backup_path.lstat()
            if not stat.S_ISREG(backup_metadata.st_mode) or (os.name != "nt" and (backup_metadata.st_uid != os.getuid() or backup_metadata.st_mode & 0o077)):
                outcomes.append({"target": str(target), "status": "conflict"})
                continue
            backup_bytes = backup_path.read_bytes()
            if _sha256(backup_bytes) != item.get("prior_sha256"):
                outcomes.append({"target": str(target), "status": "conflict"})
                continue
            _atomic_write(target, backup_bytes, mode=0o700 if target.name in {"titan-browser", "titan-browser.py", "titan-browser.cmd"} else 0o600)
        else:
            target.unlink()
        outcomes.append({"target": str(target), "status": "restored"})
    rolled_back = not any(item["status"] == "conflict" for item in outcomes)
    if rolled_back:
        if previous_receipt_bytes is None:
            receipt_path.unlink()
        else:
            _atomic_write(receipt_path, previous_receipt_bytes)
    return {"rolled_back": rolled_back, "files": outcomes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--local-app-data", type=Path)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="write launcher and skill registrations")
    parser.add_argument("--manifest", type=Path, help="reviewed JSON plan emitted by a dry run")
    parser.add_argument("--manifest-sha256", help="exact manifest_sha256 printed by the dry run")
    parser.add_argument("--rollback", action="store_true", help="restore or remove files from the latest receipt")
    args = parser.parse_args(argv)
    home = Path(os.path.abspath(args.home.expanduser()))
    codex_home = Path(os.path.abspath((args.codex_home or Path(os.environ.get("CODEX_HOME", str(home / ".codex")))).expanduser()))
    platform = sys.platform
    if args.apply:
        if not args.manifest or not args.manifest_sha256:
            sys.stderr.write("--apply requires --manifest and --manifest-sha256 from a reviewed dry run\n")
            return 2
        try:
            plan = read_reviewed_manifest(args.manifest, args.manifest_sha256)
        except ValueError as error:
            sys.stderr.write(f"{error}\n")
            return 2
        if (
            plan.get("home") != str(home)
            or plan.get("codex_home") != str(codex_home)
            or plan.get("platform") != platform
            or plan.get("local_app_data") != (
                str(Path(os.path.abspath(args.local_app_data.expanduser()))) if args.local_app_data else None
            )
        ):
            sys.stderr.write("manifest_inputs_changed\n")
            return 2
    else:
        plan = build_plan(home=home, codex_home=codex_home, platform=platform, local_app_data=args.local_app_data)
    if args.rollback:
        try:
            result = rollback_receipt(
                Path(plan["receipt_path"]),
                {record["target"] for record in plan["files"]},
                boundary=Path(plan["receipt_boundary"]),
            )
        except ValueError as error:
            sys.stdout.write(json.dumps({"status": str(error)}) + "\n")
            return 1
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0 if result["rolled_back"] else 1
    if args.apply:
        if not plan["writable"]:
            sys.stdout.write(json.dumps(plan, sort_keys=True) + "\n")
            return 2
        try:
            receipt = apply_plan(plan)
        except (OSError, ValueError) as error:
            sys.stdout.write(json.dumps({"status": str(error)}) + "\n")
            return 1
        sys.stdout.write(json.dumps({"status": "installed", "receipt": receipt}, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(json.dumps(plan, sort_keys=True, indent=2) + "\n")
    return 2 if not plan["writable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
