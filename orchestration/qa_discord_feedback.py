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
HR_LANGFUSE_FEEDBACK_MARKER = "[hgfinance-hr-langfuse-review-v1]"
QA_TERMINAL_MARKER = "[hgfinance-qa-terminal-discord-v1]"
SKILL_PROPOSAL_MARKER = "[hgfinance-skill-proposal-review-v1]"
QA_FEEDBACK_CHANNEL_DEFAULT = "1541636723006775477"
HR_LANGFUSE_CHANNEL_DEFAULT = "1542405626531942432"
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
    r"^\s*(승인|거부|반려|미승인|approve|approved|reject|rejected)\b[\s,:-]*(.*)$",
    re.IGNORECASE,
)
_ACTIONABLE_FINDINGS = frozenset(
    {
        "PRIVACY_PAYLOAD_PRESENT",
        "LANGFUSE_OBSERVABILITY_UNAVAILABLE",
        "WORKER_OR_WORKFLOW_DEGRADED",
        "LATENCY_ABOVE_THRESHOLD",
        "STRUCTURED_EVAL_SCORE_LOW",
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
    }
)

_MANAGER_TERMS = (
    ("HTTPException", "HTTP 요청 오류"),
    ("http_status=", "HTTP 상태코드="),
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
    ("Unexplained", "설명되지 않은"),
    ("Missing source coordinates and", "출처 식별자와"),
    ("source IDs", "출처 식별자"),
    ("pricing evidence", "가격 근거"),
    ("price timestamps", "가격 시각"),
    ("quality and effective/as-of validation", "자료 품질과 기준 시점 확인"),
    ("bridge difference", "대사 차이"),
    ("Keep official", "공식"),
    ("close and decision blocked until", "확정과 결정을 보류하고"),
    (
        "ledger/cash/valuation/fee-tax reconciliation evidence agrees",
        "원장·현금·평가·수수료·세금 대사 근거가 일치할 때까지",
    ),
    ("snapshot and broker independent reconciliation absent", "스냅샷과 브로커 독립 대사가 없음"),
    (
        "No investment/trading eligibility decision until evidence is independently verified",
        "근거를 독립적으로 확인하기 전에는 투자·거래 적격성을 결정하지 않음",
    ),
    ("Risk owner", "리스크 담당자"),
    ("and Accounting", "및 회계"),
    ("Accounting Engine", "회계 시스템"),
    # These are structured QA fields, not administrator-facing wording. Keep
    # the longer forms before the generic ``evidence`` label replacement.
    ("broker_evidence", "브로커 증거"),
    ("artifact/citation 좌표", "근거 좌표"),
    ("artifact/citation", "근거 좌표"),
    ("citation coordinates", "근거 좌표"),
    ("artifact metadata", "근거 자료 정보"),
    ("artifact", "근거 자료"),
    ("provided payload", "제공된 자료"),
    ("제공 payload", "제공된 자료"),
    ("제공 snapshot", "제공된 조회 자료"),
    ("payload", "제공 자료"),
    ("Preliminary", "예비"),
    ("gross/net exposure", "총·순 익스포저"),
    ("PnL", "손익"),
    ("side", "포지션 방향"),
    ("sector", "섹터"),
    ("broker", "브로커"),
    ("NAV", "순자산"),
    ("PIT", "기준 시점"),
    ("Mandate", "투자지침"),
    ("snapshot", "조회 자료"),
    ("Require ", "필요: "),
    ("next_ceo_synthesis", "다음 CEO 종합"),
)

_FINDING_LABELS = {
    "LATENCY_ABOVE_THRESHOLD": "처리 지연 기준 초과",
    "SEMANTIC_QA_FAILED": "결과 의미 검증 실패",
    "SEMANTIC_QA_SCORE_LOW": "결과 의미 검증 점수 미달",
    "STRUCTURED_EVAL_SCORE_LOW": "구조화 평가 점수 미달",
    "WORKER_OR_WORKFLOW_DEGRADED": "부서 또는 업무 흐름 성능 저하",
    "PRIVACY_PAYLOAD_PRESENT": "민감 원문 포함 감지",
    "LANGFUSE_OBSERVABILITY_UNAVAILABLE": "Langfuse 관측 연결 불가",
    "CORRELATION_METADATA_MISSING": "호출 연결 정보 누락",
    "DEPARTMENT_METADATA_MISSING": "부서·단계 정보 누락",
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
    "official_source_pit": "공식 자료·기준 시점",
    "official_metrics_consistency": "공식 지표 일관성",
    "arithmetic_reproducibility": "계산 재현성",
    "quant_evidence_reproducibility": "정량 근거 재현성",
    "mandate_nav_portfolio_suitability": "투자지침·순자산·포트폴리오 적합성",
    "conditional_conclusion_scope": "조건부 결론 범위",
    "target_and_window_consistency": "대상·기간 일관성",
    "reported_arithmetic": "보고 수치 계산 일관성",
    "cross_report_metric_consistency": "보고서 간 지표 일관성",
    "provenance_and_reproducibility": "자료 출처·재현성",
    "point_in_time_verifiability": "기준 시점 검증 가능성",
    "interpretive_claim_grounding": "해석 주장 근거성",
    "decision_readiness": "의사결정 준비도",
    "scope": "검토 범위 준수",
    "snapshot_value_consistency": "스냅샷 수치 일치",
    "nav_bridge_reconciliation_disclosure": "순자산 대사 공개",
    "evidence_provenance": "근거 재현성",
    "point_in_time": "기준 시점 처리",
    "uncertainty_handling": "불확실성 처리",
    "unsupported_claims": "근거 없는 주장 여부",
    "scope_binding": "검토 범위 준수",
    "numeric_consistency": "수치 일관성",
    "provenance_reproducibility": "자료 출처·재현성",
    "point_in_time_validity": "기준 시점 검증",
    "scope_paper_read_only": "PAPER 읽기 전용 범위",
    "position_exposure_consistency": "포지션·익스포저 일치",
    "provenance_citations": "자료 출처·인용",
    "point_in_time_reproducibility": "기준 시점 재현성",
    "prohibited_actions": "금지 행위 준수",
    # Post-response QA uses these compact keys. Keep them human-readable in
    # both the manager-facing Notion projection and the QA Discord card.
    "evidence": "근거 충실성",
    "citations": "출처 인용",
    "reproducibility": "재현성",
    "completeness": "응답 완전성",
    "paper_read_only_safety": "PAPER 읽기 전용 준수",
    "response_delivery_nonblocking": "CEO 응답 비차단",
    # Post-response QA receipts use these explicit check identifiers. Keep
    # them in the same manager-facing dictionary so Notion and Discord do not
    # fall back to the generic "추가 점검 항목" label.
    "evidence_and_citations": "근거·인용",
    "improvements_candidate_count": "개선 후보 조회",
    "observability_window": "관측 기간",
    "scorecard_window_and_scope": "성과표 기간·범위",
    "latency_reproducibility": "지연 재현성",
    "failure_retry_duplicate_reporting": "실패·재시도·중복 보고",
    "scorecard_content_claim": "성과표 내용 근거",
    "workflow_e2e_coverage": "전체 흐름 검증 범위",
    "scope_and_safety": "검토 범위·안전",
    "record_consistency": "기록 일관성",
    "failure_gate_and_delivery": "응답 비차단·전달",
    "fail_checks": "결정 차단 여부",
    "evidence_and_scope": "근거·검토 범위",
    "ceo_input_identity": "CEO 입력 동일성",
    "evidence_consistency": "근거 내부 일관성",
    "citations_and_provenance": "인용·자료 출처",
    "scope_and_claims": "검토 범위·주장",
    "e2e_delivery_and_qa_log_verification": "전달·QA 로그 검증",
    "workflow_lifecycle": "업무 흐름 생명주기",
    "safety_paper_read_only": "PAPER 읽기 전용 준수",
    "mandate_handling": "투자지침 처리",
    "scope_and_reproducibility": "검토 범위·재현성",
    "response_claims": "응답 주장",
    "delivery_and_observability": "전달·관측성",
    "qa_log_coverage": "QA 로그 범위",
    "contradictions_and_unknowns": "불일치·미확인 사항",
    "async_nonblocking": "사후 QA 비차단",
    "workforce_api_read_only_calls": "워크포스 읽기 전용 호출",
    "http_statuses": "HTTP 상태코드",
    "observability_state_counts": "관측 상태 집계",
    "scorecard_content_verification": "성과표 내용 검증",
    "improvement_candidate_count_claim": "개선 후보 수 검증",
    "citations_and_reproducibility": "인용·재현성",
    "delivery_path_lifecycle_connectivity": "전달 경로 연결성",
    "delivery_content_verification": "전달 내용 검증",
    "response_gating_behavior": "응답 차단 정책",
    "orders": "주문 변경",
    "investment_changes": "투자 변경",
    "ledger_changes": "원장 변경",
    "permission_changes": "권한 변경",
    "workflow_scope_and_identity": "업무 범위·식별성",
    "post_response_nonblocking": "사후 QA 비차단",
    "response_delivery_connectivity": "응답 전달 연결성",
    "claim_grounding": "주장 근거성",
    "evidence_and_citation_completeness": "근거·인용 완전성",
    "backtest_reproducibility": "백테스트 재현성",
    "internal_consistency": "내부 일관성",
    "scope_and_fulfillment": "범위·요청 이행",
    "workflow_compliance": "업무 흐름 준수",
    "hallucination_control": "추측·날조 방지",
    "risk_fail_closed": "리스크 차단 준수",
    "read_only_safety": "읽기 전용 안전성",
    # Accounting/portfolio QA receipts use a few source-shaped keys that
    # should remain readable in both operational surfaces.
    "position_value_sum": "포지션 평가액 합계",
    "pnl_completeness": "손익 자료 완전성",
    "broker_ledger_reconciliation": "브로커 원장 대사",
    "sector_mapping": "섹터 분류",
    "short_leg_status": "숏 포지션 상태",
    "mandate_scope_readiness": "투자지침 범위 적격성",
    "data_quality": "자료 품질",
}

_OWNER_LABELS = {
    "quant": "정량 분석 부서",
    "quant-backtest-department": "정량 분석 부서",
    "research": "리서치 부서",
    "research-department": "리서치 부서",
    "risk": "리스크 부서",
    "risk-management": "리스크 부서",
    "research / risk": "리서치·리스크 부서",
    "accounting": "회계·포트폴리오 부서",
    "accounting-portfolio-department": "회계·포트폴리오 부서",
    "accounting-portfolio": "회계·포트폴리오 부서",
    "ceo-workflow": "CEO 업무 흐름",
    "observability": "관측 시스템",
}
_SEVERITY_LABELS = {
    "BLOCKER": "차단",
    "CRITICAL": "매우 높음",
    "HIGH": "높음",
    "MEDIUM": "중간",
    "LOW": "낮음",
}


def qa_check_label(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    label = QA_CHECK_LABELS.get(normalized)
    if label:
        return label
    rendered = str(value or "").strip()
    if rendered and not re.fullmatch(r"[a-z0-9_.:/ -]+", rendered.casefold()):
        return rendered[:120]
    return "추가 점검 항목"


def qa_owner_label(value: Any) -> str:
    normalized = _bounded(value, 100).casefold()
    if normalized in _OWNER_LABELS:
        return _OWNER_LABELS[normalized]
    rendered = _manager_label(value, 100)
    # Do not leak an unmapped machine owner such as ``foo-bar-department`` to
    # an administrator-facing card. Preserve human-written Korean/English
    # prose, but replace opaque identifier-shaped values with one safe label.
    if not rendered or re.fullmatch(r"[a-z0-9_.:/ -]+", rendered):
        return "담당 부서 확인 필요" if normalized else ""
    return rendered


def _qa_result_label(value: Any) -> str:
    return {
        "PASS": "통과",
        "WARN": "주의",
        "FAIL": "실패",
        "DEFER": "보류",
    }.get(str(value or "").strip().upper(), _manager_label(value, 32) or "확인 필요")


def _manager_label(value: Any, limit: int = 160) -> str:
    rendered = _bounded(value, limit)
    for internal, friendly in _MANAGER_TERMS:
        rendered = rendered.replace(internal, friendly)
    for internal, friendly in QA_CHECK_LABELS.items():
        rendered = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(internal)}(?![A-Za-z0-9])",
            friendly,
            rendered,
            flags=re.IGNORECASE,
        )
    rendered = re.sub(
        r"\bKRW\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+))(?=\D|$)",
        r"\1원",
        rendered,
    )
    return rendered


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _qa_evidence_lines(value: Any, *, limit: int = 4) -> list[str]:
    """Render bounded worker-declared facts for the QA/operations card."""

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    lines: list[str] = []
    for item in values[:limit]:
        if isinstance(item, Mapping):
            item = (
                item.get("fact")
                or item.get("statement")
                or item.get("description")
                or item.get("message")
            )
        if item:
            lines.append(f"- {_manager_label(item, 260)}")
    return lines


def qa_feedback_channel_id() -> str:
    return os.getenv("QA_DISCORD_CHANNEL_ID", QA_FEEDBACK_CHANNEL_DEFAULT).strip()


def hr_langfuse_channel_id() -> str:
    return os.getenv(
        "HR_LANGFUSE_CHANNEL_ID", HR_LANGFUSE_CHANNEL_DEFAULT
    ).strip()


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


def post_hr_langfuse_discord_message(
    content: str,
    *,
    token: str,
    channel_id: str,
    timeout: float = 8.0,
) -> str:
    """Post one bounded HR/Langfuse card with the HR transport identity."""

    if not token.strip() or not channel_id.strip():
        raise ValueError("HR Langfuse Discord transport is not configured")
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
            "User-Agent": "HgFinance-HR-Langfuse/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    message_id = str(payload.get("id") or "") if isinstance(payload, Mapping) else ""
    if not message_id:
        raise RuntimeError("discord_message_id_missing")
    return message_id


def edit_qa_discord_message(
    content: str,
    *,
    token: str,
    channel_id: str,
    message_id: str,
    timeout: float = 8.0,
) -> str:
    """Update the one existing QA card without creating a duplicate."""

    if not token.strip() or not channel_id.strip() or not message_id.strip():
        raise ValueError("QA Discord edit transport is not configured")
    request = Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
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
        method="PATCH",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    returned_id = (
        str(payload.get("id") or message_id)
        if isinstance(payload, Mapping)
        else message_id
    )
    return returned_id or message_id


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
    numerical = _manager_label(
        evidence.get("numerical_posture") or evidence.get("numeric_posture"), 40
    ) or "확인 필요"
    if numerical.upper() == "DEFER":
        numerical = "판단 보류"

    checks: list[str] = []
    raw_checks = getattr(record, "checks", ())
    if isinstance(raw_checks, (list, tuple)):
        for item in raw_checks[:8]:
            if isinstance(item, Mapping):
                name = qa_check_label(item.get("check") or item.get("name"))
                result = _qa_result_label(item.get("result") or item.get("status"))
                checks.append(f"- {name}: {result or '확인 필요'}")
    elif isinstance(raw_checks, Mapping):
        for key, value in list(raw_checks.items())[:8]:
            checks.append(f"- {qa_check_label(key)}: {_qa_result_label(value)}")
    findings: list[str] = []
    raw_findings = getattr(record, "findings", ())
    if isinstance(raw_findings, (list, tuple)):
        for item in raw_findings[:5]:
            if isinstance(item, Mapping):
                severity = _SEVERITY_LABELS.get(
                    str(item.get("severity") or "").strip().upper(),
                    _manager_label(item.get("severity") or "확인 필요", 24),
                )
                issue = _manager_label(
                    item.get("summary")
                    or item.get("statement")
                    or item.get("description")
                    or item.get("issue")
                    or item.get("message")
                    or "근거 보완 필요",
                    300,
                )
                owner = qa_owner_label(
                    item.get("owner") or item.get("responsible_party")
                )
                impact = _manager_label(item.get("block_condition") or item.get("impact"), 150)
                finding_id = _manager_label(item.get("finding_id") or item.get("id"), 48)
                status = _manager_label(item.get("status"), 32)
                due_date = _manager_label(item.get("due_date") or item.get("due"), 32)
                prefix = f"{finding_id}: " if finding_id else ""
                suffix = f" 담당: {owner}" if owner else ""
                if impact:
                    suffix += f" 영향: {impact}"
                if status:
                    suffix += f" 상태: {status}"
                if due_date:
                    suffix += f" 기한: {due_date}"
                recommended_action = _manager_label(
                    item.get("recommended_action"), 220
                )
                if recommended_action:
                    suffix += f" 조치: {recommended_action}"
                findings.append(f"- [{severity}] {prefix}{issue}{suffix}")

    verified_facts = _qa_evidence_lines(evidence.get("verified_facts"))
    unknowns = _qa_evidence_lines(evidence.get("unknowns"), limit=3)

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
    report = (
        f"{QA_TERMINAL_MARKER}\n"
        "## QA 감사 결과\n"
        "- QA 검토 상태: 완료\n"
        f"- 업무 판정: **{decision_label}**\n"
        f"- 수치 판단: **{numerical}**\n"
        f"{latency_line}\n"
        "\n### 확인된 사실\n"
        f"{chr(10).join(checks) or '- 세부 점검 결과 없음'}\n"
    )
    if verified_facts:
        report += f"{chr(10).join(verified_facts)}\n"
    if unknowns:
        report += f"\n### 아직 확인되지 않은 점\n{chr(10).join(unknowns)}\n"
    report += (
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
    )
    return report[:1900]


def is_actionable_feedback(finding_codes: object) -> bool:
    if not isinstance(finding_codes, (list, tuple, set, frozenset)):
        return False
    return bool(
        _ACTIONABLE_FINDINGS.intersection(
            str(code).strip().upper() for code in finding_codes
        )
    )


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
        [str(code).strip()[:80].upper() for code in finding_codes]
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
    code_text = ", ".join(
        _FINDING_LABELS.get(code, "추가 확인 신호") for code in codes[:8]
    ) or "없음"
    primary_bottleneck = qa_owner_label(
        metadata.get("primary_bottleneck_department")
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
            f"- **관측 시작 지점:** `{observation_point or qa_owner_label(department)}` "
            "(원인 부서로 간주하지 않음)\n"
        )
    return (
        f"{QA_FEEDBACK_MARKER}\n"
        "## ① 자동 감지 · QA 검토 요청\n"
        f"- 피드백 기록 ID: `{_bounded(artifact_id, 80)}`\n"
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


def format_hr_langfuse_feedback_request(
    *,
    artifact_id: str,
    decision: str,
    finding_codes: object,
    summaries: object,
    metadata: Mapping[str, Any],
) -> str:
    """Build the HR-facing, metadata-only Langfuse review card.

    HR receives the aggregated Workforce read model, not trace input/output.
    Keep this separate from the QA card so the channel makes its owner and
    decision semantics obvious while the same central approval ledger remains
    the single source of truth.
    """

    codes = (
        [str(code).strip()[:80].upper() for code in finding_codes]
        if isinstance(finding_codes, (list, tuple, set, frozenset))
        else []
    )
    summary_values = (
        [_manager_label(value, 180) for value in summaries]
        if isinstance(summaries, (list, tuple))
        else []
    )
    decision_label = {
        "REVIEW_REQUIRED": "검토 필요",
        "IMPROVEMENT_CANDIDATE": "개선 검토 대상",
        "OBSERVED_PASS": "관측상 정상",
    }.get(str(decision or "").upper(), "확인 필요")
    observations: list[str] = []
    window_start = _bounded(metadata.get("window_start"), 64)
    window_end = _bounded(metadata.get("window_end"), 64)
    if window_start or window_end:
        observations.append(
            f"관측 구간: {window_start or '?'} ~ {window_end or '?'}"
        )
    report_count = metadata.get("report_count")
    if report_count:
        observations.append(f"관측 보고서: {int(report_count)}건")
    measured_count = metadata.get("measured_count")
    unavailable_count = metadata.get("unavailable_count")
    if measured_count is not None or unavailable_count is not None:
        observations.append(
            f"측정 완료 {int(measured_count or 0)}건 / 확인 불가 {int(unavailable_count or 0)}건"
        )
    if metadata.get("langfuse_queries") is not None:
        observations.append(f"Langfuse 조회 횟수: {int(metadata['langfuse_queries'])}회")
    if metadata.get("llm_calls") is not None:
        observations.append(f"모델 호출: {int(metadata['llm_calls'])}회")
    latency_ms = metadata.get("latency_ms") or metadata.get("p95_latency_ms")
    if latency_ms:
        latency = f"최장 실행 p95: {float(latency_ms) / 1000:.2f}초"
        threshold = metadata.get("latency_threshold_ms")
        if threshold:
            latency += f" (기준 {float(threshold) / 1000:.2f}초)"
        observations.append(latency)
    observations.extend(summary_values[:4])
    observation_text = "\n".join(f"- {line}" for line in observations)
    if not observation_text:
        observation_text = "- 집계된 관측값 없음"

    evidence_values = [
        ("관측 출처", metadata.get("source_project") or "Langfuse"),
        ("관측 유형", metadata.get("source_name") or "HR Workforce 관측"),
        ("관측 기록 ID", metadata.get("source_run_id")),
        ("연결 추적 ID", metadata.get("trace_id")),
        ("관측 API 단계", metadata.get("observation_point") or "워크포스 관측 API"),
    ]
    evidence = [
        f"- {label}: {_bounded(value, 160)}"
        for label, value in evidence_values
        if value
    ]
    evidence.append("- 원문 입력·출력 전송: 없음")
    evidence_text = "\n".join(evidence)
    code_text = ", ".join(
        _FINDING_LABELS.get(code, "추가 확인 신호") for code in codes[:8]
    ) or "없음"
    bottleneck = qa_owner_label(metadata.get("primary_bottleneck_department"))
    bottleneck_text = bottleneck or "미확정"
    if bottleneck and metadata.get("primary_bottleneck_duration_ms"):
        bottleneck_text += (
            f" ({float(metadata['primary_bottleneck_duration_ms']) / 1000:.2f}초)"
        )
    return (
        f"{HR_LANGFUSE_FEEDBACK_MARKER}\n"
        "## HR · Langfuse 관측 요약 및 관리자 결정 요청\n"
        f"- 관측 검토 ID: `{_bounded(artifact_id, 80)}`\n"
        "- 대상: **인사 부서**\n"
        f"- 자동 판정: **{decision_label}**\n"
        f"- 확인 신호: `{code_text}`\n"
        f"- 주요 병목: **{bottleneck_text}**\n\n"
        "### Langfuse 관측 요약\n"
        f"{observation_text}\n\n"
        "### 근거 좌표 · 원문 제외\n"
        f"{evidence_text}\n\n"
        "### 관리자 결정\n"
        f"- 이 카드에 답글: `승인 {_bounded(artifact_id, 80)} 유형=CODE_FIX <사유>`\n"
        f"- 미승인: `미승인 {_bounded(artifact_id, 80)} <사유>`\n"
        "- 보류: 답글 없이 대기\n\n"
        "> HR Hermes는 관측 요약·근거·한계만 검토하며 권한·설정·코드 변경과 주문을 수행하지 않습니다."
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
        f"- 개선안 기록 ID: `{_bounded(proposal_id, 100)}`\n"
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
    "HR_LANGFUSE_CHANNEL_DEFAULT",
    "HR_LANGFUSE_FEEDBACK_MARKER",
    "QA_FEEDBACK_MARKER",
    "SKILL_PROPOSAL_MARKER",
    "QaFeedbackCommand",
    "artifact_id_from_text",
    "format_hr_langfuse_feedback_request",
    "format_qa_feedback_request",
    "format_skill_activation_notice",
    "format_skill_proposal_request",
    "is_actionable_feedback",
    "parse_qa_feedback_command",
    "post_hr_langfuse_discord_message",
    "post_qa_discord_message",
    "proposal_id_from_text",
    "qa_feedback_channel_id",
    "hr_langfuse_channel_id",
    "submit_qa_feedback_decision",
    "submit_skill_proposal_decision",
]
