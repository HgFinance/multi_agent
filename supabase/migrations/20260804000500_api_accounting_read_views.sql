begin;

-- 회계·포트폴리오 읽기 뷰. `/ui/snapshot`의 원천을 Scripted Loop에서 Canonical 표로 옮긴다.
--
-- 소유: 도현 (회계·포트폴리오본부)
-- 근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.1~5.2(Snapshot 계약),
--       docs/02-engineering/ACCOUNTING_PORTFOLIO_DOMAIN_API_SPEC.md 4절,
--       20260729000500_audit_api_security.sql (api 스키마 규약)
--
-- 기존 `api.positions`는 원가만 있고 평가금액이 없다(D3 미구현 시점 산물). 원본
-- 테이블에 행이 생겼으므로(2026-08-04 psycopg 원장 저장소) 화면이 읽을 수 있는
-- 모양으로 덮어쓰지 않고 **새 뷰를 추가**한다 - 기존 뷰를 쓰는 곳이 있을 수 있고,
-- 다른 본부 소유 계약을 우리가 바꾸지 않는다.
--
-- 전부 security_invoker다. 호출자 권한으로 실행되므로 RLS가 그대로 적용되고,
-- 뷰가 RLS 우회 통로가 되지 않는다(api 스키마 기존 규약과 동일).
--
-- **트레이딩 쪽 뷰는 여기 없다.** `execution.orders`/`fills`가 아직 0행이고 OMS
-- 상태가 프로세스 메모리라, 뷰를 만들면 항상 빈 화면을 보여주면서 "실데이터"인 척
-- 하게 된다. TRD-01이 psycopg OrderStore를 넣은 뒤에 같은 방식으로 추가한다.

-- Fund/Book별 최신 스냅샷 한 장. 화면의 NAV·현금·Exposure가 여기서 나온다.
--
-- quality_status를 반드시 함께 낸다. WARN이면 미확정 봉(`is_final=false`)으로
-- 평가된 NAV라는 뜻이고, 그 구분이 없으면 장중 추정치가 확정 종가와 같은 얼굴로
-- 화면에 걸린다(2026-08-03 `3978ee1` "없는 확실성을 만들어내던 것"과 같은 함정).
create or replace view api.portfolio_snapshot_latest
with (security_invoker = true)
as
select distinct on (fund_id, book_id)
  portfolio_snapshot_id,
  fund_id,
  book_id,
  as_of,
  nav,
  cash,
  positions,
  gross_exposure,
  net_exposure,
  currency,
  quality_status,
  content_hash,
  schema_version,
  created_at
from accounting.portfolio_snapshots
order by fund_id, book_id, as_of desc, created_at desc;

-- 보유 종목. **symbol을 여기서 붙인다** - market-api는 symbol로 말하고 우리 도메인은
-- instrument_id(UUID)로 말하므로 화면이 그 변환을 하게 두면 매핑이 프론트에 복제된다.
--
-- `is_primary and valid_to is null`로 "지금 유효한 대표 코드"만 쓴다. 과거 시점
-- 해석이 필요하면 이 뷰가 아니라 `reference.instrument_symbols`를 as_of로 조회한다
-- (KRX는 상장폐지 코드를 재배정한다 - `repository.instrument_by_symbol` 주석).
create or replace view api.position_holdings
with (security_invoker = true)
as
select
  p.position_id,
  p.fund_id,
  p.book_id,
  p.instrument_id,
  s.symbol,
  i.display_name,
  i.market,
  p.quantity,
  p.average_cost,
  p.quantity * p.average_cost as cost_basis,
  p.cost_currency,
  p.realized_pnl,
  p.as_of,
  p.version
from accounting.positions p
join reference.instruments i on i.instrument_id = p.instrument_id
left join reference.instrument_symbols s
  on s.instrument_id = p.instrument_id
 and s.is_primary
 and s.valid_to is null
where p.quantity <> 0;

-- 계정과목별 잔액. 합계가 0이 아니면 원장이 깨진 것이다.
--
-- **REVERSED 분개를 제외하지 않는다.** 정정은 원본을 지우는 것이 아니라 반대 분개를
-- 더하는 것이므로(불변식 2), 원본을 빼고 반대 분개만 더하면 정정 효과가 두 번 반영돼
-- 차대가 무너진다. 도메인의 `Ledger.trial_balance()`도 모든 분개를 센다.
create or replace view api.ledger_balances
with (security_invoker = true)
as
select
  j.fund_id,
  j.book_id,
  a.account_code,
  a.name as account_name,
  a.account_type,
  sum(l.base_debit - l.base_credit) as balance,
  count(*) as line_count
from accounting.journal_lines l
join accounting.journals j on j.journal_id = l.journal_id
join accounting.ledger_accounts a on a.account_id = l.account_id
where j.status in ('POSTED', 'REVERSED')
group by j.fund_id, j.book_id, a.account_code, a.name, a.account_type;

-- 미종결 Break. 리스크·QA가 읽는 자리다(이벤트 전송로는 PLAT-02 대기).
-- `escalates`가 팀 가이드 4.5 5번의 "Material Break는 리스크본부와 QA로 전달" 대상이다.
create or replace view api.open_breaks
with (security_invoker = true)
as
select
  b.break_id,
  r.fund_id,
  r.reconciliation_id,
  r.reconciliation_type,
  b.severity,
  b.status,
  b.evidence ->> 'kind' as kind,
  b.evidence ->> 'detail' as detail,
  coalesce((b.evidence ->> 'escalates')::boolean, false) as escalates,
  i.internal_ref,
  i.external_ref,
  b.created_at
from accounting.breaks b
join accounting.reconciliation_items i
  on i.reconciliation_item_id = b.reconciliation_item_id
join accounting.reconciliations r
  on r.reconciliation_id = i.reconciliation_id
where b.status in ('OPEN', 'INVESTIGATING');

grant select on api.portfolio_snapshot_latest, api.position_holdings,
  api.ledger_balances, api.open_breaks to authenticated;

-- security_invoker 뷰라 원본 테이블 권한이 따로 필요하다.
grant select on accounting.portfolio_snapshots, accounting.journals,
  accounting.journal_lines, accounting.ledger_accounts, accounting.breaks,
  accounting.reconciliations, accounting.reconciliation_items to authenticated;
grant select on reference.instrument_symbols to authenticated;

commit;
