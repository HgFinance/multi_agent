"""Shared Web/Discord CEO ingress and event-mirror contracts.

This module deliberately does not import the CEO router or Hermes.  It owns
only the canonical request/event envelope and the deduplication boundary so
that Web, Discord, and future adapters cannot accidentally create separate
CEO executions for one user message.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

MirrorSource = Literal["web", "discord"]
MirrorActorType = Literal["user", "bot", "agent", "system"]
MirrorLane = Literal["execution", "evaluation"]

MIRROR_EVENT_TYPES = frozenset(
    {
        "USER_MESSAGE",
        "CEO_PLAN_CREATED",
        "TASK_CREATED",
        "TASK_ASSIGNED",
        "TASK_STARTED",
        "TASK_PROGRESS",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
        "TASK_COMPLETED",
        "TASK_FAILED",
        "RETRY_STARTED",
        "CEO_SYNTHESIS_STARTED",
        "CEO_FINAL",
        "QA_STARTED",
        "QA_RESULT",
        "HR_EVALUATION",
        "IMPROVEMENT_CANDIDATE",
        "REGRESSION_RESULT",
        "PROMOTION_RESULT",
    }
)


class CanonicalIngress(BaseModel):
    """One user-originated request shared by Web and Discord adapters."""

    query: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(
        default_factory=lambda: uuid4().hex, min_length=8, max_length=128
    )
    source: MirrorSource
    source_message_id: str | None = Field(default=None, max_length=512)
    actor_id: str = Field(default="anonymous", min_length=1, max_length=256)
    actor_type: MirrorActorType = "user"
    mirrored: bool = False
    # Web 어댑터가 `/ui/ceo/ask`로 실어 보내는 Mandate 조회 키. Discord 어댑터는
    # 아직 Fund 개념이 없어 안 보내므로 Optional이다 - 없으면 CEO Mandate
    # 스냅샷 없이 그대로 진행한다(개발 원칙 9, `ceo.ceo_query`와 동일한 정책).
    fund_id: str | None = None
    # Direct natural-language PAPER orders are authorized against one exact
    # Book. Keeping it in the canonical envelope prevents a replayed request
    # id from being rebound to a different account boundary.
    book_id: str | None = None
    # Discord 어댑터가 실어 보내는 원본 메시지 좌표(2026-08-18).
    #
    # 웹 요청에는 없다 - 그때는 BFF가 질의를 채널에 미러 게시하고 **그 게시물의**
    # 좌표를 쓴다(`apps/api/discord_mirror.py`). Discord에서 온 요청은 사용자가
    # 쓴 원본이 이미 채널에 있으므로 다시 게시하지 않고 이 값을 그대로 쓴다 -
    # 그래야 부서 진행·최종 답변이 **사용자가 쓴 그 메시지**에 붙는다.
    #
    # `source_message_id`와 겹쳐 보이지만 역할이 다르다: 그쪽은 dedup 키
    # (`source`+`source_message_id`)의 재료이고, 이쪽은 Discord 발송 좌표다.
    # 같은 값을 쓰더라도 한 필드가 두 계약을 겸하면 한쪽 형식을 바꿀 때
    # 다른 쪽이 조용히 깨진다.
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
    discord_guild_id: str | None = None
    discord_thread_id: str | None = None
    # Explicit bounded context for Discord follow-ups referring to the thread
    # starter. This is request identity, not authority or delivery metadata.
    previous_question_context: str | None = Field(default=None, max_length=3200)
    previous_question_context_source_message_id: str | None = Field(
        default=None, max_length=512
    )

    @model_validator(mode="after")
    def default_source_message_id(self) -> CanonicalIngress:
        if not self.source_message_id:
            self.source_message_id = self.request_id
        return self


class MirrorEvent(BaseModel):
    """Browser/Discord-safe event. Hidden chain-of-thought is not a field."""

    schema_version: Literal["ui.ceo-mirror-event.v1"] = "ui.ceo-mirror-event.v1"
    event_id: str = Field(min_length=8, max_length=128)
    request_id: str = Field(min_length=8, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    parent_task_id: str | None = Field(default=None, max_length=128)
    source: MirrorSource
    source_message_id: str = Field(min_length=1, max_length=512)
    actor_id: str = Field(min_length=1, max_length=256)
    actor_type: MirrorActorType
    lane: MirrorLane
    event_type: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def validate_event_type(self) -> MirrorEvent:
        if self.event_type not in MIRROR_EVENT_TYPES:
            raise ValueError(f"unsupported mirror event_type: {self.event_type}")
        return self


class MirrorEventListResponse(BaseModel):
    schema_version: Literal["ui.ceo-mirror-events.v1"] = "ui.ceo-mirror-events.v1"
    request_id: str
    events: list[MirrorEvent]
    next_cursor: str | None = None


class MirrorIngressResponse(BaseModel):
    schema_version: Literal["ui.ceo-mirror-ingress.v1"] = "ui.ceo-mirror-ingress.v1"
    accepted: bool
    duplicate: bool = False
    ignored: bool = False
    reason: str | None = None
    request_id: str
    source: MirrorSource
    task_id: str | None = None
    execution_count: int = 0
    ceo: dict[str, Any] | None = None


@dataclass(frozen=True)
class MirrorRequestRecord:
    request: CanonicalIngress
    response: dict[str, Any] | None = None


class MirrorStore(Protocol):
    """Small store interface; Redis is production, memory is test fallback."""

    def claim_request(
        self, request: CanonicalIngress
    ) -> tuple[MirrorRequestRecord, bool]: ...

    def get_request(self, request_id: str) -> MirrorRequestRecord | None: ...

    def list_request_ids(self, *, limit: int = 1000) -> list[str]: ...

    def get_projection_state(self, request_id: str) -> str | None: ...

    def save_projection_state(self, request_id: str, fingerprint: str) -> None: ...

    def save_response(self, request_id: str, response: dict[str, Any]) -> None: ...

    def release_request(self, request: CanonicalIngress) -> bool: ...

    def publish_event(self, event: MirrorEvent) -> bool: ...

    def read_events(
        self, request_id: str, after: str | None = None
    ) -> list[MirrorEvent]: ...


class MirrorRequestConflict(ValueError):
    """A source message or request id is bound to a different request."""


class MirrorStoreUnavailable(RuntimeError):
    """The durable deduplication store cannot safely claim a request."""


def _canonical_request_identity(request: CanonicalIngress) -> tuple[object, ...]:
    """Return every field that fixes one ingress's content and authority."""

    return (
        request.query,
        request.source,
        request.source_message_id,
        request.actor_id,
        request.actor_type,
        request.fund_id,
        request.book_id,
        request.discord_channel_id,
        request.discord_message_id,
        request.discord_guild_id,
        request.discord_thread_id,
        request.previous_question_context,
        request.previous_question_context_source_message_id,
        request.mirrored,
    )


def _same_canonical_request(
    left: CanonicalIngress, right: CanonicalIngress
) -> bool:
    return _canonical_request_identity(left) == _canonical_request_identity(right)


class InMemoryMirrorStore:
    """Deterministic store used by tests and as a safe local fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, MirrorRequestRecord] = {}
        self._source_index: dict[str, str] = {}
        self._events: dict[str, MirrorEvent] = {}
        self._event_order: list[str] = []
        self._projection_state: dict[str, str] = {}

    @staticmethod
    def _source_key(request: CanonicalIngress) -> str:
        return f"{request.source}:{request.source_message_id}"

    def claim_request(
        self, request: CanonicalIngress
    ) -> tuple[MirrorRequestRecord, bool]:
        with self._lock:
            source_key = self._source_key(request)
            existing_request_id = self._source_index.get(source_key)
            if existing_request_id and existing_request_id != request.request_id:
                raise MirrorRequestConflict(
                    "source_message_id is already bound to another request_id"
                )
            existing = self._requests.get(request.request_id)
            if existing is not None:
                if not _same_canonical_request(existing.request, request):
                    raise MirrorRequestConflict(
                        "request_id is already bound to a different canonical request"
                    )
                return existing, False
            record = MirrorRequestRecord(request=request)
            self._requests[request.request_id] = record
            self._source_index[source_key] = request.request_id
            return record, True

    def get_request(self, request_id: str) -> MirrorRequestRecord | None:
        with self._lock:
            return self._requests.get(request_id)

    def list_request_ids(self, *, limit: int = 1000) -> list[str]:
        with self._lock:
            bounded_limit = max(1, min(int(limit), 10_000))
            return list(self._requests)[-bounded_limit:]

    def get_projection_state(self, request_id: str) -> str | None:
        with self._lock:
            return self._projection_state.get(request_id)

    def save_projection_state(self, request_id: str, fingerprint: str) -> None:
        with self._lock:
            if request_id in self._requests:
                self._projection_state[request_id] = str(fingerprint)

    def save_response(self, request_id: str, response: dict[str, Any]) -> None:
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                return
            self._requests[request_id] = MirrorRequestRecord(
                record.request, dict(response)
            )

    def release_request(self, request: CanonicalIngress) -> bool:
        """Release only the same unfinished claim after execution raised."""

        with self._lock:
            record = self._requests.get(request.request_id)
            if (
                record is None
                or record.response is not None
                or not _same_canonical_request(record.request, request)
            ):
                return False
            source_key = self._source_key(request)
            if self._source_index.get(source_key) == request.request_id:
                self._source_index.pop(source_key, None)
            self._requests.pop(request.request_id, None)
            return True

    def publish_event(self, event: MirrorEvent) -> bool:
        with self._lock:
            if event.event_id in self._events:
                return False
            self._events[event.event_id] = event
            self._event_order.append(event.event_id)
            return True

    def read_events(
        self, request_id: str, after: str | None = None
    ) -> list[MirrorEvent]:
        with self._lock:
            events = [
                self._events[event_id]
                for event_id in self._event_order
                if self._events[event_id].request_id == request_id
            ]
        if after:
            for index, event in enumerate(events):
                if event.event_id == after:
                    return events[index + 1 :]
        return events


def _positive_float_env(name: str, default: float) -> float:
    """유한한 양수만 통과시킨다 - 0/음수/오타는 무한 대기로 되돌아가지 않는다."""

    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _redis_connect_timeout() -> float:
    return _positive_float_env("UI_MIRROR_REDIS_CONNECT_TIMEOUT_SECONDS", 2.0)


def _redis_socket_timeout() -> float:
    return _positive_float_env("UI_MIRROR_REDIS_SOCKET_TIMEOUT_SECONDS", 3.0)


class RedisMirrorStore:
    """Redis-backed request/event store using the existing compose Redis."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "hf:ui-ceo-mirror:v1",
        ttl_seconds: int = 604800,
    ) -> None:
        import redis

        # **소켓 타임아웃 없이 Redis를 잡으면 요청이 영구히 pending 된다.**
        # redis-py 기본값은 `socket_timeout=None`(무한 대기)이다. AWS에서
        # `redis` 컨테이너가 재시작·OOM kill 되거나 conntrack 항목이 만료되면
        # 풀에 남아 있던 소켓은 닫히지 않고 blackhole 이 되고, 다음 명령은
        # `recv()`에서 영원히 멈춘다. 이 store 를 쓰는 `POST /ui/ceo/ask` 는
        # 동기 `def` 핸들러라 그 스레드가 anyio 스레드풀(기본 40개)에서 그대로
        # 사라지고, 40번 반복되면 BFF의 **모든** 엔드포인트가 응답도 타임아웃도
        # 없이 대기한다. 예외가 아니라 hang 이므로 아래 `ResilientMirrorStore`
        # 의 fallback 도 발동하지 못한다 - 반드시 유한한 타임아웃이 필요하다.
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=_redis_connect_timeout(),
            socket_timeout=_redis_socket_timeout(),
            # 죽은 소켓을 명령 전에 걸러내고, 끊긴 연결 하나로 요청이 실패하지
            # 않게 한다. 이 store 에는 blocking 명령(BLPOP/XREAD BLOCK)이 없어
            # socket_timeout 이 정상 대기를 끊을 위험이 없다.
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        self.stream = stream
        self.ttl_seconds = ttl_seconds
        self.request_prefix = "hf:ui-ceo-mirror:request:"
        self.source_prefix = "hf:ui-ceo-mirror:source:"
        self.event_prefix = "hf:ui-ceo-mirror:event:"
        self.projection_state_prefix = "hf:ui-ceo-mirror:projection-state:"
        # SCAN is incremental. Keep the cursor on the long-lived worker store
        # so a large request backlog is visited over successive cycles instead
        # of returning the same first keys forever.
        self._request_scan_cursor = 0

    @staticmethod
    def _source_key(request: CanonicalIngress) -> str:
        return f"{request.source}:{request.source_message_id}"

    def claim_request(
        self, request: CanonicalIngress
    ) -> tuple[MirrorRequestRecord, bool]:
        source_key = self._source_key(request)
        existing_source = self.client.get(self.source_prefix + source_key)
        if existing_source and existing_source != request.request_id:
            raise MirrorRequestConflict(
                "source_message_id is already bound to another request_id"
            )
        request_key = self.request_prefix + request.request_id
        existing = self.client.get(request_key)
        if existing:
            payload = json.loads(existing)
            stored_request = CanonicalIngress.model_validate(payload["request"])
            if not _same_canonical_request(stored_request, request):
                raise MirrorRequestConflict(
                    "request_id is already bound to a different canonical request"
                )
            return MirrorRequestRecord(
                request=stored_request,
                response=payload.get("response"),
            ), False
        payload = {"request": request.model_dump(mode="json"), "response": None}
        inserted = bool(
            self.client.set(
                request_key, json.dumps(payload), nx=True, ex=self.ttl_seconds
            )
        )
        if not inserted:
            existing = self.client.get(request_key)
            if not existing:
                raise RuntimeError("request deduplication record disappeared")
            stored = json.loads(existing)
            stored_request = CanonicalIngress.model_validate(stored["request"])
            if not _same_canonical_request(stored_request, request):
                raise MirrorRequestConflict(
                    "request_id is already bound to a different canonical request"
                )
            return MirrorRequestRecord(
                request=stored_request,
                response=stored.get("response"),
            ), False
        self.client.set(
            self.source_prefix + source_key,
            request.request_id,
            nx=True,
            ex=self.ttl_seconds,
        )
        return MirrorRequestRecord(request=request), True

    def get_request(self, request_id: str) -> MirrorRequestRecord | None:
        raw = self.client.get(self.request_prefix + request_id)
        if not raw:
            return None
        payload = json.loads(raw)
        return MirrorRequestRecord(
            request=CanonicalIngress.model_validate(payload["request"]),
            response=payload.get("response"),
        )

    def list_request_ids(self, *, limit: int = 1000) -> list[str]:
        """Return a bounded request-id page without blocking Redis.

        The projection worker is a reconciler, not a second source of truth. A
        Redis SCAN page lets it discover requests that never opened an SSE
        connection while preserving the existing request TTL and avoiding
        KEYS on the production Redis instance.
        """

        bounded_limit = max(1, min(int(limit), 10_000))
        prefix_length = len(self.request_prefix)
        request_ids: list[str] = []
        cursor = self._request_scan_cursor
        while True:
            cursor, keys = self.client.scan(
                cursor=cursor,
                match=f"{self.request_prefix}*",
                count=min(max(bounded_limit * 2, 50), 500),
            )
            for key in keys:
                request_id = str(key)[prefix_length:]
                if request_id:
                    request_ids.append(request_id)
                if len(request_ids) >= bounded_limit:
                    break
            if len(request_ids) >= bounded_limit or cursor == 0:
                break
        self._request_scan_cursor = int(cursor)
        return request_ids

    def get_projection_state(self, request_id: str) -> str | None:
        value = self.client.get(self.projection_state_prefix + request_id)
        return str(value) if value else None

    def save_projection_state(self, request_id: str, fingerprint: str) -> None:
        self.client.set(
            self.projection_state_prefix + request_id,
            str(fingerprint),
            ex=self.ttl_seconds,
        )

    def save_response(self, request_id: str, response: dict[str, Any]) -> None:
        key = self.request_prefix + request_id
        raw = self.client.get(key)
        if not raw:
            return
        payload = json.loads(raw)
        payload["response"] = response
        self.client.set(key, json.dumps(payload), ex=self.ttl_seconds)

    def release_request(self, request: CanonicalIngress) -> bool:
        """Atomically release one exact unfinished canonical ingress claim."""

        request_key = self.request_prefix + request.request_id
        raw = self.client.get(request_key)
        if not raw:
            return False
        payload = json.loads(raw)
        stored = CanonicalIngress.model_validate(payload["request"])
        if payload.get("response") is not None or not _same_canonical_request(
            stored, request
        ):
            return False
        source_key = self.source_prefix + self._source_key(request)
        released = self.client.eval(
            """
            if redis.call('GET', KEYS[1]) == ARGV[1]
               and redis.call('GET', KEYS[2]) == ARGV[2] then
              redis.call('DEL', KEYS[1])
              redis.call('DEL', KEYS[2])
              return 1
            end
            return 0
            """,
            2,
            request_key,
            source_key,
            raw,
            request.request_id,
        )
        return bool(released)

    def publish_event(self, event: MirrorEvent) -> bool:
        key = self.event_prefix + event.event_id
        payload = event.model_dump(mode="json")
        inserted = bool(
            self.client.set(key, json.dumps(payload), nx=True, ex=self.ttl_seconds)
        )
        if not inserted:
            return False
        self.client.xadd(
            self.stream,
            {
                "event_id": event.event_id,
                "request_id": event.request_id,
                "payload": json.dumps(payload),
            },
            maxlen=10000,
            approximate=True,
        )
        return True

    def read_events(
        self, request_id: str, after: str | None = None
    ) -> list[MirrorEvent]:
        values: list[MirrorEvent] = []
        for _stream_id, fields in self.client.xrange(self.stream, min="-", max="+"):
            payload = fields.get("payload")
            if not payload:
                continue
            event = MirrorEvent.model_validate(json.loads(payload))
            if event.request_id == request_id:
                values.append(event)
        if after:
            for index, event in enumerate(values):
                if event.event_id == after:
                    return values[index + 1 :]
        return values


class LockedRedisMirrorStore(RedisMirrorStore):
    """Serialize request claims across BFF workers using the existing Redis."""

    def claim_request(
        self, request: CanonicalIngress
    ) -> tuple[MirrorRequestRecord, bool]:
        with self.client.lock(
            "hf:ui-ceo-mirror:claim-lock",
            timeout=10,
            blocking_timeout=10,
        ):
            return super().claim_request(request)


class ResilientMirrorStore:
    """Prefer Redis in AWS, but keep the BFF alive when Redis is unavailable."""

    def __init__(
        self, primary: MirrorStore, fallback: InMemoryMirrorStore | None = None
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except MirrorRequestConflict:
            raise
        except Exception:  # noqa: BLE001 - BFF must not turn Redis outage into a CEO retry storm.
            if self.fallback is not None:
                return getattr(self.fallback, method)(*args, **kwargs)
            raise MirrorStoreUnavailable(
                f"mirror store unavailable during {method}"
            ) from None

    def claim_request(
        self, request: CanonicalIngress
    ) -> tuple[MirrorRequestRecord, bool]:
        return self._call("claim_request", request)

    def get_request(self, request_id: str) -> MirrorRequestRecord | None:
        return self._call("get_request", request_id)

    def list_request_ids(self, *, limit: int = 1000) -> list[str]:
        return self._call("list_request_ids", limit=limit)

    def get_projection_state(self, request_id: str) -> str | None:
        return self._call("get_projection_state", request_id)

    def save_projection_state(self, request_id: str, fingerprint: str) -> None:
        self._call("save_projection_state", request_id, fingerprint)

    def save_response(self, request_id: str, response: dict[str, Any]) -> None:
        self._call("save_response", request_id, response)

    def release_request(self, request: CanonicalIngress) -> bool:
        return bool(self._call("release_request", request))

    def publish_event(self, event: MirrorEvent) -> bool:
        return bool(self._call("publish_event", event))

    def read_events(
        self, request_id: str, after: str | None = None
    ) -> list[MirrorEvent]:
        return self._call("read_events", request_id, after)


def build_default_mirror_store() -> ResilientMirrorStore:
    url = os.getenv("UI_MIRROR_REDIS_URL") or os.getenv("REDIS_URL")
    if url:
        try:
            return ResilientMirrorStore(
                LockedRedisMirrorStore(
                    url,
                    stream=os.getenv("UI_MIRROR_STREAM", "hf:ui-ceo-mirror:v1"),
                    ttl_seconds=max(
                        60, int(os.getenv("UI_MIRROR_DEDUPE_TTL_SECONDS", "604800"))
                    ),
                ),
                fallback=None,
            )
        except (TypeError, ValueError):
            # Invalid local configuration is treated as an unavailable mirror,
            # while the CEO Kanban boundary remains usable.
            return ResilientMirrorStore(InMemoryMirrorStore())
        except Exception:  # noqa: BLE001 - import/config failure falls back to local memory.
            return ResilientMirrorStore(InMemoryMirrorStore())
    return ResilientMirrorStore(InMemoryMirrorStore())


def stable_event_id(*parts: object) -> str:
    digest = hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"evt-{digest[:40]}"


def publish_mirror_event(
    store: MirrorStore,
    *,
    request: CanonicalIngress,
    event_type: str,
    status: str,
    actor_id: str,
    actor_type: MirrorActorType,
    lane: MirrorLane,
    task_id: str | None = None,
    parent_task_id: str | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> MirrorEvent:
    event = MirrorEvent(
        event_id=event_id or f"evt-{uuid4().hex}",
        request_id=request.request_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        source=request.source,
        source_message_id=str(request.source_message_id),
        actor_id=actor_id,
        actor_type=actor_type,
        lane=lane,
        event_type=event_type,
        status=status,
        summary=summary[:4000],
        payload=payload or {},
    )
    store.publish_event(event)
    return event


@dataclass(frozen=True)
class MirrorExecution:
    accepted: bool
    duplicate: bool
    ignored: bool
    reason: str | None
    response: dict[str, Any] | None


def execute_once(
    request: CanonicalIngress,
    *,
    store: MirrorStore,
    execute: Callable[[], dict[str, Any]],
) -> MirrorExecution:
    """Claim once, execute once, and persist the accepted response."""

    if request.actor_type == "bot" or request.mirrored:
        return MirrorExecution(False, False, True, "bot_mirror_ignored", None)

    record, created = store.claim_request(request)
    if not created and record.response is not None:
        return MirrorExecution(
            True, True, False, "request_already_executed", record.response
        )

    if not created:
        # Another BFF worker claimed the request. Never run the CEO a second
        # time; wait briefly for the first worker to persist its accepted
        # response, then let the caller poll by request_id if it is still
        # running. This is intentionally bounded so a dead worker cannot hold
        # an HTTP request forever.
        deadline = time.monotonic() + max(
            0.1, float(os.getenv("UI_MIRROR_DEDUPE_WAIT_SECONDS", "3"))
        )
        while time.monotonic() < deadline:
            time.sleep(0.05)
            record = store.get_request(request.request_id)
            if record is not None and record.response is not None:
                return MirrorExecution(
                    True, True, False, "request_already_executed", record.response
                )
        return MirrorExecution(True, True, False, "request_in_progress", None)

    if created:
        publish_mirror_event(
            store,
            request=request,
            event_type="USER_MESSAGE",
            status="accepted",
            actor_id=request.actor_id,
            actor_type="user",
            lane="execution",
            summary=request.query,
            payload={"source": request.source},
            event_id=stable_event_id(
                "user-message",
                request.request_id,
                request.source,
                request.source_message_id,
            ),
        )

    try:
        response = execute()
    except Exception:
        # The lower CEO/order boundaries use this same request ID and are
        # independently idempotent.  Keeping an empty mirror claim forever is
        # therefore less safe than releasing only the exact unfinished claim:
        # it turns a correctable validation/configuration outage into a silent
        # permanent drop.
        store.release_request(request)
        raise
    store.save_response(request.request_id, response)
    task_id = str(response.get("task_id") or "") or None
    if created and task_id:
        publish_mirror_event(
            store,
            request=request,
            event_type="CEO_PLAN_CREATED",
            status="accepted",
            actor_id="ceo-agent",
            actor_type="agent",
            lane="execution",
            task_id=task_id,
            summary="CEO root Kanban workflow가 생성되었습니다.",
            payload={
                "workflow_scope": "root_task",
                "planning": response.get("planning"),
            },
            event_id=stable_event_id("ceo-plan", request.request_id, task_id),
        )
        publish_mirror_event(
            store,
            request=request,
            event_type="TASK_CREATED",
            status="queued",
            actor_id="ceo-agent",
            actor_type="agent",
            lane="execution",
            task_id=task_id,
            summary="CEO root task가 Kanban에 등록되었습니다.",
            payload={"department_id": "ceo-agent", "role": "root"},
            event_id=stable_event_id("task-created", request.request_id, task_id),
        )
    return MirrorExecution(True, not created, False, None, response)


__all__ = [
    "MIRROR_EVENT_TYPES",
    "CanonicalIngress",
    "InMemoryMirrorStore",
    "MirrorEvent",
    "MirrorEventListResponse",
    "MirrorIngressResponse",
    "MirrorRequestConflict",
    "MirrorRequestRecord",
    "MirrorStore",
    "MirrorStoreUnavailable",
    "ResilientMirrorStore",
    "build_default_mirror_store",
    "execute_once",
    "publish_mirror_event",
    "stable_event_id",
]
