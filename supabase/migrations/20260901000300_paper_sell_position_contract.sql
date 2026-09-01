begin;

-- Keep the durable PAPER ledger aligned with the existing application
-- contract.  SELL_POSITION is not a new execution path: it is the
-- reduce-only, one-instrument counterpart of SELL_ALL and is executed by the
-- same directive service.  A one-member PLACE_BASKET likewise reuses the
-- existing notional-sizing basket path.

alter table execution.user_directives
  drop constraint if exists user_directives_action_v3_check,
  drop constraint if exists user_directives_action_v4_check,
  drop constraint if exists user_directives_payload_v3_check,
  drop constraint if exists user_directives_payload_v4_check,
  drop constraint if exists user_directives_priority_v3_check,
  drop constraint if exists user_directives_priority_v4_check;

alter table execution.user_directives
  add constraint user_directives_action_v4_check
    check (
      action in (
        'PLACE_ORDER', 'PLACE_BASKET', 'CANCEL_ALL', 'SELL_ALL', 'SELL_POSITION'
      )
    ),
  add constraint user_directives_payload_v4_check check (
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
        (payload->>'order_type' = 'MARKET'
          and jsonb_typeof(payload->'limit_price') = 'null')
        or
        (payload->>'order_type' = 'LIMIT'
          and jsonb_typeof(payload->'limit_price') = 'string'
          and payload->>'limit_price' ~ '^[0-9]+(\.[0-9]+)?$')
      ))
    or
    (action = 'PLACE_BASKET'
      and payload ? 'orders'
      and (payload - 'orders') = '{}'::jsonb
      and jsonb_typeof(payload->'orders') = 'array'
      and jsonb_array_length(payload->'orders') between 1 and 20)
    or
    (action in ('CANCEL_ALL', 'SELL_ALL') and payload = '{}'::jsonb)
    or
    (action = 'SELL_POSITION'
      and payload ?& array['instrument_id','symbol','side','order_type',
                            'time_in_force','reduce_only']
      and (payload - array['instrument_id','symbol','side','order_type',
                            'time_in_force','reduce_only']) = '{}'::jsonb
      and jsonb_typeof(payload->'instrument_id') in ('null','string')
      and payload->>'symbol' ~ '^[0-9A-Z]{6}$'
      and payload->>'side' = 'SELL'
      and payload->>'order_type' = 'MARKET'
      and payload->>'time_in_force' = 'DAY'
      and payload->'reduce_only' = 'true'::jsonb)
  ),
  add constraint user_directives_priority_v4_check check (
    (action in ('PLACE_ORDER', 'PLACE_BASKET') and priority = 1000)
    or
    (action in ('CANCEL_ALL', 'SELL_ALL', 'SELL_POSITION') and priority = 2000)
  );

alter table execution.user_directive_proofs
  drop constraint if exists user_directive_proofs_action_v3_check,
  drop constraint if exists user_directive_proofs_action_v4_check;

alter table execution.user_directive_proofs
  add constraint user_directive_proofs_action_v4_check
    check (
      action in (
        'PLACE_ORDER', 'PLACE_BASKET', 'CANCEL_ALL', 'SELL_ALL', 'SELL_POSITION'
      )
    );

alter table execution.user_order_requests
  drop constraint if exists user_order_requests_action_v2_check,
  drop constraint if exists user_order_requests_action_v3_check;

alter table execution.user_order_requests
  add constraint user_order_requests_action_v3_check
    check (
      action is null
      or action in (
        'PLACE_ORDER', 'PLACE_BASKET', 'SELL_ALL', 'SELL_POSITION', 'CANCEL_ALL'
      )
    );

comment on constraint user_directives_payload_v4_check
  on execution.user_directives is
  'PAPER directives accept 1..20 basket members and strict reduce-only SELL_POSITION payloads; Trading remains the sole executor.';

commit;
