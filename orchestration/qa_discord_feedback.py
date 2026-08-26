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
QA_TERMINAL_MARKER = "[hgfinance-qa-terminal-discord-v1]"
SKILL_PROPOSAL_MARKER = "[hgfinance-skill-proposal-review-v1]"
QA_FEEDBACK_CHANNEL_DEFAULT = "1541636723006775477"
_ARTIFACT_RE = re.compile(r"\bfeedback-[0-9a-f]{32}\b", re.IGNORECASE)
_PROPOSAL_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]{1,62}-v[1-9][0-9]*-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_TYPE_RE = re.compile(
    r"\b(?:유형|type)\s*=\s*(SKILL_CREATE|SKILL_EVOLVE|CODE_FIX|"
    r"PROMPT_POLICY|RUNTIME_CONFIG|DATA_QUALITY|NO_ACTION)\b",
    re.IGNORECASE,
)
_SKILL_RE = re.compile(
    r"\b(?:스킬|skill)\s*=\s*([a-z0-9][a-z0-9-]{1,62})\b", re.IGNORECASE
)
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

_MANAGER_TERMS = (
    (
        "end-to-end latency exceeded the configured observation threshold",
        "전체 처리 시간이 설정된 기준을 초과했습니다.",
    ),
    ("risk-management", "리스크 부서"),
    ("trading-department", "트레이딩 부서"),
    ("research-department", "리서치 부서"),
    ("hr-department", "인사 부서"),
    ("ceo-workflow", "CEO 업무 흐름"),
    ("observability", "관측 시스템"),
    ("ceo-ingress", "CEO 요청 접수 단계"),
    ("ceo-terminal", "CEO 결과 전달 단계"),
    ("end_to_end", "전체 처리 시간"),
)

_FINDING_LABELS = {
    "LATENCY_ABOVE_THRESHOLD": "처리 지연 기준 초과",
    "SEMANTIC_QA_FAILED": "결과 의미 검증 실패",
    "SEMANTIC_QA_SCORE_LOW": "결과 의미 검증 점수 미달",
    "STRUCTURED_EVAL_SCORE_LOW": "구조화 평가 점수 미달",
    "WORKER_OR_WORKFLOW_DEGRADED": "부서 또는 업무 흐름 성능 저하",
    "PRIVACY_PAYLOAD_PRESENT": "민감 원문 포함 감지",
}

QA_CHECK_LABELS = {
    "arithmetic": "산술 일관성",
    "nav_bridge": "순자산 대사",
    "provenance": "자료 출처·계보",
    "pit_valuation": "기준 시점·평가",
    "independent_reconciliation": "독립 대사",
    "prohibited_action_compliance": "금지 행위 준수",
    "mandate_decision_eligibility": "투자 결정 적격성",
    "long_short_direction": "롱·숏 방향",
    "citation": "인용 근거",
}


def qa_check_label(value: Any) -> str:
    return QA_CHECK_LABELS.get(str(value or "").strip().casefold(), _manager_label(value, 100))


def _manager_label(value: Any, limit: int = 160) -> str:
    rendered = _bounded(value, limit)
    for internal, friendly in _MANAGER_TERMS:
        rendered = rendered.replace(internal, friendly)
    return rendered


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def qa_feedback_channel_id() -> str:
    return os.getenv("QA_DISCORD_CHANNEL_ID", QA_FEEDBACK_CHANNEL_DEFAULT).strip()


def post_qa_discord_message(
    content: str,
    *,
    token: str,
    channel_id: str,
    timeout: float = 8.0,
) -> str:
    """Post one bounded QA card and return its Discord message identity."""

    if not token.strip() or not channel_id.strip():
        raise ValueError("QA Discord transport is not configured")
    request = Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps(
            {"content": content[:1900], "allowed_mentions": {"parse": []}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "HgFinance-QA-Feedback/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    message_id = str(payload.get("id") or "") if isinstance(payload, Mapping) else ""
    if not message_id:
        raise RuntimeError("discord_message_id_missing")
    return message_id


def format_qa_terminal_report(record: Any) -> str:
    """Render a compact Korean QA/operations card from the audit record."""

    evidence = getattr(record, "evidence", {})
    evidence = evidence if isinstance(evidence, Mapping) else {}
    decision = str(getattr(record, "canonical_decision", "WARN") or "WARN").upper()
    decision_label = {
        "PASS": "통과",
        "WARN": "주의",
        "CONDITIONAL": "조건부 통과",
        "FAIL": "실패·투자 결정 차단",
    }.get(decision, "확인 필요")
    numerical = _manager_label(evidence.get("numerical_posture"), 40) or "확인 필요"
    if numerical.upper() == "DEFER":
        numerical = "판단 보류"

    checks: list[str] = []
    raw_checks = getattr(record, "checks", ())
    if isinstance(raw_checks, (list, tuple)):
        for item in raw_checks[:8]:
            if isinstance(item, Mapping):
                name = qa_check_label(item.get("check") or item.get("name"))
                result = _manager_label(item.get("result") or item.get("status"), 32)
                checks.append(f"- {name}: {result or '확인 필요'}")
    findings: list[str] = []
    raw_findings = getattr(record, "findings", ())
    if isinstance(raw_findings, (list, tuple)):
        for item in raw_findings[:5]:
            if isinstance(item, Mapping):
                severity = _manager_label(item.get("severity") or "확인 필요", 24)
                issue = _manager_label(
                    item.get("summary")
                    or item.get("description")
                    or item.get("issue")
                    or item.get("message")
                    or "근거 보완 필요",
                    300,
                )
                owner = _manager_label(item.get("owner") or item.get("responsible_party"), 100)
                impact = _manager_label(item.get("block_condition") or item.get("impact"), 150)
                suffix = f" 담당: {owner}" if owner else ""
                if impact:
                    suffix += f" 영향: {impact}"
                findings.append(f"- [{severity}] {issue}{suffix}")

    latency = evidence.get("latency_ms")
    latency_line = ""
    if latency not in (None, ""):
        try:
            latency_line = f"- 처리 시간: {float(latency) / 1000:.2f}초"
        except (TypeError, ValueError):
            pass
    actions = (
        "실패·주의 항목의 원자료, 기준 시점 자료와 독립 대사 근거를 보완한 뒤 QA를 재실행합니다."
        if decision != "PASS"
        else "현재 확인 결과를 유지하고, 다음 변경 시 동일한 QA 점검을 다시 수행합니다."
    )
    return (
        f"{QA_TERMINAL_MARKER}\n"
        "## QA 감사 결과\n"
        f"- 판정: **{decision_label}**\n"
        f"- 수치 판단: **{numerical}**\n"
        f"{latency_line}\n"
        "\n### 확인된 사실\n"
        f"{chr(10).join(checks) or '- 세부 점검 결과 없음'}\n"
        "\n### 문제 위치와 영향\n"
        f"{chr(10).join(findings) or '- 차단성 문제 없음'}\n"
        "\n### 조치\n"
        f"- {actions}\n"
        "- QA 승인 전 공식 수치 확정과 투자 결정을 진행하지 않습니다.\n"
        "\n### 추적 정보\n"
        f"- 상위 업무: `{_bounded(getattr(record, 'root_task_id', ''), 80)}`\n"
        f"- QA 업무: `{_bounded(getattr(record, 'qa_task_id', ''), 80)}`\n"
        f"- 평가 기록: `{_bounded(getattr(record, 'eval_run_id', ''), 100)}`\n"
        "\n> PAPER·읽기 전용 검토입니다. 주문 제출과 원장 변경은 수행하지 않았습니다."
    )[:1900]


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

    codes = (
        [str(code)[:80] for code in finding_codes]
        if isinstance(finding_codes, (list, tuple))
        else []
    )
    summary_values = (
        [_manager_label(value, 180) for value in summaries]
        if isinstance(summaries, (list, tuple))
        else []
    )
    latency_ms = metadata.get("latency_ms") or metadata.get("p95_latency_ms")
    latency_threshold_ms = metadata.get("latency_threshold_ms")
    metric_count = metadata.get("metric_count")
    observations: list[str] = []
    if metric_count:
        observations.append(f"집계된 metric trace 수: {int(metric_count)}")
    if latency_ms:
        latency = f"관측 지연: {float(latency_ms) / 1000:.2f}초"
        if latency_threshold_ms:
            latency += f" > 기준 {float(latency_threshold_ms) / 1000:.2f}초"
        if metadata.get("latency_scope"):
            latency += f" ({_manager_label(metadata['latency_scope'], 40)})"
        observations.append(latency)
    observations.extend(summary_values[:3])
    observation_text = (
        "\n".join(f"- {line}" for line in observations) or "- 상세 관측값 없음"
    )
    evidence_values = [
        ("관측 프로젝트", metadata.get("source_project")),
        ("실행 기록 유형", metadata.get("source_name")),
        ("실행 기록 ID", metadata.get("source_run_id")),
        ("연결 추적 ID", metadata.get("trace_id")),
        ("요청 ID", metadata.get("request_id")),
        ("최상위 업무 ID", metadata.get("root_id")),
        ("부서 업무 ID", metadata.get("task_id")),
    ]
    evidence = [
        f"- {label}: {_bounded(value, 160)}"
        for label, value in evidence_values
        if value
    ]
    if metadata.get("window_start") or metadata.get("window_end"):
        evidence.append(
            "- 관측 구간: "
            f"{_bounded(metadata.get('window_start'), 64) or '?'} ~ "
            f"{_bounded(metadata.get('window_end'), 64) or '?'}"
        )
    evidence_text = "\n".join(evidence) or "- artifact metadata 참조"
    code_text = (
        ", ".join(_FINDING_LABELS.get(code, code) for code in codes[:8]) or "없음"
    )
    primary_bottleneck = _manager_label(
        metadata.get("primary_bottleneck_department"), 64
    )
    joint_targets = _manager_label(metadata.get("joint_improvement_targets"), 120)
    observation_point = _manager_label(
        metadata.get("observation_point") or metadata.get("stage"), 64
    )
    if primary_bottleneck:
        attribution_text = (
            f"- **주요 병목:** `{primary_bottleneck}`\n"
            f"- **공동 개선 대상:** `{joint_targets or 'CEO 업무 흐름 / 관측 시스템'}`\n"
            f"- **관측 시작 지점:** `{observation_point or 'CEO 요청 접수 단계'}` "
            "(원인 부서 아님)\n"
        )
    else:
        attribution_text = (
            "- **주요 병목:** `미확정` (단계별 실행시간 근거 필요)\n"
            f"- **관측 시작 지점:** `{observation_point or _manager_label(department, 64)}` "
            "(원인 부서로 간주하지 않음)\n"
        )
    return (
        f"{QA_FEEDBACK_MARKER}\n"
        "## ① 자동 감지 · QA 검토 요청\n"
        f"feedback_artifact_id={_bounded(artifact_id, 80)}\n"
        f"{attribution_text}"
        f"- **자동 분류:** `{_manager_label(decision, 40).replace('IMPROVEMENT_CANDIDATE', '개선 검토 대상')}`\n"
        f"- **감지 신호:** `{code_text}`\n\n"
        "### 관측\n"
        f"{observation_text}\n"
        "\n### 문제 추적 정보 · 원문 제외\n"
        f"{evidence_text}\n\n"
        "> 다음 메시지 `② QA Hermes 검토 결과`에서 사실·한계·조치·판단 가이드를 제공합니다.\n\n"
        "### 관리자 결정\n"
        "- QA 결과에 답글: `승인 유형=<개선유형> <사유 필수>` 또는 `거부 <사유 필수>`\n"
        f"- 새 개선안 생성: `승인 {artifact_id} 유형=SKILL_CREATE <사유 필수>`\n"
        "- 기존 개선안 보완: `유형=SKILL_EVOLVE 스킬=<이름>`를 함께 입력\n"
        "- **보류:** 명령을 입력하지 않으면 `대기` 상태 유지\n\n"
        "QA Hermes는 이 artifact 한 건만 검토하고 승인·거부·설정 변경을 직접 수행하지 마세요."
    )[:1900]


def format_skill_proposal_request(
    *,
    proposal_id: str,
    slug: str,
    version: int,
    owner_profile: str,
    content_hash: str,
    provenance_hash: str,
    diff_hash: str,
    source_artifact_ids: object,
    benchmark_ids: object,
    validation: Mapping[str, Any],
) -> str:
    """Build the second-stage review card without copying generated prose."""

    artifacts = ", ".join(str(value) for value in source_artifact_ids or ()) or "NONE"
    benchmarks = ", ".join(str(value) for value in benchmark_ids or ()) or "NONE"
    stages = validation.get("stages") if isinstance(validation, Mapping) else {}
    stages = stages if isinstance(stages, Mapping) else {}
    return (
        f"{SKILL_PROPOSAL_MARKER}\n"
        "## ⑨ Evolution Skill 2차 검토 요청\n"
        f"skill_proposal_id={_bounded(proposal_id, 100)}\n"
        f"- **스킬:** `{_bounded(slug, 64)}` v{int(version)}\n"
        f"- **소유자:** `{_bounded(owner_profile, 64)}`\n"
        f"- **SKILL SHA-256:** `{_bounded(content_hash, 64)}`\n"
        f"- **provenance SHA-256:** `{_bounded(provenance_hash, 64)}`\n"
        f"- **diff.patch SHA-256:** `{_bounded(diff_hash, 64)}`\n"
        f"- **원인 QA artifact:** `{_bounded(artifacts, 300)}`\n"
        f"- **통과 benchmark:** `{_bounded(benchmarks, 240)}`\n"
        f"- **구조·provenance:** `{_bounded(stages.get('structure_and_provenance'), 40)}`\n"
        f"- **실행 검증:** `{_bounded(stages.get('execution'), 60)}`\n"
        f"- **정본 회귀:** `{_bounded(stages.get('canonical_regression'), 60)}`\n\n"
        "### 관리자 2차 결정\n"
        "- 이 카드에 Reply: `승인 <사유>` 또는 `거부 <사유>`\n"
        "- 승인은 위 두 hash에 결박되며, 이후 control worker가 정본 승격과 회귀 검증을 수행합니다.\n"
        "- 승인 전 자동 활성화는 없습니다."
    )[:1900]


def format_skill_activation_notice(report: Mapping[str, Any]) -> str:
    """Render the auditable activation result without overstating effectiveness."""

    skill = report.get("skill") if isinstance(report, Mapping) else {}
    problem = report.get("problem_evidence") if isinstance(report, Mapping) else {}
    outcome = report.get("outcome_evidence") if isinstance(report, Mapping) else {}
    change = report.get("change_evidence") if isinstance(report, Mapping) else {}
    skill = skill if isinstance(skill, Mapping) else {}
    problem = problem if isinstance(problem, Mapping) else {}
    outcome = outcome if isinstance(outcome, Mapping) else {}
    change = change if isinstance(change, Mapping) else {}
    artifacts = ", ".join(
        str(value) for value in problem.get("source_artifact_ids") or ()
    )
    return (
        "[hgfinance-skill-activation-evidence-v1]\n"
        "## ⑪ Evolution Skill 정본 승격 결과\n"
        f"skill_proposal_id={_bounded(report.get('proposal_id'), 100)}\n"
        f"- **스킬:** `{_bounded(skill.get('slug'), 64)}` v{int(skill.get('version') or 0)}\n"
        f"- **정본 상태:** `ACTIVE`\n"
        f"- **효과 판정:** `{_bounded(outcome.get('status'), 60)}`\n"
        f"- **원인 QA artifact:** `{_bounded(artifacts or 'NONE', 300)}`\n"
        f"- **SKILL SHA-256:** `{_bounded(skill.get('content_hash'), 64)}`\n"
        f"- **승인자:** `{_bounded(change.get('approved_by'), 80)}`\n"
        f"- **정본 경로:** `{_bounded(change.get('canonical_path'), 180)}`\n"
        f"- **운영 검증:** {int(outcome.get('observed_distinct_runs') or 0)}/"
        f"{int(outcome.get('required_distinct_runs') or 3)}개 독립 실행\n\n"
        f"> {_bounded(outcome.get('claim'), 120)}. 활성화와 문제 해결 확인은 별도입니다."
    )[:1900]


@dataclass(frozen=True)
class QaFeedbackCommand:
    decision: str
    artifact_id: str | None
    reason: str
    improvement_type: str | None = None
    target_skill_slug: str | None = None
    proposal_id: str | None = None


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
        tail = (
            tail[: artifact_match.start()] + " " + tail[artifact_match.end() :]
        ).strip(" ,:-")
    proposal_match = _PROPOSAL_RE.search(tail)
    proposal_id = proposal_match.group(0).lower() if proposal_match else None
    if proposal_match:
        tail = (
            tail[: proposal_match.start()] + " " + tail[proposal_match.end() :]
        ).strip(" ,:-")
    type_match = _TYPE_RE.search(tail)
    improvement_type = type_match.group(1).upper() if type_match else None
    if type_match:
        tail = (tail[: type_match.start()] + " " + tail[type_match.end() :]).strip(
            " ,:-"
        )
    skill_match = _SKILL_RE.search(tail)
    target_skill_slug = skill_match.group(1).lower() if skill_match else None
    if skill_match:
        tail = (tail[: skill_match.start()] + " " + tail[skill_match.end() :]).strip(
            " ,:-"
        )
    decision = "APPROVED" if verb in {"승인", "approve", "approved"} else "REJECTED"
    reason = _bounded(tail, 240)
    return QaFeedbackCommand(
        decision=decision,
        artifact_id=artifact_id,
        reason=reason,
        improvement_type=improvement_type,
        target_skill_slug=target_skill_slug,
        proposal_id=proposal_id,
    )


def artifact_id_from_text(content: object) -> str | None:
    match = _ARTIFACT_RE.search(str(content or ""))
    return match.group(0).lower() if match else None


def proposal_id_from_text(content: object) -> str | None:
    match = _PROPOSAL_RE.search(str(content or ""))
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
    base_url = (
        (
            api_url
            if api_url is not None
            else os.getenv("QA_FEEDBACK_API_URL", "http://audit-api:8000")
        )
        .strip()
        .rstrip("/")
    )
    token = (
        api_token if api_token is not None else os.getenv("QA_API_AUTH_TOKEN", "")
    ).strip()
    if not base_url or not token:
        return 503, {"detail": "qa_feedback_api_unavailable"}
    payload = {
        "decision": command.decision,
        "approved_by": f"discord:{_bounded(actor_id, 64)}",
        "reason": command.reason,
        "improvement_type": command.improvement_type or "NO_ACTION",
        "target_skill_slug": command.target_skill_slug or "",
    }
    return _post_internal_decision(
        f"{base_url}/qa/v1/observability/feedback/{command.artifact_id}/decision",
        payload,
        token=token,
        message_id=message_id,
        timeout=timeout,
    )


def submit_skill_proposal_decision(
    command: QaFeedbackCommand,
    *,
    actor_id: str,
    message_id: str,
    api_url: str | None = None,
    api_token: str | None = None,
    timeout: float = 8.0,
) -> tuple[int, dict[str, Any]]:
    if not command.proposal_id:
        return 422, {"detail": "skill_proposal_id_required"}
    base_url = (
        (
            api_url
            if api_url is not None
            else os.getenv("QA_FEEDBACK_API_URL", "http://audit-api:8000")
        )
        .strip()
        .rstrip("/")
    )
    token = (
        api_token if api_token is not None else os.getenv("QA_API_AUTH_TOKEN", "")
    ).strip()
    if not base_url or not token:
        return 503, {"detail": "qa_feedback_api_unavailable"}
    return _post_internal_decision(
        f"{base_url}/qa/v1/evolution/proposals/{command.proposal_id}/decision",
        {
            "decision": command.decision,
            "approved_by": f"discord:{_bounded(actor_id, 64)}",
            "reason": command.reason,
        },
        token=token,
        message_id=message_id,
        timeout=timeout,
    )


def _post_internal_decision(
    url: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    message_id: str,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
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
    "SKILL_PROPOSAL_MARKER",
    "QaFeedbackCommand",
    "artifact_id_from_text",
    "format_qa_feedback_request",
    "format_skill_activation_notice",
    "format_skill_proposal_request",
    "is_actionable_feedback",
    "parse_qa_feedback_command",
    "post_qa_discord_message",
    "proposal_id_from_text",
    "qa_feedback_channel_id",
    "submit_qa_feedback_decision",
    "submit_skill_proposal_decision",
]
