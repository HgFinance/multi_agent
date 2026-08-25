begin;

-- P1-2 HR-04 후속: workforce.capacity_snapshots 에 writer 를 붙이면서 함께 필요한 두 가지.
-- cost_snapshots writer(20260825000100)와 같은 이유, 같은 모양이다.
--
-- 1) recorded_by - capacity 의 수치(arrivals/큐 지연/재시도율/오류율/가동률)도 인사팀이
--    집계하는 값이 아니라 플랫폼/인프라 계측이 **보고하는** 값이다(cost.py 클래스
--    주석의 F27 담당 분리와 동일: "인사팀은 귀속·Scorecard·권고만 한다"). 누가
--    보고했는지가 없으면 인사팀이 지어낸 값과 구별할 수 없다. writer 가 없어
--    지금까지 0건이므로(cost_snapshots 와 동일하게 확인) 기본값 없이 바로 not null 로
--    추가한다.
--
-- 2) (department_id, agent_id, window_start, window_end) unique -
--    get_capacity_snapshot 은 창 안에서 window_end 가 가장 늦은 행 1개를 고른다. 같은
--    창을 다시 보고했을 때 새 행이 쌓이면 어느 쪽이 최신인지 window_end 동률로
--    모호해지고, 재보고 이력이 무한히 늘어난다 - cost_snapshots 처럼 재보고는 새 행이
--    아니라 갱신이어야 한다.
--
--    cost_snapshots 와 달리 department_id/agent_id 는 **둘 중 하나만 있어도** 된다
--    (DDL check: department_id is not null or agent_id is not null) - 부서 단위 보고와
--    Agent 단위 보고가 공존한다. 일반 unique index 는 null 을 서로 다른 값으로 봐서
--    (department_id, agent_id) = (D1, null) 인 행이 같은 창에 여러 번 들어가도 막지
--    못한다. `nulls not distinct`(foundation_reference.sql 의 corp_code/isin 과 같은
--    관용구, PG15+)를 써서 null 도 같은 값으로 취급해야 부서 단위 재보고와 Agent 단위
--    재보고가 각각 제대로 막힌다.

alter table workforce.capacity_snapshots
  add column if not exists recorded_by text not null;

create unique index if not exists capacity_snapshots_dept_agent_window_uk
  on workforce.capacity_snapshots (department_id, agent_id, window_start, window_end)
  nulls not distinct;

commit;
