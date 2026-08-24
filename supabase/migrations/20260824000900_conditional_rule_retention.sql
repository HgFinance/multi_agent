begin;

-- Conditional-rule retention is a maintenance concern, not part of the hot
-- evaluator.  Keep the parent rule as an idempotency tombstone and preserve
-- every execution linked to a PAPER directive.  Only the bounded audit detail
-- tables and already-published outbox rows are eligible for deletion.
create index if not exists conditional_rules_terminal_retention_idx
  on execution.conditional_trade_rules (completed_at, rule_id)
  where execution_mode='PAPER'
    and state in ('COMPLETED','EXPIRED','CANCELLED','FAILED')
    and completed_at is not null;

create index if not exists conditional_rule_executions_retention_idx
  on execution.conditional_rule_executions (rule_id, created_at, rule_execution_id)
  where directive_id is null;

create index if not exists conditional_rule_events_retention_idx
  on execution.conditional_trade_rule_events (rule_id, created_at, event_id);

create index if not exists conditional_rule_outbox_published_idx
  on execution.conditional_rule_outbox (published_at, event_id)
  where published_at is not null;

-- The existing worker role is intentionally reused so the retention lane has
-- no new database principal or broader trading authority.  It still cannot
-- write USER_DIRECTIVE rows or access accounting positions.
grant delete on execution.conditional_rule_outbox,
  execution.conditional_rule_executions,
  execution.conditional_rule_triggers,
  execution.conditional_rule_evaluations,
  execution.conditional_trade_rule_events
  to svc_conditional_rule_worker;

comment on index execution.conditional_rules_terminal_retention_idx is
  'Supports bounded cleanup of terminal PAPER conditional-rule detail.';
comment on index execution.conditional_rule_outbox_published_idx is
  'Supports cleanup of outbox events after successful publication.';

commit;
