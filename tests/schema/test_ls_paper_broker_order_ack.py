from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260820000500_ls_paper_broker_order_ack.sql"
)


def test_ls_paper_ack_grant_is_column_scoped_and_audited() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.startswith("begin;")
    assert sql.rstrip().endswith("commit;")
    assert "grant update (broker_order_id)" in sql
    assert "on execution.user_directive_legs to svc_trading_api" in sql
    assert "has_column_privilege" in sql
    assert "'broker_order_id'" in sql
    assert "'directive_id'" in sql
    assert "'instrument_id'" in sql
    assert "'requested_quantity'" in sql
    assert "has_table_privilege" in sql
    assert "'delete'" in sql


def test_ls_paper_ack_repair_does_not_grant_table_wide_update() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "grant update on execution.user_directive_legs" not in sql
    assert "grant delete" not in sql
