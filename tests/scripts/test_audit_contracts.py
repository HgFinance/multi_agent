"""Tests for the repository/runtime contract audit entry point."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_contracts.py"
_SPEC = importlib.util.spec_from_file_location("audit_contracts_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
audit_contracts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_contracts)


def test_repository_audit_allows_direct_runtime_skill_owner() -> None:
    findings = audit_contracts.Findings()

    audit_contracts.audit_repository(findings)

    assert findings.rows == []


def test_runtime_audit_accepts_direct_profile_skill_lists(monkeypatch) -> None:
    profiles = "\n".join(sorted(audit_contracts.CANONICAL_PROFILES))
    mounted = "\n".join(
        f"/opt/shared-skills/{skill}" for skill in audit_contracts.CANONICAL_SKILLS
    )

    def fake_docker(container: str, command: list[str], timeout: int = 60) -> str:
        del timeout
        if container != audit_contracts.DISPATCHER:
            return ""
        if command == ["sh", "-c", "ls /opt/data/profiles/"]:
            return profiles
        if command == ["sh", "-c", "find /opt/shared-skills -name SKILL.md -printf '%h\\n'"]:
            return mounted
        if command == ["sh", "-c", "env | grep -c '^ANTHROPIC_API_KEY='"]:
            return "1"
        if command[:2] == ["python3", "-c"]:
            path = command[2]
            if "hr-department/config.yaml" in path or "trading-department/config.yaml" in path:
                return "CONFIG_OK:DIRECT"
            return "CONFIG_OK:EXTERNAL"
        raise AssertionError(f"unexpected docker command: {command}")

    monkeypatch.setattr(audit_contracts, "_docker", fake_docker)
    findings = audit_contracts.Findings()

    audit_contracts.audit_runtime(findings)

    assert findings.rows == []


def test_runtime_probe_uses_hermes_loader_and_reports_loader_errors() -> None:
    probe = audit_contracts._runtime_config_probe(
        "/opt/data/profiles/hr-department/config.yaml"
    )

    assert "from hermes_cli.config import fast_safe_load" in probe
    assert "yaml.safe_load" not in probe
    assert "CONFIG_LOADER_UNAVAILABLE:" in probe
    assert "CONFIG_ERROR:" in probe
