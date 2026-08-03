-- workforce.departments.department_code 표기 통일 — Hermes Profile 이름
--
-- 소유: 영주 (CEO Office / Agent Workforce 인사팀)
-- 근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.2절
--   ("부서 식별자 표기(2026-08-04 확정)" 주석)
--
-- ## 왜 필요한가
--
-- 이 컬럼에 두 가지 이름 체계가 섞여 있었다(2026-08-04 실측).
--   'AGENT-WORKFORCE'  <- 영주가 넣음. API 스펙 2.2의 owner_department 예시 표기를 따랐다.
--   'risk-management'  <- 동규가 넣음(20260802001600_risk_qa_runtime_registration.sql).
--   'qa-department'       Hermes Profile 이름을 따랐다.
-- 둘 다 자기가 보던 문서를 따른 것이라 어느 쪽도 실수가 아니다 - 스펙과 Profile이 서로 다른
-- 어휘를 쓰는 게 원인이었다.
--
-- ## 왜 Profile 이름 쪽으로 통일하는가
--
-- 실제 코드 의존이 압도적으로 Profile 이름에 있다(2026-08-04 집계).
--   Profile 이름: 40개 파일 - ai-office/app/ops/riskQaBridge.ts, apps/api/main.py,
--     departments/03-risk/harness/{core,journal}.py, departments/06-ai-qa-audit/*, tests,
--     departments/02-trading/hermes/config.yaml, 등록 마이그레이션
--   대문자 표기: 8곳이며 전부 CEO Office가 2026-08-03~04에 새로 쓴 코드와 이 seed 행
-- 즉 대문자 표기를 쓰는 기존 코드는 없었다. 다수 쪽으로 스펙 문서를 맞추고 이 행을 옮긴다.
--
-- ## 안전성
--
-- workforce.agent_profiles 등은 department_id(uuid) FK로 연결돼 있고 department_code를
-- 참조하지 않는다 - 코드 문자열만 바뀌므로 FK가 깨지지 않는다. 대상은 1행이다.
-- 멱등하다: 이미 'hr-department'면 아무 일도 하지 않는다.
--
-- 폴더 이름(`03-risk`, `07-agent-workforce`)은 세 번째 체계이며 경로 전용이다 - 데이터
-- 식별자로 쓰지 않는다.

begin;

update workforce.departments
   set department_code = 'hr-department', updated_at = now()
 where department_code = 'AGENT-WORKFORCE';

comment on column workforce.departments.department_code is
  '부서 식별자. Hermes Profile 이름을 쓴다: ceo-agent, research-department, '
  'trading-department, risk-management, quant-backtest-department, '
  'accounting-portfolio-department, qa-department, hr-department. '
  '폴더 이름(03-risk 등)은 경로 전용이며 여기 쓰지 않는다 (2026-08-04 확정).';

commit;
