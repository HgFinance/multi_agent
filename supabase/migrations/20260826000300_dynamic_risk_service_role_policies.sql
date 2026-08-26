begin;

-- The dynamic-risk tables were added after the original service_role policy
-- bootstrap. RLS therefore allowed browser-member reads only and rejected the
-- Risk API's canonical activation transaction at mandate_version_bindings.
grant select, insert, update on
  risk.mandate_version_bindings,
  risk.position_risk_plans,
  risk.position_risk_plan_events,
  risk.position_risk_plan_projections
to service_role;

drop policy if exists mandate_version_bindings_service_role_all
  on risk.mandate_version_bindings;
create policy mandate_version_bindings_service_role_all
  on risk.mandate_version_bindings for all to service_role
  using (true) with check (true);

drop policy if exists position_risk_plans_service_role_all
  on risk.position_risk_plans;
create policy position_risk_plans_service_role_all
  on risk.position_risk_plans for all to service_role
  using (true) with check (true);

drop policy if exists position_risk_plan_events_service_role_all
  on risk.position_risk_plan_events;
create policy position_risk_plan_events_service_role_all
  on risk.position_risk_plan_events for all to service_role
  using (true) with check (true);

drop policy if exists position_risk_plan_projections_service_role_all
  on risk.position_risk_plan_projections;
create policy position_risk_plan_projections_service_role_all
  on risk.position_risk_plan_projections for all to service_role
  using (true) with check (true);

commit;
