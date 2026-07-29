begin;

create extension if not exists timescaledb;
create extension if not exists pgcrypto;

create schema if not exists market;
revoke all on schema market from public;

create table market.market_ticks (
  event_time timestamptz not null,
  received_at timestamptz not null,
  observed_at timestamptz not null,
  instrument_id uuid not null,
  provider text not null,
  tr_code text,
  market text not null,
  session_type text,
  price numeric(30, 10) not null check (price >= 0),
  quantity numeric(30, 10) not null check (quantity >= 0),
  side smallint not null default 0 check (side in (-1, 0, 1)),
  cumulative_volume numeric(38, 10),
  cumulative_value numeric(38, 10),
  sequence_no text,
  source_event_id text not null,
  correction_code text,
  trading_status text,
  raw_flags jsonb not null default '{}'::jsonb,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid,
  ingested_at timestamptz not null default now(),
  primary key (event_time, source_event_id),
  check (received_at >= event_time - interval '1 day'),
  check (observed_at >= received_at)
);

select create_hypertable(
  'market.market_ticks',
  'event_time',
  if_not_exists => true,
  chunk_time_interval => interval '1 day'
);

create index market_ticks_instrument_time_idx
  on market.market_ticks (instrument_id, event_time desc);
create index market_ticks_provider_sequence_idx
  on market.market_ticks (provider, sequence_no, event_time desc)
  where sequence_no is not null;

create table market.market_quotes (
  event_time timestamptz not null,
  received_at timestamptz not null,
  observed_at timestamptz not null,
  instrument_id uuid not null,
  provider text not null,
  tr_code text,
  market text not null,
  session_type text,
  bid_prices numeric(30, 10)[] not null,
  bid_sizes numeric(30, 10)[] not null,
  ask_prices numeric(30, 10)[] not null,
  ask_sizes numeric(30, 10)[] not null,
  total_bid_size numeric(38, 10),
  total_ask_size numeric(38, 10),
  best_bid numeric(30, 10),
  best_ask numeric(30, 10),
  mid_price numeric(30, 10),
  spread numeric(30, 10),
  depth_imbalance double precision,
  sequence_no text,
  source_event_id text not null,
  trading_status text,
  raw_flags jsonb not null default '{}'::jsonb,
  schema_version integer not null check (schema_version > 0),
  trace_id uuid,
  ingested_at timestamptz not null default now(),
  primary key (event_time, source_event_id),
  check (cardinality(bid_prices) = cardinality(bid_sizes)),
  check (cardinality(ask_prices) = cardinality(ask_sizes)),
  check (cardinality(bid_prices) between 1 and 10),
  check (cardinality(ask_prices) between 1 and 10),
  check (best_ask is null or best_bid is null or best_ask >= best_bid),
  check (depth_imbalance is null or depth_imbalance between -1 and 1),
  check (observed_at >= received_at)
);

select create_hypertable(
  'market.market_quotes',
  'event_time',
  if_not_exists => true,
  chunk_time_interval => interval '1 day'
);

create index market_quotes_instrument_time_idx
  on market.market_quotes (instrument_id, event_time desc);
create index market_quotes_provider_sequence_idx
  on market.market_quotes (provider, sequence_no, event_time desc)
  where sequence_no is not null;

create table market.market_bars (
  bucket_time timestamptz not null,
  observed_at timestamptz not null,
  instrument_id uuid not null,
  market text not null,
  interval_code text not null check (interval_code in ('1S', '1M', '5M', '15M', '1H', '1D')),
  open numeric(30, 10) not null,
  high numeric(30, 10) not null,
  low numeric(30, 10) not null,
  close numeric(30, 10) not null,
  volume numeric(38, 10) not null default 0,
  trade_count bigint not null default 0,
  notional numeric(38, 10),
  vwap numeric(30, 10),
  is_final boolean not null default false,
  source text not null,
  source_version text not null,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'PARTIAL')),
  schema_version integer not null check (schema_version > 0),
  created_at timestamptz not null default now(),
  primary key (bucket_time, instrument_id, market, interval_code, source),
  check (high >= greatest(open, low, close)),
  check (low <= least(open, high, close)),
  check (observed_at >= bucket_time)
);

select create_hypertable(
  'market.market_bars',
  'bucket_time',
  if_not_exists => true,
  chunk_time_interval => interval '7 days'
);

create index market_bars_lookup_idx
  on market.market_bars (instrument_id, interval_code, bucket_time desc);

create table market.microstructure_features (
  event_time timestamptz not null,
  observed_at timestamptz not null,
  instrument_id uuid not null,
  market text not null,
  feature_set_version text not null,
  return_1s double precision,
  return_1m double precision,
  realized_volatility double precision,
  spread_bps double precision,
  depth_imbalance double precision,
  order_flow_imbalance double precision,
  trade_intensity double precision,
  volume_zscore double precision,
  relative_strength double precision,
  values jsonb not null default '{}'::jsonb,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  input_watermark timestamptz not null,
  input_hash text not null,
  created_at timestamptz not null default now(),
  primary key (event_time, instrument_id, market, feature_set_version),
  check (observed_at >= event_time),
  check (input_watermark <= observed_at)
);

select create_hypertable(
  'market.microstructure_features',
  'event_time',
  if_not_exists => true,
  chunk_time_interval => interval '7 days'
);

create index microstructure_features_lookup_idx
  on market.microstructure_features (instrument_id, feature_set_version, event_time desc);

create table market.market_breadth (
  event_time timestamptz not null,
  observed_at timestamptz not null,
  market text not null,
  universe_version_id uuid,
  advancers integer,
  decliners integer,
  unchanged integer,
  new_highs integer,
  new_lows integer,
  up_volume numeric(38, 10),
  down_volume numeric(38, 10),
  total_value numeric(38, 10),
  values jsonb not null default '{}'::jsonb,
  source text not null,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'PARTIAL')),
  created_at timestamptz not null default now(),
  primary key (event_time, market, source)
);

select create_hypertable(
  'market.market_breadth',
  'event_time',
  if_not_exists => true,
  chunk_time_interval => interval '7 days'
);

create table market.derivative_snapshots (
  event_time timestamptz not null,
  received_at timestamptz not null,
  observed_at timestamptz not null,
  instrument_id uuid not null,
  underlying_instrument_id uuid,
  provider text not null,
  market text not null,
  expiry_date date not null,
  strike_price numeric(30, 10),
  option_type text check (option_type in ('CALL', 'PUT')),
  last_price numeric(30, 10),
  bid numeric(30, 10),
  ask numeric(30, 10),
  open_interest numeric(38, 10),
  volume numeric(38, 10),
  implied_volatility double precision,
  delta double precision,
  gamma double precision,
  theta double precision,
  vega double precision,
  rho double precision,
  theoretical_price numeric(30, 10),
  underlying_price numeric(30, 10),
  days_to_expiry numeric(16, 8),
  source_event_id text not null,
  calculation_version text,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL', 'STALE')),
  raw_flags jsonb not null default '{}'::jsonb,
  schema_version integer not null check (schema_version > 0),
  created_at timestamptz not null default now(),
  primary key (event_time, source_event_id),
  check (ask is null or bid is null or ask >= bid),
  check (implied_volatility is null or implied_volatility >= 0),
  check (observed_at >= received_at)
);

select create_hypertable(
  'market.derivative_snapshots',
  'event_time',
  if_not_exists => true,
  chunk_time_interval => interval '1 day'
);

create index derivative_snapshots_contract_time_idx
  on market.derivative_snapshots (instrument_id, event_time desc);
create index derivative_snapshots_chain_idx
  on market.derivative_snapshots (underlying_instrument_id, expiry_date, strike_price, option_type, event_time desc);

create table market.data_quality_windows (
  window_start timestamptz not null,
  window_end timestamptz not null,
  provider text not null,
  stream_type text not null,
  instrument_id uuid,
  expected_count bigint,
  observed_count bigint,
  duplicate_count bigint not null default 0,
  out_of_order_count bigint not null default 0,
  gap_count bigint not null default 0,
  max_latency_ms integer,
  p95_latency_ms integer,
  freshness_ms integer,
  quality_status text not null check (quality_status in ('PASS', 'WARN', 'FAIL')),
  rule_version text not null,
  metrics jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (window_start, provider, stream_type, instrument_id),
  check (window_end > window_start)
);

select create_hypertable(
  'market.data_quality_windows',
  'window_start',
  if_not_exists => true,
  chunk_time_interval => interval '30 days'
);

create table market.feed_gaps (
  gap_id uuid primary key default gen_random_uuid(),
  provider text not null,
  stream_type text not null,
  instrument_id uuid,
  detected_at timestamptz not null,
  gap_start timestamptz not null,
  gap_end timestamptz,
  sequence_before text,
  sequence_after text,
  severity text not null check (severity in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  status text not null default 'OPEN'
    check (status in ('OPEN', 'BACKFILLING', 'RECOVERED', 'ACCEPTED', 'FAILED')),
  backfill_source text,
  recovered_at timestamptz,
  evidence jsonb not null default '{}'::jsonb,
  trace_id uuid,
  check (gap_end is null or gap_end >= gap_start)
);

create index feed_gaps_status_idx on market.feed_gaps (status, severity, detected_at desc);

create table market.ingestion_watermarks (
  provider text not null,
  stream_type text not null,
  market text not null,
  last_event_time timestamptz,
  last_received_at timestamptz,
  last_sequence_no text,
  last_source_event_id text,
  collector_instance text,
  updated_at timestamptz not null default now(),
  primary key (provider, stream_type, market)
);

create table market.archive_exports (
  archive_export_id uuid primary key default gen_random_uuid(),
  source_table text not null,
  partition_start timestamptz not null,
  partition_end timestamptz not null,
  object_path text not null,
  row_count bigint not null check (row_count >= 0),
  min_event_time timestamptz,
  max_event_time timestamptz,
  content_hash text not null,
  schema_version integer not null check (schema_version > 0),
  exported boolean not null default false,
  verified boolean not null default false,
  manifest_signed boolean not null default false,
  exported_at timestamptz,
  verified_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (source_table, partition_start, partition_end),
  unique (object_path, content_hash),
  check (partition_end > partition_start),
  check (max_event_time is null or min_event_time is null or max_event_time >= min_event_time)
);

create table market.retention_registry (
  source_table text primary key,
  hot_retention interval not null,
  archive_required boolean not null default true,
  deletion_enabled boolean not null default false,
  approved_by text,
  approved_at timestamptz,
  policy_version text not null,
  updated_at timestamptz not null default now()
);

insert into market.retention_registry (
  source_table, hot_retention, archive_required, deletion_enabled, policy_version
) values
  ('market.market_ticks', interval '90 days', true, false, 'initial-v1'),
  ('market.market_quotes', interval '90 days', true, false, 'initial-v1'),
  ('market.derivative_snapshots', interval '90 days', true, false, 'initial-v1'),
  ('market.microstructure_features', interval '365 days', true, false, 'initial-v1'),
  ('market.market_bars', interval '1095 days', true, false, 'initial-v1'),
  ('market.data_quality_windows', interval '365 days', true, false, 'initial-v1')
on conflict (source_table) do nothing;

alter table market.market_ticks set (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'instrument_id,market',
  timescaledb.compress_orderby = 'event_time DESC,source_event_id'
);
alter table market.market_quotes set (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'instrument_id,market',
  timescaledb.compress_orderby = 'event_time DESC,source_event_id'
);
alter table market.market_bars set (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'instrument_id,market,interval_code,source',
  timescaledb.compress_orderby = 'bucket_time DESC'
);
alter table market.microstructure_features set (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'instrument_id,market,feature_set_version',
  timescaledb.compress_orderby = 'event_time DESC'
);
alter table market.derivative_snapshots set (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'instrument_id,market',
  timescaledb.compress_orderby = 'event_time DESC,source_event_id'
);

select add_compression_policy('market.market_ticks', interval '7 days', if_not_exists => true);
select add_compression_policy('market.market_quotes', interval '7 days', if_not_exists => true);
select add_compression_policy('market.market_bars', interval '30 days', if_not_exists => true);
select add_compression_policy('market.microstructure_features', interval '30 days', if_not_exists => true);
select add_compression_policy('market.derivative_snapshots', interval '7 days', if_not_exists => true);

create materialized view market.bars_1m
with (timescaledb.continuous) as
select
  time_bucket(interval '1 minute', event_time) as bucket_time,
  instrument_id,
  market,
  first(price, event_time) as open,
  max(price) as high,
  min(price) as low,
  last(price, event_time) as close,
  sum(quantity) as volume,
  count(*) as trade_count,
  sum(price * quantity) as notional,
  case when sum(quantity) = 0 then null else sum(price * quantity) / sum(quantity) end as vwap,
  max(observed_at) as observed_at
from market.market_ticks
group by time_bucket(interval '1 minute', event_time), instrument_id, market
with no data;

select add_continuous_aggregate_policy(
  'market.bars_1m',
  start_offset => interval '2 days',
  end_offset => interval '1 minute',
  schedule_interval => interval '1 minute',
  if_not_exists => true
);

create or replace view market.latest_quotes as
select distinct on (instrument_id, market)
  instrument_id,
  market,
  event_time,
  observed_at,
  best_bid,
  best_ask,
  mid_price,
  spread,
  depth_imbalance,
  trading_status,
  provider
from market.market_quotes
order by instrument_id, market, event_time desc, received_at desc;

create or replace function market.reject_raw_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception '% is immutable; write a correction event instead', tg_table_schema || '.' || tg_table_name;
end;
$$;

create trigger market_ticks_immutable
before update or delete on market.market_ticks
for each row execute function market.reject_raw_mutation();

create trigger market_quotes_immutable
before update or delete on market.market_quotes
for each row execute function market.reject_raw_mutation();

create trigger derivative_snapshots_immutable
before update or delete on market.derivative_snapshots
for each row execute function market.reject_raw_mutation();

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'market_reader') then
    grant usage on schema market to market_reader;
    grant select on market.market_ticks, market.market_quotes, market.market_bars,
      market.microstructure_features, market.market_breadth, market.derivative_snapshots,
      market.data_quality_windows, market.feed_gaps, market.archive_exports,
      market.retention_registry, market.bars_1m, market.latest_quotes to market_reader;
  end if;

  if exists (select 1 from pg_roles where rolname = 'market_writer') then
    grant usage on schema market to market_writer;
    grant select, insert on market.market_ticks, market.market_quotes, market.market_bars,
      market.microstructure_features, market.market_breadth, market.derivative_snapshots,
      market.data_quality_windows, market.feed_gaps to market_writer;
    grant select, insert, update on market.ingestion_watermarks to market_writer;
  end if;
end;
$$;

commit;
