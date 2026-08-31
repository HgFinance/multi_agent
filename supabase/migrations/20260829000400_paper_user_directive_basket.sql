begin;

-- A basket is one authenticated PAPER directive with multiple durable legs.
-- It is intentionally restricted to the Trading contract's KRW BUY/MARKET
-- shape; quantity calculation still happens only under the Trading book lock
-- against fresh executable quotes.

do $$
declare
  constraint_name name;
begin
  for constraint_name in
    select c.conname
      from pg_constraint c
     where c.conrelid = 'execution.user_directives'::regclass
       and c.contype = 'c'
       and position('PLACE_ORDER' in pg_get_constraintdef(c.oid)) > 0
       and (
         position('payload' in lower(pg_get_constraintdef(c.oid))) > 0
         or position('priority' in lower(pg_get_constraintdef(c.oid))) > 0
         or (
           position('action' in lower(pg_get_constraintdef(c.oid))) > 0
           and position('payload' in lower(pg_get_constraintdef(c.oid))) = 0
           and position('priority' in lower(pg_get_constraintdef(c.oid))) = 0
         )
       )
  loop
    execute format(
      'alter table execution.user_directives drop constraint %I',
      constraint_name
    );
  end loop;
end;
$$;

alter table execution.user_directives
  add constraint user_directives_action_v3_check
    check (action in ('PLACE_ORDER', 'PLACE_BASKET', 'CANCEL_ALL', 'SELL_ALL')),
  add constraint user_directives_payload_v3_check check (
    (action = 'PLACE_ORDER'
      and payload ?& array['symbol','instrument_id','side','quantity',
                            'order_type','limit_price','time_in_force']
      and (payload - array['symbol','instrument_id','side','quantity',
                            'order_type','limit_price','time_in_force']) = '{}'::jsonb
      and payload->>'symbol' ~ '^[0-9A-Z]{6}$'
      and payload->>'side' in ('BUY','SELL')
      and payload->>'order_type' in ('MARKET','LIMIT')
      and payload->>'time_in_force' = 'DAY'
      and jsonb_typeof(payload->'quantity') = 'string'
      and payload->>'quantity' ~ '^[0-9]+(\.[0-9]+)?$'
      and jsonb_typeof(payload->'instrument_id') in ('null','string')
      and (
        (payload->>'order_type'='MARKET'
          and jsonb_typeof(payload->'limit_price')='null')
        or
        (payload->>'order_type'='LIMIT'
          and jsonb_typeof(payload->'limit_price')='string'
          and payload->>'limit_price' ~ '^[0-9]+(\.[0-9]+)?$')
      ))
    or
    (action = 'PLACE_BASKET'
      and payload ? 'orders'
      and (payload - 'orders') = '{}'::jsonb
      and jsonb_typeof(payload->'orders') = 'array'
      and jsonb_array_length(payload->'orders') between 2 and 20)
    or
    (action in ('CANCEL_ALL', 'SELL_ALL') and payload = '{}'::jsonb)
  ),
  add constraint user_directives_priority_v3_check check (
    (action in ('PLACE_ORDER', 'PLACE_BASKET') and priority = 1000)
    or (action in ('CANCEL_ALL', 'SELL_ALL') and priority = 2000)
  );

do $$
declare
  constraint_name name;
begin
  for constraint_name in
    select c.conname
      from pg_constraint c
     where c.conrelid = 'execution.user_directive_proofs'::regclass
       and c.contype = 'c'
       and position('action' in lower(pg_get_constraintdef(c.oid))) > 0
       and position('PLACE_ORDER' in pg_get_constraintdef(c.oid)) > 0
  loop
    execute format(
      'alter table execution.user_directive_proofs drop constraint %I',
      constraint_name
    );
  end loop;
end;
$$;

alter table execution.user_directive_proofs
  add constraint user_directive_proofs_action_v3_check
    check (action in ('PLACE_ORDER', 'PLACE_BASKET', 'CANCEL_ALL', 'SELL_ALL'));

do $$
declare
  constraint_name name;
begin
  for constraint_name in
    select c.conname
      from pg_constraint c
     where c.conrelid = 'execution.user_order_requests'::regclass
       and c.contype = 'c'
       and position('action' in lower(pg_get_constraintdef(c.oid))) > 0
       and position('PLACE_ORDER' in pg_get_constraintdef(c.oid)) > 0
  loop
    execute format(
      'alter table execution.user_order_requests drop constraint %I',
      constraint_name
    );
  end loop;
end;
$$;

alter table execution.user_order_requests
  add constraint user_order_requests_action_v2_check
    check (
      action is null
      or action in ('PLACE_ORDER', 'PLACE_BASKET', 'SELL_ALL', 'CANCEL_ALL')
    );

comment on constraint user_directives_payload_v3_check
  on execution.user_directives is
  'PLACE_BASKET accepts only a bounded orders array; Trading revalidates each KRW BUY/MARKET member before broker admission.';

commit;
