begin;

-- Compound PAPER activation only needs to inspect the linked immediate
-- request state.  The worker already has table SELECT privilege, but the
-- request table is protected by RLS and previously had no worker read policy.
-- No write or order-submission privilege is added here.
create policy user_order_requests_conditional_worker_read
  on execution.user_order_requests for select
  to svc_conditional_rule_worker using (true);

comment on policy user_order_requests_conditional_worker_read
  on execution.user_order_requests is
  'Read-only state lookup for deferred compound PAPER activation';

commit;
