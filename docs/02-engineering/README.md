# Engineering 문서 안내

> 기준일: 2026-08-17

이 폴더는 모든 설계 초안을 모아두는 곳이 아니라, 현재 구현·계약·운영 계획을 찾는 진입점이다. 제품의 최상위 기준은 [`HEDGE_FUND_MASTER_PLAN.md`](../HEDGE_FUND_MASTER_PLAN.md)이며, 현재 로컬 Compose 서비스의 기준은 [`docker-compose.yml`](../../docker-compose.yml)과 [LOCAL_COMPOSE_RUNTIME_BASELINE.md](LOCAL_COMPOSE_RUNTIME_BASELINE.md)다.

## 현재 구현 스냅샷

- [AS-IS Runtime Blueprint](AS_IS_RUNTIME_BLUEPRINT_2026-08-17.md) — `5c85168b` 기준 코드·Compose·DB migration 역추적 감사
- [쉬운 해설판](AS_IS_RUNTIME_BLUEPRINT_EASY_2026-08-17.md) — 비개발자·운영자용 요약

## 먼저 읽을 문서

1. [FINAL_RUNTIME_ARCHITECTURE.md](FINAL_RUNTIME_ARCHITECTURE.md) — 전체 Runtime 설계 기준
2. [LOCAL_COMPOSE_RUNTIME_BASELINE.md](LOCAL_COMPOSE_RUNTIME_BASELINE.md) — 현재 로컬 Compose 서비스·Profile·포트
3. [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md) — 부서 Backend와 Docker 이행 계획
4. [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md) — Worker 역할·trigger·권한의 Source of Truth
5. [WORKER_MODEL_MATRIX.md](WORKER_MODEL_MATRIX.md) — Head·Worker·결정론 Runner 모델 배치
6. [contracts/README.md](contracts/README.md) — 공통 JSON Contract

## 계약·API·실행 경계

- [UNIFIED_DOMAIN_API_SPEC.md](UNIFIED_DOMAIN_API_SPEC.md) — 부서 API·Event 통합 계약
- [USER_INPUT_API_SPEC.md](USER_INPUT_API_SPEC.md) — Mandate/Investor Profile 온보딩 API
- [MAS_PIPELINE_CONTRACTS.md](MAS_PIPELINE_CONTRACTS.md) — 부서 선택·fan-out/fan-in·Replay 계약
- [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md) — Hermes·Worker Graph 구조
- [REPOSITORY_DEPARTMENT_STRUCTURE.md](REPOSITORY_DEPARTMENT_STRUCTURE.md) — 저장소·부서 경계

## 운영 Runbook

- [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md) — Hermes Profile·Container 운영
- [RISK_QA_DOCKER_RUNBOOK.md](RISK_QA_DOCKER_RUNBOOK.md) — Risk·QA Compose 운영
- [RISK_QA_TEST_PRODUCTION_PIPELINE.md](RISK_QA_TEST_PRODUCTION_PIPELINE.md) — Risk·QA TEST/PRODUCTION OFF Gate
- [RISK_MANDATE_WORKER_FLOW.md](RISK_MANDATE_WORKER_FLOW.md) — Risk Worker 실행 계약
- [LOCAL_PAPER_RUNTIME.md](LOCAL_PAPER_RUNTIME.md) — 고정 데모 ID·LS PAPER 로컬 실행 기준
- [SUPABASE_READONLY_PORTFOLIO_ADAPTER.md](SUPABASE_READONLY_PORTFOLIO_ADAPTER.md) — Portfolio Read-only 데이터 경계

## Research·Quant·Model

- [RESEARCH_QUANT_AGENTIC_FRAMEWORK.md](RESEARCH_QUANT_AGENTIC_FRAMEWORK.md) — Research-to-Strategy Framework
- [RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md](RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md) — Research 산출물 품질·승격
- [INVESTMENT_DOCTRINE_MODEL_FACTORY.md](INVESTMENT_DOCTRINE_MODEL_FACTORY.md) — 조건부 Doctrine Model Factory
- [OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md](OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md) — 로컬 Worker Model Build/Eval
- [WORKER_SKILL_REGISTRY.md](WORKER_SKILL_REGISTRY.md) — Risk·QA Skill 제안·권한 경계

## 결정 기록·보조 설계

- [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) — 기술 선택과 도입 조건
- [HEDGE_FUND_IMPLEMENTATION_BACKLOG.md](HEDGE_FUND_IMPLEMENTATION_BACKLOG.md) — 구현 Backlog
- [AI_OFFICE_FRONTEND_PLAN.md](AI_OFFICE_FRONTEND_PLAN.md) — Operator Frontend
- [adr/](adr/) — Accepted/Proposed Architecture Decision Record
- [contracts/](contracts/) — 기계 검증용 JSON Schema/Registry

문서가 현재 서비스 존재 여부나 실행 상태를 단정하면 Compose 기준선과 [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md)를 함께 확인한다. 문서에 없는 경로를 새로 만들거나, 과거 실험 문서를 현재 Runtime 계약으로 승격하지 않는다.
