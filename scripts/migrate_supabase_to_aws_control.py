#!/usr/bin/env python3
"""One-time cutover: copy domain data from Hosted Supabase into AWS ``control``.

This tool does not migrate schema.  ``supabase/migrations/`` is the canonical
schema chain and ``scripts/aws_database_bootstrap.py`` already replays it onto
the AWS ``control`` database; this script only copies rows, and only after
proving the two databases are structurally identical.

Scope: base tables in the domain schemas owned by ``supabase/migrations``
(discovered at run time, never hardcoded).  Supabase-managed schemas
(``auth``, ``storage``, ``realtime``, ...) are never read or copied, and
``public``/``api`` are asserted to hold zero base tables rather than silently
skipped.  Hosted Supabase Auth is never touched: ``governance.user_profiles``
is copied like any other domain table, preserving the exact ``user_id`` UUID
(the verified Supabase JWT ``sub``), with no local ``auth.users`` created.

Every run is fail-closed: any schema drift, PK collision with different
content, unexplained target-only data, or non-deferrable circular foreign key
aborts the entire run before a single row is written anywhere.  Re-running
after a partial or aborted run is safe -- already-migrated rows are detected
and skipped, never duplicated.

Connection strings and exception details are never printed, matching
``aws_database_bootstrap.py``'s discipline.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_MODULE_PATH = ROOT / "scripts" / "aws_database_bootstrap.py"
REPORT_DIR = ROOT / "migration_reports"

EXCLUDED_SCHEMAS = frozenset(
    {
        "auth",
        "storage",
        "realtime",
        "extensions",
        "vault",
        "graphql",
        "graphql_public",
        "supabase_functions",
        "supabase_migrations",
        "net",
        "pgbouncer",
        "pgsodium",
        "pgsodium_masks",
        "pgtle",
        "cron",
        "pg_catalog",
        "pg_toast",
        "information_schema",
    }
)
NO_BASE_TABLE_SCHEMAS = frozenset({"public", "api"})

# This tool records its own run in audit.traces on the target.  That row has no
# source counterpart, so every read of the target must filter it out -- without
# this, a successful run reports its own bookkeeping as a checksum mismatch
# (status PARTIAL, exit 1) and a re-run rejects it as unexplained target-only
# data, permanently wedging the migration.
MIGRATION_TRACE_TYPE = "DATA_MIGRATION"
TARGET_ONLY_EXCLUSIONS: dict[str, str] = {
    "audit.traces": f"trace_type <> '{MIGRATION_TRACE_TYPE}'",
}


def _where_clause(table: "TableInfo", apply_exclusion: bool) -> str:
    if not apply_exclusion:
        return ""
    predicate = TARGET_ONLY_EXCLUSIONS.get(table.qualified)
    return f" where {predicate}" if predicate else ""


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "aws_database_bootstrap", BOOTSTRAP_MODULE_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - repo layout guard
        raise RuntimeError("cannot load scripts/aws_database_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap_module()


class MigrationError(RuntimeError):
    """An operator-safe migration error whose message contains no secrets."""


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    full_type: str
    not_null: bool
    is_generated: bool
    ordinal: int
    identity: str = ""  # '' none, 'a' GENERATED ALWAYS, 'd' BY DEFAULT


@dataclass(frozen=True)
class ConstraintInfo:
    name: str
    kind: str  # 'p' primary key, 'f' foreign key, 'u' unique, 'c' check
    deferrable: bool
    deferred: bool
    definition: str


@dataclass(frozen=True)
class ForeignKeyEdge:
    name: str
    deferrable: bool
    ref_schema: str
    ref_table: str


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    columns: tuple[ColumnInfo, ...]
    constraints: tuple[ConstraintInfo, ...]
    foreign_keys: tuple[ForeignKeyEdge, ...]

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def primary_key_columns(self) -> tuple[str, ...]:
        for constraint in self.constraints:
            if constraint.kind == "p":
                inner = constraint.definition.partition("(")[2].rpartition(")")[0]
                return tuple(part.strip() for part in inner.split(","))
        raise MigrationError(f"{self.qualified} has no primary key; refusing to migrate")

    @property
    def insertable_columns(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns if not column.is_generated)

    @property
    def all_column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def has_always_identity(self) -> bool:
        """True when a column is GENERATED ALWAYS AS IDENTITY.

        Preserving the source's exact key values then requires OVERRIDING
        SYSTEM VALUE; without it PostgreSQL rejects the insert outright.
        """

        return any(column.identity == "a" for column in self.columns)


def _discover_base_table_schemas(cursor) -> dict[str, list[str]]:
    cursor.execute(
        """
        select n.nspname, c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r'
           and n.nspname not in %s
         order by n.nspname, c.relname
        """,
        (tuple(EXCLUDED_SCHEMAS),),
    )
    by_schema: dict[str, list[str]] = {}
    for schema_name, table_name in cursor.fetchall():
        by_schema.setdefault(str(schema_name), []).append(str(table_name))
    return by_schema


def _introspect_table(cursor, schema: str, table: str) -> TableInfo:
    cursor.execute(
        """
        select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               a.attgenerated <> '', a.attnum, a.attidentity
          from pg_attribute a
          join pg_class c on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = %s and c.relname = %s
           and a.attnum > 0 and not a.attisdropped
         order by a.attnum
        """,
        (schema, table),
    )
    columns = tuple(
        ColumnInfo(
            name=str(row[0]),
            full_type=str(row[1]),
            not_null=bool(row[2]),
            is_generated=bool(row[3]),
            ordinal=int(row[4]),
            identity=str(row[5] or ""),
        )
        for row in cursor.fetchall()
    )
    if not columns:
        raise MigrationError(f"{schema}.{table} has no columns; refusing to migrate")

    cursor.execute(
        """
        select con.conname, con.contype::text, con.condeferrable, con.condeferred,
               pg_get_constraintdef(con.oid)
          from pg_constraint con
          join pg_class c on c.oid = con.conrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = %s and c.relname = %s
         order by con.conname
        """,
        (schema, table),
    )
    constraints = tuple(
        ConstraintInfo(
            name=str(row[0]),
            kind=str(row[1]),
            deferrable=bool(row[2]),
            deferred=bool(row[3]),
            definition=str(row[4]),
        )
        for row in cursor.fetchall()
    )

    cursor.execute(
        """
        select con.conname, con.condeferrable, fn.nspname, fc.relname
          from pg_constraint con
          join pg_class c on c.oid = con.conrelid
          join pg_namespace n on n.oid = c.relnamespace
          join pg_class fc on fc.oid = con.confrelid
          join pg_namespace fn on fn.oid = fc.relnamespace
         where n.nspname = %s and c.relname = %s and con.contype = 'f'
         order by con.conname
        """,
        (schema, table),
    )
    foreign_keys = tuple(
        ForeignKeyEdge(
            name=str(row[0]),
            deferrable=bool(row[1]),
            ref_schema=str(row[2]),
            ref_table=str(row[3]),
        )
        for row in cursor.fetchall()
    )

    return TableInfo(
        schema=schema,
        name=table,
        columns=columns,
        constraints=constraints,
        foreign_keys=foreign_keys,
    )


def introspect_domain_schema(cursor) -> dict[tuple[str, str], TableInfo]:
    by_schema = _discover_base_table_schemas(cursor)
    for special in NO_BASE_TABLE_SCHEMAS:
        if by_schema.get(special):
            raise MigrationError(
                f"schema {special!r} has base tables; refusing to guess migration scope"
            )
    tables: dict[tuple[str, str], TableInfo] = {}
    for schema_name, table_names in by_schema.items():
        if schema_name in NO_BASE_TABLE_SCHEMAS:
            continue
        for table_name in table_names:
            tables[(schema_name, table_name)] = _introspect_table(
                cursor, schema_name, table_name
            )
    return tables


def compare_schemas(
    source: dict[tuple[str, str], TableInfo],
    target: dict[tuple[str, str], TableInfo],
    scope: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Require exact structural identity for the tables being copied.

    control legitimately runs ahead of the abandoned Supabase remnant by several
    migrations, so a whole-estate comparison can never pass again.  Structural
    identity still has to hold exactly for every table in COPY scope -- that is
    the data being moved -- but demanding it for the 203 tables this tool never
    reads or writes would block the copy for no safety gain.
    """

    problems: list[str] = []
    source_keys = set(source) if scope is None else set(source) & scope
    target_keys = set(target) if scope is None else set(target) & scope
    for key in sorted(source_keys - target_keys):
        problems.append(f"missing on target: {key[0]}.{key[1]}")
    for key in sorted(target_keys - source_keys):
        problems.append(f"missing on source: {key[0]}.{key[1]}")
    for key in sorted(source_keys & target_keys):
        source_table = source[key]
        target_table = target[key]
        source_cols = {c.name: c for c in source_table.columns}
        target_cols = {c.name: c for c in target_table.columns}
        for name in sorted(set(source_cols) - set(target_cols)):
            problems.append(f"{key[0]}.{key[1]}.{name}: missing on target")
        for name in sorted(set(target_cols) - set(source_cols)):
            problems.append(f"{key[0]}.{key[1]}.{name}: missing on source")
        for name in sorted(set(source_cols) & set(target_cols)):
            left, right = source_cols[name], target_cols[name]
            if (left.full_type, left.not_null, left.is_generated) != (
                right.full_type,
                right.not_null,
                right.is_generated,
            ):
                problems.append(
                    f"{key[0]}.{key[1]}.{name}: type/nullability drift "
                    f"(source={left.full_type!r} not_null={left.not_null}, "
                    f"target={right.full_type!r} not_null={right.not_null})"
                )
        source_defs = {c.name: c.definition for c in source_table.constraints}
        target_defs = {c.name: c.definition for c in target_table.constraints}
        for name in sorted(set(source_defs) - set(target_defs)):
            problems.append(f"{key[0]}.{key[1]} constraint {name}: missing on target")
        for name in sorted(set(target_defs) - set(source_defs)):
            problems.append(f"{key[0]}.{key[1]} constraint {name}: missing on source")
        for name in sorted(set(source_defs) & set(target_defs)):
            if source_defs[name] != target_defs[name]:
                problems.append(
                    f"{key[0]}.{key[1]} constraint {name}: definition drift "
                    f"(source={source_defs[name]!r}, target={target_defs[name]!r})"
                )
    return problems


def detect_insert_blocking_triggers(
    cursor, scope: set[tuple[str, str]]
) -> list[str]:
    """Row-level BEFORE INSERT triggers on tables we are about to write.

    ``compare_schemas`` reads pg_constraint, which does not know about triggers.
    A state-machine guard such as audit.eval_runs' append-only lifecycle check
    therefore stays invisible until the copy is already running and fails
    mid-transaction.  Surfacing it during preflight turns that into a clean
    refusal that names the table and the trigger.
    """

    if not scope:
        return []
    cursor.execute(
        """
        select n.nspname, c.relname, t.tgname
          from pg_trigger t
          join pg_class c on c.oid = t.tgrelid
          join pg_namespace n on n.oid = c.relnamespace
         where not t.tgisinternal
           and (t.tgtype & 1) = 1   -- FOR EACH ROW
           and (t.tgtype & 2) = 2   -- BEFORE
           and (t.tgtype & 4) = 4   -- INSERT
         order by 1, 2, 3
        """
    )
    return [
        f"{schema}.{table} has BEFORE INSERT trigger {trigger}"
        for schema, table, trigger in cursor.fetchall()
        if (schema, table) in scope
    ]


def verify_target_migration_history(cursor) -> None:
    """Reuse the bootstrap script's own drift-detection, not a reimplementation."""

    migrations = bootstrap.discover_migrations(
        bootstrap.CONTROL_MIGRATIONS, bootstrap.CONTROL_PATTERN
    )
    cursor.execute(
        """
        select version, coalesce(name,''), statements
          from supabase_migrations.schema_migrations
         order by version
        """
    )
    rows = cursor.fetchall()
    bootstrap.validate_applied_prefix(
        migrations, (str(row[0]) for row in rows), "control"
    )
    if len(rows) != len(migrations):
        raise MigrationError(
            "control migration history is behind the repository chain; "
            "run scripts/aws_database_bootstrap.py first"
        )
    by_version = {migration.version: migration for migration in migrations}
    for version, name, statements in rows:
        migration = by_version[str(version)]
        if str(name) not in {"", migration.name}:
            raise MigrationError(f"control migration name drift at {version}")
        checksum = bootstrap._checksum_from_statements(statements)
        if checksum is None or checksum != migration.checksum:
            raise MigrationError(f"control migration checksum drift at {version}")


def verify_source_migration_versions(cursor) -> None:
    migrations = bootstrap.discover_migrations(
        bootstrap.CONTROL_MIGRATIONS, bootstrap.CONTROL_PATTERN
    )
    cursor.execute(
        "select version from supabase_migrations.schema_migrations order by version"
    )
    actual = [str(row[0]) for row in cursor.fetchall()]
    # The source trailing the repository chain is expected: it is a frozen
    # remnant and nothing pushes migrations to it any more.  Being AHEAD, or
    # holding a version the repository does not, still aborts -- that would mean
    # the source carries schema this tool cannot reason about.  Reuses the
    # bootstrap's own prefix rule rather than a second implementation of it.
    bootstrap.validate_applied_prefix(migrations, iter(actual), "source Supabase")


# ---------------------------------------------------------------------------
# Foreign-key dependency ordering
# ---------------------------------------------------------------------------


def _tarjan_sccs(
    nodes: Sequence[tuple[str, str]], edges: dict[tuple[str, str], set[tuple[str, str]]]
) -> list[list[tuple[str, str]]]:
    index_counter = [0]
    stack: list[tuple[str, str]] = []
    lowlink: dict[tuple[str, str], int] = {}
    index: dict[tuple[str, str], int] = {}
    on_stack: dict[tuple[str, str], bool] = {}
    result: list[list[tuple[str, str]]] = []

    def strongconnect(node: tuple[str, str]) -> None:
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for successor in edges.get(node, ()):
            if successor not in index:
                strongconnect(successor)
                lowlink[node] = min(lowlink[node], lowlink[successor])
            elif on_stack.get(successor):
                lowlink[node] = min(lowlink[node], index[successor])
        if lowlink[node] == index[node]:
            component: list[tuple[str, str]] = []
            while True:
                member = stack.pop()
                on_stack[member] = False
                component.append(member)
                if member == node:
                    break
            result.append(component)

    for node in nodes:
        if node not in index:
            strongconnect(node)
    return result


def dependency_order(
    tables: dict[tuple[str, str], TableInfo],
) -> list[tuple[str, str]]:
    """Return an insert-safe table order using the live FK graph (never guessed)."""

    keys = list(tables)
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {key: set() for key in keys}
    for key, table in tables.items():
        for fk in table.foreign_keys:
            ref = (fk.ref_schema, fk.ref_table)
            if ref in tables and ref != key:
                edges[key].add(ref)  # key depends on ref: ref must be inserted first

    components = _tarjan_sccs(keys, edges)
    for component in components:
        if len(component) <= 1:
            continue
        members = set(component)
        for key in component:
            for fk in tables[key].foreign_keys:
                ref = (fk.ref_schema, fk.ref_table)
                if ref in members and not fk.deferrable:
                    raise MigrationError(
                        f"circular foreign key {tables[key].qualified} -> "
                        f"{ref[0]}.{ref[1]} via {fk.name} is not DEFERRABLE; "
                        "refusing to drop or reorder constraints"
                    )

    component_of: dict[tuple[str, str], int] = {}
    for index, component in enumerate(components):
        for key in component:
            component_of[key] = index
    condensed_edges: dict[int, set[int]] = {i: set() for i in range(len(components))}
    for key, deps in edges.items():
        for dep in deps:
            if component_of[key] != component_of[dep]:
                condensed_edges[component_of[key]].add(component_of[dep])

    visited: set[int] = set()
    order: list[int] = []

    def visit(node: int) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in condensed_edges[node]:
            visit(dep)
        order.append(node)

    for node in range(len(components)):
        visit(node)

    result: list[tuple[str, str]] = []
    for component_index in order:
        result.extend(sorted(components[component_index]))
    return result


# ---------------------------------------------------------------------------
# Canonicalization / content hashing
# ---------------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def row_digest(values: Sequence[Any]) -> str:
    canonical = json.dumps(
        [_canonicalize(value) for value in values],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Known bootstrap-seed identities (Case D allowlist)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedIdentity:
    user_id: UUID
    fund_id: UUID
    book_id: UUID
    account_codes: tuple[str, ...]


def resolve_seed_identity() -> SeedIdentity:
    user_id = bootstrap._uuid_environment("PAPER_SEED_USER_ID", bootstrap.DEFAULT_USER_ID)
    fund_id = bootstrap._uuid_environment("PAPER_SEED_FUND_ID", bootstrap.DEFAULT_FUND_ID)
    book_id = bootstrap._uuid_environment("PAPER_SEED_BOOK_ID", bootstrap.DEFAULT_BOOK_ID)
    account_codes = tuple(code for code, _name, _type in bootstrap.ACCOUNT_CHART)
    return SeedIdentity(
        user_id=user_id, fund_id=fund_id, book_id=book_id, account_codes=account_codes
    )


def is_known_seed_row(table: TableInfo, pk_values: tuple[Any, ...], seed: SeedIdentity) -> bool:
    pk_columns = table.primary_key_columns
    by_name = dict(zip(pk_columns, pk_values))

    if table.qualified == "governance.user_profiles":
        return by_name.get("user_id") == seed.user_id
    if table.qualified == "accounting.funds":
        return by_name.get("fund_id") == seed.fund_id
    if table.qualified == "accounting.books":
        return by_name.get("book_id") == seed.book_id
    # Every other seed-touched table is keyed by a server-generated surrogate
    # PK (account_id / journal_id / ...), not a value this tool can predict.
    # Those rows are recognized by a provenance marker column instead -- see
    # classify_table's is_known_seed callback, which queries by value rather
    # than relying on this identity-only check.
    return False


# ---------------------------------------------------------------------------
# Conflict classification (Pass 1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scope manifest
# ---------------------------------------------------------------------------
#
# Measured 2026-08-24: hosted Supabase holds 245 domain rows while AWS control
# holds 135,833 -- control has been the operational source of truth for most of
# the estate, and only a handful of tables still carry Supabase-only rows.  A
# blanket "everything must match" rule therefore aborts on ~135,800 legitimate
# target-only rows and can never complete.  Rather than guess which side wins
# per table, every table must be declared in a reviewed manifest.  An
# undeclared table is fatal: fail-closed, and the operator has to decide.

POLICY_COPY = "COPY"
POLICY_CONTROL_AUTHORITATIVE = "CONTROL_AUTHORITATIVE"
VALID_POLICIES = frozenset({POLICY_COPY, POLICY_CONTROL_AUTHORITATIVE})


def load_scope(path: Path) -> dict[str, str]:
    """Load the reviewed per-table policy manifest."""

    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise MigrationError("scope manifest not found at the given path")
    except json.JSONDecodeError:
        raise MigrationError("scope manifest is not valid JSON")
    tables = raw.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise MigrationError("scope manifest has no 'tables' mapping")
    scope: dict[str, str] = {}
    for qualified, policy in tables.items():
        if policy not in VALID_POLICIES:
            raise MigrationError(
                f"scope manifest gives {qualified} an unknown policy; "
                f"expected one of {sorted(VALID_POLICIES)}"
            )
        scope[str(qualified)] = str(policy)
    return scope


@dataclass
class TableClassification:
    table: TableInfo
    policy: str = POLICY_COPY
    to_insert: list[tuple[Any, ...]] = field(default_factory=list)
    already_present: int = 0
    conflicts: list[str] = field(default_factory=list)
    unexplained_target_only: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    source_count: int = 0
    target_count: int = 0


SEED_PROVENANCE_QUERIES: dict[str, str] = {
    "governance.fund_memberships": (
        "select fund_id, user_id, role from governance.fund_memberships "
        "where fund_id = %(fund_id)s and user_id = %(user_id)s "
        "and role in ('OWNER', 'TRADER')"
    ),
    "accounting.ledger_accounts": (
        "select account_id from accounting.ledger_accounts "
        "where fund_id = %(fund_id)s and account_code = any(%(account_codes)s)"
    ),
    "accounting.cash_balances": (
        "select cash_balance_id from accounting.cash_balances "
        "where fund_id = %(fund_id)s and book_id = %(book_id)s"
    ),
    "accounting.journals": (
        "select journal_id from accounting.journals "
        "where created_by_service = 'aws-paper-bootstrap'"
    ),
    "accounting.journal_lines": (
        "select journal_line_id from accounting.journal_lines "
        "where journal_id in ("
        "  select journal_id from accounting.journals"
        "  where created_by_service = 'aws-paper-bootstrap'"
        ")"
    ),
}


def _known_seed_pk_set(cursor, table: TableInfo, seed: SeedIdentity) -> set[Any]:
    query = SEED_PROVENANCE_QUERIES.get(table.qualified)
    if query is None:
        return set()
    cursor.execute(
        query,
        {
            "fund_id": seed.fund_id,
            "user_id": seed.user_id,
            "book_id": seed.book_id,
            "account_codes": list(seed.account_codes),
        },
    )
    pk_columns = table.primary_key_columns
    values: set[Any] = set()
    for row in cursor.fetchall():
        values.add(row[0] if len(pk_columns) == 1 else tuple(row))
    return values


def _differing_columns(
    table: TableInfo, left: Sequence[Any], right: Sequence[Any]
) -> list[str]:
    """Column NAMES that differ -- never the values, which may be personal."""

    return [
        name
        for name, a, b in zip(table.all_column_names, left, right)
        if row_digest([a]) != row_digest([b])
    ]


def classify_table(
    source_cursor,
    target_cursor,
    table: TableInfo,
    seed: SeedIdentity,
    policy: str = POLICY_COPY,
    accepted_drift: frozenset[str] = frozenset(),
) -> TableClassification:
    pk_columns = table.primary_key_columns
    all_columns = table.all_column_names
    order_clause = ", ".join(f'"{col}"' for col in pk_columns)
    select_clause = ", ".join(f'"{col}"' for col in all_columns)
    def _query(apply_exclusion: bool) -> str:
        return (
            f'select {select_clause} from "{table.schema}"."{table.name}"'
            f"{_where_clause(table, apply_exclusion)} order by {order_clause}"
        )

    def _keyed_rows(cursor, apply_exclusion: bool = False) -> dict[Any, tuple[Any, ...]]:
        cursor.execute(_query(apply_exclusion))
        rows: dict[Any, tuple[Any, ...]] = {}
        pk_indexes = [all_columns.index(col) for col in pk_columns]
        for row in cursor.fetchall():
            key = row[pk_indexes[0]] if len(pk_indexes) == 1 else tuple(
                row[i] for i in pk_indexes
            )
            rows[key] = tuple(row)
        return rows

    source_rows = _keyed_rows(source_cursor)
    target_rows = _keyed_rows(target_cursor, apply_exclusion=True)
    known_seed_pks = _known_seed_pk_set(target_cursor, table, seed)

    result = TableClassification(
        table=table,
        policy=policy,
        source_count=len(source_rows),
        target_count=len(target_rows),
    )

    if policy == POLICY_CONTROL_AUTHORITATIVE:
        # control is declared the source of truth here: never write, and treat
        # target-only rows as expected.  Real differences are still surfaced so
        # a reviewer sees them -- they just do not block the run.
        for key, values in source_rows.items():
            if key not in target_rows:
                result.divergences.append(f"source-only pk={key}")
            elif row_digest(values) != row_digest(target_rows[key]):
                columns = _differing_columns(table, values, target_rows[key])
                result.divergences.append(f"pk={key} differs in {','.join(columns)}")
        return result

    for key, values in source_rows.items():
        if key not in target_rows:
            result.to_insert.append(values)
            continue
        if row_digest(values) == row_digest(target_rows[key]):
            result.already_present += 1
            continue
        columns = _differing_columns(table, values, target_rows[key])
        remaining = [c for c in columns if f"{table.qualified}:{c}" not in accepted_drift]
        if not remaining:
            # every differing column was explicitly accepted as server-generated
            # metadata by the operator; the row is the same row.
            result.already_present += 1
            continue
        result.conflicts.append(f"pk={key} differs in {','.join(remaining)}")

    for key in target_rows:
        if key in source_rows:
            continue
        if key in known_seed_pks or is_known_seed_row(table, key if isinstance(key, tuple) else (key,), seed):
            continue
        result.unexplained_target_only.append(str(key))

    return result


# ---------------------------------------------------------------------------
# Copy (Pass 2)
# ---------------------------------------------------------------------------


JSON_TYPES = frozenset({"json", "jsonb"})


def _json_positions(table: TableInfo, insertable: Sequence[str]) -> set[int]:
    """Positions of json/jsonb columns, decided by the DECLARED column type.

    psycopg2 parses a jsonb column into a plain dict on read but cannot adapt a
    dict back on write, so those values need a Json() wrapper.  The decision has
    to come from the column type, not from isinstance(value, list): a Python
    list is equally the shape of a real PostgreSQL array such as text[], and
    wrapping one of those would corrupt it into a JSON string.
    """

    declared = {column.name: column.full_type for column in table.columns}
    return {
        position
        for position, name in enumerate(insertable)
        if declared.get(name, "").rstrip("[]") in JSON_TYPES
    }


def copy_table(target_connection, table: TableInfo, rows: Sequence[tuple[Any, ...]]) -> None:
    if not rows:
        return
    from psycopg2.extras import Json, execute_values

    all_columns = table.all_column_names
    insertable = table.insertable_columns
    indexes = [all_columns.index(col) for col in insertable]
    column_list = ", ".join(f'"{col}"' for col in insertable)
    json_positions = _json_positions(table, insertable)
    payload = []
    for row in rows:
        values = [row[i] for i in indexes]
        for position in json_positions:
            value = values[position]
            if isinstance(value, (dict, list)):
                values[position] = Json(value)
        payload.append(tuple(values))

    with target_connection.cursor() as cursor:
        bootstrap._set_transaction_timeouts(cursor)
        overriding = " overriding system value" if table.has_always_identity else ""
        execute_values(
            cursor,
            f'insert into "{table.schema}"."{table.name}" ({column_list})'
            f"{overriding} values %s",
            payload,
            page_size=500,
        )
        _fixup_sequences(cursor, table)
    # No commit here.  The whole copy is one transaction owned by run() so that
    # a failure mid-way leaves control exactly as it was, rather than committed
    # up to some arbitrary table boundary.


def _fixup_sequences(cursor, table: TableInfo) -> None:
    for column in table.insertable_columns:
        cursor.execute(
            "select pg_get_serial_sequence(%s, %s)",
            (f"{table.schema}.{table.name}", column),
        )
        sequence_name = cursor.fetchone()[0]
        if sequence_name is None:
            continue
        cursor.execute(
            f'select coalesce(max("{column}"), 0) from "{table.schema}"."{table.name}"'
        )
        max_value = cursor.fetchone()[0]
        if not max_value:
            # No rows: leave the sequence untouched.  setval(seq, 1, true) would
            # consume value 1 and make the next insert start at 2.
            continue
        cursor.execute("select setval(%s, %s, true)", (sequence_name, max_value))


# ---------------------------------------------------------------------------
# Post-copy validation
# ---------------------------------------------------------------------------


def compute_table_checksum(
    cursor, table: TableInfo, apply_exclusion: bool = False
) -> tuple[str, int]:
    all_columns = table.all_column_names
    order_clause = ", ".join(f'"{col}"' for col in table.primary_key_columns)
    select_clause = ", ".join(f'"{col}"' for col in all_columns)
    digest = hashlib.sha256()
    row_count = 0
    with cursor.connection.cursor(name=f"migration_hash_{table.schema}_{table.name}") as named:
        named.itersize = 2000
        named.execute(
            f'select {select_clause} from "{table.schema}"."{table.name}"'
            f"{_where_clause(table, apply_exclusion)} order by {order_clause}"
        )
        for row in named:
            digest.update(row_digest(row).encode("utf-8"))
            row_count += 1
    return digest.hexdigest(), row_count


DOMAIN_INVARIANT_QUERIES: dict[str, str] = {
    "execution.orders": (
        "select count(*), coalesce(sum(requested_quantity), 0), "
        "count(distinct broker_order_id) filter (where broker_order_id is not null) "
        "from execution.orders"
    ),
    "execution.order_events": "select count(*) from execution.order_events",
    "execution.fills": "select count(*), coalesce(sum(quantity), 0) from execution.fills",
    "accounting.journals": (
        "select count(*), count(*) filter (where status = 'POSTED') from accounting.journals"
    ),
    "accounting.journal_lines": (
        "select coalesce(sum(debit), 0), coalesce(sum(credit), 0) from accounting.journal_lines "
        "join accounting.journals using (journal_id) where accounting.journals.status = 'POSTED'"
    ),
    "accounting.positions": "select count(*) from accounting.positions",
    "accounting.cash_balances": (
        "select coalesce(sum(settled_amount), 0) from accounting.cash_balances"
    ),
    "risk.risk_requests": "select count(*) from risk.risk_requests",
    "risk.risk_decisions": "select count(*) from risk.risk_decisions",
}


def run_domain_invariants(
    source_cursor, target_cursor, copy_tables: set[str] | None = None
) -> list[str]:
    """Compare ledger/order invariants for tables the source actually owns.

    Tables declared CONTROL_AUTHORITATIVE are skipped: control holds rows that
    hosted Supabase never had, so an equality check there always fails and
    would drown the real signal.
    """

    problems: list[str] = []
    for qualified, query in DOMAIN_INVARIANT_QUERIES.items():
        if copy_tables is not None and qualified not in copy_tables:
            continue
        source_cursor.execute(query)
        source_value = source_cursor.fetchone()
        target_cursor.execute(query)
        target_value = target_cursor.fetchone()
        if source_value != target_value:
            problems.append(
                f"{qualified}: invariant mismatch (source={source_value}, target={target_value})"
            )
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preflight + conflict scan only; no writes anywhere (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="perform the copy after a clean dry-run scan",
    )
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help="re-run post-copy validation without copying",
    )
    parser.add_argument("--confirm-source-supabase", action="store_true")
    parser.add_argument("--confirm-target-control", action="store_true")
    parser.add_argument(
        "--target-backup-reference",
        default="",
        help="operator attestation: snapshot id or note proving control was backed up",
    )
    parser.add_argument(
        "--scope-file",
        default="",
        help=(
            "path to the reviewed per-table policy manifest; required for "
            "--execute.  Without it a dry-run reports the manifest that would "
            "be needed instead of guessing one."
        ),
    )
    parser.add_argument(
        "--accept-metadata-drift",
        action="append",
        default=[],
        metavar="schema.table:column",
        help=(
            "explicitly accept that a server-generated column differs between "
            "source and target for otherwise-identical rows.  Repeatable.  "
            "Nothing is ignored unless named here."
        ),
    )
    return parser


def _open_connections():
    source_dsn = bootstrap._required_environment("SUPABASE_DATABASE_URL")
    target_dsn = bootstrap._required_environment("CONTROL_DATABASE_URL")
    control_database_name = os.environ.get("HEDGEFUND_CONTROL_DB_NAME", "control").strip()
    if bootstrap.database_name_from_dsn(target_dsn) != control_database_name:
        raise MigrationError("CONTROL_DATABASE_URL does not target HEDGEFUND_CONTROL_DB_NAME")
    source = bootstrap._connect(source_dsn)
    target = bootstrap._connect(target_dsn)
    for connection in (source, target):
        with connection.cursor() as cursor:
            cursor.execute("set time zone 'UTC'")
        connection.commit()
    # Structurally guarantee this tool can never write to hosted Supabase.
    # psycopg2 applies this per transaction rather than as a session-level SET;
    # a session-level read-only SET leaks through the Supabase connection
    # pooler and has previously frozen unrelated workloads sharing the pool.
    source.set_session(readonly=True)
    return source, target


def _guard_read_cursor(cursor) -> None:
    """Bound every long scan so a slow read cannot stall the lock queue."""

    cursor.execute("set local statement_timeout = '600s'")
    cursor.execute("set local lock_timeout = '15s'")


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    execute = bool(arguments.execute)
    validate_only = bool(arguments.validate_only)
    if execute and not (arguments.confirm_source_supabase and arguments.confirm_target_control):
        raise MigrationError(
            "--execute requires --confirm-source-supabase and --confirm-target-control"
        )
    if execute and not arguments.target_backup_reference.strip():
        raise MigrationError("--execute requires --target-backup-reference")

    source, target = _open_connections()
    try:
        seed = resolve_seed_identity()
        with source.cursor() as source_cursor, target.cursor() as target_cursor:
            verify_source_migration_versions(source_cursor)
            verify_target_migration_history(target_cursor)

            source_schema = introspect_domain_schema(source_cursor)
            target_schema = introspect_domain_schema(target_cursor)
            # The manifest must be read before the structural check, because it
            # decides which tables the check applies to.
            scope_path_early = arguments.scope_file.strip()
            scope_early = load_scope(Path(scope_path_early)) if scope_path_early else {}
            copy_scope = {
                key
                for key in source_schema
                if scope_early.get(f"{key[0]}.{key[1]}", POLICY_COPY) == POLICY_COPY
            }
            problems = compare_schemas(
                source_schema, target_schema, scope=copy_scope if scope_early else None
            )
            if problems:
                raise MigrationError(
                    "schema compatibility FAILED: " + "; ".join(problems[:20])
                )

            blocking = detect_insert_blocking_triggers(target_cursor, copy_scope)
            if blocking:
                raise MigrationError(
                    "target refuses plain inserts on tables in COPY scope: "
                    + "; ".join(blocking)
                    + ". Move the table to CONTROL_AUTHORITATIVE, or get an "
                    "explicit decision to suspend the guard -- this tool will "
                    "not disable an audit trigger on its own"
                )

            # Order only the tables being copied.  Classifying the other 203
            # would pull ~135k control rows into memory to prove something the
            # manifest already decided.
            order = dependency_order(
                {key: table for key, table in source_schema.items() if key in copy_scope}
            )
            all_source_keys = sorted(source_schema)

            if validate_only:
                report = {
                    "mode": "validate-only",
                    "tables": [],
                    # Same scope rule as the execute path.  Without it, every
                    # CONTROL_AUTHORITATIVE ledger table reports an "invariant
                    # mismatch" that is simply the declared policy working, and
                    # that noise hides a real regression.
                    "domain_invariants": run_domain_invariants(
                        source_cursor,
                        target_cursor,
                        {f"{key[0]}.{key[1]}" for key in order},
                    ),
                }
                for key in order:
                    table = source_schema[key]
                    source_hash, source_rows = compute_table_checksum(source_cursor, table)
                    target_hash, target_rows = compute_table_checksum(
                        target_cursor, table, apply_exclusion=True
                    )
                    report["tables"].append(
                        {
                            "table": table.qualified,
                            "source_rows": source_rows,
                            "target_rows": target_rows,
                            "match": source_hash == target_hash,
                        }
                    )
                return report

            if execute and not scope_path_early:
                raise MigrationError(
                    "--execute requires --scope-file; every table must carry a "
                    "reviewed policy before any row is written"
                )
            scope = scope_early
            accepted_drift = frozenset(arguments.accept_metadata_drift)

            undeclared = [
                f"{key[0]}.{key[1]}"
                for key in all_source_keys
                if f"{key[0]}.{key[1]}" not in scope
            ]
            if scope and undeclared:
                raise MigrationError(
                    f"{len(undeclared)} table(s) have no policy in the scope "
                    f"manifest, e.g. {', '.join(undeclared[:5])}; declare each "
                    "as COPY or CONTROL_AUTHORITATIVE"
                )

            classifications: dict[tuple[str, str], TableClassification] = {}
            for key in order:
                qualified = f"{key[0]}.{key[1]}"
                classifications[key] = classify_table(
                    source_cursor,
                    target_cursor,
                    source_schema[key],
                    seed,
                    policy=scope.get(qualified, POLICY_COPY),
                    accepted_drift=accepted_drift,
                )

            if not scope:
                # No manifest: report what one would have to say, and stop.
                # Proposing a policy here would be guessing which database wins.
                return {
                    "mode": "dry-run",
                    "migration_readiness": "SCOPE MANIFEST REQUIRED",
                    "tables_in_scope": len(order),
                    "undeclared_tables": len(undeclared),
                    "needs_decision": [
                        {
                            "table": classifications[key].table.qualified,
                            "source_rows": classifications[key].source_count,
                            "target_rows": classifications[key].target_count,
                        }
                        for key in order
                        if classifications[key].source_count
                        or classifications[key].target_count
                    ],
                }

            conflict_tables = [
                c
                for c in classifications.values()
                if c.policy == POLICY_COPY and (c.conflicts or c.unexplained_target_only)
            ]
            report: dict[str, Any] = {
                "mode": "execute" if execute else "dry-run",
                "tables_in_scope": len(order),
                "conflicts": [
                    {
                        "table": c.table.qualified,
                        "pk_conflicts": c.conflicts,
                        "unexplained_target_only": c.unexplained_target_only,
                    }
                    for c in conflict_tables
                ],
                "per_table": [
                    {
                        "table": classifications[key].table.qualified,
                        "source_rows": classifications[key].source_count,
                        "target_rows": classifications[key].target_count,
                        "policy": classifications[key].policy,
                        "to_insert": len(classifications[key].to_insert),
                        "already_present": classifications[key].already_present,
                        "divergences": classifications[key].divergences,
                    }
                    for key in order
                ],
            }
            if conflict_tables:
                report["migration_readiness"] = "FAIL"
                return report
            report["migration_readiness"] = "PASS"

            if not execute:
                return report

            run_id = str(uuid5(NAMESPACE_URL, f"hgfinance-data-migration:{time.time()}"))
            with target.cursor() as trace_cursor:
                trace_cursor.execute(
                    """
                    insert into audit.traces
                      (trace_id, trace_type, root_event_type, environment,
                       started_at, status, metadata)
                    values (%s, 'DATA_MIGRATION', 'supabase_to_aws_control',
                            'aws', now(), 'RUNNING', %s)
                    """,
                    (run_id, json.dumps({"target_backup_reference": arguments.target_backup_reference})),
                )
            target.commit()

            try:
                with target.cursor() as constraint_cursor:
                    # Deferring lets the single transaction insert in FK order
                    # without tripping on any deferrable cycle in the graph.
                    constraint_cursor.execute("set constraints all deferred")
                copied_tables = 0
                for key in order:
                    rows = classifications[key].to_insert
                    if not rows:
                        continue
                    copy_table(target, source_schema[key], rows)
                    copied_tables += 1
                target.commit()
            except Exception:
                # The copy rolls back whole, but the trace row was committed
                # before it started.  Leaving it RUNNING would misreport a dead
                # run as in-flight forever, so close it out honestly.
                target.rollback()
                with target.cursor() as failed_cursor:
                    failed_cursor.execute(
                        """
                        update audit.traces
                           set ended_at = now(), status = 'FAILED'
                         where trace_id = %s
                        """,
                        (run_id,),
                    )
                target.commit()
                raise

            copy_keys = [
                key for key in order if classifications[key].policy == POLICY_COPY
            ]
            with source.cursor() as sc, target.cursor() as tc:
                _guard_read_cursor(sc)
                _guard_read_cursor(tc)
                invariant_problems = run_domain_invariants(
                    sc, tc, {f"{k[0]}.{k[1]}" for k in copy_keys}
                )
                level_mismatches: list[str] = []
                # A CONTROL_AUTHORITATIVE table is expected to differ -- that is
                # the whole point of the declaration -- so comparing its
                # checksum would report a mismatch on every healthy run.
                for key in copy_keys:
                    table = source_schema[key]
                    source_hash, source_rows = compute_table_checksum(sc, table)
                    target_hash, target_rows = compute_table_checksum(
                        tc, table, apply_exclusion=True
                    )
                    if source_hash != target_hash or source_rows != target_rows:
                        level_mismatches.append(table.qualified)

            final_status = "COMPLETED" if not (invariant_problems or level_mismatches) else "PARTIAL"
            with target.cursor() as trace_cursor:
                trace_cursor.execute(
                    """
                    update audit.traces
                       set ended_at = now(), status = %s,
                           metadata = metadata || %s::jsonb
                     where trace_id = %s
                    """,
                    (
                        final_status,
                        json.dumps(
                            {
                                "invariant_problems": invariant_problems,
                                "checksum_mismatches": level_mismatches,
                            }
                        ),
                        run_id,
                    ),
                )
            target.commit()

            report["run_id"] = run_id
            report["status"] = final_status
            report["domain_invariants"] = invariant_problems
            report["checksum_mismatches"] = level_mismatches
            return report
    finally:
        source.close()
        target.close()


def _write_report(report: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"supabase_to_control_{timestamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = run(arguments)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # do not render driver errors or DSNs
        print(f"ERROR: migration failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, default=str))
    if report.get("mode") != "validate-only":
        path = _write_report(report)
        print(f"report written: {path}")
    if report.get("migration_readiness") == "FAIL":
        return 1
    if report.get("status") == "PARTIAL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
