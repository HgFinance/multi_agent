begin;

-- Natural-language order status is append-only in this table. Operators need
-- to join Discord/Web request -> directive quickly, and investigate a broker
-- order number without scanning every historical JSON payload.
create index if not exists user_order_request_events_directive_correlation_idx
  on execution.user_order_request_events (
    (payload ->> 'directive_id'), created_at, event_id
  )
  where event_type = 'BROKER_EXECUTION_SNAPSHOT'
    and payload ? 'directive_id';

create index if not exists user_order_request_events_broker_payload_idx
  on execution.user_order_request_events
  using gin (payload jsonb_path_ops)
  where event_type = 'BROKER_EXECUTION_SNAPSHOT';

comment on index execution.user_order_request_events_directive_correlation_idx is
  'Discord/Web order request to durable directive status correlation';
comment on index execution.user_order_request_events_broker_payload_idx is
  'Containment lookup for broker order/fill identifiers in execution snapshots';

commit;
