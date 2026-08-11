"""CEO Office query boundary for the operator UI."""

from __future__ import annotations

import os

try:
    from . import hermes_cli
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_cli  # type: ignore[no-redef]

from fastapi import APIRouter, HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department

from apps.api.ceo_hermes_client import ask_ceo

try:
    from kanban_board import BoardUnavailable, cards_for_root, progress_of
    from kanban_status_bridge import KANBAN_STATUS_BRIDGE
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.kanban_board import BoardUnavailable, cards_for_root, progress_of
    from apps.api.kanban_status_bridge import KANBAN_STATUS_BRIDGE


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])

# ▶ CEO 턴 timeout 을 Profile 값(30초)에서 떼어낸다 (2026-08-11 실측)
#   `agent.timeout_seconds` 는 **한 번 묻고 한 번 답하는** 조회 기준이다. 이 경로의
#   CEO 턴은 카드를 만들며 kanban 도구를 여러 번 부르는 다른 작업이라 90~180초가
#   걸렸고, 30초로는 통째로 잘렸다. 더 나쁜 것은 **잘려도 카드는 이미 만들어져
#   있었다는 것**이다 - 부서 4곳이 실제로 돌았는데 사용자에게는 실패로 보였다.
#   저장소의 부서 설정(영주님 소유)을 건드리지 않으려고 여기서 따로 준다.
CEO_TURN_TIMEOUT_SECONDS = int(os.getenv("CEO_TURN_TIMEOUT_SECONDS", "300"))

# ▶ CEO 를 어떻게 부를 것인가
#   `api`(기본) - `ceo-hermes` 의 OpenAI 호환 엔드포인트. AWS 배포 경로다.
#   `cli`        - 컨테이너 안 `hermes chat`. `hermes serve` 인증 설정이 선행
#                  조건이라 로컬에서는 아직 그 엔드포인트가 없어서 쓰는 길이다
#                  (HERMES_DOCKER_RUNBOOK 4-2절).
#   **조용히 넘어가지 않는다.** URL 이 없다고 알아서 CLI 로 떨어지면, 배포에서
#   URL 을 빠뜨렸을 때 "그래도 돌아가네"가 되어 경계가 무너진다. 스위치가 명시적일 때만 바꾼다.
CEO_TRANSPORT = os.getenv("CEO_TRANSPORT", "api").strip().lower()
CEO_CONFIG = "departments/00-ceo-office/hermes/config.yaml"
ENABLE_CEO_CLI = os.getenv("ENABLE_CEO_CLI", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


def _run_ceo_turn(query: str) -> dict[str, object]:
    """CEO 한 턴. transport 만 다르고 계약은 같다."""
    if CEO_TRANSPORT == "cli":
        return hermes_cli.ask(
            department=canonical_profile_for_department("ceo"),
            config=CEO_CONFIG,
            query=query,
            timeout=CEO_TURN_TIMEOUT_SECONDS,
            enabled=ENABLE_CEO_CLI,
        )
    return ask_ceo(query=query, timeout=CEO_TURN_TIMEOUT_SECONDS)

# Hermes 프로필 이름 -> 운영 Read Model 의 department_code.
# 둘이 어긋나는 이름이 생기면 화면에서 그 본부가 통째로 사라지므로 표로 둔다.
PROFILE_TO_DEPARTMENT_CODE: dict[str, str] = {}


@router.post("/ask", operation_id="ceo_query")
def ceo_query(req: hermes_cli.AgentAsk) -> dict[str, object]:
    """Send a non-binding natural-language query to the CEO Hermes Head."""

    task = hermes_cli.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=req.query,
        idempotency_key=req.request_id,
    )
    if not task or not task.get("task_id"):
        # The root task is the durable anchor for the closed-loop workflow.
        # Never call the CEO after this boundary failed: otherwise the user
        # receives an answer with no Kanban graph to supervise.
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )

    result = _run_ceo_turn(
        query=(
            "Closed-loop Kanban context: the durable CEO root task is "
            f"{task['task_id']}. Use this task as the parent for every "
            "dynamic child task and keep the workflow closed-loop.\n\n"
            # 이름을 줄여 쓰면 카드는 만들어지지만 아무도 집어가지 못한다
            # (2026-08-11 실측: `accounting-portfolio` 로 만든 카드가 그렇게 됐다).
            "Use these exact assignee names, verbatim:\n"
            "  research-department / quant-backtest-department / trading-department /\n"
            "  risk-management / accounting-portfolio-department / qa-department /\n"
            "  hr-department\n\n"
            f"Original user request:\n{req.query}"
        ),
    )
    return {
        "schema_version": "ceo.query-result.v1",
        "department": "ceo-agent",
        "binding": False,
        "task": task,
        "answer": result["answer"],
        "session_id": result.get("session_id"),
    }


@router.get("/ask/{root_task_id}", operation_id="ceo_query_progress")
def ceo_query_progress(root_task_id: str) -> dict[str, object]:
    """뿌리 카드에 매달린 본부 카드들의 진행·실패. 화면은 이걸 폴링한다.

    ▶ 왜 Kanban 대시보드 임베드로 충분하지 않은가
      임베드는 **보드 원본**을 보여준다. 거기서 회계 카드는 `done` 이었지만
      결과 본문이 비어 있었다 - "NAV 데이터가 없어 산출할 수 없습니다"를 완료로
      기록한 것이다. 원본만 보면 그건 성공으로 읽힌다. 이 경로는 같은 카드를
      "답이 됐는가" 기준으로 다시 판정해서 준다(`kanban_board` 의 fail-closed 규칙).
    """
    try:
        cards = cards_for_root(root_task_id)
    except BoardUnavailable as exc:
        # 못 읽은 것을 "카드 없음"으로 바꾸지 않는다. 빈 목록을 주면 화면이
        # "아무 일도 없었음"으로 읽는다.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not cards:
        raise HTTPException(status_code=404, detail="ceo_root_task_not_found")

    _publish_status(cards)
    return {
        "schema_version": "ceo.query-progress.v1",
        "root_task_id": root_task_id,
        # **본부가** 실제로 답을 준 게 하나라도 있는가. 뿌리 카드는 세지 않는다 -
        # CEO 가 자기 카드에 뭘 적었다고 본부 근거가 생기는 것은 아니다.
        "answer_grounded": any(
            c.outcome == "ANSWERED" and not c.is_root for c in cards
        ),
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
        **progress_of(cards),
    }


def _publish_status(cards) -> None:
    """카드 상태를 `agent.status.v1` 로 흘려 오피스 화면에도 보이게 한다.

    `kanban_status_bridge` 가 기다리던 입력이 이것이다 - 그 모듈은 정제된 이벤트를
    받도록 이미 설계돼 있었고 아무도 먹여 주지 않고 있었다. best-effort 다:
    여기서 실패해도 사용자 답변 경로를 막지 않는다.
    """
    outcome_to_kanban = {
        "QUEUED": "todo", "RUNNING": "running", "ANSWERED": "done",
        # 답을 못 낸 완료를 `done`(=IDLE)으로 흘리면 화면에서 성공으로 보인다.
        # 오피스 화면에서도 눈에 걸리도록 DEGRADED 로 올린다.
        "NO_ANSWER": "degraded", "BLOCKED": "blocked",
        "FAILED": "error", "STALE": "degraded", "NO_ASSIGNEE": "error",
    }
    for card in cards:
        try:
            KANBAN_STATUS_BRIDGE.publish_task_event({
                # 카드 결말이 바뀔 때만 새 이벤트가 되도록 결말을 키에 넣는다.
                "event_id": f"{card.task_id}:{card.outcome}",
                "status": outcome_to_kanban.get(card.outcome, "running"),
                "department_code": PROFILE_TO_DEPARTMENT_CODE.get(
                    card.assignee, card.assignee
                ),
                "agent_id": card.assignee,
                "task_id": card.task_id,
                "reason": card.summary or card.title,
            })
        except Exception:  # noqa: BLE001 - 알림은 답변 경로를 막지 않는다
            continue


__all__ = ["router", "CEO_TURN_TIMEOUT_SECONDS"]
