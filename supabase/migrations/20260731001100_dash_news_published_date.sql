begin;
-- dash_recent_news 강화 - 기사 게재일을 바로 확인할 수 있게 (재일님 요구 2026-07-31)
--
-- 담당: 재일 (리서치/퀀트)
-- 변경: 게재일(published_date)·게재시각(published_time)을 별도 컬럼으로 분리해
--       관측시각과 나란히 대조 가능하게 한다. 소스·URL·신뢰도 포함.
--       원본 View 를 고치지 않고 create or replace 로 대체한다(append 규약).
-- 주의: LS 뉴스는 URL 이 없다(realkey 가 좌표) - url 이 NULL 인 것은 결측이
--       아니라 소스 특성이다.

-- 컬럼 구성이 바뀌므로 replace 가 아니라 drop 후 재생성한다 (View 라 데이터 무손실)
drop view if exists public.dash_recent_news;

create view public.dash_recent_news
with (security_invoker = true)
as
select symbol,
       title,
       (published_at at time zone 'Asia/Seoul')::date               as published_date,
       to_char(published_at at time zone 'Asia/Seoul', 'HH24:MI:SS') as published_time,
       to_char(observed_at  at time zone 'Asia/Seoul', 'MM-DD HH24:MI') as observed_kst,
       source_code,
       relation_type,
       confidence,
       weight,
       canonical_url
from research.news_recent_weighted
order by published_at desc
limit 500;

commit;
