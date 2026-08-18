#!/usr/bin/env python3
"""Provision and audit the private control-plane reference data for PAPER orders.

This job is deliberately separate from schema migration.  It performs no
broker operation: LS REST is used only to read the KRX instrument master and
historical daily bars.  The resulting catalog and a public-notice calendar,
cross-checked against those bars, are written to the EC2-local ``control``
database.

Secrets are read only from the environment and exception details from network
or database drivers are never echoed.  Re-running the job is idempotent.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "departments" / "01-research" / "collectors"
REPOSITORY = ROOT / "departments" / "01-research" / "repository"
for import_path in (str(COLLECTORS), str(REPOSITORY)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)


DEFAULT_USER_ID = UUID("00000000-0000-4000-8000-00000000cec0")
DEFAULT_FUND_ID = UUID("5c26db42-ce83-4daf-b1dc-c81680c13a6c")
DEFAULT_BOOK_ID = UUID("07d913de-9a5b-4cf5-b893-31a625445761")
DEFAULT_MIN_ACTIVE_STOCKS = 1000
KST = timezone(timedelta(hours=9))


class ReferenceBootstrapError(RuntimeError):
    """A safe-to-display, non-secret deployment failure."""


@dataclass(frozen=True)
class ReferenceModules:
    fetch_stock_master: Callable[..., tuple[list[Any], int]]
    master_row_to_record: Callable[..., Any]
    collect_calendar: Callable[..., Any]
    build_declared_draft: Callable[..., Any]
    verify_against_observed: Callable[..., tuple[int, int]]
    declared_from: date
    declared_through: date


@dataclass(frozen=True)
class Readiness:
    active_stocks: int
    session_is_trading_day: bool
    available_cash_krw: Decimal


def _modules() -> ReferenceModules:
    from calendar_collector import collect as collect_calendar
    from calendar_declared import (
        DECLARED_FROM,
        DECLARED_THROUGH,
        build_declared_draft,
        verify_against_observed,
    )
    from ls_client import fetch_stock_master
    from reference_repository import master_row_to_record

    return ReferenceModules(
        fetch_stock_master=fetch_stock_master,
        master_row_to_record=master_row_to_record,
        collect_calendar=collect_calendar,
        build_declared_draft=build_declared_draft,
        verify_against_observed=verify_against_observed,
        declared_from=DECLARED_FROM,
        declared_through=DECLARED_THROUGH,
    )


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ReferenceBootstrapError(f"required environment key is missing: {name}")
    return value


def _uuid_environment(name: str, default: UUID) -> UUID:
    raw = os.getenv(name, str(default)).strip()
    try:
        return UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReferenceBootstrapError(f"{name} must be a UUID") from exc


def _minimum_active_stocks() -> int:
    raw = os.getenv(
        "AWS_REFERENCE_MIN_ACTIVE_KRX_STOCKS", str(DEFAULT_MIN_ACTIVE_STOCKS)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReferenceBootstrapError(
            "AWS_REFERENCE_MIN_ACTIVE_KRX_STOCKS must be an integer"
        ) from exc
    if value < 1:
        raise ReferenceBootstrapError(
            "AWS_REFERENCE_MIN_ACTIVE_KRX_STOCKS must be positive"
        )
    return value


def _database_identity(dsn: str) -> tuple[str, str]:
    import psycopg2

    connection = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "select current_database(), system_identifier::text "
                "from pg_control_system()"
            )
            row = cursor.fetchone()
        if row is None:
            raise ReferenceBootstrapError("database identity query returned no row")
        return str(row[0]), str(row[1])
    finally:
        connection.close()


def assert_database_roles(control_dsn: str, market_dsn: str) -> None:
    """Require two databases in one private cluster, never one shared database."""

    expected_control = os.getenv("HEDGEFUND_CONTROL_DB_NAME", "control").strip()
    if not expected_control or expected_control == "market":
        raise ReferenceBootstrapError("control database name must differ from market")
    control_name, control_system = _database_identity(control_dsn)
    market_name, market_system = _database_identity(market_dsn)
    if control_name != expected_control:
        raise ReferenceBootstrapError("CONTROL_DATABASE_URL targets the wrong database")
    if market_name != "market":
        raise ReferenceBootstrapError("MARKET_DATABASE_URL targets the wrong database")
    if control_name == market_name or control_system != market_system:
        raise ReferenceBootstrapError(
            "control and market must be distinct databases in one private cluster"
        )


def _declared_calendar_exists(repository: Any, draft: Any) -> bool:
    with repository._conn.cursor() as cursor:
        cursor.execute(
            """
            select exists (
              select 1 from reference.market_calendar_versions
               where market=%s and content_hash=%s
            )
            """,
            (draft.market, draft.content_hash),
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def ingest_instrument_master(
    *,
    client: Any,
    repository: Any,
    modules: ReferenceModules,
    observed_at: datetime,
) -> tuple[int, int]:
    """Read LS reference metadata only and idempotently upsert the catalog."""

    rows, unclassified = modules.fetch_stock_master(client)
    if not rows:
        raise ReferenceBootstrapError("LS instrument master returned no rows")
    records = [
        modules.master_row_to_record(row, as_of=observed_at) for row in rows
    ]
    result = repository.ingest_instruments(
        records, provider="LS", as_of=observed_at
    )
    if result.attempted != len(rows):
        raise ReferenceBootstrapError("instrument master ingest count is inconsistent")
    return len(rows), int(unclassified)


def reconcile_verified_calendar(
    *,
    client: Any,
    repository: Any,
    modules: ReferenceModules,
    today: date,
) -> tuple[int, int]:
    """Install the declared calendar only after a bounded observed cross-check.

    The observed draft is persisted only on first installation.  Once the
    full-year declared version exists, adding a newer partial observed version
    would make legacy "latest version" readers lose today's row.  We still
    fetch and verify observations on every deployment, but keep the complete
    declared calendar as the latest installed version.
    """

    if not (modules.declared_from <= today <= modules.declared_through):
        raise ReferenceBootstrapError(
            "the repository has no reviewed declared calendar for the deployment year"
        )
    observed_through = today - timedelta(days=1)
    if observed_through < modules.declared_from:
        raise ReferenceBootstrapError("insufficient historical span to verify calendar")

    observed_draft = modules.collect_calendar(
        client,
        start=modules.declared_from,
        end=observed_through,
    )
    declared_draft = modules.build_declared_draft()
    observed = {
        session.trade_date: bool(session.is_trading_day)
        for session in observed_draft.sessions
    }
    overlap, overlap_trading_days = modules.verify_against_observed(
        declared_draft, observed
    )

    if not _declared_calendar_exists(repository, declared_draft):
        repository.upsert_calendar(observed_draft)
    repository.upsert_calendar(declared_draft)
    return int(overlap), int(overlap_trading_days)


def audit_order_readiness(
    control_dsn: str,
    *,
    today: date,
    minimum_active_stocks: int,
) -> Readiness:
    """Audit every control-plane prerequisite needed before accepting orders."""

    import psycopg2
    from psycopg2.extras import register_uuid

    user_id = _uuid_environment("PAPER_SEED_USER_ID", DEFAULT_USER_ID)
    fund_id = _uuid_environment("PAPER_SEED_FUND_ID", DEFAULT_FUND_ID)
    book_id = _uuid_environment("PAPER_SEED_BOOK_ID", DEFAULT_BOOK_ID)
    register_uuid()
    connection = psycopg2.connect(control_dsn, connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select count(distinct sy.symbol)
                  from reference.instruments i
                  join reference.instrument_symbols sy using (instrument_id)
                 where i.status='ACTIVE' and i.market='KRX'
                   and upper(i.asset_class)='EQUITY'
                   and upper(i.instrument_type)='STOCK'
                   and sy.provider='LS' and sy.market='KRX'
                   and sy.symbol ~ '^[0-9]{6}$'
                   and sy.valid_from<=now()
                   and (sy.valid_to is null or sy.valid_to>now())
                """
            )
            active_stocks = int(cursor.fetchone()[0])
            if active_stocks < minimum_active_stocks:
                raise ReferenceBootstrapError(
                    "active KRX stock catalog is below the deployment minimum"
                )

            cursor.execute(
                """
                select count(distinct i.instrument_id)
                  from reference.instruments i
                  join reference.instrument_symbols sy using (instrument_id)
                 where i.status='ACTIVE' and i.market='KRX'
                   and upper(i.asset_class)='EQUITY'
                   and upper(i.instrument_type)='STOCK'
                   and sy.provider='LS' and sy.market='KRX'
                   and sy.symbol='005930' and sy.valid_from<=now()
                   and (sy.valid_to is null or sy.valid_to>now())
                """
            )
            if int(cursor.fetchone()[0]) != 1:
                raise ReferenceBootstrapError(
                    "005930 must resolve to exactly one active KRX stock"
                )

            cursor.execute(
                """
                select s.is_trading_day,s.opens_at,s.closes_at
                  from reference.market_sessions s
                  join reference.market_calendar_versions v
                    on v.calendar_version_id=s.calendar_version_id
                 where v.market='KRX' and s.market='KRX'
                   and s.session_type='REGULAR' and s.trade_date=%s
                   and v.effective_from<=%s
                   and (v.effective_to is null or v.effective_to>=%s)
                 order by v.version desc
                """,
                (today, today, today),
            )
            session_rows = cursor.fetchall()
            if not session_rows:
                raise ReferenceBootstrapError("current KRX REGULAR session is missing")
            session = session_rows[0]
            is_trading_day = bool(session[0])
            if is_trading_day:
                if session[1] is None or session[2] is None or session[1] >= session[2]:
                    raise ReferenceBootstrapError(
                        "current KRX REGULAR open/close is invalid"
                    )
            elif session[1] is not None or session[2] is not None:
                raise ReferenceBootstrapError(
                    "non-trading KRX session must not expose open/close"
                )

            cursor.execute(
                """
                select
                  count(distinct fm.role) filter (
                    where fm.role in ('OWNER','TRADER') and fm.status='ACTIVE'
                      and fm.effective_from<=now()
                      and (fm.effective_to is null or fm.effective_to>now())
                  ),
                  count(distinct up.user_id),
                  count(distinct f.fund_id),
                  count(distinct b.book_id)
                  from governance.user_profiles up
                  join governance.fund_memberships fm on fm.user_id=up.user_id
                  join accounting.funds f on f.fund_id=fm.fund_id
                  join accounting.books b on b.fund_id=f.fund_id
                 where up.user_id=%s and up.status='ACTIVE'
                   and f.fund_id=%s and f.fund_code='ACC01-PAPER'
                   and f.base_currency='KRW' and f.status='ACTIVE'
                   and b.book_id=%s and b.book_code='MAIN'
                   and b.book_type='PAPER' and b.status='ACTIVE'
                """,
                (user_id, fund_id, book_id),
            )
            scope = cursor.fetchone()
            if scope is None or tuple(int(value) for value in scope) != (2, 1, 1, 1):
                raise ReferenceBootstrapError(
                    "PAPER user/fund/book OWNER+TRADER scope is incomplete"
                )

            cursor.execute(
                """
                select coalesce(sum(
                         cb.settled_amount+cb.unsettled_amount-cb.reserved_amount
                       ),0)
                  from accounting.cash_balances cb
                  join accounting.ledger_accounts la on la.account_id=cb.account_id
                 where cb.fund_id=%s and cb.book_id=%s and cb.currency='KRW'
                   and la.fund_id=%s and la.account_code='1000'
                   and la.status='ACTIVE'
                """,
                (fund_id, book_id, fund_id),
            )
            available_cash = Decimal(cursor.fetchone()[0])
            if available_cash <= 0:
                raise ReferenceBootstrapError(
                    "PAPER account has no positive available KRW cash"
                )
    finally:
        connection.close()
    return Readiness(active_stocks, is_trading_day, available_cash)


def main() -> int:
    try:
        control_dsn = _required_environment("CONTROL_DATABASE_URL")
        market_dsn = _required_environment("MARKET_DATABASE_URL")
        today = datetime.now(KST).date()
        assert_database_roles(control_dsn, market_dsn)

        from ls_client import LsRestClient
        from reference_repository import SupabaseReferenceRepository

        modules = _modules()
        client = LsRestClient()
        repository = SupabaseReferenceRepository(dsn=control_dsn)
        try:
            master_rows, unclassified = ingest_instrument_master(
                client=client,
                repository=repository,
                modules=modules,
                observed_at=datetime.now(timezone.utc),
            )
            overlap, observed_trading_days = reconcile_verified_calendar(
                client=client,
                repository=repository,
                modules=modules,
                today=today,
            )
        finally:
            repository.close()

        readiness = audit_order_readiness(
            control_dsn,
            today=today,
            minimum_active_stocks=_minimum_active_stocks(),
        )
        print(
            "PAPER reference ready: "
            f"master_rows={master_rows}, unclassified={unclassified}, "
            f"active_stocks={readiness.active_stocks}, "
            f"calendar_overlap={overlap}, "
            f"observed_trading_days={observed_trading_days}, "
            f"today_trading={str(readiness.session_is_trading_day).lower()}, "
            f"cash_positive={str(readiness.available_cash_krw > 0).lower()}"
        )
        return 0
    except ReferenceBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - sanitize vendor/driver details
        print(
            f"ERROR: PAPER reference bootstrap failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
