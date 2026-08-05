"""Risk·QA Worker 역할/기술 프로필이 실행 계약과 함께 유지되는지 검증한다."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for department_dir in (ROOT / "departments/03-risk", ROOT / "departments/06-ai-qa-audit"):
    if str(department_dir) not in sys.path:
        sys.path.insert(0, str(department_dir))

import qa_employee_workers
import risk_employee_workers


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["test:evidence"],"escalate":false}'


def _assert_profile(profile: object) -> None:
    assert profile is not None
    data = profile.as_dict()
    assert data["stack"]
    assert data["usage"]
    assert data["inputs"]
    assert data["metrics"]
    assert data["write_capability"] == "NONE"


def test_every_risk_and_qa_worker_has_an_executable_technology_profile() -> None:
    for module in (risk_employee_workers, qa_employee_workers):
        worker_ids = [spec.worker_id for spec in module.WORKER_SPECS]
        assert len(worker_ids) == len(set(worker_ids))
        for spec in module.WORKER_SPECS:
            _assert_profile(spec.tech_profile)


def test_risk_runtime_exposes_profiles_and_conditional_roles_remain_safe() -> None:
    report = risk_employee_workers.run_employee_workers(
        {"trading_state": "ENABLED", "assessment": {"verdict": "approve"}},
        llm=_llm,
    )
    expected = {spec.worker_id for spec in risk_employee_workers.WORKER_SPECS}
    assert set(report["runtime"]["technology_profiles"]) == expected
    assert all(item["technology"]["write_capability"] == "NONE" for item in report["workers"])
    assert "compliance-policy-worker" in report["not_executed"]
    assert "derivatives-counterparty-worker" in report["not_executed"]


def test_qa_runtime_exposes_profiles_and_worker_reports() -> None:
    report = qa_employee_workers.run_employee_workers(
        {"assessment": {"decision": "PASS", "claim_checks": []}},
        llm=_llm,
    )
    expected = {spec.worker_id for spec in qa_employee_workers.WORKER_SPECS}
    assert set(report["runtime"]["technology_profiles"]) == expected
    assert report["executed"] == ["evidence-qa-worker"]
    assert report["workers"][0]["technology"]["write_capability"] == "NONE"
    assert "hallucination-critic-worker" in report["not_executed"]

