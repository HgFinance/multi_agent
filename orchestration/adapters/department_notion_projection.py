"""Non-binding Notion projection for completed department tasks.

Trading and Quant keep their existing projection contract. Research and Risk
also have native reporters for their standalone department pipelines, but a
CEO/Kanban task is a separate execution boundary: when their database IDs are
explicitly wired into the Supervisor, this observer records that terminal
task once without importing or invoking the native reporter. That avoids a
duplicate cross-boundary upload while making the natural CEO path observable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from departments.notion_markdown import markdown_to_notion_blocks
from departments.risk_notion_schema import (
    human_metadata_rows,
    risk_property_name,
)
from orchestration.adapters.notion_http import NotionHttpError, request_json
from orchestration.adapters.notion_idempotency import (
    NotionIdempotency,
)
from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache
from orchestration.adapters.terminal_projection_utils import (
    iso_timestamp,
    merged_run_metadata,
    qa_projection_checks,
    qa_projection_findings,
    safe_json,
    strip_internal_handoff,
    summary,
    task_body,
    task_id,
    terminal_success,
    text_value,
    workflow_root,
)
from orchestration.canonical_profiles import department_for_canonical_profile
from orchestration.qa_discord_feedback import qa_check_label, qa_owner_label
from orchestration.risk_plan_projection import format_position_risk_plan

DEFAULT_DATABASES = {
    "trading": "2903de9e2a7b4f6d967f709e6640ec16",
    "quant-backtest": "2adc190ac33d4d639a90f1ab86087f42",
}

DATABASE_ENV = {
    "trading": "NOTION_TRADING_DB",
    "quant-backtest": "NOTION_QUANT_BACKTEST_DB",
    "research": "NOTION_RESEARCH_DB",
    "risk": "NOTION_RISK_DB",
    "accounting": "NOTION_ACCOUNTING_DB",
    "qa": "NOTION_QA_DB",
    "hr": "NOTION_HR_DB",
}

TITLE_PROPERTY = {
    "trading": "제목",
    "quant-backtest": "전략·백테스트 run",
    "research": "종목",
    "risk": "제목",
    "accounting": "제목",
    "qa": "제목",
    # HR's native database uses the role label as its title property.
    "hr": "후보 role_code",
}

PROJECTION_MARKER = "hgfinance.department-notion-projection.v1"


class DepartmentNotionProjectionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NotionTransport:
    version = "2022-06-28"

    def __init__(self, token: str) -> None:
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            return request_json(
                method,
                path,
                self.token,
                body=body,
                version=self.version,
            )
        except NotionHttpError as exc:
            raise DepartmentNotionProjectionError(
                str(exc), status=exc.status
            ) from exc

    def database_schema(self, database_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"databases/{database_id}")

    def query_title(
        self,
        database_id: str,
        title_property: str,
        title: str,
    ) -> Sequence[Mapping[str, Any]]:
        response = self._request(
            "POST",
            f"databases/{database_id}/query",
            {
                "filter": {
                    "property": title_property,
                    "title": {"equals": title},
                },
                "page_size": 1,
            },
        )
        results = response.get("results", [])
        if isinstance(results, Sequence) and results:
            return results

        # Titles may be humanized after an earlier projection.  Fall back only
        # for the stable task-id prefix.  A human title such as "사용자 PAPER
        # 조건주문…" is shared by many cards; using it as a contains filter
        # would update an unrelated page and collapse the manager view.
        task_prefix = title.split(" · ", 1)[0].strip()
        if not re.fullmatch(r"t_[A-Za-z0-9_-]+", task_prefix):
            return ()
        response = self._request(
            "POST",
            f"databases/{database_id}/query",
            {
                "filter": {
                    "property": title_property,
                    "title": {"contains": task_prefix},
                },
                "page_size": 1,
            },
        )
        results = response.get("results", [])
        return results if isinstance(results, Sequence) else ()

    def create_page(
        self,
        database_id: str,
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "pages",
            {
                "parent": {"database_id": database_id},
                "properties": dict(properties),
                "children": list(children),
            },
        )

    def update_page(
        self, page_id: str, properties: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._request(
            "PATCH", f"pages/{page_id}", {"properties": dict(properties)}
        )

    def retrieve_page(self, page_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"pages/{page_id}")

    def append_blocks(
        self, page_id: str, children: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        return self._request(
            "PATCH", f"blocks/{page_id}/children", {"children": list(children)}
        )

    def replace_blocks(
        self, page_id: str, children: Sequence[Mapping[str, Any]]
    ) -> None:
        """Replace a projection body while preserving the Notion page itself."""

        existing: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            suffix = (
                f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            )
            page = self._request("GET", f"blocks/{page_id}/children{suffix}")
            existing.extend(
                item for item in page.get("results", []) if isinstance(item, Mapping)
            )
            if not page.get("has_more"):
                break
            cursor = str(page.get("next_cursor") or "").strip() or None
            if cursor is None:
                raise DepartmentNotionProjectionError(
                    "Notion block pagination omitted next_cursor"
                )

        self.append_blocks(page_id, children)
        for block in existing:
            block_id = str(block.get("id") or "").strip()
            if block_id:
                # ``in_trash`` is the effective Notion API field for block
                # removal.  ``archived`` alone can return HTTP 200 while
                # leaving the old block visible, creating duplicate reports.
                self._request("PATCH", f"blocks/{block_id}", {"in_trash": True})


@dataclass(frozen=True)
class DepartmentProjectionResult:
    status: str
    department: str | None = None
    task_id: str | None = None
    page_id: str | None = None
    duplicate: bool = False
    error: str | None = None
    risk_plan_id: str | None = None
    payload_hash: str | None = None
    delivery_status: str | None = None
    readback_status: str | None = None
    readback_hash: str | None = None
    evidence_status: str | None = None


def _title(value: str) -> dict[str, Any]:
    return {
        "title": [
            {
                "type": "text",
                "text": {"content": value[:1900]},
            }
        ]
    }


def _rich_text(value: Any) -> dict[str, Any]:
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": str(value or "")[:1900]},
            }
        ]
    }


def _date(value: Any) -> dict[str, Any] | None:
    stamp = iso_timestamp(value)
    if not stamp:
        return None
    return {"date": {"start": stamp}}


def _department(task: Mapping[str, Any]) -> str | None:
    profile = str(
        task.get("assignee") or task.get("profile") or task.get("assigned_to") or ""
    ).strip()
    if not profile:
        return None

    try:
        department = department_for_canonical_profile(profile)
    except (KeyError, ValueError):
        return None

    if department == "quant":
        return "quant-backtest"
    return department


def _task_title(task: Mapping[str, Any], department: str) -> str:
    """Build the manager-facing title without exposing a Kanban implementation ID."""

    tid = task_id(task)
    raw = str(
        task.get("title") or task.get("name") or task.get("subject") or ""
    ).strip()

    if not raw:
        raw = {
            "trading": "Trading department result",
            "quant-backtest": "Quant backtest result",
            "risk": "리스크 검토 결과",
            "accounting": "회계·포트폴리오 검토 결과",
        }.get(department, "부서 검토 결과")

    if department == "qa":
        raw = "QA 감사 결과"
    elif department == "hr":
        raw = raw.removeprefix("HR:").strip() or "Agent Workforce 검토 결과"
    elif department == "risk":
        raw = _humanize_risk_result(raw)

    if department in {"trading", "risk"}:
        completed = iso_timestamp(
            task.get("completed_at") or task.get("updated_at") or task.get("created_at")
        )
        if completed:
            # Seconds keep same-named PAPER operations distinct without
            # leading the page with the opaque ``t_…`` implementation ID.
            return f"{raw} · {completed[:19].replace('T', ' ')}"[:1900]
        return raw[:1900]

    return f"{tid} · {raw}"[:1900]


def _legacy_task_title(task: Mapping[str, Any], department: str) -> str:
    """Return a former ID-prefixed title for in-place migration lookup."""

    raw = str(
        task.get("title") or task.get("name") or task.get("subject") or ""
    ).strip() or (
        "리스크 검토 결과" if department == "risk" else "Trading department result"
    )
    if department == "risk":
        raw = _humanize_risk_result(raw)
    return f"{task_id(task)} · {raw}"[:1900]


def _legacy_task_titles(task: Mapping[str, Any], department: str) -> tuple[str, ...]:
    """Return prior title formats in migration order, without broad matching."""

    titles = [_legacy_task_title(task, department)]
    if department in {"trading", "risk"}:
        raw = str(
            task.get("title") or task.get("name") or task.get("subject") or ""
        ).strip() or (
            "리스크 검토 결과" if department == "risk" else "Trading department result"
        )
        if department == "risk":
            raw = _humanize_risk_result(raw)
        completed = iso_timestamp(
            task.get("completed_at") or task.get("updated_at") or task.get("created_at")
        )
        if completed:
            titles.append(f"{raw} · {completed[:16].replace('T', ' ')}"[:1900])
    return tuple(dict.fromkeys(titles))


def _result_text(task: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    """Prefer the complete user-facing result over a short handoff summary."""

    return (
        text_value(metadata.get("final_answer")).strip()
        or text_value(task.get("result")).strip()
        or summary(task, metadata).strip()
    )


_RISK_RESULT_LABELS = {
    "recommendation": "권고",
    "approval": "승인 판단",
    "binding_authority": "최종 판단 주체",
    "execution_boundary": "실행 경계",
    "execution_authority": "실행 권한",
    "paper_only": "분석용 가상거래 전용",
    "live_order_approval": "실거래 승인 여부",
    "mandate_status": "투자지침 상태",
    "risk_findings": "주요 위험 요인",
    "required_controls": "필수 통제 조건",
    "required_validation": "필수 검증 항목",
    "error": "오류·확인 필요 사유",
    "block_reason": "판단 보류 사유",
}


def _risk_result_value(value: Any) -> str:
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "\n".join(f"- {str(item).strip()}" for item in value if str(item).strip())
    if isinstance(value, Mapping):
        return ""
    rendered = str(value or "").strip()
    return {
        "none": "없음",
        "not_provided": "미제공",
        "REQUIRES_USER_REVIEW": "사람 검토 필요",
        "VETO_FOR_NOW": "현재 승인 보류",
        "DEFER": "판단 보류",
    }.get(rendered, rendered)


def _risk_structured_result_text(metadata: Mapping[str, Any]) -> str:
    """Render Risk result envelopes without leaking Python dict syntax."""

    result = metadata.get("result")
    if not isinstance(result, Mapping):
        result = metadata.get("structured_summary")
    if not isinstance(result, Mapping):
        return ""

    lines: list[str] = []
    scalar_keys = (
        "recommendation",
        "approval",
        "binding_authority",
        "execution_boundary",
        "execution_authority",
        "paper_only",
        "live_order_approval",
        "mandate_status",
    )
    scalar_rows = []
    for key in scalar_keys:
        value = _risk_result_value(result.get(key))
        if value:
            scalar_rows.append(f"- {_RISK_RESULT_LABELS[key]}: {value}")
    if scalar_rows:
        lines.extend(["### 판단", *scalar_rows])

    for key, aliases in (
        ("risk_findings", ("risk_findings", "review_findings")),
        ("required_controls", ("required_controls",)),
        ("required_validation", ("required_validation",)),
    ):
        value = next(
            (source.get(alias) for alias in aliases for source in (result, metadata) if source.get(alias)),
            None,
        )
        rendered = _risk_result_value(value)
        if rendered:
            lines.extend(["", f"### {_RISK_RESULT_LABELS[key]}", rendered])

    for key in ("error", "block_reason"):
        value = _risk_result_value(result.get(key)) or _risk_result_value(
            metadata.get(key)
        )
        if value:
            lines.extend(["", f"### {_RISK_RESULT_LABELS[key]}", value])
    return "\n".join(lines).strip()


def _risk_projection_properties(
    properties_schema: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Populate the existing human-named Risk columns from one metadata shape."""

    nested = metadata.get("result")
    nested = nested if isinstance(nested, Mapping) else {}
    structured = metadata.get("structured_summary")
    structured = structured if isinstance(structured, Mapping) else {}

    def first(*keys: str) -> Any:
        for key in keys:
            value = metadata.get(key)
            if value not in (None, "", [], {}):
                return value
            value = nested.get(key)
            if value not in (None, "", [], {}):
                return value
            value = structured.get(key)
            if value not in (None, "", [], {}):
                return value
        return None

    props: dict[str, Any] = {}

    def rich(field: str, value: Any) -> None:
        name = risk_property_name(field, properties_schema)
        rendered = _humanize_risk_result(_risk_result_value(value))
        if name in properties_schema and rendered:
            props[name] = _rich_text(rendered)

    def select(field: str, value: Any) -> None:
        name = risk_property_name(field, properties_schema)
        if name not in properties_schema or value in (None, ""):
            return
        rendered = _schema_select(properties_schema, name, str(value).strip())
        if rendered is not None:
            props[name] = rendered

    def checkbox(field: str, value: Any) -> None:
        if isinstance(value, bool):
            name = risk_property_name(field, properties_schema)
            rendered = _schema_checkbox(properties_schema, name, value)
            if rendered is not None:
                props[name] = rendered

    def number(field: str, value: Any) -> None:
        if value in (None, ""):
            return
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return
        if parsed.is_integer():
            parsed = int(parsed)
        name = risk_property_name(field, properties_schema)
        if name in properties_schema and properties_schema[name].get("type") == "number":
            props[name] = {"number": parsed}

    raw_verdict = first("verdict", "risk_verdict", "recommendation")
    canonical_verdict = {
        "APPROVED": "approve",
        "APPROVE": "approve",
        "PASS": "approve",
        "RESIZE": "resize",
        "REJECT": "reject",
        "REJECTED": "reject",
    }.get(str(raw_verdict or "").strip().upper())
    select("verdict", canonical_verdict)

    trading_state = first("trading_state", "trade_state")
    if str(trading_state or "").strip() in {"ENABLED", "REDUCE_ONLY", "ENTRY_BLOCKED", "HALTED"}:
        select("trading_state", trading_state)

    legal_verdict = first("legal_verdict", "compliance_verdict")
    legal_value = str(legal_verdict or "").strip().lower()
    if legal_value in {"ambiguous", "inconclusive"}:
        select("compliance_verdict", legal_value)
    elif legal_value in {"breach", "no_breach"} and first("legal_source_references"):
        select("compliance_verdict", "grounded")

    legal_escalate = first("legal_escalate", "escalate")
    if legal_escalate is None and legal_value in {"ambiguous", "inconclusive"}:
        legal_escalate = True
    checkbox("escalate", legal_escalate)

    reason_codes = first("reason_codes", "finding_codes")
    if isinstance(reason_codes, str):
        reason_codes = [reason_codes]
    if isinstance(reason_codes, Sequence) and not isinstance(
        reason_codes, (str, bytes, bytearray)
    ):
        name = risk_property_name("reason_codes", properties_schema)
        rendered = _schema_multi_select(properties_schema, name, reason_codes)
        if rendered is not None:
            props[name] = rendered

    rich("trade_case_id", first("trade_case_id"))
    rich("input_hash", first("input_hash", "content_hash"))
    rich("calculation_version", first("calculation_version"))
    rich("counterparty_health", first("counterparty_health"))
    rich("counterparty_narrative", first("counterparty_narrative"))
    number("approved_quantity", first("approved_quantity"))

    checks = first(
        "check_results", "risk_findings", "review_findings", "required_controls"
    )
    check_lines: list[str] = []
    if checks:
        rendered_checks = _risk_result_value(checks)
        if rendered_checks:
            check_lines.append(rendered_checks)

    calculation = first("calculation")
    if isinstance(calculation, Mapping):
        calculation_labels = {
            "cost_basis_krw": "취득원가",
            "market_value_krw": "현재 평가액",
            "unrealized_pnl_krw": "평가손익",
            "loss_rate_percent": "손실률",
            "rounding": "반올림 기준",
        }
        calculation_lines = []
        for key, label in calculation_labels.items():
            value = calculation.get(key)
            if value in (None, ""):
                continue
            if key.endswith("_krw") and isinstance(value, (int, float)):
                rendered_value = f"{value:,.0f}원"
            elif key == "loss_rate_percent" and isinstance(value, (int, float)):
                rendered_value = f"{value:.2f}%"
            else:
                rendered_value = str(value)
            calculation_lines.append(f"- {label}: {rendered_value}")
        if calculation_lines:
            check_lines.append("계산 요약:\n" + "\n".join(calculation_lines))

    legal_review = first("legal_review")
    if legal_review:
        check_lines.append(f"법률 검토: {_risk_result_value(legal_review)}")
    if legal_verdict or first("legal_wiki_calls", "legal_pages_visited"):
        legal_summary = []
        if legal_verdict:
            legal_summary.append(f"법률 검토 결과: {legal_verdict}")
        calls = first("legal_wiki_calls")
        if calls not in (None, ""):
            legal_summary.append(f"법률 Wiki 호출: {calls}회")
        refs = first("legal_source_references")
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes, bytearray)):
            legal_summary.append(f"공식 근거 좌표: {len(refs)}건")
        check_lines.extend(legal_summary)
    if check_lines:
        rich("check_results", "\n".join(check_lines))

    return props


def _humanize_risk_result(value: str) -> str:
    """Remove runtime field names from the manager-facing Risk projection."""

    value = strip_internal_handoff(value)
    replacements = (
        (
            "`unversioned·snapshot_resolvable=false`",
            "현재 유효한 투자지침 스냅샷을 확인할 수 없는 상태",
        ),
        (
            "unversioned·snapshot_resolvable=false",
            "현재 유효한 투자지침 스냅샷을 확인할 수 없는 상태",
        ),
        ("판단 보류 (DEFER)", "판단 보류"),
        ("적용 가능성 주의 (WARN)", "적용 가능성 주의"),
        ("gross 노출", "총액 기준 노출"),
        ("gross/net exposure", "총액·순액 노출"),
        ("Risk PAPER", "리스크 분석용 가상거래"),
        (
            "independent risk veto pending deterministic order-time 결정론적 리스크 검증 시스템 checks",
            "독립 리스크 검토가 필요하며 주문 시점의 결정론적 리스크 검증 결과를 확인해야 합니다",
        ),
        (
            "independent risk veto pending deterministic order-time checks",
            "독립 리스크 검토가 필요하며 주문 시점의 결정론적 리스크 검증 결과를 확인해야 합니다",
        ),
        ("deterministic order-time", "주문 시점 결정론적"),
        ("independent risk veto", "독립 리스크 검토"),
        ("cited_documents", "확인된 인용 문서"),
        ("cited documents", "확인된 인용 문서"),
        ("source_references", "공식 근거 좌표"),
        ("legal_wiki_calls", "법률 Wiki 호출 횟수"),
        ("legal_status", "법률 조회 상태"),
        ("legal_verdict", "법률 검토 결과"),
        ("legal_pages_visited", "확인한 법률 자료"),
        ("pages_visited", "확인한 법률 자료"),
        ("effective_from", "효력 시작일"),
        ("origin_url", "공식 출처 주소"),
        ("clause_id", "조문 번호"),
        ("Proposal-only", "제안 전용"),
        ("proposal-only", "제안 전용"),
        ("Workforce API", "인력 운영 조회 시스템"),
        ("Workforce", "인력 운영"),
        ("Scorecard", "성과표"),
        ("CEO delegated risk analysis", "CEO 위임 리스크 분석"),
        ("Risk 검토", "리스크 검토"),
        (
            "Risk approval: conditional PAPER orders for robotics stocks",
            "로보틱스 종목 조건부 분석용 가상거래 주문 리스크 승인 검토",
        ),
        ("Legal LLM Wiki", "법률 LLM Wiki"),
        ("one-shot", "1회 실행"),
        ("fast_advisory", "신속 검토"),
        ("standard_analysis", "일반 분석"),
        ("full_experiment", "전체 실험 분석"),
        ("NO_SNAPSHOT", "확인 자료 없음"),
        ("UNAVAILABLE", "관측 시스템에서 확인 불가"),
        ("Agent", "에이전트"),
        ("HR", "인사"),
        ("스냅샷 범위", "조회 자료 범위"),
        ("동결 스냅샷상", "동결된 조회 자료 기준"),
        ("미매핑", "분류되지 않음"),
        ("Mandate snapshot이", "투자지침 조회 자료가"),
        ("결정론적 Risk Engine 검증", "결정론적 리스크 검증 시스템"),
        ("비권위 스냅샷", "공식 확정 자료가 아닌 조회 자료"),
        ("섹터 미매핑", "섹터 분류 누락"),
        ("Accounting advisory snapshot", "회계 조회 자료"),
        ("투자지침 snapshot", "투자지침 조회 자료"),
        ("Advisory Risk", "자문성 리스크"),
        ("Risk Engine", "결정론적 리스크 검증 시스템"),
        ("Trading 활성화", "거래 활성화"),
        ("snapshot", "조회 자료"),
        ("max_gross_exposure", "총노출 한도"),
        ("max_instrument_weight", "종목 비중 한도"),
        ("max_sector_weight", "섹터 비중 한도"),
        ("max_concurrent_positions", "동시 보유 종목 수 한도"),
        ("quality_status=WARN", "자료 품질 상태: 주의"),
        ("quality_status=PASS", "자료 품질 상태: 확인"),
        ("quality_status", "자료 품질 상태"),
        ("authoritative=false", "공식 확정 자료가 아님"),
        ("authoritative=true", "공식 확정 자료"),
        ("unavailable_reference_mapping", "참조 분류 미확인"),
        ("REQUIRES_USER_REVIEW", "사람 검토 필요"),
        ("PROVISIONAL_CRYPTO", "가상자산"),
        ("as_of", "기준 시각"),
        ("ELEVATED", "주의"),
        ("HIGH", "높음"),
        ("LOW", "낮음"),
        ("DEFER", "판단 보류"),
        ("KOREA_EQUITY", "국내 주식"),
        ("PROVISIONAL_ETF", "임시 허용 ETF"),
        ("Mandate가", "투자지침이"),
        ("Mandate를", "투자지침을"),
        ("Mandate와", "투자지침과"),
        ("Mandate의", "투자지침의"),
        ("Mandate", "투자지침"),
        ("MODERATE", "보통"),
        ("위반 없음(no_breach)", "현재 입력만으로 위반을 확인하지 못함"),
        ("no_breach", "현재 입력만으로 위반을 확인하지 못함"),
        ("Risk 검증", "리스크 검증"),
    )
    humanized = value
    for internal, friendly in replacements:
        humanized = humanized.replace(internal, friendly)
    # ``NAV`` is also a substring of ``UNAVAILABLE``.  Restrict this
    # acronym replacement to a standalone runtime token so observability
    # statuses are not corrupted (for example ``U순자산 가치AILABLE``).
    humanized = re.sub(
        r"(?<![A-Za-z0-9_])NAV(?![A-Za-z0-9_])",
        "순자산 가치",
        humanized,
    )
    # Contextual replacements above run before the broad terms.  These
    # guards keep already-humanized sentences grammatical and idempotent when
    # an existing Notion page is projected more than once.
    humanized = humanized.replace(
        "투자지침 조회 자료이 주문", "투자지침 조회 자료가 주문"
    )
    humanized = humanized.replace(
        "결정론적 결정론적 리스크 검증 시스템 검증",
        "결정론적 리스크 검증 시스템",
    )
    humanized = humanized.replace(
        "독립 리스크 검토 pending 주문 시점 결정론적 결정론적 리스크 검증 시스템 checks",
        "독립 리스크 검토가 필요하며 주문 시점의 결정론적 리스크 검증 결과를 확인해야 합니다",
    )
    humanized = humanized.replace(
        "독립 리스크 검토 pending 주문 시점 결정론적 리스크 검증 시스템 checks",
        "독립 리스크 검토가 필요하며 주문 시점의 결정론적 리스크 검증 결과를 확인해야 합니다",
    )
    humanized = humanized.replace(
        "섹터 매핑은 5개 전부 분류되지 않음",
        "섹터 분류는 5개 모두 확인되지 않음",
    )
    humanized = humanized.replace(
        "섹터 분류되지 않음", "섹터 분류가 확인되지 않음"
    )
    humanized = re.sub(
        r"(조회 자료\([^\n)]*\))은",
        r"\1는",
        humanized,
    )
    humanized = re.sub(
        r"(?:PAPER(?: 가상거래)? 기준 |PAPER만으로는 )?"
        r"현재 입력만으로 위반을 확인하지 못함으로 "
        r"(?:보았|회신되었)지만",
        "법률 위반 여부를 확정할 수 없으며",
        humanized,
    )
    humanized = humanized.replace("PAPER", "분석용 가상거래")

    lines: list[str] = []
    for line in humanized.splitlines():
        if re.fullmatch(r"\s*error\s*:\s*(?:null|none|\"\")\s*", line, re.IGNORECASE):
            continue
        blocked = re.fullmatch(r"\s*block_reason\s*:\s*[\"']?(.*?)[\"']?\s*", line)
        if blocked:
            reason = blocked.group(1).strip().rstrip("\"'")
            if reason:
                lines.append(f"판단 보류 사유: {reason}")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _risk_body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    result_text: str,
    metadata: Mapping[str, Any],
) -> str:
    status = str(task.get("status") or "").casefold()
    status_label = "완료" if status in {"done", "completed"} else status or "미확인"
    title = _humanize_risk_result(
        str(task.get("title") or task.get("name") or "리스크 검토").strip()
    )
    parts = [
        "# 리스크 부서 검토 결과",
        "",
        "## 검토 정보",
        "",
        f"- 검토 제목: {title}",
        f"- 검토 ID: `{task_id(task)}`",
        f"- 상위 요청 ID: `{root_task_id}`",
        f"- 처리 상태: {status_label}",
        "",
        "## 검토 결과",
        "",
        result_text or "결과 본문이 없습니다.",
    ]

    rows = human_metadata_rows(metadata)
    if rows:
        parts.extend(["", "## 주요 운영 정보", ""])
        parts.extend(
            f"- {label}: {_humanize_risk_result(value)}" for label, value in rows
        )

    risk_plan = metadata.get("position_risk_plan") or metadata.get("risk_plan")
    if isinstance(risk_plan, Mapping):
        parts.extend(
            [
                "",
                "## 포지션 리스크 계획",
                "",
                format_position_risk_plan(risk_plan),
            ]
        )

    parts.extend(
        [
            "",
            (
                "> 이 페이지는 사람의 검토를 위한 읽기 전용 복사본입니다. "
                "최종 상태와 실행 권한은 리스크 원본 시스템과 승인된 검증 절차에서 관리합니다."
            ),
        ]
    )
    return "\n".join(parts)


def _accounting_body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    result_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Render the CEO/Kanban accounting handoff for a human manager."""

    status = str(task.get("status") or "").casefold()
    status_label = "완료" if status in {"done", "completed"} else status or "미확인"
    title = str(task.get("title") or task.get("name") or "회계·포트폴리오 검토").strip()
    parts = [
        "# 회계·포트폴리오 검토 결과",
        "",
        "## 검토 정보",
        "",
        f"- 검토 제목: {title}",
        f"- 업무 번호: `{task_id(task)}`",
        f"- 상위 요청 번호: `{root_task_id}`",
        f"- 처리 상태: {status_label}",
        "",
        "## 검토 결과",
        "",
        result_text or "결과 본문이 없습니다.",
    ]

    structured = metadata.get("structured_summary")
    if isinstance(structured, Mapping):
        labels = {
            "scope": "검토 범위",
            "as_of": "기준 시각",
            "source": "자료 기준",
            "status": "수치 상태",
            "nav": "순자산",
            "cash": "현금",
            "securities_value": "유가증권 평가액",
            "realized_pnl": "실현손익",
            "unrealized_pnl": "미실현손익",
            "fees": "수수료",
            "taxes": "세금",
            "open_breaks": "미해결 대사 차이",
            "valuation_evidence": "평가 근거",
            "paper_boundary": "운영 경계",
        }
        rows = []
        for key, label in labels.items():
            value = structured.get(key)
            if value not in (None, "", [], {}):
                rows.append(f"- {label}: {value}")
        if rows:
            parts.extend(["", "## 주요 수치와 확인 사항", "", *rows])

    parts.extend(
        [
            "",
            "> 이 기록은 읽기 전용 PAPER 검토 결과입니다. 주문, 원장 수정, 공식 NAV 확정은 수행하지 않았습니다.",
        ]
    )
    return "\n".join(parts)


def _humanize_accounting_result(value: str) -> str:
    """Keep runtime field names out of the manager-facing accounting page."""

    replacements = (
        ("기준시각(as_of)", "기준 시각"),
        ("기준시각", "기준 시각"),
        ("source_of_record", "자료 기준"),
        ("authoritative=false", "공식 확정 아님"),
        ("authoritative", "공식 확정 여부"),
        ("quality_status", "자료 품질 상태"),
        ("instrument_id", "종목 식별자"),
        ("valuation confirmation", "평가 확정 여부"),
        ("snapshot weight", "조회 자료 기준 비중"),
        ("snapshot", "조회 자료"),
        ("스냅샷", "조회 자료"),
        ("as_of", "기준 시각"),
        ("Reconciliation Break", "대사 차이"),
        ("Break", "대사 차이"),
        ("Long/Short", "롱/숏"),
        ("Long", "롱"),
        ("Short", "숏"),
        ("NAV close", "공식 NAV 확정"),
        ("contract=hgfinance.accounting-advisory-portfolio.v1", "회계 조회 자료 형식"),
        ("accounting.journals (Supabase)", "Accounting Engine 원장"),
        ("Fund/Book/Strategy", "펀드·장부·전략"),
        ("Posted Journal", "게시 원장"),
        ("reversing/additional entry", "역분개/추가 분개"),
    )
    humanized = value
    for internal, friendly in replacements:
        humanized = humanized.replace(internal, friendly)
    return humanized


def _humanize_hr_result(value: str) -> str:
    """Keep runtime markers and local artifact paths out of HR pages."""

    humanized = str(value or "")
    for internal, friendly in (
        ("NO_SNAPSHOT", "확인 자료 없음"),
        ("UNAVAILABLE", "관측 시스템에서 확인 불가"),
        ("proposal_only_pending_evidence", "근거 보강 후 재검토하는 조건부 제안"),
    ):
        humanized = humanized.replace(internal, friendly)
    return re.sub(r"/opt/data/shared-kanban/[^\s]+", "HR 제안서", humanized)


def _humanize_qa(value: Any, limit: int = 320) -> str:
    rendered = " ".join(str(value or "").split())
    replacements = (
        ("Accounting Engine", "회계 시스템"),
        ("accounting system", "회계 시스템"),
        ("Risk owner", "리스크 담당자"),
        ("Research handoff", "리서치 부서 전달"),
        ("Quant 백테스트 부서 및 Risk 부서", "정량 분석 부서·리스크 부서"),
        ("Quant 백테스트 부서", "정량 분석 부서"),
        ("Risk 부서", "리스크 부서"),
        ("Quant", "정량 분석 부서"),
        ("Risk", "리스크 부서"),
        ("Research", "리서치 부서"),
        ("NO_HYPOTHESIS", "등록된 가설 없음"),
        ("this workflow", "이번 업무 흐름"),
        ("해당 workflow", "해당 업무 흐름"),
        ("workflow 범위", "업무 흐름 범위"),
        ("answer_gaps", "응답의 근거 공백"),
        ("cited_documents", "인용 문서"),
        ("Experiment Spec", "실험 계획서"),
        ("read-only", "읽기 전용"),
        ("workflow observation", "업무 흐름 관측"),
        ("workflow_observation", "업무 흐름 관측"),
        ("candidate_count", "개선 후보 수"),
        ("bounded receipt", "제한된 확인 기록"),
        ("hash", "무결성 값"),
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
        ("broker_evidence", "브로커 증거"),
        ("artifact/citation 좌표", "근거 좌표"),
        ("artifact/citation", "근거 좌표"),
        ("artifact", "근거 자료"),
        ("citation coordinates", "인용 좌표"),
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
        (
            "No investment/trading eligibility decision until evidence is independently verified",
            "근거를 독립적으로 확인하기 전에는 투자·거래 적격성을 결정하지 않음",
        ),
        ("Mandate", "투자지침"),
        ("and 회계 시스템", "및 회계 시스템"),
        ("and Accounting", "및 회계"),
        ("broker reconciliation", "브로커 대사"),
        ("snapshot", "조회 자료"),
        ("Require ", "필요: "),
        ("next_ceo_synthesis", "다음 CEO 종합"),
        ("NAV", "순자산"),
        ("PIT", "기준 시점"),
        ("Point-in-Time", "기준 시점"),
        ("provenance", "자료 출처·계보"),
        ("근거 자료 자료", "근거 자료"),
        ("ceo-workflow", "CEO 업무 흐름"),
        ("accounting-portfolio-department", "회계·포트폴리오 부서"),
        ("부서 부서", "부서"),
        ("MEDIUM", "중간"),
        ("CRITICAL", "매우 높음"),
        ("BLOCKER", "차단"),
        ("HIGH", "높음"),
        ("LOW", "낮음"),
        ("DEFER", "보류"),
        ("FAIL", "실패"),
        ("PASS", "통과"),
        ("WARN", "주의"),
    )
    for internal, friendly in replacements:
        rendered = rendered.replace(internal, friendly)
    rendered = re.sub(
        r"\bKRW\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+))(?=\D|$)",
        r"\1원",
        rendered,
    )
    return rendered[:limit]


def _qa_decision_label(value: Any) -> str:
    return {
        "PASS": "통과",
        "WARN": "주의",
        "CONDITIONAL": "조건부 통과",
        "CONDITIONAL PASS": "조건부 통과",
        "FAIL": "실패·결정 차단",
        "DEFER": "판단 보류",
    }.get(str(value or "").strip().upper(), "확인 필요")


def _qa_findings_lines(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [f"- {_humanize_qa(value)}"] if value else []
    lines: list[str] = []
    for item in value[:8]:
        if isinstance(item, Mapping):
            severity = _humanize_qa(
                str(item.get("severity") or "확인 필요").upper(), 24
            )
            issue = _humanize_qa(
                item.get("summary")
                or item.get("statement")
                or item.get("description")
                or item.get("issue")
                or item.get("message")
                or item.get("title")
                or item.get("finding")
                or item.get("detail")
                or "세부 근거가 기록되지 않은 점검 항목",
                300,
            )
            owner = _humanize_qa(
                qa_owner_label(item.get("owner") or item.get("responsible_party")),
                100,
            )
            block = _humanize_qa(item.get("block_condition") or item.get("impact"), 180)
            status_value = str(item.get("status") or "").strip().upper()
            status = {
                "OPEN": "확인 필요",
                "CLOSED": "해결됨",
                "FAIL": "실패",
                "WARN": "주의",
                "PASS": "통과",
            }.get(status_value, _humanize_qa(item.get("status"), 32))
            due_date = _humanize_qa(item.get("due_date") or item.get("due"), 32)
            suffix = f" 담당: {owner}" if owner else ""
            if block:
                suffix += f" 영향: {block}"
            if status:
                suffix += f" 상태: {status}"
            if due_date:
                suffix += f" 기한: {due_date}"
            recommended_action = _humanize_qa(
                item.get("recommended_action")
                or item.get("required_action")
                or item.get("action"),
                220,
            )
            if recommended_action:
                suffix += f" 조치: {recommended_action}"
            lines.append(f"- [{severity}] {issue}{suffix}")
        elif item:
            lines.append(f"- {_humanize_qa(item)}")
    return lines


def _qa_evidence_lines(value: Any, *, limit: int = 4) -> list[str]:
    """Render bounded QA facts without exposing structured field names."""

    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
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
            lines.append(f"- {_humanize_qa(item, 260)}")
    return lines


def _qa_check_lines(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in list(value.items())[:12]:
            if isinstance(item, Mapping):
                result = _qa_decision_label(item.get("result") or item.get("status"))
                detail = _humanize_qa(item.get("detail") or item.get("reason"), 180)
            else:
                result = _qa_decision_label(item)
                detail = ""
            name = qa_check_label(key)
            lines.append(f"- {name}: {result}{f' ({detail})' if detail else ''}")
        return lines
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [f"- {_humanize_qa(value)}"] if value else []
    lines: list[str] = []
    for item in value[:12]:
        if isinstance(item, Mapping):
            raw_name = str(item.get("check") or item.get("name") or "확인 항목")
            name = qa_check_label(raw_name)
            result = _qa_decision_label(item.get("result") or item.get("status"))
            detail = _humanize_qa(item.get("detail") or item.get("reason"), 180)
            lines.append(f"- {name}: {result}{f' ({detail})' if detail else ''}")
        elif item:
            lines.append(f"- {_humanize_qa(item)}")
    return lines


_QA_SEVERITY_ALIASES = {
    "BLOCKER": "CRITICAL",
    "ERROR": "HIGH",
    "WARNING": "MEDIUM",
    "WARN": "MEDIUM",
}
_QA_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
QA_NOTION_PROPERTY_TYPES = {
    "제목": {"type": "title"},
    "판정": {"type": "select"},
    "findings severity": {"type": "select"},
    "findings": {"type": "rich_text"},
    "claim_checks": {"type": "rich_text"},
    "claim_checks 판정": {"type": "multi_select"},
    "claim_narrative": {"type": "rich_text"},
    "원본 리포트": {"type": "rich_text"},
    "reason_codes": {"type": "multi_select"},
    "input_hash": {"type": "rich_text"},
    "calculation_version": {"type": "rich_text"},
    "escalate": {"type": "checkbox"},
    "생성 시각": {"type": "date"},
}


def _schema_rich_text(
    properties_schema: Mapping[str, Any], name: str, value: Any
) -> dict[str, Any] | None:
    spec = properties_schema.get(name)
    if (
        not isinstance(spec, Mapping)
        or spec.get("type") != "rich_text"
        or value in (None, "")
    ):
        return None
    return _rich_text(value)


def _schema_multi_select(
    properties_schema: Mapping[str, Any], name: str, values: Any
) -> dict[str, Any] | None:
    spec = properties_schema.get(name)
    if not isinstance(spec, Mapping) or spec.get("type") != "multi_select":
        return None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        return None
    allowed = {
        str(option.get("name"))
        for option in (spec.get("multi_select") or {}).get("options", [])
        if isinstance(option, Mapping) and option.get("name")
    }
    selected: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("code") or value.get("value")
        candidate = str(value or "").strip()
        if (not allowed or candidate in allowed) and candidate not in selected:
            selected.append(candidate)
    return {"multi_select": [{"name": value} for value in selected]} if selected else None


def _qa_finding_severities(findings: Any) -> list[str]:
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes, bytearray)):
        return []
    values: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        raw = str(finding.get("severity") or "").strip().upper()
        if raw and raw not in values:
            values.append(raw)
    return values


def _qa_highest_severity(value: Any, findings: Any) -> list[str]:
    candidates = [str(value or "").strip().upper(), *_qa_finding_severities(findings)]
    candidates = [candidate for candidate in candidates if candidate]
    if not candidates:
        return []
    highest = max(
        candidates,
        key=lambda candidate: _QA_SEVERITY_RANK.get(
            _QA_SEVERITY_ALIASES.get(candidate, candidate), 0
        ),
    )
    return [highest, _QA_SEVERITY_ALIASES.get(highest, highest)]


def _qa_claim_check_results(checks: Any) -> list[str]:
    values: list[str] = []
    items = list(checks.items()) if isinstance(checks, Mapping) else checks
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return values
    for item in items:
        raw = item[1] if isinstance(checks, Mapping) else item
        if isinstance(raw, Mapping):
            raw = raw.get("result") or raw.get("status")
        status = str(raw or "").strip().upper()
        # Some terminal receipts keep a human-readable basis after the
        # decision (for example ``PASS — ...``).  Read only the leading
        # decision token so the same canonical multi-select is populated for
        # both compact and narrated receipts.
        status_match = re.match(
            r"^(PASS|SUPPORTED|OK|WARN|WARNING|CONDITIONAL|PARTIAL|FAIL|"
            r"UNSUPPORTED|BLOCK|CONTRADICTED|NOT_APPLICABLE)\b",
            status,
        )
        if status_match:
            status = status_match.group(1)
        mapped = {
            "PASS": "SUPPORTED",
            "SUPPORTED": "SUPPORTED",
            "OK": "SUPPORTED",
            "WARN": "PARTIAL",
            "WARNING": "PARTIAL",
            "CONDITIONAL": "PARTIAL",
            "PARTIAL": "PARTIAL",
            "FAIL": "UNSUPPORTED",
            "UNSUPPORTED": "UNSUPPORTED",
            "BLOCK": "UNSUPPORTED",
            "CONTRADICTED": "CONTRADICTED",
            "NOT_APPLICABLE": "NOT_APPLICABLE",
        }.get(status)
        if mapped and mapped not in values:
            values.append(mapped)
    return values


_QA_REASON_CODES = frozenset(
    {
        "evidence_not_found",
        "evidence_access_denied",
        "evidence_not_yet_valid",
        "numeric_citation_mismatch",
        "fact_without_evidence",
        "unacknowledged_contradiction",
        "tool_summary_deviation",
        "partial_evidence_set",
        "pipeline_fallback",
    }
)


def _qa_reason_codes(findings: Any, checks: Any) -> list[str]:
    """Return only evidence-backed reason codes accepted by the live schema."""

    values: list[str] = []
    if isinstance(findings, Sequence) and not isinstance(
        findings, (str, bytes, bytearray)
    ):
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            raw_values = finding.get("reason_codes") or finding.get("reason_code")
            if isinstance(raw_values, str):
                raw_values = [raw_values]
            if isinstance(raw_values, Sequence) and not isinstance(
                raw_values, (bytes, bytearray)
            ):
                for value in raw_values:
                    candidate = str(value or "").strip()
                    if candidate in _QA_REASON_CODES and candidate not in values:
                        values.append(candidate)

            code = str(finding.get("code") or "").strip().upper()
            text = " ".join(
                str(finding.get(key) or "")
                for key in ("statement", "finding", "description", "impact")
            ).casefold()
            if (
                code.startswith(("QA-HR-", "E2E_", "INDEPENDENT_REPLAY"))
                or any(
                    marker in text
                    for marker in (
                        "검증되지 않",
                        "확인되지 않",
                        "근거 공백",
                        "원문",
                        "unverified",
                        "unknown",
                    )
                )
            ) and "partial_evidence_set" not in values:
                values.append("partial_evidence_set")

    if not values:
        items = list(checks.items()) if isinstance(checks, Mapping) else checks
        if isinstance(items, Sequence) and not isinstance(
            items, (str, bytes, bytearray)
        ):
            for item in items:
                raw = item[1] if isinstance(checks, Mapping) else item
                raw = raw.get("result") or raw.get("status") if isinstance(raw, Mapping) else raw
                if re.match(r"^(WARN|WARNING|PARTIAL|FAIL|BLOCK)\b", str(raw or "").strip().upper()):
                    values.append("partial_evidence_set")
                    break
    return values


def _qa_fallback_input_hash(
    *, title: str, findings: Any, checks: Any, claim_narrative: Any
) -> str:
    """Hash the bounded audit material when the worker supplied no input hash."""

    material = {
        "title": title,
        "findings": findings,
        "checks": checks,
        "claim_narrative": claim_narrative,
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_qa_notion_properties(
    properties_schema: Mapping[str, Any],
    *,
    title: str,
    verdict: Any,
    findings: Any = (),
    checks: Any = (),
    highest_severity: Any = None,
    claim_narrative: Any = "",
    input_hash: Any = None,
    calculation_version: Any = None,
    reason_codes: Any = (),
    escalate: Any = None,
    created_at: Any = None,
    original_report: Any = "",
) -> dict[str, Any]:
    """Build the single schema-aware QA property projection used by both paths."""

    schema = properties_schema or QA_NOTION_PROPERTY_TYPES
    props: dict[str, Any] = {}
    if isinstance(schema.get("제목"), Mapping):
        props["제목"] = _title(title)
    canonical = {
        "CONDITIONAL PASS": "CONDITIONAL",
        "CONDITIONAL_PASS": "CONDITIONAL",
        "REJECT": "FAIL",
        "BLOCK": "FAIL",
    }.get(str(verdict or "WARN").strip().upper(), str(verdict or "WARN").strip().upper())
    decision = _schema_select(schema, "판정", canonical)
    if decision is not None:
        props["판정"] = decision

    for candidate in _qa_highest_severity(highest_severity, findings):
        severity = _schema_select(schema, "findings severity", candidate)
        if severity is not None:
            props["findings severity"] = severity
            break

    finding_text = "\n".join(_qa_findings_lines(findings)) or "- 확인된 문제 없음"
    check_text = "\n".join(_qa_check_lines(checks)) or "- 세부 점검 없음"
    narrative_text = claim_narrative or original_report or "- 주장 서술 없음"
    if input_hash in (None, ""):
        input_hash = _qa_fallback_input_hash(
            title=title,
            findings=findings,
            checks=checks,
            claim_narrative=narrative_text,
        )
    if calculation_version in (None, ""):
        calculation_version = "QA 감사 양식 v2"
    for name, value in (
        ("findings", finding_text),
        ("claim_checks", check_text),
        ("claim_narrative", narrative_text),
        ("input_hash", input_hash),
        ("calculation_version", calculation_version),
        ("원본 리포트", original_report),
    ):
        property_value = _schema_rich_text(schema, name, value)
        if property_value is not None:
            props[name] = property_value

    check_results = _schema_multi_select(
        schema, "claim_checks 판정", _qa_claim_check_results(checks)
    )
    if check_results is not None:
        props["claim_checks 판정"] = check_results
    reason_values = _schema_multi_select(
        schema,
        "reason_codes",
        reason_codes or _qa_reason_codes(findings, checks),
    )
    if reason_values is not None:
        props["reason_codes"] = reason_values

    if escalate is None:
        escalate = canonical == "FAIL"
    checkbox = _schema_checkbox(schema, "escalate", bool(escalate))
    if checkbox is not None:
        props["escalate"] = checkbox
    date_value = _date(created_at)
    if date_value is not None and isinstance(schema.get("생성 시각"), Mapping):
        props["생성 시각"] = date_value
    return props


def _qa_summary_text(
    *, task: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    """Create a Korean summary from structured QA fields, not raw run prose."""

    verdict = (
        metadata.get("verdict")
        or metadata.get("qa_verdict")
        or metadata.get("overall")
        or metadata.get("overall_status")
        or metadata.get("qa_status")
        or metadata.get("qa_result")
        or metadata.get("audit_result")
        or task.get("verdict")
    )
    numerical = (
        metadata.get("numerical_posture")
        or metadata.get("numeric_posture")
        or metadata.get("decision")
    )
    checks = _qa_check_lines(qa_projection_checks(task, metadata))
    findings = _qa_findings_lines(qa_projection_findings(task, metadata))
    passed = sum("통과" in line for line in checks)
    return (
        f"QA 검토를 완료했습니다. 종합 판정은 {_qa_decision_label(verdict)}이며, "
        f"수치 판단은 {_qa_decision_label(numerical) if numerical else '확인 필요'}입니다. "
        f"세부 점검 {len(checks)}건 중 통과 {passed}건, 보완이 필요한 문제 {len(findings)}건을 확인했습니다. "
        "실패·주의 항목을 해소하기 전에는 공식 수치 확정과 투자 결정을 진행하지 않습니다."
    )


def _qa_body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    result_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Render a manager-readable QA decision without runtime field names."""

    verdict = (
        metadata.get("verdict")
        or metadata.get("qa_verdict")
        or metadata.get("overall")
        or metadata.get("overall_status")
        or metadata.get("qa_status")
        or metadata.get("qa_result")
        or task.get("verdict")
    )
    numerical = (
        metadata.get("numerical_posture")
        or metadata.get("numeric_posture")
        or metadata.get("decision")
    )
    findings = _qa_findings_lines(qa_projection_findings(task, metadata))
    checks = _qa_check_lines(qa_projection_checks(task, metadata))
    verified_facts = _qa_evidence_lines(
        metadata.get("verified_facts") or task.get("verified_facts")
    )
    unknowns = _qa_evidence_lines(
        metadata.get("unknowns") or task.get("unknowns"), limit=3
    )
    status = str(task.get("status") or "").casefold()
    status_label = "완료" if status in {"done", "completed"} else "확인 필요"
    parts = [
        "# QA 감사 결과",
        "",
        "## 검토 정보",
        "",
        f"- 검토 업무: `{task_id(task)}`",
        f"- 상위 업무: `{root_task_id}`",
        f"- 처리 상태: {status_label}",
        f"- 종합 판정: {_qa_decision_label(verdict)}",
        f"- 수치 판단: {_qa_decision_label(numerical) if numerical else '확인 필요'}",
        "",
        "## 확인 결과",
        "",
    ]
    parts.extend(checks or ["- 세부 점검 결과가 없습니다."])
    if verified_facts:
        parts.extend(["", "## 확인된 근거", ""])
        parts.extend(verified_facts)
    if unknowns:
        parts.extend(["", "## 아직 확인되지 않은 점", ""])
        parts.extend(unknowns)
    parts.extend(["", "## 주요 문제와 영향", ""])
    parts.extend(findings or ["- 중대한 문제 항목이 기록되지 않았습니다."])
    parts.extend(
        [
            "",
            "## QA 요약",
            "",
            _qa_summary_text(task=task, metadata=metadata),
        ]
    )
    parts.extend(
        [
            "",
            "## 후속 조치",
            "",
            "- 실패·주의 항목의 원자료와 대사 근거를 보완한 뒤 QA를 다시 실행합니다.",
            "- QA 승인 전에는 공식 수치 확정이나 투자 결정을 진행하지 않습니다.",
            "",
            "> PAPER·읽기 전용 검토입니다. 주문 제출과 원장 변경은 수행하지 않았습니다.",
        ]
    )
    return "\n".join(parts)


def _hr_body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    result_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Render a concise Korean HR projection for managers.

    The supervisor's CEO/Kanban path shares the native HR database. Keep this
    page readable without runtime field names, raw JSON, worker session IDs,
    or local workspace paths.
    """

    # HR worker versions keep the authoritative read snapshot under different
    # terminal envelopes.  Flatten only the small manager-facing facts needed
    # by this page so a successful run is not displayed as "확인 필요".
    normalized = dict(metadata)
    worker_result = metadata.get("result")
    if isinstance(worker_result, Mapping):
        improvements = worker_result.get("improvements")
        if isinstance(improvements, Mapping):
            normalized.setdefault(
                "improvement_candidates", improvements.get("candidate_count")
            )
        if normalized.get("improvement_candidates") in (None, ""):
            normalized["improvement_candidates"] = worker_result.get(
                "improvement_candidate_count"
            )
        idle_agents = worker_result.get("idle_agents")
        if isinstance(idle_agents, Mapping):
            statuses = idle_agents.get("statuses")
            if isinstance(statuses, Mapping) and statuses.get("UNAVAILABLE"):
                normalized.setdefault("observability_risk", "UNAVAILABLE")
        observability_result = worker_result.get("observability")
        if isinstance(observability_result, Mapping):
            unavailable = observability_result.get("UNAVAILABLE")
            if unavailable is None:
                unavailable = observability_result.get("unavailable_count")
            normalized.setdefault(
                "observability_risk",
                "UNAVAILABLE" if unavailable else observability_result.get("status"),
            )
        scorecard_result = worker_result.get("scorecard")
        if isinstance(scorecard_result, Mapping):
            quality_value = scorecard_result.get("both_quality")
            if quality_value is None and scorecard_result.get(
                "quality_eval_run_references"
            ) is not None:
                quality_value = "확인 자료 없음"
            normalized["risk_scorecard"] = {
                "capacity": scorecard_result.get("both_capacity")
                or scorecard_result.get("capacity"),
                "cost": scorecard_result.get("both_cost")
                or scorecard_result.get("cost"),
                "quality_metrics": "확인 자료 없음"
                if quality_value is not None
                else scorecard_result.get("quality"),
            }
        api_checks = worker_result.get("api_checks")
        if isinstance(api_checks, Sequence) and not isinstance(api_checks, (str, bytes)):
            for check in api_checks:
                if not isinstance(check, Mapping):
                    continue
                endpoint = str(check.get("endpoint") or "")
                if "/improvements" in endpoint:
                    normalized.setdefault(
                        "improvement_candidates", check.get("candidate_count")
                    )
                elif "/observability" in endpoint and worker_result.get("idle_agents"):
                    normalized.setdefault("observability_risk", "UNAVAILABLE")
        idle_agents = worker_result.get("idle_agents")
        if (
            isinstance(idle_agents, Sequence)
            and not isinstance(idle_agents, (str, bytes))
            and any(
                isinstance(agent, Mapping)
                and str(agent.get("status") or "").strip() == "UNAVAILABLE"
                for agent in idle_agents
            )
        ):
            normalized.setdefault("observability_risk", "UNAVAILABLE")
        summary_metadata = metadata.get("summary")
        if isinstance(summary_metadata, Mapping):
            observation = str(summary_metadata.get("scorecard_observation") or "")
            if "NO_SNAPSHOT" in observation:
                normalized["risk_scorecard"] = {
                    "capacity": "NO_SNAPSHOT",
                    "cost": "NO_SNAPSHOT",
                    "quality_metrics": "NO_SNAPSHOT",
                }

    # The latest HR worker keeps receipts at the top level of run metadata.
    direct_observability = metadata.get("observability")
    direct_scorecard = metadata.get("scorecard")
    direct_summary = metadata.get("summary")
    if isinstance(direct_observability, Mapping) and isinstance(
        direct_scorecard, Mapping
    ):
        if isinstance(direct_summary, Mapping):
            normalized.setdefault(
                "improvement_candidates",
                direct_summary.get("improvement_candidate_count"),
            )
        states = direct_observability.get("states") or direct_observability.get(
            "idle_state_counts"
        )
        if isinstance(states, Mapping) and states.get("UNAVAILABLE"):
            normalized.setdefault("observability_risk", "UNAVAILABLE")
        capacity = direct_scorecard.get("capacity")
        cost = direct_scorecard.get("cost")
        quality = direct_scorecard.get("quality")
        normalized["risk_scorecard"] = {
            "capacity": "NO_SNAPSHOT" if isinstance(capacity, Mapping) else capacity,
            "cost": "NO_SNAPSHOT" if isinstance(cost, Mapping) else cost,
            "quality_metrics": "NO_SNAPSHOT"
            if isinstance(quality, Mapping)
            else quality,
        }
    if isinstance(metadata.get("proposal_only_job_profile"), Mapping):
        normalized.setdefault("proposal_only", True)

    endpoint_receipts = metadata.get("endpoints")
    if isinstance(endpoint_receipts, Sequence) and not isinstance(
        endpoint_receipts, (str, bytes)
    ):
        receipts = [item for item in endpoint_receipts if isinstance(item, Mapping)]

        def _receipt(fragment: str) -> Mapping[str, Any]:
            return next(
                (
                    item
                    for item in receipts
                    if fragment in str(item.get("path") or item.get("endpoint") or "")
                ),
                {},
            )

        improvements = _receipt("/improvements")
        observability = _receipt("/observability")
        scorecard = _receipt("/scorecard-brief")
        normalized.setdefault(
            "improvement_candidates", improvements.get("candidate_count")
        )
        idle_status_counts = observability.get("idle_status_counts")
        if isinstance(idle_status_counts, Mapping) and idle_status_counts.get(
            "UNAVAILABLE"
        ):
            normalized.setdefault("observability_risk", "UNAVAILABLE")
        capacity = scorecard.get("capacity_observation")
        cost = scorecard.get("cost_observation")
        quality_refs = scorecard.get("quality_eval_run_references")
        normalized["risk_scorecard"] = {
            "capacity": "NO_SNAPSHOT"
            if "NO_SNAPSHOT" in str(capacity)
            else capacity,
            "cost": "NO_SNAPSHOT" if "NO_SNAPSHOT" in str(cost) else cost,
            "quality_metrics": "NO_SNAPSHOT"
            if isinstance(quality_refs, Mapping)
            and all(not value for value in quality_refs.values())
            else quality_refs,
        }
        if any("proposal-only" in str(item) for item in metadata.get("actions_not_taken", [])):
            normalized.setdefault("proposal_only", True)

    # The durable Kanban run contract stores the same authoritative snapshot
    # under ``api_reads``.  Keep this projection tolerant of both envelopes so
    # a successful HR run is not rendered as an unknown result for managers.
    api_reads = metadata.get("api_reads")
    if isinstance(api_reads, Mapping):
        improvements = api_reads.get("improvements")
        if isinstance(improvements, Mapping):
            normalized.setdefault(
                "improvement_candidates", improvements.get("candidate_count")
            )

        observability_reads = api_reads.get("observability")
        if isinstance(observability_reads, Mapping):
            counts = observability_reads.get("idle_state_counts")
            unavailable = (
                counts.get("UNAVAILABLE")
                if isinstance(counts, Mapping)
                else observability_reads.get("unavailable_count")
            )
            if unavailable:
                normalized.setdefault("observability_risk", "UNAVAILABLE")

        scorecard_reads = api_reads.get("scorecard_brief")
        if isinstance(scorecard_reads, Mapping):
            def _same_status(value: Any, expected: str) -> bool:
                if isinstance(value, Mapping):
                    values = [str(item).strip() for item in value.values()]
                    return bool(values) and all(item == expected for item in values)
                return str(value or "").strip() == expected

            capacity = scorecard_reads.get("capacity")
            cost = scorecard_reads.get("cost")
            quality = scorecard_reads.get("quality")
            normalized["risk_scorecard"] = {
                "capacity": "NO_SNAPSHOT"
                if _same_status(capacity, "NO_SNAPSHOT")
                else capacity,
                "cost": "NO_SNAPSHOT"
                if _same_status(cost, "NO_SNAPSHOT")
                else cost,
                "quality_metrics": "NO_SNAPSHOT"
                if isinstance(quality, Mapping)
                and all(
                    isinstance(item, Mapping)
                    and not item.get("eval_run_refs")
                    and str(item.get("eval_score") or "—") in {"—", ""}
                    for item in quality.values()
                )
                else quality,
            }

        if api_reads.get("proposal_only") is True:
            normalized.setdefault("proposal_only", True)

    metadata = normalized

    recommendation = str(metadata.get("recommendation") or "").strip()
    if not recommendation and (
        metadata.get("block_reason") or metadata.get("proposal_only") is True
    ):
        recommendation = "proposal_only_pending_evidence"
    recommendation_label = {
        "proposal_only_pending_evidence": "근거 보강 후 재검토하는 조건부 제안",
    }.get(recommendation, "제안 상태")

    scorecard = metadata.get("risk_scorecard")
    scorecard = scorecard if isinstance(scorecard, Mapping) else {}
    capacity = str(scorecard.get("capacity") or "").strip()
    cost = str(scorecard.get("cost") or "").strip()
    quality = str(scorecard.get("quality_metrics") or "").strip()
    observability = str(metadata.get("observability_risk") or "").strip()

    def status_label(value: str, *, unavailable: str) -> str:
        return {
            "NO_SNAPSHOT": "확인 자료 없음",
            "UNAVAILABLE": unavailable,
            "—": "기록 없음",
        }.get(value, value or "확인 필요")

    title = str(task.get("title") or "Agent Workforce 검토").strip()
    readable_result = _humanize_hr_result(result_text) or "제안서가 작성되었습니다."
    lines = [
        "# HR 부서 업무·성과 요약",
        "",
        "## 요청 업무",
        "",
        f"- 검토 내용: {title.removeprefix('HR:').strip()}",
        f"- 상위 업무 번호: {root_task_id}",
        "- 처리 상태: 완료",
        "",
        "## 수행 내용",
        "",
        "- 리스크 분석 보조 Agent의 역할·책임·금지 범위를 설계했습니다.",
        "- Golden 평가 사례와 Adversarial 평가 사례를 제안했습니다.",
        "- 현재 인력 운영 근거와 관측 가능 여부를 확인했습니다.",
        "",
        "## 핵심 결과",
        "",
        f"- 결론: {recommendation_label}",
        f"- HR 결과: {readable_result}",
        f"- 개선 후보: {metadata.get('improvement_candidates', '확인 필요')}건",
        f"- 처리량 자료: {status_label(capacity, unavailable='확인 불가')}",
        f"- 비용 자료: {status_label(cost, unavailable='확인 불가')}",
        f"- 품질 지표: {status_label(quality, unavailable='확인 불가')}",
        f"- 최근 24시간 관측: {status_label(observability, unavailable='관측 시스템에서 확인 불가')}",
        "",
        "## 승인·운영 경계",
        "",
        "- CEO 승인, AI QA 독립 검증, Platform/IAM 권한 부여와 Agent 활성화는 수행하지 않았습니다.",
        "- 실제 주문·투자 판단·원장 변경은 수행하지 않았습니다.",
        "- 관측·품질·비용 자료를 보강한 뒤 독립 검증과 승인 절차를 다시 진행해야 합니다.",
    ]
    return "\n".join(lines)


def _schema_select(
    properties_schema: Mapping[str, Any], name: str, value: str
) -> dict[str, Any] | None:
    spec = properties_schema.get(name)
    if not isinstance(spec, Mapping) or spec.get("type") != "select":
        return None
    options = spec.get("select", {}).get("options", [])
    allowed = {
        str(option.get("name"))
        for option in options
        if isinstance(option, Mapping) and option.get("name")
    }
    return {"select": {"name": value}} if not allowed or value in allowed else None


def _schema_checkbox(
    properties_schema: Mapping[str, Any], name: str, value: bool
) -> dict[str, Any] | None:
    spec = properties_schema.get(name)
    return {"checkbox": bool(value)} if isinstance(spec, Mapping) and spec.get("type") == "checkbox" else None


def _notion_property_signature(value: Any) -> Any:
    """Reduce a Notion property to a stable, non-secret readback value."""

    if not isinstance(value, Mapping):
        return None
    property_type = str(value.get("type") or "").strip()
    raw = value.get(property_type) if property_type else None
    if property_type in {"title", "rich_text"}:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return ""
        return "".join(
            str(item.get("plain_text") or "")
            for item in raw
            if isinstance(item, Mapping)
        )
    if property_type == "select":
        return (
            str(raw.get("name") or "")
            if isinstance(raw, Mapping)
            else ""
        )
    if property_type == "multi_select":
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return ()
        return tuple(
            sorted(
                str(item.get("name") or "")
                for item in raw
                if isinstance(item, Mapping) and item.get("name")
            )
        )
    if property_type == "date":
        return _notion_date_signature(raw)
    if property_type in {"checkbox", "number"}:
        return raw
    return None


def _expected_property_signature(value: Any) -> Any:
    """Use the same shape as `_notion_property_signature` for outgoing props."""

    if not isinstance(value, Mapping):
        return None
    if "title" in value or "rich_text" in value:
        key = "title" if "title" in value else "rich_text"
        items = value.get(key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            return ""
        return "".join(
            str(
                item.get("plain_text")
                or (item.get("text") or {}).get("content")
                or ""
            )
            for item in items
            if isinstance(item, Mapping)
        )
    if "select" in value:
        selected = value.get("select")
        return str(selected.get("name") or "") if isinstance(selected, Mapping) else ""
    if "multi_select" in value:
        selected = value.get("multi_select")
        if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes, bytearray)):
            return ()
        return tuple(
            sorted(
                str(item.get("name") or "")
                for item in selected
                if isinstance(item, Mapping) and item.get("name")
            )
        )
    for key in ("date", "checkbox", "number"):
        if key in value:
            raw = value.get(key)
            if key == "date":
                return _notion_date_signature(raw)
            return raw
    return None


def _notion_date_signature(value: Any) -> str:
    """Compare Notion dates at the precision retained by the database column."""

    if not isinstance(value, Mapping):
        return ""
    start = str(value.get("start") or "")
    # The live Notion date column currently retains minute precision even when
    # the API accepts seconds.  Keep the readback check strict for the stored
    # precision without declaring an otherwise identical write failed.
    return start[:16] if len(start) >= 16 and start[10:11] == "T" else start


def _trading_tool_result(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the bounded Trading MCP receipt, never a raw tool transcript."""

    value = metadata.get("tool_result")
    return value if isinstance(value, Mapping) else {}


def _trading_value_label(value: Any, *, kind: str = "") -> str:
    raw = str(value or "").strip()
    labels = {
        "BUY": "매수",
        "SELL": "매도",
        "MARKET": "시장가",
        "LIMIT": "지정가",
        "ALL": "전량",
        "ONCE": "1회",
        "ACTIVE": "활성",
        "PENDING": "대기",
        "PAUSED": "일시 중지",
        "CANCELLED": "취소",
        "EXPIRED": "만료",
        "PAPER": "PAPER",
    }
    if kind == "state" and raw == "ACTIVE":
        return "조건 규칙 활성"
    return labels.get(raw.upper(), raw)


def _trading_summary(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    tool_result = _trading_tool_result(metadata)
    summary_value = tool_result.get("summary")
    return summary_value if isinstance(summary_value, Mapping) else {}


def _trading_property_name(
    properties_schema: Mapping[str, Any],
    *candidates: str,
    property_type: str,
) -> str | None:
    """Resolve renamed manager-facing Trading columns without guessing types."""

    for name in candidates:
        spec = properties_schema.get(name)
        if isinstance(spec, Mapping) and spec.get("type") == property_type:
            return name
    return None


def _trading_projection_properties(
    properties_schema: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """Populate only manager-meaningful columns from the trusted tool receipt."""

    tool_result = _trading_tool_result(metadata)
    summary_value = _trading_summary(metadata)
    props: dict[str, Any] = {}

    symbol = str(summary_value.get("symbol") or "").strip()
    symbol_column = _trading_property_name(
        properties_schema, "종목", property_type="rich_text"
    )
    if symbol and symbol_column:
        props[symbol_column] = _rich_text(symbol)

    mode = _trading_value_label(tool_result.get("mode"))
    state = _trading_value_label(tool_result.get("state"), kind="state")
    state_column = _trading_property_name(
        properties_schema,
        "조건 규칙 상태",
        "주문 관리 상태",
        "OMS 상태",
        property_type="rich_text",
    )
    if state_column and (mode or state):
        props[state_column] = _rich_text(" · ".join(item for item in (mode, state) if item))

    # A successfully activated rule is a verified processing result, not a
    # trading recommendation.  Keep the existing select value as a backwards
    # compatible fallback until the remote schema has been renamed.
    if tool_result.get("rule_active") is True:
        verdict_column = _trading_property_name(
            properties_schema, "검토 결과", "판정", property_type="select"
        )
        if verdict_column:
            for value in ("처리 확인", "GROUNDED"):
                selected = _schema_select(properties_schema, verdict_column, value)
                if selected is not None:
                    props[verdict_column] = selected
                    break
    return props


def _trading_body_markdown(
    *,
    task: Mapping[str, Any],
    result_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Render one concise Korean administrator report for Trading terminal work."""

    tool_result = _trading_tool_result(metadata)
    summary_value = _trading_summary(metadata)
    status = str(task.get("status") or "").strip().casefold()
    status_label = {
        "done": "완료",
        "completed": "완료",
        "blocked": "보류",
        "failed": "실패",
    }.get(status, "확인 필요")
    completed = iso_timestamp(
        task.get("completed_at") or task.get("updated_at") or task.get("created_at")
    )

    lines = [
        "# 트레이딩 부서 업무 결과",
        "",
        "## 업무 개요",
        "",
        f"- 업무명: {str(task.get('title') or '트레이딩 업무 검토').strip()}",
        f"- 처리 상태: {status_label}",
    ]
    if completed:
        lines.append(f"- 완료 시각: {completed}")

    lines.extend(["", "## 핵심 결과", "", result_text or "결과 내용이 기록되지 않았습니다."])

    if tool_result:
        symbol = str(summary_value.get("symbol") or "").strip()
        details = [
            ("처리 구분", _trading_value_label(tool_result.get("mode"))),
            ("조건 규칙 상태", _trading_value_label(tool_result.get("state"), kind="state")),
            ("대상 종목", symbol),
            ("주문 방향", _trading_value_label(summary_value.get("side"))),
            ("수량", _trading_value_label(summary_value.get("sizing_type"))),
            ("주문 방식", _trading_value_label(summary_value.get("order_type"))),
            ("실행 횟수", _trading_value_label(summary_value.get("repeat_policy"))),
            ("만료 시각", str(summary_value.get("expires_at") or "").strip()),
        ]
        rendered = [f"- {label}: {value}" for label, value in details if value]
        if rendered:
            lines.extend(["", "## 처리 내용", "", *rendered])
        if tool_result.get("rule_active") is True:
            lines.extend(
                [
                    "",
                    "## 안전 경계",
                    "",
                    "- 이번 결과는 PAPER 조건 규칙의 활성화 기록입니다.",
                    "- 주문 접수·체결·원장 반영 결과를 의미하지 않습니다.",
                    "- 조건 충족 시에도 결정론적 안전 검증을 통과한 경우에만 후속 PAPER 처리 대상이 됩니다.",
                ]
            )
    return "\n".join(lines)


def _body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    department: str,
    result_text: str,
) -> str:
    metadata = merged_run_metadata(task)
    if department == "trading":
        return _trading_body_markdown(
            task=task,
            result_text=result_text,
            metadata=metadata,
        )
    if department == "risk":
        return _risk_body_markdown(
            task=task,
            root_task_id=root_task_id,
            result_text=result_text,
            metadata=metadata,
        )
    if department == "accounting":
        return _accounting_body_markdown(
            task=task,
            root_task_id=root_task_id,
            result_text=result_text,
            metadata=metadata,
        )
    if department == "qa":
        return _qa_body_markdown(
            task=task,
            root_task_id=root_task_id,
            result_text=result_text,
            metadata=metadata,
        )
    if department == "hr":
        return _hr_body_markdown(
            task=task,
            root_task_id=root_task_id,
            result_text=result_text,
            metadata=metadata,
        )

    original_instruction = task_body(task)

    safe_metadata = safe_json(metadata)

    parts = [
        "# Department Task Result",
        "",
        f"- Task ID: `{task_id(task)}`",
        f"- Workflow Root Task ID: `{root_task_id}`",
        f"- Department: `{department}`",
        f"- Status: `{task.get('status') or ''}`",
        "",
        "## Original Instruction",
        "",
        original_instruction or "(empty)",
        "",
        "## Result",
        "",
        result_text or "(empty)",
    ]

    if safe_metadata:
        parts.extend(
            [
                "",
                "## Terminal Metadata",
                "",
                "```json",
                json.dumps(
                    safe_metadata,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )[:12000],
                "```",
            ]
        )

    risk_plan = metadata.get("position_risk_plan") or metadata.get("risk_plan")
    if department == "risk" and isinstance(risk_plan, Mapping):
        parts.extend(
            [
                "",
                "## Position Risk Plan (read-only projection)",
                "",
                format_position_risk_plan(risk_plan),
                "",
                "This page is not authoritative. Canonical state remains in the Risk database.",
            ]
        )

    return "\n".join(parts)


class DepartmentNotionProjection:
    """Project terminal department task output into explicitly wired DBs."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        transport: Any | None = None,
        projection_recorder: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.transport = transport
        self.projection_recorder = projection_recorder
        self._transport_token: str | None = None
        self._schema_cache = BoundedNotionSchemaCache()
        self._idempotency = NotionIdempotency(
            self.env,
            namespace="department-projection",
        )

    def record_projection_evidence(self, payload: Mapping[str, Any]) -> str:
        """Persist observer evidence without changing Notion delivery success."""

        try:
            if self.projection_recorder is not None:
                self.projection_recorder(payload)
                return "RECORDED"

            base_url = str(self.env.get("RISK_API_URL") or "").strip().rstrip("/")
            if not base_url:
                return "NOT_CONFIGURED"
            data = json.dumps(dict(payload)).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            token = str(self.env.get("RISK_API_AUTH_TOKEN") or "").strip()
            if token:
                headers["X-Risk-Internal-Token"] = token
            request = urllib.request.Request(
                f"{base_url}/risk/v1/position-risk-plans/projections",
                data=data,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status >= 300:
                    return f"HTTP_{response.status}"
            return "RECORDED"
        except Exception as exc:  # noqa: BLE001 - observer is fail-open
            return f"FAILED:{type(exc).__name__}"

    def _transport_for(self, token: str) -> Any:
        if self.transport is None or (
            self._transport_token is not None and self._transport_token != token
        ):
            self.transport = _NotionTransport(token)
            self._transport_token = token
        return self.transport

    def _schema_for(
        self,
        transport: Any,
        database_id: str,
    ) -> tuple[Mapping[str, Any], bool]:
        return self._schema_cache.get(
            database_id,
            lambda: transport.database_schema(database_id),
        )

    def project(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]] = (),
        event: Mapping[str, Any] | None = None,
    ) -> DepartmentProjectionResult:
        del workflow_tasks
        force_upsert = bool((event or {}).get("force_upsert"))
        correction = str((event or {}).get("correction") or "").strip()

        tid = task_id(task)
        department = _department(task)

        if department not in DATABASE_ENV:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
            )

        if not terminal_success(task):
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
            )

        declared_root = workflow_root(task)
        if declared_root and declared_root != root_task_id:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
                error="workflow_root_mismatch",
            )

        token = str(self.env.get("NOTION_TOKEN") or "").strip()
        db_env = DATABASE_ENV[department]
        database_id = str(
            self.env.get(db_env) or DEFAULT_DATABASES.get(department, "")
        ).strip()

        # Research/Risk are opt-in here because their standalone reporters
        # remain the owner of their own department pipelines.  The Supervisor
        # only projects them when the corresponding DB is explicitly wired;
        # never guess a database ID or silently write into another department.
        if not database_id:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
                error=f"{db_env} missing",
            )

        if not token:
            return DepartmentProjectionResult(
                "failed",
                department=department,
                task_id=tid,
                error="NOTION_TOKEN missing",
            )

        transport = self._transport_for(token)

        schema, schema_cache_hit = self._schema_for(transport, database_id)
        properties_schema = schema.get("properties") or {}
        title_property = TITLE_PROPERTY[department]
        schema_mismatch = (
            title_property not in properties_schema
            or not isinstance(properties_schema[title_property], Mapping)
            or properties_schema[title_property].get("type") != "title"
        )

        if schema_mismatch and schema_cache_hit:
            # A cached schema may be stale after a Notion property migration.
            # Invalidate and perform one authoritative fresh read before
            # failing closed on the same projection.
            self._schema_cache.invalidate(database_id)
            schema, _ = self._schema_for(transport, database_id)
            properties_schema = schema.get("properties") or {}
            schema_mismatch = (
                title_property not in properties_schema
                or not isinstance(properties_schema[title_property], Mapping)
                or properties_schema[title_property].get("type") != "title"
            )

        if schema_mismatch:
            self._schema_cache.invalidate(database_id)
            return DepartmentProjectionResult(
                "failed",
                department=department,
                task_id=tid,
                error=f"title property missing or incompatible: {title_property}",
            )

        title = _task_title(task, department)

        metadata = merged_run_metadata(task)
        result_text = correction or _result_text(task, metadata)
        if department == "risk":
            # Some older Risk workers persisted only a structured ``result``
            # envelope. Render that envelope once instead of exposing its
            # Python dict representation to managers.
            structured_text = _risk_structured_result_text(metadata)
            if structured_text and (
                not result_text
                or result_text.lstrip().startswith(("{", "["))
                or any(
                    marker in result_text
                    for marker in ("execution_authority", "review_findings", "required_validation")
                )
            ):
                result_text = structured_text
            result_text = _humanize_risk_result(result_text)
        elif department == "accounting":
            result_text = _humanize_accounting_result(result_text)
        elif department == "hr":
            result_text = _humanize_hr_result(result_text)

        props: dict[str, Any] = {
            title_property: _title(title),
        }

        if department == "trading":
            props.update(_trading_projection_properties(properties_schema, metadata))

        if department == "risk":
            props.update(
                _risk_projection_properties(
                    properties_schema,
                    metadata=metadata,
                )
            )

        if department == "hr":
            # HR output is proposal-only. Keep the native approval and
            # activation gates explicitly unchecked until separate systems
            # perform those actions.
            for property_name in ("CEO 승인", "IAM 생성", "QA 독립검증"):
                checkbox = _schema_checkbox(properties_schema, property_name, False)
                if checkbox is not None:
                    props[property_name] = checkbox

        if department == "qa":
            verdict = (
                metadata.get("verdict")
                or metadata.get("qa_verdict")
                or metadata.get("overall")
                or metadata.get("overall_status")
                or metadata.get("qa_status")
                or metadata.get("qa_result")
                or task.get("verdict")
                or "WARN"
            )
            props.update(
                build_qa_notion_properties(
                    properties_schema,
                    title=title,
                    verdict=verdict,
                    findings=qa_projection_findings(task, metadata),
                    checks=qa_projection_checks(task, metadata),
                    highest_severity=metadata.get("highest_severity")
                    or task.get("highest_severity"),
                    claim_narrative=metadata.get("claim_narrative") or result_text,
                    input_hash=metadata.get("input_hash") or task.get("input_hash"),
                    calculation_version=(
                        metadata.get("calculation_version")
                        or task.get("calculation_version")
                    ),
                    reason_codes=(
                        metadata.get("reason_codes")
                        or metadata.get("finding_codes")
                        or task.get("reason_codes")
                    ),
                    escalate=metadata.get("escalate"),
                    created_at=(
                        task.get("completed_at")
                        or task.get("updated_at")
                        or task.get("created_at")
                    ),
                    original_report=_qa_summary_text(task=task, metadata=metadata),
                )
            )

        narrative_property = (
            risk_property_name("narrative", properties_schema)
            if department == "risk"
            else _trading_property_name(
                properties_schema, "결과 요약", "서술", property_type="rich_text"
            )
            if department == "trading"
            else "서술"
        )
        if narrative_property and narrative_property in properties_schema:
            props[narrative_property] = _rich_text(result_text)

        original_report_property = (
            risk_property_name("original_report", properties_schema)
            if department == "risk"
            else _trading_property_name(
                properties_schema, "상세 결과", "원본 리포트", property_type="rich_text"
            )
            if department == "trading"
            else "원본 리포트"
        )
        if (
            department != "qa"
            and original_report_property
            and original_report_property in properties_schema
        ):
            props[original_report_property] = _rich_text(
                _qa_summary_text(task=task, metadata=metadata)
                if department == "qa"
                else result_text
            )

        created = (
            task.get("completed_at") or task.get("updated_at") or task.get("created_at")
        )
        created_property = (
            risk_property_name("created_at", properties_schema)
            if department == "risk"
            else "생성 시각"
        )
        if created_property in properties_schema:
            date_value = _date(created)
            if date_value is not None:
                props[created_property] = date_value

        # QA keeps technical joins in LangSmith/Kanban. Do not copy those
        # field names into the manager-facing Notion database. Other native
        # department projections retain their existing domain-ID contract.
        if department != "qa":
            for key in ("trade_case_id", "trace_id"):
                value = metadata.get(key) or task.get(key)
                if value and key in properties_schema:
                    props[key] = _rich_text(value)

        # Quant metrics are written only when explicitly present.
        if department == "quant-backtest":
            metric_map = {
                "Sharpe": ("sharpe", "sharpe_ratio"),
                "MDD": ("mdd", "max_drawdown"),
                "수익률": ("return", "return_rate", "total_return"),
            }
            for notion_name, candidates in metric_map.items():
                if notion_name not in properties_schema:
                    continue
                value = next(
                    (
                        metadata.get(key)
                        for key in candidates
                        if metadata.get(key) is not None
                    ),
                    None,
                )
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    props[notion_name] = {"number": float(value)}

        body = _body_markdown(
            task=task,
            root_task_id=root_task_id,
            department=department,
            result_text=result_text,
        )
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        risk_plan = metadata.get("position_risk_plan") or metadata.get("risk_plan")
        risk_plan_id = (
            str(risk_plan.get("risk_plan_id") or "").strip()
            if isinstance(risk_plan, Mapping)
            else ""
        )

        children = markdown_to_notion_blocks(body)

        def projection_result(
            status: str, page_id: str | None, *, duplicate: bool = False
        ) -> DepartmentProjectionResult:
            readback_status = "NOT_SUPPORTED"
            readback_hash = None
            retrieve = getattr(transport, "retrieve_page", None)
            if page_id and callable(retrieve):
                try:
                    page = retrieve(page_id)
                    observed_properties = page.get("properties")
                    properties_verified = isinstance(observed_properties, Mapping) and all(
                        name in observed_properties
                        and _notion_property_signature(observed_properties[name])
                        == _expected_property_signature(value)
                        for name, value in props.items()
                    )
                    readback_status = (
                        "VERIFIED"
                        if (
                            str(page.get("id") or "").replace("-", "")
                            == page_id.replace("-", "")
                            and properties_verified
                        )
                        else "FAILED"
                    )
                    if readback_status == "VERIFIED":
                        readback_material = {
                            name: _notion_property_signature(observed_properties[name])
                            for name in props
                        }
                        readback_hash = hashlib.sha256(
                            json.dumps(
                                readback_material,
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest()
                except Exception:  # noqa: BLE001 - observer is fail-open
                    readback_status = "FAILED"
            evidence_status = None
            if risk_plan_id:
                evidence_status = self.record_projection_evidence(
                    {
                        "risk_plan_id": risk_plan_id,
                        "target": "NOTION",
                        "projection_version": "risk-plan-notion-projection.v1",
                        "payload_hash": payload_hash,
                        "external_id": page_id,
                        "delivery_status": "DELIVERED",
                        "readback_status": (
                            readback_status
                            if readback_status in {"VERIFIED", "FAILED"}
                            else "NOT_CHECKED"
                        ),
                        "readback_hash": readback_hash,
                        "task_id": tid,
                        "trace_id": str(
                            risk_plan.get("trace_id")
                            or metadata.get("trace_id")
                            or root_task_id
                        ),
                    }
                )
            return DepartmentProjectionResult(
                status,
                department=department,
                task_id=tid,
                page_id=page_id,
                duplicate=duplicate,
                risk_plan_id=risk_plan_id or None,
                payload_hash=payload_hash,
                delivery_status="DELIVERED",
                readback_status=readback_status,
                readback_hash=readback_hash,
                evidence_status=evidence_status,
            )

        def lookup() -> Sequence[Mapping[str, Any]]:
            try:
                current = transport.query_title(database_id, title_property, title)
                if current:
                    return current
                # Existing Trading pages used the opaque Kanban ID as part of
                # the title.  Check that exact former value once so rollout
                # updates the same page instead of creating a duplicate.
                for legacy_title in _legacy_task_titles(task, department):
                    if legacy_title == title:
                        continue
                    legacy = transport.query_title(
                        database_id, title_property, legacy_title
                    )
                    if legacy:
                        return legacy
                return current
            except DepartmentNotionProjectionError as exc:
                if exc.status == 400:
                    self._schema_cache.invalidate(database_id)
                raise

        def create() -> Mapping[str, Any]:
            try:
                return transport.create_page(database_id, props, children)
            except DepartmentNotionProjectionError as exc:
                if exc.status == 400:
                    self._schema_cache.invalidate(database_id)
                raise

        if force_upsert:
            existing = lookup()
            if existing:
                page_id = str(existing[0].get("id") or "").strip()
                update_page = getattr(transport, "update_page", None)
                append_blocks = getattr(transport, "append_blocks", None)
                replace_blocks = getattr(transport, "replace_blocks", None)
                if not page_id or not callable(update_page):
                    raise DepartmentNotionProjectionError(
                        "Notion transport does not support page upsert"
                    )
                update_page(page_id, props)
                if callable(replace_blocks):
                    replace_blocks(page_id, children)
                elif correction and callable(append_blocks):
                    append_blocks(page_id, children)
                return projection_result("updated", page_id)
            created = create()
            return projection_result("created", str(created.get("id") or "") or None)

        result = self._idempotency.execute(
            database_id,
            # The durable task identifier belongs in the hidden idempotency
            # key, not in the manager-facing Notion title.
            f"{department}:{tid}",
            lookup=lookup,
            create=create,
        )

        return projection_result(
            "duplicate" if result.duplicate else "created",
            result.page_id,
            duplicate=result.duplicate,
        )
