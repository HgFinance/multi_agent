# HgFinance Documentation Portal

> **상태:** CURRENT PORTAL
> 이 파일은 문서 위치와 읽는 순서만 안내한다. 현재 구조·구현 상태·실행 계약을
> 다시 서술하지 않는다.

HgFinance는 Research, Quant, Trading, Risk, Accounting, QA, CEO와 Agent
Workforce를 분리한 개인형 멀티에이전트 금융 연구·PAPER 운용 시스템이다. 실제
자금 주문, LIVE 주문, 외부 사용자 Auth·로그인·가입·세션은 현재 범위가 아니다.

## 먼저 읽을 문서

| 질문 | 정본 |
|---|---|
| 지금 시스템 구조와 실행 흐름은 무엇인가 | [Current Architecture](CURRENT_PROJECT_ARCHITECTURE.md) |
| 무엇이 구현·검증됐고 무엇이 남았는가 | [Implementation Status](PROJECT_IMPLEMENTATION_STATUS.md) |
| Hermes·Worker·Runner·Gateway·Gate의 상세 실행 계약은 무엇인가 | [Final Runtime Architecture](02-engineering/FINAL_RUNTIME_ARCHITECTURE.md) |
| Worker 역할·권한·활성 편제는 무엇인가 | [Worker Role Boundaries](02-engineering/WORKER_ROLE_BOUNDARIES.md) |
| Head/Worker 모델과 fallback은 무엇인가 | [Worker Model Matrix](02-engineering/WORKER_MODEL_MATRIX.md) |
| Domain API·Event·BFF 경계는 무엇인가 | [Unified Domain API Specification](02-engineering/UNIFIED_DOMAIN_API_SPEC.md), [Contracts](02-engineering/contracts/README.md) |
| 제품 목표와 장기 범위는 무엇인가 | [Hedge Fund Master Plan](HEDGE_FUND_MASTER_PLAN.md) |
| 문서 상태·배치·충돌은 어떻게 처리하는가 | [Documentation Governance](DOCUMENTATION_GOVERNANCE.md) |
| 과거 시점의 감사·구조는 어디에 있는가 | [Archive](archive/README.md) |

현재 구현을 설명할 때는 코드·Compose·migration·registry·test를 먼저 보고,
위 표의 정본을 보조 설명으로 사용한다. 날짜·commit에 고정된 문서와 ADR은 현재
상태를 덮어쓰지 않는다.

## 영역별 문서

### 제품과 운영 범위

- [Core Plan](01-product/HEDGE_FUND_CORE_PLAN.md)
- [Minimum Service Unit](01-product/MINIMUM_SERVICE_UNIT_SPEC.md)
- [Advancement Roadmap](01-product/WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md)
- [Implementation Backlog](02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)

### 실행·배포·모델

- [Engineering Index](02-engineering/README.md)
- [Local Compose Runtime Baseline](02-engineering/LOCAL_COMPOSE_RUNTIME_BASELINE.md)
- [Hermes Docker Runbook](02-engineering/HERMES_DOCKER_RUNBOOK.md)
- [Research Worker AWS Runbook](02-engineering/RESEARCH_WORKER_AWS_RUNBOOK.md)
- [Technology Decisions](02-engineering/TECH_STACK_DECISIONS.md)

### Research·Quant·데이터

- [Research–Quant Agentic Framework](02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)
- [Research Output Strategy](02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md)
- [MCP On-demand Architecture](02-engineering/MCP_ONDEMAND_ARCHITECTURE.md)
- [Data Governance Guide](03-data/DATA_GOVERNANCE_GUIDE.md)
- [Database Index](database/README.md)

### Trading·Risk·Accounting·QA

- [Trading/Accounting Team Guide](05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md)
- [Risk/QA Team Guide](05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md)
- [User PAPER Authority ADR](02-engineering/adr/0007-authenticated-user-paper-directive-authority.md)
- [Risk Mandate Worker Flow](02-engineering/RISK_MANDATE_WORKER_FLOW.md)

### UI·CEO·조직

- [AI Office Frontend](02-engineering/AI_OFFICE_FRONTEND_PLAN.md)
- [Discord/Web CEO Mirroring](02-engineering/DISCORD_WEB_CEO_MIRRORING.md)
- [CEO/HR Role Classification](02-engineering/CEO_HR_AGENT_ROLE_CLASSIFICATION.md)
- [Agent Employee Profiles](04-organization/AGENT_EMPLOYEE_PROFILES.md)

### 외부 연동 참조

- [LS Open API](06-integrations/ls-openapi/README.md)
- [OpenDART](06-integrations/opendart/README.md)
- [KRX Data Marketplace](06-integrations/krx-openapi/README.md)
- [SerpApi](06-integrations/serpapi/README.md)

## 개발 진입점

1. 저장소 공통 규칙은 루트 `AGENTS.md`와 담당 부서 README를 먼저 읽는다.
2. 현재 서비스 목록은 문서의 숫자를 복사하지 말고 `docker compose config --services`로 확인한다.
3. 실행 역할은 Worker registry와 `hermes/config.yaml`에서 확인한다.
4. API는 route registry와 각 FastAPI `openapi()` 계약을 확인한다.
5. 변경 후 담당 계약 테스트와 문서 링크 검사를 함께 실행한다.

문서에 없는 구현 여부를 추정하지 않는다. Compose 선언은 실행 증거가 아니며,
외부 API·DB·GPU·Broker 상태는 실제 runtime 관측이 있을 때만
`RUNTIME_VERIFIED`로 기록한다.
