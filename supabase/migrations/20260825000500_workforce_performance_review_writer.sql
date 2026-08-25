begin;

-- workforce.performance_reviews / performance_actions 에 writer 를 붙이면서 함께
-- 필요한 두 가지. 두 테이블 다 DDL 만 있고 writer·reader 가 없어 지금까지 0건이므로
-- (cost_snapshots/capacity_snapshots 와 같은 상황) 기본값 없이 바로 제약을 건다.
--
-- 1) decision check - performance_reviews.decision 은 `text not null` 일 뿐 값 어휘가
--    제약돼 있지 않았다. 앱 계약(performance/review.py ReviewDecision)은 5개를 쓴다:
--    CONTINUE + performance_actions.action_type 의 4개(LEARNING/PIP/ROLE_CHANGE/
--    DEACTIVATION). 새 어휘를 지어낸 게 아니라 이미 옆 테이블 check 에 있던 것을
--    그대로 쓴 것이고, 여기에 "조치 없음" 한 칸만 더했다.
--    DDL 과 앱이 같은 규칙을 강제한다는 원칙은 improvement_candidates 가 이미 따르고
--    있다(candidate.py 머리말: "이 Pydantic 계약과 대응 테이블의 DDL check 제약은
--    같은 규칙을 강제한다"). 제약이 없으면 오타 한 번에 조회가 조용히 0건이 된다.
--
-- 2) reviewer - 형제 테이블은 전부 작성자 칸을 갖고 있다(quality_snapshots.recorded_by
--    20260806000200, cost_snapshots/capacity_snapshots.recorded_by 20260825000100/200,
--    improvement_candidates.author). performance_reviews 만 없었다.
--    이 표의 decision 은 역할 축소·비활성화 "제안"까지 담는다(AGENT_EMPLOYEE_PROFILES
--    HR-03). 누가 제안했는지 없이 남으면 자기 평가와 독립 평가를 감사로 구별할 수
--    없다 - CEO 승인 단계에서 그 구별이 필요하다.

alter table workforce.performance_reviews
  add column if not exists reviewer text not null;

alter table workforce.performance_reviews
  add constraint performance_reviews_decision_check
  check (decision in ('CONTINUE', 'LEARNING', 'PIP', 'ROLE_CHANGE', 'DEACTIVATION'));

commit;
