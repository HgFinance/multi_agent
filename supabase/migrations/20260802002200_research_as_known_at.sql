begin;

-- research: as_known_at - "그 판단을 내릴 때 무엇까지 알 수 있었는가"
--
-- 소유: 재일 (리서치본부)
-- 근거: WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md 7.2(시간과 데이터 계약) 및 6.2 P0 1항 -
--       event_time / received_at / observed_at / ingested_at / **as_known_at** 의
--       의미와 순서 불변식.
--
-- 왜 필요한가
--   우리는 지금 published_at / observed_at / vintage_date 셋을 쓴다. 5시각 모형 중
--   as_known_at 만 **질의 파라미터로만 존재하고 어디에도 저장되지 않는다.**
--   Evidence 조회는 as_of 를 안 주면 now() 가 기본이라 실행 시각이 곧 컷오프인데,
--   그 값이 기록되지 않으니 며칠 뒤 같은 trace_id 를 재실행하면 **다른 증거 집합**
--   을 보고도 같은 실행으로 보인다. 판단을 재현할 수 없다는 뜻이다.
--
--   Packet 의 주장을 사후 채점하는 선순환에서 이건 특히 아프다 - "그때 알았던
--   것으로 그렇게 판단한 게 맞았나"를 물어야 하는데 '그때 알았던 것'의 경계가
--   기록에 없으면 채점이 사후확신(hindsight)으로 오염된다.
--
-- 무엇을 하고 무엇을 안 하는가
--   이 마이그레이션은 **컷오프를 기록**한다. 모든 수집 경로를 그 시각으로
--   파라미터화하는 것(완전한 PIT 재현)은 별개 작업이다 - 분석가 일부는 아직
--   as_of 를 받지 않고 최신을 본다. 기록이 먼저다: 기록이 없으면 무엇이
--   파라미터화됐는지조차 확인할 수 없다.
--
-- 되돌리기: alter table ... drop column as_known_at;

alter table research.pipeline_runs
  add column if not exists as_known_at timestamptz;

comment on column research.pipeline_runs.as_known_at is
  '이 실행의 증거 컷오프. 이 시각 이후에 관측된 증거는 판단에 쓰이지 않았어야 한다. '
  'NULL 은 이 컬럼 도입(2026-08-02) 이전 실행이다 - 소급해 채우지 않는다.';

alter table research.packet_claims
  add column if not exists as_known_at timestamptz;

comment on column research.packet_claims.as_known_at is
  '이 주장을 발행할 때의 증거 컷오프(pipeline_runs.as_known_at 과 같은 값). '
  '사후 채점이 hindsight 로 오염되지 않았는지 확인하는 기준이다.';

-- 채점 결과와 나란히 볼 수 있게 - "언제까지 알고 한 판단이 어떻게 됐나"
create index if not exists packet_claims_as_known_idx
  on research.packet_claims (as_known_at desc nulls last);

commit;
