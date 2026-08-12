begin;

-- 보수 발생주의 계정과목 3개.
-- 소유: 도현 (회계·포트폴리오본부)
-- 근거: HEDGE_FUND_MASTER_PLAN.md 19.13(Fee/Tax 발생), 12.3(Fund Ledger)
--       departments/05-accounting-portfolio/fees/fee_accrual.py
--
-- **거래 수수료(5000)에 보수를 섞지 않는다.** 5000은 체결 비용이라 TCA가 집행
-- 품질을 재는 데 쓴다. 관리보수를 거기 넣으면 거래를 안 한 날에도 집행 비용이
-- 생긴 것처럼 보이고, 전략 알파와 운용 보수를 분리할 수 없다.
--
-- 미지급보수(2100)를 미지급금(2000)과 나누는 이유도 같다. 2000은 T+2 결제 대금이고
-- 며칠 안에 현금으로 나간다. 보수는 확정(crystallization) 전까지 남아 있는 부채라
-- 한 계정에 섞이면 결제 사다리와 보수 잔액을 구분할 수 없다.

insert into accounting.ledger_accounts (account_id, account_code, name, account_type, currency)
values
  ('00000000-0000-0000-0000-000000002100', '2100', '미지급보수',   'LIABILITY', 'KRW'),
  ('00000000-0000-0000-0000-000000005200', '5200', '관리보수비용', 'EXPENSE',   'KRW'),
  ('00000000-0000-0000-0000-000000005300', '5300', '성과보수비용', 'EXPENSE',   'KRW')
on conflict (account_code) do nothing;

commit;
