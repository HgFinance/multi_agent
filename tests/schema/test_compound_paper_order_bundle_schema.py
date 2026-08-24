from pathlib import Path


SQL = Path(
    "supabase/migrations/20260824000600_compound_paper_order_bundles.sql"
).read_text(encoding="utf-8")

WORKER_POLICY_SQL = Path(
    "supabase/migrations/20260824001000_conditional_worker_bundle_request_read.sql"
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


def test_conditional_worker_has_read_only_request_rls_policy() -> None:
    assert "for select" in WORKER_POLICY_SQL.lower()
    assert "to svc_conditional_rule_worker" in WORKER_POLICY_SQL
    assert "for update" not in WORKER_POLICY_SQL.lower()
    assert "for insert" not in WORKER_POLICY_SQL.lower()
    assert "for delete" not in WORKER_POLICY_SQL.lower()
