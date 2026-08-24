#!/usr/bin/env python3
"""Hermes 부서 Agent CLI 실행기. 부서별 Router가 공유한다.

소유: 도현 (트레이딩 + 회계·포트폴리오)
근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)

**부서는 Router가 정한다 - 요청 Body로 받지 않는다.** 클라이언트가 부서 이름을
보낼 수 있으면 서버 화이트리스트가 유일한 방어선이지만, 경로로 고정하면 회계
화면에서 트레이딩 Agent를 부를 방법 자체가 없다. 화이트리스트 항목을 하나
늘리는 실수로 5.6이 무너지지 않는다.

응답 계약 두 개는 여기서 한 번만 정의한다.

1. `authoritative: false` - Agent 텍스트는 공식 수치가 아니다.
2. `source_of_record: /ui/snapshot` - 화면이 수치를 가져갈 곳은 여기뿐이다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]

try:
    from orchestration.canonical_profiles import (
        USER_QUERY_PRIORITY,
        CanonicalKanbanTaskRequest,
    )
    from orchestration.ceo_workflow_scope import build_root_comment
    from orchestration.primary_task_idempotency import validate_primary_create
except ImportError:  # pragma: no cover - `python apps/api/hermes_boundary.py` 직접 실행
    # 스크립트로 직접 돌리면 sys.path[0] 이 apps/api 라 저장소 루트가 안 보인다.
    # (`from ..orchestration...` 는 이 파일이 패키지 안이 아니라 어떤 경로로도
    #  성립하지 않는다 - 자체 점검을 돌리려면 sys.path 를 넣는 쪽이 필요하다.)
    import sys

    sys.path.insert(0, str(ROOT))
    from orchestration.canonical_profiles import (
        USER_QUERY_PRIORITY,
        CanonicalKanbanTaskRequest,
    )
    from orchestration.ceo_workflow_scope import build_root_comment
    from orchestration.primary_task_idempotency import validate_primary_create

# Hermes chat은 응답이 문자열이어도 Profile의 Tool을 실행할 수 있다. 인증, 사용자별
# 권한과 Tool Allowlist가 붙기 전에는 명시적인 로컬 개발 Opt-in 없이는 열지 않는다.
ENABLE_AGENT_ASK = os.getenv("ENABLE_AGENT_ASK", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


def agent_ask_enabled() -> bool:
    """Resolve the feature flag at call time, without import-order leakage.

    ``main`` and the domain routers are imported by several E2E modules.  A
    module-level snapshot made the first import win, so a later test or
    deployment environment could change ``ENABLE_AGENT_ASK`` without changing
    the behavior.  Keep the constant for compatibility with existing callers
    that patch it, while honoring an explicitly present environment value.
    """

    configured = os.getenv("ENABLE_AGENT_ASK")
    if configured is None:
        return ENABLE_AGENT_ASK
    return configured.strip().lower() in {"1", "true", "yes", "on"}


class AgentAsk(BaseModel):
    """부서 Agent 질의 Body. 부서 이름이 없는 것이 이 계약의 핵심이다."""

    query: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=128)


# ▶ 어느 런타임에 붙을 것인가 (2026-08-11 추가, 로컬 시험용)
#   이 모듈은 BFF 와 Hermes 가 같은 호스트에 있다고 가정한다. AWS 는 그게 맞지만
#   로컬 시험에서는 Hermes 가 부서마다 **컨테이너 안**에 있고 호스트(윈도우)에는
#   `hermes` 가 없다. 그래서 실행 방식을 env 로 고른다 - 기본이 `local` 이라
#   AWS 동작은 바뀌지 않는다.
#     HERMES_EXEC_MODE=local  : `hermes -p <profile> ...`          (기본)
#     HERMES_EXEC_MODE=docker : `docker exec -u hermes <컨테이너> hermes ...`
#   컨테이너 이름은 프로필 이름에서 짓지 않고 표에 적는다 - 이름 규칙이 어긋난 게
#   하나라도 있으면(인사팀처럼) 규칙으로 지은 이름은 조용히 틀린 곳에 붙는다.
HERMES_EXEC_MODE = os.getenv("HERMES_EXEC_MODE", "local").strip().lower()
PROFILE_CONTAINERS = {
    "ceo-agent": "hedgefund-ceo-hermes",
    "research-department": "hedgefund-research-hermes",
    "trading-department": "hedgefund-trading-hermes",
    "risk-management": "hedgefund-risk-hermes",
    "quant-backtest-department": "hedgefund-quant-hermes",
    "accounting-portfolio-department": "hedgefund-accounting-hermes",
    "qa-department": "hedgefund-qa-hermes",
    # 인사팀만 프로필 이름(hr-department)과 컨테이너 이름이 어긋난다.
    "hr-department": "hedgefund-workforce-hermes",
}
# 카드 생성처럼 부서를 특정하지 않는 kanban 명령을 돌릴 컨테이너.
KANBAN_CLI_CONTAINER = os.getenv("KANBAN_CLI_CONTAINER", "hedgefund-qa-hermes")

# ▶ 부서장 계측(2026-08-20). stage 와 신원(head_persona)은 여기서 만들지 않는다 -
#   orchestration/llm_observability.stage_for_profile / head_persona_for_profile 가
#   정본이고, ceo-supervisor 의 카드 종료 관측도 **같은 함수**를 쓴다. 두 write 측이
#   각자 이름을 만들면 같은 부서장이 두 신원으로 쪼개져 유휴 판정이 양쪽 다 놓친다.
#   (한때 여기 자체 표가 있었는데 orchestration/canonical_profiles.py 의 부서 코드
#   표와 값이 겹치는 복제였다 - 어긋나도 아무도 모르는 종류의 중복이라 지웠다.)


def local_binary() -> str:
    """local 모드에서 실제로 실행 가능한 `hermes` 경로.

    ## 왜 `HERMES_BIN` 을 그대로 믿지 않나 (2026-08-20 실측)

    `.env` 의 `# [컨테이너 내부 경로]` 블록에 `HERMES_BIN=/usr/local/bin/hermes`
    가 있고 `main.py` 가 그 파일을 `load_dotenv` 로 읽는다. 그래서 **컨테이너
    경로가 호스트 BFF 프로세스까지 새어 들어와** 윈도우에서 그 경로를 실행하려다
    `FileNotFoundError` 가 났고, 화면에는 "Hermes CLI를 찾을 수 없습니다"(503)만
    떴다 - 정작 `hermes` 는 PATH 에 멀쩡히 있었다. Agent Logs 의 Kanban 보드가
    통째로 안 뜬 원인이 이것이다.

    `docker-compose.yml` 은 같은 값을 서비스마다 자기가 직접 적어 주므로
    (`HERMES_BIN: /usr/local/bin/hermes`) `.env` 쪽 값을 무시해도 컨테이너 동작은
    바뀌지 않는다.

    **설정을 버리는 게 아니라 실행 가능할 때만 쓴다.** 리눅스 호스트에서 BFF 를
    컨테이너 밖으로 돌리면 `/usr/local/bin/hermes` 가 진짜로 있을 수 있고, 그때는
    그 값이 맞다. 없을 때만 PATH 로 떨어진다.
    """

    configured = os.environ.get("HERMES_BIN", "").strip()
    if configured and (Path(configured).exists() or shutil.which(configured)):
        return configured
    # PATH 에도 없으면 이름 그대로 둔다 - 여기서 예외를 올리면 "설치 안 됨"과
    # "경로 설정 오류"가 같은 자리에서 터져 호출부가 구분하지 못한다.
    return shutil.which("hermes") or "hermes"


def argv_for(department: str | None, tail: list[str]) -> list[str]:
    """실행 argv 를 만든다. shell 을 거치지 않으므로 사용자 문자열이 안전하다.

    `department` 가 None 이면 부서에 매이지 않는 명령(kanban 등)이다.

    ▶ `-u hermes` 는 빼면 안 된다. `docker exec` 는 기본이 root 라, 그렇게 부르면
      세션·메모리·kanban WAL 파일이 root 소유로 생기고 **그다음부터 정작
      에이전트(uid 1000)가 자기 파일을 못 쓴다.** 2026-08-11 에 보드 WAL 이
      root:root 가 돼 부서 워커의 `kanban_complete` 가 전부 실패했다.
    """
    if HERMES_EXEC_MODE != "docker":
        return [local_binary(), *(["-p", department] if department else []), *tail]
    if department is None:
        return ["docker", "exec", "-u", "hermes", "-i", KANBAN_CLI_CONTAINER, "hermes", *tail]
    container = PROFILE_CONTAINERS.get(department)
    if container is None:
        # 규칙으로 지어내지 않는다. 모르는 부서는 못 부르는 게 맞다.
        raise HTTPException(503, f"컨테이너를 모르는 프로필입니다: {department}")
    # 컨테이너 안에서는 /opt/data 가 그 부서 프로필 자체라 `-p` 를 붙이지 않는다.
    # 붙이면 `/opt/data/profiles/<이름>` 을 다시 찾아 들어가 memory·session 이
    # 부서 본체가 아니라 이름표 디렉터리에 쌓인다.
    return ["docker", "exec", "-u", "hermes", "-i", container, "hermes", *tail]


def create_kanban_task(
    *,
    assignee: str,
    title: str,
    body: str,
    idempotency_key: str,
    priority: int = USER_QUERY_PRIORITY,
    initial_status: str | None = None,
) -> dict[str, object] | None:
    """Create a shared-board card through Hermes CLI when available.

    The BFF never opens ``kanban.db`` directly. The Hermes CLI owns the board
    path and applies the same profile/permission boundary as other Kanban
    operations. When the CLI is not installed, the caller gets ``None`` and
    the natural-language query can still use the normal Hermes path.
    """

    request = CanonicalKanbanTaskRequest(
        assignee=assignee,
        title=title,
        body=body,
        idempotency_key=idempotency_key,
        priority=priority,
    )
    if os.getenv("ENABLE_KANBAN_TASK_TRACKING", "1").casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    primary_rejection = validate_primary_create(
        request.body,
        request.assignee,
        request.idempotency_key,
    )
    if primary_rejection:
        # Keep invalid QA-primary requests out of the CLI and durable board.
        raise ValueError(primary_rejection)

    # 부서에 매이지 않는 명령이라 department=None 이다. 로컬(docker) 모드에서는
    # 컨테이너 안에서 돌아 보드 경로·권한이 에이전트와 같아진다.
    if initial_status not in {None, "blocked", "running"}:
        raise ValueError("initial_status must be blocked, running, or None")
    command_tail = [
        "kanban",
        "create",
        request.title,
        "--body",
        request.body,
        "--assignee",
        request.assignee,
        "--idempotency-key",
        request.idempotency_key,
        "--created-by",
        "ai-office-bff",
        # 사람이 기다리는 카드를 공장 주기 뒤에 세우지 않는다. Hermes ready 큐가
        # priority DESC 정렬이라 이 한 값이 대기열 순서를 바꾼다(2026-08-14 실측:
        # ready 23 장 뒤에서 사용자 질의가 6 분 대기).
        "--priority",
        str(request.priority),
    ]
    if initial_status is not None:
        command_tail.extend(("--initial-status", initial_status))
    command_tail.append("--json")
    command = argv_for(None, command_tail)
    cli_environment = os.environ.copy()
    cli_environment.setdefault("HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban"))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # 컨테이너 안 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면
            # 한국어 제목이 깨진 채로 보드에 들어간다.
            encoding="utf-8", errors="replace",
            timeout=float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8")),
            check=False,
            cwd=ROOT,
            env=cli_environment,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("id") or payload.get("task_id")
    return {
        "task_id": str(task_id) if task_id else None,
        "status": str(payload.get("status", "TODO")),
        "source": "hermes-kanban",
    }


def unblock_kanban_task(*, task_id: str) -> bool:
    """Release a deliberately blocked card after its durable scope is bound."""

    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    cli_environment = os.environ.copy()
    cli_environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    command_timeout = float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8"))
    try:
        proc = subprocess.run(
            argv_for(None, ["kanban", "unblock", task_id]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=command_timeout,
            cwd=ROOT,
            env=cli_environment,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    except subprocess.TimeoutExpired:
        # Hermes/SQLite can commit the promotion before the CLI finishes its
        # cold-start/JSON teardown.  A timeout therefore has an unknown result,
        # not a proven failure; verify the durable task state below.
        proc = None
    if proc is not None and proc.returncode == 0:
        return True
    # The create boundary is idempotent, so a replay may encounter a card
    # that a previous attempt already released (or even completed). Treat only
    # a positively observed non-blocked state as success; unreadable state
    # remains a failure.
    current = show_kanban_task(task_id, timeout=max(command_timeout, 2.0))
    return bool(
        current
        and str(current.get("status") or "").casefold()
        in {"ready", "running", "done", "completed"}
    )


def complete_kanban_task(*, task_id: str, result: str) -> bool:
    """Close a non-executing scope card without exposing it to a worker.

    Direct PAPER-order roots are durable workflow containers, not executable
    CEO prompts.  They are created running but unclaimed while the SQL bindings
    and blocked Trading primary are assembled, then completed in place.  This
    avoids the otherwise unavoidable race where the CEO dispatcher claims the
    root before Trading invokes the trusted order tool.

    A CLI timeout has unknown commit status, so verify the supported read model
    before reporting failure.  Replays are idempotent when the card is already
    terminal.
    """

    task_id = str(task_id or "").strip()
    result = str(result or "").strip()
    if not task_id or not result:
        return False
    cli_environment = os.environ.copy()
    cli_environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    command_timeout = float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8"))
    try:
        proc = subprocess.run(
            argv_for(
                None,
                [
                    "kanban",
                    "complete",
                    task_id,
                    "--result",
                    result,
                    "--summary",
                    result,
                ],
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=command_timeout,
            cwd=ROOT,
            env=cli_environment,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    except subprocess.TimeoutExpired:
        proc = None
    if proc is not None and proc.returncode == 0:
        return True
    current = show_kanban_task(task_id, timeout=max(command_timeout, 2.0))
    return bool(
        current
        and str(current.get("status") or "").casefold()
        in {"done", "completed", "archived"}
    )


def show_kanban_task(
    task_id: str,
    *,
    timeout: float | None = None,
) -> dict[str, object] | None:
    """Read one Kanban task through Hermes' supported JSON CLI boundary.

    This is intentionally read-only.  The BFF uses it to expose the planner's
    already-created graph; it never starts a second CEO turn or accesses the
    shared Kanban database directly.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return None

    cli_environment = os.environ.copy()
    cli_environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    command = argv_for(None, ["kanban", "show", task_id, "--json"])
    read_timeout = timeout
    if read_timeout is None:
        try:
            read_timeout = float(os.getenv("CEO_PLANNING_READ_TIMEOUT_SECONDS", "2"))
        except ValueError:
            read_timeout = 2.0
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # 이 CLI 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면 한글
            # 제목에서 UnicodeDecodeError 가 나고 **리더 스레드가 죽어 stdout 이
            # None 이 된다** - 그러면 "잘못된 JSON"으로 보여 원인이 가려진다.
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, read_timeout),
            cwd=ROOT,
            env=cli_environment,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    task = payload.get("task", payload)
    if not isinstance(task, dict):
        return None
    normalized = dict(task)
    normalized.setdefault("id", normalized.get("task_id") or task_id)
    # Hermes puts graph/run projections beside ``task``.  Preserve only the
    # supported fields needed by the presentation layer.
    for key in ("latest_summary", "parents", "children", "comments", "events", "runs"):
        if key in payload:
            normalized[key] = payload[key]
    return normalized


def list_kanban_tasks(*, timeout: float | None = None) -> tuple[dict[str, object], ...] | None:
    """Read the current Kanban task projection through Hermes' JSON CLI."""
    cli_environment = os.environ.copy()
    cli_environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    try:
        read_timeout = timeout
        if read_timeout is None:
            read_timeout = float(os.getenv("CEO_PLANNING_READ_TIMEOUT_SECONDS", "2"))
    except ValueError:
        read_timeout = 2.0
    command = [local_binary(), "kanban", "list", "--json"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # 이 CLI 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면 한글
            # 제목에서 UnicodeDecodeError 가 나고 **리더 스레드가 죽어 stdout 이
            # None 이 된다** - 그러면 "잘못된 JSON"으로 보여 원인이 가려진다.
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, read_timeout),
            cwd=ROOT,
            env=cli_environment,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        return None
    return tuple(dict(item) for item in payload)


def comment_kanban_task(*, task_id: str, text: str) -> bool:
    """Write a durable Kanban comment through Hermes' supported CLI.

    The BFF only uses this for the concrete CEO root scope marker.  It never
    opens a gateway or writes the shared database directly.
    """

    if not task_id or not text.strip():
        return False
    cli_environment = os.environ.copy()
    cli_environment.setdefault(
        "HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban")
    )
    # `argv_for` 를 거쳐야 HERMES_EXEC_MODE=docker 가 지켜진다. 2026-08-14 실측:
    # 여기서만 로컬 바이너리를 직접 불러 컨테이너(hermes 미설치)에서 항상 실패했고,
    # /ui/ceo/ask 가 루트 카드를 만든 뒤 503 을 던져 **고아 루트만 쌓였다**
    # (카드 생성은 argv_for 를 쓰므로 성공, 스코프 코멘트만 실패 = 입구가 반만 동작).
    command = argv_for(
        None,
        ["kanban", "comment", task_id, text, "--author", "ai-office-bff"],
    )
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # 이 CLI 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면 한글
            # 제목에서 UnicodeDecodeError 가 나고 **리더 스레드가 죽어 stdout 이
            # None 이 된다** - 그러면 "잘못된 JSON"으로 보여 원인이 가려진다.
            encoding="utf-8",
            errors="replace",
            timeout=float(os.getenv("KANBAN_CLI_TIMEOUT_SECONDS", "8")),
            cwd=ROOT,
            env=cli_environment,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def comment_root_scope(*, task_id: str, request_id: str) -> bool:
    """Bind a created root ID before its ready task can be dispatched."""

    return comment_kanban_task(
        task_id=task_id,
        text=build_root_comment(task_id, request_id),
    )


def ask(
    *,
    department: str,
    config: str,
    query: str,
    resume: str | None = None,
    enabled: bool | None = None,
    timeout: int | None = None,
) -> dict:
    """Hermes 부서 Profile에 질의하고 텍스트만 돌려준다. 아무 상태도 바꾸지 않는다.

    `hermes -p <profile>`로 부른다. `hermes profile create`가 만들어주는 부서 이름
    Wrapper(`accounting-portfolio-department.bat`)는 쓰지 않는다 - 그 Wrapper는
    `~/.local/bin`에 생기고 PATH에 없을 수 있으며, 내용도 이 명령 한 줄이다.

    `resume` 는 이전 세션을 이어받는다. 사용자 질의 하나가 여러 부서를 거칠 때
    CEO 가 **자기가 뭘 시켰는지 기억한 채로** 종합해야 하기 때문이다.

    `enabled` 는 어느 스위치가 이 호출을 허가했는지 호출자가 명시하는 자리다.
    기본은 지금까지처럼 `ENABLE_AGENT_ASK` 다.
    """
    if not (agent_ask_enabled() if enabled is None else enabled):
        raise HTTPException(
            503,
            "Agent 질의는 인증·Tool Allowlist 연결 전까지 기본 비활성화 상태입니다.",
        )

    # ▶ 기본은 부서 Profile 의 `agent.timeout_seconds` 다. 그 값은 **한 번 묻고 한 번
    #   답하는** 조회용으로 잡혀 있다(CEO 는 30초). 카드를 만들며 도구를 여러 번
    #   부르는 라우팅·종합 턴은 같은 초를 쓰면 매번 잘린다 - 2026-08-11 실측에서
    #   CEO 라우팅은 90~180초가 걸렸고 30초에서 통째로 끊겼다(그러고도 카드는 이미
    #   만들어져 부서들이 실행됐다). 그래서 그런 호출은 자기 timeout 을 명시한다.
    timeout = timeout_of(config) if timeout is None else timeout
    # 부서장 턴의 소요시간·성공 여부를 여기서 잰다(2026-08-20). 모든 부서 라우터가
    # 이 함수 하나를 지나므로 일반 질문 경로가 전부 계측된다.
    _started = time.perf_counter()
    tail = ["chat", "-Q"]
    if resume:
        tail += ["--resume", resume]
    tail += ["-q", query]
    try:
        # ponytail: 요청마다 CLI 프로세스를 새로 띄운다(호출당 ~20s, 대화 이어짐 없음).
        # 상시 연결이 필요해지면 `hermes serve`(JSON-RPC/WebSocket, 기본 9119)로 바꾼다.
        # shell=False. 사용자 문자열이 셸을 거치지 않게 인자 리스트로만 넘긴다.
        # -Q: 배너·스피너·Tool Preview 없이 최종 답변만. 이게 없으면 ANSI 색코드와
        # 박스 문자가 그대로 화면까지 흘러간다.
        proc = subprocess.run(
        argv_for(department, tail),
        check=False, capture_output=True, text=True,
        # 컨테이너 안 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면
        # 한국어 답변이 깨진 채로 화면까지 간다.
        encoding="utf-8", errors="replace",
        timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError as exc:
        # Hermes Runtime은 PyPI 패키지가 아니라 별도 설치다(CLAUDE.md).
        # 런타임 부재는 부서장이 못 돈 것이지 안 돈 것이 아니다 - DEGRADED 로 남긴다.
        _publish_head_turn(department=department, started=_started, status="DEGRADED")
        raise HTTPException(
            503, f"Hermes CLI 없음: hermes -p {department}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _publish_head_turn(department=department, started=_started, status="TIMEOUT")
        raise HTTPException(504, f"{timeout}s 초과") from exc

    if proc.returncode != 0:
        _publish_head_turn(department=department, started=_started, status="DEGRADED")
        raise HTTPException(502, (proc.stderr or "").strip()[:500] or "agent failed")

    _publish_head_turn(department=department, started=_started, status="COMPLETED")

    return {
        "department": department,
        # -Q 여도 stdout 에 터미널 chrome 과 추론 스트림이 섞여 나온다
        # (2026-08-11 실측). 그대로 두면 사용자 화면에 테두리 문자와
        # `**Planning ...**` 이 찍힌다.
        "answer": clean_answer(proc.stdout),
        # 어느 Hermes Session이 이 문장을 만들었는지. 감사 추적에 필요하다.
        "session_id": session_id_of(proc.stderr or ""),
        # 화면이 이 값을 수치로 쓰지 못하게 계약에 박아둔다.
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# 한 줄 전체가 박스 chrome 인 경우만 지운다. 답변 안에 표(│)가 들어 있을 수 있으므로
# "│ 로 시작한다" 같은 넓은 규칙은 쓰지 않는다 - 내용을 지우는 쪽이 더 나쁘다.
_BOX_LINE = re.compile(r"^[┌└├┬┴┼]─.*$|^[│─┌┐└┘├┤┬┴┼\s]*$")
_REASONING_OPEN = re.compile(r"^┌─\s*Reasoning")
# 추론 헤드라인은 **한 줄 전체가** 굵은 글씨 토막들로만 이루어져 있다
# (실측: `**A****A****B**` 처럼 같은 문구가 두 번씩 붙어 나온다).
_REASONING_LINE = re.compile(r"^(?:\s*\*\*[^*]+\*\*\s*)+$")


def clean_answer(stdout: str) -> str:
    """`-Q` 로도 남는 터미널 chrome 과 추론 스트림을 걷어낸다.

    ▶ 실측 형태 (2026-08-11, 도구를 쓰는 턴):
        \\n┌─ Reasoning ────────┐\\n**A****A****B**\\n...\\n13\\n
      **닫는 테두리가 없다.** 그래서 "여는 테두리~닫는 테두리 사이를 버린다"는
      규칙은 쓸 수 없고, 추론 헤드라인 줄을 모양으로 알아본 뒤 **처음으로 그
      모양이 아닌 줄에서 멈춘다** - 그 아래는 전부 답변이다. 답변 안에
      `**결론**` 같은 줄이 있어도 그때는 이미 스트립을 끝낸 뒤라 살아남는다.
    """
    lines: list[str] = []
    in_reasoning = False
    stripping = True  # 답변 본문이 시작되기 전까지만 걷어낸다
    for raw in _ANSI.sub("", stdout).replace("\r", "\n").split("\n"):
        text = raw.strip()
        if stripping:
            if _REASONING_OPEN.match(text):
                in_reasoning = True
                continue
            if _BOX_LINE.match(text):
                continue
            if in_reasoning and _REASONING_LINE.match(text):
                continue
            if not text:
                continue
            stripping = False  # 여기서부터 답변이다
        lines.append(raw.rstrip())
    return "\n".join(lines).strip()


def session_id_of(stderr: str) -> str | None:
    """Hermes가 stderr에 찍는 `session_id: ...`를 뽑는다. 없으면 None."""
    for line in stderr.splitlines():
        if line.startswith("session_id: "):
            return line[len("session_id: "):].strip()
    return None


def timeout_of(config: str) -> int:
    """저장소 Profile의 agent.timeout_seconds를 그대로 쓴다. 부서마다 다르다."""
    cfg = yaml.safe_load((ROOT / config).read_text(encoding="utf-8"))
    return int(cfg["agent"]["timeout_seconds"])


def _publish_head_turn(*, department: str, started: float, status: str) -> None:
    """부서장 턴 1건을 HR 관측으로 내보낸다. 실패는 삼킨다.

    일반 질문 트래픽은 여기서 끝난다 - Worker 를 부를지는 부서장의 판단이라,
    이 지점을 재지 않으면 Worker 실행 0 회가 "일이 없었다"인지 "위임하지 않았다"인지
    구분되지 않는다(Department Scorecard 의 arrivals).
    """

    try:
        from orchestration.llm_observability import (
            head_persona_for_profile,
            publish_head_activity,
            stage_for_profile,
        )

        stage = stage_for_profile(department)
        persona = head_persona_for_profile(department)
        if not stage or not persona:
            # 모르는 프로필의 stage 를 이름으로 지어내지 않는다 - 틀린 stage 로
            # 나간 이벤트는 조회되지 않으면서 있는 것처럼 보인다.
            return
        publish_head_activity(
            stage=stage,
            head_persona=persona,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_count=0 if status == "COMPLETED" else 1,
            source="bff_ask",
        )
    except Exception:  # noqa: BLE001 - 계측은 질의 경로를 바꾸지 못한다
        return


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 실측 형태(2026-08-11): 추론 헤드라인 뒤에 답이 온다
    real = (
        "\n┌─ Reasoning ─────────┐\n"
        "**Searching repo****Searching repo****Inspecting CLI**\n"
        "**Inspecting CLI****Clarifying criteria**\n"
        "13\n"
    )
    assert clean_answer(real) == "13", repr(clean_answer(real))
    assert clean_answer("\n입구 연결 확인\n") == "입구 연결 확인"
    # 답변 안의 굵은 제목은 지우지 않는다 - 본문이 시작된 뒤엔 스트립을 멈춘다
    kept = clean_answer("┌─ Reasoning ─┐\n**Thinking**\n결론입니다.\n**요약**\n- 한 줄\n")
    assert kept == "결론입니다.\n**요약**\n- 한 줄", repr(kept)
    assert clean_answer("| a | b |\n│ 표 │ 유지 │\n") == "| a | b |\n│ 표 │ 유지 │"

    # 컨테이너 모드: 부서 호출은 그 부서 컨테이너로, kanban 은 공용 컨테이너로
    saved = HERMES_EXEC_MODE
    try:
        globals()["HERMES_EXEC_MODE"] = "docker"
        assert argv_for("ceo-agent", ["chat"])[:5] == ["docker", "exec", "-u", "hermes", "-i"]
        assert argv_for(None, ["kanban", "create"])[5] == KANBAN_CLI_CONTAINER
        try:
            argv_for("없는-부서", ["chat"])
        except HTTPException:
            pass
        else:
            raise AssertionError("모르는 프로필인데 argv 를 만들었다")
    finally:
        globals()["HERMES_EXEC_MODE"] = saved
    # local 모드는 예전 동작 그대로여야 한다(AWS 가 이 경로다). 다만 binary 는
    # 이제 `local_binary()` 가 푼 실행 가능한 경로다 - 이름만 같으면 된다.
    local_argv = argv_for("ceo-agent", ["chat"])
    assert local_argv[1:3] == ["-p", "ceo-agent"], local_argv
    assert "hermes" in Path(local_argv[0]).name.casefold(), local_argv[0]

    # ▶ `.env` 의 컨테이너 경로가 호스트로 새는 것을 막는다(2026-08-20 실측).
    #   이게 없으면 윈도우에서 `/usr/local/bin/hermes` 를 실행하려다 503 이 뜬다.
    with_bogus = dict(os.environ)
    try:
        os.environ["HERMES_BIN"] = "/usr/local/bin/hermes"   # 컨테이너 전용 경로
        resolved = local_binary()
        assert resolved != "/usr/local/bin/hermes" or Path(resolved).exists(), (
            "존재하지 않는 컨테이너 경로를 그대로 실행하려 한다: " + resolved
        )
        # 실제로 있는 경로는 존중한다 - 리눅스 호스트에서 BFF 를 컨테이너 밖으로
        # 돌리는 배치가 그 경우다.
        os.environ["HERMES_BIN"] = sys.executable
        assert local_binary() == sys.executable, "실행 가능한 설정은 그대로 쓴다"
    finally:
        os.environ.clear()
        os.environ.update(with_bogus)

    # 부서 이름표가 정본과 어긋나지 않는가
    from orchestration.canonical_profiles import CANONICAL_PROFILES
    assert set(PROFILE_CONTAINERS) == set(CANONICAL_PROFILES), (
        set(PROFILE_CONTAINERS) ^ set(CANONICAL_PROFILES)
    )

    assert session_id_of("session_id: 20260811_x\n") == "20260811_x"
    assert session_id_of("아무것도 없음") is None
    print("hermes_boundary 자체 점검 통과")
