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
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import hermes_boundary
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]

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
from orchestration.ceo_workflow_scope import (
    CEO_WORKFLOW_SCOPE_MARKER,
    requested_by_from_body,
    selected_primary_profiles_from_task,
)

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
_WORKFLOW_ROOT_RE = re.compile(r"(?m)^workflow_root_task_id=(\S+)\s*$")
_WORKFLOW_ROLE_RE = re.compile(r"(?m)^workflow_role=(\S+)\s*$")
_WORKFLOW_METADATA_KEYS = (
    "primary_tasks",
    "primary_task_ids",
    "analysis_task_ids",
    "qa_task",
    "qa_task_id",
    "qa_dependency_ids",
    "synthesis_tasks",
    "synthesis_task_ids",
)
_PRIMARY_PROFILE_ORDER = (
    "research-department",
    "quant-backtest-department",
    "trading-department",
    "accounting-portfolio-department",
    "risk-management",
    "hr-department",
)


class KanbanUnavailable(RuntimeError):
    """Hermes Kanban CLI를 쓸 수 없다. 호출자는 503으로 옮긴다."""


class KanbanTaskNotFound(LookupError):
    """요청한 Task가 판에 없다. 호출자는 404로 옮긴다."""


def _timeout() -> float:
    return float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8"))


# ── 읽기 전용 CLI 호출 TTL 캐시 (2026-08-14) ────────────────────────────────
#
# `hermes kanban ...`는 호출마다 파이썬 프로세스가 새로 뜬다. 그 자체는 수십 ms지만
# 호출 **횟수**가 문제였다 - `load_workflow`가 워크플로 하나를 읽을 때
#   (1) `resolve_root_id`와 본문에서 root를 두 번 `show` 하고
#   (2) 소속 판정을 위해 보드 전체를 `list` 하며(아래 `load_workflow` 참고)
#   (3) 노드 수만큼 `show` 한다.
# `/ui/ceo/tasks`는 이걸 root 개수만큼 반복하므로, root 20개면 동일한
# `list --json`만 20번 돌고 전체 프로세스는 수백 개가 된다.
#
# 짧은 TTL 캐시가 이 중복을 걷어낸다. 주 목적은 **한 요청 안의 중복 제거**이고,
# 화면 폴링 주기(본부 진행 10초 / 최종 답변 15초)보다 TTL이 훨씬 짧아 다음
# polling은 항상 새 값을 받는다 - 진행 상황이 캐시 때문에 멈춰 보이지 않는다.
# 회계 스냅샷이 쓰는 2초 TTL과 같은 패턴이다(`apps/api/main.py`).
#
# 캐시하지 않는 것: 쓰기 명령(`archive`)과 **예외**. 실패를 캐시하면 일시적인
# CLI 장애가 TTL 동안 고정되어 fail-closed가 아니라 fail-stuck이 된다.
#
# ── 끝난 Task 는 더 길게 캐시한다 (2026-08-14) ──
#
# 위 TTL 은 "한 요청 안의 중복"만 걷어낸다. 목록 조회의 진짜 비용은 **서로 다른**
# Task 를 노드 수만큼 `show` 하는 것이라, 중복 제거만으로는 남는다.
#
# 그런데 `done`/`completed`/`archived` 로 끝난 Task 의 `show` 결과는 **더 이상
# 바뀌지 않는다.** 그래서 그 응답만 길게 캐시하면, 이미 끝난 과거 대화를 다시 그릴
# 때 CLI 를 아예 안 부른다 - 계정을 오가며 이력을 다시 여는 화면이 정확히 이 경우다.
#
# `failed`/`blocked` 는 길게 캐시하지 않는다. Retry·Replan 으로 다시 running 이
# 될 수 있어서(위 `Workflow.status` 주석) 끝난 상태가 아니다.
#
# ▶ 이 방식을 고른 이유(그리고 "마커가 있으면 `list` 만으로 목록을 만든다"를
#   포기한 이유): `list --json` 행에는 `parents`·`runs`·`latest_summary` 가 없다
#   (Hermes `_task_to_dict` 실측). `Workflow.status` 는 `archived`/`synthesis.done`
#   에서 run_outcome 보다 먼저 단락되므로 거기까지는 증명이 되지만,
#   `selected_departments` 가 `self.metadata`(= `show` 의 runs/metadata)와 노드
#   **탐색 순서**에 함께 의존해서 `list` 만으로는 같은 답을 보장할 수 없다.
#   여기서는 정확도를 깎지 않는 쪽을 택한다 - 같은 `show` 응답을 그대로 쓰되,
#   변하지 않는 것만 오래 들고 있는다.
_READ_ONLY_KANBAN_COMMANDS = frozenset({"show", "list"})
_cache_lock = threading.Lock()
# key -> (만료 시각, stdout). 항목마다 TTL 이 다르므로 저장 시각이 아니라 만료
# 시각을 넣는다.
_cache: dict[tuple[str, ...], tuple[float, str]] = {}


def _cache_ttl() -> float:
    """0이면 캐시를 끈다. 결정론이 필요한 테스트가 그렇게 쓴다."""

    try:
        return max(0.0, float(os.getenv("KANBAN_READ_CACHE_TTL_SECONDS", "3")))
    except ValueError:
        return 3.0


def _terminal_cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("KANBAN_DONE_CACHE_TTL_SECONDS", "300")))
    except ValueError:
        return 300.0


def _entry_ttl(key: tuple[str, ...], stdout: str, base_ttl: float) -> float:
    """이 응답을 얼마나 들고 있을지. 끝난 `show` 만 길게 잡는다.

    판정에 실패하면(형태가 예상과 다르면) 조용히 긴 TTL 로 넘어가지 않고 기본
    TTL 로 떨어진다 - 확신이 없을 때 오래 들고 있는 쪽이 위험하다.
    """

    if key[0] != "show":
        return base_ttl
    try:
        payload = json.loads(stdout)
        task = payload.get("task", payload) if isinstance(payload, Mapping) else None
        if not isinstance(task, Mapping):
            return base_ttl
        status = str(task.get("status") or "").casefold()
    except (TypeError, ValueError):
        return base_ttl
    if status in _DONE_STATUSES:
        return max(base_ttl, _terminal_cache_ttl())
    return base_ttl


def clear_kanban_cache() -> None:
    """캐시를 비운다. 보드를 바꾼 직후(archive)와 테스트가 부른다."""

    with _cache_lock:
        _cache.clear()


def _cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    return environment


def run_kanban(args: Sequence[str]) -> str:
    """`hermes kanban ...`를 실행하고 stdout을 준다. shell=False로만 부른다.

    argv 조립은 `hermes_boundary.argv_for(None, ...)`에 맡긴다 - 여기서 직접
    `[HERMES_BIN, "kanban", ...]`을 만들면 `HERMES_EXEC_MODE=docker` 환경에서
    이 경로만 호스트의 `hermes`를 찾아 실패한다(2026-08-14 발견). 쓰기 경로
    (`hermes_boundary.create_kanban_task`)는 이미 그 모드를 존중하고 있었으므로,
    같은 보드를 읽는 이 경로만 규칙이 달랐던 것이다. `department=None`은 부서에
    매이지 않는 kanban 명령을 뜻하고, docker 모드에서는 `KANBAN_CLI_CONTAINER`
    안에서 실행된다.

    읽기 명령(`show`/`list`)의 성공 결과만 짧은 TTL 동안 캐시한다 - 근거는
    `_READ_ONLY_KANBAN_COMMANDS` 위 주석 참고.
    """

    key = tuple(str(arg) for arg in args)
    cacheable = bool(key) and key[0] in _READ_ONLY_KANBAN_COMMANDS
    ttl = _cache_ttl()
    if cacheable and ttl > 0:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now < hit[0]:
                return hit[1]

    command = hermes_boundary.argv_for(None, ["kanban", *args])
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
    if cacheable and ttl > 0:
        entry_ttl = _entry_ttl(key, process.stdout, ttl)
        with _cache_lock:
            _cache[key] = (time.monotonic() + entry_ttl, process.stdout)
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
    # 보드를 바꿨으므로 읽기 캐시를 버린다 - 그러지 않으면 archive 직후 목록에
    # 방금 치운 카드가 TTL 동안 그대로 남는다.
    clear_kanban_cache()


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

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return (value,) if value.strip() else ()
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


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _run_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the newest durable task-run metadata, not ingress prose."""
    merged: dict[str, Any] = {}
    for key in ("metadata", "task_run_metadata", "run_metadata"):
        merged.update(_mapping(payload.get(key)))
    task_run = payload.get("task_run")
    if isinstance(task_run, Mapping):
        merged.update(_mapping(task_run.get("metadata", task_run)))
    nested = _mapping(merged.get("workflow_metadata"))
    merged.update(nested)
    runs = payload.get("runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes, bytearray)):
        return merged
    for run in runs:
        if isinstance(run, Mapping):
            run_metadata = _mapping(run.get("metadata"))
            merged.update(run_metadata)
            merged.update(_mapping(run_metadata.get("workflow_metadata")))
    return merged


def _workflow_root_id(payload: Mapping[str, Any]) -> str | None:
    body = _text(payload.get("body"))
    match = _WORKFLOW_ROOT_RE.search(body)
    return match.group(1).strip() if match else None


def _workflow_role(payload: Mapping[str, Any]) -> str | None:
    body = _text(payload.get("body"))
    match = _WORKFLOW_ROLE_RE.search(body)
    return match.group(1).strip().casefold() if match else None


def _metadata_task_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    for key in _WORKFLOW_METADATA_KEYS:
        ids.extend(_ids(metadata.get(key)))
    return tuple(dict.fromkeys(ids))


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
        return (
            self.profile == QA_PROFILE
            or self.profile in _LEGACY_QA_ALIASES
            or _workflow_role({"body": self.body}) == ROLE_QA
        )

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
        declared_role = _workflow_role({"body": self.body})
        if declared_role in {ROLE_PRIMARY, ROLE_QA, ROLE_SYNTHESIS}:
            return declared_role
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
    metadata: Mapping[str, Any] = field(default_factory=dict)
    root_payload: Mapping[str, Any] = field(default_factory=dict)

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

        selected = set(selected_primary_profiles_from_task(self.root_payload))
        return tuple(
            node
            for node in self.descendants
            if node.role(root_task_id=self.root_task_id) == ROLE_PRIMARY
            and (not selected or node.profile in selected)
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
        declared: Any = self.metadata.get("selected_departments")
        if isinstance(declared, str):
            try:
                declared = json.loads(declared)
            except (TypeError, ValueError):
                declared = ()
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
            for profile in declared:
                profile = str(profile).strip()
                if profile in _PRIMARY_PROFILE_ORDER and profile not in seen:
                    seen.append(profile)
        return tuple(seen)

    @property
    def qa_required(self) -> bool:
        """QA Task 존재가 유일한 durable 신호다. 없으면 Supervisor 기본값(True).

        Synthesis까지 갔는데 QA Task가 하나도 없다면, 그 워크플로는 실제로
        `qa_required=false`로 돌았다는 뜻이다. 그 전에는 아직 QA 단계에
        도달하지 않았을 뿐이므로 기본값을 유지한다.
        """

        declared: Any = self.metadata.get("qa_required")
        if isinstance(declared, str):
            declared = declared.strip().casefold() == "true"
        if isinstance(declared, bool):
            return declared or bool(self.qa_nodes)
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
        current_payload = fetch(current)
        declared_root = _workflow_root_id(current_payload)
        if declared_root:
            fetch(declared_root)
            return declared_root
        parents = _ids(current_payload.get("parents"))
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
    root_metadata = _run_metadata(payloads[root_id])
    frontier = list(_ids(payloads[root_id].get("children")))
    frontier.extend(_metadata_task_ids(root_metadata))

    # Primary/QA/synthesis tasks may intentionally have no Hermes parent edge.
    # The durable workflow marker is the membership source for those tasks.
    try:
        listed = list_tasks(include_archived=True)
    except (KanbanTaskNotFound, KanbanUnavailable):
        listed = []
    for row in listed:
        if _workflow_root_id(row) != root_id:
            continue
        row_id = str(row.get("id") or row.get("task_id") or "").strip()
        if row_id:
            frontier.append(row_id)

    with ThreadPoolExecutor(max_workers=max_workers or _FETCH_WORKERS) as pool:
        while frontier and len(payloads) < _MAX_NODES:
            pending = [child_id for child_id in dict.fromkeys(frontier) if child_id not in payloads]
            pending = pending[: max(0, _MAX_NODES - len(payloads))]
            if not pending:
                break
            fetched = list(pool.map(fetch, pending))
            frontier = []
            for child_id, payload in zip(pending, fetched, strict=True):
                payloads[child_id] = payload
                frontier.extend(_ids(payload.get("children")))

    nodes = [WorkflowNode.from_hermes(payloads[root_id])]
    nodes.extend(
        WorkflowNode.from_hermes(payload)
        for node_id, payload in payloads.items()
        if node_id != root_id
    )
    return Workflow(
        root_task_id=root_id,
        nodes=tuple(nodes),
        metadata=_run_metadata(payloads[root_id]),
        root_payload=payloads[root_id],
    )


def list_ceo_roots(
    *, limit: int, include_archived: bool = False, owner_id: str | None = None
) -> list[dict[str, Any]]:
    """사용자가 만든 CEO Root만 최신순으로. Supervisor 제어 Task는 뺀다.

    `owner_id`가 주어지면 `requested_by=` 줄이 그 값과 일치하는 Root만 남긴다
    (`limit` 컷오프 전에 걸러야 다른 계정 Root가 자리를 차지해 진짜 대상이
    잘려나가지 않는다). `requested_by`가 없는 과거 Root는 "계정 불명"으로
    보고 어떤 `owner_id` 필터에도 포함하지 않는다.
    """

    rows = list_tasks(assignee=CEO_PROFILE, include_archived=include_archived)
    roots: list[dict[str, Any]] = []
    for row in rows:
        body = _text(row.get("body"))
        if not is_ceo_root_body(body):
            continue
        if owner_id is not None and requested_by_from_body(body) != owner_id:
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
    "clear_kanban_cache",
    "extract_user_query",
    "is_ceo_root_body",
    "list_ceo_roots",
    "list_tasks",
    "load_workflow",
    "resolve_root_id",
    "run_kanban",
    "show_task",
]
