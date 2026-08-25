begin;

-- workforce.probation_periods 에 writer 를 붙이면서 필요한 제약.
--
-- 같은 Agent 에 **열린 수습은 하나뿐**이어야 한다. 둘이 동시에 열려 있으면 어느
-- 기준(success_metrics)으로 판정할지가 정해지지 않는다 - probation.py 불변식 1
-- ("Pass/Fail 기준은 관찰 전에 고정한다", AGENT_EMPLOYEE_PROFILES HR-03)이 의미를
-- 가지려면 그 기준이 하나여야 한다.
--
-- 이건 행 하나만 보는 DDL check 로는 못 막는다. 앱에서 select-then-insert 로 확인할
-- 수는 있지만, 열린 수습이 없을 때는 잠글 행이 없어서 `for update` 가 동시 요청 둘을
-- 막지 못한다 - 부분 unique index 라야 실제로 막힌다.
--
-- 종료된 수습(ended_at is not null)은 여러 건이 정상이다: EXTENDED 로 닫고 새 기간을
-- 여는 것이 정상 경로이므로 한 Agent 에 종료된 수습이 여러 개 쌓인다.

create unique index if not exists probation_periods_one_open_per_agent_uk
  on workforce.probation_periods (agent_id)
  where ended_at is null;

commit;
