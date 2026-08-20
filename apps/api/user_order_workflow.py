"""Durable authority record for CEO -> Trading Hermes -> PAPER OMS.

The record is created while the browser's authenticated subject is present.
Hermes receives only ``order_request_id`` and a SHA-256 digest.  A trusted
orchestrator later reloads this row, re-checks current Fund/Book membership,
and mints the existing 20-second payload-bound Trading proof just in time.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, register_uuid

# The workflow repository deliberately uses ``uuid.UUID`` values for every
# authority identifier.  psycopg2 does not adapt UUID objects unless its UUID
# adapter is registered; without this, the production Postgres path fails on
# the very first ``admit`` even though in-memory tests pass.
register_uuid()


PAPER_ORDER_NORMALIZER_VERSION = "user-order-language.v1"
PAPER_ORDER_MODE = "PAPER"
ORDER_REQUEST_STATES = frozenset(
    {
        "RECEIVED",
        "KANBAN_QUEUED",
        "INTERPRETING",
        "INTERPRETED",
        "CLARIFICATION_REQUIRED",
        "NOT_ORDER",
        "REJECTED",
        "SUBMITTED",
        "IN_PROGRESS",
        "ACCOUNTING_PENDING",
        "COMPLETED",
        "FAILED",
        "UNKNOWN",
    }
)


class UserOrderWorkflowError(RuntimeError):
    """Base error for the durable pre-directive workflow."""


class UserOrderWorkflowUnavailable(UserOrderWorkflowError):
    """The operational request store is not safely available."""


class UserOrderRequestConflict(UserOrderWorkflowError):
    """An idempotency identity is already bound to different authority."""


class UserOrderRequestStateError(UserOrderWorkflowError):
    """A requested state transition is not valid for the stored row."""


def normalize_user_instruction(value: str) -> str:
    """Normalize presentation-only differences without changing semantics."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().split())


def raw_instruction_sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def canonical_payload_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def directive_execution_event_payload(
    record: "UserOrderRequestRecord", response: Any
) -> dict[str, Any]:
    """Project one Trading response into a correlation-safe audit payload.

    The payload deliberately contains no account number, credential, proof, or
    raw user text. It is sufficient to join a Discord/Web request to the
    durable directive, its legs, and the broker identifiers actually returned
    by Trading.
    """

    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        raw = model_dump(mode="json")
    elif isinstance(response, Mapping):
        raw = dict(response)
    else:
        raise UserOrderRequestStateError("directive response is not an object")

    legs: list[dict[str, Any]] = []
    for leg in raw.get("legs") or []:
        if not isinstance(leg, Mapping):
            continue
        broker_order_id = str(leg.get("broker_order_id") or "").strip() or None
        broker_order_no = broker_order_id
        if broker_order_no and broker_order_no.startswith("ls-paper:"):
            broker_order_no = broker_order_no.split(":", 1)[1] or None
        legs.append(
            {
                "leg_id": str(leg.get("leg_id") or "") or None,
                "leg_index": leg.get("leg_index"),
                "symbol": leg.get("symbol"),
                "side": leg.get("side"),
                "order_type": leg.get("order_type"),
                "limit_price": leg.get("limit_price"),
                "state": leg.get("state"),
                "requested_quantity": leg.get("requested_quantity"),
                "filled_quantity": leg.get("filled_quantity"),
                "average_fill_price": leg.get("average_fill_price"),
                "broker_order_id": broker_order_id,
                "broker_order_no": broker_order_no,
                "broker_event_id": leg.get("broker_event_id"),
                "error_code": leg.get("error_code"),
            }
        )

    client_request_id = record.client_request_id
    return {
        "schema_version": "paper-order-correlation.v1",
        "source": "NATURAL_LANGUAGE",
        "request_source": (
            "DISCORD" if client_request_id.startswith("discord:") else "WEB_OR_API"
        ),
        "mode": record.mode,
        "client_request_id": client_request_id,
        "order_request_id": record.order_request_id,
        "ceo_root_task_id": record.ceo_root_task_id,
        "trading_task_id": record.trading_task_id,
        "directive_id": str(raw.get("directive_id") or "") or None,
        "directive_state": raw.get("state"),
        "action": raw.get("action"),
        "error_code": raw.get("error_code"),
        "legs": legs,
    }


@dataclass(frozen=True)
class UserOrderRequestRecord:
    order_request_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    raw_instruction: str
    normalized_instruction: str
    raw_instruction_sha256: str
    state: str = "RECEIVED"
    ceo_root_task_id: str | None = None
    trading_task_id: str | None = None
    action: str | None = None
    canonical_payload: dict[str, Any] | None = None
    payload_sha256: str | None = None
    directive_id: str | None = None
    clarification_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    mode: str = PAPER_ORDER_MODE


class UserOrderRequestRepository(Protocol):
    def admit(
        self,
        *,
        user_id: str,
        fund_id: str,
        book_id: str,
        client_request_id: str,
        raw_instruction: str,
    ) -> UserOrderRequestRecord: ...

    def get(self, order_request_id: str) -> UserOrderRequestRecord | None: ...

    def find_committed_directive(
        self, record: UserOrderRequestRecord
    ) -> str | None: ...

    def bind_root(
        self, order_request_id: str, root_task_id: str
    ) -> UserOrderRequestRecord: ...

    def bind_trading_task(
        self, order_request_id: str, trading_task_id: str
    ) -> UserOrderRequestRecord: ...

    def record_interpretation(
        self,
        order_request_id: str,
        *,
        trading_task_id: str,
        interpretation: Mapping[str, Any],
        interpretation_sha256: str,
        source: str = "HERMES",
    ) -> UserOrderRequestRecord: ...

    def mark_outcome(
        self,
        order_request_id: str,
        *,
        state: str,
        action: str | None = None,
        canonical_payload: Mapping[str, Any] | None = None,
        payload_sha256: str | None = None,
        directive_id: str | None = None,
        clarification_code: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserOrderRequestRecord: ...


def _same_admission(
    record: UserOrderRequestRecord,
    *,
    user_id: str,
    fund_id: str,
    book_id: str,
    client_request_id: str,
    raw_instruction: str,
) -> bool:
    return (
        record.user_id == user_id
        and record.fund_id == fund_id
        and record.book_id == book_id
        and record.client_request_id == client_request_id
        and record.raw_instruction == raw_instruction
        and record.raw_instruction_sha256 == raw_instruction_sha256(raw_instruction)
        and record.mode == PAPER_ORDER_MODE
    )


class InMemoryUserOrderRequestRepository:
    """Deterministic local/test repository with the production invariants."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, UserOrderRequestRecord] = {}
        self._request_index: dict[tuple[str, str], str] = {}
        self._interpretations: dict[tuple[str, str], str] = {}
        self._events: list[dict[str, Any]] = []

    def admit(
        self,
        *,
        user_id: str,
        fund_id: str,
        book_id: str,
        client_request_id: str,
        raw_instruction: str,
    ) -> UserOrderRequestRecord:
        normalized = normalize_user_instruction(raw_instruction)
        if not normalized:
            raise UserOrderRequestConflict("raw instruction is empty")
        key = (str(user_id), str(client_request_id))
        with self._lock:
            existing_id = self._request_index.get(key)
            if existing_id:
                existing = self._records[existing_id]
                if not _same_admission(
                    existing,
                    user_id=str(user_id),
                    fund_id=str(fund_id),
                    book_id=str(book_id),
                    client_request_id=str(client_request_id),
                    raw_instruction=str(raw_instruction),
                ):
                    raise UserOrderRequestConflict(
                        "client request id is bound to different PAPER authority"
                    )
                return existing
            now = datetime.now(timezone.utc)
            record = UserOrderRequestRecord(
                order_request_id=str(uuid4()),
                user_id=str(user_id),
                fund_id=str(fund_id),
                book_id=str(book_id),
                client_request_id=str(client_request_id),
                raw_instruction=str(raw_instruction),
                normalized_instruction=normalized,
                raw_instruction_sha256=raw_instruction_sha256(raw_instruction),
                created_at=now,
                updated_at=now,
            )
            self._records[record.order_request_id] = record
            self._request_index[key] = record.order_request_id
            return record

    def get(self, order_request_id: str) -> UserOrderRequestRecord | None:
        with self._lock:
            return self._records.get(str(order_request_id))

    def find_committed_directive(
        self, record: UserOrderRequestRecord
    ) -> str | None:
        del record
        return None

    def _replace(self, record: UserOrderRequestRecord, **changes: Any) -> UserOrderRequestRecord:
        updated = replace(
            record,
            **changes,
            version=record.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        self._records[record.order_request_id] = updated
        return updated

    def bind_root(self, order_request_id: str, root_task_id: str) -> UserOrderRequestRecord:
        with self._lock:
            record = self._required(order_request_id)
            if record.ceo_root_task_id not in {None, root_task_id}:
                raise UserOrderRequestConflict("order request is bound to another CEO root")
            if record.ceo_root_task_id == root_task_id:
                return record
            return self._replace(
                record, ceo_root_task_id=root_task_id, state="KANBAN_QUEUED"
            )

    def bind_trading_task(
        self, order_request_id: str, trading_task_id: str
    ) -> UserOrderRequestRecord:
        with self._lock:
            record = self._required(order_request_id)
            if record.trading_task_id not in {None, trading_task_id}:
                raise UserOrderRequestConflict("order request is bound to another Trading task")
            if record.trading_task_id == trading_task_id:
                return record
            return self._replace(record, trading_task_id=trading_task_id)

    def record_interpretation(
        self,
        order_request_id: str,
        *,
        trading_task_id: str,
        interpretation: Mapping[str, Any],
        interpretation_sha256: str,
        source: str = "HERMES",
    ) -> UserOrderRequestRecord:
        del interpretation
        with self._lock:
            record = self._required(order_request_id)
            if record.trading_task_id != trading_task_id:
                raise UserOrderRequestConflict("Trading task does not match admitted request")
            key = (record.order_request_id, source)
            existing = self._interpretations.get(key)
            if existing and existing != interpretation_sha256:
                raise UserOrderRequestConflict("interpretation replay changed content")
            self._interpretations[key] = interpretation_sha256
            if record.state not in {"RECEIVED", "KANBAN_QUEUED", "INTERPRETING"}:
                return record
            return self._replace(record, state="INTERPRETED")

    def mark_outcome(
        self,
        order_request_id: str,
        *,
        state: str,
        action: str | None = None,
        canonical_payload: Mapping[str, Any] | None = None,
        payload_sha256: str | None = None,
        directive_id: str | None = None,
        clarification_code: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserOrderRequestRecord:
        if state not in ORDER_REQUEST_STATES:
            raise UserOrderRequestStateError(f"unsupported order request state: {state}")
        if event_type is not None and not (1 <= len(event_type) <= 64):
            raise UserOrderRequestStateError("invalid order request event type")
        payload = dict(canonical_payload) if canonical_payload is not None else None
        if (payload is None) != (payload_sha256 is None):
            raise UserOrderRequestStateError("canonical payload and digest must be stored together")
        with self._lock:
            record = self._required(order_request_id)
            if record.directive_id and directive_id and record.directive_id != directive_id:
                raise UserOrderRequestConflict("order request is bound to another directive")
            completed_at = (
                datetime.now(timezone.utc)
                if state in {"COMPLETED", "FAILED", "REJECTED", "NOT_ORDER"}
                else record.completed_at
            )
            updated = self._replace(
                record,
                state=state,
                action=action if action is not None else record.action,
                canonical_payload=payload if payload is not None else record.canonical_payload,
                payload_sha256=payload_sha256 or record.payload_sha256,
                directive_id=directive_id or record.directive_id,
                clarification_code=clarification_code,
                error_code=error_code,
                error_message=error_message,
                completed_at=completed_at,
            )
            payload = {
                "schema_version": "user-order-request-event.v1",
                "client_request_id": updated.client_request_id,
                "order_request_id": updated.order_request_id,
                "directive_id": updated.directive_id,
                "action": updated.action,
                "error_code": updated.error_code,
            }
            payload.update(dict(event_payload or {}))
            self._events.append(
                {
                    "event_type": event_type or f"STATE_{state}",
                    "to_state": state,
                    "payload": payload,
                }
            )
            return updated

    def events_for(self, order_request_id: str) -> list[dict[str, Any]]:
        """Test/local projection of the append-only production event journal."""

        with self._lock:
            return [
                dict(event)
                for event in self._events
                if event["payload"].get("order_request_id") == str(order_request_id)
            ]

    def _required(self, order_request_id: str) -> UserOrderRequestRecord:
        record = self._records.get(str(order_request_id))
        if record is None:
            raise UserOrderRequestStateError("order request not found")
        return record


_SELECT_COLUMNS = """
order_request_id,user_id,fund_id,book_id,client_request_id,mode,
raw_instruction,normalized_instruction,raw_instruction_sha256,state,
ceo_root_task_id,trading_task_id,action,canonical_payload,payload_sha256,
directive_id,clarification_code,error_code,error_message,version,
created_at,updated_at,completed_at
"""


class PostgresUserOrderRequestRepository:
    def __init__(self, dsn: str, *, role: str = "svc_order_orchestrator") -> None:
        if not dsn.strip():
            raise UserOrderWorkflowUnavailable("operational database URL is required")
        if not role.strip():
            raise UserOrderWorkflowUnavailable("order orchestrator database role is required")
        self.dsn = dsn
        self.role = role

    def _connect(self):
        return psycopg2.connect(self.dsn, connect_timeout=8)

    def _set_role(self, cursor: Any) -> None:
        cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(self.role)))

    @staticmethod
    def _row(row: tuple[Any, ...] | None) -> UserOrderRequestRecord | None:
        if row is None:
            return None
        payload = row[13]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return UserOrderRequestRecord(
            order_request_id=str(row[0]),
            user_id=str(row[1]),
            fund_id=str(row[2]),
            book_id=str(row[3]),
            client_request_id=str(row[4]),
            mode=str(row[5]),
            raw_instruction=str(row[6]),
            normalized_instruction=str(row[7]),
            raw_instruction_sha256=str(row[8]),
            state=str(row[9]),
            ceo_root_task_id=str(row[10]) if row[10] else None,
            trading_task_id=str(row[11]) if row[11] else None,
            action=str(row[12]) if row[12] else None,
            canonical_payload=dict(payload) if isinstance(payload, Mapping) else None,
            payload_sha256=str(row[14]) if row[14] else None,
            directive_id=str(row[15]) if row[15] else None,
            clarification_code=str(row[16]) if row[16] else None,
            error_code=str(row[17]) if row[17] else None,
            error_message=str(row[18]) if row[18] else None,
            version=int(row[19]),
            created_at=row[20],
            updated_at=row[21],
            completed_at=row[22],
        )

    def admit(
        self,
        *,
        user_id: str,
        fund_id: str,
        book_id: str,
        client_request_id: str,
        raw_instruction: str,
    ) -> UserOrderRequestRecord:
        normalized = normalize_user_instruction(raw_instruction)
        digest = raw_instruction_sha256(raw_instruction)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    insert into execution.user_order_requests (
                      user_id,fund_id,book_id,client_request_id,mode,
                      raw_instruction,normalized_instruction,raw_instruction_sha256,
                      normalizer_version,state
                    ) values (%s,%s,%s,%s,'PAPER',%s,%s,%s,%s,'RECEIVED')
                    on conflict (user_id,client_request_id) do nothing
                    returning {_SELECT_COLUMNS}
                    """,
                    (
                        UUID(str(user_id)),
                        UUID(str(fund_id)),
                        UUID(str(book_id)),
                        client_request_id,
                        raw_instruction,
                        normalized,
                        digest,
                        PAPER_ORDER_NORMALIZER_VERSION,
                    ),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    cursor.execute(
                        f"""select {_SELECT_COLUMNS}
                              from execution.user_order_requests
                             where user_id=%s and client_request_id=%s
                             for update""",
                        (UUID(str(user_id)), client_request_id),
                    )
                    record = self._row(cursor.fetchone())
                if record is None or not _same_admission(
                    record,
                    user_id=str(user_id),
                    fund_id=str(fund_id),
                    book_id=str(book_id),
                    client_request_id=client_request_id,
                    raw_instruction=raw_instruction,
                ):
                    raise UserOrderRequestConflict(
                        "client request id is bound to different PAPER authority"
                    )
                self._event(cursor, record, "REQUEST_ADMITTED", record.state, {})
                return record
        except UserOrderWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable("could not admit user PAPER order") from exc

    def get(self, order_request_id: str) -> UserOrderRequestRecord | None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"select {_SELECT_COLUMNS} from execution.user_order_requests where order_request_id=%s",
                    (UUID(str(order_request_id)),),
                )
                return self._row(cursor.fetchone())
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable("could not read user PAPER order") from exc

    def find_committed_directive(
        self, record: UserOrderRequestRecord
    ) -> str | None:
        """Find an exact directive after an ambiguous submission response.

        This is a read-only recovery lookup. It never resubmits an order and
        requires the durable authority, deterministic idempotency key, action,
        and source request identity to match.  The request digest is not used:
        the admitted payload contains an instrument mention while the durable
        directive contains its resolved instrument UUID/symbol, so equal
        orders intentionally have different pre/post-resolution digests.
        """

        if (
            record.state != "UNKNOWN"
            or record.directive_id is not None
            or not record.action
        ):
            return None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select directive_id
                      from execution.user_directives
                     where user_id=%s and fund_id=%s and book_id=%s
                       and idempotency_key=%s and action=%s
                       and (source_order_request_id is null
                            or source_order_request_id=%s)
                     limit 2
                    """,
                    (
                        UUID(record.user_id),
                        UUID(record.fund_id),
                        UUID(record.book_id),
                        f"ceo-paper:{record.order_request_id}",
                        record.action,
                        UUID(record.order_request_id),
                    ),
                )
                rows = cursor.fetchall()
                if len(rows) > 1:
                    raise UserOrderRequestConflict(
                        "ambiguous PAPER directive recovery result"
                    )
                return str(rows[0][0]) if rows else None
        except UserOrderWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable(
                "could not recover committed PAPER directive"
            ) from exc

    def bind_root(self, order_request_id: str, root_task_id: str) -> UserOrderRequestRecord:
        return self._bind_task(order_request_id, "ceo_root_task_id", root_task_id, "KANBAN_QUEUED")

    def bind_trading_task(
        self, order_request_id: str, trading_task_id: str
    ) -> UserOrderRequestRecord:
        return self._bind_task(order_request_id, "trading_task_id", trading_task_id, None)

    def _bind_task(
        self,
        order_request_id: str,
        column: str,
        task_id: str,
        state: str | None,
    ) -> UserOrderRequestRecord:
        if column not in {"ceo_root_task_id", "trading_task_id"}:
            raise UserOrderRequestStateError("invalid task binding column")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""select {_SELECT_COLUMNS} from execution.user_order_requests
                         where order_request_id=%s for update""",
                    (UUID(str(order_request_id)),),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    raise UserOrderRequestStateError("order request not found")
                existing = getattr(record, column)
                if existing not in {None, task_id}:
                    raise UserOrderRequestConflict("order request task binding conflict")
                if existing == task_id:
                    return record
                state_sql = ", state=%s" if state else ""
                params: list[Any] = [task_id]
                if state:
                    params.append(state)
                params.extend((UUID(str(order_request_id)), record.version))
                cursor.execute(
                    f"""update execution.user_order_requests
                            set {column}=%s{state_sql}, version=version+1
                          where order_request_id=%s and version=%s
                      returning {_SELECT_COLUMNS}""",
                    tuple(params),
                )
                updated = self._row(cursor.fetchone())
                if updated is None:
                    raise UserOrderRequestStateError("concurrent task binding conflict")
                self._event(
                    cursor,
                    updated,
                    "CEO_ROOT_BOUND" if column == "ceo_root_task_id" else "TRADING_TASK_BOUND",
                    updated.state,
                    {column: task_id},
                )
                return updated
        except UserOrderWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable("could not bind user PAPER order task") from exc

    def record_interpretation(
        self,
        order_request_id: str,
        *,
        trading_task_id: str,
        interpretation: Mapping[str, Any],
        interpretation_sha256: str,
        source: str = "HERMES",
    ) -> UserOrderRequestRecord:
        if source not in {"HERMES", "DETERMINISTIC"}:
            raise UserOrderRequestStateError("invalid interpretation source")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""select {_SELECT_COLUMNS} from execution.user_order_requests
                         where order_request_id=%s for update""",
                    (UUID(str(order_request_id)),),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    raise UserOrderRequestStateError("order request not found")
                if record.trading_task_id != trading_task_id:
                    raise UserOrderRequestConflict("Trading task does not match admitted request")
                cursor.execute(
                    """
                    select interpretation_sha256
                      from execution.user_order_interpretations
                     where order_request_id=%s and interpretation_version=1 and source=%s
                    """,
                    (UUID(record.order_request_id), source),
                )
                existing = cursor.fetchone()
                if existing and str(existing[0]) != interpretation_sha256:
                    raise UserOrderRequestConflict("interpretation replay changed content")
                cursor.execute(
                    """
                    insert into execution.user_order_interpretations (
                      order_request_id,interpretation_version,source,trading_task_id,
                      raw_instruction_sha256,interpretation,interpretation_sha256
                    ) values (%s,1,%s,%s,%s,%s,%s)
                    on conflict (order_request_id,interpretation_version,source) do nothing
                    """,
                    (
                        UUID(record.order_request_id),
                        source,
                        trading_task_id,
                        record.raw_instruction_sha256,
                        Json(dict(interpretation)),
                        interpretation_sha256,
                    ),
                )
                if record.state in {"RECEIVED", "KANBAN_QUEUED", "INTERPRETING"}:
                    cursor.execute(
                        f"""update execution.user_order_requests
                                set state='INTERPRETED', version=version+1
                              where order_request_id=%s and version=%s
                          returning {_SELECT_COLUMNS}""",
                        (UUID(record.order_request_id), record.version),
                    )
                    record = self._row(cursor.fetchone())
                    if record is None:
                        raise UserOrderRequestStateError("concurrent interpretation conflict")
                self._event(cursor, record, "INTERPRETATION_RECORDED", record.state, {})
                return record
        except UserOrderWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable("could not record PAPER order interpretation") from exc

    def mark_outcome(
        self,
        order_request_id: str,
        *,
        state: str,
        action: str | None = None,
        canonical_payload: Mapping[str, Any] | None = None,
        payload_sha256: str | None = None,
        directive_id: str | None = None,
        clarification_code: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserOrderRequestRecord:
        if state not in ORDER_REQUEST_STATES:
            raise UserOrderRequestStateError(f"unsupported order request state: {state}")
        if event_type is not None and not (1 <= len(event_type) <= 64):
            raise UserOrderRequestStateError("invalid order request event type")
        payload = dict(canonical_payload) if canonical_payload is not None else None
        if (payload is None) != (payload_sha256 is None):
            raise UserOrderRequestStateError("canonical payload and digest must be stored together")
        terminal = state in {"COMPLETED", "FAILED", "REJECTED", "NOT_ORDER"}
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""select {_SELECT_COLUMNS} from execution.user_order_requests
                         where order_request_id=%s for update""",
                    (UUID(str(order_request_id)),),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    raise UserOrderRequestStateError("order request not found")
                if record.directive_id and directive_id and record.directive_id != directive_id:
                    raise UserOrderRequestConflict("order request is bound to another directive")
                cursor.execute(
                    f"""
                    update execution.user_order_requests
                       set state=%s,
                           action=coalesce(%s,action),
                           canonical_payload=coalesce(%s,canonical_payload),
                           payload_sha256=coalesce(%s,payload_sha256),
                           directive_id=coalesce(%s,directive_id),
                           clarification_code=%s,error_code=%s,error_message=%s,
                           completed_at=case when %s then coalesce(completed_at,now()) else completed_at end,
                           version=version+1
                     where order_request_id=%s and version=%s
                 returning {_SELECT_COLUMNS}
                    """,
                    (
                        state,
                        action,
                        Json(payload) if payload is not None else None,
                        payload_sha256,
                        UUID(str(directive_id)) if directive_id else None,
                        clarification_code,
                        error_code,
                        error_message,
                        terminal,
                        UUID(record.order_request_id),
                        record.version,
                    ),
                )
                updated = self._row(cursor.fetchone())
                if updated is None:
                    raise UserOrderRequestStateError("concurrent order request transition")
                if directive_id:
                    cursor.execute(
                        """update execution.user_directives
                              set source_order_request_id=%s
                            where directive_id=%s
                              and (source_order_request_id is null or source_order_request_id=%s)""",
                        (
                            UUID(updated.order_request_id),
                            UUID(str(directive_id)),
                            UUID(updated.order_request_id),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise UserOrderRequestConflict("directive source request binding conflict")
                audit_payload = {
                    "schema_version": "user-order-request-event.v1",
                    "client_request_id": updated.client_request_id,
                    "order_request_id": updated.order_request_id,
                    "directive_id": updated.directive_id,
                    "action": updated.action,
                    "error_code": updated.error_code,
                }
                audit_payload.update(dict(event_payload or {}))
                self._event(
                    cursor,
                    updated,
                    event_type or f"STATE_{state}",
                    state,
                    audit_payload,
                )
                return updated
        except UserOrderWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise UserOrderWorkflowUnavailable("could not transition user PAPER order") from exc

    @staticmethod
    def _event(
        cursor: Any,
        record: UserOrderRequestRecord,
        event_type: str,
        to_state: str,
        payload: Mapping[str, Any],
    ) -> None:
        identity = f"{record.order_request_id}:{event_type}:{record.version}"
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        cursor.execute(
            """
            insert into execution.user_order_request_events (
              event_id,order_request_id,event_type,from_state,to_state,payload
            ) values (%s,%s,%s,null,%s,%s)
            on conflict (event_id) do nothing
            """,
            (
                event_id,
                UUID(record.order_request_id),
                event_type,
                to_state,
                Json(dict(payload)),
            ),
        )


def recover_committed_directive(
    repository: UserOrderRequestRepository,
    record: UserOrderRequestRecord,
) -> UserOrderRequestRecord:
    """Bind an exact post-timeout directive without resubmitting the order."""

    if record.state != "UNKNOWN" or record.directive_id is not None:
        return record
    directive_id = repository.find_committed_directive(record)
    if directive_id is None:
        return record
    return repository.mark_outcome(
        record.order_request_id,
        state="UNKNOWN",
        directive_id=directive_id,
        error_code=record.error_code,
        error_message=record.error_message,
    )


_repository_override: UserOrderRequestRepository | None = None
_repository_cache: UserOrderRequestRepository | None = None


def set_user_order_repository_for_tests(
    repository: UserOrderRequestRepository | None,
) -> None:
    global _repository_override, _repository_cache
    _repository_override = repository
    _repository_cache = None


def _production_order_runtime() -> bool:
    return (
        os.getenv("APP_ENV", "development").casefold() in {"production", "staging"}
        or os.getenv("PORTFOLIO_DATA_MODE", "").casefold() == "production"
    )


def _order_orchestrator_database_url() -> str:
    """Select the isolated order login, failing closed in production."""

    dedicated = os.getenv("ORDER_ORCHESTRATOR_DATABASE_URL", "").strip()
    if dedicated:
        return dedicated
    if _production_order_runtime():
        raise UserOrderWorkflowUnavailable(
            "dedicated order orchestrator database is required"
        )
    # Local/test compatibility only.  Production must never let the generic
    # application login acquire the critical order role.
    return (
        os.getenv("CONTROL_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )


def user_order_repository() -> UserOrderRequestRepository:
    global _repository_cache
    if _repository_override is not None:
        return _repository_override
    if _repository_cache is not None:
        return _repository_cache
    dsn = _order_orchestrator_database_url()
    production = _production_order_runtime()
    if not dsn:
        if production:
            raise UserOrderWorkflowUnavailable(
                "operational database is required for user PAPER orders"
            )
        _repository_cache = InMemoryUserOrderRequestRepository()
    else:
        _repository_cache = PostgresUserOrderRequestRepository(
            dsn,
            role=os.getenv(
                "ORDER_ORCHESTRATOR_DATABASE_ROLE", "svc_order_orchestrator"
            ).strip(),
        )
    return _repository_cache


__all__ = [
    "InMemoryUserOrderRequestRepository",
    "ORDER_REQUEST_STATES",
    "PAPER_ORDER_MODE",
    "PostgresUserOrderRequestRepository",
    "UserOrderRequestConflict",
    "UserOrderRequestRecord",
    "UserOrderRequestRepository",
    "UserOrderRequestStateError",
    "UserOrderWorkflowError",
    "UserOrderWorkflowUnavailable",
    "canonical_payload_sha256",
    "normalize_user_instruction",
    "raw_instruction_sha256",
    "recover_committed_directive",
    "set_user_order_repository_for_tests",
    "user_order_repository",
]
