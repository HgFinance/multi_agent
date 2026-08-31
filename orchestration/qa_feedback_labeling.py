"""Redacted QA classification sampling and measurement.

This module is a measurement aid, not an approval path. It reads only the
metadata-only feedback projection, stores a separate adjudication label, and
never changes the feedback lifecycle, benchmark state, or evolution registry.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from orchestration.langsmith_feedback import FeedbackLedger

LABEL_REVIEW = "REVIEW"
LABEL_NO_ACTION = "NO_ACTION"
LABEL_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
MANUAL_LABELER = "qa-redacted-adjudication-v1"


def _redacted_rows(ledger: FeedbackLedger) -> list[dict[str, Any]]:
    with ledger._connect() as db:
        rows = db.execute(
            """SELECT artifact_id, source_run_id, decision, finding_codes,
                      summaries, metadata, created_at
               FROM langsmith_feedback_artifacts
               ORDER BY created_at DESC, artifact_id DESC"""
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
            finding_codes = json.loads(row["finding_codes"] or "[]")
            summaries = json.loads(row["summaries"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or not isinstance(finding_codes, list):
            continue
        if metadata.get("canary") or str(row["source_run_id"]).startswith("canary:"):
            continue
        result.append(
            {
                "artifact_id": str(row["artifact_id"]),
                "auto_decision": str(row["decision"]),
                "finding_codes": [str(value).upper() for value in finding_codes[:12]],
                "summaries": [str(value)[:180] for value in summaries[:4]],
                "metadata": {
                    key: metadata.get(key)
                    for key in (
                        "source",
                        "source_name",
                        "department",
                        "stage",
                        "review_class",
                        "candidate_type",
                        "request_id",
                        "root_id",
                        "latency_ms",
                        "latency_threshold_ms",
                        "sample_count",
                    )
                    if metadata.get(key) is not None
                },
                "created_at": str(row["created_at"]),
            }
        )
    return result


def select_redacted_sample(
    ledger: FeedbackLedger, *, sample_size: int = 40
) -> list[dict[str, Any]]:
    """Select a deterministic, status-stratified redacted sample."""

    size = max(30, min(int(sample_size), 50))
    rows = _redacted_rows(ledger)
    buckets = {
        decision: [row for row in rows if row["auto_decision"] == decision]
        for decision in (
            "IMPROVEMENT_CANDIDATE",
            "REVIEW_WORTHY",
            "REVIEW_REQUIRED",
            "OBSERVED_PASS",
        )
    }
    requested = {
        "IMPROVEMENT_CANDIDATE": max(1, size // 2),
        "REVIEW_WORTHY": max(1, size // 4),
        "REVIEW_REQUIRED": max(1, size // 8),
    }
    requested["OBSERVED_PASS"] = size - sum(requested.values())
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for bucket_name, count in requested.items():
        for row in buckets[bucket_name][:count]:
            selected.append(row)
            selected_ids.add(row["artifact_id"])
    if len(selected) < size:
        for row in rows:
            if row["artifact_id"] in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row["artifact_id"])
            if len(selected) >= size:
                break
    return selected[:size]


def adjudicate_redacted_artifact(row: dict[str, Any]) -> tuple[str, str]:
    """Apply the conservative manual rubric to one metadata-only row."""

    codes = set(row.get("finding_codes") or ())
    decision = str(row.get("auto_decision") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    has_coordinate = bool(metadata.get("request_id") or metadata.get("root_id"))
    if decision == "OBSERVED_PASS" and not codes:
        return LABEL_NO_ACTION, "자동 통과이며 관측 신호가 없음"
    if decision == "IMPROVEMENT_CANDIDATE":
        if (
            any(code.startswith("D5_") for code in codes)
            and metadata.get("source") == "memo_harness_d5"
            and has_coordinate
        ):
            return LABEL_REVIEW, "검증된 D5 신호와 업무 좌표가 있어 owner review 필요"
        return LABEL_INSUFFICIENT_EVIDENCE, "개선 후보지만 검증 가능한 redacted 좌표 부족"
    if "LATENCY_ABOVE_THRESHOLD" in codes:
        latency = metadata.get("latency_ms")
        threshold = metadata.get("latency_threshold_ms")
        if (
            isinstance(latency, (int, float))
            and isinstance(threshold, (int, float))
            and latency > threshold
        ):
            return LABEL_REVIEW, "측정 지연이 redacted 기준값을 실제 초과"
    if codes.intersection({"PRIVACY_PAYLOAD_PRESENT", "REDACTION_MARKER_MISSING"}):
        return LABEL_REVIEW, "보안·원문 비전송 확인은 검토 필수"
    if decision in {"REVIEW_REQUIRED", "REVIEW_WORTHY"} and has_coordinate:
        return LABEL_REVIEW, "검토 신호와 redacted 업무 좌표가 함께 존재"
    return LABEL_INSUFFICIENT_EVIDENCE, "검토 신호는 있으나 redacted 근거가 부족"


def label_sample(
    ledger: FeedbackLedger,
    *,
    sample_size: int = 40,
    labeled_by: str = MANUAL_LABELER,
) -> dict[str, Any]:
    """Label one stable sample and return bounded classification metrics."""

    sample = select_redacted_sample(ledger, sample_size=sample_size)
    for row in sample:
        label, rationale = adjudicate_redacted_artifact(row)
        ledger.record_manual_label(
            row["artifact_id"],
            label=label,
            labeled_by=labeled_by,
            rationale=rationale,
        )
    labels = ledger.manual_labels([row["artifact_id"] for row in sample])
    scored = [
        row
        for row in sample
        if labels.get(row["artifact_id"], {}).get("label")
        in {LABEL_REVIEW, LABEL_NO_ACTION}
    ]
    auto_positive = [
        row for row in scored if row["auto_decision"] != "OBSERVED_PASS"
    ]
    false_positive = [
        row
        for row in auto_positive
        if labels[row["artifact_id"]]["label"] == LABEL_NO_ACTION
    ]
    true_positive = [
        row
        for row in auto_positive
        if labels[row["artifact_id"]]["label"] == LABEL_REVIEW
    ]
    false_negative = [
        row
        for row in scored
        if row["auto_decision"] == "OBSERVED_PASS"
        and labels[row["artifact_id"]]["label"] == LABEL_REVIEW
    ]
    candidate_rows = [
        row for row in scored if row["auto_decision"] == "IMPROVEMENT_CANDIDATE"
    ]
    candidate_false_positive = [
        row
        for row in candidate_rows
        if labels[row["artifact_id"]]["label"] == LABEL_NO_ACTION
    ]
    denominator = len(true_positive) + len(false_positive)
    candidate_denominator = len(candidate_rows)
    return {
        "sample_size": len(sample),
        "scored_size": len(scored),
        "unresolved_size": len(sample) - len(scored),
        "auto_decisions": dict(Counter(row["auto_decision"] for row in sample)),
        "manual_labels": dict(
            Counter(labels[row["artifact_id"]]["label"] for row in sample)
        ),
        "precision": round(len(true_positive) / denominator, 6) if denominator else None,
        "overclassification_rate": round(len(false_positive) / denominator, 6)
        if denominator
        else None,
        "false_negative_rate": round(len(false_negative) / len(scored), 6)
        if scored
        else None,
        "improvement_candidate_overclassification_rate": (
            round(len(candidate_false_positive) / candidate_denominator, 6)
            if candidate_denominator
            else None
        ),
        "artifact_ids": [row["artifact_id"] for row in sample],
        "labeler": labeled_by,
        "rubric": "redacted metadata v1; no raw prompt/output and no approval mutation",
    }
