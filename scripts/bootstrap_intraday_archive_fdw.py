#!/usr/bin/env python3
"""Safely bootstrap the read-only intraday archive FDW contract.

The target market database is supplied by ``TIMESCALE_DATABASE_URL`` and the
archive source by ``INTRADAY_ARCHIVE_DATABASE_URL``.  Connection strings and
credentials are deliberately never included in diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit


SERVER_NAME = "trading_src"
FOREIGN_SCHEMA = "ext_src"
REMOTE_SCHEMA = "public"
FETCH_SIZE = "50000"
MIN_COVERAGE_CALENDAR_DAYS = 60
LOCK_NAME = "intraday-archive-fdw-bootstrap-v1"

QUOTE_REQUIRED_COLUMNS = frozenset(
    ["ts", "symbol", "spread"]
    + [f"bid{i}" for i in range(1, 11)]
    + [f"ask{i}" for i in range(1, 11)]
    + [f"bid_vol{i}" for i in range(1, 11)]
    + [f"ask_vol{i}" for i in range(1, 11)]
)
TICK_REQUIRED_COLUMNS = frozenset(
    {"ts", "symbol", "price", "volume", "ofi_contrib"}
)
REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "quotes": QUOTE_REQUIRED_COLUMNS,
    "ticks": TICK_REQUIRED_COLUMNS,
}

# These libpq options have direct postgres_fdw equivalents.  Other source URL
# query parameters remain valid for the direct preflight connection but are not
# silently copied into CREATE SERVER.
FDW_SOURCE_OPTIONS = frozenset(
    {"sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl"}
)


class BootstrapError(RuntimeError):
    """Expected, safe-to-display bootstrap failure."""


class ConfigurationDrift(BootstrapError):
    """Existing infrastructure differs from the declared contract."""


@dataclass(frozen=True)
class DatabaseEndpoint:
    host: str
    port: int
    database: str
    user: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    fdw_options: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def server_options(self) -> dict[str, str]:
        options = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.database,
            "fetch_size": FETCH_SIZE,
        }
        options.update(self.fdw_options)
        return options


@dataclass(frozen=True)
class RelationState:
    kind: str
    server: str | None = None
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetState:
    extension_exists: bool
    schema_exists: bool
    current_user: str
    server_fdw: str | None = None
    server_options: Mapping[str, str] = field(default_factory=dict)
    mapping_options: Mapping[str, str] | None = None
    relations: Mapping[str, RelationState] = field(default_factory=dict)
    columns: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    kind: str
    table: str | None = None
    actual_options: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Coverage:
    table: str
    first: datetime
    last: datetime
    chunks: int
    probe_start: datetime
    probe_end: datetime

    @property
    def calendar_days(self) -> int:
        return max(0, (self.last.date() - self.first.date()).days)

    def public_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "first": self.first.isoformat(),
            "last": self.last.isoformat(),
            "calendar_days": self.calendar_days,
            "chunks": self.chunks,
        }


def parse_database_url(
    value: str | None,
    *,
    label: str,
    require_credentials: bool,
) -> DatabaseEndpoint:
    """Parse a PostgreSQL URL without ever placing its secret in an error."""

    if not value:
        raise BootstrapError(f"{label} is required")
    try:
        parsed = urlsplit(value)
        port = parsed.port or 5432
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        database = unquote(parsed.path.lstrip("/"))
        host = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise BootstrapError(f"{label} is not a valid PostgreSQL URL") from None

    if parsed.scheme not in {"postgresql", "postgres"}:
        raise BootstrapError(f"{label} must use postgresql:// or postgres://")
    if not host or not database:
        raise BootstrapError(f"{label} must include host and database")
    if require_credentials and (not username or password is None):
        raise BootstrapError(f"{label} must include source user and password")

    fdw_options: dict[str, str] = {}
    for key, option_value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in FDW_SOURCE_OPTIONS:
            if key in fdw_options:
                raise BootstrapError(f"{label} repeats FDW option {key}")
            fdw_options[key] = option_value

    return DatabaseEndpoint(
        host=host,
        port=port,
        database=database,
        user=username,
        password=password,
        fdw_options=fdw_options,
    )


def _same_database(left: DatabaseEndpoint, right: DatabaseEndpoint) -> bool:
    return (
        left.host.casefold(),
        left.port,
        left.database.casefold(),
    ) == (
        right.host.casefold(),
        right.port,
        right.database.casefold(),
    )


def _options(raw_options: Iterable[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_options or ():
        key, separator, value = item.partition("=")
        if separator:
            parsed[key] = value
    return parsed


def _mapping_matches(
    actual: Mapping[str, str] | None,
    *,
    expected_user: str,
    expected_password: str,
) -> bool:
    if actual is None or set(actual) != {"user", "password"}:
        return False
    password = actual.get("password")
    # PostgreSQL masks this field for callers that may inspect the mapping but
    # not its password.  Connectivity validation below remains the authority.
    password_matches = password == expected_password or password in {"********", None}
    return actual.get("user") == expected_user and password_matches


def build_plan(
    state: TargetState,
    source: DatabaseEndpoint,
    *,
    reconfigure: bool,
) -> list[Action]:
    """Return the smallest mutation plan or fail on undeclared drift."""

    if source.user is None or source.password is None:
        raise BootstrapError("source credentials are required for the user mapping")

    actions: list[Action] = []
    if not state.extension_exists:
        actions.append(Action("create_extension"))
    if not state.schema_exists:
        actions.append(Action("create_schema"))

    if state.server_fdw is None:
        actions.append(Action("create_server"))
    elif state.server_fdw != "postgres_fdw":
        raise ConfigurationDrift(
            f"server {SERVER_NAME} uses a different FDW; manual intervention required"
        )
    elif dict(state.server_options) != source.server_options:
        if not reconfigure:
            raise ConfigurationDrift(
                f"server {SERVER_NAME} options drifted; rerun with --reconfigure"
            )
        actions.append(
            Action("alter_server", actual_options=dict(state.server_options))
        )

    if state.mapping_options is None:
        actions.append(Action("create_mapping"))
    elif reconfigure:
        # pg_user_mappings may redact the stored password even for a role that
        # can use the mapping.  Explicit reconfiguration is also the supported
        # credential-rotation path, so always reapply the injected credential.
        actions.append(
            Action("alter_mapping", actual_options=dict(state.mapping_options))
        )
    elif not _mapping_matches(
        state.mapping_options,
        expected_user=source.user,
        expected_password=source.password,
    ):
        raise ConfigurationDrift(
            f"current-user mapping for {SERVER_NAME} drifted; "
            "rerun with --reconfigure"
        )

    for table, required in REQUIRED_COLUMNS.items():
        relation = state.relations.get(table)
        if relation is None:
            actions.append(Action("import_table", table=table))
            continue
        if relation.kind != "f":
            raise ConfigurationDrift(
                f"{FOREIGN_SCHEMA}.{table} is not a foreign table; "
                "manual intervention required"
            )
        expected_relation_options = {
            "schema_name": REMOTE_SCHEMA,
            "table_name": table,
        }
        relation_drift = (
            relation.server != SERVER_NAME
            or dict(relation.options) != expected_relation_options
            or not required.issubset(state.columns.get(table, frozenset()))
        )
        if relation_drift:
            if not reconfigure:
                raise ConfigurationDrift(
                    f"{FOREIGN_SCHEMA}.{table} drifted; rerun with --reconfigure"
                )
            actions.append(Action("recreate_foreign_table", table=table))
    return actions


def read_target_state(connection: Any) -> TargetState:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        current_user = cursor.fetchone()[0]

        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = %s)",
            ("postgres_fdw",),
        )
        extension_exists = bool(cursor.fetchone()[0])

        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
            (FOREIGN_SCHEMA,),
        )
        schema_exists = bool(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT w.fdwname, s.srvoptions
            FROM pg_foreign_server AS s
            JOIN pg_foreign_data_wrapper AS w ON w.oid = s.srvfdw
            WHERE s.srvname = %s
            """,
            (SERVER_NAME,),
        )
        server_row = cursor.fetchone()
        server_fdw = server_row[0] if server_row else None
        server_options = _options(server_row[1]) if server_row else {}

        cursor.execute(
            """
            SELECT umoptions
            FROM pg_user_mappings
            WHERE srvname = %s AND usename = current_user
            """,
            (SERVER_NAME,),
        )
        mapping_row = cursor.fetchone()
        mapping_options = _options(mapping_row[0]) if mapping_row else None

        cursor.execute(
            """
            SELECT c.relname, c.relkind, s.srvname, ft.ftoptions
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_foreign_table AS ft ON ft.ftrelid = c.oid
            LEFT JOIN pg_foreign_server AS s ON s.oid = ft.ftserver
            WHERE n.nspname = %s AND c.relname = ANY(%s)
            """,
            (FOREIGN_SCHEMA, list(REQUIRED_COLUMNS)),
        )
        relations = {
            row[0]: RelationState(
                kind=row[1], server=row[2], options=_options(row[3])
            )
            for row in cursor.fetchall()
        }

        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = ANY(%s)
            """,
            (FOREIGN_SCHEMA, list(REQUIRED_COLUMNS)),
        )
        columns: dict[str, set[str]] = {table: set() for table in REQUIRED_COLUMNS}
        for table, column in cursor.fetchall():
            columns.setdefault(table, set()).add(column)

    return TargetState(
        extension_exists=extension_exists,
        schema_exists=schema_exists,
        current_user=current_user,
        server_fdw=server_fdw,
        server_options=server_options,
        mapping_options=mapping_options,
        relations=relations,
        columns={table: frozenset(values) for table, values in columns.items()},
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_source(connection: Any) -> dict[str, Coverage]:
    """Validate source schema and coverage without scanning raw archive rows."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = ANY(%s)
            """,
            (REMOTE_SCHEMA, list(REQUIRED_COLUMNS)),
        )
        found: dict[str, set[str]] = {table: set() for table in REQUIRED_COLUMNS}
        for table, column in cursor.fetchall():
            found.setdefault(table, set()).add(column)

        coverage: dict[str, Coverage] = {}
        for table, required in REQUIRED_COLUMNS.items():
            missing = sorted(required - found.get(table, set()))
            if missing:
                raise BootstrapError(
                    f"source {REMOTE_SCHEMA}.{table} is missing required columns: "
                    + ", ".join(missing)
                )

            # Timescale chunk metadata gives a bounded coverage check even when
            # the archive contains billions of rows.
            cursor.execute(
                """
                SELECT min(range_start), max(range_end), count(*)
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = %s AND hypertable_name = %s
                """,
                (REMOTE_SCHEMA, table),
            )
            first, last, chunks = cursor.fetchone()
            if first is None or last is None or not chunks:
                raise BootstrapError(
                    f"source {REMOTE_SCHEMA}.{table} has no Timescale chunk coverage"
                )

            cursor.execute(
                """
                SELECT range_start, range_end
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = %s AND hypertable_name = %s
                ORDER BY range_end DESC
                LIMIT 1
                """,
                (REMOTE_SCHEMA, table),
            )
            probe_row = cursor.fetchone()
            if not probe_row:
                raise BootstrapError(
                    f"source {REMOTE_SCHEMA}.{table} has no readable latest chunk"
                )
            probe_start, probe_end = probe_row

            item = Coverage(
                table=table,
                first=_as_utc(first),
                last=_as_utc(last),
                chunks=int(chunks),
                probe_start=_as_utc(probe_start),
                probe_end=_as_utc(probe_end),
            )
            if item.calendar_days < MIN_COVERAGE_CALENDAR_DAYS:
                raise BootstrapError(
                    f"source {REMOTE_SCHEMA}.{table} covers only "
                    f"{item.calendar_days} calendar days; "
                    f"at least {MIN_COVERAGE_CALENDAR_DAYS} required"
                )

            # The interval predicate is pushed into the newest chunk.  This is
            # an access/readability probe, not an unbounded COUNT/MIN scan.
            cursor.execute(
                f"""
                SELECT ts, btrim(symbol)
                FROM {REMOTE_SCHEMA}.{table}
                WHERE ts >= %s AND ts < %s
                  AND ts IS NOT NULL AND btrim(symbol) <> ''
                LIMIT 1
                """,
                (item.probe_start, item.probe_end),
            )
            if cursor.fetchone() is None:
                raise BootstrapError(
                    f"source {REMOTE_SCHEMA}.{table} latest chunk is unreadable or empty"
                )
            coverage[table] = item
    return coverage


def _sql_module() -> Any:
    try:
        from psycopg2 import sql
    except ImportError:
        raise BootstrapError("psycopg2 is required to bootstrap postgres_fdw") from None
    return sql


def _option_clause(
    expected: Mapping[str, str],
    *,
    actual: Mapping[str, str] | None = None,
) -> Any:
    sql = _sql_module()
    parts = []
    altering = actual is not None
    actual_options = actual or {}
    for key in sorted(set(actual_options) - set(expected)):
        parts.append(sql.SQL("DROP {}").format(sql.Identifier(key)))
    for key in sorted(expected):
        if altering:
            operation = "SET" if key in actual_options else "ADD"
            parts.append(
                sql.SQL("{} {} {}").format(
                    sql.SQL(operation),
                    sql.Identifier(key),
                    sql.Literal(expected[key]),
                )
            )
        else:
            parts.append(
                sql.SQL("{} {}").format(
                    sql.Identifier(key), sql.Literal(expected[key])
                )
            )
    return sql.SQL(", ").join(parts)


def apply_plan(
    connection: Any,
    actions: Sequence[Action],
    source: DatabaseEndpoint,
) -> None:
    sql = _sql_module()
    mapping_expected = {"user": source.user or "", "password": source.password or ""}
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_NAME,))
        for action in actions:
            if action.kind == "create_extension":
                cursor.execute("CREATE EXTENSION postgres_fdw")
            elif action.kind == "create_schema":
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(FOREIGN_SCHEMA))
                )
            elif action.kind == "create_server":
                cursor.execute(
                    sql.SQL(
                        "CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw OPTIONS ({})"
                    ).format(
                        sql.Identifier(SERVER_NAME),
                        _option_clause(source.server_options),
                    )
                )
            elif action.kind == "alter_server":
                cursor.execute(
                    sql.SQL("ALTER SERVER {} OPTIONS ({})").format(
                        sql.Identifier(SERVER_NAME),
                        _option_clause(
                            source.server_options, actual=action.actual_options
                        ),
                    )
                )
            elif action.kind == "create_mapping":
                cursor.execute(
                    sql.SQL(
                        "CREATE USER MAPPING FOR CURRENT_USER SERVER {} OPTIONS ({})"
                    ).format(
                        sql.Identifier(SERVER_NAME),
                        _option_clause(mapping_expected),
                    )
                )
            elif action.kind == "alter_mapping":
                cursor.execute(
                    sql.SQL(
                        "ALTER USER MAPPING FOR CURRENT_USER SERVER {} OPTIONS ({})"
                    ).format(
                        sql.Identifier(SERVER_NAME),
                        _option_clause(
                            mapping_expected, actual=action.actual_options
                        ),
                    )
                )
            elif action.kind in {"import_table", "recreate_foreign_table"}:
                if action.table not in REQUIRED_COLUMNS:
                    raise BootstrapError("internal error: unsupported foreign table")
                if action.kind == "recreate_foreign_table":
                    cursor.execute(
                        sql.SQL("DROP FOREIGN TABLE {}.{}").format(
                            sql.Identifier(FOREIGN_SCHEMA),
                            sql.Identifier(action.table),
                        )
                    )
                cursor.execute(
                    sql.SQL(
                        "IMPORT FOREIGN SCHEMA {} LIMIT TO ({}) "
                        "FROM SERVER {} INTO {}"
                    ).format(
                        sql.Identifier(REMOTE_SCHEMA),
                        sql.Identifier(action.table),
                        sql.Identifier(SERVER_NAME),
                        sql.Identifier(FOREIGN_SCHEMA),
                    )
                )
            else:
                raise BootstrapError(f"internal error: unknown action {action.kind}")


def validate_target_fdw(
    connection: Any,
    coverage: Mapping[str, Coverage],
) -> None:
    """Prove the target can read each remote table through the FDW mapping."""

    with connection.cursor() as cursor:
        for table, required in REQUIRED_COLUMNS.items():
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (FOREIGN_SCHEMA, table),
            )
            found = {row[0] for row in cursor.fetchall()}
            missing = sorted(required - found)
            if missing:
                raise BootstrapError(
                    f"target {FOREIGN_SCHEMA}.{table} is missing required columns: "
                    + ", ".join(missing)
                )

            item = coverage[table]
            cursor.execute(
                f"""
                SELECT ts, btrim(symbol)
                FROM {FOREIGN_SCHEMA}.{table}
                WHERE ts >= %s AND ts < %s
                  AND ts IS NOT NULL AND btrim(symbol) <> ''
                LIMIT 1
                """,
                (item.probe_start, item.probe_end),
            )
            if cursor.fetchone() is None:
                raise BootstrapError(
                    f"target cannot read the latest source interval through "
                    f"{FOREIGN_SCHEMA}.{table}"
                )


def _connect(connector: Any, dsn: str, *, label: str) -> Any:
    try:
        return connector(dsn, connect_timeout=15, application_name=LOCK_NAME)
    except Exception:
        raise BootstrapError(f"{label} database connection failed") from None


def run(
    *,
    target_dsn: str,
    source_dsn: str,
    check: bool,
    reconfigure: bool,
    connector: Any | None = None,
) -> dict[str, Any]:
    target_endpoint = parse_database_url(
        target_dsn,
        label="TIMESCALE_DATABASE_URL",
        require_credentials=False,
    )
    source_endpoint = parse_database_url(
        source_dsn,
        label="INTRADAY_ARCHIVE_DATABASE_URL",
        require_credentials=True,
    )
    if _same_database(target_endpoint, source_endpoint):
        raise BootstrapError("target and archive source must be different databases")

    if connector is None:
        try:
            import psycopg2
        except ImportError:
            raise BootstrapError(
                "psycopg2 is required to bootstrap postgres_fdw"
            ) from None
        connector = psycopg2.connect

    source_connection = _connect(connector, source_dsn, label="source")
    try:
        source_connection.set_session(readonly=True)
        coverage = validate_source(source_connection)
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("source validation failed") from None
    finally:
        source_connection.close()

    target_connection = _connect(connector, target_dsn, label="target")
    actions: list[Action] = []
    try:
        target_connection.set_session(readonly=check)
        state = read_target_state(target_connection)
        actions = build_plan(state, source_endpoint, reconfigure=reconfigure)
        if check and actions:
            missing = ", ".join(action.kind for action in actions)
            raise BootstrapError(f"FDW preflight requires changes: {missing}")
        if actions:
            apply_plan(target_connection, actions, source_endpoint)
            state = read_target_state(target_connection)
            residual = build_plan(state, source_endpoint, reconfigure=False)
            if residual:
                raise BootstrapError("FDW bootstrap left an incomplete target state")
        validate_target_fdw(target_connection, coverage)
        if check:
            target_connection.rollback()
        else:
            target_connection.commit()
    except BootstrapError:
        target_connection.rollback()
        raise
    except Exception:
        target_connection.rollback()
        raise BootstrapError("target FDW bootstrap or validation failed") from None
    finally:
        target_connection.close()

    mode = "check" if check else ("reconfigure" if reconfigure else "apply")
    return {
        "status": "PASS",
        "mode": mode,
        "server": SERVER_NAME,
        "schema": FOREIGN_SCHEMA,
        "tables": sorted(REQUIRED_COLUMNS),
        "actions": [action.kind for action in actions],
        "source_coverage": {
            table: item.public_dict() for table, item in sorted(coverage.items())
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap and validate the intraday archive postgres_fdw contract."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Fail closed if any mutation or connectivity repair is required.",
    )
    mode.add_argument(
        "--reconfigure",
        action="store_true",
        help="Explicitly reconcile safe server, mapping, and foreign-table drift.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    connector: Any | None = None,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        result = run(
            target_dsn=environment.get("TIMESCALE_DATABASE_URL", ""),
            source_dsn=environment.get("INTRADAY_ARCHIVE_DATABASE_URL", ""),
            check=args.check,
            reconfigure=args.reconfigure,
            connector=connector,
        )
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Never echo arbitrary driver exceptions: they can embed a DSN.
        print(
            f"ERROR: unexpected FDW bootstrap failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
