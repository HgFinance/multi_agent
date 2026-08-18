begin;

-- KRX short codes are six uppercase alphanumeric characters.  The original
-- PAPER directive migration admitted numeric-only codes, so replace only the
-- two symbol-format checks without rewriting an already-applied migration.
do $$
declare
  matched_constraints name[];
begin
  select array_agg(c.conname order by c.conname)
    into matched_constraints
    from pg_constraint c
   where c.conrelid = 'execution.user_directives'::regclass
     and c.contype = 'c'
     and position('payload' in lower(pg_get_constraintdef(c.oid))) > 0
     and position('symbol' in lower(pg_get_constraintdef(c.oid))) > 0
     and position('[0-9]{6}' in pg_get_constraintdef(c.oid)) > 0;

  if coalesce(cardinality(matched_constraints), 0) <> 1 then
    raise exception
      'expected exactly one numeric-only user_directives payload constraint';
  end if;
  execute format(
    'alter table execution.user_directives drop constraint %I',
    matched_constraints[1]
  );
end;
$$;

alter table execution.user_directives
  add constraint user_directives_place_order_payload_v2_check check (
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
    or (action in ('CANCEL_ALL', 'SELL_ALL') and payload = '{}'::jsonb)
  );

do $$
declare
  matched_constraints name[];
begin
  select array_agg(c.conname order by c.conname)
    into matched_constraints
    from pg_constraint c
   where c.conrelid = 'execution.user_directive_legs'::regclass
     and c.contype = 'c'
     and position('symbol' in lower(pg_get_constraintdef(c.oid))) > 0
     and position('[0-9]{6}' in pg_get_constraintdef(c.oid)) > 0;

  if coalesce(cardinality(matched_constraints), 0) <> 1 then
    raise exception
      'expected exactly one numeric-only user_directive_legs symbol constraint';
  end if;
  execute format(
    'alter table execution.user_directive_legs drop constraint %I',
    matched_constraints[1]
  );
end;
$$;

alter table execution.user_directive_legs
  add constraint user_directive_legs_krx_symbol_v2_check
  check (symbol is null or symbol ~ '^[0-9A-Z]{6}$');

comment on constraint user_directives_place_order_payload_v2_check
  on execution.user_directives is
  'PLACE_ORDER accepts only canonical six-character uppercase KRX symbols.';
comment on constraint user_directive_legs_krx_symbol_v2_check
  on execution.user_directive_legs is
  'PAPER directive legs retain canonical six-character uppercase KRX symbols.';

commit;
