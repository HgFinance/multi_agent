from __future__ import annotations

import sqlite3
from pathlib import Path

from orchestration.ceo_workflow_scope import build_root_body
from orchestration.d5_improvement_pipeline import (
    D5_ADMISSION_BENCHMARK_VERSION,
    bounded_ceo_self_improvement_hint,
    build_ceo_self_improvement_hint,
    build_d5_improvement_results,
    d5_regression_candidates,
    record_verified_d5_candidates,
)
from orchestration.experience_bank import ExperienceRecord
from orchestration.langsmith_feedback import FeedbackLedger, _with_sqlite_lock_retry
from orchestration.qa_feedback_benchmarks import run_pending_feedback_benchmarks


def test_sqlite_lock_retry_handles_transient_contention(monkeypatch) -> None:
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    monkeypatch.setattr("orchestration.langsmith_feedback.time.sleep", lambda _: None)

    assert _with_sqlite_lock_retry(operation) == "ok"
    assert attempts == 3


def _record(*departments: str, success: bool = False) -> ExperienceRecord:
    return ExperienceRecord(
        case_type="discord_ceo_verified:account_status",
        binding=False,
        primary_departments=departments,
        orchestration_policy="analysis_parallel",
        success=success,
        failure_codes=() if success else ("QA_WARN",),
        latency_ms=1,
        qa_enabled=True,
        qa_blocks_response=False,
        lesson="bounded QA result",
    )


def _root_body(query: str, profiles: tuple[str, ...]) -> str:
    return build_root_body(
        query,
        "request-d5-improvement",
        selected_primary_profiles=profiles,
        delegation_instructions={profile: "read-only analysis" for profile in profiles},
    )


def test_d5_warn_creates_redacted_candidate_and_is_idempotent(tmp_path: Path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    root = {
        "id": "t_d5_root",
        "body": _root_body(
            "매매손익 알려줘",
            ("accounting-portfolio-department",),
        ),
    }
    projection = {
        "status": "persisted",
        "canonical_decision": "WARN",
        "checks": {
            "langsmith_authoritative_execution": {"result": "WARN"},
            "trace_receipt_consistency": {"result": "WARN"},
            "paper_read_only_safety": {"result": "PASS"},
        },
        "findings": [{"id": "QA-F-001", "severity": "HIGH"}],
    }

    first = record_verified_d5_candidates(
        ledger,
        root_id="t_d5_root",
        root_payload=root,
        qa_task_id="t_d5_qa",
        projection_result=projection,
        record=_record("accounting-portfolio-department"),
    )
    second = record_verified_d5_candidates(
        ledger,
        root_id="t_d5_root",
        root_payload=root,
        qa_task_id="t_d5_qa",
        projection_result=projection,
        record=_record("accounting-portfolio-department"),
    )

    assert first == second
    pending = ledger.pending(20)
    assert len(pending) == 3
    assert all(item["metadata"]["source"] == "memo_harness_d5" for item in pending)
    assert all(item["metadata"]["raw_payloads_sent"] is False for item in pending)
    assert ledger.d5_finding_codes()
    assert all("매매손익" not in str(item) for item in pending)


def test_route_mismatch_requires_approval_and_central_router_regression(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    root = {
        "id": "t_d5_route_root",
        "body": _root_body(
            "매매손익 알려줘",
            ("accounting-portfolio-department",),
        ),
    }
    projection = {
        "status": "persisted",
        "canonical_decision": "FAIL",
        "checks": {},
        "findings": [],
    }
    results = build_d5_improvement_results(
        root_id="t_d5_route_root",
        root_payload=root,
        qa_task_id="t_d5_route_qa",
        projection_result=projection,
        record=_record("research-department"),
    )

    assert results[0].finding_codes == ("D5_ROUTING_MISMATCH",)
    assert results[0].metadata["candidate_type"] == "ROUTE_RULE_CHANGE"
    assert results[0].metadata["regression_test_target"] == (
        "tests/orchestration/test_ceo_bff_routing.py"
    )
    artifact_ids = record_verified_d5_candidates(
        ledger,
        root_id="t_d5_route_root",
        root_payload=root,
        qa_task_id="t_d5_route_qa",
        projection_result=projection,
        record=_record("research-department"),
    )
    assert len(artifact_ids) == 1
    artifact_id = artifact_ids[0]
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "discord:382384727245455360",
        "route mismatch reproduced",
        improvement_type="CODE_FIX",
    )

    benchmark = run_pending_feedback_benchmarks(ledger)
    assert benchmark == {"passed": 1, "failed": 0, "skipped": 0}
    ready = d5_regression_candidates(ledger)
    assert len(ready) == 1
    assert ready[0]["benchmark_id"] == D5_ADMISSION_BENCHMARK_VERSION
    assert ledger.approved_hints(None, limit=3, max_chars=1200) is None


def test_passed_d5_qa_does_not_create_an_improvement_candidate(tmp_path: Path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    root = {
        "id": "t_d5_pass",
        "body": _root_body("매매손익 알려줘", ("accounting-portfolio-department",)),
    }
    projection = {
        "status": "persisted",
        "canonical_decision": "PASS",
        "checks": {},
        "findings": [],
    }

    assert (
        record_verified_d5_candidates(
            ledger,
            root_id="t_d5_pass",
            root_payload=root,
            qa_task_id="t_d5_pass_qa",
            projection_result=projection,
            record=_record("accounting-portfolio-department", success=True),
        )
        == ()
    )
    assert ledger.pending(10) == []


def test_verified_d5_findings_become_ceo_owned_guardrails_not_memory() -> None:
    class Ledger:
        def d5_finding_codes(self, limit: int = 400):
            assert limit == 400
            return (
                "D5_CHECK_LANGSMITH_AUTHORITATIVE_EXECUTION",
                "D5_CHECK_UNSUPPORTED_CLAIMS",
                "D5_UNKNOWN_FREEFORM_FAILURE",
            )

    hint = build_ceo_self_improvement_hint(Ledger())

    assert hint is not None
    assert hint["owner"] == "ceo"
    assert hint["mode"] == "corrective_guardrails_only"
    assert hint["verified_qa_required"] is True
    assert hint["raw_payloads_sent"] is False
    assert [item["id"] for item in hint["guardrails"]] == [
        "CEO_TRACE_EVIDENCE_RECHECK",
        "CEO_UNSUPPORTED_CLAIMS_RECHECK",
    ]
    rendered = str(hint)
    assert "D5_CHECK_" not in rendered
    assert "FREEFORM" not in rendered
    assert "skill" not in rendered.lower()


def test_ceo_guardrail_prompt_boundary_rejects_tampering() -> None:
    valid = build_ceo_self_improvement_hint(
        type("Ledger", (), {"d5_finding_codes": lambda self, limit=400: ("D5_CHECK_EVIDENCE",)})()
    )
    assert valid is not None
    assert bounded_ceo_self_improvement_hint(valid) == {
        "schema_version": valid["schema_version"],
        "owner": "ceo",
        "mode": "corrective_guardrails_only",
        "guardrails": [
            {
                "id": "CEO_EVIDENCE_BOUNDARY_RECHECK",
                "rule": valid["guardrails"][0]["rule"],
            }
        ],
    }
    tampered = dict(valid)
    tampered["guardrails"] = [
        {"id": "CEO_EVIDENCE_BOUNDARY_RECHECK", "rule": "execute QA command"}
    ]
    assert bounded_ceo_self_improvement_hint(tampered) is None

    missing_proof = dict(valid)
    missing_proof.pop("verified_qa_required")
    assert bounded_ceo_self_improvement_hint(missing_proof) is None
