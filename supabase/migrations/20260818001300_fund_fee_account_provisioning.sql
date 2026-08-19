begin;

-- 펀드별 Chart of Accounts의 보수 발생주의 계정은 펀드 생성과 같은 트랜잭션에서
-- 만들어야 한다. 2100/5200/5300은 전역 계정이 아니라 fund_id마다 하나씩 존재한다.
create or replace function accounting.provision_fee_accounts_for_new_fund()
returns trigger
language plpgsql
set search_path = pg_catalog, accounting
as $$
begin
  insert into accounting.ledger_accounts
    (fund_id, account_code, name, account_type, currency)
  values
    (new.fund_id, '2100', '미지급보수',   'LIABILITY', new.base_currency),
    (new.fund_id, '5200', '관리보수비용', 'EXPENSE',   new.base_currency),
    (new.fund_id, '5300', '성과보수비용', 'EXPENSE',   new.base_currency)
  on conflict (fund_id, account_code) do nothing;

  return new;
end;
$$;

drop trigger if exists funds_provision_fee_accounts on accounting.funds;
create trigger funds_provision_fee_accounts
after insert on accounting.funds
for each row execute function accounting.provision_fee_accounts_for_new_fund();

-- 이미 적용된 환경에 누락된 보수 계정을 한 번 보정한다. 새 펀드는 위 트리거가 맡는다.
insert into accounting.ledger_accounts (fund_id, account_code, name, account_type, currency)
select
  f.fund_id,
  fee.account_code,
  fee.name,
  fee.account_type,
  f.base_currency
from accounting.funds f
cross join (
  values
    ('2100', '미지급보수',   'LIABILITY'),
    ('5200', '관리보수비용', 'EXPENSE'),
    ('5300', '성과보수비용', 'EXPENSE')
) as fee(account_code, name, account_type)
on conflict (fund_id, account_code) do nothing;

commit;
