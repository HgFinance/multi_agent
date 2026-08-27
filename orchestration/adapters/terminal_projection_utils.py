"""Shared, loss-averse helpers for terminal task projections.

이 모듈은 Hermes task의 durable marker와 최신 run metadata만 읽는다.
사용자에게 공개되는 projection에는 reasoning/chain-of-thought를 넣지 않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

# 패턴은 ceo_workflow_scope.read_marker 가 소유한다 - 여기서 다시 적지 않는다
# (2026-08-14: 같은 마커를 5곳에서 4가지 패턴으로 읽고 있었다).
from orchestration.ceo_workflow_scope import (
    BACKGROUND_RESEARCH_ROLE,
    CONTINUOUS_RESEARCH_MARKER,
    CONTINUOUS_RESEARCH_PLANE,
    read_marker,
)


class _Match:
    """read_marker 결과를 기존 `match.group(1)` 호출부와 맞춰 주는 얇은 껍데기."""

    def __init__(self, value: str) -> None:
        self._value = value

    def group(self, _index: int = 1) -> str:
        return self._value


def _search(body: object, key: str):
    value = read_marker(str(body or ""), key)
    return _Match(value) if value else None


_ROOT_KEY = "workflow_root_task_id"
_ROLE_KEY = "workflow_role"
_ACTION_RE = re.compile(r"(?m)^(?:action|workflow_action)=(\S+)\s*$")
_SUPERVISOR_MARKER = "hgfinance.ceo-supervisor.v1"
_SUPERVISOR_LINE_RE = re.compile(
    rf"^{re.escape(_SUPERVISOR_MARKER)}(?:\s+(?P<fields>.*))?$"
)
_METADATA_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$")
_MODE_KEY = "workflow_mode"
_PLANE_KEY = "workflow_plane"
_FORBIDDEN_KEYS = {
    "chain_of_thought",
    "cot",
    "hidden_reasoning",
    "reasoning",
    "thought",
    "thoughts",
    "scratchpad",
}

_INTERNAL_HANDOFF_RE = re.compile(r"(?im)^\s*\[terminal handoff\]\s*$")


def strip_internal_handoff(value: Any) -> str:
    """Keep internal terminal metadata out of user-facing projections.

    Hermes workers sometimes append a compact handoff after the user-ready
    answer. It is useful in the task record, but fields such as ``mode`` and
    ``execution`` are implementation details and must not leak into Discord
    or manager-facing Notion pages.
    """

    text = str(value or "").strip()
    match = _INTERNAL_HANDOFF_RE.search(text)
    return text[: match.start()].rstrip() if match else text


def as_mapping(value: Any) -> dict[str, Any]:
    """Decode the JSON-shaped metadata returned by different Hermes versions."""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or "")


def task_body(task: Mapping[str, Any]) -> str:
    return str(task.get("body") or "")


def workflow_root(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _ROOT_KEY)
    return match.group(1).strip() if match else None


def workflow_role(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _ROLE_KEY)
    return match.group(1).strip().casefold() if match else None


def _supervisor_marker_fields(task: Mapping[str, Any]) -> dict[str, str] | None:
    """Parse the exact durable supervisor marker, if present."""

    for raw_line in task_body(task).splitlines():
        match = _SUPERVISOR_LINE_RE.fullmatch(raw_line.strip())
        if match is None:
            continue
        fields_text = match.group("fields") or ""
        if not fields_text:
            # A bare supervisor marker is malformed; fail closed.
            return {}
        tokens = fields_text.split()
        if not all(_METADATA_FIELD_RE.fullmatch(token) for token in tokens):
            return {}
        return {
            key: value
            for key, value in (token.split("=", 1) for token in tokens)
        }
    return None


def action(task: Mapping[str, Any]) -> str | None:
    marker_fields = _supervisor_marker_fields(task)
    if marker_fields is not None:
        value = marker_fields.get("action") or marker_fields.get("workflow_action")
        return value.strip().upper() if value else None
    match = _ACTION_RE.search(task_body(task))
    return match.group(1).strip().upper() if match else None


def workflow_mode(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _MODE_KEY)
    return match.group(1).strip().casefold() if match else None


def workflow_plane(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _PLANE_KEY)
    return match.group(1).strip().casefold() if match else None


def is_background_research(task: Mapping[str, Any]) -> bool:
    """Return whether a task belongs to the independent research plane.

    Continuous research deliberately has no request-workflow dependency.  The
    marker is checked before assignee/profile validation so a future
    research-intelligence profile cannot leak into a CEO workflow projection.
    """

    body = task_body(task)
    return (
        CONTINUOUS_RESEARCH_MARKER in body
        or workflow_plane(task) == CONTINUOUS_RESEARCH_PLANE
        or workflow_role(task) == BACKGROUND_RESEARCH_ROLE
    )


def is_request_scoped_role(
    task: Mapping[str, Any], root_task_id: str, role: str
) -> bool:
    """Match only a current-root task with the requested workflow role."""

    return (
        not is_background_research(task)
        and workflow_root(task) == root_task_id
        and workflow_role(task) == role.casefold()
    )


def merged_run_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    """Prefer durable task-run metadata over ingress-only fields."""

    merged: dict[str, Any] = {}
    for key in ("metadata", "task_run_metadata", "run_metadata"):
        merged.update(as_mapping(task.get(key)))
    task_run = task.get("task_run")
    if isinstance(task_run, Mapping):
        merged.update(as_mapping(task_run.get("metadata", task_run)))
    merged.update(as_mapping(merged.get("workflow_metadata")))
    runs = task.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        for run in runs:
            if isinstance(run, Mapping):
                run_metadata = as_mapping(run.get("metadata"))
                merged.update(run_metadata)
                merged.update(as_mapping(run_metadata.get("workflow_metadata")))
    return merged


_QA_FLATTENED_CHECK_KEYS = (
    "scope",
    "prohibited_action_compliance",
    "snapshot_value_consistency",
    "nav_bridge_reconciliation_disclosure",
    "evidence_provenance",
    "point_in_time",
    "uncertainty_handling",
    "unsupported_claims",
)


def _qa_nav_gap_text(metadata: Mapping[str, Any]) -> str:
    gap = metadata.get("nav_cash_securities_gap_krw") or metadata.get(
        "nav_bridge_gap_krw"
    )
    if gap in (None, ""):
        verified = metadata.get("math_verified")
        gap = verified.get("nav_residual") if isinstance(verified, Mapping) else None
    if gap in (None, ""):
        return ""
    try:
        return f"{int(str(gap)):,}원"
    except (TypeError, ValueError):
        return str(gap)


def qa_projection_checks(
    task: Mapping[str, Any], metadata: Mapping[str, Any]
) -> Any:
    """Normalize QA arrays and the newer flattened metadata contract."""

    checks = metadata.get("checks") or task.get("checks")
    if checks:
        return checks
    return [
        {"check": key, "result": metadata[key]}
        for key in _QA_FLATTENED_CHECK_KEYS
        if key in metadata and metadata[key] not in (None, "")
    ]


def qa_projection_findings(
    task: Mapping[str, Any], metadata: Mapping[str, Any]
) -> Any:
    """Normalize QA findings without fabricating source coordinates."""

    findings = metadata.get("findings") or task.get("findings")
    if findings:
        gap_text = _qa_nav_gap_text(metadata)
        mapped = metadata.get("mapped_positions")
        total = metadata.get("positions_count")
        pnl_available = metadata.get("pnl_available")
        enriched: list[Any] = []
        for item in findings:
            if not isinstance(item, Mapping):
                enriched.append(item)
                continue
            copy = dict(item)
            issue = copy.get("summary") or copy.get("statement") or copy.get("issue")
            issue_text = str(issue or "").strip()
            issue_lower = issue_text.casefold()
            if issue_text and any(
                term in issue_lower for term in ("reconciliation", "sector", "mapping", "대사")
            ) and gap_text:
                detail = f"순자산 대사 차이 {gap_text}"
                if mapped not in (None, "") and total not in (None, ""):
                    detail += f", 섹터 매핑 {mapped}/{total}건"
                if pnl_available is not None:
                    detail += ", PnL 제공" if pnl_available else ", PnL 미제공"
                if detail not in issue_text:
                    copy["issue"] = f"{issue_text} ({detail})"
            enriched.append(copy)
        return enriched
    statement = metadata.get("finding") or task.get("finding")
    if not statement:
        return []
    if isinstance(statement, Mapping):
        finding = dict(statement)
        if not any(
            finding.get(key)
            for key in ("summary", "statement", "description", "issue", "message")
        ):
            finding["statement"] = "재현 가능한 출처 좌표가 없습니다."
            gap_text = _qa_nav_gap_text(metadata)
            if gap_text:
                finding["statement"] += f" (순자산 대사 차이 {gap_text})"
            finding["block_condition"] = "공식 수치 확정·주문·리스크 결정에 사용하지 않음"
            finding["recommended_action"] = (
                "원자료 출처, 기준 시점, 업무 식별자를 다음 CEO 합성에 포함합니다."
            )
        return [finding]
    gap_text = _qa_nav_gap_text(metadata)
    if gap_text:
        detail = f"순자산 대사 차이 {gap_text}"
        mapped = metadata.get("mapped_positions")
        total = metadata.get("positions_count")
        if mapped not in (None, "") and total not in (None, ""):
            detail += f", 섹터 매핑 {mapped}/{total}건"
        if metadata.get("pnl_available") is not None:
            detail += ", PnL 제공" if metadata["pnl_available"] else ", PnL 미제공"
        statement = f"{statement} ({detail})"
    decision_status = str(metadata.get("decision_status") or "").strip().upper()
    severity = "BLOCKER" if decision_status == "BLOCKED_FOR_DECISION" else "HIGH"
    return [
        {
            "severity": severity,
            "statement": statement,
            "block_condition": "공식 수치 확정과 투자 결정 보류",
            "status": "OPEN",
            "recommended_action": metadata.get("recommended_action"),
        }
    ]


def terminal_success(task: Mapping[str, Any]) -> bool:
    status = str(task.get("status") or "").casefold()
    outcome = str(task.get("outcome") or "").casefold()
    runs = task.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        for run in runs:
            if isinstance(run, Mapping):
                candidate = str(run.get("outcome") or run.get("status") or "").casefold()
                if candidate:
                    outcome = candidate
    return status in {"done", "completed"} and outcome not in {
        "failed",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("summary", "result", "final_answer", "message"):
            if value.get(key):
                return str(value[key])
    return str(value)


def summary(task: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    metadata = metadata or {}
    for key in (
        "final_answer",
        "synthesis_summary",
        "final_summary",
        "summary",
        "result",
    ):
        if metadata.get(key):
            return text_value(metadata[key])
    for key in ("latest_summary", "summary", "result"):
        if task.get(key):
            return text_value(task[key])
    return ""


def ids_from(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return (value,) if value else ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("id") or item.get("task_id")
        if item:
            result.append(str(item))
    return tuple(dict.fromkeys(result))


def safe_json(value: Any) -> Any:
    """Remove hidden reasoning recursively while retaining audit evidence."""

    if isinstance(value, Mapping):
        return {
            str(key): safe_json(item)
            for key, item in value.items()
            if str(key).casefold() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe_json(item) for item in value]
    return value


def iso_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "BACKGROUND_RESEARCH_ROLE",
    "CONTINUOUS_RESEARCH_MARKER",
    "CONTINUOUS_RESEARCH_PLANE",
    "action",
    "as_mapping",
    "ids_from",
    "is_background_research",
    "is_request_scoped_role",
    "iso_timestamp",
    "merged_run_metadata",
    "safe_json",
    "summary",
    "task_id",
    "terminal_success",
    "workflow_mode",
    "workflow_plane",
    "workflow_role",
    "workflow_root",
]
