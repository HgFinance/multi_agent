\set ON_ERROR_STOP on

-- Run after all Supabase migrations against an isolated PostgreSQL database.
-- The transaction rolls back every fixture.
begin;

set session_replication_role = replica;
insert into execution.order_intents (
  order_intent_id,trade_case_id,intent_group_id,fund_id,book_id,
  strategy_version_id,instrument_id,side,position_effect,leg_index,
  order_type,quantity,time_in_force,valid_until,market_snapshot_id,
  intent_status,idempotency_key,schema_version,trace_id
) values (
  '00000000-0000-0000-0000-000000000101',
  '00000000-0000-0000-0000-000000000102',
  '00000000-0000-0000-0000-000000000103',
  '00000000-0000-0000-0000-000000000104',
  '00000000-0000-0000-0000-000000000105',
  '00000000-0000-0000-0000-000000000106',
  '00000000-0000-0000-0000-000000000107',
  'BUY','OPEN',0,'MARKET',1,'DAY',now()+interval '1 day',
  '00000000-0000-0000-0000-000000000108',
  'READY_TO_SUBMIT','pg-smoke-strategy-buy',1,
  '00000000-0000-0000-0000-000000000109'
);
insert into execution.orders (
  order_id,order_intent_id,client_order_id,broker_order_id,broker_adapter,
  state,requested_quantity,filled_quantity,average_fill_price,trace_id
) values (
  '00000000-0000-0000-0000-000000000110',
  '00000000-0000-0000-0000-000000000101',
  'pg-smoke-client','pg-smoke-broker-order','paper','FILLED',1,1,100,
  '00000000-0000-0000-0000-000000000109'
);
insert into execution.fills (
  fill_id,order_id,broker_fill_id,instrument_id,side,quantity,price,
  gross_amount,fee_amount,tax_amount,currency,event_time,received_at,trace_id
) values (
  '00000000-0000-0000-0000-000000000111',
  '00000000-0000-0000-0000-000000000110',
  'pg-smoke-strategy-fill',
  '00000000-0000-0000-0000-000000000107',
  'BUY',1,100,100,0,0,'KRW',now(),now(),
  '00000000-0000-0000-0000-000000000109'
);
insert into execution.outbox (
  event_id,event_type,schema_version,trace_id,producer,occurred_at,
  idempotency_key,payload_ref,status,sent_at
) values (
  '00000000-0000-0000-0000-000000000112',
  'trading.fill.v1','event-envelope-v1',
  '00000000-0000-0000-0000-000000000109',
  'trading-oms',now(),'pg-smoke-strategy-fill-envelope',
  jsonb_build_object(
    'artifact_type','FILL',
    'artifact_id','00000000-0000-0000-0000-000000000111',
    'artifact_schema','execution-fill-v1'
  ),
  'SENT',now()
);
set session_replication_role = origin;

-- A committed Journal is deliberately present while projection/receipt ACK is
-- absent. SELL_ALL must still see the inbound BUY as pending in this window.
set session_replication_role = replica;
insert into accounting.journals (
  journal_id,fund_id,book_id,event_type,source_event_id,effective_at,
  accounting_date,base_currency,status,created_by_service,trace_id
) values (
  '00000000-0000-0000-0000-000000000113',
  '00000000-0000-0000-0000-000000000104',
  '00000000-0000-0000-0000-000000000105',
  'fill','pg-smoke-strategy-fill',now(),current_date,'KRW','DRAFT',
  'accounting-ledger','00000000-0000-0000-0000-000000000109'
);
set session_replication_role = origin;

set local role svc_trading_api;
do $guard_before_ack$
begin
  if not exists (
    select 1
      from execution.fills fill
      join execution.orders broker_order on broker_order.order_id=fill.order_id
      join execution.order_intents intent
        on intent.order_intent_id=broker_order.order_intent_id
      left join execution.outbox envelope
        on envelope.event_type='trading.fill.v1'
       and envelope.payload_ref->>'artifact_type'='FILL'
       and envelope.payload_ref->>'artifact_id'=fill.fill_id::text
      left join execution.outbox_consumed consumed
        on consumed.event_id=envelope.event_id
       and consumed.consumer='accounting-ledger'
     where intent.fund_id='00000000-0000-0000-0000-000000000104'
       and intent.book_id='00000000-0000-0000-0000-000000000105'
       and fill.side='BUY'
       and (envelope.event_id is null or consumed.event_id is null)
  ) then
    raise exception 'strategy BUY was hidden before projection receipt ACK';
  end if;
  if has_table_privilege(current_user,'execution.orders','UPDATE') then
    raise exception 'trading role unexpectedly has broad order UPDATE';
  end if;
end
$guard_before_ack$;

reset role;
set local role svc_accounting_ledger;
do $ledger_role_contract$
begin
  if not has_table_privilege(
    current_user,'accounting.journals','INSERT'
  ) or not has_table_privilege(
    current_user,'execution.outbox_consumed','INSERT'
  ) then
    raise exception 'ledger role lacks a required posting/receipt privilege';
  end if;
  if has_table_privilege(current_user,'execution.orders','UPDATE')
     or has_table_privilege(
       current_user,'governance.user_profiles','SELECT'
     ) then
    raise exception 'ledger role escaped its least-privilege boundary';
  end if;
end
$ledger_role_contract$;
insert into execution.outbox_consumed (consumer,event_id)
values ('accounting-ledger','00000000-0000-0000-0000-000000000112');

reset role;
set local role svc_trading_api;
do $guard_after_ack$
begin
  if exists (
    select 1
      from execution.fills fill
      join execution.orders broker_order on broker_order.order_id=fill.order_id
      join execution.order_intents intent
        on intent.order_intent_id=broker_order.order_intent_id
      left join execution.outbox envelope
        on envelope.event_type='trading.fill.v1'
       and envelope.payload_ref->>'artifact_type'='FILL'
       and envelope.payload_ref->>'artifact_id'=fill.fill_id::text
      left join execution.outbox_consumed consumed
        on consumed.event_id=envelope.event_id
       and consumed.consumer='accounting-ledger'
     where intent.fund_id='00000000-0000-0000-0000-000000000104'
       and intent.book_id='00000000-0000-0000-0000-000000000105'
       and fill.side='BUY'
       and (envelope.event_id is null or consumed.event_id is null)
  ) then
    raise exception 'strategy BUY remained pending after projection receipt ACK';
  end if;
end
$guard_after_ack$;

reset role;
rollback;
