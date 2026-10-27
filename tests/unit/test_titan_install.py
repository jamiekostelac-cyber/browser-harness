import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = ROOT / "scripts" / "install-titan-browser.py"
LAUNCHER_PATH = ROOT / "scripts" / "titan-browser"
SPEC = importlib.util.spec_from_file_location("titan_browser_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


def test_launcher_runs_from_unrelated_cwd_and_strips_shared_browser_environment(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    capture = tmp_path / "invocation.json"
    node = fake_bin / "node"
    fake_browser = tmp_path / "fake-chromium"
    fake_browser.write_text("fake executable")
    node.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n print('v25.6.1'); raise SystemExit(0)\n"
        "if '--input-type=module' in sys.argv:\n print(os.environ['FAKE_BROWSER_PATH']); raise SystemExit(0)\n"
        "with open(os.environ['CAPTURE_PATH'], 'w') as stream:\n"
        " json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:], 'env': dict(os.environ)}, stream)\n"
    )
    node.chmod(0o700)
    install_root = tmp_path / ".local" / "share" / "titan-browser"
    package_dir = install_root / "integrations" / "titan-playwright"
    (package_dir / "node_modules" / "playwright").mkdir(parents=True)
    (package_dir / "src").mkdir()
    (package_dir / "package.json").write_text(json.dumps({"dependencies": {"playwright": "1.64.1"}}))
    (package_dir / "node_modules" / "playwright" / "package.json").write_text(json.dumps({"version": "1.64.1"}))
    (package_dir / "src" / "job.js").write_text("// installed fake runner")
    launcher = tmp_path / ".local" / "bin" / "titan-browser"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER_PATH, launcher)
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    environment = os.environ.copy()
    environment.update({
        "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
        "CAPTURE_PATH": str(capture),
        "FAKE_BROWSER_PATH": str(fake_browser),
        "BU_CDP_URL": "http://127.0.0.1:9333",
        "BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/secret",
        "BU_NAME": "shared-session",
        "BU_BROWSER_ID": "shared-browser",
        "BU_CUSTOM_SETTING": "shared",
        "BROWSER_USE_API_KEY": "must-not-inherit",
        "HTTPS_PROXY": "http://proxy.invalid:8080",
    })

    result = subprocess.run(
        [sys.executable, str(launcher), "run", "relative-job.json"],
        cwd=elsewhere,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocation = json.loads(capture.read_text())
    assert invocation["cwd"] == str(elsewhere)
    assert invocation["argv"][0] == str(package_dir / "src/job.js")
    assert invocation["argv"][1] == "relative-job.json"
    assert not any(key.startswith(("BU_", "BH_", "BROWSER_HARNESS_")) for key in invocation["env"])
    assert "BROWSER_USE_API_KEY" not in invocation["env"]
    assert "HTTPS_PROXY" not in invocation["env"]


def test_launcher_doctor_rejects_non_loopback_without_network(tmp_path):
    share = tmp_path / ".local" / "share" / "titan-browser"
    doctor_package = share / "src" / "browser_harness"
    doctor_package.mkdir(parents=True)
    shutil.copy2(ROOT / "src/browser_harness/__init__.py", doctor_package / "__init__.py")
    shutil.copy2(ROOT / "src/browser_harness/titan_doctor.py", doctor_package / "titan_doctor.py")
    launcher = tmp_path / ".local" / "bin" / "titan-browser"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER_PATH, launcher)
    shadow_package = tmp_path / "browser_harness"
    shadow_package.mkdir()
    shadow_marker = tmp_path / "doctor-shadow-executed"
    (shadow_package / "__init__.py").write_text(
        f"from pathlib import Path; Path({str(shadow_marker)!r}).write_text('shadowed')\n",
    )
    (shadow_package / "titan_doctor.py").write_text("raise SystemExit(0)\n")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(launcher), "doctor", "--endpoint", "http://example.invalid:9333", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "INVALID_ENDPOINT"
    assert not shadow_marker.exists()


def _build_plan(home: Path):
    return installer.build_plan(
        home=home,
        codex_home=home / ".codex",
        platform=sys.platform,
        local_app_data=home / "LocalAppData" if sys.platform == "win32" else None,
    )


def test_installer_dry_run_is_read_only_and_includes_hash_receipt_and_rollback(tmp_path):
    home = tmp_path / "home"
    plan = _build_plan(home)

    assert plan["writable"] is True
    assert all(record["action"] == "install" for record in plan["files"])
    assert all(len(record["source_sha256"]) == 64 for record in plan["files"])
    assert len(plan["receipt_preview"]["files"]) == len(plan["files"])
    assert len(plan["rollback_plan"]) == len(plan["files"])
    assert not home.exists()


def test_installed_skill_bundle_contains_relative_resources_for_both_runtimes(tmp_path):
    targets = installer.installation_targets(
        home=tmp_path / "home", codex_home=tmp_path / "codex-home", platform="linux",
    )
    target_map = {str(target): content for _, target, content, _ in targets}
    for base in (
        tmp_path / "home/.claude/skills/browser-harness",
        tmp_path / "codex-home/skills/browser-harness",
    ):
        skill = target_map[str(base / "SKILL.md")].decode()
        assert str(base / "install.md") in target_map
        assert str(base / "docs/titan-browser-workflow.md") in target_map
        assert str(base / "docs/codex-browser-access.md") in target_map
        assert "docs/titan-browser-workflow.md" in skill


def test_installer_detects_symlinked_target_parent_without_writing_outside_home(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    home.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)

    plan = _build_plan(home)

    assert plan["writable"] is False
    assert any(str(home / ".local") in conflict for conflict in plan["conflicts"])
    assert list(outside.iterdir()) == []


def test_installer_cli_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    result = subprocess.run(
        [sys.executable, str(INSTALLER_PATH), "--home", str(home), "--codex-home", str(codex_home)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["writable"] is True
    assert not home.exists()
    assert not codex_home.exists()


def test_apply_creates_missing_custom_codex_home_only_after_safe_preflight(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "custom-codex-home"
    plan = installer.build_plan(home=home, codex_home=codex_home, platform="linux")

    assert plan["writable"] is True
    receipt = installer.apply_plan(plan)

    assert codex_home.is_dir()
    assert (codex_home / "skills/browser-harness/SKILL.md").is_file()
    assert receipt["status"] == "complete"


def test_installer_rejects_symlinked_codex_home_boundary(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-codex"
    outside.mkdir()
    codex_home = home / ".codex"
    codex_home.symlink_to(outside, target_is_directory=True)

    plan = installer.build_plan(home=home, codex_home=codex_home, platform="linux")

    assert plan["writable"] is False
    assert any(str(codex_home) in conflict for conflict in plan["conflicts"])
    assert list(outside.iterdir()) == []


def _test_source_tree(tmp_path):
    source_root = tmp_path / "source"
    targets = installer.installation_targets(
        home=tmp_path / "home", codex_home=tmp_path / "codex", platform="linux", source_root=ROOT,
    )
    for _role, _target, content, relative in targets:
        if relative is None:
            continue
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return source_root


def test_partial_install_journal_recovers_updates_and_preserves_user_changes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_root = _test_source_tree(tmp_path)
    initial_plan = installer.build_plan(
        home=home, codex_home=codex_home, platform="linux", repo_root=source_root,
    )
    installer.apply_plan(initial_plan, repo_root=source_root)

    original_launcher = (source_root / "scripts/titan-browser").read_bytes()
    original_package = (source_root / "integrations/titan-playwright/package.json").read_bytes()
    (source_root / "scripts/titan-browser").write_bytes(original_launcher + b"\n# update\n")
    (source_root / "integrations/titan-playwright/package.json").write_bytes(original_package + b"\n")
    update_plan = installer.build_plan(
        home=home, codex_home=codex_home, platform="linux", repo_root=source_root,
    )
    updated = [record for record in update_plan["files"] if record["action"] == "update"]
    assert len(updated) == 2
    targets = [Path(record["target"]) for record in updated]
    unrelated = targets[0].parent / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    original_atomic_write = installer._atomic_write

    def fail_after_second_update(path, data, **kwargs):
        original_atomic_write(path, data, **kwargs)
        if Path(path) == targets[1]:
            raise OSError("injected failure after replacement")

    monkeypatch.setattr(installer, "_atomic_write", fail_after_second_update)
    with pytest.raises(OSError, match="injected failure"):
        installer.apply_plan(update_plan, repo_root=source_root)
    monkeypatch.setattr(installer, "_atomic_write", original_atomic_write)

    receipt_path = Path(update_plan["receipt_path"])
    pending = installer._read_prior_receipt(receipt_path)
    assert pending["status"] == "applying"
    (targets[1]).write_bytes(b"operator changed this after interrupted install")
    rollback = installer.rollback_receipt(receipt_path, {record["target"] for record in update_plan["files"]}, boundary=home)

    assert rollback["rolled_back"] is False
    assert targets[0].read_bytes() == original_launcher
    assert targets[1].read_bytes() == b"operator changed this after interrupted install"
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_interrupted_initial_install_requires_rollback_before_a_new_apply(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_root = _test_source_tree(tmp_path)
    plan = installer.build_plan(home=home, codex_home=codex_home, platform=sys.platform, repo_root=source_root)
    first_target = Path(plan["files"][0]["target"])
    original_atomic_write = installer._atomic_write

    def fail_after_first_target(path, data, **kwargs):
        original_atomic_write(path, data, **kwargs)
        if Path(path) == first_target:
            raise OSError("injected initial install failure")

    monkeypatch.setattr(installer, "_atomic_write", fail_after_first_target)
    with pytest.raises(OSError, match="injected initial install failure"):
        installer.apply_plan(plan, repo_root=source_root)
    monkeypatch.setattr(installer, "_atomic_write", original_atomic_write)

    receipt_path = Path(plan["receipt_path"])
    pending_bytes = receipt_path.read_bytes()
    with pytest.raises(ValueError, match="recovery_required"):
        installer.apply_plan(plan, repo_root=source_root)
    assert receipt_path.read_bytes() == pending_bytes
    retry_plan = installer.build_plan(home=home, codex_home=codex_home, platform=sys.platform, repo_root=source_root)
    assert retry_plan["recovery_required"] is True
    assert retry_plan["writable"] is False
    with pytest.raises(ValueError, match="install_conflict"):
        installer.apply_plan(retry_plan, repo_root=source_root)
    assert receipt_path.read_bytes() == pending_bytes

    rollback = installer.rollback_receipt(
        receipt_path,
        {record["target"] for record in retry_plan["files"]},
        boundary=home,
    )
    assert rollback["rolled_back"] is True
    assert not receipt_path.exists()
    assert not first_target.exists()
    clean_plan = installer.build_plan(home=home, codex_home=codex_home, platform="linux", repo_root=source_root)
    assert clean_plan["recovery_required"] is False
    assert clean_plan["writable"] is True


def test_apply_rejects_changed_source_and_target_after_manifest_creation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_root = _test_source_tree(tmp_path)
    plan = installer.build_plan(
        home=home, codex_home=codex_home, platform="linux", repo_root=source_root,
    )
    (source_root / "scripts/titan-browser").write_bytes(b"changed source")

    with pytest.raises(ValueError, match="source_changed"):
        installer.apply_plan(plan, repo_root=source_root)
    assert not (home / ".local/bin/titan-browser").exists()


@pytest.mark.parametrize("mutate", ["source", "target"])
def test_apply_revalidates_unchanged_source_and_target(tmp_path, mutate):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_root = _test_source_tree(tmp_path)
    initial = installer.build_plan(home=home, codex_home=codex_home, platform="linux", repo_root=source_root)
    installer.apply_plan(initial, repo_root=source_root)
    reviewed = installer.build_plan(home=home, codex_home=codex_home, platform="linux", repo_root=source_root)
    record = next(item for item in reviewed["files"] if item["action"] == "unchanged")
    assert record["source"]
    if mutate == "source":
        (source_root / record["source"]).write_bytes(b"changed after review")
        expected = "source_changed"
    else:
        Path(record["target"]).write_bytes(b"operator change after review")
        expected = "target_changed"

    with pytest.raises(ValueError, match=expected):
        installer.apply_plan(reviewed, repo_root=source_root)


def test_complete_receipt_rollback_restores_previous_inventory_and_only_latest_actions(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_root = _test_source_tree(tmp_path)
    initial = installer.build_plan(home=home, codex_home=codex_home, platform="linux", repo_root=source_root)
    first_receipt = installer.apply_plan(initial, repo_root=source_root)
    first_files = first_receipt["files"]
    original_launcher = (source_root / "scripts/titan-browser").read_bytes()
    (source_root / "scripts/titan-browser").write_bytes(original_launcher + b"\n# second version\n")
    second_plan = installer.build_plan(home=home, codex_home=codex_home, platform="linux", repo_root=source_root)
    second_receipt = installer.apply_plan(second_plan, repo_root=source_root)
    changed = [item for item in second_receipt["transaction"]]
    assert len(changed) == 1

    rollback = installer.rollback_receipt(
        Path(second_plan["receipt_path"]),
        {record["target"] for record in second_plan["files"]},
        boundary=home,
    )
    restored_receipt = installer._read_prior_receipt(Path(second_plan["receipt_path"]))

    assert rollback["rolled_back"] is True
    assert Path(changed[0]["target"]).read_bytes() == original_launcher
    assert {item["target"] for item in restored_receipt["files"]} == {item["target"] for item in first_files}
    assert restored_receipt == first_receipt


def test_repeated_noop_applies_keep_receipt_bounded_and_rollback_restores_previous_receipt(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    installer.apply_plan(_build_plan(home))
    receipt_path = installer._receipt_path(home, sys.platform, None)
    prior_bytes = b""
    for _ in range(40):
        prior_bytes = receipt_path.read_bytes()
        plan = _build_plan(home)
        assert plan["writable"] is True
        installer.apply_plan(plan)
        current = receipt_path.read_bytes()
        assert len(current) <= installer.MAX_RECEIPT_BYTES
        assert installer._read_prior_receipt(receipt_path) is not None

    final_receipt = installer._read_prior_receipt(receipt_path)
    rollback = installer.rollback_receipt(
        receipt_path,
        {record["target"] for record in final_receipt["files"]},
        boundary=home,
    )

    assert rollback["rolled_back"] is True
    assert receipt_path.read_bytes() == prior_bytes


def test_tampered_previous_receipt_backup_blocks_rollback_before_target_changes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    codex_home.mkdir()
    installer.apply_plan(_build_plan(home))
    target = home / ".local/bin/titan-browser"
    original = target.read_bytes()
    source_launcher = ROOT / "scripts/titan-browser"
    source_root = _test_source_tree(tmp_path)
    (source_root / "scripts/titan-browser").write_bytes(source_launcher.read_bytes() + b"\n# update\n")
    plan = installer.build_plan(home=home, codex_home=codex_home, platform=sys.platform, repo_root=source_root)
    installer.apply_plan(plan, repo_root=source_root)
    receipt_path = Path(plan["receipt_path"])
    receipt = installer._read_prior_receipt(receipt_path)
    backup_path = receipt_path.parent / "backups" / f"receipt-{receipt['previous_receipt_backup']['sha256']}.bak"
    backup_path.write_bytes(b"tampered metadata")
    updated_target = target.read_bytes()
    allowed = {record["target"] for record in plan["files"]}

    with pytest.raises(ValueError, match="receipt_backup_conflict"):
        installer.rollback_receipt(receipt_path, allowed, boundary=home)

    assert target.read_bytes() == updated_target
    assert target.read_bytes() != original


def test_apply_rejects_target_created_after_reviewed_plan(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    plan = installer.build_plan(home=home, codex_home=codex_home, platform="linux")
    target = Path(next(record["target"] for record in plan["files"] if record["role"] == "launcher"))
    target.parent.mkdir(parents=True)
    target.write_text("operator file", encoding="utf-8")

    with pytest.raises(ValueError, match="install_conflict"):
        installer.apply_plan(plan)
    assert target.read_text(encoding="utf-8") == "operator file"


def test_reviewed_manifest_hash_rejects_modified_plan_file(tmp_path):
    home = tmp_path / "home"
    plan = _build_plan(home)
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    assert installer.read_reviewed_manifest(manifest, plan["manifest_sha256"]) == plan
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("true", "false", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_hash_mismatch"):
        installer.read_reviewed_manifest(manifest, plan["manifest_sha256"])


def test_installer_cli_applies_only_the_reviewed_manifest(tmp_path):
    home = tmp_path / "home"
    plan = _build_plan(home)
    manifest = tmp_path / "reviewed-plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(INSTALLER_PATH), "--home", str(home), "--codex-home", str(home / ".codex"),
            "--apply", "--manifest", str(manifest), "--manifest-sha256", plan["manifest_sha256"],
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)["receipt"]
    assert receipt["status"] == "complete"
    assert (home / ".local/bin/titan-browser").is_file()


def test_rollback_rejects_symlinked_receipt_parent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-receipt"
    outside.mkdir()
    (home / ".config").symlink_to(outside, target_is_directory=True)
    receipt = home / ".config/titan-browser/install-receipt.json"

    with pytest.raises(ValueError, match="receipt_conflict"):
        installer.rollback_receipt(receipt, set(), boundary=home)

    assert list(outside.iterdir()) == []


def test_installer_apply_rejects_symlinked_backup_parent(tmp_path):
    home = tmp_path / "home"
    plan = _build_plan(home)
    # Force one existing managed target so apply needs to create/use a backup.
    record = next(item for item in plan["files"] if item["role"] == "launcher")
    target = Path(record["target"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old managed launcher")
    record["action"] = "update"
    record["target_sha256"] = installer._sha256(b"old managed launcher")
    record["managed"] = True
    plan["manifest_sha256"] = installer._manifest_hash(plan)
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    backup_dir = Path(plan["receipt_path"]).parent / "backups"
    backup_dir.parent.mkdir(parents=True)
    backup_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="directory_conflict"):
        installer.apply_plan(plan)

    assert list(outside.iterdir()) == []


def test_installer_detects_conflicting_skill_without_overwriting_it(tmp_path):
    home = tmp_path / "home"
    conflict = home / ".claude/skills/browser-harness/SKILL.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("user-owned skill\n")
    original = conflict.read_bytes()

    plan = _build_plan(home)

    assert plan["writable"] is False
    assert str(conflict) in plan["conflicts"]
    assert conflict.read_bytes() == original


def test_installer_apply_receipt_and_rollback_operate_only_on_planned_targets(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    plan = _build_plan(home)
    receipt = installer.apply_plan(plan)
    assert receipt["files"]
    for record in receipt["files"]:
        target = Path(record["target"])
        assert installer._sha256(target.read_bytes()) == record["installed_sha256"]

    rollback = installer.rollback_receipt(
        Path(plan["receipt_path"]),
        {record["target"] for record in plan["files"]},
    )

    assert rollback["rolled_back"] is True
    assert all(not Path(record["target"]).exists() for record in receipt["files"])


def test_installer_rollback_preserves_files_modified_after_install(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    plan = _build_plan(home)
    receipt = installer.apply_plan(plan)
    target = Path(receipt["files"][0]["target"])
    target.write_bytes(b"edited after install")

    rollback = installer.rollback_receipt(
        Path(plan["receipt_path"]),
        {record["target"] for record in plan["files"]},
    )

    assert rollback["rolled_back"] is False
    assert target.read_bytes() == b"edited after install"
