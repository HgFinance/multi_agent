"""Internal Discord contract for QA observability feedback.

This module contains only redacted formatting and deterministic command/API
helpers.  LangSmith content and Discord credentials are never included in a
card or log message.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

QA_FEEDBACK_MARKER = "[hgfinance-qa-feedback-request-v1]"
QA_FEEDBACK_CHANNEL_DEFAULT = "1541636723006775477"
_ARTIFACT_RE = re.compile(r"\bfeedback-[0-9a-f]{32}\b", re.IGNORECASE)
_COMMAND_RE = re.compile(
    r"^\s*(승인|거부|반려|approve|approved|reject|rejected)\b[\s,:-]*(.*)$",
    re.IGNORECASE,
)
_ACTIONABLE_FINDINGS = frozenset(
    {
        "PRIVACY_PAYLOAD_PRESENT",
        "WORKER_OR_WORKFLOW_DEGRADED",
        "LATENCY_ABOVE_THRESHOLD",
        "STRUCTURED_EVAL_SCORE_LOW",
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
    }
)


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def qa_feedback_channel_id() -> str:
    return os.getenv("QA_DISCORD_CHANNEL_ID", QA_FEEDBACK_CHANNEL_DEFAULT).strip()


def is_actionable_feedback(finding_codes: object) -> bool:
    if not isinstance(finding_codes, (list, tuple, set, frozenset)):
        return False
    return bool(_ACTIONABLE_FINDINGS.intersection(str(code) for code in finding_codes))


def format_qa_feedback_request(
    *,
    artifact_id: str,
    department: str,
    decision: str,
    finding_codes: object,
    summaries: object,
    metadata: Mapping[str, Any],
) -> str:
    """Build a bounded metadata-only request for the QA Hermes Agent."""

    codes = [str(code)[:80] for code in finding_codes] if isinstance(finding_codes, (list, tuple)) else []
    summary_values = [
        _bounded(value, 180)
        for value in summaries
    ] if isinstance(summaries, (list, tuple)) else []
    latency_ms = metadata.get("latency_ms") or metadata.get("p95_latency_ms")
    latency_threshold_ms = metadata.get("latency_threshold_ms")
    metric_count = metadata.get("metric_count")
    observations: list[str] = []
    if metric_count:
        observations.append(f"집계된 metric trace 수: {int(metric_count)}")
    if latency_ms:
        latency = f"관측 지연: {float(latency_ms) / 1000:.2f}s"
        if latency_threshold_ms:
            latency += f" > 기준 {float(latency_threshold_ms) / 1000:.2f}s"
        if metadata.get("latency_scope"):
            latency += f" ({_bounded(metadata['latency_scope'], 40)})"
        observations.append(latency)
    observations.extend(summary_values[:3])
    observation_text = "\n".join(f"- {line}" for line in observations) or "- 상세 관측값 없음"
    evidence_values = [
        ("project", metadata.get("source_project")),
        ("source_run_id", metadata.get("source_run_id")),
        ("trace_id", metadata.get("trace_id")),
        ("request_id", metadata.get("request_id")),
        ("root_id", metadata.get("root_id")),
    ]
    evidence = [f"- {label}: {_bounded(value, 160)}" for label, value in evidence_values if value]
    if metadata.get("window_start") or metadata.get("window_end"):
        evidence.append(
            "- 관측 구간: "
            f"{_bounded(metadata.get('window_start'), 64) or '?'} ~ "
            f"{_bounded(metadata.get('window_end'), 64) or '?'}"
        )
    evidence_text = "\n".join(evidence) or "- artifact metadata 참조"
    code_text = ", ".join(codes[:8]) or "NONE"
    return (
        f"{QA_FEEDBACK_MARKER}\n"
        f"feedback_artifact_id={_bounded(artifact_id, 80)}\n"
        f"대상 부서: {_bounded(department, 64)}\n"
        f"자동 분류: {_bounded(decision, 40)}\n"
        f"finding: {code_text}\n"
        f"{observation_text}\n"
        "증거 참조(원문 payload 제외):\n"
        f"{evidence_text}\n\n"
        "QA Hermes는 이 메시지의 feedback_artifact_id 한 건만 검토하세요. "
        "다른 대기 artifact를 같은 응답에 합치지 말고, 위 redacted 관측만으로 사실/추론을 구분해 "
        "담당 부서와 구체적인 조치·검증 방법을 제안하세요. "
        "승인이나 설정 변경은 직접 수행하지 마세요. 응답에 feedback_artifact_id를 그대로 남기세요.\n"
        f"관리자 명령: `승인 {artifact_id} [사유]` 또는 `거부 {artifact_id} [사유]`"
    )[:1900]


@dataclass(frozen=True)
class QaFeedbackCommand:
    decision: str
    artifact_id: str | None
    reason: str


def parse_qa_feedback_command(content: object) -> QaFeedbackCommand | None:
    text = _bounded(content, 500)
    match = _COMMAND_RE.match(text)
    if match is None:
        return None
    verb = match.group(1).casefold()
    tail = match.group(2).strip()
    artifact_match = _ARTIFACT_RE.search(tail)
    artifact_id = artifact_match.group(0).lower() if artifact_match else None
    if artifact_match:
        tail = (tail[: artifact_match.start()] + " " + tail[artifact_match.end() :]).strip(" ,:-")
    decision = "APPROVED" if verb in {"승인", "approve", "approved"} else "REJECTED"
    reason = _bounded(tail, 240) or (
        "Discord QA 관리자 승인" if decision == "APPROVED" else "Discord QA 관리자 거부"
    )
    return QaFeedbackCommand(decision=decision, artifact_id=artifact_id, reason=reason)


def artifact_id_from_text(content: object) -> str | None:
    match = _ARTIFACT_RE.search(str(content or ""))
    return match.group(0).lower() if match else None


def submit_qa_feedback_decision(
    command: QaFeedbackCommand,
    *,
    actor_id: str,
    message_id: str,
    api_url: str | None = None,
    api_token: str | None = None,
    timeout: float = 8.0,
) -> tuple[int, dict[str, Any]]:
    """Submit the one-shot decision to the existing audit API ledger."""

    if not command.artifact_id:
        return 422, {"detail": "feedback_artifact_id_required"}
    base_url = (api_url if api_url is not None else os.getenv("QA_FEEDBACK_API_URL", "http://audit-api:8000")).strip().rstrip("/")
    token = (api_token if api_token is not None else os.getenv("QA_API_AUTH_TOKEN", "")).strip()
    if not base_url or not token:
        return 503, {"detail": "qa_feedback_api_unavailable"}
    payload = json.dumps(
        {
            "decision": command.decision,
            "approved_by": f"discord:{_bounded(actor_id, 64)}",
            "reason": command.reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{base_url}/qa/v1/observability/feedback/{command.artifact_id}/decision",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-HgFinance-Discord-Message-Id": _bounded(message_id, 80),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace")
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"detail": f"qa_feedback_api_http_{status}"}
    return status, body if isinstance(body, dict) else {}


__all__ = [
    "QA_FEEDBACK_MARKER",
    "QaFeedbackCommand",
    "artifact_id_from_text",
    "format_qa_feedback_request",
    "is_actionable_feedback",
    "parse_qa_feedback_command",
    "qa_feedback_channel_id",
    "submit_qa_feedback_decision",
]
