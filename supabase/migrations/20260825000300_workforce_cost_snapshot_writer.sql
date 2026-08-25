begin;

-- P1-2 HR-04: workforce.cost_snapshots 에 writer 를 붙이면서 함께 필요한 두 가지.
--
-- 1) recorded_by - 형제 테이블 improvement_candidate_scorecards(P1-1)는 처음부터,
--    quality_snapshots 는 20260806000200 에서 가진 컬럼이다. cost_snapshots 의 수치는
--    인사팀이 집계하는 값이 아니라 플랫폼/인프라의 과금 계측이 **보고하는** 값이라
--    (cost.py F27 담당 분리: "플랫폼 = 토큰 측정·과금, 인사팀 = 귀속·Scorecard·권고"),
--    누가 보고했는지가 없으면 "인사팀이 자기 비용 수치를 지어내지 않았다"를 감사로
--    확인할 수 없다. writer 가 없어 지금까지 0건이므로 quality_snapshots 와 같이
--    기본값 없이 바로 not null 로 추가한다.
--
-- 2) (agent_id, profile_version_id, window_start, window_end) unique -
--    reader(list_cost_snapshots_by_agent/by_department)가 창 안의 행을 **합산**한다.
--    같은 창을 두 번 보고하면 사용량이 조용히 두 배가 되고 예산 판정이 OK -> EXCEEDED 로
--    뒤집힌다. performance_reviews 가 (agent_id, profile_version_id, period_start,
--    period_end)로 이미 쓰는 것과 같은 키다 - 재보고는 새 행이 아니라 갱신이어야 한다.

alter table workforce.cost_snapshots
  add column if not exists recorded_by text not null;

create unique index if not exists cost_snapshots_agent_version_window_uk
  on workforce.cost_snapshots (agent_id, profile_version_id, window_start, window_end);

commit;
