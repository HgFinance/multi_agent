begin;

-- CSPAT00601 may succeed before the Trading API persists its acknowledgement.
-- The runtime repository writes the LS order number together with the leg's
-- ACKNOWLEDGED transition.  The original role grant omitted broker_order_id,
-- so every successful external acknowledgement failed after the broker had
-- already accepted the order and surfaced to callers as HTTP 500/UNKNOWN.
grant update (broker_order_id)
  on execution.user_directive_legs to svc_trading_api;

do $ls_paper_broker_order_ack_role_audit$
begin
  if not has_column_privilege(
       'svc_trading_api',
       'execution.user_directive_legs',
       'broker_order_id',
       'UPDATE'
     ) then
    raise exception
      'svc_trading_api cannot persist the LS PAPER broker order acknowledgement';
  end if;

  -- The repair is intentionally column-scoped.  It must not widen Trading's
  -- authority to delete legs or rewrite their user/book/instrument identity.
  if has_table_privilege(
       'svc_trading_api', 'execution.user_directive_legs', 'DELETE'
     )
     or has_column_privilege(
       'svc_trading_api', 'execution.user_directive_legs', 'directive_id', 'UPDATE'
     )
     or has_column_privilege(
       'svc_trading_api', 'execution.user_directive_legs', 'instrument_id', 'UPDATE'
     )
     or has_column_privilege(
       'svc_trading_api', 'execution.user_directive_legs', 'requested_quantity', 'UPDATE'
     ) then
    raise exception 'LS PAPER acknowledgement repair widened the Trading role';
  end if;
end
$ls_paper_broker_order_ack_role_audit$;

comment on column execution.user_directive_legs.broker_order_id is
  'Immutable external acknowledgement key; LS PAPER writes it once before read-only fill reconciliation';

commit;
