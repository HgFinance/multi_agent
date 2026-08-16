begin;

-- A proposal must declare which clock/evaluator owns it.  The default preserves
-- every historical daily proposal; intraday proposals are fail-closed unless a
-- typed semantic plan and an executable event-time AST are both persisted.
alter table research.experiment_proposals
  add column if not exists research_lane text not null
    default 'DAILY_CROSS_SECTIONAL',
  add column if not exists semantic_plan jsonb not null default '{}'::jsonb;

alter table research.experiment_proposals
  drop constraint if exists chk_prop_research_lane,
  add constraint chk_prop_research_lane check (
    research_lane in ('DAILY_CROSS_SECTIONAL', 'INTRADAY_EVENT')),
  drop constraint if exists chk_prop_intraday_contract,
  add constraint chk_prop_intraday_contract check (
    research_lane <> 'INTRADAY_EVENT'
    or (
      semantic_plan <> '{}'::jsonb
      and jsonb_typeof(semantic_plan) = 'object'
      and suggested_params ? 'intraday_signal_expr'
      and coalesce(
        (data_requirements->'tables') ?& array['market_quotes', 'market_ticks'],
        false
      )
    ));

comment on column research.experiment_proposals.research_lane is
  'Deterministic evaluator route. INTRADAY_EVENT never falls back to the daily evaluator.';
comment on column research.experiment_proposals.semantic_plan is
  'Typed Event/Context/Qualities/Direction/Output plan. Numeric tuning is excluded from family identity.';

-- This manifest identifies a governed live Timescale slice, not a materialized
-- parquet snapshot.  Each experiment still persists its exact cutoff, dates,
-- instruments, row counts, and hashes in quant.experiments.config.
insert into quant.dataset_manifests
  (name, version, as_of, source_versions, feature_spec_versions, partitions,
   point_in_time_policy, quality_summary, object_path, content_hash, row_count,
   schema_definition, notional_unit, volume_unit)
values
  ('krx-intraday-events', 'v1', now(),
   '{"market_quotes":"ls-realtime-book-v1","market_ticks":"ls-realtime-trade-v1"}'::jsonb,
   '{"intraday_microstructure":"intraday-microstructure-v1","intraday_alpha_ast":"intraday-alpha-ast-v1"}'::jsonb,
   '[]'::jsonb,
   '{"knowledge_clock":"available_at=max(received_at,observed_at)","feature_cutoff":"event_time<=decision_time and available_at<=decision_time","label_cutoff":"entry_time+horizon","instrument_isolation":true}'::jsonb,
   '{"status":"LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT","missing_received_at":"reject"}'::jsonb,
   'timescaledb://market/{market_quotes,market_ticks}',
   'f11657a020260816150000000000000000000000000000000000000000000001',
   null,
   '{"market_quotes":{"required":["event_time","received_at","observed_at","instrument_id","bid_prices","bid_sizes","ask_prices","ask_sizes","source_event_id"]},"market_ticks":{"required":["event_time","received_at","observed_at","instrument_id","price","quantity","side","source_event_id"]}}'::jsonb,
   'KRW', 'SHARES')
on conflict (name, version) do update set
  source_versions = excluded.source_versions,
  feature_spec_versions = excluded.feature_spec_versions,
  point_in_time_policy = excluded.point_in_time_policy,
  quality_summary = excluded.quality_summary,
  object_path = excluded.object_path,
  schema_definition = excluded.schema_definition,
  notional_unit = excluded.notional_unit,
  volume_unit = excluded.volume_unit;

commit;
