-- 최신 기사 가중치 View - 실시간 분석 입력용
--
-- 담당: 재일 (리서치/퀀트)
-- 근거: 재일님 지시(2026-07-31) "장 상관없이 실시간 수집 + 실시간 분석 + 최신기사 가중치"
--
-- 가중치는 지수 시간감쇠다: weight = 2^(-age_hours / half_life_hours)
--   반감기 6시간 - 오늘 아침 기사는 오후에 절반, 어제 기사는 ~6%.
--   고정 상수를 View 에 굽지 않고 age_hours 를 함께 내보내므로, 소비자가 다른
--   반감기가 필요하면 2^(-age_hours/H) 로 직접 다시 계산하면 된다.
--
-- ⚠ PIT 규칙 (백테스트가 이 View 를 흉내낼 때):
--   weight 의 기준은 published_at(사건 시각)이지만, **그 기사가 보였는지는
--   observed_at 으로 가려야 한다** - 게시 후 관측까지 5~9분(NAVER 색인+폴링)의
--   공백이 있고, published_at 기준 재생은 그 구간을 미래 정보로 앞당긴다.
--   실시간 소비자는 이미 관측된 것만 보므로 그냥 쓰면 된다.
--
-- 사용 예 (지난 24시간, 종목별 가중 기사):
--   select * from research.news_recent_weighted
--   where symbol = '005930' and age_hours < 24 order by weight desc limit 20;

create or replace view research.news_recent_weighted
with (security_invoker = true)
as
select
  d.document_id,
  s.source_code,
  d.title,
  d.canonical_url,
  d.published_at,
  d.observed_at,
  di.instrument_id,
  isym.symbol,
  di.relation_type,
  di.confidence,
  round((extract(epoch from (now() - d.published_at)) / 3600.0)::numeric, 2) as age_hours,
  round(
    power(2.0, - extract(epoch from (now() - d.published_at)) / 3600.0 / 6.0)::numeric,
    4
  ) as weight,
  round(
    (di.confidence
     * power(2.0, - extract(epoch from (now() - d.published_at)) / 3600.0 / 6.0))::numeric,
    4
  ) as weighted_confidence
from research.documents d
join reference.data_sources s using (source_id)
join research.document_instruments di using (document_id)
-- is_primary 가드: 지금은 instrument 당 symbol 이 1행이지만 Schema 는 표준코드 등
-- 복수 행을 허용한다 - 그때 이 View 가 기사를 곱절로 부풀리면 안 된다.
join reference.instrument_symbols isym
  on isym.instrument_id = di.instrument_id and isym.is_primary
where d.document_type = 'NEWS'
  and d.status = 'ACTIVE'
  and d.published_at is not null
  and d.published_at <= now()   -- 미래로 찍힌 기사(Provider 시계 이상)는 빼고 센다
  and d.published_at > now() - interval '7 days';
