from pathlib import Path


SQL = Path(
    "supabase/migrations/20260824000600_compound_paper_order_bundles.sql"
).read_text(encoding="utf-8")


def test_compound_bundle_migration_reuses_existing_authority_rows() -> None:
    assert "create table execution.user_paper_order_bundles" in SQL
    assert "immediate_order_request_id uuid not null unique" in SQL
    assert "conditional_rule_id uuid unique" in SQL
    assert "references execution.user_order_requests" in SQL
    assert "references execution.conditional_trade_rules" in SQL
    assert "unique (user_id, client_request_id)" in SQL
    assert "grant select on execution.user_order_requests to svc_conditional_rule_worker" in SQL
    assert "drop table" not in SQL.lower()
    assert "alter table execution.user_order_requests" not in SQL.lower()
