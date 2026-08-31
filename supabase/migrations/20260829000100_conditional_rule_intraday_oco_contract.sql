begin;

-- The conditional-rule AST and LS resolver support these completed-bar
-- cadences.  The original table check predated 3M/10M/30M support, so a
-- correct validated PAPER rule would otherwise fail only at INSERT time.
alter table execution.conditional_trade_rules
  drop constraint if exists conditional_trade_rules_primary_timeframe_check;

alter table execution.conditional_trade_rules
  add constraint conditional_trade_rules_primary_timeframe_v2_check
  check (
    primary_timeframe is null
    or primary_timeframe in ('1M','3M','5M','10M','15M','30M','1H','1D')
  );

-- OCO identity is immutable inside the confirmed version JSON.  All worker
-- arbitration queries use this expression while still joining on the exact
-- current version; indexing it prevents an OCO submission from scanning every
-- historical rule version under market load.
create index if not exists conditional_rule_versions_oco_group_idx
  on execution.conditional_trade_rule_versions ((spec->>'oco_group_id'))
  where spec ? 'oco_group_id';

comment on index execution.conditional_rule_versions_oco_group_idx is
  'Current-version OCO lookup for conditional PAPER submission arbitration.';

commit;
