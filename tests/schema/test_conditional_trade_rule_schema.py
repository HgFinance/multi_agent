from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260820000300_conditional_paper_rules.sql"
)
TRADING_EVALUATION_READ_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260824000700_conditional_trading_evaluation_read.sql"
)
OUTBOX_CLAIM_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260827000200_conditional_rule_outbox_claim_lease.sql"
)
INTRADAY_OCO_CONTRACT_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260829000100_conditional_rule_intraday_oco_contract.sql"
)
TRAILING_STOP_STATE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260829000200_conditional_rule_trailing_stop_state.sql"
)
ACTIVATION_LIFETIME_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260829000300_conditional_rule_activation_lifetime.sql"
)
TEMPORAL_STATE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260831000200_conditional_rule_temporal_state.sql"
)
TRAILING_RETURN_BASELINE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260831000300_conditional_rule_trailing_return_baseline.sql"
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


def test_trading_admission_has_read_only_paper_evaluation_access() -> None:
    sql = " ".join(
        TRADING_EVALUATION_READ_MIGRATION.read_text(encoding="utf-8")
        .lower()
        .split()
    )

    assert "grant select on execution.conditional_rule_evaluations to svc_trading_api" in sql
    assert "conditional_rule_evaluations_trading_select" in sql
    assert "rule.execution_mode = 'paper'" in sql
    assert "grant insert" not in sql
    assert "grant update" not in sql


def test_lifecycle_trigger_rejects_unsafe_transitions() -> None:
    sql = compact()

    assert "guard_conditional_rule_state_transition" in sql
    assert "invalid conditional rule transition" in sql
    assert "old.state='active' and new.state in" in sql
    assert "'paused','triggered','expired','cancelled','failed'" in sql


def test_outbox_claim_lease_is_worker_writable_and_pairwise() -> None:
    sql = " ".join(
        OUTBOX_CLAIM_MIGRATION.read_text(encoding="utf-8").lower().split()
    )

    assert "add column if not exists claim_token text" in sql
    assert "add column if not exists claim_expires_at timestamptz" in sql
    assert "conditional_rule_outbox_claim_pair_check" in sql
    assert "grant update (claim_token,claim_expires_at)" in sql
    assert "conditional_rule_outbox_claim_idx" in sql


def test_intraday_timeframe_and_oco_worker_contract_is_migrated() -> None:
    sql = " ".join(
        INTRADAY_OCO_CONTRACT_MIGRATION.read_text(encoding="utf-8")
        .lower()
        .split()
    )

    assert "drop constraint if exists conditional_trade_rules_primary_timeframe_check" in sql
    assert "primary_timeframe in ('1m','3m','5m','10m','15m','30m','1h','1d')" in sql
    assert "conditional_rule_versions_oco_group_idx" in sql
    assert "(spec->>'oco_group_id')" in sql


def test_trailing_stop_state_is_durable_and_worker_only() -> None:
    sql = " ".join(
        TRAILING_STOP_STATE_MIGRATION.read_text(encoding="utf-8")
        .lower()
        .split()
    )

    assert "create table execution.conditional_rule_trailing_states" in sql
    assert "primary key (rule_id, rule_version)" in sql
    assert "on delete cascade" in sql
    assert "enable row level security" in sql
    assert "grant select, insert, update on execution.conditional_rule_trailing_states" in sql
    assert "to svc_conditional_rule_worker" in sql
    assert "conditional_rule_trailing_states_worker_all" in sql


def test_fill_gated_activation_lifetime_uses_only_the_governed_krx_calendar() -> None:
    sql = " ".join(
        ACTIVATION_LIFETIME_MIGRATION.read_text(encoding="utf-8").lower().split()
    )

    assert "on reference.market_calendar_versions to svc_conditional_rule_worker" in sql
    assert "on reference.market_sessions to svc_conditional_rule_worker" in sql
    assert "grant update (expires_at) on execution.conditional_trade_rules to svc_conditional_rule_worker" in sql
    assert "market_sessions_conditional_rule_worker_krx_select" in sql
    assert "conditional_trade_rule_versions', 'update'" in sql


def test_temporal_sequence_state_is_durable_and_worker_only() -> None:
    sql = " ".join(
        TEMPORAL_STATE_MIGRATION.read_text(encoding="utf-8").lower().split()
    )

    assert "create table execution.conditional_rule_temporal_states" in sql
    assert "remaining_bars integer not null" in sql
    assert "primary key (rule_id, rule_version)" in sql
    assert "on delete cascade" in sql
    assert "enable row level security" in sql
    assert "grant select, insert, update, delete" in sql
    assert "to svc_conditional_rule_worker" in sql
    assert "conditional_rule_temporal_states_worker_all" in sql


def test_trailing_return_points_persists_the_original_cost_basis() -> None:
    sql = " ".join(
        TRAILING_RETURN_BASELINE_MIGRATION.read_text(encoding="utf-8")
        .lower()
        .split()
    )

    assert "alter table execution.conditional_rule_trailing_states" in sql
    assert "add column baseline_average_entry_price numeric(30,10)" in sql
    assert "baseline_average_entry_price is null or baseline_average_entry_price > 0" in sql
