from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260820000300_conditional_paper_rules.sql"
)


def compact() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_rule_store_is_paper_only_and_confirmation_gated() -> None:
    sql = compact()

    assert "execution_mode = 'paper'" in sql
    assert "repeat_policy = 'once'" in sql
    assert "confirmation_sha256 is not null and confirmed_at is not null" in sql
    assert "market_closed_policy = 'reject_trigger'" in sql


def test_evaluation_and_trigger_idempotency_are_durable() -> None:
    sql = compact()

    assert "unique (rule_id, rule_version, evaluation_key)" in sql
    assert "evaluation_id text not null unique" in sql
    assert "trigger_id text not null unique" in sql
    assert "idempotency_key text not null unique" in sql
    assert "conditional_rule_outbox_pending_idx" in sql


def test_rule_roles_cannot_write_directives_or_read_portfolios() -> None:
    sql = compact()

    assert "conditional rule roles exceed their authority boundary" in sql
    assert "'svc_conditional_rule_worker','execution.user_directives','insert'" in sql
    assert "'svc_conditional_rule_worker','accounting.positions','select'" in sql
    assert "grant select on execution.conditional_rule_triggers" in sql
    assert "to svc_trading_api" in sql


def test_rule_versions_and_evaluations_are_append_only_for_callers() -> None:
    sql = compact()

    assert "grant select, insert on execution.conditional_trade_rule_versions" not in sql
    assert "grant select, insert on execution.conditional_trade_rules," in sql
    assert "execution.conditional_trade_rule_versions" in sql
    assert "grant update ( state,current_version" in sql
    assert "grant update (state,guard_code)" in sql


def test_lifecycle_trigger_rejects_unsafe_transitions() -> None:
    sql = compact()

    assert "guard_conditional_rule_state_transition" in sql
    assert "invalid conditional rule transition" in sql
    assert "old.state='active' and new.state in" in sql
    assert "'paused','triggered','expired','cancelled','failed'" in sql
