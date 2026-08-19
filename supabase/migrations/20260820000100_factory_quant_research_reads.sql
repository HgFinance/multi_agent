begin;

-- factory_autopilot connects as svc_quant and needs read-only access to the
-- governed research queue it measures.  Keep the boundary narrow: no research
-- writes and no execution, accounting, or governance privileges are granted.
grant usage on schema research to svc_quant;
grant select on
  research.methodology_leads,
  research.experiment_proposals,
  research.proposal_review_outcomes
to svc_quant;

commit;
