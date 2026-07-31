begin;
-- Supabase 대시보드용 public View (dash_*)
--
-- 담당: 재일 (리서치/퀀트)
-- 근거: 재일님 요구(2026-07-31) "대시보드에 보이게 해줘" - Table Editor 기본
--       화면이 public 스키마라 research/reference 스키마 테이블이 첫 화면에
--       안 보인다. 원본을 옮기지 않고 **읽기 전용 View 만 public 에 비춘다.**
--
-- 보안: 전부 security_invoker - 호출자 권한으로 실행되므로 anon/authenticated
--       키가 PostgREST 로 이 View 를 읽으려 해도 원본 스키마 권한이 없어
--       거부된다. 대시보드(postgres 역할)만 본다. 원본 계약은 그대로다.

-- 1) 파이프라인 상태판 - "수집기 -> DB 적재가 살아 있나" 를 한 눈에
create or replace view public.dash_pipeline_status
with (security_invoker = true)
as
select s.source_code                                        as source,
       count(*)                                             as total_rows,
       count(*) filter (where d.observed_at::date = (now() at time zone 'Asia/Seoul')::date)
                                                            as today_rows,
       count(*) filter (where d.observed_at > now() - interval '60 minutes')
                                                            as last_hour_rows,
       to_char(max(d.observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI') as last_observed_kst
from research.documents d
join reference.data_sources s using (source_id)
group by s.source_code
union all
select 'financial_facts', count(*),
       count(*) filter (where observed_at::date = (now() at time zone 'Asia/Seoul')::date),
       count(*) filter (where observed_at > now() - interval '60 minutes'),
       to_char(max(observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI')
from research.financial_facts
union all
select 'macro_observations', count(*),
       count(*) filter (where observed_at::date = (now() at time zone 'Asia/Seoul')::date),
       count(*) filter (where observed_at > now() - interval '60 minutes'),
       to_char(max(observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI')
from research.macro_observations
union all
select 'corporate_actions', count(*),
       count(*) filter (where created_at::date = (now() at time zone 'Asia/Seoul')::date),
       count(*) filter (where created_at > now() - interval '60 minutes'),
       to_char(max(created_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI')
from reference.corporate_actions;

-- 2) 최신 뉴스 (종목 연결·시간감쇠 가중치 포함)
create or replace view public.dash_recent_news
with (security_invoker = true)
as
select symbol, title, source_code, relation_type, confidence, weight,
       published_at at time zone 'Asia/Seoul' as published_kst,
       observed_at  at time zone 'Asia/Seoul' as observed_kst,
       canonical_url
from research.news_recent_weighted
order by published_at desc
limit 500;

-- 3) 최신 공시
create or replace view public.dash_recent_disclosures
with (security_invoker = true)
as
select d.title,
       iss.legal_name                                   as issuer,
       d.status,
       d.published_at at time zone 'Asia/Seoul'         as published_kst,
       d.observed_at  at time zone 'Asia/Seoul'         as observed_kst,
       d.canonical_url
from research.documents d
join reference.data_sources s using (source_id)
left join reference.issuers iss using (issuer_id)
where s.source_code = 'opendart'
order by d.observed_at desc
limit 500;

-- 4) 발행사 (기업개황 보강 결과)
create or replace view public.dash_issuers
with (security_invoker = true)
as
select iss.corp_code, iss.legal_name, iss.industry_code, iss.fiscal_month,
       iss.metadata->>'homepage_url' as homepage,
       iss.metadata->>'ir_url'       as ir_url,
       iss.created_at at time zone 'Asia/Seoul' as registered_kst
from reference.issuers iss
order by iss.created_at desc;

-- 5) 뉴스 수집 지연 상태판 (시간대별 p50/p95)
create or replace view public.dash_news_latency
with (security_invoker = true)
as
select * from research.news_ingest_latency_hourly
order by observed_hour desc
limit 168;

commit;
