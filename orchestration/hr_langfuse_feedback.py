"""Langfuse Workforce 관측을 HR Discord 검토 카드로 연결한다.

이 모듈은 이미 workforce-api가 수집한 ``WorkforceObservability``만 받는다.
Langfuse 원문 input/output을 다시 조회하거나 저장하지 않고, 이상 신호가 있는
완료 관측 창만 중앙 QA 승인 원장과 HR Discord 채널에 한 번 전달한다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from orchestration.langsmith_feedback import EvaluationResult, FeedbackLedger
from orchestration.qa_discord_feedback import (
    format_hr_langfuse_feedback_request,
    hr_langfuse_channel_id,
    is_actionable_feedback,
    post_hr_langfuse_discord_message,
)

LOGGER = logging.getLogger("hr.langfuse.feedback")
HR_LANGFUSE_REVIEW_MODES = frozenset({"off", "shadow", "active"})
DEFAULT_STATE_PATH = "/var/lib/portfolio/langsmith-feedback.sqlite3"
DEFAULT_LATENCY_WARN_MS = 60_000


def hr_langfuse_review_mode(value: str | None = None) -> str:
    """Return the explicit HR mode, falling back to the existing feedback mode."""

    candidate = value
    if candidate is None:
        candidate = os.getenv("HR_LANGFUSE_REVIEW_MODE")
    if candidate is None:
        candidate = os.getenv("LANGSMITH_FEEDBACK_MODE", "shadow")
    normalized = str(candidate).strip().lower()
    return normalized if normalized in HR_LANGFUSE_REVIEW_MODES else "off"


def _status_value(report: Any) -> str:
    status = getattr(report, "status", "")
    return str(getattr(status, "value", status) or "").upper()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _report_metrics(observability: Any) -> tuple[list[Any], int, int, int, int]:
    """Flatten the four bounded report groups without copying report payloads."""

    groups = (
        getattr(observability, "idle_agents", ()),
        getattr(observability, "capacity", ()),
        getattr(observability, "llm_usage", ()),
        getattr(observability, "worker_usage", ()),
        getattr(observability, "trigger_rates", ()),
    )
    reports = [report for group in groups for report in (group or ())]
    unavailable = sum(_status_value(report) == "UNAVAILABLE" for report in reports)
    measured = sum(_status_value(report) in {"MEASURED", "ACTIVE", "IDLE"} for report in reports)
    return reports, measured, unavailable, len(groups), len(reports)


def build_hr_langfuse_evaluation(
    observability: Any,
    *,
    latency_warn_ms: int = DEFAULT_LATENCY_WARN_MS,
) -> EvaluationResult:
    """Convert one Workforce Langfuse window into a redacted review result."""

    reports, measured_count, unavailable_count, group_count, report_count = _report_metrics(
        observability
    )
    capacity = list(getattr(observability, "capacity", ()) or ())
    usage = list(getattr(observability, "worker_usage", ()) or ())
    duration_reports = [
        report
        for report in capacity
        if _status_value(report) == "MEASURED"
        and _safe_float(getattr(report, "duration_p95_ms", None)) is not None
    ]
    longest = max(
        duration_reports,
        key=lambda report: _safe_float(getattr(report, "duration_p95_ms", None)) or 0,
        default=None,
    )
    longest_ms = (
        _safe_int(getattr(longest, "duration_p95_ms", None)) if longest is not None else None
    )
    error_rates = [
        rate
        for report in capacity
        if (rate := _safe_float(getattr(report, "error_rate", None))) is not None
    ]
    retry_rates = [
        rate
        for report in capacity
        if (rate := _safe_float(getattr(report, "retry_rate", None))) is not None
    ]
    arrivals = sum(
        _safe_int(getattr(report, "arrivals", None)) or 0
        for report in capacity
        if _status_value(report) == "MEASURED"
    )
    llm_calls = sum(
        _safe_int(getattr(report, "llm_calls", None)) or 0
        for report in usage
        if _status_value(report) == "MEASURED"
    )

    findings: list[str] = []
    summaries: list[str] = []
    if unavailable_count:
        findings.append("LANGFUSE_OBSERVABILITY_UNAVAILABLE")
        summaries.append(
            f"Langfuse 관측 보고서 {unavailable_count}건을 확인하지 못했습니다."
        )
    if longest_ms and longest_ms > latency_warn_ms:
        findings.append("LATENCY_ABOVE_THRESHOLD")
        summaries.append(
            f"측정된 실행 p95 {longest_ms / 1000:.2f}초가 기준 {latency_warn_ms / 1000:.2f}초를 초과했습니다."
        )
    max_error_rate = max(error_rates, default=0.0)
    max_retry_rate = max(retry_rates, default=0.0)
    if max_error_rate > 0 or max_retry_rate > 0:
        findings.append("WORKER_OR_WORKFLOW_DEGRADED")
        summaries.append(
            "관측된 부서 실행에서 오류율 또는 재시도율이 0보다 높았습니다."
        )
    if not findings:
        decision = "OBSERVED_PASS"
        summaries.append("Langfuse HR 관측 창에서 운영 이상 신호가 확인되지 않았습니다.")
    elif "LANGFUSE_OBSERVABILITY_UNAVAILABLE" in findings:
        decision = "REVIEW_REQUIRED"
    else:
        decision = "IMPROVEMENT_CANDIDATE"

    window_start = getattr(observability, "window_start", None)
    window_end = getattr(observability, "window_end", None)
    window_start_text = window_start.isoformat() if window_start else ""
    window_end_text = window_end.isoformat() if window_end else ""
    metadata: dict[str, Any] = {
        "schema_version": "hgfinance.observability.feedback.v1",
        "source_project": "Langfuse",
        "source_name": "HR Workforce 관측 창",
        "source": "langfuse-workforce-observability",
        "source_run_id": (
            f"hr-langfuse-observability:{window_start_text or 'unknown'}"
        )[:160],
        "department": "hr",
        "workflow_role": "hr-langfuse-observability-review",
        "observation_unit": "langfuse_observability_window",
        "observation_point": "workforce-api",
        "status": "degraded" if findings else "success",
        "window_start": window_start_text,
        "window_end": window_end_text,
        "latency_ms": longest_ms,
        "p95_latency_ms": longest_ms,
        "latency_threshold_ms": max(0, int(latency_warn_ms)),
        "latency_scope": "worker_execution",
        "primary_bottleneck_department": (
            getattr(longest, "department", None) if longest is not None else None
        ),
        "primary_bottleneck_duration_ms": longest_ms,
        "joint_improvement_targets": "인사 부서 / 관측 시스템",
        "metric_count": arrivals,
        "llm_calls": llm_calls,
        "report_count": report_count,
        "report_group_count": group_count,
        "measured_count": measured_count,
        "unavailable_count": unavailable_count,
        "max_error_rate": max_error_rate,
        "max_retry_rate": max_retry_rate,
        "langfuse_queries": _safe_int(getattr(observability, "langfuse_queries", 0)) or 0,
        "raw_payloads_sent": False,
    }
    return EvaluationResult(
        source_run_id=metadata["source_run_id"],
        department="hr",
        workflow_role="hr-langfuse-observability-review",
        decision=decision,
        score=None,
        finding_codes=tuple(dict.fromkeys(findings)),
        summaries=tuple(summaries),
        metadata=metadata,
    )


def publish_hr_langfuse_review(
    observability: Any,
    *,
    ledger: FeedbackLedger | None = None,
    latency_warn_ms: int = DEFAULT_LATENCY_WARN_MS,
    dry_run: bool = False,
) -> str:
    """Create and deliver one HR card; return a safe operational status."""

    mode = hr_langfuse_review_mode()
    if mode != "active" or dry_run:
        return "DISABLED" if mode != "active" else "DRY_RUN"
    channel_id = hr_langfuse_channel_id()
    token = os.getenv("DISCORD_BOT_TOKEN_HR", "").strip()
    if not channel_id or not token:
        return "NOT_CONFIGURED"
    result = build_hr_langfuse_evaluation(
        observability, latency_warn_ms=latency_warn_ms
    )
    if not is_actionable_feedback(result.finding_codes):
        return "NOT_ACTIONABLE"
    try:
        feedback_ledger = ledger or FeedbackLedger(
            os.getenv("LANGSMITH_FEEDBACK_STATE_PATH", DEFAULT_STATE_PATH).strip()
            or DEFAULT_STATE_PATH
        )
        artifact_id = feedback_ledger.complete(
            result.source_run_id,
            result.source_run_id,
            result,
        )
        if not feedback_ledger.claim_discord_delivery(artifact_id):
            return "DUPLICATE"
        content = format_hr_langfuse_feedback_request(
            artifact_id=artifact_id,
            decision=result.decision,
            finding_codes=result.finding_codes,
            summaries=result.summaries,
            metadata=result.metadata,
        )
        message_id = post_hr_langfuse_discord_message(
            content, token=token, channel_id=channel_id
        )
        feedback_ledger.finish_discord_delivery(
            artifact_id, delivered=True, discord_message_id=message_id
        )
        LOGGER.info(
            "hr-langfuse-review status=DELIVERED artifact_id=%s channel_configured=true",
            artifact_id,
        )
        return "DELIVERED"
    except Exception as exc:  # noqa: BLE001 - review is fail-open to snapshots
        LOGGER.warning(
            "hr-langfuse-review status=FAILED error_type=%s",
            type(exc).__name__,
        )
        return "FAILED"


__all__ = [
    "DEFAULT_LATENCY_WARN_MS",
    "HR_LANGFUSE_REVIEW_MODES",
    "build_hr_langfuse_evaluation",
    "hr_langfuse_review_mode",
    "publish_hr_langfuse_review",
]
