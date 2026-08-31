from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_package_spec = importlib.util.spec_from_file_location(
    "scripts",
    ROOT / "scripts/__init__.py",
    submodule_search_locations=[str(ROOT / "scripts")],
)
assert _package_spec is not None and _package_spec.loader is not None
_scripts_package = importlib.util.module_from_spec(_package_spec)
sys.modules["scripts"] = _scripts_package
_package_spec.loader.exec_module(_scripts_package)

from scripts.release_readiness_audit import build_report


def test_release_audit_keeps_unverified_gates_blocked() -> None:
    report = build_report(ROOT)
    findings = report["findings"]

    assert report["overall_release_status"] == "BLOCKED"
    assert findings["paper_orders"]["status"] == "PASS"
    assert findings["quant_bottleneck"]["status"] == "PASS"
    assert findings["garbage_collection"]["status"] == "PASS"
    assert findings["stress_evidence"]["status"] == "NOT_VERIFIED"
    assert findings["stress_evidence"]["scenario_count"] == 10
    assert findings["latency_sla"]["status"] == "NOT_VERIFIED"
    assert findings["runtime_e2e"]["status"] == "NOT_VERIFIED"


def test_release_audit_records_conditional_rule_and_dependency_gaps() -> None:
    findings = build_report(ROOT)["findings"]

    assert findings["dependency_hygiene"]["status"] == "PASS"
    assert findings["dependency_hygiene"]["direct_requirements_present"] is True
    assert findings["dependency_hygiene"]["lock_present"] is True
    assert findings["dependency_hygiene"]["sbom_present"] is True
    assert findings["dependency_hygiene"]["cve_clean"] is True
    assert findings["conditional_rules"]["status"] == "PARTIAL"
    assert findings["conditional_rules"]["contract_evidence_present"] is True
