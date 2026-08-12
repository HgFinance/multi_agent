begin;

-- 담당자: 재일 (리서치본부)
-- 근거: 재일님 질문 2026-08-02 "24시간 돌아가면서 작업을 수행할 수 있게 되나".
--       점검하다 어제 넣은 Job 2개(geopolitical, bluesky-watch)가 컨테이너에서
--       조용히 실패하고 있던 것을 발견했다(config 미마운트). 스케줄러가
--       `실패 exit=1` 을 로그에 남기긴 했지만 **아무도 안 보면 없는 것과 같다.**
--
-- 24시간 운영에서 가장 큰 위험은 실패가 아니라 **조용한 실패**다. 상태가
-- 스케줄러 메모리에만 있으면 컨테이너가 재시작될 때 함께 사라지고, 로그는
-- 사람이 열어봐야 보인다. 그래서 실행 결과를 DB 에 남겨 누구든(감사 리포트,
-- 대시보드, 팀원) 조회할 수 있게 한다.
--
-- 남기는 것은 좌표와 결과뿐이다 - 수집 데이터 자체는 각 수집기가 자기
-- 정규화 테이블에 넣는다(여기는 운영 기록층).

create table research.collector_runs (
  run_id uuid primary key default gen_random_uuid(),
  job_name text not null,
  argv text not null,                      -- 실제 실행 인자 (규약 위반 추적용)
  started_at timestamptz not null,
  ended_at timestamptz not null,
  duration_ms integer,
  exit_code integer not null,
  -- 수집기 종료 코드 규약: 0=OK, 2=SKIP(의도된 미수집), 그 외=실패.
  -- TIMEOUT 은 exit_code 를 못 받으므로 -1 로 기록한다.
  status text not null check (status in ('OK', 'SKIP', 'FAILED', 'TIMEOUT')),
  consecutive_failures integer not null default 0,
  output_tail text,                        -- 마지막 몇 줄 - 원인 파악용
  scheduler_version text,
  check (ended_at >= started_at)
);

create index collector_runs_job_time_idx
  on research.collector_runs (job_name, started_at desc);
-- 실패만 훑는 조회가 잦다 - 부분 인덱스로 가볍게
create index collector_runs_problem_idx
  on research.collector_runs (started_at desc)
  where status in ('FAILED', 'TIMEOUT');

-- 24시간 건강 상태. "지금 뭐가 고장나 있나"를 한 줄로 답한다.
-- SKIP 은 실패가 아니다(휴장 등 의도된 미수집) - 성공률에서 분리한다.
create view research.collector_health as
select
  job_name,
  count(*)                                             as runs_24h,
  count(*) filter (where status = 'OK')                as ok_24h,
  count(*) filter (where status = 'SKIP')              as skip_24h,
  count(*) filter (where status in ('FAILED','TIMEOUT')) as bad_24h,
  max(started_at) filter (where status = 'OK')         as last_ok_at,
  max(started_at)                                      as last_run_at,
  (array_agg(status order by started_at desc))[1]      as last_status,
  (array_agg(output_tail order by started_at desc) filter (
     where status in ('FAILED','TIMEOUT')))[1]         as last_error_tail
from research.collector_runs
where started_at > now() - interval '24 hours'
group by job_name;

alter table research.collector_runs enable row level security;

commit;
