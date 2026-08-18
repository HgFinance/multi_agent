from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "20260818001500_paper_user_directive_execution.sql"
WORKFLOW_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260818001600_ceo_hermes_paper_order_workflow.sql"
)


def test_user_directive_migration_has_paper_authority_guards():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert sql.lstrip().startswith("begin;") and sql.rstrip().endswith("commit;")
    for marker in (
        "create role svc_trading_api",
        "create role svc_accounting_ledger",
        "with set true, inherit false",
        "nobypassrls",
        "user_directive_proofs",
        "proof_jti text primary key",
        "time_in_force text",
        "expires_at timestamptz",
        "'expired'",
        "active_directive_id, fund_id, book_id",
        "guard_automated_paper_order_admission",
        "pg_advisory_xact_lock",
        "new.broker_adapter <> 'paper'",
        "security definer",
        "membership.role in ('owner','cio','trader')",
        "profile.status='active'",
        "paper_user_directive_fills",
        "trading-user-directive-fill-v1",
        "execution.outbox to svc_trading_api",
        "execution.fills\n  to svc_trading_api",
        "journals_svc_trading_api_select",
        "set true, inherit false",
        "outbox_svc_accounting_ledger_select",
        "outbox_consumed_svc_trading_api_select",
        "journals_svc_accounting_ledger_all",
    ):
        assert marker in sql
    assert " to service_role" not in sql
    assert "where state in ('received', 'running', 'in_progress', 'partial', 'unknown')" not in sql


def test_ceo_hermes_workflow_keeps_authority_paper_only_and_scope_bound():
    sql = WORKFLOW_MIGRATION.read_text(encoding="utf-8").lower()
    assert sql.lstrip().startswith("begin;") and sql.rstrip().endswith("commit;")
    for marker in (
        "create role svc_order_orchestrator",
        "nologin nosuperuser nocreatedb nocreaterole noinherit",
        "noreplication nobypassrls",
        "pool_login name := session_user",
        "with set true, inherit false",
        "role name is occupied by an unsafe role",
        "revoke all privileges on all tables in schema execution",
        "mode text not null default 'paper' check (mode = 'paper')",
        "user_directives_authority_identity_unique",
        "unique (directive_id, user_id, fund_id, book_id)",
        "unique (order_request_id, user_id, fund_id, book_id)",
        "user_directives_source_order_scope_fkey",
        "jsonb_typeof(canonical_payload) = 'object'",
        "jsonb_typeof(interpretation) = 'object'",
        "grant update (\n  ceo_root_task_id, trading_task_id, state, action",
        "grant update (source_order_request_id) on execution.user_directives",
        "user_order_interpretations_svc_order_orchestrator_all",
        "user_order_request_events_svc_order_orchestrator_all",
        "svc_order_orchestrator exceeds its paper workflow boundary",
    ):
        assert marker in sql
    assert "grant select, insert, update on execution.user_order_requests" not in sql
    assert " to service_role" not in sql


def test_directive_repository_is_paper_only_and_unknown_never_auto_expires():
    source = (ROOT / "departments" / "02-trading" / "directives" / "repository.py").read_text(encoding="utf-8")
    assert "and o.broker_adapter='paper'" in source
    assert "and state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED')" in source
    assert "and l.state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED')" in source
    assert "order by v.version desc\n                 limit 1" in source
    assert "left join execution.outbox_consumed consumed" in source
    assert "consumed.consumer='accounting-ledger'" in source


def test_eb_trading_runtime_requires_real_private_market_api():
    compose = (ROOT / "deploy" / "eb" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "TRADING_EXECUTION_MODE: PAPER" in compose
    assert "TRADING_BROKER_ADAPTER: paper" in compose
    assert "TRADING_DIRECTIVE_REPOSITORY: postgres" in compose
    assert "TRADING_DATABASE_ROLE:" in compose
    assert "TRADING_AUTH_MODE: service" in compose
    assert "MARKET_API_URL: ${MARKET_API_URL:?" in compose
    assert "trading-directive-worker:" in compose
    assert 'command: ["python", "-m", "directives.worker"]' in compose


def test_accounting_fill_consumer_reduces_the_operational_pool_role():
    repository = (
        ROOT / "departments" / "05-accounting-portfolio" / "ledger" / "repository.py"
    ).read_text(encoding="utf-8")
    consumer = (
        ROOT / "departments" / "05-accounting-portfolio" / "ledger" / "consumer.py"
    ).read_text(encoding="utf-8")
    local_compose = (
        ROOT / "departments" / "05-accounting-portfolio" / "compose.yaml"
    ).read_text(encoding="utf-8")
    eb_compose = (ROOT / "deploy" / "eb" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert 'ACCOUNTING_LEDGER_DATABASE_ROLE = "svc_accounting_ledger"' in repository
    read_write = 'cur.execute("set transaction read write")'
    reduced_role = 'cur.execute("set local role svc_accounting_ledger")'
    assert read_write in repository
    assert reduced_role in repository
    assert repository.index(read_write) < repository.index(reduced_role)
    assert "accounting ledger consumer requires" in consumer
    for compose in (local_compose, eb_compose):
        assert "ACCOUNTING_DATABASE_ROLE:" in compose
        assert "svc_accounting_ledger" in compose

    pg_smoke = (ROOT / "tests" / "schema" / "paper_user_directive_pg_smoke.sql").read_text(
        encoding="utf-8"
    )
    assert "strategy BUY was hidden before projection receipt ACK" in pg_smoke
    assert "strategy BUY remained pending after projection receipt ACK" in pg_smoke
    assert "set local role svc_accounting_ledger" in pg_smoke
