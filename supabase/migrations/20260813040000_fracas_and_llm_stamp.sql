begin;

-- FRACAS 폐루프 + LLM 시점 스탬프 (2026-08-13, 전부 additive·nullable)
--
-- ① FRACAS (미 해군 MIL-STD-2155 폐루프를 환류에 이식):
--    REJECT 는 {근본원인, 시정조치, 검증 창} 없이 닫히지 않는다.
--    NASA LLIS 20년 사문화의 교훈대로 별도 시스템이 아니라 원장 컬럼으로만
--    존재한다 - 브리핑 생성·접수 게이트가 이 컬럼을 직접 읽는다.
-- ② LLM 시점 스탬프 (관문 9조항 후보의 계측 기초):
--    가설을 쓴 모델과 지식 컷오프를 기획안에 각인한다. 컷오프 이전/이후
--    이중 성적표의 전제 조건. 판정 규칙 변경은 아니다(그건 CEO 승인 사안).

alter table research.experiment_outcomes
  add column if not exists root_cause text,
  add column if not exists corrective_action text,
  add column if not exists verification_state text not null default 'OPEN',
  add column if not exists verification_note text;

comment on column research.experiment_outcomes.root_cause is
  'FRACAS 근본원인 분류: 데이터_결함/가설_논리/표본_설계/실행_결함/순수_무알파';
comment on column research.experiment_outcomes.verification_state is
  'OPEN(시정조치 미검증) / VERIFIED(유사 계열에서 재발 없음 확인) / RECURRED(재발 - 루프 재개)';

alter table research.experiment_proposals
  add column if not exists llm_model_id text,
  add column if not exists llm_training_cutoff date;

commit;
