begin;

insert into governance.user_profiles (user_id, display_name)
values ('90000000-0000-0000-0000-000000000001', 'RLS Smoke User');

insert into accounting.funds (
  fund_id, fund_code, name, base_currency, inception_date, status
) values (
  '91000000-0000-0000-0000-000000000001',
  'RLS-SMOKE',
  'RLS Smoke Fund',
  'KRW',
  current_date,
  'ACTIVE'
);

insert into governance.fund_memberships (fund_id, user_id, role)
values (
  '91000000-0000-0000-0000-000000000001',
  '90000000-0000-0000-0000-000000000001',
  'OWNER'
);

insert into accounting.funds (
  fund_id, fund_code, name, base_currency, inception_date, status
) values (
  '91000000-0000-0000-0000-000000000002',
  'RLS-HIDDEN',
  'RLS Hidden Fund',
  'KRW',
  current_date,
  'ACTIVE'
);

insert into accounting.books (
  book_id, fund_id, book_code, name, book_type, status
) values
  (
    '91100000-0000-0000-0000-000000000001',
    '91000000-0000-0000-0000-000000000001',
    'VISIBLE',
    'Visible Book',
    'PAPER',
    'ACTIVE'
  ),
  (
    '91100000-0000-0000-0000-000000000002',
    '91000000-0000-0000-0000-000000000002',
    'HIDDEN',
    'Hidden Book',
    'PAPER',
    'ACTIVE'
  );

insert into reference.instruments (
  instrument_id,
  instrument_type,
  asset_class,
  market,
  currency,
  display_name,
  listed_from,
  status
) values (
  '91200000-0000-0000-0000-000000000001',
  'COMMON_STOCK',
  'EQUITY',
  'KRX',
  'KRW',
  'RLS Smoke Instrument',
  current_date,
  'ACTIVE'
);

insert into accounting.positions (
  position_id,
  fund_id,
  book_id,
  instrument_id,
  quantity,
  average_cost,
  cost_currency,
  realized_pnl,
  version,
  as_of
) values
  (
    '91300000-0000-0000-0000-000000000001',
    '91000000-0000-0000-0000-000000000001',
    '91100000-0000-0000-0000-000000000001',
    '91200000-0000-0000-0000-000000000001',
    10,
    10000,
    'KRW',
    0,
    1,
    now()
  ),
  (
    '91300000-0000-0000-0000-000000000002',
    '91000000-0000-0000-0000-000000000002',
    '91100000-0000-0000-0000-000000000002',
    '91200000-0000-0000-0000-000000000001',
    20,
    10000,
    'KRW',
    0,
    1,
    now()
  );

select set_config(
  'request.jwt.claim.sub',
  '90000000-0000-0000-0000-000000000001',
  true
);
select set_config('request.jwt.claim.role', 'authenticated', true);

set local role authenticated;

do $$
begin
  begin
    execute 'select count(*) from accounting.funds';
    raise exception 'expected direct internal schema access rejection';
  exception
    when insufficient_privilege then null;
  end;
end;
$$;

do $$
declare
  visible_positions integer;
begin
  select count(*) into visible_positions from api.positions;
  if visible_positions <> 1 then
    raise exception 'expected one visible position, got %', visible_positions;
  end if;
end;
$$;

select count(*) from api.investment_cases;
select count(*) from api.open_orders;
select count(*) from api.risk_status;
select count(*) from api.strategy_registry;
select count(*) from api.agent_registry;
select count(*) from api.get_case_timeline('92000000-0000-0000-0000-000000000001');

reset role;
rollback;
