-- 거래소 통합 1분봉.
--
-- market.bars_1m 은 (bucket_time, instrument_id, market) 로 묶여 한 종목의 같은
-- 분에 KRX 행과 NXT 행이 따로 나온다. 2026-08-26 실측: 하루 중복 버킷 33,083개,
-- 019170 의 경우 NXT 가 분당 거래량의 27~53% 를 차지하고 종가도 갈렸다
-- (03:16 기준 KRX 9,310 vs NXT 9,320).
--
-- 그래서 조건주문 지표를 bars_1m 위에서 바로 계산하면 두 거래소 봉이 번갈아
-- 섞여 SMA 가 틀어진다. CONDITIONAL_TRADING_RULE_ENGINE.md 도 "중복 또는 분 단위
-- 내부 공백이 있는 bucket 은 Indicator Engine 에서 사용하지 않는다" 고 못박는다.
--
-- 거래소를 KRX 로 필터해 버리면 안 된다 - subscription_plan.py 가 통합시세
-- (US3/UH1) 를 쓰는 이유가 정확히 이것이고, KRX 단독으로 돌아가면 NXT 가 통째로
-- 빠진다(2026-08-11 실측: 하루 체결의 25%). 그래서 봉을 사후에 합치지 않고
-- 틱 단계에서 묶는다 - first/last 가 event_time 을 보므로 거래소를 가로질러도
-- 시가/종가 순서가 정확하다.

create materialized view market.bars_1m_consolidated
with (timescaledb.continuous) as
select
  time_bucket(interval '1 minute', event_time) as bucket_time,
  instrument_id,
  first(price, event_time) as open,
  max(price) as high,
  min(price) as low,
  last(price, event_time) as close,
  sum(quantity) as volume,
  count(*) as trade_count,
  sum(price * quantity) as notional,
  case when sum(quantity) = 0 then null else sum(price * quantity) / sum(quantity) end as vwap,
  max(observed_at) as observed_at
from market.market_ticks
group by time_bucket(interval '1 minute', event_time), instrument_id
with no data;

-- bars_1m 과 같은 주기. end_offset 1 minute 이 곧 "확정봉" 경계다 - 진행 중인
-- 분은 실체화되지 않으므로 부분봉이 지표에 들어가지 않는다.
select add_continuous_aggregate_policy(
  'market.bars_1m_consolidated',
  start_offset => interval '2 days',
  end_offset => interval '1 minute',
  schedule_interval => interval '1 minute',
  if_not_exists => true
);

-- 001 의 bars_1m 과 같은 권한. 이게 빠지면 market-api 가 배포 직후
-- permission denied 로 떨어지고, 증상은 다시 "봉이 안 온다" 로만 보인다.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'market_reader') then
    grant select on market.bars_1m_consolidated to market_reader;
  end if;
  if exists (select 1 from pg_roles where rolname = 'hgfinance_runtime') then
    grant select on market.bars_1m_consolidated to hgfinance_runtime;
  end if;
end;
$$;

-- with no data 로 만들었으므로 정책이 처음 도는 1분 뒤까지 뷰가 비어 있고,
-- 정책은 start_offset(2일) 안쪽만 채운다. 조건주문이 배포 직후 평가되려면
-- 최초 실체화를 한 번 돌려야 한다. refresh_continuous_aggregate 는 트랜잭션
-- 블록에서 실행할 수 없어(마이그레이션 러너는 파일을 다중 명령 문자열로 보낸다)
-- 여기 넣지 않는다. 마이그레이션 적용 후 별도 세션에서 한 번 실행한다:
--
--   call refresh_continuous_aggregate(
--     'market.bars_1m_consolidated', now() - interval '2 days', now());
