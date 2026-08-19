"""FastAPI adapter for the shared Web/Discord CEO execution timeline."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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
    from .current_user import (
        authorized_trading_books,
        current_user,
        require_fund_membership,
    )
    from .discord_ingress_auth import request_is_authorized as discord_ingress_authorized
    from .discord_actor_map import resolve as resolve_discord_actor
    from .discord_mirror import post_question
    from .governance_client import fetch_fund_id_by_user
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
    from current_user import (  # type: ignore[no-redef]
        authorized_trading_books,
        current_user,
        require_fund_membership,
    )
    from discord_ingress_auth import (  # type: ignore[no-redef]
        request_is_authorized as discord_ingress_authorized,
    )
    from discord_actor_map import resolve as resolve_discord_actor  # type: ignore[no-redef]
    from discord_mirror import post_question  # type: ignore[no-redef]
    from governance_client import fetch_fund_id_by_user  # type: ignore[no-redef]


router = APIRouter(prefix="/ui/ceo", tags=["ceo-mirror"])
MIRROR_STORE: MirrorStore = build_default_mirror_store()


_ANONYMOUS_ACTOR_IDS = frozenset({"anonymous", "web-user"})


def _resolved_owner(owner_id: object) -> str | None:
    """Return a dependency-resolved subject, not FastAPI's direct-call sentinel."""

    return owner_id if isinstance(owner_id, str) and owner_id.strip() else None


def _require_mirror_request_owner(
    request_id: str, owner_id: object
) -> Any:
    """Authorize a mirror journal by the immutable ingress actor and Fund."""

    owner = _resolved_owner(owner_id)
    record = MIRROR_STORE.get_request(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="ceo_mirror_request_not_found")
    if owner is None:
        return record
    if record.request.actor_type != "user" or record.request.actor_id != owner:
        raise HTTPException(status_code=403, detail="ceo_mirror_request_forbidden")
    if record.request.fund_id is not None:
        require_fund_membership(owner, record.request.fund_id)
    return record


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
    book_id = request.book_id

    # Discord로 들어온 요청의 `actor_id`는 **Discord 작성자 id**다 - 테스트 계정
    # UUID가 아니다. 그대로 두면 `requested_by=`에 Discord 숫자 id가 박혀 계정별
    # 이력 필터가 영영 비고, `fund_id`가 없어 Mandate 스냅샷도 안 붙는다
    # (2026-08-18 이전 Discord 질의의 상태).
    #
    # 매핑이 없으면 **아무것도 바꾸지 않는다.** 임의의 기본 계정으로 채우면
    # 사용자가 정하지 않은 한도가 판단 근거가 된다(개발 원칙 9).
    if request.source == "discord" and request.actor_type == "user":
        # The private gateway authenticates the transport, but fund/book
        # fields remain caller-controlled JSON. Derive Discord account scope
        # only from the server-owned actor binding and database grants.
        owner_id = None
        fund_id = None
        book_id = None
        binding = resolve_discord_actor(request.actor_id)
        if binding is not None:
            owner_id = binding.user_id
            # 요청이 이미 fund를 실어 보냈으면 그쪽이 우선한다 - 매핑은 fund를
            # 모르는 경로를 위한 기본값이지, 명시된 값을 덮어쓰는 규칙이 아니다.
            fund_id = binding.fund_id

    # `user_id -> fund_id` 역참조(2026-08-18). `governance.fund_memberships`가
    # 채워지면서 서버가 직접 풀 수 있게 됐다 - 그 전까지는 프론트엔드가 계정과
    # fund를 쌍으로 하드코딩해 보내는 것 말고 방법이 없었다.
    #
    # 명시된 `fund_id`가 있으면 조회하지 않는다: 호출자가 지정한 Fund를 서버
    # 추론으로 덮으면, 화면이 보고 있는 Fund와 판단 근거가 달라진다.
    if owner_id and not fund_id:
        fund_id = fetch_fund_id_by_user(owner_id)

    if request.source == "discord" and owner_id and fund_id and not book_id:
        # Discord carries a stable author id, not a browser-selected Book.  A
        # unique ACTIVE trading Book for the mapped Fund is deterministic; an
        # absent or ambiguous choice remains unset so the order admission
        # boundary returns clarification instead of guessing an account.
        matching_books = [
            row
            for row in authorized_trading_books(owner_id)
            if str(row.get("fund_id") or "") == str(fund_id)
        ]
        if len(matching_books) == 1:
            book_id = str(matching_books[0]["book_id"])

    # Discord 발송 좌표. `deliver()`가 channel_id와 message_id를 **둘 다** 요구한다.
    #
    # 출처에 따라 좌표의 출처가 다르다:
    #   - Discord: 사용자가 쓴 원본이 이미 채널에 있다. **다시 게시하지 않는다** -
    #     그러면 원본 옆에 봇이 같은 내용을 한 번 더 올린다. 어댑터가 준 좌표를
    #     그대로 쓰면 답변이 사용자가 쓴 그 메시지에 붙는다.
    #   - Web: 채널에 아무것도 없다. 질의를 미러 게시하고 **그 게시물의** 좌표를 쓴다.
    #
    # 게시 판단이 여기 있는 이유는 `ceo.ceo_query`가 출처를 모르기 때문이다 -
    # 거기서 무조건 게시하면 Discord 요청까지 중복 게시되고, 그 함수를 부르는
    # 단위 테스트가 전부 실제 채널로 나간다(2026-08-18 실측).
    if request.source == "discord":
        discord_channel_id = request.discord_channel_id
        discord_message_id = request.discord_message_id
        discord_guild_id = request.discord_guild_id
        # HgFinance gateway owns request-thread creation before BFF ingress.
        # Preserve the exact thread correlation supplied by that gateway.
        discord_thread_id = request.discord_thread_id
    else:
        mirror = post_question(request.query, asked_by=owner_id)
        discord_channel_id = mirror.channel_id if mirror else None
        discord_message_id = mirror.message_id if mirror else None
        discord_guild_id = mirror.guild_id if mirror else None
        discord_thread_id = mirror.thread_id if mirror else None

    return ceo.ceo_query(
        CeoAsk(
            query=request.query,
            request_id=request.request_id,
            fund_id=fund_id,
            book_id=book_id,
        ),
        owner_id=owner_id,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
        discord_guild_id=discord_guild_id,
        discord_thread_id=discord_thread_id,
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
    owner_id: str | None = Depends(current_user),
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

    `owner_id`는 중앙 인증 경계가 검증한 Supabase JWT `sub`다. 로컬/test fixture
    모드에서만 같은 의존성이 `X-User-Id`를 읽는다. `actor_id`를 그대로 재사용하는
    이유는 dedup 키(`source`+`source_message_id`)가
    `actor_id`를 쓰지 않아 안전하기 때문이다 - `ceo_mirror.py`의
    `InMemoryMirrorStore._source_key`/`RedisMirrorStore._source_key` 참고.
    """

    require_fund_membership(owner_id, request.fund_id)
    canonical = CanonicalIngress(
        query=request.query,
        request_id=request.request_id,
        source="web",
        source_message_id=x_source_message_id or request.request_id,
        actor_id=owner_id or x_actor_id or "web-user",
        actor_type="user",
        fund_id=request.fund_id,
        book_id=request.book_id,
    )
    execution = _execute(canonical)
    if execution.response is None:
        raise HTTPException(status_code=202, detail="request_in_progress")
    return execution.response


@router.post("/ingress", status_code=202, response_model=MirrorIngressResponse)
def mirror_ingress(
    request: CanonicalIngress,
    http_request: Request,
    owner_id: str | None = Depends(current_user),
) -> MirrorIngressResponse:
    """Canonical ingress for a human Web or Discord message."""

    internal_discord = discord_ingress_authorized(http_request)
    if request.source == "discord" and not internal_discord:
        raise HTTPException(status_code=401, detail="discord_ingress_authentication_required")
    if request.source != "discord" and internal_discord:
        raise HTTPException(status_code=403, detail="discord_ingress_source_forbidden")
    owner = _resolved_owner(owner_id)
    canonical = request
    if owner is not None:
        if (
            request.actor_id not in _ANONYMOUS_ACTOR_IDS
            and request.actor_id != owner
        ):
            raise HTTPException(status_code=403, detail="ceo_mirror_actor_mismatch")
        if request.fund_id is not None:
            require_fund_membership(owner, request.fund_id)
        canonical = request.model_copy(
            update={"actor_id": owner, "actor_type": "user"}
        )
    execution = _execute(canonical)
    response = execution.response
    return MirrorIngressResponse(
        accepted=execution.accepted,
        duplicate=execution.duplicate,
        ignored=execution.ignored,
        reason=execution.reason,
        request_id=canonical.request_id,
        source=canonical.source,
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
    owner_id: str | None = Depends(current_user),
) -> MirrorEventListResponse:
    _require_mirror_request_owner(request_id, owner_id)
    _publish_workflow_projection(request_id)
    return MirrorEventListResponse(
        request_id=request_id, events=MIRROR_STORE.read_events(request_id, after)
    )


@router.get("/events/stream")
def mirror_event_stream(
    request_id: str = Query(min_length=8, max_length=128),
    after: str | None = Query(default=None, min_length=8, max_length=128),
    owner_id: str | None = Depends(current_user),
) -> StreamingResponse:
    """Short-lived SSE; clients reconnect with the last event_id cursor."""

    _require_mirror_request_owner(request_id, owner_id)

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
def publish_event(
    event: MirrorEvent,
    owner_id: str | None = Depends(current_user),
) -> MirrorEvent:
    """Sanitized adapter for supervisor, department, QA, and HR events."""

    if _resolved_owner(owner_id) is not None:
        # Browser users cannot forge agent/supervisor timeline records. Runtime
        # producers publish through the internal store/service boundary.
        raise HTTPException(status_code=403, detail="ceo_mirror_event_publish_forbidden")
    MIRROR_STORE.publish_event(event)
    return event


__all__ = ["MIRROR_STORE", "router"]
