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

import os
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, Field

try:
    from orchestration.canonical_profiles import CanonicalKanbanTaskRequest
    from orchestration.ceo_workflow_scope import build_root_comment
except ImportError:  # pragma: no cover - direct module execution
    from ..orchestration.canonical_profiles import CanonicalKanbanTaskRequest
    from ..orchestration.ceo_workflow_scope import build_root_comment

ROOT = Path(__file__).resolve().parents[2]

# Hermes chat은 응답이 문자열이어도 Profile의 Tool을 실행할 수 있다. 인증, 사용자별
# 권한과 Tool Allowlist가 붙기 전에는 명시적인 로컬 개발 Opt-in 없이는 열지 않는다.
ENABLE_AGENT_ASK = os.getenv("ENABLE_AGENT_ASK", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


class AgentAsk(BaseModel):
    """부서 Agent 질의 Body. 부서 이름이 없는 것이 이 계약의 핵심이다."""

    query: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=128)


def create_kanban_task(
    *,
    assignee: str,
    title: str,
    body: str,
    idempotency_key: str,
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
    )
    if os.getenv("ENABLE_KANBAN_TASK_TRACKING", "1").casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None

    command = [
        os.environ.get("HERMES_BIN", "hermes"),
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
        "--initial-status",
        "ready",
        "--json",
    ]
    cli_environment = os.environ.copy()
    cli_environment.setdefault("HERMES_KANBAN_HOME", str(Path.home() / ".hermes" / "shared-kanban"))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
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
    command = [
        os.environ.get("HERMES_BIN", "hermes"),
        "kanban",
        "comment",
        task_id,
        text,
        "--author",
        "ai-office-bff",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
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


def ask(*, department: str, config: str, query: str) -> dict:
    """Hermes 부서 Profile에 질의하고 텍스트만 돌려준다. 아무 상태도 바꾸지 않는다.

    `hermes -p <profile>`로 부른다. `hermes profile create`가 만들어주는 부서 이름
    Wrapper(`accounting-portfolio-department.bat`)는 쓰지 않는다 - 그 Wrapper는
    `~/.local/bin`에 생기고 PATH에 없을 수 있으며, 내용도 이 명령 한 줄이다.
    """
    if not ENABLE_AGENT_ASK:
        raise HTTPException(
            503,
            "Agent 질의는 인증·Tool Allowlist 연결 전까지 기본 비활성화 상태입니다.",
        )

    timeout = timeout_of(config)
    try:
        # ponytail: 요청마다 CLI 프로세스를 새로 띄운다(호출당 ~20s, 대화 이어짐 없음).
        # 상시 연결이 필요해지면 `hermes serve`(JSON-RPC/WebSocket, 기본 9119)로 바꾼다.
        # shell=False. 사용자 문자열이 셸을 거치지 않게 인자 리스트로만 넘긴다.
        # -Q: 배너·스피너·Tool Preview 없이 최종 답변만. 이게 없으면 ANSI 색코드와
        # 박스 문자가 그대로 화면까지 흘러간다.
        proc = subprocess.run(
        ["hermes", "-p", department, "chat", "-Q", "-q", query],
        check=False, capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        # Hermes Runtime은 PyPI 패키지가 아니라 별도 설치다(CLAUDE.md).
        raise HTTPException(503, f"Hermes CLI 없음: hermes -p {department}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{timeout}s 초과")

    if proc.returncode != 0:
        raise HTTPException(502, (proc.stderr or "").strip()[:500] or "agent failed")

    return {
        "department": department,
        # -Q에서 stdout은 최종 답변만 담는다. 부가 출력은 전부 stderr로 간다.
        "answer": proc.stdout.strip(),
        # 어느 Hermes Session이 이 문장을 만들었는지. 감사 추적에 필요하다.
        "session_id": session_id_of(proc.stderr or ""),
        # 화면이 이 값을 수치로 쓰지 못하게 계약에 박아둔다.
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


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
