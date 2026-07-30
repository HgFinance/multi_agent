begin;

insert into accounting.funds (
  fund_id, fund_code, name, base_currency, inception_date, status
) values (
  '10000000-0000-0000-0000-000000000001',
  'SCHEMA-SMOKE',
  'Schema Smoke Fund',
  'KRW',
  current_date,
  'ACTIVE'
);

insert into accounting.books (
  book_id, fund_id, book_code, name, book_type, status
) values (
  '20000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  'PAPER',
  'Paper Book',
  'PAPER',
  'ACTIVE'
);

insert into accounting.ledger_accounts (
  account_id, fund_id, account_code, name, account_type, currency
) values
  (
    '30000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'CASH-KRW',
    'Cash KRW',
    'ASSET',
    'KRW'
  ),
  (
    '30000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    'CAPITAL-KRW',
    'Capital KRW',
    'EQUITY',
    'KRW'
  );

insert into accounting.journals (
  journal_id,
  fund_id,
  book_id,
  event_type,
  source_event_id,
  effective_at,
  accounting_date,
  base_currency,
  created_by_service,
  trace_id
) values (
  '40000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001',
  'SMOKE',
  'SMOKE-UNBALANCED',
  now(),
  current_date,
  'KRW',
  'schema-test',
  '50000000-0000-0000-0000-000000000001'
);

insert into accounting.journal_lines (
  journal_id, account_id, line_no, debit, credit, currency
) values
  (
    '40000000-0000-0000-0000-000000000001',
    '30000000-0000-0000-0000-000000000001',
    1,
    100,
    0,
    'KRW'
  ),
  (
    '40000000-0000-0000-0000-000000000001',
    '30000000-0000-0000-0000-000000000002',
    2,
    0,
    90,
    'KRW'
  );

do $$
begin
  begin
    update accounting.journals
    set status = 'POSTED'
    where journal_id = '40000000-0000-0000-0000-000000000001';
    raise exception 'expected unbalanced journal rejection';
  exception
    when others then
      if sqlerrm not like 'journal % is not balanced; base imbalance=%' then
        raise;
      end if;
  end;
end;
$$;

update accounting.journal_lines
set credit = 100
where journal_id = '40000000-0000-0000-0000-000000000001'
  and line_no = 2;

update accounting.journals
set status = 'POSTED'
where journal_id = '40000000-0000-0000-0000-000000000001';

do $$
begin
  begin
    update accounting.journals
    set source_event_id = 'MUTATED-AFTER-POSTING'
    where journal_id = '40000000-0000-0000-0000-000000000001';
    raise exception 'expected posted journal mutation rejection';
  exception
    when others then
      if sqlerrm not like 'journal % is immutable in status POSTED' then
        raise;
      end if;
  end;
end;
$$;

create temporary table smoke_orders (
  state text not null
);

create trigger smoke_orders_validate_state
before update of state on smoke_orders
for each row execute function execution.validate_order_state_transition();

insert into smoke_orders (state) values ('CREATED');

do $$
begin
  begin
    update smoke_orders set state = 'FILLED';
    raise exception 'expected invalid OMS transition rejection';
  exception
    when others then
      if sqlerrm not like 'invalid order state transition:%' then
        raise;
      end if;
  end;
end;
$$;

update smoke_orders set state = 'SUBMITTED';
update smoke_orders set state = 'ACKNOWLEDGED';
update smoke_orders set state = 'PARTIALLY_FILLED';
update smoke_orders set state = 'FILLED';

create temporary table smoke_events (
  event_id uuid primary key default gen_random_uuid(),
  payload jsonb not null
);

create trigger smoke_events_append_only
before update or delete on smoke_events
for each row execute function governance.reject_append_only_change();

insert into smoke_events (payload) values ('{}'::jsonb);

do $$
begin
  begin
    update smoke_events set payload = '{"changed": true}'::jsonb;
    raise exception 'expected append-only rejection';
  exception
    when others then
      if sqlerrm not like '% is append-only' then
        raise;
      end if;
  end;
end;
$$;

rollback;
