begin;

-- The relay publishes committed execution envelopes and updates delivery
-- metadata.  It is neither the strategy OMS writer nor the user-directive API,
-- so reusing either producer role lets one producer mutate the other's rows or
-- makes valid fills invisible under RLS.  This dedicated role has no INSERT.
do $role_setup$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_trading_outbox_relay') then
    create role svc_trading_outbox_relay nologin nosuperuser nocreatedb nocreaterole noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'hgfinance_trading_runtime') then
    raise exception 'hgfinance_trading_runtime must exist before the relay grant';
  end if;
  execute 'grant svc_trading_outbox_relay to hgfinance_trading_runtime with set true, inherit false';
end
$role_setup$;

grant usage on schema execution to svc_trading_outbox_relay;
grant select on execution.outbox to svc_trading_outbox_relay;
grant update (status,sent_at,attempts,last_error,available_at)
  on execution.outbox to svc_trading_outbox_relay;

create policy outbox_svc_trading_outbox_relay_select
  on execution.outbox for select to svc_trading_outbox_relay
  using (
    schema_version = 'event-envelope-v1'
    and (
      (
        producer = 'trading-oms'
        and event_type in ('execution.order_state_changed.v1', 'trading.fill.v1')
      )
      or (
        producer = 'trading-user-directive'
        and event_type = 'trading.fill.v1'
        and payload_ref ->> 'artifact_type' = 'FILL'
        and payload_ref ->> 'artifact_schema' = 'trading-user-directive-fill-v1'
      )
    )
  );

create policy outbox_svc_trading_outbox_relay_update
  on execution.outbox for update to svc_trading_outbox_relay
  using (
    schema_version = 'event-envelope-v1'
    and (
      (
        producer = 'trading-oms'
        and event_type in ('execution.order_state_changed.v1', 'trading.fill.v1')
      )
      or (
        producer = 'trading-user-directive'
        and event_type = 'trading.fill.v1'
        and payload_ref ->> 'artifact_type' = 'FILL'
        and payload_ref ->> 'artifact_schema' = 'trading-user-directive-fill-v1'
      )
    )
  )
  with check (
    schema_version = 'event-envelope-v1'
    and status in ('PENDING','FAILED','SENT','DLQ')
    and (
      (
        producer = 'trading-oms'
        and event_type in ('execution.order_state_changed.v1', 'trading.fill.v1')
      )
      or (
        producer = 'trading-user-directive'
        and event_type = 'trading.fill.v1'
        and payload_ref ->> 'artifact_type' = 'FILL'
        and payload_ref ->> 'artifact_schema' = 'trading-user-directive-fill-v1'
      )
    )
  );

comment on role svc_trading_outbox_relay is
  'SELECT/UPDATE-only delivery role for canonical Trading outbox envelopes';

commit;
