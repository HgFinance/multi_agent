"""Transactional tests for the narrow AWS Hermes profile merger."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "aws_install_hermes_profiles.py"
SPEC = importlib.util.spec_from_file_location("aws_install_hermes_profiles", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def _release(root: Path) -> Path:
    ceo = root / "departments/00-ceo-office/hermes"
    trading = root / "departments/02-trading/hermes"
    ceo.mkdir(parents=True)
    trading.mkdir(parents=True)
    # Deliberately do not create a CEO config: it is not deployment-owned.
    (ceo / "SOUL.md").write_text(
        "# Repository CEO\n\n"
        "## Marked direct user PAPER-order lane\n\n"
        "new CEO managed behavior\n\n"
        "## Repository-only CEO tail\n\n"
        "must not enter runtime\n",
        encoding="utf-8",
    )
    (trading / "config.yaml").write_text(
        "mcp_servers:\n"
        "  user-paper-order:\n"
        "    url: http://paper-order-orchestrator-mcp:8046/mcp\n"
        "    headers:\n"
        "      Authorization: Bearer ${MCP_TRADING_ORDER_API_KEY}\n"
        "    enabled: true\n"
        "  release-only-server:\n"
        "    url: http://must-not-merge.invalid/mcp\n",
        encoding="utf-8",
    )
    (trading / "SOUL.md").write_text(
        "# Repository Trading\n\n"
        "## Marked direct user PAPER-order interpretation lane\n\n"
        "new Trading managed behavior\n\n"
        "## Repository-only Trading tail\n\n"
        "must not enter runtime\n",
        encoding="utf-8",
    )
    return root


def _runtime(root: Path) -> dict[Path, bytes]:
    ceo = root / "profiles/ceo-agent"
    trading = root / "profiles/trading-department"
    ceo.mkdir(parents=True)
    trading.mkdir(parents=True)

    (ceo / "config.yaml").write_text(
        "provider: host-ceo-provider\n"
        "discord:\n"
        "  enabled: true\n"
        "onboarding:\n"
        "  completed: true\n"
        "platform_toolsets:\n"
        "  - host-ceo-tools\n",
        encoding="utf-8",
    )
    (ceo / "SOUL.md").write_text(
        "# Runtime CEO\n\n"
        "## Host-only CEO preface\n\n"
        "preserve CEO preface exactly\n\n"
        "## Marked direct user PAPER-order lane\n\n"
        "obsolete CEO managed behavior\n\n"
        "## Host-only CEO tail\n\n"
        "preserve CEO tail exactly\n",
        encoding="utf-8",
    )
    (trading / "config.yaml").write_text(
        "provider: host-trading-provider\n"
        "gateway:\n"
        "  enabled: true\n"
        "  host_setting: preserve-gateway\n"
        "platform_toolsets:\n"
        "  - host-trading-tools\n"
        "unknown_runtime_setting:\n"
        "  nested: preserve-unknown\n"
        "mcp_servers:\n"
        "  existing-private-server:\n"
        "    url: http://existing-private:9000/mcp\n"
        "    headers:\n"
        "      Authorization: Bearer preserve-existing-value\n"
        "  user-paper-order:\n"
        "    url: http://obsolete:1/mcp\n"
        "    headers:\n"
        "      Authorization: Bearer obsolete-value\n",
        encoding="utf-8",
    )
    (trading / "SOUL.md").write_text(
        "# Runtime Trading\n\n"
        "## Host-only Trading preface\n\n"
        "preserve Trading preface exactly\n\n"
        "## Marked direct user PAPER-order interpretation lane\n\n"
        "obsolete Trading managed behavior\n\n"
        "## Host-only Trading tail\n\n"
        "preserve Trading tail exactly\n",
        encoding="utf-8",
    )

    for profile in (ceo, trading):
        (profile / "auth.json").write_bytes(
            b'{"credential":"already-authenticated"}\n'
        )
        (profile / "memories").mkdir()
        (profile / "sessions").mkdir()
        (profile / "memories/durable.json").write_text(
            f"{profile.name}-memory", encoding="utf-8"
        )
        (profile / "sessions/active.json").write_text(
            f"{profile.name}-session", encoding="utf-8"
        )
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _private_env(path: Path, key: str) -> Path:
    path.write_text(
        "UNRELATED=preserved\n" f"MCP_TRADING_ORDER_API_KEY={key}\n",
        encoding="utf-8",
    )
    return path


def _assert_bytes(expected: dict[Path, bytes]) -> None:
    for path, content in expected.items():
        assert path.read_bytes() == content


def _managed_files(runtime: Path) -> dict[Path, bytes]:
    return {
        runtime / "profiles" / profile / filename: (
            runtime / "profiles" / profile / filename
        ).read_bytes()
        for profile, filename in installer.PROFILE_TARGETS
    }


def test_merge_preserves_host_settings_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release = _release(tmp_path / "release")
    runtime = tmp_path / "runtime"
    original = _runtime(runtime)
    backup_one = tmp_path / "backup-one"
    backup_two = tmp_path / "backup-two"
    backup_one.mkdir(mode=0o700)
    backup_two.mkdir(mode=0o700)
    key = 'S3cure-token_with:/+.#"\\\'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    runtime_env = _private_env(tmp_path / "runtime.env", key)
    ceo_config = runtime / "profiles/ceo-agent/config.yaml"

    installer.install_profiles(
        release_root=release,
        runtime_env=runtime_env,
        runtime_root=runtime,
        backup_dir=backup_one,
    )
    first_install = _managed_files(runtime)

    assert capsys.readouterr() == ("", "")
    assert ceo_config.read_bytes() == original[ceo_config]
    trading_config = runtime / "profiles/trading-department/config.yaml"
    trading = yaml.safe_load(trading_config.read_text(encoding="utf-8"))
    assert trading["provider"] == "host-trading-provider"
    assert trading["gateway"] == {
        "enabled": True,
        "host_setting": "preserve-gateway",
    }
    assert trading["platform_toolsets"] == ["host-trading-tools"]
    assert trading["unknown_runtime_setting"] == {"nested": "preserve-unknown"}
    assert trading["mcp_servers"]["existing-private-server"] == {
        "url": "http://existing-private:9000/mcp",
        "headers": {"Authorization": "Bearer preserve-existing-value"},
    }
    assert "release-only-server" not in trading["mcp_servers"]
    order_server = trading["mcp_servers"]["user-paper-order"]
    assert order_server["url"].endswith(":8046/mcp")
    assert order_server["headers"]["Authorization"] == f"Bearer {key}"
    assert order_server["enabled"] is True

    ceo_soul = (runtime / "profiles/ceo-agent/SOUL.md").read_text(encoding="utf-8")
    trading_soul = (runtime / "profiles/trading-department/SOUL.md").read_text(
        encoding="utf-8"
    )
    assert "preserve CEO preface exactly" in ceo_soul
    assert "preserve CEO tail exactly" in ceo_soul
    assert "new CEO managed behavior" in ceo_soul
    assert "obsolete CEO managed behavior" not in ceo_soul
    assert "Repository-only CEO tail" not in ceo_soul
    assert "preserve Trading preface exactly" in trading_soul
    assert "preserve Trading tail exactly" in trading_soul
    assert "new Trading managed behavior" in trading_soul
    assert "obsolete Trading managed behavior" not in trading_soul
    assert "Repository-only Trading tail" not in trading_soul

    backup_files = {
        path.relative_to(backup_one).as_posix()
        for path in backup_one.rglob("*")
        if path.is_file()
    }
    assert backup_files == {
        "manifest.json",
        "profiles/ceo-agent/SOUL.md",
        "profiles/trading-department/config.yaml",
        "profiles/trading-department/SOUL.md",
    }
    assert key not in (backup_one / "manifest.json").read_text(encoding="utf-8")
    if os.name != "nt":
        for path in first_install:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    installer.install_profiles(
        release_root=release,
        runtime_env=runtime_env,
        runtime_root=runtime,
        backup_dir=backup_two,
    )

    assert _managed_files(runtime) == first_install
    assert ceo_config.read_bytes() == original[ceo_config]
    installer.restore_profiles(runtime_root=runtime, backup_dir=backup_two)
    assert _managed_files(runtime) == first_install
    installer.restore_profiles(runtime_root=runtime, backup_dir=backup_one)
    _assert_bytes(original)


def test_current_repository_fragments_merge_into_runtime_without_broad_replacement(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    original = _runtime(runtime)
    key = "repository-contract-key-0123456789ABCDEF"

    rendered = installer._render_targets(
        release_root=ROOT,
        runtime_root=runtime,
        mcp_key=key,
    )

    assert set(rendered) == set(installer.PROFILE_TARGETS)
    trading = yaml.safe_load(rendered[("trading-department", "config.yaml")])
    assert trading["gateway"]["host_setting"] == "preserve-gateway"
    assert trading["mcp_servers"]["existing-private-server"]["url"] == (
        "http://existing-private:9000/mcp"
    )
    assert trading["mcp_servers"]["user-paper-order"]["headers"][
        "Authorization"
    ] == f"Bearer {key}"
    ceo_config = runtime / "profiles/ceo-agent/config.yaml"
    assert ceo_config.read_bytes() == original[ceo_config]
    assert b"hgfinance.user-paper-order-request.v1" in rendered[
        ("ceo-agent", "SOUL.md")
    ]
    assert b"hgfinance.user-paper-order-interpretation.v1" in rendered[
        ("trading-department", "SOUL.md")
    ]


def test_missing_runtime_target_fails_before_backup_or_other_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release = _release(tmp_path / "release")
    runtime = tmp_path / "runtime"
    original = _runtime(runtime)
    missing = runtime / "profiles/trading-department/config.yaml"
    missing.unlink()
    original.pop(missing)
    backup = tmp_path / "backup"
    backup.mkdir()
    runtime_env = _private_env(tmp_path / "runtime.env", "m7-" * 16)

    result = installer.main(
        [
            "install",
            "--release-root",
            str(release),
            "--runtime-env",
            str(runtime_env),
            "--runtime-root",
            str(runtime),
            "--backup-dir",
            str(backup),
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "ERROR: AWS Hermes profile operation failed\n"
    assert list(backup.iterdir()) == []
    assert not missing.exists()
    _assert_bytes(original)


def test_invalid_private_key_fails_before_backup_or_runtime_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release = _release(tmp_path / "release")
    runtime = tmp_path / "runtime"
    original = _runtime(runtime)
    backup = tmp_path / "backup"
    backup.mkdir()
    rejected = "secret_here_that_must_never_be_disclosed"
    runtime_env = _private_env(tmp_path / "runtime.env", rejected)

    result = installer.main(
        [
            "install",
            "--release-root",
            str(release),
            "--runtime-env",
            str(runtime_env),
            "--runtime-root",
            str(runtime),
            "--backup-dir",
            str(backup),
        ]
    )

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ERROR: AWS Hermes profile operation failed\n"
    assert rejected not in captured.err
    assert list(backup.iterdir()) == []
    _assert_bytes(original)


def test_partial_install_failure_restores_all_three_managed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _release(tmp_path / "release")
    runtime = tmp_path / "runtime"
    original = _runtime(runtime)
    backup = tmp_path / "backup"
    backup.mkdir()
    runtime_env = _private_env(tmp_path / "runtime.env", "v7-" * 16)
    real_atomic_write = installer._atomic_write
    failed_once = False

    def fail_once(path: Path, data: bytes, *, mode: int) -> None:
        nonlocal failed_once
        target = runtime / "profiles/trading-department/config.yaml"
        if path == target and not failed_once:
            failed_once = True
            raise OSError("simulated atomic replace failure")
        real_atomic_write(path, data, mode=mode)

    monkeypatch.setattr(installer, "_atomic_write", fail_once)

    with pytest.raises(installer.ProfileInstallError, match="installation failed"):
        installer.install_profiles(
            release_root=release,
            runtime_env=runtime_env,
            runtime_root=runtime,
            backup_dir=backup,
        )

    assert failed_once is True
    _assert_bytes(original)


def test_corrupt_backup_is_rejected_before_any_restore_mutation(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    runtime = tmp_path / "runtime"
    _runtime(runtime)
    backup = tmp_path / "backup"
    backup.mkdir()
    runtime_env = _private_env(tmp_path / "runtime.env", "z8-" * 16)
    installer.install_profiles(
        release_root=release,
        runtime_env=runtime_env,
        runtime_root=runtime,
        backup_dir=backup,
    )
    installed = _managed_files(runtime)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"][-1]["files"][-1]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(installer.ProfileInstallError, match="checksum failed"):
        installer.restore_profiles(runtime_root=runtime, backup_dir=backup)

    _assert_bytes(installed)
