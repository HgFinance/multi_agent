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
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from departments.notion_markdown import markdown_to_notion_blocks
from departments.risk_notion_schema import (
    human_metadata_rows,
    risk_property_name,
)
from orchestration.adapters.notion_idempotency import (
    NotionIdempotency,
)
from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache
from orchestration.adapters.terminal_projection_utils import (
    iso_timestamp,
    merged_run_metadata,
    safe_json,
    summary,
    task_body,
    task_id,
    terminal_success,
    text_value,
    workflow_root,
)
from orchestration.canonical_profiles import department_for_canonical_profile
from orchestration.qa_discord_feedback import qa_check_label
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
}

TITLE_PROPERTY = {
    "trading": "제목",
    "quant-backtest": "전략·백테스트 run",
    "research": "종목",
    "risk": "제목",
    "accounting": "제목",
    "qa": "제목",
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
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.notion.com/v1/{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                decoded = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read())
            except (OSError, TypeError, ValueError):
                detail = str(exc)
            raise DepartmentNotionProjectionError(str(detail), status=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise DepartmentNotionProjectionError(str(exc)) from exc

        if not isinstance(decoded, Mapping):
            raise DepartmentNotionProjectionError(
                "Notion returned a non-object response"
            )
        return decoded

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
                self._request("PATCH", f"blocks/{block_id}", {"archived": True})


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
    tid = task_id(task)
    raw = str(
        task.get("title") or task.get("name") or task.get("subject") or ""
    ).strip()

    if not raw:
        raw = (
            "Trading department result"
            if department == "trading"
            else (
                "Quant backtest result"
                if department == "quant-backtest"
                else "회계·포트폴리오 검토 결과"
            )
        )

    if department == "qa":
        raw = "QA 감사 결과"

    return f"{tid} · {raw}"[:1900]


def _result_text(task: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    """Prefer the complete user-facing result over a short handoff summary."""

    return (
        text_value(metadata.get("final_answer")).strip()
        or text_value(task.get("result")).strip()
        or summary(task, metadata).strip()
    )


def _humanize_risk_result(value: str) -> str:
    """Remove runtime field names from the manager-facing Risk projection."""

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
        ("KOREA_EQUITY", "국내 주식"),
        ("PROVISIONAL_ETF", "임시 허용 ETF"),
        ("Mandate가", "투자지침이"),
        ("Mandate를", "투자지침을"),
        ("Mandate와", "투자지침과"),
        ("Mandate의", "투자지침의"),
        ("Mandate", "투자지침"),
        ("MODERATE", "보통"),
        ("NAV", "순자산 가치"),
        ("위반 없음(no_breach)", "현재 입력만으로 위반을 확인하지 못함"),
        ("no_breach", "현재 입력만으로 위반을 확인하지 못함"),
        ("Risk 검증", "리스크 검증"),
    )
    humanized = value
    for internal, friendly in replacements:
        humanized = humanized.replace(internal, friendly)
    humanized = re.sub(
        r"(?:PAPER(?: 가상거래)? 기준 |PAPER만으로는 )?"
        r"현재 입력만으로 위반을 확인하지 못함으로 "
        r"(?:보았|회신되었)지만",
        "법률 위반 여부를 확정할 수 없으며",
        humanized,
    )

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
        parts.extend(f"- {label}: {value}" for label, value in rows)

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


def _humanize_qa(value: Any, limit: int = 320) -> str:
    rendered = " ".join(str(value or "").split())
    replacements = (
        ("Accounting Engine", "회계 시스템"),
        ("accounting system", "회계 시스템"),
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
        ("Mandate", "투자지침"),
        ("Risk owner", "리스크 담당자"),
        ("and 회계 시스템", "및 회계 시스템"),
        ("and Accounting", "및 회계"),
        ("broker reconciliation", "브로커 대사"),
        ("snapshot", "조회 자료"),
        ("Require ", "필요: "),
        ("NAV", "순자산"),
        ("PIT", "기준 시점"),
        ("provenance", "자료 출처·계보"),
        ("DEFER", "보류"),
        ("FAIL", "실패"),
        ("PASS", "통과"),
        ("WARN", "주의"),
    )
    for internal, friendly in replacements:
        rendered = rendered.replace(internal, friendly)
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
            severity = _humanize_qa(item.get("severity") or "확인 필요", 24)
            issue = _humanize_qa(
                item.get("summary")
                or item.get("statement")
                or item.get("description")
                or item.get("issue")
                or item.get("message")
                or "구체적인 문제 설명이 없습니다.",
                300,
            )
            owner = _humanize_qa(item.get("owner") or item.get("responsible_party"), 100)
            block = _humanize_qa(item.get("block_condition") or item.get("impact"), 180)
            finding_id = _humanize_qa(item.get("finding_id") or item.get("id"), 48)
            status = _humanize_qa(item.get("status"), 32)
            due_date = _humanize_qa(item.get("due_date"), 32)
            prefix = f"{finding_id}: " if finding_id else ""
            suffix = f" 담당: {owner}" if owner else ""
            if block:
                suffix += f" 영향: {block}"
            if status:
                suffix += f" 상태: {status}"
            if due_date:
                suffix += f" 기한: {due_date}"
            lines.append(f"- [{severity}] {prefix}{issue}{suffix}")
        elif item:
            lines.append(f"- {_humanize_qa(item)}")
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


def _qa_summary_text(
    *, task: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    """Create a Korean summary from structured QA fields, not raw run prose."""

    verdict = (
        metadata.get("verdict")
        or metadata.get("qa_verdict")
        or metadata.get("overall")
        or task.get("verdict")
    )
    numerical = (
        metadata.get("numerical_posture")
        or metadata.get("numeric_posture")
        or metadata.get("decision")
    )
    checks = _qa_check_lines(metadata.get("checks") or task.get("checks"))
    findings = _qa_findings_lines(metadata.get("findings") or task.get("findings"))
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
        or task.get("verdict")
    )
    numerical = (
        metadata.get("numerical_posture")
        or metadata.get("numeric_posture")
        or metadata.get("decision")
    )
    findings = _qa_findings_lines(metadata.get("findings") or task.get("findings"))
    checks = _qa_check_lines(metadata.get("checks") or task.get("checks"))
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
    return {"select": {"name": value}} if value in allowed else None


def _schema_checkbox(
    properties_schema: Mapping[str, Any], name: str, value: bool
) -> dict[str, Any] | None:
    spec = properties_schema.get(name)
    return {"checkbox": bool(value)} if isinstance(spec, Mapping) and spec.get("type") == "checkbox" else None


def _body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    department: str,
    result_text: str,
) -> str:
    metadata = merged_run_metadata(task)
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
            result_text = _humanize_risk_result(result_text)
        elif department == "accounting":
            result_text = _humanize_accounting_result(result_text)

        props: dict[str, Any] = {
            title_property: _title(title),
        }

        if department == "qa":
            verdict = str(
                metadata.get("verdict")
                or metadata.get("qa_verdict")
                or metadata.get("overall")
                or task.get("verdict")
                or "WARN"
            ).strip().upper()
            canonical_verdict = {
                "CONDITIONAL PASS": "CONDITIONAL",
                "CONDITIONAL_PASS": "CONDITIONAL",
                "REJECT": "FAIL",
                "BLOCK": "FAIL",
            }.get(verdict, verdict)
            decision_property = _schema_select(
                properties_schema, "판정", canonical_verdict
            )
            if decision_property is not None:
                props["판정"] = decision_property

            severity = str(
                metadata.get("highest_severity")
                or task.get("highest_severity")
                or "UNKNOWN"
            ).strip().upper()
            severity_property = _schema_select(
                properties_schema, "findings severity", severity
            )
            if severity_property is not None:
                props["findings severity"] = severity_property

            findings_text = "\n".join(
                _qa_findings_lines(
                    metadata.get("findings") or task.get("findings")
                )
            )
            checks_text = "\n".join(
                _qa_check_lines(metadata.get("checks") or task.get("checks"))
            )
            qa_text_properties = {
                "findings": findings_text,
                "claim_checks": checks_text,
                "claim_narrative": _qa_summary_text(
                    task=task, metadata=metadata
                ),
                "원본 리포트": _qa_summary_text(task=task, metadata=metadata),
            }
            for property_name, value in qa_text_properties.items():
                spec = properties_schema.get(property_name)
                if value and isinstance(spec, Mapping) and spec.get("type") == "rich_text":
                    props[property_name] = _rich_text(value)

            escalate = canonical_verdict in {"FAIL", "CONDITIONAL"}
            escalate_property = _schema_checkbox(properties_schema, "escalate", escalate)
            if escalate_property is not None:
                props["escalate"] = escalate_property

            for property_name, metadata_key in (
                ("input_hash", "input_hash"),
                ("calculation_version", "calculation_version"),
            ):
                value = metadata.get(metadata_key)
                spec = properties_schema.get(property_name)
                if value and isinstance(spec, Mapping) and spec.get("type") == "rich_text":
                    props[property_name] = _rich_text(value)

        narrative_property = (
            risk_property_name("narrative", properties_schema)
            if department == "risk"
            else "서술"
        )
        if narrative_property in properties_schema:
            props[narrative_property] = _rich_text(result_text)

        original_report_property = (
            risk_property_name("original_report", properties_schema)
            if department == "risk"
            else "원본 리포트"
        )
        if original_report_property in properties_schema:
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

        # Domain IDs are never repurposed as Kanban IDs.
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
            retrieve = getattr(transport, "retrieve_page", None)
            if page_id and callable(retrieve):
                try:
                    page = retrieve(page_id)
                    readback_status = (
                        "VERIFIED"
                        if str(page.get("id") or "").replace("-", "")
                        == page_id.replace("-", "")
                        else "FAILED"
                    )
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
                evidence_status=evidence_status,
            )

        def lookup() -> Sequence[Mapping[str, Any]]:
            try:
                return transport.query_title(database_id, title_property, title)
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
            f"{department}:{title}",
            lookup=lookup,
            create=create,
        )

        return projection_result(
            "duplicate" if result.duplicate else "created",
            result.page_id,
            duplicate=result.duplicate,
        )
