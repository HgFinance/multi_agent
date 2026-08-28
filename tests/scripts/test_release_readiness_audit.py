from __future__ import annotations

from pathlib import Path

from scripts.release_readiness_audit import build_report


ROOT = Path(__file__).resolve().parents[2]


def test_release_audit_keeps_unverified_gates_blocked() -> None:
    report = build_report(ROOT)
    findings = report["findings"]

    assert report["overall_release_status"] == "BLOCKED"
    assert findings["paper_orders"]["status"] == "PASS"
    assert findings["quant_bottleneck"]["status"] == "PASS"
    assert findings["garbage_collection"]["status"] == "PASS"
    assert findings["stress_evidence"]["status"] == "BLOCKED"
    assert findings["stress_evidence"]["scenario_count"] == 10
    assert findings["latency_sla"]["status"] == "NOT_VERIFIED"
    assert findings["runtime_e2e"]["status"] == "NOT_VERIFIED"


def test_release_audit_records_conditional_rule_and_dependency_gaps() -> None:
    findings = build_report(ROOT)["findings"]

    assert findings["dependency_hygiene"]["status"] == "PARTIAL"
    assert findings["dependency_hygiene"]["direct_requirements_present"] is True
    assert findings["conditional_rules"]["status"] == "PARTIAL"
    assert findings["conditional_rules"]["contract_evidence_present"] is True
