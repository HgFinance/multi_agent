from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "migrate_supabase_to_aws_control.py"
SPEC = importlib.util.spec_from_file_location("migrate_supabase_to_aws_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
migrate = importlib.util.module_from_spec(SPEC)
import sys

sys.modules[SPEC.name] = migrate
SPEC.loader.exec_module(migrate)


def make_table(schema, name, *, pk="id", fks=(), columns=None, constraints=None):
    if columns is None:
        columns = (migrate.ColumnInfo(pk, "uuid", True, False, 1),)
    if constraints is None:
        constraints = (migrate.ConstraintInfo("pk", "p", False, False, f"PRIMARY KEY ({pk})"),)
    foreign_keys = tuple(
        migrate.ForeignKeyEdge(f"fk_{i}", deferrable, ref_schema, ref_table)
        for i, (deferrable, ref_schema, ref_table) in enumerate(fks)
    )
    return migrate.TableInfo(
        schema=schema, name=name, columns=columns, constraints=constraints, foreign_keys=foreign_keys
    )


# ---------------------------------------------------------------------------
# Static contracts: this tool must never touch Auth or invent new env names
# ---------------------------------------------------------------------------


def test_never_references_local_auth_users():
    source_text = MODULE_PATH.read_text().lower()
    assert "into auth." not in source_text
    assert "insert into auth" not in source_text
    assert "create schema" not in source_text


def test_excluded_schemas_cover_known_supabase_managed_schemas():
    for schema in ("auth", "storage", "realtime", "vault", "supabase_migrations", "graphql"):
        assert schema in migrate.EXCLUDED_SCHEMAS
    assert "public" in migrate.NO_BASE_TABLE_SCHEMAS
    assert "api" in migrate.NO_BASE_TABLE_SCHEMAS


def test_reuses_bootstrap_module_symbols_instead_of_reimplementing():
    for name in (
        "BootstrapError",
        "discover_migrations",
        "CONTROL_MIGRATIONS",
        "CONTROL_PATTERN",
        "validate_applied_prefix",
        "_checksum_from_statements",
        "_connect",
        "_required_environment",
        "database_name_from_dsn",
        "DEFAULT_USER_ID",
        "DEFAULT_FUND_ID",
        "DEFAULT_BOOK_ID",
        "ACCOUNT_CHART",
    ):
        assert hasattr(migrate.bootstrap, name)


# ---------------------------------------------------------------------------
# Primary key / column introspection helpers
# ---------------------------------------------------------------------------


def test_primary_key_columns_single_and_composite():
    single = make_table("execution", "orders", pk="order_id")
    assert single.primary_key_columns == ("order_id",)

    composite = make_table(
        "governance",
        "fund_memberships",
        columns=(
            migrate.ColumnInfo("fund_id", "uuid", True, False, 1),
            migrate.ColumnInfo("user_id", "uuid", True, False, 2),
            migrate.ColumnInfo("role", "text", True, False, 3),
        ),
        constraints=(
            migrate.ConstraintInfo(
                "pk", "p", False, False, "PRIMARY KEY (fund_id, user_id, role)"
            ),
        ),
    )
    assert composite.primary_key_columns == ("fund_id", "user_id", "role")


def test_table_without_primary_key_refuses_to_migrate():
    table = make_table("x", "y", constraints=())
    with pytest.raises(migrate.MigrationError):
        _ = table.primary_key_columns


def test_generated_columns_excluded_from_insertable_columns():
    table = make_table(
        "accounting",
        "journal_lines",
        pk="journal_line_id",
        columns=(
            migrate.ColumnInfo("journal_line_id", "uuid", True, False, 1),
            migrate.ColumnInfo("debit", "numeric", True, False, 2),
            migrate.ColumnInfo("base_debit", "numeric", True, True, 3),
        ),
    )
    assert table.insertable_columns == ("journal_line_id", "debit")
    assert table.all_column_names == ("journal_line_id", "debit", "base_debit")


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------


def test_compare_schemas_identical_is_clean():
    table = make_table("execution", "orders", pk="order_id")
    assert migrate.compare_schemas({("execution", "orders"): table}, {("execution", "orders"): table}) == []


def test_compare_schemas_detects_missing_table():
    table = make_table("execution", "orders", pk="order_id")
    problems = migrate.compare_schemas({("execution", "orders"): table}, {})
    assert any("missing on target" in p for p in problems)


def test_compare_schemas_detects_type_drift():
    source = make_table("execution", "orders", pk="order_id")
    target = make_table(
        "execution",
        "orders",
        pk="order_id",
        columns=(migrate.ColumnInfo("order_id", "text", True, False, 1),),
    )
    problems = migrate.compare_schemas({("execution", "orders"): source}, {("execution", "orders"): target})
    assert any("type/nullability drift" in p for p in problems)


def test_compare_schemas_detects_constraint_drift():
    source = make_table("execution", "orders", pk="order_id")
    target = make_table(
        "execution",
        "orders",
        pk="order_id",
        constraints=(
            migrate.ConstraintInfo("pk", "p", False, False, "PRIMARY KEY (order_id, client_order_id)"),
        ),
    )
    problems = migrate.compare_schemas({("execution", "orders"): source}, {("execution", "orders"): target})
    assert any("constraint pk" in p and "definition drift" in p for p in problems)


# ---------------------------------------------------------------------------
# FK dependency ordering
# ---------------------------------------------------------------------------


def test_dependency_order_respects_layered_fk_dag():
    tables = {
        ("governance", "a"): make_table("governance", "a"),
        ("reference", "b"): make_table("reference", "b", fks=[(False, "governance", "a")]),
        ("execution", "c"): make_table(
            "execution", "c", fks=[(False, "reference", "b"), (False, "governance", "a")]
        ),
    }
    order = migrate.dependency_order(tables)
    assert order.index(("governance", "a")) < order.index(("reference", "b"))
    assert order.index(("reference", "b")) < order.index(("execution", "c"))


def test_dependency_order_rejects_non_deferrable_cycle():
    tables = {
        ("x", "p"): make_table("x", "p", fks=[(False, "x", "q")]),
        ("x", "q"): make_table("x", "q", fks=[(False, "x", "p")]),
    }
    with pytest.raises(migrate.MigrationError):
        migrate.dependency_order(tables)


def test_dependency_order_allows_fully_deferrable_cycle():
    tables = {
        ("x", "p"): make_table("x", "p", fks=[(True, "x", "q")]),
        ("x", "q"): make_table("x", "q", fks=[(True, "x", "p")]),
    }
    order = migrate.dependency_order(tables)
    assert set(order) == {("x", "p"), ("x", "q")}


def test_dependency_order_ignores_fk_targets_outside_migration_scope():
    tables = {
        ("execution", "c"): make_table("execution", "c", fks=[(False, "reference", "not_in_scope")]),
    }
    order = migrate.dependency_order(tables)
    assert order == [("execution", "c")]


# ---------------------------------------------------------------------------
# Content-hash canonicalization
# ---------------------------------------------------------------------------


def test_row_digest_stable_under_dict_key_order():
    a = migrate.row_digest([{"b": 1, "a": 2}])
    b = migrate.row_digest([{"a": 2, "b": 1}])
    assert a == b


def test_row_digest_sensitive_to_value_change():
    a = migrate.row_digest([UUID("00000000-0000-4000-8000-00000000cec0")])
    b = migrate.row_digest([UUID("00000000-0000-4000-8000-00000000cec1")])
    assert a != b


def test_row_digest_covers_domain_types():
    values = [
        UUID("00000000-0000-4000-8000-00000000cec0"),
        Decimal("1000000000.0000000000"),
        datetime(2026, 8, 24, tzinfo=timezone.utc),
        {"seed": "aws-paper"},
        ["ACTIVE", "SUSPENDED"],
        None,
        True,
    ]
    digest = migrate.row_digest(values)
    assert isinstance(digest, str) and len(digest) == 64


def test_row_digest_naive_vs_aware_timestamp_differ_only_when_offset_differs():
    naive = migrate.row_digest([datetime(2026, 8, 24, 9, 0, 0)])
    aware_utc = migrate.row_digest([datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc)])
    assert naive != aware_utc  # distinct representations; never silently coerced


# ---------------------------------------------------------------------------
# CLI / execution-mode safety
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_posture():
    args = migrate.build_parser().parse_args([])
    assert args.dry_run is False and args.execute is False and args.validate_only is False


def test_execute_and_dry_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        migrate.build_parser().parse_args(["--dry-run", "--execute"])


def test_execute_without_confirm_flags_is_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_DATABASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://user:pass@host/control")
    args = migrate.build_parser().parse_args(["--execute", "--target-backup-reference", "snap-1"])
    with pytest.raises(migrate.MigrationError, match="confirm"):
        migrate.run(args)


def test_execute_without_backup_reference_is_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_DATABASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://user:pass@host/control")
    args = migrate.build_parser().parse_args(
        ["--execute", "--confirm-source-supabase", "--confirm-target-control"]
    )
    with pytest.raises(migrate.MigrationError, match="backup"):
        migrate.run(args)


def test_main_does_not_render_unexpected_exception_details(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_DATABASE_URL", raising=False)
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    exit_code = migrate.main(["--dry-run"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR:" in captured.err
    assert "postgresql://" not in captured.err
    assert "password" not in captured.err.lower()


# ---------------------------------------------------------------------------
# Known bootstrap-seed identity resolution (Case D allowlist)
# ---------------------------------------------------------------------------


def test_resolve_seed_identity_defaults_match_bootstrap_constants(monkeypatch):
    for key in ("PAPER_SEED_USER_ID", "PAPER_SEED_FUND_ID", "PAPER_SEED_BOOK_ID"):
        monkeypatch.delenv(key, raising=False)
    seed = migrate.resolve_seed_identity()
    assert seed.user_id == migrate.bootstrap.DEFAULT_USER_ID
    assert seed.fund_id == migrate.bootstrap.DEFAULT_FUND_ID
    assert seed.book_id == migrate.bootstrap.DEFAULT_BOOK_ID
    assert set(seed.account_codes) == {code for code, _name, _type in migrate.bootstrap.ACCOUNT_CHART}


def test_is_known_seed_row_matches_only_configured_identity():
    seed = migrate.resolve_seed_identity()
    user_table = make_table("governance", "user_profiles", pk="user_id")
    assert migrate.is_known_seed_row(user_table, (seed.user_id,), seed) is True
    assert migrate.is_known_seed_row(user_table, (UUID(int=42),), seed) is False


# ---------------------------------------------------------------------------
# Live-DB integration tests (require two disposable Postgres instances,
# never the real Supabase project). Skipped unless both env vars are set,
# matching the existing tests/schema/*_pg.py convention in this repo.
# ---------------------------------------------------------------------------

LIVE_SOURCE = os.environ.get("MIGRATION_TEST_SOURCE_DATABASE_URL", "")
LIVE_TARGET = os.environ.get("MIGRATION_TEST_TARGET_DATABASE_URL", "")
requires_live_db = pytest.mark.skipif(
    not (LIVE_SOURCE and LIVE_TARGET),
    reason="set MIGRATION_TEST_SOURCE_DATABASE_URL and MIGRATION_TEST_TARGET_DATABASE_URL "
    "to disposable local Postgres instances to run live-DB migration tests",
)


@pytest.fixture()
def live_connections():
    import psycopg2
    from psycopg2.extras import register_uuid

    register_uuid()
    source = psycopg2.connect(LIVE_SOURCE)
    target = psycopg2.connect(LIVE_TARGET)
    for connection in (source, target):
        with connection.cursor() as cursor:
            cursor.execute("drop schema if exists mig_test cascade")
            cursor.execute("create schema mig_test")
            cursor.execute(
                """
                create table mig_test.parent (
                  parent_id uuid primary key,
                  name text not null
                )
                """
            )
            cursor.execute(
                """
                create table mig_test.child (
                  child_id uuid primary key,
                  parent_id uuid not null references mig_test.parent(parent_id),
                  amount numeric(10,2) not null
                )
                """
            )
        connection.commit()
    yield source, target
    for connection in (source, target):
        with connection.cursor() as cursor:
            cursor.execute("drop schema if exists mig_test cascade")
        connection.commit()
        connection.close()


def _mig_test_tables(cursor):
    return migrate.introspect_domain_schema(cursor)


@requires_live_db
def test_live_empty_target_copies_all_rows(live_connections):
    source, target = live_connections
    parent_id = UUID(int=1)
    with source.cursor() as cursor:
        cursor.execute(
            "insert into mig_test.parent (parent_id, name) values (%s, %s)", (parent_id, "acme")
        )
        cursor.execute(
            "insert into mig_test.child (child_id, parent_id, amount) values (%s, %s, %s)",
            (UUID(int=2), parent_id, Decimal("10.50")),
        )
    source.commit()

    seed = migrate.resolve_seed_identity()
    with source.cursor() as sc, target.cursor() as tc:
        tables = migrate.introspect_domain_schema(sc)
        parent = tables[("mig_test", "parent")]
        child = tables[("mig_test", "child")]
        classification = migrate.classify_table(sc, tc, parent, seed)
        assert classification.target_count == 0
        assert len(classification.to_insert) == 1
        migrate.copy_table(target, parent, classification.to_insert)
        child_classification = migrate.classify_table(sc, tc, child, seed)
        migrate.copy_table(target, child, child_classification.to_insert)

    with target.cursor() as cursor:
        cursor.execute("select count(*) from mig_test.parent")
        assert cursor.fetchone()[0] == 1
        cursor.execute("select count(*) from mig_test.child")
        assert cursor.fetchone()[0] == 1


@requires_live_db
def test_live_identical_row_is_idempotent_skip(live_connections):
    source, target = live_connections
    parent_id = UUID(int=3)
    for connection in (source, target):
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into mig_test.parent (parent_id, name) values (%s, %s)",
                (parent_id, "identical"),
            )
        connection.commit()

    seed = migrate.resolve_seed_identity()
    with source.cursor() as sc, target.cursor() as tc:
        tables = migrate.introspect_domain_schema(sc)
        parent = tables[("mig_test", "parent")]
        classification = migrate.classify_table(sc, tc, parent, seed)
        assert classification.already_present == 1
        assert classification.to_insert == []
        assert classification.conflicts == []


@requires_live_db
def test_live_pk_collision_with_different_content_is_a_conflict(live_connections):
    source, target = live_connections
    parent_id = UUID(int=4)
    with source.cursor() as cursor:
        cursor.execute(
            "insert into mig_test.parent (parent_id, name) values (%s, %s)", (parent_id, "source-name")
        )
    source.commit()
    with target.cursor() as cursor:
        cursor.execute(
            "insert into mig_test.parent (parent_id, name) values (%s, %s)", (parent_id, "target-name")
        )
    target.commit()

    seed = migrate.resolve_seed_identity()
    with source.cursor() as sc, target.cursor() as tc:
        tables = migrate.introspect_domain_schema(sc)
        parent = tables[("mig_test", "parent")]
        classification = migrate.classify_table(sc, tc, parent, seed)
        assert classification.conflicts == [str(parent_id)]


@requires_live_db
def test_live_unexplained_target_only_row_is_flagged(live_connections):
    source, target = live_connections
    with target.cursor() as cursor:
        cursor.execute(
            "insert into mig_test.parent (parent_id, name) values (%s, %s)",
            (UUID(int=5), "target-only"),
        )
    target.commit()

    seed = migrate.resolve_seed_identity()
    with source.cursor() as sc, target.cursor() as tc:
        tables = migrate.introspect_domain_schema(sc)
        parent = tables[("mig_test", "parent")]
        classification = migrate.classify_table(sc, tc, parent, seed)
        assert classification.unexplained_target_only == [str(UUID(int=5))]


@requires_live_db
def test_live_checksum_matches_after_copy(live_connections):
    source, target = live_connections
    parent_id = UUID(int=6)
    with source.cursor() as cursor:
        cursor.execute(
            "insert into mig_test.parent (parent_id, name) values (%s, %s)", (parent_id, "hash-me")
        )
    source.commit()

    seed = migrate.resolve_seed_identity()
    with source.cursor() as sc, target.cursor() as tc:
        tables = migrate.introspect_domain_schema(sc)
        parent = tables[("mig_test", "parent")]
        classification = migrate.classify_table(sc, tc, parent, seed)
        migrate.copy_table(target, parent, classification.to_insert)

    with source.cursor() as sc, target.cursor() as tc:
        source_hash, source_rows = migrate.compute_table_checksum(sc, parent)
        target_hash, target_rows = migrate.compute_table_checksum(tc, parent)
        assert source_hash == target_hash
        assert source_rows == target_rows == 1
