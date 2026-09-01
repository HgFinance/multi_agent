begin;

-- A stale quote may be retried only on the existing, already-claimed
-- conditional execution. This never creates a second directive or order path.
alter table execution.conditional_rule_executions
  add column if not exists submission_attempts integer not null default 0
  check (submission_attempts >= 0);

grant update (submission_attempts)
  on execution.conditional_rule_executions to svc_conditional_rule_worker;

commit;
