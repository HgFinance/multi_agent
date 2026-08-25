"""Durable management workflow for authenticated conditional PAPER rules."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, register_uuid

from orchestration.conditional_rules import ConditionalRuleSpec, RuleState, rule_fingerprint


register_uuid()


class ConditionalRuleWorkflowError(RuntimeError):
    pass


class ConditionalRuleUnavailable(ConditionalRuleWorkflowError):
    pass


class ConditionalRuleConflict(ConditionalRuleWorkflowError):
    pass


class ConditionalRuleNotFound(ConditionalRuleWorkflowError):
    pass


@dataclass(frozen=True)
class ConditionalRuleRecord:
    rule_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    state: RuleState
    rule_version: int
    spec: ConditionalRuleSpec
    spec_sha256: str
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_execution_state: str | None = None
    last_guard_code: str | None = None
    directive_id: str | None = None
    last_error_code: str | None = None


class ConditionalRuleRepository(Protocol):
    def create_pending(
        self,
        *,
        spec: ConditionalRuleSpec,
        raw_instruction: str,
        client_request_id: str,
        parser_source: str,
    ) -> ConditionalRuleRecord: ...

    def get(self, rule_id: str, *, user_id: str) -> ConditionalRuleRecord | None: ...

    def list_for_user(self, user_id: str) -> list[ConditionalRuleRecord]: ...

    def find_by_directive_ids(
        self, directive_ids: set[str]
    ) -> dict[str, ConditionalRuleRecord]: ...

    def activate(
        self, rule_id: str, *, user_id: str, confirmation_sha256: str
    ) -> ConditionalRuleRecord: ...

    def transition(
        self, rule_id: str, *, user_id: str, target: RuleState
    ) -> ConditionalRuleRecord: ...


class InMemoryConditionalRuleRepository:
    def __init__(self) -> None:
        self._records: dict[str, ConditionalRuleRecord] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def create_pending(
        self,
        *,
        spec: ConditionalRuleSpec,
        raw_instruction: str,
        client_request_id: str,
        parser_source: str,
    ) -> ConditionalRuleRecord:
        del raw_instruction, parser_source
        key = (str(spec.authority.user_id), client_request_id)
        digest = rule_fingerprint(spec)
        with self._lock:
            existing_id = self._requests.get(key)
            if existing_id:
                existing = self._records[existing_id]
                if existing.spec_sha256 != digest:
                    raise ConditionalRuleConflict(
                        "client request id is bound to a different conditional rule"
                    )
                return existing
            now = datetime.now(timezone.utc)
            record = ConditionalRuleRecord(
                rule_id=str(uuid4()),
                user_id=str(spec.authority.user_id),
                fund_id=str(spec.authority.fund_id),
                book_id=str(spec.authority.book_id),
                client_request_id=client_request_id,
                state=RuleState.PENDING_CONFIRMATION,
                rule_version=1,
                spec=spec,
                spec_sha256=digest,
                confirmed_at=None,
                created_at=now,
                updated_at=now,
            )
            self._records[record.rule_id] = record
            self._requests[key] = record.rule_id
            return record

    def get(self, rule_id: str, *, user_id: str) -> ConditionalRuleRecord | None:
        record = self._records.get(str(rule_id))
        return record if record is not None and record.user_id == str(user_id) else None

    def list_for_user(self, user_id: str) -> list[ConditionalRuleRecord]:
        return sorted(
            (record for record in self._records.values() if record.user_id == str(user_id)),
            key=lambda record: (record.created_at, record.rule_id),
            reverse=True,
        )

    def find_by_directive_ids(
        self, directive_ids: set[str]
    ) -> dict[str, ConditionalRuleRecord]:
        wanted = {str(value) for value in directive_ids}
        return {
            str(record.directive_id): record
            for record in self._records.values()
            if record.directive_id is not None and str(record.directive_id) in wanted
        }

    def activate(
        self, rule_id: str, *, user_id: str, confirmation_sha256: str
    ) -> ConditionalRuleRecord:
        with self._lock:
            record = self.get(rule_id, user_id=user_id)
            if record is None:
                raise ConditionalRuleNotFound("conditional rule not found")
            if confirmation_sha256 != record.spec_sha256:
                raise ConditionalRuleConflict("confirmation fingerprint does not match rule")
            if record.state is RuleState.ACTIVE:
                return record
            if record.state is not RuleState.PENDING_CONFIRMATION:
                raise ConditionalRuleConflict("conditional rule is not awaiting confirmation")
            now = datetime.now(timezone.utc)
            if record.spec.expires_at <= now:
                raise ConditionalRuleConflict("conditional rule expired before confirmation")
            updated = replace(record, state=RuleState.ACTIVE, confirmed_at=now, updated_at=now)
            self._records[record.rule_id] = updated
            return updated

    def transition(
        self, rule_id: str, *, user_id: str, target: RuleState
    ) -> ConditionalRuleRecord:
        allowed = {
            RuleState.ACTIVE: {RuleState.PAUSED, RuleState.CANCELLED},
            RuleState.PAUSED: {RuleState.ACTIVE, RuleState.CANCELLED},
            RuleState.PENDING_CONFIRMATION: {RuleState.CANCELLED},
        }
        with self._lock:
            record = self.get(rule_id, user_id=user_id)
            if record is None:
                raise ConditionalRuleNotFound("conditional rule not found")
            if target == record.state:
                return record
            if target not in allowed.get(record.state, set()):
                raise ConditionalRuleConflict(
                    f"invalid conditional rule transition {record.state.value} -> {target.value}"
                )
            if target is RuleState.ACTIVE and record.spec.expires_at <= datetime.now(
                timezone.utc
            ):
                raise ConditionalRuleConflict("expired conditional rule cannot resume")
            updated = replace(record, state=target, updated_at=datetime.now(timezone.utc))
            self._records[record.rule_id] = updated
            return updated


_SELECT = """
r.rule_id,r.user_id,r.fund_id,r.book_id,r.client_request_id,r.state,
r.current_version,v.spec,v.spec_sha256,r.confirmed_at,r.created_at,r.updated_at,
(select execution.state
   from execution.conditional_rule_executions execution
  where execution.rule_id=r.rule_id
  order by execution.created_at desc limit 1),
(select execution.guard_code
   from execution.conditional_rule_executions execution
  where execution.rule_id=r.rule_id
  order by execution.created_at desc limit 1),
(select execution.directive_id::text
   from execution.conditional_rule_executions execution
  where execution.rule_id=r.rule_id
  order by execution.created_at desc limit 1),
(select execution.error_code
   from execution.conditional_rule_executions execution
  where execution.rule_id=r.rule_id
  order by execution.created_at desc limit 1)
"""


class PostgresConditionalRuleRepository:
    def __init__(
        self,
        dsn: str,
        *,
        role: str = "svc_conditional_rule_orchestrator",
    ) -> None:
        if not dsn.strip() or not role.strip():
            raise ConditionalRuleUnavailable("conditional rule database is not configured")
        self.dsn = dsn
        self.role = role

    def _connect(self):
        return psycopg2.connect(self.dsn, connect_timeout=8)

    def _set_role(self, cursor: Any) -> None:
        cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(self.role)))

    @staticmethod
    def _row(row: tuple[Any, ...] | None) -> ConditionalRuleRecord | None:
        if row is None:
            return None
        raw_spec = json.loads(row[7]) if isinstance(row[7], str) else row[7]
        spec = ConditionalRuleSpec.model_validate(raw_spec)
        if rule_fingerprint(spec) != str(row[8]):
            raise ConditionalRuleUnavailable(
                "stored conditional rule payload does not match its fingerprint"
            )
        return ConditionalRuleRecord(
            rule_id=str(row[0]),
            user_id=str(row[1]),
            fund_id=str(row[2]),
            book_id=str(row[3]),
            client_request_id=str(row[4]),
            state=RuleState(str(row[5])),
            rule_version=int(row[6]),
            spec=spec,
            spec_sha256=str(row[8]),
            confirmed_at=row[9],
            created_at=row[10],
            updated_at=row[11],
            last_execution_state=str(row[12]) if row[12] is not None else None,
            last_guard_code=str(row[13]) if row[13] is not None else None,
            directive_id=str(row[14]) if row[14] is not None else None,
            last_error_code=str(row[15]) if row[15] is not None else None,
        )

    @staticmethod
    def _event(
        cursor: Any,
        record: ConditionalRuleRecord,
        event_type: str,
        from_state: RuleState | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        cursor.execute(
            """
            insert into execution.conditional_trade_rule_events (
              event_id,rule_id,rule_version,event_type,from_state,to_state,payload
            ) values (%s,%s,%s,%s,%s,%s,%s)
            on conflict (event_id) do nothing
            """,
            (
                f"cre_{uuid4().hex}",
                UUID(record.rule_id),
                record.rule_version,
                event_type,
                from_state.value if from_state else None,
                record.state.value,
                Json(payload or {}),
            ),
        )

    def create_pending(
        self,
        *,
        spec: ConditionalRuleSpec,
        raw_instruction: str,
        client_request_id: str,
        parser_source: str,
    ) -> ConditionalRuleRecord:
        if parser_source not in {"HERMES", "DETERMINISTIC"}:
            raise ConditionalRuleConflict("invalid conditional rule parser source")
        digest = rule_fingerprint(spec)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    insert into execution.conditional_trade_rules (
                      user_id,fund_id,book_id,instrument_id,symbol,client_request_id,
                      state,current_version,execution_mode,repeat_policy,
                      evaluation_clock,primary_timeframe,market_closed_policy,expires_at
                    ) values (
                      %s,%s,%s,%s,%s,%s,'PENDING_CONFIRMATION',1,'PAPER','ONCE',
                      %s,%s,'REJECT_TRIGGER',%s
                    )
                    on conflict (user_id,client_request_id) do nothing
                    returning rule_id
                    """,
                    (
                        spec.authority.user_id,
                        spec.authority.fund_id,
                        spec.authority.book_id,
                        spec.instrument_id,
                        spec.symbol,
                        client_request_id,
                        spec.evaluation.clock.value,
                        spec.evaluation.primary_timeframe.value
                        if spec.evaluation.primary_timeframe
                        else None,
                        spec.expires_at,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        f"""
                        select {_SELECT}
                          from execution.conditional_trade_rules r
                          join execution.conditional_trade_rule_versions v
                            on v.rule_id=r.rule_id and v.rule_version=r.current_version
                         where r.user_id=%s and r.client_request_id=%s
                         for update of r
                        """,
                        (spec.authority.user_id, client_request_id),
                    )
                    existing = self._row(cursor.fetchone())
                    if existing is None or existing.spec_sha256 != digest:
                        raise ConditionalRuleConflict(
                            "client request id is bound to a different conditional rule"
                        )
                    return existing
                rule_id = UUID(str(inserted[0]))
                cursor.execute(
                    """
                    insert into execution.conditional_trade_rule_versions (
                      rule_id,rule_version,schema_version,spec,spec_sha256,
                      raw_instruction,raw_instruction_sha256,parser_source
                    ) values (%s,1,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        rule_id,
                        spec.schema_version,
                        Json(spec.model_dump(mode="json", exclude_none=True)),
                        digest,
                        raw_instruction,
                        spec.raw_instruction_sha256,
                        parser_source,
                    ),
                )
                cursor.execute(
                    f"""
                    select {_SELECT}
                      from execution.conditional_trade_rules r
                      join execution.conditional_trade_rule_versions v
                        on v.rule_id=r.rule_id and v.rule_version=r.current_version
                     where r.rule_id=%s
                    """,
                    (rule_id,),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    raise ConditionalRuleUnavailable("created conditional rule disappeared")
                self._event(cursor, record, "PENDING_CONFIRMATION", None)
                return record
        except ConditionalRuleWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable("could not create conditional rule") from exc

    def get(self, rule_id: str, *, user_id: str) -> ConditionalRuleRecord | None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    select {_SELECT}
                      from execution.conditional_trade_rules r
                      join execution.conditional_trade_rule_versions v
                        on v.rule_id=r.rule_id and v.rule_version=r.current_version
                     where r.rule_id=%s and r.user_id=%s
                    """,
                    (UUID(str(rule_id)), UUID(str(user_id))),
                )
                return self._row(cursor.fetchone())
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable("could not read conditional rule") from exc

    def list_for_user(self, user_id: str) -> list[ConditionalRuleRecord]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    select {_SELECT}
                      from execution.conditional_trade_rules r
                      join execution.conditional_trade_rule_versions v
                        on v.rule_id=r.rule_id and v.rule_version=r.current_version
                     where r.user_id=%s
                     order by r.created_at desc,r.rule_id
                    """,
                    (UUID(str(user_id)),),
                )
                return [self._row(row) for row in cursor.fetchall()]  # type: ignore[misc]
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable("could not list conditional rules") from exc

    def find_by_directive_ids(
        self, directive_ids: set[str]
    ) -> dict[str, ConditionalRuleRecord]:
        wanted = [UUID(value) for value in sorted({str(value) for value in directive_ids})]
        if not wanted:
            return {}
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    select {_SELECT}
                      from execution.conditional_trade_rules r
                      join execution.conditional_trade_rule_versions v
                        on v.rule_id=r.rule_id and v.rule_version=r.current_version
                     where exists (
                       select 1
                         from execution.conditional_rule_executions execution
                        where execution.rule_id=r.rule_id
                          and execution.directive_id=any(%s)
                     )
                     order by r.created_at desc,r.rule_id
                    """,
                    (wanted,),
                )
                records = [self._row(row) for row in cursor.fetchall()]
                return {
                    str(record.directive_id): record
                    for record in records
                    if record is not None and record.directive_id is not None
                }
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable(
                "could not correlate conditional rule directives"
            ) from exc

    def activate(
        self, rule_id: str, *, user_id: str, confirmation_sha256: str
    ) -> ConditionalRuleRecord:
        if len(confirmation_sha256) != 64:
            raise ConditionalRuleConflict("invalid confirmation fingerprint")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                current = self._locked(cursor, rule_id, user_id)
                if current.spec_sha256 != confirmation_sha256:
                    raise ConditionalRuleConflict("confirmation fingerprint does not match rule")
                if current.state is RuleState.ACTIVE:
                    return current
                if current.state is not RuleState.PENDING_CONFIRMATION:
                    raise ConditionalRuleConflict("conditional rule is not awaiting confirmation")
                if current.spec.expires_at <= datetime.now(timezone.utc):
                    raise ConditionalRuleConflict("conditional rule expired before confirmation")
                cursor.execute(
                    f"""
                    update execution.conditional_trade_rules
                       set state='ACTIVE',confirmation_sha256=%s,confirmed_at=now(),
                           version=version+1
                     where rule_id=%s and user_id=%s and state='PENDING_CONFIRMATION'
                 returning rule_id
                    """,
                    (confirmation_sha256, UUID(rule_id), UUID(user_id)),
                )
                if cursor.fetchone() is None:
                    raise ConditionalRuleConflict("concurrent conditional rule activation")
                updated = self._locked(cursor, rule_id, user_id)
                self._event(cursor, updated, "ACTIVATED", current.state)
                return updated
        except ConditionalRuleWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable("could not activate conditional rule") from exc

    def transition(
        self, rule_id: str, *, user_id: str, target: RuleState
    ) -> ConditionalRuleRecord:
        if target not in {RuleState.ACTIVE, RuleState.PAUSED, RuleState.CANCELLED}:
            raise ConditionalRuleConflict("unsupported user lifecycle transition")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                current = self._locked(cursor, rule_id, user_id)
                if current.state is target:
                    return current
                allowed = {
                    RuleState.ACTIVE: {RuleState.PAUSED, RuleState.CANCELLED},
                    RuleState.PAUSED: {RuleState.ACTIVE, RuleState.CANCELLED},
                    RuleState.PENDING_CONFIRMATION: {RuleState.CANCELLED},
                }
                if target not in allowed.get(current.state, set()):
                    raise ConditionalRuleConflict(
                        "invalid conditional rule lifecycle transition"
                    )
                if target is RuleState.ACTIVE and current.spec.expires_at <= datetime.now(
                    timezone.utc
                ):
                    raise ConditionalRuleConflict("expired conditional rule cannot resume")
                cursor.execute(
                    """
                    update execution.conditional_trade_rules
                       set state=%s,version=version+1,
                           completed_at=case when %s='CANCELLED' then now() else completed_at end
                     where rule_id=%s and user_id=%s
                 returning rule_id
                    """,
                    (target.value, target.value, UUID(rule_id), UUID(user_id)),
                )
                if cursor.fetchone() is None:
                    raise ConditionalRuleNotFound("conditional rule not found")
                updated = self._locked(cursor, rule_id, user_id)
                self._event(cursor, updated, target.value, current.state)
                return updated
        except ConditionalRuleWorkflowError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise ConditionalRuleUnavailable("could not transition conditional rule") from exc

    def _locked(self, cursor: Any, rule_id: str, user_id: str) -> ConditionalRuleRecord:
        cursor.execute(
            f"""
            select {_SELECT}
              from execution.conditional_trade_rules r
              join execution.conditional_trade_rule_versions v
                on v.rule_id=r.rule_id and v.rule_version=r.current_version
             where r.rule_id=%s and r.user_id=%s
             for update of r
            """,
            (UUID(str(rule_id)), UUID(str(user_id))),
        )
        record = self._row(cursor.fetchone())
        if record is None:
            raise ConditionalRuleNotFound("conditional rule not found")
        return record


_repository_override: ConditionalRuleRepository | None = None
_repository_cache: ConditionalRuleRepository | None = None


def set_conditional_rule_repository_for_tests(
    repository: ConditionalRuleRepository | None,
) -> None:
    global _repository_override, _repository_cache
    _repository_override = repository
    _repository_cache = None


def conditional_rule_repository() -> ConditionalRuleRepository:
    global _repository_cache
    if _repository_override is not None:
        return _repository_override
    if _repository_cache is not None:
        return _repository_cache
    production = (
        os.getenv("APP_ENV", "development").casefold() in {"production", "staging"}
        or os.getenv("PORTFOLIO_DATA_MODE", "").casefold() == "production"
    )
    dedicated = os.getenv("CONDITIONAL_RULE_DATABASE_URL", "").strip()
    if production and not dedicated:
        raise ConditionalRuleUnavailable(
            "dedicated conditional rule database URL is required"
        )
    dsn = dedicated or os.getenv("CONTROL_DATABASE_URL", "").strip() or os.getenv(
        "DATABASE_URL", ""
    ).strip()
    if not dsn:
        if production:
            raise ConditionalRuleUnavailable("conditional rule database is required")
        _repository_cache = InMemoryConditionalRuleRepository()
    else:
        _repository_cache = PostgresConditionalRuleRepository(
            dsn,
            role=os.getenv(
                "CONDITIONAL_RULE_DATABASE_ROLE",
                "svc_conditional_rule_orchestrator",
            ).strip(),
        )
    return _repository_cache


__all__ = [
    "ConditionalRuleConflict",
    "ConditionalRuleNotFound",
    "ConditionalRuleRecord",
    "ConditionalRuleRepository",
    "ConditionalRuleUnavailable",
    "InMemoryConditionalRuleRepository",
    "PostgresConditionalRuleRepository",
    "conditional_rule_repository",
    "set_conditional_rule_repository_for_tests",
]
