begin;

-- 이관 구간에서 "수신 시각을 재지 않았다" 를 표현할 수 있게 한다.
--
-- received_at 이 NOT NULL 이라 외부 이관 행을 넣을 수 없었다. 그런데 저쪽 원본에는
-- 수신 시각이 아예 없다 - 넣을 값이 없는 것이지 0 인 것이 아니다. 여기서 아무 값이나
-- 채우면(예: event_time 그대로) **지연 0 으로 측정된 것처럼 읽힌다.** 그건 우리가
-- 다른 데서 계속 경계해 온 "미측정을 0 으로 채우기" 와 같은 사고다.
--
-- 그렇다고 제약을 그냥 없애면 우리 수집기의 누락도 함께 통과한다. 그래서 제약을
-- **조건부로** 바꾼다: 이관 행(provider='LS-IMPORT')만 NULL 을 허용하고, 우리가
-- 직접 수집한 행은 여전히 반드시 있어야 한다.
alter table market.market_ticks  alter column received_at drop not null;
alter table market.market_quotes alter column received_at drop not null;

alter table market.market_ticks
  add constraint market_ticks_received_at_required_when_measured
  check (provider = 'LS-IMPORT' or received_at is not null) not valid;

alter table market.market_quotes
  add constraint market_quotes_received_at_required_when_measured
  check (provider = 'LS-IMPORT' or received_at is not null) not valid;

-- NOT VALID 로 둔다. **신규 INSERT 는 그래도 검사되므로** 이관·수집 양쪽에
-- 대한 보호는 지금부터 유효하다. 기존 행은 방금 전까지 NOT NULL 이었으므로
-- 전부 조건을 만족한다 - 검증은 기록상의 의미뿐이다.
--
-- market_quotes 는 columnstore(압축)가 켜져 있어 validate 자체가 지원되지 않는다
-- (2026-08-10 실측: "operation not supported on hypertables that have columnstore
-- enabled"). 압축을 풀었다 다시 거는 비용을 치를 이유가 없어 NOT VALID 로 남긴다.

comment on constraint market_ticks_received_at_required_when_measured
  on market.market_ticks is
  '우리가 수집한 행은 수신 시각이 반드시 있다. 이관 행만 NULL 허용 - 원본에 없는 값을 지어내지 않기 위해';

commit;
