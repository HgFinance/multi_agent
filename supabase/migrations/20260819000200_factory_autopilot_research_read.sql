begin;

-- The AWS factory-autopilot enters svc_quant before measuring the research ->
-- quant queue. Keep that session least-privileged while exposing only the
-- immutable planning inputs that its deterministic SQL joins.
grant usage on schema research to svc_quant;
grant select on
  research.methodology_leads,
  research.experiment_proposals,
  research.proposal_review_outcomes
to svc_quant;

revoke insert, update, delete, truncate on
  research.methodology_leads,
  research.experiment_proposals,
  research.proposal_review_outcomes
from svc_quant;

do $factory_autopilot_research_read_audit$
declare
  required_relation text;
begin
  foreach required_relation in array array[
    'research.methodology_leads',
    'research.experiment_proposals',
    'research.proposal_review_outcomes'
  ] loop
    if not has_table_privilege('svc_quant', required_relation, 'SELECT') then
      raise exception 'svc_quant cannot read factory planning input %',
        required_relation;
    end if;
    if has_table_privilege('svc_quant', required_relation, 'INSERT')
       or has_table_privilege('svc_quant', required_relation, 'UPDATE')
       or has_table_privilege('svc_quant', required_relation, 'DELETE')
       or has_table_privilege('svc_quant', required_relation, 'TRUNCATE') then
      raise exception 'svc_quant can mutate factory planning input %',
        required_relation;
    end if;
  end loop;
end
$factory_autopilot_research_read_audit$;

commit;
