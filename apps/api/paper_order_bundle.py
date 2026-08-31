"""Durable composition for one immediate PAPER order plus one deferred rule."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg2
from psycopg2 import sql
from psycopg2.extras import register_uuid


register_uuid()


class PaperOrderBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperOrderBundle:
    bundle_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    raw_instruction: str
    immediate_order_request_id: str
    conditional_rule_id: str | None
    required_quantity: str
    state: str
    error_code: str | None
    created_at: datetime | None
    updated_at: datetime | None


class PostgresPaperOrderBundleRepository:
    """Use the existing order-orchestrator connection boundary."""

    _COLUMNS = """
        bundle_id,user_id,fund_id,book_id,client_request_id,raw_instruction,
        immediate_order_request_id,conditional_rule_id,required_quantity,state,
        error_code,created_at,updated_at
    """

    def __init__(self, dsn: str | None = None, *, role: str | None = None) -> None:
        self.dsn = (
            dsn
            or os.getenv("ORDER_ORCHESTRATOR_DATABASE_URL")
            or os.getenv("CONTROL_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip()
        self.role = (
            role
            or os.getenv("ORDER_ORCHESTRATOR_DATABASE_ROLE", "svc_order_orchestrator")
        ).strip()
        if not self.dsn:
            raise PaperOrderBundleError("compound PAPER bundle database URL is required")

    def _connect(self):
        return psycopg2.connect(self.dsn, connect_timeout=8)

    def _set_role(self, cursor: Any) -> None:
        if not self.role or not self.role.replace("_", "").isalnum():
            raise PaperOrderBundleError("compound PAPER bundle database role is invalid")
        cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(self.role)))

    @classmethod
    def _row(cls, row: tuple[Any, ...] | None) -> PaperOrderBundle | None:
        if row is None:
            return None
        return PaperOrderBundle(
            bundle_id=str(row[0]),
            user_id=str(row[1]),
            fund_id=str(row[2]),
            book_id=str(row[3]),
            client_request_id=str(row[4]),
            raw_instruction=str(row[5]),
            immediate_order_request_id=str(row[6]),
            conditional_rule_id=str(row[7]) if row[7] is not None else None,
            required_quantity=str(row[8]),
            state=str(row[9]),
            error_code=str(row[10]) if row[10] is not None else None,
            created_at=row[11],
            updated_at=row[12],
        )

    def create(
        self,
        *,
        user_id: str,
        fund_id: str,
        book_id: str,
        client_request_id: str,
        raw_instruction: str,
        immediate_order_request_id: str,
        required_quantity: int,
    ) -> PaperOrderBundle:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""
                    insert into execution.user_paper_order_bundles (
                      user_id,fund_id,book_id,client_request_id,raw_instruction,
                      immediate_order_request_id,required_quantity,state
                    ) values (%s,%s,%s,%s,%s,%s,%s,'RECEIVED')
                    on conflict (user_id,client_request_id) do nothing
                    """,
                    (
                        UUID(str(user_id)), UUID(str(fund_id)), UUID(str(book_id)),
                        client_request_id, raw_instruction,
                        UUID(str(immediate_order_request_id)), required_quantity,
                    ),
                )
                cursor.execute(
                    f"""select {self._COLUMNS}
                           from execution.user_paper_order_bundles
                          where user_id=%s and client_request_id=%s
                          for update""",
                    (UUID(str(user_id)), client_request_id),
                )
                record = self._row(cursor.fetchone())
                if record is None or record.immediate_order_request_id != str(immediate_order_request_id):
                    raise PaperOrderBundleError("compound PAPER bundle admission conflict")
                return record
        except PaperOrderBundleError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise PaperOrderBundleError("could not create compound PAPER bundle") from exc

    def bind_conditional_rule(self, bundle_id: str, rule_id: str) -> PaperOrderBundle:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""update execution.user_paper_order_bundles
                           set conditional_rule_id=%s,state='WAITING_FOR_IMMEDIATE_FILL',
                               version=version+1
                         where bundle_id=%s and state='RECEIVED'
                           and (conditional_rule_id is null or conditional_rule_id=%s)
                     returning {self._COLUMNS}""",
                    (UUID(str(rule_id)), UUID(str(bundle_id)), UUID(str(rule_id))),
                )
                record = self._row(cursor.fetchone())
                if record is None:
                    raise PaperOrderBundleError("compound PAPER bundle rule binding conflict")
                return record
        except PaperOrderBundleError:
            raise
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise PaperOrderBundleError("could not bind compound PAPER rule") from exc

    def get_by_immediate_order_request(
        self, *, user_id: str, immediate_order_request_id: str
    ) -> PaperOrderBundle | None:
        """Read the bundle owned by an admitted immediate PAPER request."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    f"""select {self._COLUMNS}
                           from execution.user_paper_order_bundles
                          where user_id=%s and immediate_order_request_id=%s
                          limit 1""",
                    (UUID(str(user_id)), UUID(str(immediate_order_request_id))),
                )
                return self._row(cursor.fetchone())
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise PaperOrderBundleError(
                "could not read compound PAPER bundle"
            ) from exc

    def mark_failed(self, bundle_id: str, *, code: str, message: str) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """update execution.user_paper_order_bundles
                          set state='FAILED',error_code=%s,error_message=%s,
                              completed_at=now(),version=version+1
                        where bundle_id=%s and state not in ('FAILED','COMPLETED')""",
                    (code[:200], message[:1000], UUID(str(bundle_id))),
                )
        except (psycopg2.Error, TypeError, ValueError) as exc:
            raise PaperOrderBundleError("could not fail compound PAPER bundle") from exc


def paper_order_bundle_repository() -> PostgresPaperOrderBundleRepository:
    return PostgresPaperOrderBundleRepository()


__all__ = [
    "PaperOrderBundle",
    "PaperOrderBundleError",
    "PostgresPaperOrderBundleRepository",
    "paper_order_bundle_repository",
]
