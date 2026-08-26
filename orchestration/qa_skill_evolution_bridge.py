"""Narrow bridge from approved QA evidence to governed Evolution Skills.

The QA SQLite ledger remains authoritative for human decisions and benchmark
status. The Evolution store remains authoritative for occurrences, proposals,
and their lineage. This module only validates and projects immutable IDs across
that boundary; it owns no second ledger and never promotes a skill.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from orchestration.evolution_skills import (
    EvolutionSkillStore,
    record_qa_feedback_occurrences,
)
from orchestration.langsmith_feedback import FeedbackLedger

ADMISSION_BENCHMARK_VERSION = "qa-evolution-admission-v1"
_ACTIONABLE_CODES = frozenset(
    {
        "WORKER_OR_WORKFLOW_DEGRADED",
        "LATENCY_ABOVE_THRESHOLD",
        "STRUCTURED_EVAL_SCORE_LOW",
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
    }
)


def _admission_result(candidate: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Validate redacted baseline evidence; this is not a solution score."""

    artifact_id = str(candidate.get("artifact_id") or "")
    source_runs = sorted(
        {str(value).strip() for value in candidate.get("source_run_ids") or [] if value}
    )
    finding_codes = sorted(
        {
            str(value).strip().upper()
            for value in candidate.get("finding_codes") or []
            if value
        }
    )
    improvement_type = str(candidate.get("improvement_type") or "")
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    errors: list[str] = []
    if not artifact_id.startswith("feedback-"):
        errors.append("artifact identity missing")
    if not source_runs:
        errors.append("source run identity missing")
    if not _ACTIONABLE_CODES.intersection(finding_codes):
        errors.append("no skill-actionable deterministic finding")
    if (
        "LATENCY_ABOVE_THRESHOLD" in finding_codes
        and str(metadata.get("latency_scope") or "") == "end_to_end"
        and str(metadata.get("latency_attribution_status") or "") != "MEASURED"
    ):
        errors.append("end-to-end latency owner is not measured")
    if (
        str(metadata.get("trace_kind") or "") == "workflow_root"
        and "WORKER_OR_WORKFLOW_DEGRADED" in finding_codes
        and not {
            "SEMANTIC_QA_FAILED",
            "SEMANTIC_QA_SCORE_LOW",
            "STRUCTURED_EVAL_SCORE_LOW",
        }.intersection(finding_codes)
        and not metadata.get("primary_bottleneck_department")
    ):
        errors.append("workflow root degradation has no measured skill owner")
    if metadata.get("raw_payloads_sent") is not False:
        errors.append("redacted metadata contract not proven")
    if improvement_type not in {"SKILL_CREATE", "SKILL_EVOLVE"}:
        errors.append("not routed to skill evolution")
    if improvement_type == "SKILL_EVOLVE" and not str(
        candidate.get("target_skill_slug") or ""
    ):
        errors.append("target skill missing")
    manifest = {
        "schema_version": ADMISSION_BENCHMARK_VERSION,
        "artifact_id": artifact_id,
        "source_run_ids": source_runs,
        "finding_codes": finding_codes,
        "improvement_type": improvement_type,
        "target_skill_slug": str(candidate.get("target_skill_slug") or ""),
        "errors": errors,
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    summary = (
        "manager-approved redacted baseline evidence passed skill admission"
        if not errors
        else "; ".join(errors)
    )
    return not errors, f"sha256:{digest}", summary


def run_admission_benchmarks(
    ledger: FeedbackLedger, *, limit: int = 100
) -> dict[str, int]:
    """Finish only the deterministic skill-admission benchmark class."""

    passed = failed = 0
    for candidate in ledger.evolution_benchmark_candidates(limit):
        ok, report_ref, summary = _admission_result(candidate)
        updated = ledger.update_benchmark(
            str(candidate["artifact_id"]),
            status="PASSED" if ok else "FAILED",
            benchmark_id=ADMISSION_BENCHMARK_VERSION,
            score=1.0 if ok else 0.0,
            report_ref=report_ref,
            result_summary=summary,
        )
        if updated and ok:
            passed += 1
        elif updated:
            failed += 1
    return {"passed": passed, "failed": failed}


def reconcile_passed_feedback(
    ledger: FeedbackLedger,
    store: EvolutionSkillStore,
    *,
    limit: int = 500,
) -> int:
    """Idempotently project every passed skill artifact into occurrences."""

    written = 0
    for item in ledger.evolution_ready(limit):
        written += record_qa_feedback_occurrences(
            store,
            department=str(item.get("department") or ""),
            source_run_ids=item.get("source_run_ids") or (),
            finding_codes=item.get("finding_codes") or (),
            detail="; ".join(str(value) for value in item.get("summaries") or ()),
            artifact_id=str(item.get("artifact_id") or ""),
            benchmark_id=str(item.get("benchmark_id") or ""),
            improvement_type=str(item.get("improvement_type") or ""),
            target_skill_slug=str(item.get("target_skill_slug") or ""),
            at=str(item.get("created_at") or ""),
        )
    return written


def process_qa_skill_feedback(
    ledger: FeedbackLedger,
    store: EvolutionSkillStore,
) -> dict[str, int]:
    benchmark = run_admission_benchmarks(ledger)
    return {
        "benchmark_passed": benchmark["passed"],
        "benchmark_failed": benchmark["failed"],
        "occurrences_written": reconcile_passed_feedback(ledger, store),
    }


__all__ = [
    "ADMISSION_BENCHMARK_VERSION",
    "process_qa_skill_feedback",
    "reconcile_passed_feedback",
    "run_admission_benchmarks",
]
