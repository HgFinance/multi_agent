begin;

-- The deferred compound-PAPER activation (20260824000600) moves a bundled rule
-- from PENDING_CONFIRMATION to ACTIVE, and the rules table requires
-- confirmation_sha256/confirmed_at to be present in that state
-- (conditional_trade_rules_check1).  The worker's column grant was written in
-- 20260820000300, before bundles existed, and only covers
-- (state, version, completed_at).  So every activation attempt failed with
-- 42501 and the rule stayed pending forever while the immediate order had
-- already filled -- the user bought the position and the protective sell rule
-- never armed (observed 2026-08-27 on 000500).
--
-- The worker writes the spec_sha256 it just read for that exact rule version,
-- so this grant does not let it confirm a rule the user never approved; it only
-- lets it record the confirmation the bundle already earned.
grant update (confirmation_sha256, confirmed_at)
  on execution.conditional_trade_rules to svc_conditional_rule_worker;

do $conditional_rule_worker_activation_privilege_audit$
begin
  if not has_column_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'confirmation_sha256',
       'UPDATE'
     )
     or not has_column_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'confirmed_at',
       'UPDATE'
     )
     or not has_column_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'state',
       'UPDATE'
     ) then
    raise exception 'conditional-rule worker cannot activate a deferred bundle';
  end if;
  -- The worker must not gain authority to author or retire a rule outright.
  if has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'INSERT'
     )
     or has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'DELETE'
     ) then
    raise exception 'conditional-rule worker must stay update-only on rules';
  end if;
end
$conditional_rule_worker_activation_privilege_audit$;

commit;
