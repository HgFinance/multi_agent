-- 뉴스 수집 지연 관측 View
--
-- 담당: 재일 (리서치/퀀트)
-- 근거: 재일님 요구(2026-07-31) "기사 업로드한 시간 측정 - DB에서 적재 시간 확인할 수 있어야 함"
--       TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 4.2 (observed_at 기준 판단)
--
-- 세 시각은 이미 research.documents 가 끝까지 보존한다 (upsert 가 덮지 않는다):
--   published_at  기사 게시 시각 (Provider 가 준 값 - 최초 관측본 유지)
--   observed_at   수집기가 처음 본 시각 (수집기가 페이지 수신 시점에 찍는다)
--   created_at    DB 최초 적재 시각 (default now(), on conflict 에서 미변경)
--
-- 이 View 는 그 세 시각의 차이를 바로 조회할 수 있게 한다:
--   detect_lag = observed_at - published_at   게시→관측 (폴링 주기 + Provider 색인 지연)
--   ingest_lag = created_at  - observed_at    관측→적재 (Sink 배치 지연)
--   total_lag  = created_at  - published_at   게시→적재 (전체)
--
-- 해석 주의 (읽는 사람이 반드시 알아야 하는 것):
--   1. 백필은 지연이 아니다. 수집기를 처음 켜거나 --symbols 로 과거分을 훑으면
--      detect_lag 가 수 시간~수 일로 나온다. 상주 수집(Watch Mode)의 정상 지연은
--      detect_lag 가 폴링 주기 근처로 수렴한 뒤부터 읽는다.
--   2. published_at 은 Provider 가 준 값이라 신뢰 수준이 Source 마다 다르다.
--      미래로 찍힌 기사(detect_lag 음수)는 admit() 이 10분까지 허용한다 -
--      hourly View 의 future_skew 로 따로 센다.
--   3. document_type = 'NEWS' 만 본다. DART 공시는 published_at 이 날짜뿐이라
--      (접수 시각 없음 - 가이드 Sprint J2 PIT 한계) 지연 계산이 무의미하다.

create or replace view research.news_ingest_latency
with (security_invoker = true)
as
select
  d.document_id,
  s.source_code,
  d.external_id,
  d.title,
  d.published_at,
  d.observed_at,
  d.created_at,
  d.observed_at - d.published_at as detect_lag,
  d.created_at  - d.observed_at  as ingest_lag,
  d.created_at  - d.published_at as total_lag
from research.documents d
join reference.data_sources s using (source_id)
where d.document_type = 'NEWS'
  and d.published_at is not null;

-- Source별·시간대별 요약. 상주 수집기의 상태판 질의용:
--   select * from research.news_ingest_latency_hourly
--   where observed_hour > now() - interval '6 hours' order by observed_hour desc;
create or replace view research.news_ingest_latency_hourly
with (security_invoker = true)
as
select
  s.source_code,
  date_trunc('hour', d.observed_at) as observed_hour,
  count(*) as documents,
  count(*) filter (where d.published_at > d.observed_at) as future_skew,
  round(percentile_cont(0.5) within group
    (order by extract(epoch from d.observed_at - d.published_at))::numeric, 1) as detect_lag_p50_s,
  round(percentile_cont(0.95) within group
    (order by extract(epoch from d.observed_at - d.published_at))::numeric, 1) as detect_lag_p95_s,
  round(max(extract(epoch from d.observed_at - d.published_at))::numeric, 1) as detect_lag_max_s,
  round(percentile_cont(0.5) within group
    (order by extract(epoch from d.created_at - d.observed_at))::numeric, 1) as ingest_lag_p50_s,
  round(max(extract(epoch from d.created_at - d.observed_at))::numeric, 1) as ingest_lag_max_s
from research.documents d
join reference.data_sources s using (source_id)
where d.document_type = 'NEWS'
  and d.published_at is not null
group by s.source_code, date_trunc('hour', d.observed_at);
