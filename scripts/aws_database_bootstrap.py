#!/usr/bin/env python3
"""Bootstrap the private AWS control and market databases without leaking DSNs.

The EC2 stack deliberately uses one private Timescale/PostgreSQL container and
two *databases* inside it:

* ``control`` receives the canonical ``supabase/migrations`` chain.  Hosted
  Supabase remains an identity provider only.
* ``market`` receives the canonical ``timescaledb/migrations`` chain and keeps
  tick/quote/microstructure data.

Every migration and its history row commit in the same transaction.  Existing
untracked market data is never guessed at: adoption requires an explicit flag
and a terminal-schema audit.  Connection strings and exception details are not
printed because both can contain credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid5, NAMESPACE_URL


ROOT = Path(__file__).resolve().parents[1]
CONTROL_MIGRATIONS = ROOT / "supabase" / "migrations"
MARKET_MIGRATIONS = ROOT / "timescaledb" / "migrations"

CONTROL_PATTERN = re.compile(r"^(?P<version>\d{14})_(?P<name>[a-z0-9_]+)\.sql$")
MARKET_PATTERN = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
OUTER_TRANSACTION_PATTERN = re.compile(
    r"\A\ufeff?\s*begin\s*;(?P<body>.*)commit\s*;\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
CHECKSUM_PREFIX = "hgfinance-sha256:"
CONTROL_LOCK_KEY = 8_260_001
MARKET_LOCK_KEY = 8_260_002

GENERIC_RUNTIME_LOGIN = "hgfinance_runtime"
ORDER_RUNTIME_LOGIN = "hgfinance_order_runtime"
TRADING_RUNTIME_LOGIN = "hgfinance_trading_runtime"
ACCOUNTING_RUNTIME_LOGIN = "hgfinance_accounting_runtime"
CONDITIONAL_ORCHESTRATOR_RUNTIME_LOGIN = "hgfinance_conditional_orchestrator"
CONDITIONAL_WORKER_RUNTIME_LOGIN = "hgfinance_conditional_worker"
RUNTIME_LOGIN_PASSWORD_KEYS = {
    GENERIC_RUNTIME_LOGIN: "HEDGEFUND_RUNTIME_DB_PASSWORD",
    ORDER_RUNTIME_LOGIN: "HEDGEFUND_ORDER_DB_PASSWORD",
    TRADING_RUNTIME_LOGIN: "HEDGEFUND_TRADING_DB_PASSWORD",
    ACCOUNTING_RUNTIME_LOGIN: "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
    CONDITIONAL_ORCHESTRATOR_RUNTIME_LOGIN: (
        "HEDGEFUND_CONDITIONAL_ORCHESTRATOR_DB_PASSWORD"
    ),
    CONDITIONAL_WORKER_RUNTIME_LOGIN: "HEDGEFUND_CONDITIONAL_WORKER_DB_PASSWORD",
}
RUNTIME_LOGIN_MEMBERSHIPS = {
    GENERIC_RUNTIME_LOGIN: {"service_role": True},
    ORDER_RUNTIME_LOGIN: {"svc_order_orchestrator": False},
    # Trading uses one password-authenticated pool login but selects one
    # explicit capability role per DSN: API, PAPER OMS, or outbox relay.
    # Keeping all three memberships here prevents a bootstrap rerun from
    # revoking the two valid PAPER execution boundaries as stale grants.
    TRADING_RUNTIME_LOGIN: {
        "svc_trading_api": False,
        "svc_strategy_paper_executor": False,
        "svc_trading_outbox_relay": False,
    },
    ACCOUNTING_RUNTIME_LOGIN: {"svc_accounting_ledger": False},
    CONDITIONAL_ORCHESTRATOR_RUNTIME_LOGIN: {
        "svc_conditional_rule_orchestrator": False,
    },
    CONDITIONAL_WORKER_RUNTIME_LOGIN: {"svc_conditional_rule_worker": False},
}
GENERIC_RUNTIME_SET_ROLES = (
    "svc_quant",
    "svc_dataset_builder",
    "svc_audit_api",
    "svc_qa_worker",
    "svc_qa_reproducer",
)
URL_SAFE_PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{32,}$")

# The shared runtime remains a compatibility credential for services that have
# not yet received a domain-specific LOGIN.  Keep this surface explicit: the
# generic role may operate non-order domain state and read reference/accounting
# data, but it cannot mutate any directive, order, fill, outbox, reservation,
# or ledger/projection state.
GENERIC_DML_SCHEMAS = (
    "governance",
    "workforce",
    "research",
    "strategy",
    "risk",
)
GENERIC_READ_SCHEMAS = ("api", "accounting", "reference")
GENERIC_EXECUTION_PRIVILEGES = {
    "market_snapshots": ("SELECT",),
}
MANAGED_COMPATIBILITY_POLICY_PREFIX = "hgfinance_runtime_service_role_"
TRADING_OUTBOX_RELAY_UPDATE_COLUMNS = (
    "status",
    "sent_at",
    "attempts",
    "last_error",
    "available_at",
)

DEFAULT_USER_ID = UUID("00000000-0000-4000-8000-00000000cec0")
DEFAULT_FUND_ID = UUID("3838f7d6-0c7c-4e54-85f3-316a451e7eeb")
DEFAULT_BOOK_ID = UUID("07d913de-9a5b-4cf5-b893-31a625445761")
DEFAULT_CASH_KRW = Decimal("1000000000")


class BootstrapError(RuntimeError):
    """An operator-safe bootstrap error whose message contains no secrets."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str
    sql: str


def _import_driver():
    try:
        import psycopg2
        from psycopg2 import sql
    except ModuleNotFoundError as exc:  # pragma: no cover - image contract
        raise BootstrapError("psycopg2 is required in the bootstrap image") from exc
    return psycopg2, sql


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BootstrapError(f"required environment variable is missing: {name}")
    return value


def _positive_decimal_environment(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name, "").strip()
    try:
        value = Decimal(raw) if raw else default
    except InvalidOperation as exc:
        raise BootstrapError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise BootstrapError(f"{name} must be finite and greater than zero")
    return value


def _uuid_environment(name: str, default: UUID) -> UUID:
    raw = os.environ.get(name, "").strip()
    try:
        return UUID(raw) if raw else default
    except ValueError as exc:
        raise BootstrapError(f"{name} must be a UUID") from exc


def database_name_from_dsn(dsn: str) -> str:
    """Return a URL DSN's database name without ever returning credentials."""

    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise BootstrapError("database URLs must use postgres:// or postgresql://")
    if not parsed.hostname:
        raise BootstrapError("database URL has no hostname")
    database_name = unquote(parsed.path.lstrip("/"))
    if not database_name or "/" in database_name:
        raise BootstrapError("database URL must name exactly one database")
    return database_name


def discover_migrations(directory: Path, pattern: re.Pattern[str]) -> list[Migration]:
    if not directory.is_dir():
        raise BootstrapError(f"migration directory is missing: {directory.name}")
    migrations: list[Migration] = []
    seen: set[str] = set()
    unexpected = sorted(path.name for path in directory.glob("*.sql") if not pattern.fullmatch(path.name))
    if unexpected:
        raise BootstrapError(
            f"unexpected migration filename in {directory.name}: {unexpected[0]}"
        )
    for path in sorted(directory.glob("*.sql"), key=lambda item: item.name):
        match = pattern.fullmatch(path.name)
        if match is None:  # guarded above; keeps the type checker honest
            continue
        version = match.group("version")
        if version in seen:
            raise BootstrapError(f"duplicate migration version: {version}")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BootstrapError(f"migration is not UTF-8: {path.name}") from exc
        if not text.strip():
            raise BootstrapError(f"migration is empty: {path.name}")
        seen.add(version)
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=text,
            )
        )
    if not migrations:
        raise BootstrapError(f"no migrations found in {directory.name}")
    return migrations


def migration_body(text: str) -> str:
    """Remove one repository-owned outer BEGIN/COMMIT pair, when present."""

    match = OUTER_TRANSACTION_PATTERN.fullmatch(text)
    return match.group("body").strip() if match else text.strip()


def validate_applied_prefix(
    migrations: Sequence[Migration], applied_versions: Iterable[str], label: str
) -> int:
    actual = list(applied_versions)
    expected = [migration.version for migration in migrations[: len(actual)]]
    if actual != expected:
        raise BootstrapError(f"{label} migration history is not a contiguous prefix")
    return len(actual)


def _connect(dsn: str, *, autocommit: bool = False):
    psycopg2, _ = _import_driver()
    # psycopg2 deliberately requires explicit registration for stdlib UUID;
    # seed parameters use UUID objects so type adaptation cannot depend on an
    # application module having imported/registering it first.
    from psycopg2.extras import register_uuid

    register_uuid()
    try:
        connection = psycopg2.connect(
            dsn,
            connect_timeout=10,
            application_name="hgfinance_aws_database_bootstrap",
        )
    except Exception as exc:  # deliberately do not render libpq's DSN-bearing error
        raise BootstrapError("database connection failed") from exc
    connection.autocommit = autocommit
    return connection


def ensure_control_database(market_dsn: str, control_database_name: str) -> None:
    """Create the sibling control database while holding a cluster-wide lock."""

    if not DATABASE_NAME_PATTERN.fullmatch(control_database_name):
        raise BootstrapError(
            "HEDGEFUND_CONTROL_DB_NAME must contain only letters, digits and underscore"
        )
    connection = _connect(market_dsn, autocommit=True)
    _, sql = _import_driver()
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (CONTROL_LOCK_KEY,))
            cursor.execute("select current_database()")
            market_database_name = str(cursor.fetchone()[0])
            if market_database_name == control_database_name:
                raise BootstrapError("control and market database names must differ")
            cursor.execute(
                "select datallowconn, datistemplate from pg_database where datname=%s",
                (control_database_name,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    sql.SQL("create database {} template template0 encoding 'UTF8'").format(
                        sql.Identifier(control_database_name)
                    )
                )
            elif not bool(row[0]) or bool(row[1]):
                raise BootstrapError("existing control database cannot accept connections")
            cursor.execute("select pg_advisory_unlock(%s)", (CONTROL_LOCK_KEY,))
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError("control database creation failed") from exc
    finally:
        connection.close()


def assert_distinct_databases(control_dsn: str, market_dsn: str) -> None:
    """Prove that both DSNs reach one cluster but distinct databases."""

    control = _connect(control_dsn)
    market = _connect(market_dsn)
    try:
        identities: list[tuple[str, str]] = []
        for connection in (control, market):
            with connection.cursor() as cursor:
                cursor.execute(
                    "select current_database(), system_identifier::text from pg_control_system()"
                )
                database_name, system_identifier = cursor.fetchone()
                identities.append((str(database_name), str(system_identifier)))
        if identities[0][0] == identities[1][0]:
            raise BootstrapError("control and market DSNs resolve to the same database")
        if identities[0][1] != identities[1][1]:
            raise BootstrapError("control and market DSNs must use the private EC2 cluster")
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError("database separation audit failed") from exc
    finally:
        control.close()
        market.close()


def _set_transaction_timeouts(cursor) -> None:
    timeout_seconds = os.environ.get("HGFINANCE_MIGRATION_TIMEOUT_SECONDS", "1800").strip()
    lock_timeout_seconds = os.environ.get(
        "HGFINANCE_MIGRATION_LOCK_TIMEOUT_SECONDS", "120"
    ).strip()
    if not timeout_seconds.isdigit() or int(timeout_seconds) < 60:
        raise BootstrapError("HGFINANCE_MIGRATION_TIMEOUT_SECONDS must be at least 60")
    if not lock_timeout_seconds.isdigit() or int(lock_timeout_seconds) < 1:
        raise BootstrapError(
            "HGFINANCE_MIGRATION_LOCK_TIMEOUT_SECONDS must be a positive integer"
        )
    cursor.execute("set local statement_timeout = %s", (f"{timeout_seconds}s",))
    cursor.execute("set local lock_timeout = %s", (f"{lock_timeout_seconds}s",))
    cursor.execute("set local idle_in_transaction_session_timeout = '5min'")


def _checksum_from_statements(statements: object) -> str | None:
    if not isinstance(statements, list):
        return None
    for statement in statements:
        value = str(statement)
        if value.startswith(CHECKSUM_PREFIX):
            return value[len(CHECKSUM_PREFIX) :]
    return None


def replay_control_migrations(control_dsn: str, migrations: Sequence[Migration]) -> None:
    connection = _connect(control_dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (CONTROL_LOCK_KEY,))
            cursor.execute("create schema if not exists supabase_migrations")
            cursor.execute(
                """
                create table if not exists supabase_migrations.schema_migrations (
                  version text primary key,
                  statements text[],
                  name text
                )
                """
            )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                select version, coalesce(name,''), statements
                  from supabase_migrations.schema_migrations
                 order by version
                """
            )
            rows = cursor.fetchall()
        validate_applied_prefix(migrations, (str(row[0]) for row in rows), "control")
        by_version = {migration.version: migration for migration in migrations}
        for version, name, statements in rows:
            migration = by_version[str(version)]
            if str(name) not in {"", migration.name}:
                raise BootstrapError(f"control migration name drift at {version}")
            checksum = _checksum_from_statements(statements)
            if checksum is None:
                raise BootstrapError(
                    f"control migration {version} has no repository checksum; refuse adoption"
                )
            if checksum != migration.checksum:
                raise BootstrapError(f"control migration checksum drift at {version}")

        for migration in migrations[len(rows) :]:
            try:
                with connection.cursor() as cursor:
                    _set_transaction_timeouts(cursor)
                    cursor.execute(migration_body(migration.sql))
                    cursor.execute(
                        """
                        insert into supabase_migrations.schema_migrations
                          (version, statements, name)
                        values (%s, %s, %s)
                        """,
                        (
                            migration.version,
                            [CHECKSUM_PREFIX + migration.checksum],
                            migration.name,
                        ),
                    )
                connection.commit()
            except BootstrapError:
                connection.rollback()
                raise
            except Exception as exc:
                connection.rollback()
                code = getattr(exc, "pgcode", None) or type(exc).__name__
                raise BootstrapError(
                    f"control migration failed: {migration.path.name} ({code})"
                ) from exc

        with connection.cursor() as cursor:
            cursor.execute(
                """
                select count(*) from supabase_migrations.schema_migrations
                 where version = any(%s)
                """,
                ([migration.version for migration in migrations],),
            )
            if int(cursor.fetchone()[0]) != len(migrations):
                raise BootstrapError("control migration count audit failed")
            cursor.execute(
                """
                select to_regclass('execution.user_directives') is not null,
                       to_regclass('execution.user_order_requests') is not null,
                       to_regclass('accounting.cash_balances') is not null
                """
            )
            if not all(bool(value) for value in cursor.fetchone()):
                raise BootstrapError("control terminal schema audit failed")
            cursor.execute("select pg_advisory_unlock(%s)", (CONTROL_LOCK_KEY,))
        connection.commit()
    finally:
        connection.close()


MARKET_RELATIONS = (
    "market.market_ticks",
    "market.market_quotes",
    "market.market_bars",
    "market.microstructure_features",
    "market.market_breadth",
    "market.derivative_snapshots",
    "market.data_quality_windows",
    "market.feed_gaps",
    "market.ingestion_watermarks",
    "market.archive_exports",
    "market.retention_registry",
    "market.pit_provenance",
    "market.bars_1m",
    "market.latest_quotes",
)

MARKET_FEATURE_COLUMNS = (
    "traded_value",
    "traded_volume",
    "ofi_close",
    "ofi_open",
    "ofi_intraday_std",
    "close_vs_vwap",
    "spread_close_ratio",
    "depth_imbalance_l1",
    "depth_imbalance_l10",
    "depth_imbalance_slope",
    "size_weighted_ofi",
    "book_depth_notional_l1",
    "book_depth_notional_l10",
)


def _market_has_untracked_relations(cursor) -> bool:
    cursor.execute(
        """
        select exists (
          select 1 from pg_class c
          join pg_namespace n on n.oid=c.relnamespace
          where n.nspname='market' and c.relkind in ('r','p','v','m')
        )
        """
    )
    return bool(cursor.fetchone()[0])


def audit_market_terminal_schema(cursor) -> None:
    cursor.execute(
        "select " + ",".join("to_regclass(%s) is not null" for _ in MARKET_RELATIONS),
        MARKET_RELATIONS,
    )
    if not all(bool(value) for value in cursor.fetchone()):
        raise BootstrapError("market terminal relation audit failed")
    cursor.execute(
        """
        select column_name from information_schema.columns
         where table_schema='market' and table_name='microstructure_features'
           and column_name = any(%s)
        """,
        (list(MARKET_FEATURE_COLUMNS),),
    )
    if {str(row[0]) for row in cursor.fetchall()} != set(MARKET_FEATURE_COLUMNS):
        raise BootstrapError("market terminal feature-column audit failed")
    cursor.execute(
        """
        select conname from pg_constraint
         where conrelid='market.microstructure_features'::regclass
           and conname = any(%s)
        """,
        (
            [
                "microstructure_v4_signed_flow_bounds",
                "microstructure_v4_depth_bounds",
                "microstructure_v5_signed_flow_bounds",
                "microstructure_v5_depth_capacity_nonnegative",
            ],
        ),
    )
    if len(cursor.fetchall()) != 4:
        raise BootstrapError("market terminal constraint audit failed")
    cursor.execute(
        """
        select count(*)
          from timescaledb_information.jobs
         where hypertable_schema='market'
           and hypertable_name = any(%s)
           and (proc_name like '%%compress%%' or proc_name like '%%columnstore%%')
           and max_runtime > interval '0'
           and max_runtime <= interval '20 minutes'
        """,
        (
            [
                "market_ticks",
                "market_quotes",
                "market_bars",
                "microstructure_features",
                "derivative_snapshots",
            ],
        ),
    )
    if int(cursor.fetchone()[0]) < 5:
        raise BootstrapError("market compression runtime audit failed")


def replay_market_migrations(
    market_dsn: str,
    migrations: Sequence[Migration],
    *,
    adopt_existing: bool,
) -> None:
    connection = _connect(market_dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (MARKET_LOCK_KEY,))
            cursor.execute("create schema if not exists hgfinance_migrations")
            cursor.execute(
                """
                create table if not exists hgfinance_migrations.schema_migrations (
                  version text primary key,
                  name text not null,
                  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
                  adopted boolean not null default false,
                  applied_at timestamptz not null default now()
                )
                """
            )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                select version,name,sha256,adopted
                  from hgfinance_migrations.schema_migrations order by version
                """
            )
            rows = cursor.fetchall()
            if not rows and _market_has_untracked_relations(cursor):
                if not adopt_existing:
                    raise BootstrapError(
                        "market schema has untracked data; use explicit terminal-schema adoption"
                    )
                audit_market_terminal_schema(cursor)
                for migration in migrations:
                    cursor.execute(
                        """
                        insert into hgfinance_migrations.schema_migrations
                          (version,name,sha256,adopted)
                        values (%s,%s,%s,true)
                        """,
                        (migration.version, migration.name, migration.checksum),
                    )
                connection.commit()
                rows = [
                    (migration.version, migration.name, migration.checksum, True)
                    for migration in migrations
                ]

        validate_applied_prefix(migrations, (str(row[0]) for row in rows), "market")
        by_version = {migration.version: migration for migration in migrations}
        for version, name, checksum, _adopted in rows:
            migration = by_version[str(version)]
            if str(name) != migration.name:
                raise BootstrapError(f"market migration name drift at {version}")
            if str(checksum) != migration.checksum:
                raise BootstrapError(f"market migration checksum drift at {version}")

        for migration in migrations[len(rows) :]:
            try:
                with connection.cursor() as cursor:
                    _set_transaction_timeouts(cursor)
                    cursor.execute(migration_body(migration.sql))
                    cursor.execute(
                        """
                        insert into hgfinance_migrations.schema_migrations
                          (version,name,sha256,adopted)
                        values (%s,%s,%s,false)
                        """,
                        (migration.version, migration.name, migration.checksum),
                    )
                connection.commit()
            except BootstrapError:
                connection.rollback()
                raise
            except Exception as exc:
                connection.rollback()
                code = getattr(exc, "pgcode", None) or type(exc).__name__
                raise BootstrapError(
                    f"market migration failed: {migration.path.name} ({code})"
                ) from exc

        with connection.cursor() as cursor:
            audit_market_terminal_schema(cursor)
            cursor.execute("select pg_advisory_unlock(%s)", (MARKET_LOCK_KEY,))
        connection.commit()
    finally:
        connection.close()


def runtime_login_passwords() -> dict[str, str]:
    """Load non-disclosing, distinct URL-safe runtime passwords."""

    passwords: dict[str, str] = {}
    invalid: list[str] = []
    for login, environment_key in RUNTIME_LOGIN_PASSWORD_KEYS.items():
        value = os.environ.get(environment_key, "").strip()
        if URL_SAFE_PASSWORD_PATTERN.fullmatch(value) is None:
            invalid.append(environment_key)
        else:
            passwords[login] = value
    if invalid:
        raise BootstrapError(
            "runtime database passwords must be at least 32 URL-safe characters: "
            + ", ".join(invalid)
        )
    if len(set(passwords.values())) != len(passwords):
        raise BootstrapError("runtime database passwords must all be distinct")
    return passwords


def _application_schemas(cursor) -> list[str]:
    cursor.execute(
        """
        select nspname
         from pg_namespace
         where nspname not in ('information_schema','public')
           and left(nspname,3) <> 'pg_'
         order by nspname
        """
    )
    return [str(row[0]) for row in cursor.fetchall()]


def _schema_relations(cursor, schema_name: str) -> list[tuple[str, bool]]:
    cursor.execute(
        """
        select relation.relname,relation.relrowsecurity
          from pg_class relation
          join pg_namespace namespace on namespace.oid=relation.relnamespace
         where namespace.nspname=%s and relation.relkind in ('r','p')
         order by relation.relname
        """,
        (schema_name,),
    )
    return [(str(row[0]), bool(row[1])) for row in cursor.fetchall()]


def _replace_compatibility_policies(
    cursor,
    *,
    schema_name: str,
    privileges_by_table: dict[str, tuple[str, ...]],
) -> None:
    """Install exact service_role RLS policies for the managed table surface."""

    _, sql = _import_driver()
    relations = _schema_relations(cursor, schema_name)
    relation_names = {name for name, _rls in relations}
    missing = set(privileges_by_table) - relation_names
    if missing:
        raise BootstrapError(
            f"runtime compatibility relation is missing: {schema_name}.{sorted(missing)[0]}"
        )
    for table_name, row_security in relations:
        relation = sql.SQL("{}.{}").format(
            sql.Identifier(schema_name), sql.Identifier(table_name)
        )
        for command in ("select", "insert", "update", "delete"):
            policy_name = MANAGED_COMPATIBILITY_POLICY_PREFIX + command
            cursor.execute(
                sql.SQL("drop policy if exists {} on {}").format(
                    sql.Identifier(policy_name), relation
                )
            )
        if not row_security:
            continue
        for privilege in privileges_by_table.get(table_name, ()):
            command = privilege.casefold()
            policy_name = MANAGED_COMPATIBILITY_POLICY_PREFIX + command
            if command == "select" or command == "delete":
                predicate = sql.SQL("using (true)")
            elif command == "insert":
                predicate = sql.SQL("with check (true)")
            elif command == "update":
                predicate = sql.SQL("using (true) with check (true)")
            else:  # constants above own this vocabulary
                raise BootstrapError("runtime compatibility privilege is invalid")
            cursor.execute(
                sql.SQL(
                    "create policy {} on {} for {} to service_role {}"
                ).format(
                    sql.Identifier(policy_name),
                    relation,
                    sql.SQL(command),
                    predicate,
                )
            )


def _configure_control_compatibility_role(cursor) -> None:
    """Give the generic login a bounded, RLS-aware compatibility surface."""

    _, sql = _import_driver()
    cursor.execute(
        """
        select rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,
               rolbypassrls
          from pg_roles where rolname='service_role'
        """
    )
    role = cursor.fetchone()
    if role is None or tuple(bool(value) for value in role) != (
        False,
        False,
        False,
        False,
        False,
        False,
    ):
        raise BootstrapError("service_role compatibility role is unsafe")
    cursor.execute("select current_user")
    object_owner = sql.Identifier(str(cursor.fetchone()[0]))
    service_role = sql.Identifier("service_role")

    for schema_name in (*GENERIC_DML_SCHEMAS, *GENERIC_READ_SCHEMAS, "execution"):
        cursor.execute(
            "select 1 from pg_namespace where nspname=%s", (schema_name,)
        )
        if cursor.fetchone() is None:
            raise BootstrapError(
                f"runtime compatibility schema is missing: {schema_name}"
            )
        cursor.execute(
            sql.SQL("grant usage on schema {} to {}").format(
                sql.Identifier(schema_name), service_role
            )
        )

    full_dml = ("SELECT", "INSERT", "UPDATE", "DELETE")
    for schema_name in GENERIC_DML_SCHEMAS:
        schema = sql.Identifier(schema_name)
        cursor.execute(
            sql.SQL("revoke all privileges on all tables in schema {} from {}").format(
                schema, service_role
            )
        )
        cursor.execute(
            sql.SQL(
                "grant select,insert,update,delete on all tables in schema {} to {}"
            ).format(schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "revoke all privileges on all sequences in schema {} from {}"
            ).format(schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "grant usage,select on all sequences in schema {} to {}"
            ).format(schema, service_role)
        )
        relations = _schema_relations(cursor, schema_name)
        _replace_compatibility_policies(
            cursor,
            schema_name=schema_name,
            privileges_by_table={name: full_dml for name, _rls in relations},
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "revoke all on tables from {}"
            ).format(object_owner, schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "grant select,insert,update,delete on tables to {}"
            ).format(object_owner, schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "revoke all on sequences from {}"
            ).format(object_owner, schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "grant usage,select on sequences to {}"
            ).format(object_owner, schema, service_role)
        )

    for schema_name in GENERIC_READ_SCHEMAS:
        schema = sql.Identifier(schema_name)
        cursor.execute(
            sql.SQL("revoke all privileges on all tables in schema {} from {}").format(
                schema, service_role
            )
        )
        cursor.execute(
            sql.SQL("grant select on all tables in schema {} to {}").format(
                schema, service_role
            )
        )
        cursor.execute(
            sql.SQL(
                "revoke all privileges on all sequences in schema {} from {}"
            ).format(schema, service_role)
        )
        relations = _schema_relations(cursor, schema_name)
        _replace_compatibility_policies(
            cursor,
            schema_name=schema_name,
            privileges_by_table={
                name: ("SELECT",) for name, _rls in relations
            },
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "revoke all on tables from {}"
            ).format(object_owner, schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "grant select on tables to {}"
            ).format(object_owner, schema, service_role)
        )
        cursor.execute(
            sql.SQL(
                "alter default privileges for role {} in schema {} "
                "revoke all on sequences from {}"
            ).format(object_owner, schema, service_role)
        )

    execution = sql.Identifier("execution")
    cursor.execute(
        sql.SQL("revoke all privileges on all tables in schema {} from {}").format(
            execution, service_role
        )
    )
    cursor.execute(
        sql.SQL("revoke all privileges on all sequences in schema {} from {}").format(
            execution, service_role
        )
    )
    for table_name, privileges in GENERIC_EXECUTION_PRIVILEGES.items():
        cursor.execute(
            sql.SQL("grant {} on table {}.{} to {}").format(
                sql.SQL(",").join(sql.SQL(privilege) for privilege in privileges),
                execution,
                sql.Identifier(table_name),
                service_role,
            )
        )
    _replace_compatibility_policies(
        cursor,
        schema_name="execution",
        privileges_by_table=GENERIC_EXECUTION_PRIVILEGES,
    )
    cursor.execute(
        sql.SQL(
            "alter default privileges for role {} in schema execution "
            "revoke all on tables from {}"
        ).format(object_owner, service_role)
    )
    cursor.execute(
        sql.SQL(
            "alter default privileges for role {} in schema execution "
            "revoke all on sequences from {}"
        ).format(object_owner, service_role)
    )


def _configure_critical_runtime_roles(cursor) -> None:
    """Finish the Trading relay and Accounting service-role surfaces.

    The migration roles are NOLOGIN/NOINHERIT capabilities.  AWS LOGINs select
    exactly one of them; the generic compatibility role receives none of these
    grants.  No default privileges are installed here so future execution or
    accounting objects remain denied until explicitly reviewed.
    """

    _, sql = _import_driver()
    for role_name in ("svc_trading_api", "svc_accounting_ledger"):
        cursor.execute(
            """
            select rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,
                   rolreplication,rolbypassrls
              from pg_roles where rolname=%s
            """,
            (role_name,),
        )
        role = cursor.fetchone()
        if role is None or any(bool(value) for value in role):
            raise BootstrapError(f"critical runtime role is unsafe: {role_name}")

    trading_role = sql.Identifier("svc_trading_api")
    outbox = sql.SQL("execution.outbox")
    cursor.execute(
        sql.SQL("grant select on table {} to {}").format(outbox, trading_role)
    )
    cursor.execute(
        sql.SQL("grant update ({}) on table {} to {}").format(
            sql.SQL(",").join(
                sql.Identifier(column)
                for column in TRADING_OUTBOX_RELAY_UPDATE_COLUMNS
            ),
            outbox,
            trading_role,
        )
    )
    for policy_name, command, predicate in (
        ("outbox_svc_trading_api_relay_select", "select", "using (true)"),
        (
            "outbox_svc_trading_api_relay_update",
            "update",
            "using (true) with check (true)",
        ),
    ):
        cursor.execute(
            sql.SQL("drop policy if exists {} on {}").format(
                sql.Identifier(policy_name), outbox
            )
        )
        cursor.execute(
            sql.SQL("create policy {} on {} for {} to {} {}").format(
                sql.Identifier(policy_name),
                outbox,
                sql.SQL(command),
                trading_role,
                sql.SQL(predicate),
            )
        )

    accounting_role = sql.Identifier("svc_accounting_ledger")
    accounting_schema = sql.Identifier("accounting")
    cursor.execute(
        sql.SQL("grant usage on schema {} to {}").format(
            accounting_schema, accounting_role
        )
    )
    # Views/read models need SELECT; mutations are granted only on physical
    # Accounting-owned relations so a future view cannot become a write path.
    cursor.execute(
        sql.SQL("grant select on all tables in schema {} to {}").format(
            accounting_schema, accounting_role
        )
    )
    cursor.execute(
        sql.SQL("grant usage,select on all sequences in schema {} to {}").format(
            accounting_schema, accounting_role
        )
    )
    policy_name = "hgfinance_runtime_svc_accounting_ledger_all"
    for table_name, row_security in _schema_relations(cursor, "accounting"):
        relation = sql.SQL("{}.{}").format(
            accounting_schema, sql.Identifier(table_name)
        )
        cursor.execute(
            sql.SQL("grant insert,update,delete on table {} to {}").format(
                relation, accounting_role
            )
        )
        if not row_security:
            continue
        cursor.execute(
            sql.SQL("drop policy if exists {} on {}").format(
                sql.Identifier(policy_name), relation
            )
        )
        cursor.execute(
            sql.SQL(
                "create policy {} on {} for all to {} "
                "using (true) with check (true)"
            ).format(sql.Identifier(policy_name), relation, accounting_role)
        )


def _revoke_direct_object_privileges(cursor, login: str) -> None:
    """Remove stale direct grants; runtime access must come from one role."""

    _, sql = _import_driver()
    role = sql.Identifier(login)
    for schema_name in _application_schemas(cursor):
        schema = sql.Identifier(schema_name)
        cursor.execute(
            sql.SQL("revoke all privileges on schema {} from {}").format(
                schema, role
            )
        )
        cursor.execute(
            sql.SQL(
                "revoke all privileges on all tables in schema {} from {}"
            ).format(schema, role)
        )
        cursor.execute(
            sql.SQL(
                "revoke all privileges on all sequences in schema {} from {}"
            ).format(schema, role)
        )
        cursor.execute(
            sql.SQL(
                "revoke all privileges on all functions in schema {} from {}"
            ).format(schema, role)
        )


def _configure_login(
    cursor,
    *,
    login: str,
    password: str,
    inherited_membership: bool,
) -> None:
    _, sql = _import_driver()
    identifier = sql.Identifier(login)
    cursor.execute("select 1 from pg_roles where rolname=%s", (login,))
    if cursor.fetchone() is None:
        cursor.execute(sql.SQL("create role {} nologin").format(identifier))
    inherit_keyword = sql.SQL("inherit" if inherited_membership else "noinherit")
    cursor.execute(
        sql.SQL(
            "alter role {} with login {} nosuperuser nocreatedb nocreaterole "
            "noreplication nobypassrls"
        ).format(identifier, inherit_keyword)
    )
    # psycopg quotes this parameter as a SQL string literal; it never becomes
    # an identifier or an interpolated/logged statement fragment.
    cursor.execute(
        sql.SQL("alter role {} password %s").format(identifier), (password,)
    )


def _memberships_for_login(login: str) -> dict[str, bool]:
    memberships = dict(RUNTIME_LOGIN_MEMBERSHIPS[login])
    if login == GENERIC_RUNTIME_LOGIN:
        memberships.update({role: False for role in GENERIC_RUNTIME_SET_ROLES})
    return memberships


def _replace_login_memberships(
    cursor,
    *,
    login: str,
) -> None:
    _, sql = _import_driver()
    expected = _memberships_for_login(login)
    cursor.execute(
        """
        select granted.rolname
          from pg_auth_members membership
          join pg_roles granted on granted.oid=membership.roleid
          join pg_roles member on member.oid=membership.member
         where member.rolname=%s
        """,
        (login,),
    )
    for row in cursor.fetchall():
        existing = str(row[0])
        if existing not in expected:
            cursor.execute(
                sql.SQL("revoke {} from {}").format(
                    sql.Identifier(existing), sql.Identifier(login)
                )
            )
    for granted_role, inherit_option in expected.items():
        cursor.execute(
            sql.SQL("grant {} to {} with set true, inherit {}").format(
                sql.Identifier(granted_role),
                sql.Identifier(login),
                sql.SQL("true" if inherit_option else "false"),
            )
        )


def _audit_runtime_logins(cursor) -> None:
    for login in RUNTIME_LOGIN_MEMBERSHIPS:
        expected_memberships = _memberships_for_login(login)
        inherited_membership = login == GENERIC_RUNTIME_LOGIN
        cursor.execute(
            """
            select rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls,rolinherit
              from pg_roles where rolname=%s
            """,
            (login,),
        )
        row = cursor.fetchone()
        if row is None or tuple(bool(value) for value in row) != (
            True,
            False,
            False,
            False,
            False,
            False,
            inherited_membership,
        ):
            raise BootstrapError(f"runtime login safety audit failed: {login}")
        cursor.execute(
            """
            with recursive settable(roleid) as (
              select membership.roleid
                from pg_auth_members membership
                join pg_roles member on member.oid=membership.member
               where member.rolname=%s and membership.set_option
              union
              select membership.roleid
                from pg_auth_members membership
                join settable prior on prior.roleid=membership.member
               where membership.set_option
            )
            select role.rolname
              from settable
              join pg_roles role on role.oid=settable.roleid
             order by role.rolname
            """,
            (login,),
        )
        if [str(member[0]) for member in cursor.fetchall()] != sorted(
            expected_memberships
        ):
            raise BootstrapError(f"runtime SET ROLE boundary audit failed: {login}")
        cursor.execute(
            """
            select membership.set_option,membership.inherit_option
              from pg_auth_members membership
              join pg_roles granted on granted.oid=membership.roleid
              join pg_roles member on member.oid=membership.member
             where granted.rolname=any(%s) and member.rolname=%s
             order by granted.rolname
            """,
            (list(expected_memberships), login),
        )
        memberships = cursor.fetchall()
        expected_options = [
            (True, expected_memberships[role])
            for role in sorted(expected_memberships)
        ]
        observed_options = [
            tuple(bool(value) for value in membership)
            for membership in memberships
        ]
        if observed_options != expected_options:
            raise BootstrapError(f"runtime role membership audit failed: {login}")


def provision_runtime_logins(
    control_dsn: str,
    market_dsn: str,
    passwords: dict[str, str],
) -> None:
    """Install least-privilege LOGIN roles after both migration chains."""

    if set(passwords) != set(RUNTIME_LOGIN_PASSWORD_KEYS):
        raise BootstrapError("runtime database password set is incomplete")
    _, sql = _import_driver()
    control_database_name = database_name_from_dsn(control_dsn)
    market_database_name = database_name_from_dsn(market_dsn)
    control = _connect(control_dsn)
    try:
        with control.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "revoke connect,temporary on database {} from public"
                ).format(sql.Identifier(control_database_name))
            )
            _configure_control_compatibility_role(cursor)
            _configure_critical_runtime_roles(cursor)
            for login, password in passwords.items():
                inherited = login == GENERIC_RUNTIME_LOGIN
                _configure_login(
                    cursor,
                    login=login,
                    password=password,
                    inherited_membership=inherited,
                )
                _revoke_direct_object_privileges(cursor, login)
                cursor.execute(
                    sql.SQL("revoke all privileges on database {} from {}").format(
                        sql.Identifier(control_database_name), sql.Identifier(login)
                    )
                )
                cursor.execute(
                    sql.SQL("grant connect on database {} to {}").format(
                        sql.Identifier(control_database_name), sql.Identifier(login)
                    )
                )
                _replace_login_memberships(cursor, login=login)
            _audit_runtime_logins(cursor)
        control.commit()
    except BootstrapError:
        control.rollback()
        raise
    except Exception as exc:
        control.rollback()
        raise BootstrapError("runtime control login provisioning failed") from exc
    finally:
        control.close()

    market = _connect(market_dsn)
    try:
        with market.cursor() as cursor:
            generic = sql.Identifier(GENERIC_RUNTIME_LOGIN)
            cursor.execute(
                sql.SQL(
                    "revoke connect,temporary on database {} from public"
                ).format(sql.Identifier(market_database_name))
            )
            for login in passwords:
                _revoke_direct_object_privileges(cursor, login)
                cursor.execute(
                    sql.SQL("revoke all privileges on database {} from {}").format(
                        sql.Identifier(market_database_name), sql.Identifier(login)
                    )
                )
            cursor.execute(
                sql.SQL("grant connect on database {} to {}").format(
                    sql.Identifier(market_database_name), generic
                )
            )
            cursor.execute(
                sql.SQL("grant usage on schema market to {}").format(generic)
            )
            cursor.execute(
                sql.SQL(
                    "grant select,insert,update on all tables in schema market to {}"
                ).format(generic)
            )
            cursor.execute(
                sql.SQL(
                    "grant usage,select on all sequences in schema market to {}"
                ).format(generic)
            )
            cursor.execute(
                sql.SQL(
                    "grant execute on all functions in schema market to {}"
                ).format(generic)
            )
            cursor.execute("select current_user")
            object_owner = sql.Identifier(str(cursor.fetchone()[0]))
            cursor.execute(
                sql.SQL(
                    "alter default privileges for role {} in schema market "
                    "grant select,insert,update on tables to {}"
                ).format(object_owner, generic)
            )
            cursor.execute(
                sql.SQL(
                    "alter default privileges for role {} in schema market "
                    "grant usage,select on sequences to {}"
                ).format(object_owner, generic)
            )
            cursor.execute(
                sql.SQL(
                    "alter default privileges for role {} in schema market "
                    "grant execute on functions to {}"
                ).format(object_owner, generic)
            )
            cursor.execute(
                "select has_schema_privilege(%s,'market','USAGE')",
                (GENERIC_RUNTIME_LOGIN,),
            )
            if not bool(cursor.fetchone()[0]):
                raise BootstrapError("generic market runtime privilege audit failed")
        market.commit()
    except BootstrapError:
        market.rollback()
        raise
    except Exception as exc:
        market.rollback()
        raise BootstrapError("runtime market login provisioning failed") from exc
    finally:
        market.close()


ACCOUNT_CHART = (
    ("1000", "Cash", "ASSET"),
    ("1100", "Securities", "ASSET"),
    ("1200", "Receivable", "ASSET"),
    ("2000", "Payable", "LIABILITY"),
    ("2100", "Fee payable", "LIABILITY"),
    ("3000", "Capital", "EQUITY"),
    ("4000", "Realized PnL", "INCOME"),
    ("4100", "Unrealized PnL", "INCOME"),
    ("5000", "Commission expense", "EXPENSE"),
    ("5100", "Tax expense", "EXPENSE"),
    ("5200", "Management fee expense", "EXPENSE"),
    ("5300", "Performance fee expense", "EXPENSE"),
)


def _assert_single_scope_row(rows: Sequence[Sequence[object]], label: str) -> None:
    if len(rows) > 1:
        raise BootstrapError(f"PAPER seed {label} identity collides with existing data")


def _assert_adoptable_paper_fund(
    row: Sequence[object], expected_fund_id: UUID
) -> None:
    """Accept an existing configured Fund without rewriting team-owned identity."""

    if (
        len(row) < 4
        or UUID(str(row[0])) != expected_fund_id
        or str(row[2]) != "KRW"
        or str(row[3]) != "ACTIVE"
    ):
        raise BootstrapError("PAPER seed fund is not an active KRW fund")


def _assert_adoptable_paper_book(
    row: Sequence[object], expected_fund_id: UUID, expected_book_id: UUID
) -> None:
    """Accept only the configured Fund's already-active PAPER Book."""

    if (
        len(row) < 5
        or UUID(str(row[0])) != expected_book_id
        or UUID(str(row[1])) != expected_fund_id
        or str(row[3]) != "PAPER"
        or str(row[4]) != "ACTIVE"
    ):
        raise BootstrapError("PAPER seed book is not an active PAPER book")


def _post_seed_journal(
    cursor,
    *,
    fund_id: UUID,
    book_id: UUID,
    cash_account_id: UUID,
    capital_account_id: UUID,
    amount: Decimal,
    source_event_id: str,
) -> UUID:
    journal_id = uuid5(NAMESPACE_URL, "hgfinance:" + source_event_id)
    trace_id = uuid5(NAMESPACE_URL, "hgfinance-trace:" + source_event_id)
    cursor.execute(
        """
        insert into accounting.journals (
          journal_id,fund_id,book_id,event_type,source_event_id,effective_at,
          accounting_date,base_currency,status,created_by_service,trace_id,posted_at
        ) values (%s,%s,%s,'PAPER_CAPITAL_SEED',%s,now(),current_date,'KRW',
                  'DRAFT','aws-paper-bootstrap',%s,null)
        """,
        (journal_id, fund_id, book_id, source_event_id, trace_id),
    )
    cursor.execute(
        """
        insert into accounting.journal_lines (
          journal_id,account_id,line_no,debit,credit,currency,fx_rate,metadata
        ) values
          (%s,%s,1,%s,0,'KRW',1,'{"seed":"aws-paper"}'::jsonb),
          (%s,%s,2,0,%s,'KRW',1,'{"seed":"aws-paper"}'::jsonb)
        """,
        (journal_id, cash_account_id, amount, journal_id, capital_account_id, amount),
    )
    # Posting is an explicit state transition after both balanced lines exist;
    # the canonical trigger rejects lines inserted into an already-POSTED row.
    cursor.execute(
        "update accounting.journals set status='POSTED' where journal_id=%s",
        (journal_id,),
    )
    return journal_id


def seed_paper_principal(control_dsn: str, *, top_up_cash: bool) -> None:
    user_id = _uuid_environment("PAPER_SEED_USER_ID", DEFAULT_USER_ID)
    fund_id = _uuid_environment("PAPER_SEED_FUND_ID", DEFAULT_FUND_ID)
    book_id = _uuid_environment("PAPER_SEED_BOOK_ID", DEFAULT_BOOK_ID)
    cash_floor = _positive_decimal_environment("PAPER_SEED_CASH_KRW", DEFAULT_CASH_KRW)
    connection = _connect(control_dsn)
    try:
        with connection.cursor() as cursor:
            _set_transaction_timeouts(cursor)
            cursor.execute(
                """
                select fund_id,fund_code,base_currency,status
                  from accounting.funds
                 where fund_id=%s
                 for update
                """,
                (fund_id,),
            )
            fund_row = cursor.fetchone()
            if fund_row is not None:
                _assert_adoptable_paper_fund(fund_row, fund_id)
            else:
                # A team-owned Fund may legitimately use another code. Only
                # the exact configured UUID is adoptable; a separate Fund
                # already owning the bootstrap code is still a hard conflict.
                cursor.execute(
                    """
                    select fund_id from accounting.funds
                     where fund_code='ACC01-PAPER'
                     for update
                    """
                )
                if cursor.fetchone() is not None:
                    raise BootstrapError(
                        "PAPER seed fund code conflicts with existing data"
                    )
                cursor.execute(
                    """
                    insert into accounting.funds
                      (fund_id,fund_code,name,base_currency,inception_date,status)
                    values (%s,'ACC01-PAPER','AWS PAPER Account','KRW',current_date,'ACTIVE')
                    """,
                    (fund_id,),
                )

            cursor.execute(
                """
                select book_id,fund_id,book_code,book_type,status
                  from accounting.books
                 where book_id=%s
                 for update
                """,
                (book_id,),
            )
            book_row = cursor.fetchone()
            if book_row is not None:
                _assert_adoptable_paper_book(book_row, fund_id, book_id)
            else:
                cursor.execute(
                    """
                    select book_id from accounting.books
                     where fund_id=%s and book_code='MAIN'
                     for update
                    """,
                    (fund_id,),
                )
                if cursor.fetchone() is not None:
                    raise BootstrapError(
                        "PAPER seed book code conflicts with existing data"
                    )
                cursor.execute(
                    """
                    insert into accounting.books
                      (book_id,fund_id,book_code,name,book_type,status)
                    values (%s,%s,'MAIN','Main PAPER Book','PAPER','ACTIVE')
                    """,
                    (book_id, fund_id),
                )

            cursor.execute(
                """
                insert into governance.user_profiles (
                  user_id,display_name,timezone,status
                ) values (%s,'AWS PAPER Operator','Asia/Seoul','ACTIVE')
                on conflict (user_id) do update
                  set status='ACTIVE'
                """,
                (user_id,),
            )
            for role in ("OWNER", "TRADER"):
                cursor.execute(
                    """
                    insert into governance.fund_memberships
                      (fund_id,user_id,role,status,effective_from,effective_to)
                    values (%s,%s,%s,'ACTIVE',now(),null)
                    on conflict (fund_id,user_id,role) do update
                      set status='ACTIVE',
                          effective_from=least(governance.fund_memberships.effective_from,now()),
                          effective_to=null
                    """,
                    (fund_id, user_id, role),
                )

            accounts: dict[str, UUID] = {}
            for account_code, name, account_type in ACCOUNT_CHART:
                cursor.execute(
                    """
                    insert into accounting.ledger_accounts
                      (fund_id,account_code,name,account_type,currency,status)
                    values (%s,%s,%s,%s,'KRW','ACTIVE')
                    on conflict (fund_id,account_code) do nothing
                    """,
                    (fund_id, account_code, name, account_type),
                )
                cursor.execute(
                    """
                    select account_id,account_type,currency,status
                      from accounting.ledger_accounts
                     where fund_id=%s and account_code=%s
                    """,
                    (fund_id, account_code),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or str(row[1]) != account_type
                    or str(row[2]) != "KRW"
                    or str(row[3]) != "ACTIVE"
                ):
                    raise BootstrapError(
                        f"PAPER seed account contract mismatch: {account_code}"
                    )
                accounts[account_code] = UUID(str(row[0]))

            cursor.execute(
                """
                select cash_balance_id,settled_amount,unsettled_amount,reserved_amount,version
                  from accounting.cash_balances
                 where fund_id=%s and book_id=%s and account_id=%s and currency='KRW'
                 for update
                """,
                (fund_id, book_id, accounts["1000"]),
            )
            cash_row = cursor.fetchone()
            if cash_row is None:
                source_event_id = f"aws-paper-principal-v1:{fund_id}:{book_id}"
                journal_id = _post_seed_journal(
                    cursor,
                    fund_id=fund_id,
                    book_id=book_id,
                    cash_account_id=accounts["1000"],
                    capital_account_id=accounts["3000"],
                    amount=cash_floor,
                    source_event_id=source_event_id,
                )
                cursor.execute(
                    """
                    insert into accounting.cash_balances (
                      fund_id,book_id,account_id,currency,settled_amount,
                      unsettled_amount,reserved_amount,last_journal_id,version,as_of
                    ) values (%s,%s,%s,'KRW',%s,0,0,%s,1,now())
                    """,
                    (fund_id, book_id, accounts["1000"], cash_floor, journal_id),
                )
            else:
                available = Decimal(cash_row[1]) + Decimal(cash_row[2]) - Decimal(cash_row[3])
                if available < cash_floor and not top_up_cash:
                    raise BootstrapError(
                        "existing PAPER cash is below the configured floor; explicit top-up required"
                    )
                if available < cash_floor:
                    delta = cash_floor - available
                    next_version = int(cash_row[4]) + 1
                    source_event_id = (
                        f"aws-paper-topup-v1:{fund_id}:{book_id}:{next_version}:{cash_floor}"
                    )
                    journal_id = _post_seed_journal(
                        cursor,
                        fund_id=fund_id,
                        book_id=book_id,
                        cash_account_id=accounts["1000"],
                        capital_account_id=accounts["3000"],
                        amount=delta,
                        source_event_id=source_event_id,
                    )
                    cursor.execute(
                        """
                        update accounting.cash_balances
                           set settled_amount=settled_amount+%s,
                               last_journal_id=%s,version=%s,as_of=now(),updated_at=now()
                         where cash_balance_id=%s
                        """,
                        (delta, journal_id, next_version, cash_row[0]),
                    )

            cursor.execute(
                """
                select count(*) from governance.fund_memberships
                 where user_id=%s and fund_id=%s and role in ('OWNER','TRADER')
                   and status='ACTIVE' and effective_from<=now()
                   and (effective_to is null or effective_to>now())
                """,
                (user_id, fund_id),
            )
            if int(cursor.fetchone()[0]) != 2:
                raise BootstrapError("PAPER seed membership audit failed")
        connection.commit()
    except BootstrapError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        code = getattr(exc, "pgcode", None) or type(exc).__name__
        raise BootstrapError(f"PAPER principal seed failed ({code})") from exc
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adopt-existing-market",
        action="store_true",
        help="record an untracked market schema only after a terminal-schema audit",
    )
    parser.add_argument(
        "--seed-paper-principal",
        action="store_true",
        help="provision the configured local demo identity, PAPER fund/book and cash",
    )
    parser.add_argument(
        "--top-up-paper-cash",
        action="store_true",
        help="explicitly raise existing PAPER available cash to PAPER_SEED_CASH_KRW",
    )
    return parser


def run(arguments: argparse.Namespace) -> None:
    control_dsn = _required_environment("CONTROL_DATABASE_URL")
    market_dsn = _required_environment("MARKET_DATABASE_URL")
    runtime_passwords = runtime_login_passwords()
    control_database_name = os.environ.get("HEDGEFUND_CONTROL_DB_NAME", "control").strip()
    if database_name_from_dsn(control_dsn) != control_database_name:
        raise BootstrapError("CONTROL_DATABASE_URL does not target HEDGEFUND_CONTROL_DB_NAME")
    market_database_name = database_name_from_dsn(market_dsn)
    if market_database_name != "market":
        raise BootstrapError("MARKET_DATABASE_URL must target the market database")

    control_migrations = discover_migrations(CONTROL_MIGRATIONS, CONTROL_PATTERN)
    market_migrations = discover_migrations(MARKET_MIGRATIONS, MARKET_PATTERN)
    ensure_control_database(market_dsn, control_database_name)
    assert_distinct_databases(control_dsn, market_dsn)
    replay_control_migrations(control_dsn, control_migrations)
    replay_market_migrations(
        market_dsn,
        market_migrations,
        adopt_existing=bool(arguments.adopt_existing_market),
    )
    provision_runtime_logins(control_dsn, market_dsn, runtime_passwords)
    if arguments.seed_paper_principal:
        seed_paper_principal(control_dsn, top_up_cash=bool(arguments.top_up_paper_cash))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        run(arguments)
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # do not render driver errors or DSNs
        print(f"ERROR: database bootstrap failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("database bootstrap complete: control and market migration audits passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
