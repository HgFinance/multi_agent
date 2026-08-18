"""FastAPI adapter for the shared Web/Discord CEO execution timeline."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

try:
    from .ceo_mirror import (
        CanonicalIngress,
        MirrorEvent,
        MirrorEventListResponse,
        MirrorIngressResponse,
        MirrorRequestConflict,
        MirrorStore,
        MirrorStoreUnavailable,
        build_default_mirror_store,
        execute_once,
        publish_mirror_event,
        stable_event_id,
    )
    from .ceo import CeoAsk
    from .ceo_schemas import CeoQueryAcceptedResponse
    from .discord_actor_map import resolve as resolve_discord_actor
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from ceo_mirror import (  # type: ignore[no-redef]
        CanonicalIngress,
        MirrorEvent,
        MirrorEventListResponse,
        MirrorIngressResponse,
        MirrorRequestConflict,
        MirrorStore,
        MirrorStoreUnavailable,
        build_default_mirror_store,
        execute_once,
        publish_mirror_event,
        stable_event_id,
    )
    from ceo import CeoAsk  # type: ignore[no-redef]
    from ceo_schemas import CeoQueryAcceptedResponse  # type: ignore[no-redef]
    from discord_actor_map import resolve as resolve_discord_actor  # type: ignore[no-redef]


router = APIRouter(prefix="/ui/ceo", tags=["ceo-mirror"])
MIRROR_STORE: MirrorStore = build_default_mirror_store()


_ANONYMOUS_ACTOR_IDS = frozenset({"anonymous", "web-user"})


def _ceo_query(request: CanonicalIngress) -> dict[str, Any]:
    # Lazy import avoids making apps.api.ceo import the mirror adapter and
    # keeps the existing CEO module's public contract unchanged.
    try:
        from . import ceo
    except ImportError:  # pragma: no cover
        import ceo  # type: ignore[no-redef]

    # `actor_id`는 `mirror_ask`가 `X-User-Id`를 우선으로 채운다. 헤더가 없어
    # 익명 fallback("web-user"/"anonymous")으로 채워진 경우에는 "이 root는 요청자를
    # 모른다"를 정확히 유지하기 위해 `owner_id`를 넘기지 않는다(개발 원칙 9).
    owner_id = (
        request.actor_id
        if request.actor_type == "user" and request.actor_id not in _ANONYMOUS_ACTOR_IDS
        else None
    )
    fund_id = request.fund_id

    # Discord로 들어온 요청의 `actor_id`는 **Discord 작성자 id**다 - 테스트 계정
    # UUID가 아니다. 그대로 두면 `requested_by=`에 Discord 숫자 id가 박혀 계정별
    # 이력 필터가 영영 비고, `fund_id`가 없어 Mandate 스냅샷도 안 붙는다
    # (2026-08-18 이전 Discord 질의의 상태).
    #
    # 매핑이 없으면 **아무것도 바꾸지 않는다.** 임의의 기본 계정으로 채우면
    # 사용자가 정하지 않은 한도가 판단 근거가 된다(개발 원칙 9).
    if request.source == "discord" and request.actor_type == "user":
        binding = resolve_discord_actor(request.actor_id)
        if binding is not None:
            owner_id = binding.user_id
            # 요청이 이미 fund를 실어 보냈으면 그쪽이 우선한다 - 매핑은 fund를
            # 모르는 경로를 위한 기본값이지, 명시된 값을 덮어쓰는 규칙이 아니다.
            fund_id = fund_id or binding.fund_id

    return ceo.ceo_query(
        CeoAsk(
            query=request.query,
            request_id=request.request_id,
            fund_id=fund_id,
        ),
        owner_id=owner_id,
    )


def _execute(request: CanonicalIngress):
    try:
        return execute_once(
            request,
            store=MIRROR_STORE,
            execute=lambda: _ceo_query(request),
        )
    except MirrorRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MirrorStoreUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="CEO mirror dedup store is unavailable"
        ) from exc


def _publish_workflow_projection(request_id: str) -> None:
    """Project sanitized Kanban states into the shared mirror stream.

    Kanban remains the execution source of truth. This projection never stores
    prompts, tool transcripts, or hidden reasoning and is idempotent by event
    id, so repeated SSE polling cannot duplicate events.
    """

    record = MIRROR_STORE.get_request(request_id)
    if record is None or not record.response:
        return
    task_id = str(record.response.get("task_id") or "").strip()
    if not task_id:
        return
    try:
        try:
            from .ceo_kanban_read import load_workflow
        except ImportError:  # pragma: no cover
            from ceo_kanban_read import load_workflow  # type: ignore[no-redef]

        workflow = load_workflow(task_id)
    except Exception:  # noqa: BLE001 - read projection must not break SSE.
        return

    request = record.request
    for node in workflow.nodes:
        status = str(node.status or "unknown").casefold()
        if node.is_qa:
            event_type = (
                "QA_RESULT"
                if status in {"done", "completed", "failed", "blocked"}
                else "QA_STARTED"
            )
            lane = "evaluation"
        elif node.role(root_task_id=workflow.root_task_id) == "synthesis":
            event_type = (
                "CEO_FINAL"
                if status in {"done", "completed"}
                else "CEO_SYNTHESIS_STARTED"
            )
            lane = "execution"
        else:
            event_type = {
                "done": "TASK_COMPLETED",
                "completed": "TASK_COMPLETED",
                "failed": "TASK_FAILED",
                "blocked": "TASK_FAILED",
            }.get(status, "TASK_STARTED")
            lane = "execution"
        parent_task_id = node.parents[0] if node.parents else None
        summary = (node.summary or node.error or node.block_reason or "").strip()
        publish_mirror_event(
            MIRROR_STORE,
            request=request,
            event_type=event_type,
            status=status,
            actor_id=node.profile or "hermes-kanban",
            actor_type="agent",
            lane=lane,
            task_id=node.task_id,
            parent_task_id=parent_task_id,
            summary=summary,
            payload={
                "department_id": node.profile,
                "role": node.role(root_task_id=workflow.root_task_id),
                "run_outcome": node.run_outcome,
            },
            event_id=stable_event_id(
                "workflow", request_id, node.task_id, event_type, status, summary
            ),
        )


@router.post(
    "/ask",
    status_code=202,
    operation_id="ceo_query_mirror_compat",
    response_model=CeoQueryAcceptedResponse,
)
def mirror_ask(
    request: CeoAsk,
    x_source_message_id: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict[str, Any]:
    """`POST /ui/ceo/ask`의 유일한 등록 지점 - `ceo.py`는 이 경로를 스스로 등록하지
    않는다(`ceo.ceo_query`는 순수 함수로 남는다).

    이 핸들러는 `ceo.CeoAsk`를 그대로 요청 모델로 쓴다 - 새 필드가 추가돼도
    자동으로 따라가고, 별도의 `fund_id` 없는 모델로 다시 조립할 여지가 없다.
    `_ceo_query`가 실제로 `ceo.ceo_query`를 부를 때도 같은 `CeoAsk`를 그대로
    새로 만들어 넘기므로(값만 옮겨 담는다), 두 모듈이 서로 다른 요청 스키마를
    독자적으로 들고 있다가 조용히 갈라지는 상태가 구조적으로 불가능하다.
    dedup(`_execute`/`MirrorStore`)과 Web/Discord 공용 event journal
    (`_publish_workflow_projection`)이 이 함수를 감싸는 layer다.

    `X-User-Id`(2026-08-14 추가)는 `fund_id`와 같은 이유로 여기서 빠져 있었다 -
    이 핸들러가 헤더를 받지 않으면 그 값이 실제로 요청을 실행하는 `_ceo_query`
    (그리고 `ceo.ceo_query`의 root body)까지 절대 도달할 수 없다. `actor_id`를
    그대로 재사용하는 이유는 dedup 키(`source`+`source_message_id`)가
    `actor_id`를 쓰지 않아 안전하기 때문이다 - `ceo_mirror.py`의
    `InMemoryMirrorStore._source_key`/`RedisMirrorStore._source_key` 참고.
    """

    canonical = CanonicalIngress(
        query=request.query,
        request_id=request.request_id,
        source="web",
        source_message_id=x_source_message_id or request.request_id,
        actor_id=x_user_id or x_actor_id or "web-user",
        actor_type="user",
        fund_id=request.fund_id,
    )
    execution = _execute(canonical)
    if execution.response is None:
        raise HTTPException(status_code=202, detail="request_in_progress")
    return execution.response


@router.post("/ingress", status_code=202, response_model=MirrorIngressResponse)
def mirror_ingress(request: CanonicalIngress) -> MirrorIngressResponse:
    """Canonical ingress for a human Web or Discord message."""

    execution = _execute(request)
    response = execution.response
    return MirrorIngressResponse(
        accepted=execution.accepted,
        duplicate=execution.duplicate,
        ignored=execution.ignored,
        reason=execution.reason,
        request_id=request.request_id,
        source=request.source,
        task_id=str(response.get("task_id"))
        if response and response.get("task_id")
        else None,
        execution_count=0 if execution.duplicate or execution.ignored else 1,
        ceo=response,
    )


@router.get("/events", response_model=MirrorEventListResponse)
def mirror_events(
    request_id: str = Query(min_length=8, max_length=128),
    after: str | None = Query(default=None, min_length=8, max_length=128),
) -> MirrorEventListResponse:
    _publish_workflow_projection(request_id)
    return MirrorEventListResponse(
        request_id=request_id, events=MIRROR_STORE.read_events(request_id, after)
    )


@router.get("/events/stream")
def mirror_event_stream(
    request_id: str = Query(min_length=8, max_length=128),
    after: str | None = Query(default=None, min_length=8, max_length=128),
) -> StreamingResponse:
    """Short-lived SSE; clients reconnect with the last event_id cursor."""

    def generate():
        cursor = after
        deadline = time.monotonic() + max(
            1.0, float(os.getenv("UI_MIRROR_SSE_SECONDS", "25"))
        )
        while time.monotonic() < deadline:
            _publish_workflow_projection(request_id)
            events = MIRROR_STORE.read_events(request_id, cursor)
            for event in events:
                cursor = event.event_id
                yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
            if not events:
                yield ": heartbeat\n\n"
                time.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/events", response_model=MirrorEvent, status_code=202)
def publish_event(event: MirrorEvent) -> MirrorEvent:
    """Sanitized adapter for supervisor, department, QA, and HR events."""

    MIRROR_STORE.publish_event(event)
    return event


__all__ = ["MIRROR_STORE", "router"]
