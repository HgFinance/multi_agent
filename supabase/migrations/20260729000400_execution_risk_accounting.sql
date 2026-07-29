begin;

create table accounting.strategy_allocations (
  strategy_allocation_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  strategy_id uuid not null references strategy.strategies(strategy_id),
  capital_limit numeric(30, 10) not null check (capital_limit >= 0),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  effective_from timestamptz not null,
  effective_to timestamptz,
  governance_allocation_id uuid not null references governance.capital_allocations(allocation_id),
  status text not null default 'ACTIVE'
    check (status in ('PENDING', 'ACTIVE', 'SUSPENDED', 'ENDED')),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (book_id, strategy_id, effective_from)
);

create table accounting.portfolio_snapshots (
  portfolio_snapshot_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid references accounting.books(book_id),
  as_of timestamptz not null,
  cash jsonb not null,
  positions jsonb not null,
  gross_exposure numeric(30, 10) not null default 0,
  net_exposure numeric(30, 10) not null default 0,
  nav numeric(30, 10),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  content_hash text not null,
  schema_version integer not null check (schema_version > 0),
  created_at timestamptz not null default now(),
  unique nulls not distinct (fund_id, book_id, as_of, content_hash)
);

create table execution.market_snapshots (
  market_snapshot_id uuid primary key default gen_random_uuid(),
  instrument_id uuid not null references reference.instruments(instrument_id),
  as_of timestamptz not null,
  bid numeric(30, 10),
  ask numeric(30, 10),
  last_price numeric(30, 10),
  mid numeric(30, 10),
  spread numeric(30, 10),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  source_ref text not null,
  content_hash text not null,
  created_at timestamptz not null default now(),
  check (ask is null or bid is null or ask >= bid),
  unique (instrument_id, as_of, content_hash)
);

create table execution.trade_cases (
  trade_case_id uuid primary key references governance.cases(case_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  strategy_family text not null,
  primary_instrument_id uuid references reference.instruments(instrument_id),
  research_packet_id uuid references research.research_packets(research_packet_id),
  signal_id uuid not null references strategy.signals(signal_id),
  case_status text not null,
  thesis jsonb not null,
  invalidation jsonb not null,
  expires_at timestamptz not null,
  created_by text not null,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id)
);

create table execution.trade_case_instruments (
  trade_case_id uuid not null references execution.trade_cases(trade_case_id) on delete cascade,
  instrument_id uuid not null references reference.instruments(instrument_id),
  leg_index integer not null check (leg_index >= 0),
  role text not null,
  target_weight numeric(18, 12),
  target_quantity numeric(30, 10),
  primary key (trade_case_id, leg_index),
  unique (trade_case_id, instrument_id, role)
);

create table execution.intent_groups (
  intent_group_id uuid primary key default gen_random_uuid(),
  trade_case_id uuid not null references execution.trade_cases(trade_case_id),
  fund_id uuid not null references accounting.funds(fund_id),
  capability_profile_id uuid not null references strategy.capability_profiles(capability_profile_id),
  atomicity_policy text not null,
  failure_policy text not null,
  group_status text not null default 'DRAFT'
    check (group_status in ('DRAFT', 'RISK_PENDING', 'APPROVED', 'REJECTED', 'EXECUTING', 'COMPLETED', 'PARTIAL_RECOVERY', 'CANCELLED', 'FAILED_SAFE')),
  gross_target numeric(30, 10),
  net_target numeric(30, 10),
  idempotency_key text not null unique,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table execution.order_intents (
  order_intent_id uuid primary key default gen_random_uuid(),
  trade_case_id uuid not null references execution.trade_cases(trade_case_id),
  intent_group_id uuid not null references execution.intent_groups(intent_group_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  instrument_id uuid not null references reference.instruments(instrument_id),
  side text not null check (side in ('BUY', 'SELL')),
  position_effect text not null check (position_effect in ('OPEN', 'CLOSE', 'INCREASE', 'REDUCE', 'HEDGE')),
  leg_index integer not null check (leg_index >= 0),
  order_type text not null check (order_type in ('MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT', 'PEGGED')),
  quantity numeric(30, 10) not null check (quantity > 0),
  limit_price numeric(30, 10),
  stop_price numeric(30, 10),
  time_in_force text not null check (time_in_force in ('DAY', 'GTC', 'IOC', 'FOK', 'GTD')),
  valid_until timestamptz not null,
  market_snapshot_id uuid not null references execution.market_snapshots(market_snapshot_id),
  risk_request_id uuid,
  risk_decision_id uuid,
  intent_status text not null default 'DRAFT'
    check (intent_status in ('DRAFT', 'RISK_PENDING', 'APPROVED', 'RESIZED', 'REJECTED', 'EXPIRED', 'USER_PENDING', 'USER_APPROVED', 'READY_TO_SUBMIT')),
  idempotency_key text not null unique,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (intent_group_id, leg_index),
  check ((order_type in ('LIMIT', 'STOP_LIMIT') and limit_price is not null) or order_type not in ('LIMIT', 'STOP_LIMIT')),
  check ((order_type in ('STOP', 'STOP_LIMIT') and stop_price is not null) or order_type not in ('STOP', 'STOP_LIMIT'))
);

create table execution.execution_plans (
  execution_plan_id uuid primary key default gen_random_uuid(),
  order_intent_id uuid not null unique references execution.order_intents(order_intent_id),
  algorithm text not null,
  child_order_policy jsonb not null,
  participation_limit numeric(8, 7) check (participation_limit is null or participation_limit between 0 and 1),
  price_limits jsonb not null default '{}'::jsonb,
  stop_conditions jsonb not null,
  expires_at timestamptz not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'COMPLETED', 'CANCELLED', 'FAILED')),
  created_at timestamptz not null default now()
);

create table execution.orders (
  order_id uuid primary key default gen_random_uuid(),
  order_intent_id uuid not null references execution.order_intents(order_intent_id),
  parent_order_id uuid references execution.orders(order_id),
  client_order_id text not null unique,
  broker_order_id text,
  broker_adapter text not null,
  state text not null default 'CREATED'
    check (state in ('CREATED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'EXPIRED', 'UNKNOWN')),
  requested_quantity numeric(30, 10) not null check (requested_quantity > 0),
  filled_quantity numeric(30, 10) not null default 0 check (filled_quantity >= 0),
  average_fill_price numeric(30, 10),
  submitted_at timestamptz,
  acknowledged_at timestamptz,
  last_event_at timestamptz not null default now(),
  version integer not null default 1 check (version > 0),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (filled_quantity <= requested_quantity),
  unique nulls not distinct (broker_adapter, broker_order_id)
);

create table execution.order_events (
  order_event_id uuid primary key default gen_random_uuid(),
  order_id uuid not null references execution.orders(order_id),
  event_type text not null,
  event_time timestamptz not null,
  received_at timestamptz not null,
  broker_adapter text not null,
  broker_event_id text,
  from_state text,
  to_state text not null,
  payload jsonb not null default '{}'::jsonb,
  payload_hash text not null,
  sequence bigint,
  trace_id uuid not null,
  recorded_at timestamptz not null default now(),
  unique nulls not distinct (broker_adapter, broker_event_id),
  unique nulls not distinct (order_id, sequence)
);

create index order_events_order_time_idx
  on execution.order_events (order_id, event_time, received_at);

create table execution.fills (
  fill_id uuid primary key default gen_random_uuid(),
  order_id uuid not null references execution.orders(order_id),
  broker_fill_id text not null,
  instrument_id uuid not null references reference.instruments(instrument_id),
  side text not null check (side in ('BUY', 'SELL')),
  quantity numeric(30, 10) not null check (quantity > 0),
  price numeric(30, 10) not null check (price >= 0),
  gross_amount numeric(30, 10) not null,
  fee_amount numeric(30, 10) not null default 0,
  tax_amount numeric(30, 10) not null default 0,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  liquidity_flag text,
  event_time timestamptz not null,
  received_at timestamptz not null,
  settlement_date date,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  unique (order_id, broker_fill_id)
);

create table execution.broker_sessions (
  broker_session_id uuid primary key default gen_random_uuid(),
  broker_adapter text not null,
  environment text not null,
  account_ref text not null,
  connected_at timestamptz,
  disconnected_at timestamptz,
  last_heartbeat_at timestamptz,
  state text not null check (state in ('CONNECTING', 'HEALTHY', 'DEGRADED', 'DISCONNECTED', 'AUTH_FAILED', 'SAFE_STATE')),
  reason text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (disconnected_at is null or connected_at is null or disconnected_at >= connected_at)
);

create table execution.tca_results (
  tca_result_id uuid primary key default gen_random_uuid(),
  order_id uuid not null references execution.orders(order_id),
  arrival_price numeric(30, 10),
  midpoint_price numeric(30, 10),
  vwap_price numeric(30, 10),
  slippage_bps numeric(20, 8),
  fees_bps numeric(20, 8),
  market_impact_bps numeric(20, 8),
  calculation_version text not null,
  market_data_ref text not null,
  calculated_at timestamptz not null default now(),
  unique (order_id, calculation_version)
);

create table execution.execution_exceptions (
  exception_id uuid primary key default gen_random_uuid(),
  order_id uuid references execution.orders(order_id),
  intent_group_id uuid references execution.intent_groups(intent_group_id),
  exception_type text not null,
  severity text not null check (severity in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  details jsonb not null,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'INVESTIGATING', 'RESOLVED', 'ACCEPTED', 'CLOSED')),
  owner text,
  resolution text,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create table risk.policies (
  policy_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  policy_code text not null,
  version integer not null check (version > 0),
  scope jsonb not null,
  rules jsonb not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  approval_id uuid references governance.approvals(approval_id),
  content_hash text not null,
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from),
  unique (fund_id, policy_code, version),
  unique (fund_id, policy_code, content_hash)
);

create table risk.limits (
  limit_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  policy_id uuid not null references risk.policies(policy_id),
  scope_type text not null,
  scope_id text not null,
  metric text not null,
  soft_limit numeric(38, 12),
  hard_limit numeric(38, 12) not null,
  unit text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now(),
  check (soft_limit is null or soft_limit <= hard_limit),
  check (effective_to is null or effective_to > effective_from),
  unique (policy_id, scope_type, scope_id, metric, effective_from)
);

create table risk.limit_changes (
  change_id uuid primary key default gen_random_uuid(),
  limit_id uuid not null references risk.limits(limit_id),
  before_value jsonb not null,
  after_value jsonb not null,
  reason text not null,
  requested_by text not null,
  approved_by text,
  approval_id uuid references governance.approvals(approval_id),
  trace_id uuid not null,
  occurred_at timestamptz not null default now()
);

create table risk.restricted_items (
  restriction_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  instrument_id uuid references reference.instruments(instrument_id),
  issuer_id uuid references reference.issuers(issuer_id),
  restriction_type text not null,
  source text not null,
  reason text,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'EXPIRED', 'REVOKED')),
  created_at timestamptz not null default now(),
  check (instrument_id is not null or issuer_id is not null),
  check (effective_to is null or effective_to > effective_from)
);

create table risk.counterparties (
  counterparty_id uuid primary key default gen_random_uuid(),
  counterparty_code text not null unique,
  name text not null,
  status text not null check (status in ('ACTIVE', 'WATCH', 'RESTRICTED', 'DEFAULTED', 'CLOSED')),
  exposure_limit numeric(30, 10),
  health jsonb not null,
  observed_at timestamptz not null,
  source text not null,
  created_at timestamptz not null default now()
);

create table risk.margin_rules (
  margin_rule_id uuid primary key default gen_random_uuid(),
  counterparty_id uuid references risk.counterparties(counterparty_id),
  product_scope jsonb not null,
  version integer not null check (version > 0),
  parameters jsonb not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  source text not null,
  content_hash text not null,
  check (effective_to is null or effective_to > effective_from),
  unique nulls not distinct (counterparty_id, version, content_hash)
);

create table risk.risk_requests (
  risk_request_id uuid primary key default gen_random_uuid(),
  intent_group_id uuid not null unique references execution.intent_groups(intent_group_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  capability_profile_id uuid not null references strategy.capability_profiles(capability_profile_id),
  market_snapshot_ids uuid[] not null,
  portfolio_snapshot_id uuid not null references accounting.portfolio_snapshots(portfolio_snapshot_id),
  policy_id uuid not null references risk.policies(policy_id),
  received_at timestamptz not null,
  expires_at timestamptz not null,
  input_hash text not null,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid not null,
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  check (expires_at > received_at),
  unique (input_hash)
);

create table risk.risk_request_items (
  risk_request_item_id uuid primary key default gen_random_uuid(),
  risk_request_id uuid not null references risk.risk_requests(risk_request_id) on delete cascade,
  order_intent_id uuid not null unique references execution.order_intents(order_intent_id),
  instrument_id uuid not null references reference.instruments(instrument_id),
  side text not null check (side in ('BUY', 'SELL')),
  position_effect text not null,
  requested_quantity numeric(30, 10) not null check (requested_quantity > 0),
  requested_price numeric(30, 10),
  leg_index integer not null check (leg_index >= 0),
  unique (risk_request_id, leg_index)
);

create table risk.risk_decisions (
  risk_decision_id uuid primary key default gen_random_uuid(),
  risk_request_id uuid not null references risk.risk_requests(risk_request_id),
  decision text not null check (decision in ('APPROVE', 'RESIZE', 'REJECT')),
  approved_quantity numeric(30, 10),
  max_price numeric(30, 10),
  approved_legs jsonb not null,
  aggregate_exposure jsonb not null,
  valid_until timestamptz not null,
  reason_codes text[] not null,
  check_results jsonb not null,
  calculation_version text not null,
  input_hash text not null,
  created_by_service text not null,
  created_at timestamptz not null default now(),
  unique (risk_request_id, calculation_version),
  check (decision <> 'RESIZE' or approved_legs <> '[]'::jsonb)
);

create table risk.snapshots (
  risk_snapshot_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid references accounting.books(book_id),
  strategy_version_id uuid references strategy.versions(strategy_version_id),
  as_of timestamptz not null,
  gross_exposure numeric(30, 10) not null,
  net_exposure numeric(30, 10) not null,
  value_at_risk numeric(30, 10),
  expected_shortfall numeric(30, 10),
  drawdown numeric(18, 12),
  margin_used numeric(30, 10),
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  input_hash text not null,
  calculation_version text not null,
  created_at timestamptz not null default now(),
  unique nulls not distinct (fund_id, book_id, strategy_version_id, as_of, calculation_version)
);

create table risk.exposure_components (
  exposure_component_id uuid primary key default gen_random_uuid(),
  risk_snapshot_id uuid not null references risk.snapshots(risk_snapshot_id) on delete cascade,
  dimension text not null,
  dimension_id text not null,
  value numeric(38, 12) not null,
  unit text not null,
  metadata jsonb not null default '{}'::jsonb,
  unique (risk_snapshot_id, dimension, dimension_id, unit)
);

create table risk.stress_scenarios (
  scenario_id uuid primary key default gen_random_uuid(),
  scenario_code text not null,
  version integer not null check (version > 0),
  name text not null,
  shocks jsonb not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
  content_hash text not null,
  check (effective_to is null or effective_to > effective_from),
  unique (scenario_code, version),
  unique (scenario_code, content_hash)
);

create table risk.stress_results (
  stress_run_id uuid primary key default gen_random_uuid(),
  risk_snapshot_id uuid not null references risk.snapshots(risk_snapshot_id),
  scenario_id uuid not null references risk.stress_scenarios(scenario_id),
  loss numeric(30, 10) not null,
  breached_limit_ids uuid[] not null default '{}',
  component_results jsonb not null,
  code_version text not null,
  created_at timestamptz not null default now(),
  unique (risk_snapshot_id, scenario_id, code_version)
);

create table risk.breaches (
  breach_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  limit_id uuid not null references risk.limits(limit_id),
  risk_snapshot_id uuid references risk.snapshots(risk_snapshot_id),
  severity text not null check (severity in ('WARNING', 'SOFT', 'HARD', 'CRITICAL')),
  observed_value numeric(38, 12) not null,
  limit_value numeric(38, 12) not null,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'ACKNOWLEDGED', 'MITIGATING', 'RESOLVED', 'WAIVED')),
  owner text,
  due_at timestamptz,
  trace_id uuid not null,
  observed_at timestamptz not null,
  resolved_at timestamptz
);

create table risk.trading_states (
  trading_state_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  scope_type text not null,
  scope_id text not null,
  state text not null check (state in ('ENABLED', 'REDUCE_ONLY', 'ENTRY_BLOCKED', 'HALTED')),
  reason text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  set_by text not null,
  approval_id uuid references governance.approvals(approval_id),
  trace_id uuid not null,
  check (effective_to is null or effective_to > effective_from)
);

create table risk.kill_switch_events (
  kill_switch_event_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  from_state text,
  to_state text not null check (to_state in ('REDUCE_ONLY', 'ENTRY_BLOCKED', 'HALTED', 'ENABLED')),
  trigger_type text not null,
  trigger_details jsonb not null,
  evidence jsonb not null,
  requested_by text not null,
  approved_release_by text,
  trace_id uuid not null,
  occurred_at timestamptz not null,
  released_at timestamptz
);

alter table execution.order_intents
  add constraint order_intents_risk_request_fk
  foreign key (risk_request_id) references risk.risk_requests(risk_request_id),
  add constraint order_intents_risk_decision_fk
  foreign key (risk_decision_id) references risk.risk_decisions(risk_decision_id);

alter table governance.investment_cases
  add constraint investment_cases_portfolio_snapshot_fk
  foreign key (portfolio_snapshot_id) references accounting.portfolio_snapshots(portfolio_snapshot_id),
  add constraint investment_cases_intent_group_fk
  foreign key (intent_group_id) references execution.intent_groups(intent_group_id),
  add constraint investment_cases_risk_decision_fk
  foreign key (risk_decision_id) references risk.risk_decisions(risk_decision_id);

create table accounting.ledger_accounts (
  account_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  account_code text not null,
  name text not null,
  account_type text not null check (account_type in ('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE', 'FX_BRIDGE')),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  parent_account_id uuid references accounting.ledger_accounts(account_id),
  status text not null default 'ACTIVE' check (status in ('ACTIVE', 'INACTIVE')),
  created_at timestamptz not null default now(),
  unique (fund_id, account_code)
);

create table accounting.journals (
  journal_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  event_type text not null,
  source_event_id text not null,
  effective_at timestamptz not null,
  accounting_date date not null,
  base_currency text not null check (base_currency ~ '^[A-Z]{3}$'),
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'POSTED', 'REVERSED', 'VOID')),
  reversal_of_journal_id uuid references accounting.journals(journal_id),
  created_by_service text not null,
  approved_by text,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  posted_at timestamptz,
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (event_type, source_event_id),
  check (reversal_of_journal_id is null or status in ('POSTED', 'DRAFT'))
);

create table accounting.journal_lines (
  journal_line_id uuid primary key default gen_random_uuid(),
  journal_id uuid not null references accounting.journals(journal_id) on delete cascade,
  account_id uuid not null references accounting.ledger_accounts(account_id),
  instrument_id uuid references reference.instruments(instrument_id),
  line_no integer not null check (line_no > 0),
  debit numeric(38, 10) not null default 0 check (debit >= 0),
  credit numeric(38, 10) not null default 0 check (credit >= 0),
  quantity numeric(30, 10),
  unit_price numeric(30, 10),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  fx_rate numeric(30, 12) not null default 1 check (fx_rate > 0),
  base_debit numeric(38, 10) generated always as (debit * fx_rate) stored,
  base_credit numeric(38, 10) generated always as (credit * fx_rate) stored,
  metadata jsonb not null default '{}'::jsonb,
  unique (journal_id, line_no),
  check ((debit > 0 and credit = 0) or (credit > 0 and debit = 0))
);

create table accounting.positions (
  position_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  strategy_version_id uuid references strategy.versions(strategy_version_id),
  instrument_id uuid not null references reference.instruments(instrument_id),
  quantity numeric(30, 10) not null,
  average_cost numeric(30, 10),
  cost_currency text not null check (cost_currency ~ '^[A-Z]{3}$'),
  realized_pnl numeric(30, 10) not null default 0,
  last_journal_id uuid references accounting.journals(journal_id),
  version bigint not null default 1 check (version > 0),
  as_of timestamptz not null,
  updated_at timestamptz not null default now(),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique nulls not distinct (fund_id, book_id, strategy_version_id, instrument_id)
);

create table accounting.cash_balances (
  cash_balance_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  account_id uuid not null references accounting.ledger_accounts(account_id),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  settled_amount numeric(38, 10) not null default 0,
  unsettled_amount numeric(38, 10) not null default 0,
  reserved_amount numeric(38, 10) not null default 0,
  last_journal_id uuid references accounting.journals(journal_id),
  version bigint not null default 1 check (version > 0),
  as_of timestamptz not null,
  updated_at timestamptz not null default now(),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (fund_id, book_id, account_id, currency)
);

create table accounting.valuations (
  valuation_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  instrument_id uuid not null references reference.instruments(instrument_id),
  position_id uuid references accounting.positions(position_id),
  as_of timestamptz not null,
  price numeric(30, 10) not null,
  price_source text not null,
  market_snapshot_id uuid references execution.market_snapshots(market_snapshot_id),
  quantity numeric(30, 10) not null,
  market_value numeric(38, 10) not null,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  fx_rate numeric(30, 12) not null default 1,
  base_market_value numeric(38, 10) not null,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE', 'ESTIMATED')),
  model_version text,
  created_at timestamptz not null default now(),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (fund_id, book_id, instrument_id, as_of, price_source)
);

create table accounting.pnl_snapshots (
  pnl_snapshot_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid references accounting.books(book_id),
  strategy_version_id uuid references strategy.versions(strategy_version_id),
  instrument_id uuid references reference.instruments(instrument_id),
  as_of timestamptz not null,
  realized_pnl numeric(38, 10) not null default 0,
  unrealized_pnl numeric(38, 10) not null default 0,
  fee_pnl numeric(38, 10) not null default 0,
  tax_pnl numeric(38, 10) not null default 0,
  fx_pnl numeric(38, 10) not null default 0,
  total_pnl numeric(38, 10) not null,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  input_hash text not null,
  calculation_version text not null,
  created_at timestamptz not null default now(),
  unique nulls not distinct (fund_id, book_id, strategy_version_id, instrument_id, as_of, calculation_version)
);

create table accounting.nav_runs (
  nav_run_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  valuation_date date not null,
  as_of timestamptz not null,
  run_type text not null check (run_type in ('INTRADAY', 'PRELIMINARY', 'OFFICIAL')),
  status text not null check (status in ('RUNNING', 'CALCULATED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
  total_nav numeric(38, 10),
  base_currency text not null check (base_currency ~ '^[A-Z]{3}$'),
  input_hash text not null,
  calculation_version text not null,
  approval_id uuid references governance.approvals(approval_id),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  unique (fund_id, valuation_date, run_type, calculation_version, input_hash)
);

create table accounting.nav_components (
  nav_component_id uuid primary key default gen_random_uuid(),
  nav_run_id uuid not null references accounting.nav_runs(nav_run_id) on delete cascade,
  component_type text not null,
  component_ref_id uuid,
  amount numeric(38, 10) not null,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  fx_rate numeric(30, 12) not null default 1,
  base_amount numeric(38, 10) not null,
  metadata jsonb not null default '{}'::jsonb
);

create table accounting.performance_attribution (
  attribution_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  nav_run_id uuid references accounting.nav_runs(nav_run_id),
  period_start timestamptz not null,
  period_end timestamptz not null,
  dimension text not null,
  dimension_id text not null,
  pnl_contribution numeric(38, 10) not null,
  return_contribution numeric(20, 12),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  calculation_version text not null,
  created_at timestamptz not null default now(),
  check (period_end > period_start),
  unique (fund_id, period_start, period_end, dimension, dimension_id, calculation_version)
);

create table accounting.external_statements (
  statement_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  provider text not null,
  account_ref text not null,
  statement_date date not null,
  statement_type text not null,
  object_path text not null,
  content_hash text not null,
  parser_version text,
  status text not null default 'RECEIVED'
    check (status in ('RECEIVED', 'PARSED', 'QUARANTINED', 'RECONCILED')),
  created_at timestamptz not null default now(),
  unique (provider, account_ref, statement_date, statement_type, content_hash)
);

create table accounting.reconciliations (
  reconciliation_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  statement_id uuid not null references accounting.external_statements(statement_id),
  reconciliation_type text not null,
  internal_snapshot_ref jsonb not null,
  external_snapshot_ref jsonb not null,
  rule_version text not null,
  status text not null check (status in ('RUNNING', 'MATCHED', 'BREAKS_FOUND', 'FAILED', 'APPROVED')),
  summary jsonb not null,
  trace_id uuid not null,
  started_at timestamptz not null,
  completed_at timestamptz,
  check (completed_at is null or completed_at >= started_at)
);

create table accounting.reconciliation_items (
  reconciliation_item_id uuid primary key default gen_random_uuid(),
  reconciliation_id uuid not null references accounting.reconciliations(reconciliation_id) on delete cascade,
  item_type text not null,
  internal_ref text,
  external_ref text,
  match_method text,
  internal_value jsonb,
  external_value jsonb,
  difference jsonb,
  status text not null check (status in ('MATCHED', 'UNMATCHED', 'TOLERANCE', 'REVIEW'))
);

create table accounting.breaks (
  break_id uuid primary key default gen_random_uuid(),
  reconciliation_item_id uuid not null references accounting.reconciliation_items(reconciliation_item_id),
  severity text not null check (severity in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  owner text,
  due_at timestamptz,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'INVESTIGATING', 'RESOLVED', 'WAIVED', 'CLOSED')),
  resolution text,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create or replace function execution.validate_order_state_transition()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  allowed boolean := false;
begin
  if new.state = old.state then
    return new;
  end if;

  allowed := case old.state
    when 'CREATED' then new.state in ('SUBMITTED', 'CANCELLED', 'EXPIRED')
    when 'SUBMITTED' then new.state in ('ACKNOWLEDGED', 'REJECTED', 'CANCEL_PENDING', 'UNKNOWN')
    when 'ACKNOWLEDGED' then new.state in ('PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'EXPIRED', 'UNKNOWN')
    when 'PARTIALLY_FILLED' then new.state in ('FILLED', 'CANCEL_PENDING', 'UNKNOWN')
    when 'CANCEL_PENDING' then new.state in ('CANCELLED', 'PARTIALLY_FILLED', 'FILLED', 'UNKNOWN')
    when 'UNKNOWN' then new.state in ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED')
    else false
  end;

  if not allowed then
    raise exception 'invalid order state transition: % -> %', old.state, new.state;
  end if;
  return new;
end;
$$;

create trigger orders_validate_state_transition
before update of state on execution.orders
for each row execute function execution.validate_order_state_transition();

create or replace function accounting.protect_posted_journal_lines()
returns trigger
language plpgsql
set search_path = pg_catalog, accounting
as $$
declare
  target_journal_id uuid;
  target_status text;
begin
  target_journal_id := case when tg_op = 'DELETE' then old.journal_id else new.journal_id end;
  select status into target_status from accounting.journals where journal_id = target_journal_id;
  if target_status in ('POSTED', 'REVERSED') then
    raise exception 'journal % is immutable in status %', target_journal_id, target_status;
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end;
$$;

create trigger journal_lines_protect_posted
before insert or update or delete on accounting.journal_lines
for each row execute function accounting.protect_posted_journal_lines();

create or replace function accounting.protect_posted_journal()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if tg_op = 'DELETE' and old.status in ('POSTED', 'REVERSED') then
    raise exception 'journal % is immutable in status %', old.journal_id, old.status;
  end if;

  if tg_op = 'UPDATE' and old.status in ('POSTED', 'REVERSED') then
    if not (
      old.status = 'POSTED'
      and new.status = 'REVERSED'
      and (to_jsonb(new) - 'status') = (to_jsonb(old) - 'status')
    ) then
      raise exception 'journal % is immutable in status %', old.journal_id, old.status;
    end if;
  end if;

  return case when tg_op = 'DELETE' then old else new end;
end;
$$;

create trigger journals_protect_posted
before update or delete on accounting.journals
for each row execute function accounting.protect_posted_journal();

create or replace function accounting.validate_journal_posting()
returns trigger
language plpgsql
set search_path = pg_catalog, accounting
as $$
declare
  line_count integer;
  imbalance numeric(38, 10);
begin
  if old.status <> 'POSTED' and new.status = 'POSTED' then
    select count(*), coalesce(sum(base_debit - base_credit), 0)
      into line_count, imbalance
      from accounting.journal_lines
      where journal_id = new.journal_id;

    if line_count < 2 then
      raise exception 'journal % requires at least two lines', new.journal_id;
    end if;
    if imbalance <> 0 then
      raise exception 'journal % is not balanced; base imbalance=%', new.journal_id, imbalance;
    end if;
    new.posted_at := coalesce(new.posted_at, now());
  elsif old.status in ('POSTED', 'REVERSED') and new.status <> old.status then
    if not (old.status = 'POSTED' and new.status = 'REVERSED') then
      raise exception 'posted journal status transition is not allowed: % -> %', old.status, new.status;
    end if;
  end if;
  return new;
end;
$$;

create trigger journals_validate_posting
before update of status on accounting.journals
for each row execute function accounting.validate_journal_posting();

create trigger order_events_append_only
before update or delete on execution.order_events
for each row execute function governance.reject_append_only_change();

create trigger fills_append_only
before update or delete on execution.fills
for each row execute function governance.reject_append_only_change();

create trigger limit_changes_append_only
before update or delete on risk.limit_changes
for each row execute function governance.reject_append_only_change();

create trigger kill_switch_events_append_only
before update or delete on risk.kill_switch_events
for each row execute function governance.reject_append_only_change();

create trigger intent_groups_touch_updated_at
before update on execution.intent_groups
for each row execute function governance.touch_updated_at();

create trigger order_intents_touch_updated_at
before update on execution.order_intents
for each row execute function governance.touch_updated_at();

create trigger orders_touch_updated_at
before update on execution.orders
for each row execute function governance.touch_updated_at();

create trigger positions_touch_updated_at
before update on accounting.positions
for each row execute function governance.touch_updated_at();

create trigger cash_balances_touch_updated_at
before update on accounting.cash_balances
for each row execute function governance.touch_updated_at();

do $$
declare
  schema_name text;
  table_name text;
  tables text[];
begin
  foreach schema_name in array array['execution', 'risk', 'accounting'] loop
    if schema_name = 'execution' then
      tables := array[
        'market_snapshots', 'trade_cases', 'trade_case_instruments', 'intent_groups',
        'order_intents', 'execution_plans', 'orders', 'order_events', 'fills',
        'broker_sessions', 'tca_results', 'execution_exceptions'
      ];
    elsif schema_name = 'risk' then
      tables := array[
        'policies', 'limits', 'limit_changes', 'restricted_items', 'counterparties',
        'margin_rules', 'risk_requests', 'risk_request_items', 'risk_decisions',
        'snapshots', 'exposure_components', 'stress_scenarios', 'stress_results',
        'breaches', 'trading_states', 'kill_switch_events'
      ];
    else
      tables := array[
        'strategy_allocations', 'portfolio_snapshots', 'ledger_accounts', 'journals',
        'journal_lines', 'positions', 'cash_balances', 'valuations', 'pnl_snapshots',
        'nav_runs', 'nav_components', 'performance_attribution', 'external_statements',
        'reconciliations', 'reconciliation_items', 'breaks'
      ];
    end if;

    foreach table_name in array tables loop
      execute format('alter table %I.%I enable row level security', schema_name, table_name);
    end loop;
  end loop;
end;
$$;

create policy intent_groups_fund_member_select
on execution.intent_groups for select
using (governance.can_access_fund(fund_id));

create policy order_intents_fund_member_select
on execution.order_intents for select
using (governance.can_access_fund(fund_id));

create policy risk_policies_fund_member_select
on risk.policies for select
using (governance.can_access_fund(fund_id));

create policy risk_requests_fund_member_select
on risk.risk_requests for select
using (governance.can_access_fund(fund_id));

create policy risk_snapshots_fund_member_select
on risk.snapshots for select
using (governance.can_access_fund(fund_id));

create policy risk_breaches_fund_member_select
on risk.breaches for select
using (governance.can_access_fund(fund_id));

create policy portfolio_snapshots_fund_member_select
on accounting.portfolio_snapshots for select
using (governance.can_access_fund(fund_id));

create policy positions_fund_member_select
on accounting.positions for select
using (governance.can_access_fund(fund_id));

create policy cash_balances_fund_member_select
on accounting.cash_balances for select
using (governance.can_access_fund(fund_id));

create policy pnl_snapshots_fund_member_select
on accounting.pnl_snapshots for select
using (governance.can_access_fund(fund_id));

create policy nav_runs_fund_member_select
on accounting.nav_runs for select
using (governance.can_access_fund(fund_id));

commit;
