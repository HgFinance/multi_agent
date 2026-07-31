begin;

-- 담당자: 동규 (AI QA/감사본부)
-- 근거: departments/06-ai-qa-audit/hermes/config.yaml `implementation.not_started`
--       ("audit.qa_decisions에 calculation_version/input_hash 실제 컬럼 추가 - 스키마
--       변경(DDL)이라 별도 Migration PR 필요"), risk.risk_decisions(같은 파일 392행)의
--       calculation_version/input_hash 컬럼과 같은 재현성 원칙.
--
-- evidence_qa_engine.py의 QaAssessment는 이미 calculation_version(checker_version)과
-- input_hash(_context_hash)를 계산해 갖고 있지만(같은 파일 245-247행 docstring), 원래
-- audit.qa_decisions Table엔 이 두 컬럼이 없어 conditions jsonb 안에 우회해 넣어야 했다.
-- 이 Table엔 아직 아무 코드도 쓰지 않는다(audit.qa_decisions에 INSERT하는 경로가 없다)
-- - 항상 비어 있으므로 not null로 바로 추가해도 안전하고, risk.risk_decisions와 같은
-- 모양을 유지한다.
alter table audit.qa_decisions
  add column calculation_version text not null,
  add column input_hash text not null;

commit;
