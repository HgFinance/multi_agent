begin;

-- RLS predicates on the canonical trading tables call this SECURITY DEFINER
-- ownership function.  The executor may invoke it, but does not receive any
-- additional table privilege through this grant.
grant execute on function governance.can_access_fund(uuid)
  to svc_strategy_paper_executor;

commit;
