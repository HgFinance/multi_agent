#!/usr/bin/env python3
"""CEO Kanban workflow Read Model. `/ui/ceo/tasks/*`가 이 모듈만 통해 판을 읽는다.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)

경계 세 개.

1. **BFF는 `kanban.db`를 직접 열지 않는다.** `hermes kanban` CLI가 Board 경로와
   Profile 경계를 소유한다(`hermes_boundary.create_kanban_task`와 같은 규칙).
2. **읽기 전용이다.** 여기서 만드는 상태 변경은 Archive 하나뿐이고, 그것도
   기록을 지우지 않는다. Task 생성·QA 판정·Synthesis는 전부 CEO Supervisor
   몫이다(`orchestration/adapters/ceo_supervisor.py`).
3. **Supervisor의 `ChildTaskState`를 재사용하지 않는다.** 그쪽은 정책 계층이라
   비표준 Profile을 만나면 Fail closed로 멈춰야 하지만, 화면용 Read Model이
   같은 이유로 500을 내면 사용자는 진행 중인 작업을 아예 못 본다. 대신 판정
   기준이 갈라지지 않도록 상수(`SUPERVISOR_MARKER`, `TERMINAL_STATUSES`,
   `FAILURE_OUTCOMES`)는 Supervisor에서 그대로 가져다 쓴다.

Hermes v0.19.0 `kanban show --json` 실측 형태:
    {"task": {...}, "latest_summary": str|null, "parents": [id],
     "children": [id], "comments": [...], "events": [...], "runs": [...]}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from orchestration.adapters.ceo_supervisor import (
    FAILURE_OUTCOMES,
    SUPERVISOR_MARKER,
    TERMINAL_STATUSES,
)
from orchestration.canonical_profiles import (
    CanonicalProfileError,
    canonical_profile_for_department,
    department_for_canonical_profile,
)
from orchestration.ceo_workflow_scope import CEO_WORKFLOW_SCOPE_MARKER

ROOT = Path(__file__).resolve().parents[2]

CEO_PROFILE = canonical_profile_for_department("ceo")
QA_PROFILE = canonical_profile_for_department("qa")

# `canonical_profiles.py`는 쓰기 경로(신규 Task 생성)에서 별칭을 의도적으로
# 거부한다 - 오타나 폐기 이름이 Hermes에 도달하기 전에 실패해야 하기 때문이다
# (departments/00-ceo-office/hermes/SOUL.md: "Never write... legacy aliases
# such as ai-qa-audit-department"). 이 Read Model은 그 규칙이 생기기 전(또는
# 다른 파이프라인)에 만들어져 이미 공유 판에 있는 과거 Task까지 읽어야 하므로,
# 쓰기 경로와 달리 알려진 폐기 별칭을 관대하게 인식한다 - 신규 생성을 허용하는
# 게 아니라 이미 존재하는 데이터를 오분류하지 않기 위해서다.
_LEGACY_QA_ALIASES = frozenset({"ai-qa-audit-department"})

# `build_root_body`가 사용자 질의 앞에 붙이는 구분자. 이 표시가 있는 Task만
# 화면에 노출할 CEO Root로 인정한다.
_USER_REQUEST_HEADING = "## User request"

# 그래프 노드 역할. 화면이 CEO/부서/QA/Synthesis를 구분해서 그릴 때 쓴다.
ROLE_ROOT = "root"
ROLE_PRIMARY = "primary"
ROLE_QA = "qa"
ROLE_SYNTHESIS = "synthesis"
ROLE_USER_INPUT = "user_input"

# 워크플로 단위 상태. Kanban Task 하나의 status가 아니라 Root 그래프 전체의 상태다.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"
STATUS_ARCHIVED = "archived"

_DONE_STATUSES = frozenset({"done", "completed", "archived"})

# Kanban status -> 화면 단계 상태. `todo`는 "Task가 없다"와 "만들어졌지만 부모
# 의존성 때문에 아직 대기 중이다"를 같은 칸으로 묶는다 - 화면 입장에서 둘 다
# "아직 시작 안 함"이고, 세부는 graph 경로에서 원값으로 볼 수 있다.
_STAGE_BY_STATUS: dict[str, str] = {
    "triage": "todo",
    "todo": "todo",
    "ready": "todo",
    "scheduled": "todo",
    "running": "running",
    "review": "running",
    "done": "done",
    "completed": "done",
    "archived": "done",
    "blocked": "blocked",
}
# 앞에 있을수록 우선. 문제를 진행 중 표시 뒤에 숨기지 않는다.
_STAGE_PRIORITY = ("blocked", "failed", "running", "todo", "done")

_NOT_FOUND_RE = re.compile(r"no such task|unknown id|unknown or non-canonical", re.IGNORECASE)

# CEO Synthesis와 QA 요약은 Agent가 쓴 자유 서술이다. 구조화된 판정 필드가
# 아니므로, 명시적으로 라벨을 붙여 적은 값만 뽑고 없으면 None을 준다 - 문장을
# 읽고 결론을 추측하지 않는다(개발 원칙 2: LLM 출력은 스키마로만 확정한다).
_DECISION_VALUES = frozenset({"BUY", "RESIZE", "HOLD", "REJECT", "ESCALATE", "DEFER"})
_VERDICT_VALUES = frozenset({"PASS", "WARN", "FAIL"})
_DECISION_RE = re.compile(
    r"\b(?:decision|recommendation|final_decision)\s*[:=]\s*\**\s*([A-Za-z_]+)",
    re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"\b(?:qa[_\s-]?verdict|verdict)\s*[:=]\s*\**\s*([A-Za-z_]+)",
    re.IGNORECASE,
)

# QA가 Block으로 끝난 워크플로에 프론트가 쓰는 고정 토큰. QA Task가 blocked면
# "판정을 못 냈다"가 아니라 "결정을 막았다"이므로 PASS/FAIL과 구분한다.
QA_BLOCKED_VERDICT = "FAIL_BLOCKED_FOR_DECISION"

# 그래프 폭주 방지. 정상 워크플로는 Root + 부서 6 + QA + Synthesis 수준이다.
_MAX_NODES = int(os.getenv("CEO_KANBAN_MAX_NODES", "200"))
_MAX_PARENT_HOPS = 32
_FETCH_WORKERS = max(1, int(os.getenv("CEO_KANBAN_FETCH_WORKERS", "8")))


class KanbanUnavailable(RuntimeError):
    """Hermes Kanban CLI를 쓸 수 없다. 호출자는 503으로 옮긴다."""


class KanbanTaskNotFound(LookupError):
    """요청한 Task가 판에 없다. 호출자는 404로 옮긴다."""


def _timeout() -> float:
    return float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8"))


def _cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    return environment


def run_kanban(args: Sequence[str]) -> str:
    """`hermes kanban ...`를 실행하고 stdout을 준다. shell=False로만 부른다."""

    command = [os.environ.get("HERMES_BIN", "hermes"), "kanban", *args]
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_timeout(),
            cwd=ROOT,
            env=_cli_environment(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise KanbanUnavailable(
            "Hermes CLI를 찾을 수 없습니다. Hermes Runtime 설치를 확인하세요."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KanbanUnavailable("Hermes Kanban CLI 응답이 시간 내에 오지 않았습니다.") from exc
    except OSError as exc:
        raise KanbanUnavailable(f"Hermes Kanban CLI 실행 실패: {type(exc).__name__}") from exc

    if process.returncode != 0:
        message = (process.stderr or process.stdout or "").strip()
        if _NOT_FOUND_RE.search(message):
            raise KanbanTaskNotFound(message[:200])
        raise KanbanUnavailable(
            message[:200] or f"hermes kanban {args[0]} exited {process.returncode}"
        )
    return process.stdout


def _load_json(payload: str, *, what: str) -> Any:
    try:
        return json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise KanbanUnavailable(f"hermes kanban {what}가 잘못된 JSON을 반환했습니다.") from exc


def show_task(task_id: str) -> dict[str, Any]:
    """`kanban show --json`의 Task Row와 그래프 투영을 하나로 펼쳐준다."""

    payload = _load_json(run_kanban(("show", task_id, "--json")), what="show")
    if not isinstance(payload, Mapping):
        raise KanbanUnavailable("hermes kanban show가 객체를 반환하지 않았습니다.")
    task = payload.get("task", payload)
    if not isinstance(task, Mapping):
        raise KanbanUnavailable("hermes kanban show에 task 객체가 없습니다.")
    flattened = dict(task)
    for key in ("latest_summary", "parents", "children", "comments", "events", "runs"):
        if key in payload:
            flattened[key] = payload[key]
    return flattened


def list_tasks(*, assignee: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
    """`kanban list --json`. Row에는 parents/children이 없다(그래프는 show로만)."""

    args: list[str] = ["list", "--json", "--sort", "created-desc"]
    if assignee:
        args.extend(("--assignee", assignee))
    if include_archived:
        args.append("--archived")
    payload = _load_json(run_kanban(args), what="list")
    if not isinstance(payload, list):
        raise KanbanUnavailable("hermes kanban list가 배열을 반환하지 않았습니다.")
    return [dict(row) for row in payload if isinstance(row, Mapping)]


def archive_tasks(task_ids: Sequence[str]) -> None:
    """`kanban archive`. 기록은 남고 기본 목록·실행 대상에서만 빠진다."""

    if not task_ids:
        return
    run_kanban(("archive", *task_ids))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("summary", "result", "error", "reason", "message"):
            if value.get(key):
                return str(value[key])
    return str(value)


def _ids(value: Any) -> tuple[str, ...]:
    """`parents`/`children`은 실측상 ID 문자열 배열이지만 객체 배열도 견딘다."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    collected: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            collected.append(item)
        elif isinstance(item, Mapping):
            task_id = item.get("id") or item.get("task_id")
            if task_id:
                collected.append(str(task_id))
    return tuple(collected)


def _epoch_to_iso(value: Any) -> str | None:
    """Hermes는 Unix epoch 초(int)를 준다. 화면 계약은 ISO 8601 UTC다."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def extract_user_query(body: str) -> str | None:
    """Root Body에서 원래 사용자 질의만 떼어낸다. Scope 헤더는 화면에 보내지 않는다."""

    if _USER_REQUEST_HEADING not in body:
        return None
    query = body.split(_USER_REQUEST_HEADING, 1)[1].strip()
    return query or None


def is_ceo_root_body(body: str) -> bool:
    """`build_root_body`가 만든 사용자 발원 Root인지. Supervisor 산출물은 제외한다."""

    return (
        CEO_WORKFLOW_SCOPE_MARKER in body
        and SUPERVISOR_MARKER not in body
        and _USER_REQUEST_HEADING in body
    )


def _labelled_token(text: str, pattern: re.Pattern[str], allowed: frozenset[str]) -> str | None:
    for match in pattern.finditer(text):
        token = match.group(1).strip().upper()
        if token in allowed:
            return token
    return None


@dataclass(frozen=True)
class WorkflowNode:
    """화면용 Task 투영. 비표준 Profile을 만나도 예외를 내지 않는다."""

    task_id: str
    profile: str
    title: str
    body: str
    status: str
    parents: tuple[str, ...]
    children: tuple[str, ...]
    summary: str
    error: str
    block_reason: str
    run_outcome: str
    created_at: str | None
    completed_at: str | None

    @classmethod
    def from_hermes(cls, payload: Mapping[str, Any]) -> "WorkflowNode":
        runs = payload.get("runs")
        run_outcome = ""
        if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes)):
            for run in runs:
                if isinstance(run, Mapping):
                    outcome = str(run.get("outcome") or run.get("status") or "").casefold()
                    if outcome:
                        run_outcome = outcome
        summary = _text(
            payload.get("latest_summary")
            or payload.get("summary")
            or payload.get("result")
        )
        return cls(
            task_id=str(payload.get("id") or payload.get("task_id") or ""),
            profile=str(payload.get("assignee") or ""),
            title=_text(payload.get("title")),
            body=_text(payload.get("body")),
            status=str(payload.get("status") or "unknown").casefold(),
            parents=_ids(payload.get("parents")),
            children=_ids(payload.get("children")),
            summary=summary,
            error=_text(payload.get("error") or payload.get("last_error")),
            block_reason=_text(
                payload.get("block_reason")
                or payload.get("blocked_reason")
                or payload.get("reason")
            ),
            run_outcome=run_outcome,
            created_at=_epoch_to_iso(payload.get("created_at")),
            completed_at=_epoch_to_iso(payload.get("completed_at")),
        )

    @property
    def department(self) -> str:
        """정규 Profile이면 논리 부서 코드, 아니면 Profile 문자열 그대로."""

        try:
            return department_for_canonical_profile(self.profile)
        except CanonicalProfileError:
            return self.profile

    @property
    def is_qa(self) -> bool:
        return self.profile == QA_PROFILE or self.profile in _LEGACY_QA_ALIASES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES or self.run_outcome in TERMINAL_STATUSES

    @property
    def done(self) -> bool:
        return self.status in _DONE_STATUSES or self.run_outcome == "completed"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked" or self.run_outcome == "blocked"

    @property
    def failed(self) -> bool:
        return self.status in FAILURE_OUTCOMES or self.run_outcome in FAILURE_OUTCOMES

    def role(self, *, root_task_id: str) -> str:
        """`SUPERVISOR_MARKER` 문자열이 아니라 그래프 구조로 판정한다.

        실측(2026-08-12, 실 CEO Kanban 워크플로): CEO 자신의 LLM 턴이 부서
        선택과 동시에 QA·Synthesis Task까지 한 번에 만들어두는 경우, 그 Task들
        body에는 `orchestration/adapters/ceo_supervisor.py`(별도 Fallback 데몬)
        가 붙이는 `SUPERVISOR_MARKER`가 없다 - 데몬은 CEO 턴이 부서를 못 고르거나
        (REQUEST_USER_INPUT) 재시도가 필요할 때만 개입한다. 마커 유무로 분류하면
        데몬 미개입 워크플로에서 Synthesis를 못 찾아 `/result`가 영원히 null이
        된다.

        CEO는 자기 자신에게 분석 업무를 배정하지 않는다 - `ceo-agent`가
        assignee인 root 이하 Task는 항상 제어/산출 Task다. `parents`가 root
        하나뿐이면 대기용 제어 Task(REQUEST_USER_INPUT류), root 밖의 Task에도
        의존하면 그 하위 결과를 모아 쓰는 Synthesis다.
        """

        if self.task_id == root_task_id:
            return ROLE_ROOT
        if self.is_qa:
            return ROLE_QA
        if self.profile == CEO_PROFILE:
            # Analysis-mode synthesis consumes primary outputs directly; QA is
            # an independent governance branch and is not its parent.
            if set(self.parents) - {root_task_id}:
                return ROLE_SYNTHESIS
            return ROLE_USER_INPUT
        return ROLE_PRIMARY


@dataclass(frozen=True)
class Workflow:
    """하나의 CEO Root와 그 하위 그래프 전체."""

    root_task_id: str
    nodes: tuple[WorkflowNode, ...]

    @property
    def root(self) -> WorkflowNode:
        return self.by_id[self.root_task_id]

    @property
    def by_id(self) -> dict[str, WorkflowNode]:
        return {node.task_id: node for node in self.nodes}

    @property
    def descendants(self) -> tuple[WorkflowNode, ...]:
        return tuple(node for node in self.nodes if node.task_id != self.root_task_id)

    @property
    def primary_nodes(self) -> tuple[WorkflowNode, ...]:
        """실제 분석을 수행하는 부서 Task. CEO 제어 Task(Synthesis 등)와 QA는 뺀다."""

        return tuple(
            node
            for node in self.descendants
            if node.role(root_task_id=self.root_task_id) == ROLE_PRIMARY
        )

    @property
    def qa_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(node for node in self.descendants if node.is_qa)

    @property
    def synthesis_node(self) -> WorkflowNode | None:
        matches = [
            node
            for node in self.descendants
            if node.role(root_task_id=self.root_task_id) == ROLE_SYNTHESIS
        ]
        return matches[-1] if matches else None

    @property
    def user_input_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(
            node
            for node in self.descendants
            if node.role(root_task_id=self.root_task_id) == ROLE_USER_INPUT
        )

    @property
    def query(self) -> str | None:
        return extract_user_query(self.root.body)

    @property
    def selected_departments(self) -> tuple[str, ...]:
        """CEO Planner가 실제로 고른 부서 Profile. 생성 순서를 유지한다."""

        seen: list[str] = []
        for node in self.primary_nodes:
            if node.profile and node.profile not in seen:
                seen.append(node.profile)
        return tuple(seen)

    @property
    def qa_required(self) -> bool:
        """QA Task 존재가 유일한 durable 신호다. 없으면 Supervisor 기본값(True).

        Synthesis까지 갔는데 QA Task가 하나도 없다면, 그 워크플로는 실제로
        `qa_required=false`로 돌았다는 뜻이다. 그 전에는 아직 QA 단계에
        도달하지 않았을 뿐이므로 기본값을 유지한다.
        """

        if self.qa_nodes:
            return True
        return self.synthesis_node is None

    @property
    def status(self) -> str:
        if self.root.status == "archived":
            return STATUS_ARCHIVED
        synthesis = self.synthesis_node
        if synthesis is not None and synthesis.done:
            return STATUS_COMPLETED
        if self.root.blocked:
            return STATUS_BLOCKED
        if self.root.failed:
            return STATUS_FAILED
        if not self.descendants:
            # Root만 있는 상태. Planner가 아직 child를 만들지 않았다.
            return STATUS_COMPLETED if self.root.done else STATUS_QUEUED
        # Blocked/Failed를 Running보다 먼저 본다. 형제 하나가 아직 돌고 있다는
        # 이유로 "Supervisor 개입이 필요하다"를 숨기면, 화면은 영원히 진행 중으로
        # 보이고 사용자는 자기 입력을 기다리는 Task를 못 찾는다. Retry/Replan으로
        # 회복되면 다음 polling에서 다시 running으로 돌아온다.
        if any(node.blocked for node in self.descendants):
            return STATUS_BLOCKED
        if any(node.failed for node in self.descendants):
            return STATUS_FAILED
        if any(not node.terminal for node in self.descendants):
            return STATUS_RUNNING
        if self.root.done:
            return STATUS_COMPLETED
        # 모든 자식이 끝났지만 Supervisor가 다음 Task를 아직 안 만든 구간.
        return STATUS_RUNNING

    def _stage_status(self, nodes: Sequence[WorkflowNode]) -> str:
        """단계(QA/Synthesis) 한 칸의 표시 상태. Task가 아직 없으면 `todo`."""

        if not nodes:
            return "todo"
        observed = set()
        for node in nodes:
            if node.blocked:
                observed.add("blocked")
            elif node.failed:
                observed.add("failed")
            else:
                observed.add(_STAGE_BY_STATUS.get(node.status, "running"))
        for stage in _STAGE_PRIORITY:
            if stage in observed:
                return stage
        return "running"

    @property
    def qa_stage(self) -> str:
        return self._stage_status(self.qa_nodes)

    @property
    def synthesis_stage(self) -> str:
        synthesis = self.synthesis_node
        return self._stage_status((synthesis,) if synthesis else ())

    @property
    def qa_verdict(self) -> str | None:
        """QA Task의 종료 상태에서 결정론적으로 뽑는다. 문장 해석은 하지 않는다.

        라벨 포맷(`verdict: PASS` 같은 콜론 표기)이 없으면 None을 반환한다.
        "라벨이 없으니까 무조건 PASS로 간주"는 위험하다 — QA가 조건부(CONDITIONAL)
        또는 불명확한 판정을 내렸을 수도 있는데, 정규식이 한국어 조사("verdict는",
        "verdict가" 등) 변형을 못 잡으면 그 QA 판정을 "깨끗하게 통과"로 둔갑시킨다.
        이건 개발 원칙 9("실패 시 확대가 아니라 진입 차단")의 정반대 방향이다.
        """

        if not self.qa_nodes:
            return None
        node = self.qa_nodes[-1]
        if node.blocked:
            return QA_BLOCKED_VERDICT
        if node.failed:
            return "FAIL"
        if not node.done:
            return None
        return _labelled_token(node.summary, _VERDICT_RE, _VERDICT_VALUES)

    @property
    def decision(self) -> str | None:
        """Synthesis 요약에 라벨로 적힌 결정만 인정한다. 없으면 None."""

        synthesis = self.synthesis_node
        if synthesis is None or not synthesis.done:
            return None
        return _labelled_token(synthesis.summary, _DECISION_RE, _DECISION_VALUES)

    @property
    def block_reason(self) -> str | None:
        """워크플로가 막힌 첫 사유. Root -> QA -> 나머지 순으로 본다."""

        candidates: list[WorkflowNode] = [self.root, *self.qa_nodes, *self.descendants]
        for node in candidates:
            if node.blocked or node.failed:
                reason = node.block_reason or node.error or node.summary
                if reason:
                    return reason[:2000]
                return f"{node.task_id} {node.status}"
        return None

    @property
    def department_summaries(self) -> dict[str, str]:
        """부서 코드 -> 요약. 같은 부서가 여러 Task면 마지막 완료본을 쓴다."""

        summaries: dict[str, str] = {}
        for node in (*self.primary_nodes, *self.qa_nodes):
            if node.summary:
                summaries[node.department] = node.summary
        return summaries

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        """(parent, child) 쌍. 그래프 안에 있는 부모만 남긴다."""

        known = set(self.by_id)
        collected: list[tuple[str, str]] = []
        for node in self.descendants:
            for parent_id in node.parents:
                if parent_id in known and (parent_id, node.task_id) not in collected:
                    collected.append((parent_id, node.task_id))
        return tuple(collected)


Fetch = Callable[[str], dict[str, Any]]


def resolve_root_id(task_id: str, *, fetch: Fetch = show_task) -> str:
    """자식 ID로 물어봐도 같은 워크플로를 보게 Root까지 올라간다."""

    current = task_id
    visited: set[str] = set()
    for _ in range(_MAX_PARENT_HOPS):
        if current in visited:
            break
        visited.add(current)
        parents = _ids(fetch(current).get("parents"))
        if not parents:
            return current
        current = parents[0]
    return current


def load_workflow(
    task_id: str, *, fetch: Fetch = show_task, max_workers: int | None = None
) -> Workflow:
    """Root를 찾고 그 아래 그래프 전체를 폭 우선으로 읽는다.

    Task 한 건마다 CLI 프로세스가 하나씩 뜨므로 같은 깊이는 병렬로 읽는다.
    Polling 주기가 짧은 화면에서 직렬 호출은 그대로 응답 지연이 된다.
    목록처럼 여러 Root를 동시에 읽는 호출자는 `max_workers`를 낮춰서 전체
    동시 프로세스 수를 스스로 제한한다.
    """

    root_id = resolve_root_id(task_id, fetch=fetch)
    payloads: dict[str, dict[str, Any]] = {root_id: fetch(root_id)}
    frontier = list(_ids(payloads[root_id].get("children")))

    with ThreadPoolExecutor(max_workers=max_workers or _FETCH_WORKERS) as pool:
        while frontier and len(payloads) < _MAX_NODES:
            pending = [child_id for child_id in dict.fromkeys(frontier) if child_id not in payloads]
            pending = pending[: max(0, _MAX_NODES - len(payloads))]
            if not pending:
                break
            fetched = list(pool.map(fetch, pending))
            frontier = []
            for child_id, payload in zip(pending, fetched):
                payloads[child_id] = payload
                frontier.extend(_ids(payload.get("children")))

    nodes = [WorkflowNode.from_hermes(payloads[root_id])]
    nodes.extend(
        WorkflowNode.from_hermes(payload)
        for node_id, payload in payloads.items()
        if node_id != root_id
    )
    return Workflow(root_task_id=root_id, nodes=tuple(nodes))


def list_ceo_roots(*, limit: int, include_archived: bool = False) -> list[dict[str, Any]]:
    """사용자가 만든 CEO Root만 최신순으로. Supervisor 제어 Task는 뺀다."""

    rows = list_tasks(assignee=CEO_PROFILE, include_archived=include_archived)
    roots: list[dict[str, Any]] = []
    for row in rows:
        body = _text(row.get("body"))
        if not is_ceo_root_body(body):
            continue
        roots.append(row)
        if len(roots) >= limit:
            break
    return roots


__all__ = [
    "CEO_PROFILE",
    "QA_BLOCKED_VERDICT",
    "QA_PROFILE",
    "ROLE_PRIMARY",
    "ROLE_QA",
    "ROLE_ROOT",
    "ROLE_SYNTHESIS",
    "ROLE_USER_INPUT",
    "STATUS_ARCHIVED",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "KanbanTaskNotFound",
    "KanbanUnavailable",
    "Workflow",
    "WorkflowNode",
    "archive_tasks",
    "extract_user_query",
    "is_ceo_root_body",
    "list_ceo_roots",
    "list_tasks",
    "load_workflow",
    "resolve_root_id",
    "run_kanban",
    "show_task",
]
