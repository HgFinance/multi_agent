"""Deterministic, privacy-safe offline gates for approved QA code feedback.

The runner consumes the existing redacted feedback ledger and writes results
through its existing benchmark transition.  It never reads prompts, answers,
provider payloads, or credentials, and it never treats the original failing
observation as proof that a fix works.  Only explicitly registered executable
contract suites can pass; unknown finding classes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orchestration.langsmith_feedback import (
    FeedbackLedger,
    TraceObservation,
    attribute_workflow_bottleneck,
)
from orchestration.llm_observability import (
    trace_correlation_metadata,
    worker_graph_trace_config,
)
from orchestration.semantic_qa import evaluate_prompt_answer

BENCHMARK_VERSION = "qa-privacy-safe-contract-v1"
D5_ADMISSION_BENCHMARK_VERSION = "d5-improvement-admission-v1"


def _correlation_suite() -> dict[str, Any]:
    first = trace_correlation_metadata({})
    second = trace_correlation_metadata({})
    config = worker_graph_trace_config(stage="qa", worker_id="benchmark-worker")
    keys = ("request_id", "root_id", "task_id", "trace_id")
    passed = (
        all(first.get(key) for key in keys)
        and all(config["metadata"].get(key) for key in keys)
        and first["trace_id"] != second["trace_id"]
        and config["metadata"].get("raw_payloads_sent") is False
    )
    return {"suite": "correlation", "passed": passed}


def _latency_attribution_suite() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        database_path = Path(directory) / "kanban.db"
        with sqlite3.connect(database_path) as database:
            database.execute(
                """CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, body TEXT, assignee TEXT,
                    created_at INTEGER NOT NULL, started_at INTEGER,
                    completed_at INTEGER, idempotency_key TEXT
                )"""
            )
            database.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ("t_root", "request", "ceo-agent", 1, 1, 31, "req-1"),
                    (
                        "t_risk",
                        "workflow_root_task_id=t_root\nworkflow_role=primary",
                        "risk-management",
                        35,
                        36,
                        97,
                        "t_root:primary:risk-management",
                    ),
                ),
            )
        result = attribute_workflow_bottleneck(
            TraceObservation(
                source_run_id="benchmark-root",
                name="hgfinance.user-query",
                status="completed",
                started_at=None,
                ended_at=None,
                metadata={
                    "request_id": "req-1",
                    "root_id": "t_root",
                    "task_id": "t_root",
                    "trace_id": "trace-1",
                    "stage": "ceo-ingress",
                    "trace_kind": "workflow_root",
                    "latency_scope": "end_to_end",
                    "latency_ms": 98_000,
                    "raw_payloads_sent": False,
                },
            ),
            kanban_db_path=str(database_path),
        )
    passed = (
        result.department == "risk-management"
        and result.metadata.get("latency_attribution_status") == "MEASURED"
        and result.metadata.get("primary_bottleneck_duration_ms") == 61_000
    )
    return {"suite": "latency_attribution", "passed": passed}


def _semantic_suite() -> dict[str, Any]:
    quality = evaluate_prompt_answer(
        "권위 근거와 기준 시각을 포함해 위험 계획을 설명해줘.",
        "기준 시각은 2026-08-26T00:00:00Z이며 근거는 task:t_risk입니다. "
        "권위 데이터가 없어 수치는 생성하지 않고 DEFER합니다. 미확인: 시장 스냅샷.",
    )
    metadata = quality.as_metadata()
    passed = (
        quality.verdict == "PASS"
        and metadata.get("raw_payloads_sent") is False
        and "권위" not in json.dumps(metadata, ensure_ascii=False)
    )
    return {"suite": "semantic_answer", "passed": passed}


_SUITES: dict[str, Callable[[], dict[str, Any]]] = {
    "CORRELATION_METADATA_MISSING": _correlation_suite,
    "LATENCY_ABOVE_THRESHOLD": _latency_attribution_suite,
    "SEMANTIC_QA_FAILED": _semantic_suite,
    "SEMANTIC_QA_SCORE_LOW": _semantic_suite,
}


def _failed_suite_result(suite_name: str, error_code: str) -> dict[str, Any]:
    """Return a bounded failure record without persisting exception details."""

    return {
        "suite": suite_name,
        "passed": False,
        "status": "ERROR",
        "error_code": error_code,
    }


def _run_suite(suite_name: str) -> dict[str, Any]:
    """Isolate one suite so its exception cannot abort sibling suites."""

    try:
        result = _SUITES[suite_name]()
    except Exception:  # noqa: BLE001 - one broken gate must not stop the cycle.
        return _failed_suite_result(suite_name, "suite_exception")
    if not isinstance(result, Mapping):
        return _failed_suite_result(suite_name, "invalid_suite_result")
    normalized = dict(result)
    normalized["suite"] = suite_name
    normalized["passed"] = bool(result.get("passed"))
    return normalized


def _candidate_failure(candidate: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Build a redacted deterministic failure result for candidate isolation."""

    artifact_id = str(candidate.get("artifact_id") or "")
    improvement_type = str(candidate.get("improvement_type") or "")
    manifest = {
        "schema_version": BENCHMARK_VERSION,
        "artifact_id": artifact_id,
        "improvement_type": improvement_type,
        "finding_codes": [],
        "suites": [],
        "errors": ["candidate_exception"],
        "raw_payloads_sent": False,
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return False, f"sha256:{digest}", "privacy-safe benchmark failed: candidate_exception"


def _run_candidate(candidate: Mapping[str, Any]) -> tuple[bool, str, str]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("source") == "memo_harness_d5":
        return _run_d5_candidate(candidate)
    codes = sorted({str(value).upper() for value in candidate.get("finding_codes") or ()})
    suite_names = [code for code in codes if code in _SUITES]
    results = [_run_suite(name) for name in suite_names]
    errors = [
        f"{result['suite']}:{result.get('error_code')}"
        if result.get("status") == "ERROR"
        else result["suite"]
        for result in results
        if not result["passed"]
    ]
    if not results:
        errors.append("no_registered_executable_suite")
    manifest = {
        "schema_version": BENCHMARK_VERSION,
        "artifact_id": str(candidate.get("artifact_id") or ""),
        "improvement_type": str(candidate.get("improvement_type") or ""),
        "finding_codes": codes,
        "suites": results,
        "errors": errors,
        "raw_payloads_sent": False,
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    passed = not errors
    summary = (
        "privacy-safe deterministic contract suites passed"
        if passed
        else "privacy-safe benchmark failed: " + ",".join(errors)
    )
    return passed, f"sha256:{digest}", summary


def _run_d5_candidate(candidate: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Admit a payload-free D5 candidate to the central regression queue.

    This gate checks the candidate envelope only. It deliberately does not
    claim that a router or QA implementation has been fixed; that proof is
    supplied by the named regression suite after human approval.
    """

    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    codes = sorted({str(value).upper() for value in candidate.get("finding_codes") or ()})
    errors: list[str] = []
    if metadata.get("schema_version") != "hgfinance.memo-harness.d5-improvement.v1":
        errors.append("d5_schema_missing")
    if metadata.get("raw_payloads_sent") is not False:
        errors.append("payload_redaction_unproven")
    if not str(metadata.get("root_id") or "").strip():
        errors.append("root_identity_missing")
    if not str(metadata.get("task_id") or "").strip():
        errors.append("qa_identity_missing")
    target = str(metadata.get("regression_test_target") or "")
    if not target.startswith("tests/"):
        errors.append("regression_target_missing")
    case_hash = str(metadata.get("regression_case_hash") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", case_hash):
        errors.append("regression_case_hash_missing")
    if not codes or not all(code.startswith("D5_") for code in codes):
        errors.append("d5_finding_code_missing")
    if "D5_ROUTING_MISMATCH" in codes:
        if metadata.get("route_verification") != "mismatch":
            errors.append("route_verification_missing")
        if not metadata.get("expected_primary_profiles") or not metadata.get(
            "actual_primary_profiles"
        ):
            errors.append("route_profile_sets_missing")
    manifest = {
        "schema_version": D5_ADMISSION_BENCHMARK_VERSION,
        "artifact_id": str(candidate.get("artifact_id") or ""),
        "finding_codes": codes,
        "category": str(metadata.get("category") or ""),
        "candidate_type": str(metadata.get("candidate_type") or ""),
        "regression_test_target": target,
        "regression_case_hash": case_hash,
        "errors": errors,
        "raw_payloads_sent": False,
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    summary = (
        "D5 candidate envelope passed; central router regression remains pending"
        if not errors
        else "D5 candidate admission failed: " + ",".join(errors)
    )
    return not errors, f"sha256:{digest}", summary


def run_pending_feedback_benchmarks(
    ledger: FeedbackLedger, *, limit: int = 50
) -> dict[str, int]:
    """Run only non-skill PENDING jobs with registered contract suites."""

    passed = failed = skipped = 0
    for candidate in ledger.benchmark_candidates(limit):
        if candidate.get("benchmark_status") != "PENDING":
            skipped += 1
            continue
        if candidate.get("improvement_type") in {"SKILL_CREATE", "SKILL_EVOLVE"}:
            skipped += 1
            continue
        try:
            ok, report_ref, summary = _run_candidate(candidate)
        except Exception:  # noqa: BLE001 - isolate a malformed candidate.
            ok, report_ref, summary = _candidate_failure(candidate)
        candidate_metadata = candidate.get("metadata")
        benchmark_version = (
            D5_ADMISSION_BENCHMARK_VERSION
            if isinstance(candidate_metadata, Mapping)
            and candidate_metadata.get("source") == "memo_harness_d5"
            else BENCHMARK_VERSION
        )
        try:
            updated = ledger.update_benchmark(
                str(candidate["artifact_id"]),
                status="PASSED" if ok else "FAILED",
                benchmark_id=benchmark_version,
                score=1.0 if ok else 0.0,
                report_ref=report_ref,
                result_summary=summary,
            )
        except Exception:  # noqa: BLE001 - continue with later candidates.
            updated = False
        if updated and ok:
            passed += 1
        elif updated:
            failed += 1
    return {"passed": passed, "failed": failed, "skipped": skipped}


__all__ = [
    "BENCHMARK_VERSION",
    "D5_ADMISSION_BENCHMARK_VERSION",
    "run_pending_feedback_benchmarks",
]
