begin;

create table research.collection_runs (
  collection_run_id uuid primary key default gen_random_uuid(),
  source_id uuid not null references reference.data_sources(source_id),
  collector_version text not null,
  cursor_before jsonb,
  cursor_after jsonb,
  started_at timestamptz not null,
  ended_at timestamptz,
  records_seen bigint not null default 0,
  records_inserted bigint not null default 0,
  records_deduplicated bigint not null default 0,
  records_quarantined bigint not null default 0,
  status text not null check (status in ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')),
  error_summary jsonb not null default '{}'::jsonb,
  trace_id uuid not null,
  check (ended_at is null or ended_at >= started_at)
);

create table research.documents (
  document_id uuid primary key default gen_random_uuid(),
  source_id uuid not null references reference.data_sources(source_id),
  external_id text,
  document_type text not null,
  canonical_url text,
  title text,
  language text not null default 'ko',
  issuer_id uuid references reference.issuers(issuer_id),
  published_at timestamptz,
  observed_at timestamptz not null,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'CORRECTED', 'RETRACTED', 'DELETED', 'QUARANTINED')),
  current_version integer not null default 1 check (current_version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique nulls not distinct (source_id, external_id)
);

create index documents_pit_idx
  on research.documents (published_at desc, observed_at desc);
create index documents_issuer_pit_idx
  on research.documents (issuer_id, published_at desc, observed_at desc);

create table research.document_versions (
  document_version_id uuid primary key default gen_random_uuid(),
  document_id uuid not null references research.documents(document_id),
  version integer not null check (version > 0),
  content_hash text not null,
  object_path text not null,
  media_type text,
  byte_size bigint check (byte_size is null or byte_size >= 0),
  parser_name text,
  parser_version text,
  parsed_object_path text,
  extraction_quality jsonb not null default '{}'::jsonb,
  license_scope text not null,
  published_at timestamptz,
  observed_at timestamptz not null,
  supersedes_version_id uuid references research.document_versions(document_version_id),
  collection_run_id uuid references research.collection_runs(collection_run_id),
  created_at timestamptz not null default now(),
  unique (document_id, version),
  unique (document_id, content_hash)
);

create table research.document_instruments (
  document_id uuid not null references research.documents(document_id) on delete cascade,
  instrument_id uuid not null references reference.instruments(instrument_id),
  relation_type text not null default 'MENTIONS',
  confidence numeric(8, 7) check (confidence is null or confidence between 0 and 1),
  extraction_version text,
  primary key (document_id, instrument_id, relation_type)
);

create table research.document_relations (
  from_document_id uuid not null references research.documents(document_id),
  to_document_id uuid not null references research.documents(document_id),
  relation_type text not null,
  confidence numeric(8, 7) check (confidence is null or confidence between 0 and 1),
  detected_at timestamptz not null default now(),
  primary key (from_document_id, to_document_id, relation_type),
  check (from_document_id <> to_document_id)
);

create table research.story_clusters (
  cluster_id uuid primary key default gen_random_uuid(),
  canonical_document_id uuid references research.documents(document_id),
  event_type text,
  cluster_key text not null unique,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  clustering_version text not null,
  metadata jsonb not null default '{}'::jsonb,
  check (last_seen_at >= first_seen_at)
);

create table research.story_cluster_members (
  cluster_id uuid not null references research.story_clusters(cluster_id) on delete cascade,
  document_id uuid not null references research.documents(document_id) on delete cascade,
  similarity numeric(8, 7) check (similarity is null or similarity between 0 and 1),
  is_canonical boolean not null default false,
  added_at timestamptz not null default now(),
  primary key (cluster_id, document_id)
);

create table research.evidence_chunks (
  chunk_id uuid primary key default gen_random_uuid(),
  document_version_id uuid not null references research.document_versions(document_version_id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  content text not null,
  content_hash text not null,
  token_count integer check (token_count is null or token_count >= 0),
  char_start integer check (char_start is null or char_start >= 0),
  char_end integer check (char_end is null or char_end >= char_start),
  embedding extensions.vector(1536),
  embedding_model text,
  embedding_version text,
  license_scope text not null,
  published_at timestamptz,
  observed_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (document_version_id, chunk_index),
  unique (document_version_id, content_hash)
);

create index evidence_chunks_pit_idx
  on research.evidence_chunks (observed_at desc, published_at desc);
create index evidence_chunks_embedding_hnsw_idx
  on research.evidence_chunks using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

create table research.financial_facts (
  financial_fact_id uuid primary key default gen_random_uuid(),
  issuer_id uuid not null references reference.issuers(issuer_id),
  source_id uuid not null references reference.data_sources(source_id),
  account_code text not null,
  account_name text,
  period_start date,
  period_end date not null,
  period_type text not null,
  consolidation_scope text not null,
  value numeric(38, 10),
  unit text not null,
  currency text,
  published_at timestamptz not null,
  observed_at timestamptz not null,
  revision integer not null default 1 check (revision > 0),
  source_document_id uuid references research.documents(document_id),
  metadata jsonb not null default '{}'::jsonb,
  unique (issuer_id, source_id, account_code, period_end, period_type, consolidation_scope, revision)
);

create index financial_facts_pit_idx
  on research.financial_facts (issuer_id, account_code, period_end desc, observed_at desc);

create table research.macro_series (
  series_id uuid primary key default gen_random_uuid(),
  source_id uuid not null references reference.data_sources(source_id),
  external_series_code text not null,
  name text not null,
  unit text,
  frequency text not null,
  seasonal_adjustment text,
  country_code text,
  definition jsonb not null default '{}'::jsonb,
  status text not null default 'ACTIVE' check (status in ('ACTIVE', 'INACTIVE')),
  unique (source_id, external_series_code)
);

create table research.macro_observations (
  macro_observation_id uuid primary key default gen_random_uuid(),
  series_id uuid not null references research.macro_series(series_id),
  period date not null,
  value numeric(38, 12),
  published_at timestamptz not null,
  observed_at timestamptz not null,
  vintage_date date not null,
  revision integer not null default 1 check (revision > 0),
  metadata jsonb not null default '{}'::jsonb,
  unique (series_id, period, vintage_date, revision)
);

create index macro_observations_pit_idx
  on research.macro_observations (series_id, period desc, observed_at desc);

create table research.research_packets (
  research_packet_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id),
  fund_id uuid not null references accounting.funds(fund_id),
  primary_instrument_id uuid references reference.instruments(instrument_id),
  as_of timestamptz not null,
  claims jsonb not null,
  invalidation jsonb not null,
  bull_case jsonb not null default '{}'::jsonb,
  bear_case jsonb not null default '{}'::jsonb,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL')),
  schema_version integer not null check (schema_version > 0),
  producer_profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),
  trace_id uuid not null,
  created_at timestamptz not null default now()
);

create table research.research_packet_evidence (
  research_packet_id uuid not null references research.research_packets(research_packet_id) on delete cascade,
  chunk_id uuid not null references research.evidence_chunks(chunk_id),
  evidence_role text not null,
  rank integer check (rank is null or rank > 0),
  relevance numeric(8, 7) check (relevance is null or relevance between 0 and 1),
  primary key (research_packet_id, chunk_id, evidence_role)
);

create table research.agent_decisions (
  decision_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id),
  fund_id uuid not null references accounting.funds(fund_id),
  research_packet_id uuid references research.research_packets(research_packet_id),
  decision_type text not null,
  strategy_family text,
  directionality text,
  thesis text not null,
  confidence numeric(8, 7) not null check (confidence between 0 and 1),
  invalidation jsonb not null,
  scope_instrument_ids uuid[] not null,
  target_portfolio jsonb not null default '[]'::jsonb,
  expires_at timestamptz,
  model_id uuid references workforce.models(model_id),
  profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),
  schema_version integer not null check (schema_version > 0),
  input_hash text not null,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  unique (case_id, decision_type, input_hash)
);

create table quant.universe_versions (
  universe_version_id uuid primary key default gen_random_uuid(),
  name text not null,
  as_of timestamptz not null,
  rules jsonb not null,
  member_manifest_path text,
  member_count integer not null check (member_count >= 0),
  content_hash text not null,
  source_versions jsonb not null,
  created_at timestamptz not null default now(),
  unique (name, as_of, content_hash)
);

create table quant.universe_members (
  universe_version_id uuid not null references quant.universe_versions(universe_version_id) on delete cascade,
  instrument_id uuid not null references reference.instruments(instrument_id),
  member_role text not null default 'TRADABLE',
  weight numeric(20, 12),
  attributes jsonb not null default '{}'::jsonb,
  primary key (universe_version_id, instrument_id, member_role)
);

create table quant.feature_specs (
  feature_spec_id uuid primary key default gen_random_uuid(),
  feature_code text not null,
  version integer not null check (version > 0),
  definition jsonb not null,
  input_contract jsonb not null,
  lookback jsonb not null,
  availability_lag interval not null default interval '0 seconds',
  code_version text not null,
  owner text not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'ACTIVE', 'DEPRECATED', 'RETIRED')),
  created_at timestamptz not null default now(),
  unique (feature_code, version)
);

create table quant.feature_snapshots (
  feature_snapshot_id uuid primary key default gen_random_uuid(),
  instrument_id uuid references reference.instruments(instrument_id),
  universe_version_id uuid references quant.universe_versions(universe_version_id),
  as_of timestamptz not null,
  observed_at timestamptz not null,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  feature_values jsonb not null,
  source_ref text not null,
  content_hash text not null,
  schema_version integer not null check (schema_version > 0),
  created_at timestamptz not null default now(),
  unique nulls not distinct (instrument_id, universe_version_id, as_of, content_hash)
);

create table quant.dataset_manifests (
  dataset_id uuid primary key default gen_random_uuid(),
  name text not null,
  version text not null,
  as_of timestamptz not null,
  universe_version_id uuid references quant.universe_versions(universe_version_id),
  source_versions jsonb not null,
  feature_spec_versions jsonb not null,
  partitions jsonb not null,
  point_in_time_policy jsonb not null,
  quality_summary jsonb not null,
  object_path text not null,
  content_hash text not null,
  row_count bigint check (row_count is null or row_count >= 0),
  schema_definition jsonb not null,
  created_at timestamptz not null default now(),
  unique (name, version),
  unique (content_hash)
);

create table quant.dataset_partitions (
  dataset_partition_id uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references quant.dataset_manifests(dataset_id) on delete cascade,
  partition_key text not null,
  object_path text not null,
  row_count bigint not null check (row_count >= 0),
  min_event_time timestamptz,
  max_event_time timestamptz,
  content_hash text not null,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL')),
  unique (dataset_id, partition_key),
  unique (dataset_id, content_hash),
  check (max_event_time is null or min_event_time is null or max_event_time >= min_event_time)
);

create table quant.hypotheses (
  hypothesis_id uuid primary key default gen_random_uuid(),
  case_id uuid references governance.cases(case_id),
  title text not null,
  rationale text not null,
  expected_edge jsonb not null,
  falsification_criteria jsonb not null,
  required_data_products jsonb not null,
  status text not null default 'PROPOSED'
    check (status in ('PROPOSED', 'APPROVED', 'TESTING', 'SUPPORTED', 'REJECTED', 'ARCHIVED')),
  created_by text not null,
  trace_id uuid not null,
  created_at timestamptz not null default now()
);

create table quant.experiments (
  experiment_id uuid primary key default gen_random_uuid(),
  hypothesis_id uuid not null references quant.hypotheses(hypothesis_id),
  dataset_id uuid not null references quant.dataset_manifests(dataset_id),
  code_version text not null,
  config jsonb not null,
  seed bigint not null,
  split_policy jsonb not null,
  cost_model_version text not null,
  status text not null default 'QUEUED'
    check (status in ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
  input_hash text not null,
  trace_id uuid not null,
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz not null default now(),
  unique (input_hash),
  check (ended_at is null or started_at is null or ended_at >= started_at)
);

create table quant.experiment_metrics (
  experiment_metric_id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references quant.experiments(experiment_id) on delete cascade,
  split text not null check (split in ('TRAIN', 'VALIDATION', 'TEST', 'WALK_FORWARD', 'STRESS')),
  metric text not null,
  value numeric(38, 12),
  unit text,
  dimensions jsonb not null default '{}'::jsonb,
  cost_model_version text not null,
  unique (experiment_id, split, metric, dimensions)
);

create table quant.model_artifacts (
  model_artifact_id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references quant.experiments(experiment_id),
  model_type text not null,
  artifact_path text not null,
  signature jsonb not null,
  limitations jsonb not null,
  content_hash text not null,
  training_data_hash text not null,
  status text not null default 'CANDIDATE'
    check (status in ('CANDIDATE', 'APPROVED', 'REJECTED', 'RETIRED')),
  created_at timestamptz not null default now(),
  unique (content_hash)
);

create table quant.backtest_runs (
  backtest_run_id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references quant.experiments(experiment_id),
  dataset_id uuid not null references quant.dataset_manifests(dataset_id),
  universe_version_id uuid not null references quant.universe_versions(universe_version_id),
  start_date date not null,
  end_date date not null,
  initial_capital numeric(30, 10) not null check (initial_capital > 0),
  cost_model jsonb not null,
  risk_model jsonb not null,
  result_summary jsonb not null default '{}'::jsonb,
  result_object_path text,
  code_version text not null,
  input_hash text not null unique,
  status text not null check (status in ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
  created_at timestamptz not null default now(),
  check (end_date >= start_date)
);

create table quant.backtest_trades (
  backtest_trade_id uuid primary key default gen_random_uuid(),
  backtest_run_id uuid not null references quant.backtest_runs(backtest_run_id) on delete cascade,
  instrument_id uuid not null references reference.instruments(instrument_id),
  opened_at timestamptz not null,
  closed_at timestamptz,
  side text not null check (side in ('BUY', 'SELL', 'SHORT', 'COVER')),
  quantity numeric(30, 10) not null check (quantity > 0),
  open_price numeric(30, 10) not null,
  close_price numeric(30, 10),
  fees numeric(30, 10) not null default 0,
  realized_pnl numeric(30, 10),
  signal_ref jsonb not null,
  check (closed_at is null or closed_at >= opened_at)
);

create table strategy.strategies (
  strategy_id uuid primary key default gen_random_uuid(),
  strategy_code text not null unique,
  name text not null,
  family text not null,
  directionality text not null,
  owner_department text not null,
  status text not null default 'RESEARCH'
    check (status in ('RESEARCH', 'SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE', 'SUSPENDED', 'RETIRED')),
  current_version integer not null default 0 check (current_version >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table strategy.capability_profiles (
  capability_profile_id uuid primary key default gen_random_uuid(),
  profile_code text not null,
  version integer not null check (version > 0),
  required_data_products jsonb not null,
  required_instruments jsonb not null,
  execution_capabilities jsonb not null,
  risk_capabilities jsonb not null,
  accounting_capabilities jsonb not null,
  environment_status jsonb not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
  content_hash text not null,
  created_at timestamptz not null default now(),
  unique (profile_code, version),
  unique (profile_code, content_hash)
);

create table strategy.candidates (
  candidate_id uuid primary key default gen_random_uuid(),
  strategy_id uuid references strategy.strategies(strategy_id),
  experiment_id uuid not null references quant.experiments(experiment_id),
  backtest_run_id uuid references quant.backtest_runs(backtest_run_id),
  strategy_family text not null,
  directionality text not null,
  mandate_assumptions jsonb not null,
  risk_assumptions jsonb not null,
  capability_profile_id uuid not null references strategy.capability_profiles(capability_profile_id),
  status text not null default 'PROPOSED'
    check (status in ('PROPOSED', 'VALIDATING', 'APPROVED', 'REJECTED', 'PROMOTED')),
  trace_id uuid not null,
  created_at timestamptz not null default now()
);

create table strategy.versions (
  strategy_version_id uuid primary key default gen_random_uuid(),
  strategy_id uuid not null references strategy.strategies(strategy_id),
  version integer not null check (version > 0),
  candidate_id uuid references strategy.candidates(candidate_id),
  capability_profile_id uuid not null references strategy.capability_profiles(capability_profile_id),
  signal_schema jsonb not null,
  target_portfolio_schema jsonb not null,
  config jsonb not null,
  artifact_path text not null,
  artifact_hash text not null,
  code_version text not null,
  dataset_id uuid references quant.dataset_manifests(dataset_id),
  model_artifact_id uuid references quant.model_artifacts(model_artifact_id),
  effective_from timestamptz,
  effective_to timestamptz,
  deployment_state text not null default 'DRAFT'
    check (deployment_state in ('DRAFT', 'SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE', 'SUSPENDED', 'RETIRED')),
  approved_by_decision_id uuid references governance.committee_decisions(committee_decision_id),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_from is null or effective_to > effective_from),
  unique (strategy_id, version),
  unique (strategy_id, artifact_hash)
);

create table strategy.deployments (
  deployment_id uuid primary key default gen_random_uuid(),
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  environment text not null check (environment in ('RESEARCH', 'SHADOW', 'PAPER', 'LIVE')),
  status text not null check (status in ('PENDING', 'ACTIVE', 'PAUSED', 'ROLLED_BACK', 'FAILED', 'RETIRED')),
  runtime_config jsonb not null,
  deployed_artifact_hash text not null,
  approval_id uuid references governance.approvals(approval_id),
  rollback_of_deployment_id uuid references strategy.deployments(deployment_id),
  deployed_at timestamptz,
  stopped_at timestamptz,
  trace_id uuid not null,
  created_at timestamptz not null default now()
);

create table strategy.signals (
  signal_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id),
  fund_id uuid not null references accounting.funds(fund_id),
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  decision_id uuid references research.agent_decisions(decision_id),
  signal_type text not null,
  directionality text not null,
  strength numeric(12, 8),
  confidence numeric(8, 7) check (confidence is null or confidence between 0 and 1),
  as_of timestamptz not null,
  valid_until timestamptz not null,
  payload jsonb not null,
  input_hash text not null,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  check (valid_until > as_of),
  unique (strategy_version_id, input_hash)
);

create table strategy.signal_targets (
  signal_id uuid not null references strategy.signals(signal_id) on delete cascade,
  instrument_id uuid not null references reference.instruments(instrument_id),
  leg_index integer not null check (leg_index >= 0),
  role text not null,
  target_weight numeric(18, 12),
  target_quantity numeric(30, 10),
  target_notional numeric(30, 10),
  currency text check (currency is null or currency ~ '^[A-Z]{3}$'),
  metadata jsonb not null default '{}'::jsonb,
  primary key (signal_id, leg_index),
  unique (signal_id, instrument_id, role)
);

create table strategy.evaluations (
  evaluation_id uuid primary key default gen_random_uuid(),
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  environment text not null,
  window_start timestamptz not null,
  window_end timestamptz not null,
  metrics jsonb not null,
  benchmark jsonb not null default '{}'::jsonb,
  limit_breaches jsonb not null default '[]'::jsonb,
  data_quality jsonb not null,
  decision text not null check (decision in ('CONTINUE', 'PROMOTE', 'PAUSE', 'REJECT', 'RETIRE')),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  check (window_end > window_start)
);

create table strategy.promotion_decisions (
  promotion_decision_id uuid primary key default gen_random_uuid(),
  strategy_version_id uuid not null references strategy.versions(strategy_version_id),
  from_stage text not null,
  to_stage text not null,
  evaluation_id uuid not null references strategy.evaluations(evaluation_id),
  committee_decision_id uuid not null references governance.committee_decisions(committee_decision_id),
  decision text not null check (decision in ('APPROVE', 'REJECT', 'DEFER')),
  conditions jsonb not null default '{}'::jsonb,
  decided_at timestamptz not null default now(),
  unique (strategy_version_id, from_stage, to_stage, evaluation_id)
);

alter table governance.investment_cases
  add constraint investment_cases_universe_version_fk
  foreign key (universe_version_id) references quant.universe_versions(universe_version_id),
  add constraint investment_cases_feature_snapshot_fk
  foreign key (feature_snapshot_id) references quant.feature_snapshots(feature_snapshot_id),
  add constraint investment_cases_decision_fk
  foreign key (decision_id) references research.agent_decisions(decision_id),
  add constraint investment_cases_strategy_version_fk
  foreign key (strategy_version_id) references strategy.versions(strategy_version_id),
  add constraint investment_cases_evaluation_fk
  foreign key (evaluation_id) references strategy.evaluations(evaluation_id);

alter table governance.capital_allocations
  add constraint capital_allocations_strategy_fk
  foreign key (strategy_id) references strategy.strategies(strategy_id);

create trigger documents_touch_updated_at
before update on research.documents
for each row execute function governance.touch_updated_at();

create trigger strategies_touch_updated_at
before update on strategy.strategies
for each row execute function governance.touch_updated_at();

do $$
declare
  schema_name text;
  table_name text;
  tables text[];
begin
  foreach schema_name in array array['research', 'quant', 'strategy'] loop
    if schema_name = 'research' then
      tables := array[
        'collection_runs', 'documents', 'document_versions', 'document_instruments',
        'document_relations', 'story_clusters', 'story_cluster_members', 'evidence_chunks',
        'financial_facts', 'macro_series', 'macro_observations', 'research_packets',
        'research_packet_evidence', 'agent_decisions'
      ];
    elsif schema_name = 'quant' then
      tables := array[
        'universe_versions', 'universe_members', 'feature_specs', 'feature_snapshots',
        'dataset_manifests', 'dataset_partitions', 'hypotheses', 'experiments',
        'experiment_metrics', 'model_artifacts', 'backtest_runs', 'backtest_trades'
      ];
    else
      tables := array[
        'strategies', 'capability_profiles', 'candidates', 'versions', 'deployments',
        'signals', 'signal_targets', 'evaluations', 'promotion_decisions'
      ];
    end if;

    foreach table_name in array tables loop
      execute format('alter table %I.%I enable row level security', schema_name, table_name);
    end loop;
  end loop;
end;
$$;

create policy research_packets_fund_member_select
on research.research_packets for select
using (governance.can_access_fund(fund_id));

create policy agent_decisions_fund_member_select
on research.agent_decisions for select
using (governance.can_access_fund(fund_id));

create policy signals_fund_member_select
on strategy.signals for select
using (governance.can_access_fund(fund_id));

commit;
