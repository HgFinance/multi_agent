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
-- (2026-07-31 보정, 재일: 운영 DB 실측에서 qa_decisions 에 이미 1행이 있었다
--  - gate=research_trading_artifact, decision=FAIL, decided_by=svc_qa_evaluator,
--  07-30 05:27. "항상 비어 있다" 전제가 깨져 not null 즉시 추가는 적용 자체가
--  실패한다. 의도(재현성 컬럼 + not null 계약)는 그대로 두고, 컬럼 추가 →
--  기존 행 backfill → not null 승격의 3단으로만 바꾼다. 컬럼 도입 전 행은
--  재현 정보가 실제로 없으므로 'pre-001000-unknown' 으로 정직하게 표기한다
--  - conditions jsonb 에 값이 있으면 그걸 우선한다.)
alter table audit.qa_decisions
  add column calculation_version text,
  add column input_hash text;

update audit.qa_decisions
   set calculation_version = coalesce(conditions->>'calculation_version',
                                      'pre-001000-unknown'),
       input_hash          = coalesce(conditions->>'input_hash',
                                      'pre-001000-unknown')
 where calculation_version is null or input_hash is null;

alter table audit.qa_decisions
  alter column calculation_version set not null,
  alter column input_hash set not null;

commit;
