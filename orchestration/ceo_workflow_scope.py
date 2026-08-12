"""Durable scope contract for one CEO Kanban workflow.

Hermes supplies recent work by the assignee in a worker's context.  That is
useful background, but those task IDs are not part of the current workflow
graph.  This module defines the small machine-readable contract used by the
BFF and supervisor to keep a fresh CEO request isolated from that history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


CEO_WORKFLOW_SCOPE_MARKER = "hgfinance.ceo-workflow-scope.v1"
CEO_WORKFLOW_SCOPE_POLICY = "fresh"
CEO_WORKFLOW_REUSE_POLICY = "disabled"
WORKFLOW_MODES = frozenset({"analysis", "binding"})

# Mandate 스냅샷 블록. root body에 **한 번만** 박히고, 부서는 이 body를
# `kanban show <root_task_id>`로 직접 읽는다.
#
# ## 왜 값을 실어 보내고 조회하게 하지 않나
#
# 부서 Hermes 컨테이너에는 `DATABASE_URL`이 없다(docker-compose.yml의
# research-hermes/risk-hermes/ceo-hermes: 환경변수가 HERMES_* 와 MCP 키뿐).
# 그건 실수가 아니라 규칙이다 - 부서는 DB를 직접 열지 않고 읽기 전용 API·MCP를
# 거친다. 그런데 governance Mandate를 서빙하는 MCP·도구가 아직 없다
# (`ceo-hermes`에는 `mcp_servers` 섹션 자체가 없고, CEO config.yaml의
# `governance.mandate.read`는 허용 목록 선언이지 배선이 아니다).
#
# 그래서 참조(`mandate_version_id`)만 넘기면 부서가 풀 수 없다. 값을 함께 싣는다.
#
# ## 왜 CEO가 자식 body에 복사하게 하지 않나
#
# CEO의 LLM 턴이 `risk_bounds` 블록을 자식 4개에 정확히 복사해야 하는데, 요약·
# 누락이 생기면 부서마다 다른 한도로 판단한다. 대신 root body 하나를 단일 원본으로
# 두고 부서가 `kanban show`로 읽게 한다 - 부서 Profile에 이미 있는 도구다
# (research SOUL.md: "Read the card back").
#
# ## 왜 스냅샷인가 (PIT, 개발 원칙 5)
#
# 이 블록은 생성 후 바뀌지 않는다. Task 실행 중 사용자가 Mandate를 조여도 이
# 워크플로는 시작 시점 값으로 끝까지 판단한다 - 그래야 같은 Task 안에서 Research와
# Risk가 같은 기준을 쓰고, 나중에 replay해도 같은 결과가 나온다. 조인 한도가
# 무시되는 것은 아니다: CEO 산출물은 `binding: false`이고, 실제 한도 집행은 주문
# 시점의 결정론적 Risk Engine이 **항상 현재 Mandate로** 한다(개발 원칙 4).
CEO_MANDATE_SNAPSHOT_MARKER = "hgfinance.mandate-snapshot.v1"

# 스냅샷에 싣는 한도 키. `policy.risk_bounds` 전체를 붓지 않는 이유는 부서가
# 실제로 쓰는 값만 노출해 "이 워크플로가 무엇을 근거로 판단했나"를 좁히기
# 위해서다. 키를 늘릴 때는 `CEO_MANDATE_SNAPSHOT_MARKER` 버전도 올린다.
_SNAPSHOT_RISK_KEYS = (
    "base_capital",
    "currency",
    "max_instrument_weight",
    "max_sector_weight",
    "max_gross_exposure",
    "max_concurrent_positions",
    "max_daily_loss",
    "max_drawdown_pct",
)

_TASK_ID_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")
_REFERENCE_KEYS = frozenset(
    {
        "department_tasks",
        "qa_task",
        "qa_task_id",
        "primary_task_ids",
        "analysis_task_ids",
        "qa_dependency_ids",
        "synthesis_task_ids",
    }
)
_ROOT_KEYS = frozenset(
    {"current_root_task_id", "workflow_root_task_id", "root_task_id"}
)


class WorkflowScopeViolation(ValueError):
    """A task reference or parent edge escapes the active root graph."""


@dataclass(frozen=True)
class WorkflowScopeReferences:
    """Task IDs declared by machine-readable workflow metadata/comments."""

    root_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()


def build_mandate_snapshot_block(mandate: Mapping[str, Any] | None) -> str:
    """`GET .../mandates/.../current` 응답을 root body용 스냅샷 블록으로 만든다.

    `mandate`가 `None`이거나 아직 활성 Version이 없으면(`current_version=0`) 빈
    문자열을 준다 - **없는 한도를 지어내지 않는다.** 그 경우 부서는 Mandate 블록을
    못 찾고, 그건 "이 사용자는 아직 Mandate가 없다"는 정확한 사실이다. 기본값을
    채워 넣으면 사용자가 정하지 않은 한도가 판단 근거로 쓰인다(개발 원칙 9).

    한 줄 `key=value` 형태로 쓰는 이유: 부서가 LLM으로 이 블록을 읽으므로 중첩
    JSON보다 평평한 줄이 오독될 여지가 적고, `grep`·정규식으로도 뽑을 수 있다.
    """

    if not mandate:
        return ""
    version = mandate.get("current_version")
    policy = mandate.get("policy")
    if not version or not isinstance(policy, Mapping):
        # Version이 없으면 정책도 없다. 껍데기만 있는 Mandate는 근거가 아니다.
        return ""

    lines = [
        CEO_MANDATE_SNAPSHOT_MARKER,
        "snapshot_policy=frozen_at_request_time",
        f"mandate_id={mandate.get('mandate_id', '')}",
        f"mandate_version={version}",
        f"content_hash={mandate.get('content_hash', '')}",
    ]
    fund_id = mandate.get("fund_id")
    if fund_id:
        lines.append(f"fund_id={fund_id}")
    objective = str(mandate.get("objective_text") or "").strip().replace("\n", " ")
    if objective:
        lines.append(f"objective_text={objective[:300]}")

    risk_bounds = policy.get("risk_bounds")
    if isinstance(risk_bounds, Mapping):
        for key in _SNAPSHOT_RISK_KEYS:
            value = risk_bounds.get(key)
            if value is not None:
                lines.append(f"risk.{key}={value}")

    universe = policy.get("universe_policy")
    if isinstance(universe, Mapping):
        for key in (
            "allowed_asset_classes",
            "forbidden_asset_classes",
            "preferred_sectors",
            "excluded_sectors",
        ):
            value = universe.get(key)
            if value:
                lines.append(f"universe.{key}={json.dumps(value, ensure_ascii=False)}")

    lines.append(
        "These limits are the frozen basis for this workflow. Do not fetch a newer"
        " Mandate; a mid-run change must not alter this workflow's basis."
    )
    lines.append(
        "Advisory only - these values do not authorize an order. Order-time"
        " enforcement is the deterministic Risk Engine's job."
    )
    return "\n".join(lines)


def infer_workflow_mode(query: str) -> str:
    """Classify high-risk intent; this never grants execution authority."""
    text = str(query or "").casefold()
    non_binding_phrases = (
        "do not place", "don't place", "do not execute", "don't execute",
        "실제 주문이나 집행은 하지", "주문이나 집행은 하지",
        "주문하지 말", "집행하지 말", "실행하지 말",
    )
    if any(phrase in text for phrase in non_binding_phrases):
        return "analysis"
    binding_terms = (
        "place order", "send order", "execute order", "broker", "buy ",
        "sell ", "주문", "매수", "매도", "집행", "배분 변경", "리밸런싱",
        "rebalance", "change nav", "nav 변경", "ledger post", "원장 반영",
        "promote to production", "production promotion", "프로덕션 승격",
        "deploy strategy", "전략 배포", "실제 거래", "실행해",
    )
    return "binding" if any(term in text for term in binding_terms) else "analysis"


def workflow_mode_from_body(body: str) -> str:
    """Read the explicit workflow mode, preserving the legacy gate."""
    match = re.search(r"(?m)^workflow_mode=(\S+)\s*$", str(body or ""))
    if not match:
        return "binding" if CEO_WORKFLOW_SCOPE_MARKER in str(body or "") else "analysis"
    mode = match.group(1).casefold()
    if mode not in WORKFLOW_MODES:
        raise WorkflowScopeViolation(f"unknown workflow_mode: {mode}")
    return mode


def build_root_body(
    query: str,
    request_id: str,
    *,
    workflow_mode: str = "analysis",
    mandate: Mapping[str, Any] | None = None,
) -> str:
    """Build a root body that is unambiguous before the root ID exists.

    `workflow_mode`는 고위험 의도(주문·집행)를 분류만 한다 - 실행 권한을 주지
    않는다. `mandate`(2026-08-12 추가)가 채워지면
    `hgfinance.mandate-snapshot.v1` 블록이 함께 실려, 부서가
    `kanban show <root_task_id>`로 사용자의 투자 한도를 읽을 수 있다.

    둘 다 선택 인자다 - 기존 호출부는 그대로 동작한다.
    """

    if workflow_mode not in WORKFLOW_MODES:
        raise ValueError("workflow_mode must be analysis or binding")
    return (
        f"{CEO_WORKFLOW_SCOPE_MARKER}\n"
        f"workflow_scope={CEO_WORKFLOW_SCOPE_POLICY}\n"
        f"reuse_policy={CEO_WORKFLOW_REUSE_POLICY}\n"
        f"request_id={request_id}\n"
        f"workflow_mode={workflow_mode}\n"
        "root_task_role=scope_and_planning\n"
        "primary_execution_parent=none\n"
        "primary_scope_field=workflow_root_task_id\n"
        "planning_terminal_state=done_after_child_creation\n"
        "Primary tasks must bind workflow_root_task_id to this task ID in their body;\n"
        "do not pass this root as a Hermes execution parent for primary tasks.\n"
        "Only task IDs carrying this workflow root marker may be used.\n"
        "Do not reuse IDs from recent work, memory, or another root.\n"
        # Mandate 블록은 scope 지시문 뒤, 사용자 질의 앞에 온다. `extract_user_query`가
        # `## User request` 뒤만 잘라내므로 이 블록이 질의에 섞이지 않는다.
        f"{_mandate_section(mandate)}"
        "\n## User request\n"
        f"{query}"
    )


def _mandate_section(mandate: Mapping[str, Any] | None) -> str:
    """스냅샷 블록을 root body에 끼울 형태로 감싼다. 없으면 빈 문자열."""

    block = build_mandate_snapshot_block(mandate)
    if not block:
        return ""
    return f"\n## Investor mandate (frozen snapshot)\n{block}\n"


def build_root_comment(root_task_id: str, request_id: str) -> str:
    """Build the post-create comment that binds the concrete root ID."""

    return (
        f"{CEO_WORKFLOW_SCOPE_MARKER} "
        f"root_task_id={root_task_id} "
        f"request_id={request_id} "
        f"workflow_scope={CEO_WORKFLOW_SCOPE_POLICY} "
        f"reuse_policy={CEO_WORKFLOW_REUSE_POLICY}"
    )


def build_scoped_task_body(
    body: str,
    root_task_id: str,
    *,
    role: str,
    request_id: str | None = None,
    workflow_mode: str = "analysis",
) -> str:
    """Bind a task to a workflow without creating a dependency edge."""

    root_task_id = str(root_task_id).strip()
    if not root_task_id:
        raise ValueError("root_task_id must not be empty")
    role = str(role).strip().casefold()
    if role not in {"primary", "qa", "synthesis", "control"}:
        raise ValueError("workflow task role must be primary, qa, synthesis, or control")
    if workflow_mode not in WORKFLOW_MODES:
        raise ValueError("workflow_mode must be analysis or binding")

    metadata = [
        CEO_WORKFLOW_SCOPE_MARKER,
        f"workflow_root_task_id={root_task_id}",
        f"workflow_role={role}",
        f"workflow_mode={workflow_mode}",
    ]
    if request_id:
        metadata.append(f"request_id={request_id}")
    prefix = "\n".join(metadata)
    body = str(body or "").strip()
    return f"{prefix}\n\n{body}" if body else prefix


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _task_ids(value: Any) -> tuple[str, ...]:
    """Extract IDs from the supported structured metadata shapes."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return tuple(_TASK_ID_RE.findall(value))
        return _task_ids(decoded)
    if isinstance(value, Mapping):
        direct = value.get("task_id") or value.get("id")
        if direct:
            return (str(direct),)
        return _ordered_unique(
            tuple(task_id for item in value.values() for task_id in _task_ids(item))
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _ordered_unique(
            tuple(task_id for item in value for task_id in _task_ids(item))
        )
    return ()


def _labelled_ids(text: str, labels: frozenset[str]) -> tuple[str, ...]:
    """Read IDs from a human-readable comment only when it names a field."""

    ids: list[str] = []
    for label in labels:
        match = re.search(
            rf"\b{re.escape(label)}\s*[:=]\s*([^\n]+)", text,
            flags=re.IGNORECASE,
        )
        if match:
            ids.extend(_TASK_ID_RE.findall(match.group(1)))
    return _ordered_unique(ids)


def extract_scope_references(payload: Mapping[str, Any]) -> WorkflowScopeReferences:
    """Collect only declared workflow references, never arbitrary prose IDs."""

    root_ids: list[str] = []
    task_ids: list[str] = []

    def visit(value: Any, key: str | None = None) -> None:
        if key in _ROOT_KEYS:
            root_ids.extend(_task_ids(value))
        elif key in _REFERENCE_KEYS:
            task_ids.extend(_task_ids(value))

        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key).casefold())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and key in {"body", "text", "comment"}:
            root_ids.extend(_labelled_ids(value, _ROOT_KEYS))
            task_ids.extend(_labelled_ids(value, _REFERENCE_KEYS))

    visit(payload)
    return WorkflowScopeReferences(
        root_ids=_ordered_unique(root_ids),
        task_ids=_ordered_unique(task_ids),
    )


def validate_workflow_scope(
    *,
    root_task_id: str,
    root_payload: Mapping[str, Any],
    descendants: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed when metadata or graph edges leave ``root_task_id``."""

    graph_ids = {root_task_id}
    graph_ids.update(
        str(payload.get("id") or payload.get("task_id"))
        for payload in descendants
        if payload.get("id") or payload.get("task_id")
    )

    refs = extract_scope_references(root_payload)
    wrong_roots = set(refs.root_ids) - {root_task_id}
    if wrong_roots:
        raise WorkflowScopeViolation(
            f"declared root IDs outside active root {root_task_id}: "
            f"{sorted(wrong_roots)}"
        )

    outside = set(refs.task_ids) - graph_ids
    if outside:
        raise WorkflowScopeViolation(
            f"workflow metadata references non-descendant task IDs under "
            f"{root_task_id}: {sorted(outside)}"
        )

    for payload in descendants:
        task_refs = extract_scope_references(payload)
        wrong_task_roots = set(task_refs.root_ids) - {root_task_id}
        if wrong_task_roots:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"task {task_id} declares root IDs outside active root "
                f"{root_task_id}: {sorted(wrong_task_roots)}"
            )

    # A descendant discovered through root.children must not secretly point
    # at a different root.  QA and synthesis legitimately have multiple
    # parents, but every parent must still be inside this graph.
    for payload in descendants:
        parent_ids = _task_ids(payload.get("parents"))
        outside_parents = set(parent_ids) - graph_ids
        if outside_parents:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"task {task_id} has parent IDs outside active root "
                f"{root_task_id}: {sorted(outside_parents)}"
            )
        if extract_scope_references(payload).root_ids and root_task_id in parent_ids:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"scoped task {task_id} must not use workflow root "
                f"{root_task_id} as an execution parent"
            )


__all__ = [
    "CEO_WORKFLOW_REUSE_POLICY",
    "CEO_WORKFLOW_SCOPE_MARKER",
    "CEO_WORKFLOW_SCOPE_POLICY",
    "WORKFLOW_MODES",
    "WorkflowScopeReferences",
    "WorkflowScopeViolation",
    "build_root_body",
    "build_root_comment",
    "extract_scope_references",
    "infer_workflow_mode",
    "validate_workflow_scope",
    "workflow_mode_from_body",
]
