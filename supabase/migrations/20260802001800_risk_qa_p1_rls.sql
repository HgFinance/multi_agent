begin;

-- Risk/QA P1 read boundaries only.
-- Writes remain service-role/domain-service responsibilities; no seed data is
-- created here.  These policies make missing fund scope fail closed instead
-- of exposing a cross-fund snapshot or operational trace.

alter table risk.exposure_components enable row level security;
alter table risk.stress_results enable row level security;
alter table risk.kill_switch_events enable row level security;
alter table audit.agent_runs enable row level security;
alter table audit.tool_calls enable row level security;

create policy risk_exposure_components_fund_member_select
on risk.exposure_components
for select
using (
  exists (
    select 1
    from risk.snapshots snapshot
    where snapshot.risk_snapshot_id = exposure_components.risk_snapshot_id
      and governance.can_access_fund(snapshot.fund_id)
  )
);

create policy risk_stress_results_fund_member_select
on risk.stress_results
for select
using (
  exists (
    select 1
    from risk.snapshots snapshot
    where snapshot.risk_snapshot_id = stress_results.risk_snapshot_id
      and governance.can_access_fund(snapshot.fund_id)
  )
);

create policy risk_kill_switch_events_fund_member_select
on risk.kill_switch_events
for select
using (governance.can_access_fund(fund_id));

create policy audit_agent_runs_fund_member_select
on audit.agent_runs
for select
using (fund_id is not null and governance.can_access_fund(fund_id));

create policy audit_tool_calls_fund_member_select
on audit.tool_calls
for select
using (
  exists (
    select 1
    from audit.agent_runs run
    where run.agent_run_id = tool_calls.agent_run_id
      and run.fund_id is not null
      and governance.can_access_fund(run.fund_id)
  )
);

commit;
