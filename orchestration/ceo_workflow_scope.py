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
CONTINUOUS_RESEARCH_MARKER = "hgfinance.continuous-research.v1"
CONTINUOUS_RESEARCH_PLANE = "continuous_research"
BACKGROUND_RESEARCH_ROLE = "background_research"
PRIMARY_SELECTION_FIELD = "selected_primary_profiles"
WORKFLOW_MODES = frozenset({"analysis", "binding"})
# 역할 유효값. 이전에는 build_scoped_task_body 안의 인라인 집합으로만 있어서
# 읽는 쪽은 무엇이 유효한지 알 수 없었다(2026-08-14: 없는 값을 쓴 카드가 CEO
# 감독관에게 abort 당했는데, 유효값 목록이 어디에도 노출돼 있지 않았다).
WORKFLOW_ROLES = frozenset({"primary", "qa", "synthesis", "control"})

# ▶ 마커는 **이 모듈이 쓰고 이 모듈이 읽는다.**
#   2026-08-14 실측: 같은 마커를 5곳에서 4가지 방식으로 파싱하고 있었다 -
#   `(?m)^k=(\S+)\s*$` 두 벌, `(?:^|\n)k=(\w+)` 한 벌, 그리고 단순 문자열 포함
#   (`"origin=user-query" in body`). 마지막 것은 본문 산문에 그 문자열이 있으면
#   그대로 오인한다. 패턴이 갈리면 같은 카드가 읽는 쪽마다 다르게 해석된다.
_MARKER_CACHE: dict[str, "re.Pattern[str]"] = {}


def _marker_re(key: str) -> "re.Pattern[str]":
    if key not in _MARKER_CACHE:
        _MARKER_CACHE[key] = re.compile(rf"(?m)^{re.escape(key)}=(\S+)\s*$")
    return _MARKER_CACHE[key]


def read_marker(body: str, key: str) -> str:
    """카드 본문에서 `key=값` 마커 하나를 읽는다. 없으면 빈 문자열.

    줄 전체가 마커일 때만 인정한다 - 산문 안에 같은 문자열이 있어도 마커가 아니다.
    """

    match = _marker_re(key).search(str(body or ""))
    return match.group(1).strip() if match else ""


def is_user_query_body(body: str) -> bool:
    """사람이 발원한 카드인가 (RFC 3834 동형 도장).

    공장 자동 생성물과 사용자 질의를 가르는 유일한 신호다. 이 판정이 틀리면
    공장 카드에 "사용자에게 물어보라" 카드를 찍어내는 순환이 생긴다(실측 53장).
    """

    return read_marker(body, "origin") == "user-query"


def workflow_role_from_body(body: str) -> str:
    """워크플로 역할. 없으면 빈 문자열(역할 없는 카드는 워크플로 밖이다)."""

    return read_marker(body, "workflow_role").casefold()


def workflow_root_from_body(body: str) -> str:
    """이 카드가 속한 워크플로 루트 ID. 없으면 빈 문자열."""

    return read_marker(body, "workflow_root_task_id")

# These aliases make the CEO planner's durable selection machine-readable.
# They do not choose departments; the planner remains the source of truth.
_PRIMARY_PROFILE_ALIASES = {
    "research-department": ("research-department", "research", "리서치", "연구"),
    "quant-backtest-department": (
        "quant-backtest-department", "quant", "backtest", "퀀트"
    ),
    "trading-department": ("trading-department", "trading", "트레이딩"),
    "accounting-portfolio-department": (
        "accounting-portfolio-department",
        "accounting",
        "portfolio",
        "accounting/portfolio",
        "회계",
        "포트폴리오",
    ),
    "risk-management": ("risk-management", "risk management", "risk", "리스크관리"),
    "hr-department": ("hr-department", "workforce", "hr", "인사"),
}


def primary_idempotency_key(root_task_id: str, profile: str) -> str:
    """Stable create key for one request-scoped primary profile."""

    root = str(root_task_id).strip()
    canonical = str(profile).strip()
    if not root or not canonical:
        raise ValueError("root_task_id and profile are required")
    return f"{root}:primary:{canonical}"


def _selection_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ()
        if raw.startswith(("[", "{", '"')):
            try:
                return _selection_values(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return tuple(
            part.strip()
            for part in re.split(r"[,;|]|\s+and\s+|\s+및\s+", raw)
            if part.strip()
        )
    if isinstance(value, Mapping):
        return tuple(str(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def selected_primary_profiles_from_body(body: str) -> tuple[str, ...]:
    """Read the planner-selected primary set from a root body.

    New CEO sessions should emit ``selected_primary_profiles=...``.  The
    legacy prose form is accepted only to diagnose already-created roots; it
    never authorizes reuse of a task from another root.
    """

    text = str(body or "")
    values: tuple[str, ...] = ()
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() in {
            PRIMARY_SELECTION_FIELD,
            "selected_departments",
        }:
            values = _selection_values(value)
            break
    if not values:
        match = re.search(
            r"(?is)dynamic\s+departments\s+selected\s*:\s*(.+?)(?:\n\s*primary|\n\s*advisory|$)",
            text,
        )
        if match:
            values = _selection_values(match.group(1).replace("\n", " "))

    selected: list[str] = []
    for value in values:
        normalized = value.strip().strip(" .,:;()[]{}").casefold()
        for profile, aliases in _PRIMARY_PROFILE_ALIASES.items():
            if normalized == profile or normalized in {
                alias.casefold() for alias in aliases
            }:
                if profile not in selected:
                    selected.append(profile)
                break
    return tuple(selected)


def selected_primary_profiles_from_task(
    task: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Resolve the planner's primary set from one root task projection.

    New producers must persist the selection in the root body (or structured
    root metadata).  Comments are a compatibility fallback for the direct
    Hermes producer that existed before the machine-readable field was added.
    The fallback is deliberately root-local; it never searches other tasks or
    infers a department from recency.
    """

    if not isinstance(task, Mapping):
        return ()

    body = str(task.get("body") or "")

    # Prefer the new machine-readable root field. The older direct-Hermes
    # producer wrote a prose ``Dynamic departments selected: ...`` sentence;
    # that sentence must not override a precise root comment or run metadata.
    for line in body.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() in {
            PRIMARY_SELECTION_FIELD,
            "selected_departments",
        }:
            explicit_selection = _selection_values(value)
            if explicit_selection:
                return explicit_selection

    def from_metadata(value: Any) -> tuple[str, ...]:
        if isinstance(value, Mapping):
            for key in (
                PRIMARY_SELECTION_FIELD,
                "selected_departments",
                "selected_primary",
            ):
                if key in value:
                    parsed = _selection_values(value[key])
                    if parsed:
                        return parsed
            for key in (
                "metadata",
                "workflow_metadata",
                "run_metadata",
                "task_run_metadata",
                "task_run",
            ):
                if key in value:
                    parsed = from_metadata(value[key])
                    if parsed:
                        return parsed
            return ()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                parsed = from_metadata(item)
                if parsed:
                    return parsed
        return ()

    metadata_selection = from_metadata(task.get("metadata"))
    if metadata_selection:
        return metadata_selection
    for key in ("task_run_metadata", "run_metadata", "task_run", "runs"):
        metadata_selection = from_metadata(task.get(key))
        if metadata_selection:
            return metadata_selection

    # Legacy direct-Discord producer: selected_primary_profiles was written
    # as a root comment. Read only comments attached to this root. This is
    # intentionally before the old prose fallback below.
    comments = task.get("comments")
    if isinstance(comments, Sequence) and not isinstance(comments, (str, bytes)):
        for comment in comments:
            comment_body = (
                comment.get("body")
                if isinstance(comment, Mapping)
                else comment
            )
            parsed = selected_primary_profiles_from_body(str(comment_body or ""))
            if parsed:
                return parsed

    # Compatibility only for roots whose producer persisted the selection
    # solely in the old prose sentence.
    return selected_primary_profiles_from_body(body)

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


def mandate_snapshot_present(body: str) -> bool:
    """root body에 Mandate 스냅샷 블록이 실렸는지.

    `build_mandate_snapshot_block`은 Mandate가 없거나 활성 Version이 없으면 빈
    문자열을 주므로, 마커의 존재가 곧 "이 워크플로에 사용자 한도가 있다"는 뜻이다.
    """

    return CEO_MANDATE_SNAPSHOT_MARKER in str(body or "")


def build_mandate_reference_line(root_task_id: str) -> str:
    """자식 Task body에 넣을 Mandate 참조 지시문.

    ## 왜 자식 body에 한도 값을 복사하지 않나

    복사본이 늘어나면 요약·누락으로 부서마다 다른 한도를 쓰게 된다. root body
    하나가 단일 원본이고, 자식은 그 위치만 가리킨다 - `kanban show`는 부서
    Profile에 이미 있는 도구다.

    ## 왜 Mandate가 없을 때는 이 줄을 아예 넣지 않나

    "root를 봐라"라고 해놓고 아무것도 없으면 부서 LLM은 헛읽고, 최악의 경우
    없는 한도를 추론해 채운다. 줄이 없으면 "이 워크플로에는 사용자 한도가 없다"가
    되고, 그게 정확한 사실이다(개발 원칙 9).
    """

    return (
        f"mandate_snapshot=see_root_task_body root_task_id={root_task_id}\n"
        f"Read `kanban show {root_task_id}` and use the "
        f"`{CEO_MANDATE_SNAPSHOT_MARKER}` block as this workflow's investor limits.\n"
        "Do not copy those limits into new tasks, do not fetch a newer Mandate,"
        " and do not substitute defaults for any limit the block does not state.\n"
        "Advisory only - these limits do not authorize an order."
    )


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
    text = str(body or "")
    raw = read_marker(text, "workflow_mode")
    if not raw:
        # Legacy scoped roots default to binding. Direct CEO roots may explicitly
        # declare an asynchronous non-binding response workflow and must not be
        # promoted into the QA-gated binding path solely because they carry the
        # durable workflow scope marker.
        request_class = re.search(r"(?mi)^request_class=(.+?)\s*$", text)
        if request_class:
            request_class_text = request_class.group(1).casefold()
            if "non-binding" in request_class_text or "advisory" in request_class_text:
                return "analysis"

        producer = (read_marker(text, "producer") or "").casefold()
        qa_marker = (read_marker(text, "qa_required") or "").casefold()
        lowered = text.casefold()

        direct_async_analysis = (
            producer == "ceo-hermes-direct"
            and qa_marker == "false"
            and (
                "not a prerequisite for synthesis or user response" in lowered
                or "non-binding analysis" in lowered
                or "no action or authority change" in lowered
                or "read-only" in lowered
                or "read only" in lowered
            )
        )

        if direct_async_analysis:
            return "analysis"

        return "binding" if CEO_WORKFLOW_SCOPE_MARKER in text else "analysis"

    mode = raw.casefold()
    if mode not in WORKFLOW_MODES:
        raise WorkflowScopeViolation(f"unknown workflow_mode: {mode}")
    return mode


def build_root_body(
    query: str,
    request_id: str,
    *,
    workflow_mode: str = "analysis",
    mandate: Mapping[str, Any] | None = None,
    requested_by: str | None = None,
) -> str:
    """Build a root body that is unambiguous before the root ID exists.

    `workflow_mode`는 고위험 의도(주문·집행)를 분류만 한다 - 실행 권한을 주지
    않는다. `mandate`(2026-08-12 추가)가 채워지면
    `hgfinance.mandate-snapshot.v1` 블록이 함께 실려, 부서가
    `kanban show <root_task_id>`로 사용자의 투자 한도를 읽을 수 있다.

    `requested_by`(2026-08-14 추가)는 `X-User-Id`로 식별된 요청자다. 채워지면
    `requested_by=<id>` 한 줄이 실려 `GET /ui/ceo/tasks?owner_id=`가 계정별
    이력을 서버에서 걸러낼 수 있다. 없으면 줄 자체를 넣지 않는다 - "요청자
    불명"을 임의 기본값으로 채우지 않는다(개발 원칙 9).

    셋 다 선택 인자다 - 기존 호출부는 그대로 동작한다.
    """

    if workflow_mode not in WORKFLOW_MODES:
        raise ValueError("workflow_mode must be analysis or binding")
    requested_by_line = f"requested_by={requested_by}\n" if requested_by else ""
    return (
        f"{CEO_WORKFLOW_SCOPE_MARKER}\n"
        f"workflow_scope={CEO_WORKFLOW_SCOPE_POLICY}\n"
        f"reuse_policy={CEO_WORKFLOW_REUSE_POLICY}\n"
        f"request_id={request_id}\n"
        f"workflow_mode={workflow_mode}\n"
        f"{requested_by_line}"
        "response_plane=primary_results_ready\n"
        "governance_plane=async_qa\n"
        "qa_is_not_synthesis_prerequisite=true\n"
        # RFC 3834 동형(2026-08-13): 사람이 발원한 카드에만 이 도장이 찍힌다.
        # 공장 자동 생성물은 origin=factory 를 찍는다 - 자동 생성물이 질의
        # 응답 경로를 다시 부르는 순환은 이 도장의 대조로 끊는다.
        "origin=user-query\n"
        "analysis_response_rule=primary_results_ready_allows_immediate_ceo_synthesis\n"
        "qa_rule=async_post_hoc_audit_not_user_response_prerequisite\n"
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


_REQUESTED_BY_RE = re.compile(r"(?m)^requested_by=(\S+)\s*$")


def requested_by_from_body(body: str) -> str | None:
    """root body의 `requested_by=` 줄을 읽는다. 없으면 `None`("계정 불명").

    과거에 만들어진 root task는 이 줄이 없을 수 있다 - 그런 task는 특정 계정
    이력에 넣지 않는다(개발 원칙 9, `build_root_body`의 `requested_by` 인자와 짝).
    """

    match = _REQUESTED_BY_RE.search(str(body or ""))
    return match.group(1).strip() if match else None


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
    has_mandate: bool = False,
) -> str:
    """Bind a task to a workflow without creating a dependency edge.

    `has_mandate`(2026-08-13 추가)가 참이면 Mandate 참조 지시문이 함께 실린다.
    호출부는 root body에 `mandate_snapshot_present()`를 물어 넘긴다 - 자식 body를
    만드는 쪽이 root를 이미 읽고 있으므로 추가 조회가 없다. 기본값이 `False`라
    기존 호출부는 그대로 동작한다.
    """

    root_task_id = str(root_task_id).strip()
    if not root_task_id:
        raise ValueError("root_task_id must not be empty")
    role = str(role).strip().casefold()
    if role not in WORKFLOW_ROLES:
        raise ValueError("workflow task role must be primary, qa, synthesis, or control")
    if workflow_mode not in WORKFLOW_MODES:
        raise ValueError("workflow_mode must be analysis or binding")

    metadata = [
        CEO_WORKFLOW_SCOPE_MARKER,
        f"workflow_root_task_id={root_task_id}",
        f"workflow_role={role}",
        f"workflow_mode={workflow_mode}",
        # 질의 파생 카드 전체에 발원 도장이 전파된다(RFC 3834 동형, 2026-08-13).
        # 창구(liaison)는 이 도장 없는 자동 생성물(origin=factory·공장 접두어)
        # 을 MISROUTED 로 되돌린다 - 순환의 가장 싼 절단점이 수신 거부다.
        "origin=user-query",
    ]
    if request_id:
        metadata.append(f"request_id={request_id}")
    if has_mandate:
        metadata.append(build_mandate_reference_line(root_task_id))
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
            # A labelled field in a human-readable comment ends at a
            # statement delimiter.  Do not let workflow_root_task_id consume
            # later prose such as "Primary child created: t_xxx" on the same
            # line and accidentally promote that child ID into root_ids.
            rf"\b{re.escape(label)}\s*[:=]\s*([^;\n]+)", text,
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
    "BACKGROUND_RESEARCH_ROLE",
    "CEO_MANDATE_SNAPSHOT_MARKER",
    "CEO_WORKFLOW_REUSE_POLICY",
    "CEO_WORKFLOW_SCOPE_MARKER",
    "CEO_WORKFLOW_SCOPE_POLICY",
    "CONTINUOUS_RESEARCH_MARKER",
    "CONTINUOUS_RESEARCH_PLANE",
    "PRIMARY_SELECTION_FIELD",
    "WORKFLOW_MODES",
    "workflow_root_from_body",
    "workflow_role_from_body",
    "is_user_query_body",
    "read_marker",
    "WORKFLOW_ROLES",
    "WorkflowScopeReferences",
    "WorkflowScopeViolation",
    "build_mandate_reference_line",
    "build_mandate_snapshot_block",
    "build_root_body",
    "build_root_comment",
    "build_scoped_task_body",
    "extract_scope_references",
    "infer_workflow_mode",
    "mandate_snapshot_present",
    "primary_idempotency_key",
    "requested_by_from_body",
    "selected_primary_profiles_from_body",
    "selected_primary_profiles_from_task",
    "validate_workflow_scope",
    "workflow_mode_from_body",
]
