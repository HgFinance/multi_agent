begin;

-- experiment_outcomes.proposal_id was left empty by both the live orchestrator
-- and orphan finalizer even though the hypothesis retained the causal proposal.
-- Repair historical rows deterministically and let future inserts inherit the
-- same value in factory_bridge._SQL_INSERT_OUTCOME.
update research.experiment_outcomes o
   set proposal_id = h.proposal_id
  from quant.experiments e
  join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
 where o.experiment_id = e.experiment_id::text
   and o.hypothesis_id = h.hypothesis_id::text
   and o.proposal_id = ''
   and h.proposal_id is not null;

commit;
