begin;

-- The imported 61-session archive is a different scientific dataset from the
-- live receipt-clock feed.  It exposes completed exchange-second snapshots via
-- a read-only FDW, has no receipt clock or intra-second order, and may support
-- historical TAKER search only.  Every experiment still freezes exact sessions,
-- instruments, raw row multisets, and the fixed regular-session content window.
insert into quant.dataset_manifests
  (name, version, as_of, source_versions, feature_spec_versions, partitions,
   point_in_time_policy, quality_summary, object_path, content_hash, row_count,
   schema_definition, notional_unit, volume_unit)
values
  ('krx-intraday-completed-second', 'v1', now(),
   '{"market_quotes":"trading-bot-completed-second-book-v1",'
     '"market_ticks":"trading-bot-completed-second-trade-v1"}'::jsonb,
   '{"intraday_microstructure":"intraday-microstructure-v1",'
     '"clock_aggregation":"completed-second-state-median-taker-envelope-v1",'
     '"intraday_alpha_ast":"intraday-alpha-ast-v1"}'::jsonb,
   '[]'::jsonb,
   '{"knowledge_clock":"event_time_only_no_receipt_clock",'
     '"feature_cutoff":"completed_source_second<=decision_time",'
     '"label_cutoff":"effective_entry_time+horizon",'
     '"instrument_isolation":true,'
     '"evidence_scope":"HISTORICAL_SEARCH_ONLY",'
     '"content_window":"[09:00:00,15:30:00) Asia/Seoul",'
     '"maximum_horizon_seconds":600}'::jsonb,
   '{"status":"HISTORICAL_COMPLETED_SECOND_REQUIRES_PER_EXPERIMENT_AUDIT",'
     '"timestamp_resolution":"SECOND",'
     '"intra_second_order":"UNAVAILABLE",'
     '"receipt_clock":"UNAVAILABLE",'
     '"execution":"TAKER_ONLY"}'::jsonb,
   'postgresql+fdw://ext_src/{quotes,ticks}',
   'c76c3f359b1881f6e7613b56fc8f10cb7f09e91a132d3bfe0a6a527151339e14',
   null,
   '{"market_quotes":{"physical_table":"ext_src.quotes",'
     '"required":["ts","symbol","bid1","ask1","bid_vol1","ask_vol1",'
                  '"bid10","ask10","bid_vol10","ask_vol10"]},'
     '"market_ticks":{"physical_table":"ext_src.ticks",'
     '"required":["ts","symbol","price","volume","ofi_contrib"]}}'::jsonb,
   'KRW', 'SHARES')
on conflict (name, version) do update set
  source_versions = excluded.source_versions,
  feature_spec_versions = excluded.feature_spec_versions,
  partitions = excluded.partitions,
  point_in_time_policy = excluded.point_in_time_policy,
  quality_summary = excluded.quality_summary,
  object_path = excluded.object_path,
  content_hash = excluded.content_hash,
  row_count = excluded.row_count,
  schema_definition = excluded.schema_definition,
  notional_unit = excluded.notional_unit,
  volume_unit = excluded.volume_unit;

commit;
