# Department-Oriented Repository Structure

직원 실행 계층과 부서 간 핸드오프는 [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)를 따른다.

> 문서 상태: Confirmed Target Structure v1.2
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)
> 적용 범위: CEO Office, CEO 직속 Agent Workforce 인사팀, 6개 투자 본부와 공통 Platform
> 현재 변경 범위: 11절 단계 1~3(Department Scaffold, Hermes Profile 이동, 본부 코드 이동) 완료.
> 임시 CLI 호환 Wrapper는 예정(2026-10-31)보다 일찍 제거됐다 — 구 경로는 더 이상 존재하지 않는다.
> 단계 4(DB Prototype 통합)와 단계 5(구조 Gate)는 아직 진행 전이다.
> 목적: 팀원이 자기 본부의 Agent, Service, Test와 운영 문서를 한 경계 안에서 관리하면서도 Risk·회계·감사의 독립성을 유지하게 한다.
> Frontend 경계: [AI_OFFICE_FRONTEND_PLAN.md](AI_OFFICE_FRONTEND_PLAN.md)
> Backend·Event·Docker 연결 경계: [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)
> 본부별 Ollama Model 경계: [OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md](OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)

## 1. 이 구조가 필요한 이유

현재 저장소는 설계에서 구현으로 넘어가는 과도기다. Hermes Profile과 트레이딩·회계·리서치 본부의 실행
코드는 `departments/<n>/` 아래 본부별 폴더로 이동했지만(11절 단계 1~3), 공통 계약·Skill·DB Migration은
아직 `skills/`, `db/`, `supabase/`, `timescaledb/` 최상위 경로에 남아 공유 경계로 추출되지 않았다.
이 상태에서는 다음 문제가 생긴다.

- 담당자가 자기 본부 변경 범위를 한눈에 파악하기 어렵다.
- Profile, 결정론적 Service, Test와 운영 문서의 소유자가 달라질 수 있다.
- 공통 Platform과 본부 코드의 경계가 흐려져 한 본부가 다른 본부의 내부 구현을 직접 참조할 수 있다.
- `db/` 프로토타입과 `supabase/migrations/` 전사 기준 Schema처럼 같은 Domain을 표현하는 구현이 동시에 존재할 때 어떤 것이 기준인지 모호해진다.
- 대규모 파일 이동을 한 번에 수행하면 Import, Workflow, CI와 운영 스크립트가 동시에 깨질 수 있다.

목표는 모든 파일을 본부 폴더 안에 억지로 넣는 것이 아니다. **본부가 소유하는 업무 코드와 Agent Profile은 본부 경계에 두고, 전사 계약·DB Migration·Orchestration·Integration은 공통 경계에 둔다.**

## 2. 확정 조직과 담당자

| 조직 단위 | 성격 | 담당자 | 목표 루트 |
|---|---|---|---|
| CEO Office | 전사 조정, Mandate, 위원회와 Escalation | 영주님 | `departments/00-ceo-office/` |
| 1. 리서치본부 | 데이터 수집, RAG Evidence와 Research Packet | 재일님 | `departments/01-research/` |
| 2. 트레이딩본부 | Trade Case, Signal, OrderIntent와 집행 | 도현님 | `departments/02-trading/` |
| 3. 리스크본부 | Pre/Post-Trade Risk, Compliance와 Kill State | 동규님 | `departments/03-risk/` |
| 4. 퀀트/백테스트본부 | 전략 가설, Dataset, Backtest와 Release Candidate | 재일님 | `departments/04-quant-backtest/` |
| 5. 회계/포트폴리오본부 | Ledger, Position, Cash, NAV와 Reconciliation | 도현님 | `departments/05-accounting-portfolio/` |
| 6. AI QA/감사본부 | Evidence QA, Model Risk, 권한 검증과 Audit | 동규님 | `departments/06-ai-qa-audit/` |
| Agent Workforce 인사팀 | CEO 직속 Shared Service, Agent 채용·평가·Lifecycle | 영주님 | `departments/07-agent-workforce/` |

인사팀은 경로상 다른 조직 단위와 나란히 두지만 제7의 투자 본부가 아니다. 폴더 번호는 탐색과 정렬을 위한 것이며 조직 권한 순위를 의미하지 않는다.

## 3. 현재 저장소의 사실 기준

11절 단계 1~3이 완료되어 아래 `departments/<n>/` 경로가 실행 기준이다. 단계 4(DB Prototype 통합)와
단계 5(구조 Gate)는 아직 적용 전이므로 `db/`, `supabase/`, `timescaledb/`, `skills/`는 최상위에 그대로
있다.

| 현재 경로 | 현재 역할 | 상태 |
|---|---|---|
| `departments/<n>/hermes/` | 8개 Hermes `config.yaml`과 `SOUL.md`의 Git 기준 사본 | 사용 중 |
| `multi-agent-workflow.yaml` | 5개 전사 Workflow와 Event Routing 설정. `files:` 블록이 `departments/<n>/hermes/config.yaml`을 가리킨다 | Prototype |
| `departments/01-research/collectors/` | 시세·호가·체결·시장 파생 관측 전용 수집기 | 사용 중 |
| `departments/01-research/api/external_*.py` | 뉴스·공시·거시 비영속 요청형 MCP | 사용 중 |
| `skills/agentic-rag/` | `compliance-policy-agent`용 Agentic RAG baseline (공용 skills 경계 유지, Domain Owner는 리스크본부) | Baseline |
| `departments/02-trading/{contracts,oms,broker}/` | 계약, Paper OMS와 Paper Broker | D0-D2 Prototype |
| `departments/05-accounting-portfolio/{ledger,reconciliation}/` | Ledger와 Reconciliation | D2 Prototype |
| `db/` | 트레이딩·회계 초기 Prototype SQL | Transitional |
| `supabase/migrations/` | 전사 운영 DB의 통합 Migration 기준 | Canonical |
| `timescaledb/migrations/` | 고빈도 시장 시계열 DB Migration 기준 | Canonical |
| `tests/schema/` | DB 정적 계약과 Runtime Smoke Test | 사용 중 |
| `docs/06-integrations/ls-openapi/` | LS증권 공개 API 계약 참조 | 사용 중 |
| `ai-office/` | 8개 조직·2개 층 Pixel Office, Trading/Portfolio DEMO Panel과 Scripted Simulation | Frontend Prototype |
| `apps/api/main.py` | 테스트 Paper Loop 기반 `/ui/snapshot`과 기본 차단된 `/agent/ask` | Read-only DEMO BFF Prototype |

구 경로 `orchestration/hermes/`, `trading/`, `execution/`, `accounting/`, `fetch_news.py`는 임시 CLI 호환
Wrapper와 함께 완전히 삭제됐다(2026-07-30, 예정보다 빠름). 위 `departments/<n>/` 경로만 존재한다.

`~/.hermes/profiles/`는 로컬 Runtime 상태이며 Git 저장소가 아니다. 저장소 사본과의 동기화는 현재 `scripts/sync_hermes_profiles.sh`가 담당한다.

## 4. 목표 저장소 구조

```text
multi_agent/
├── departments/
│   ├── 00-ceo-office/
│   │   ├── README.md
│   │   ├── hermes/
│   │   │   ├── config.yaml
│   │   │   └── SOUL.md
│   │   ├── workflows/
│   │   ├── src/
│   │   └── tests/
│   ├── 01-research/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── collectors/
│   │   ├── services/
│   │   ├── rag/
│   │   ├── references/
│   │   └── tests/
│   ├── 02-trading/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── contracts/
│   │   ├── oms/
│   │   ├── execution/
│   │   ├── broker/
│   │   └── tests/
│   ├── 03-risk/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── engine/
│   │   ├── compliance/
│   │   ├── stress/
│   │   └── tests/
│   ├── 04-quant-backtest/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── datasets/
│   │   ├── experiments/
│   │   ├── backtests/
│   │   ├── registry/
│   │   └── tests/
│   ├── 05-accounting-portfolio/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── ledger/
│   │   ├── portfolio/
│   │   ├── reconciliation/
│   │   ├── nav/
│   │   └── tests/
│   ├── 06-ai-qa-audit/
│   │   ├── README.md
│   │   ├── hermes/
│   │   ├── evidence/
│   │   ├── evals/
│   │   ├── model-risk/
│   │   ├── audit/
│   │   └── tests/
│   └── 07-agent-workforce/
│       ├── README.md
│       ├── hermes/
│       ├── profiles/
│       ├── evals/
│       ├── improvements/
│       ├── deployments/
│       ├── lifecycle/
│       └── tests/
├── orchestration/
│   ├── workflows/
│   ├── routing/
│   └── schemas/
├── contracts/
│   ├── events/
│   ├── investment-case/
│   ├── strategy/
│   ├── risk/
│   ├── execution/
│   └── accounting/
├── skills/
│   ├── catalog/
│   ├── packages/
│   └── eval-fixtures/
├── integrations/
│   ├── ls-openapi/
│   ├── broker/
│   ├── model-gateway/
│   └── supabase/
├── apps/
│   ├── api/
│   └── operator-web/       # ai-office를 단계적으로 이전할 운영 Frontend
├── supabase/
│   └── migrations/
├── timescaledb/
│   └── migrations/
├── tests/
│   ├── contract/
│   ├── integration/
│   ├── replay/
│   └── e2e/
├── infrastructure/
│   └── compose/
│       ├── core.yaml
│       ├── observability.yaml
│       └── local-llm.yaml
├── compose.yaml
├── scripts/
├── docs/
└── references/
```

`supabase/`와 `timescaledb/`는 CLI와 Migration Tool의 표준 경로를 유지한다. Schema의 논리적 소유자는 본부별로 나누되 Migration 파일을 본부 폴더로 복제하지 않는다.

각 `departments/<department>/`는 목표적으로 `Dockerfile`과 `compose.yaml`을 소유한다. 루트 `compose.yaml`은 Docker Compose `include`로 본부별 Fragment를 조립하며 API, Worker와 Hermes를 하나의 Process에 합치지 않는다. 현재 루트 `docker-compose.yml`의 Research Collector는 [Backend 연결 계획 Phase B1](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md#phase-b1-compose-modularization)에서 동작 검증을 유지한 채 이동한다.

## 5. 본부 폴더 표준

모든 본부·Shared Service 폴더는 다음 원칙을 따른다.

| 경로 | 포함 내용 | 포함하면 안 되는 것 |
|---|---|---|
| `README.md` | Mission, Owner, 입력·출력 계약, 실행법, 테스트와 Handoff | Secret, 개인 환경값 |
| `hermes/` | Git으로 관리하는 `config.yaml`, `SOUL.md` | `auth.json`, `.env`, Memory, Session, Runtime DB |
| `Modelfile` | Local Model의 Base Model과 최소 역할 요약 | Agent 권한, Secret, Runtime Memory, 미승인 투자자 Persona |
| `src/` 또는 Domain 하위 폴더 | 해당 본부가 독점 소유하는 결정론적 Service | 다른 본부 DB 직접 접근 코드 |
| `tests/` | 본부 Unit·Contract Test와 Failure Fixture | 외부 실거래 Credential |
| `config/`가 필요한 경우 | Version 관리 가능한 비밀이 아닌 정책·Preset | API Key, Broker Token |

본부 폴더 이름은 조직 경계를 표현하고, Python Package 이름은 짧고 안정적인 Domain 이름을 사용한다. 숫자 Prefix는 Import 경로로 사용하지 않는다. 실제 구현 시 `pyproject.toml`의 Package Mapping 또는 `src/` Layout으로 Import 이름을 고정한다.

## 6. 공통 경계의 소유 규칙

| 공통 경계 | 변경 제안 | 필수 검토 | 기준 |
|---|---|---|---|
| `contracts/` | 해당 Output의 생산 본부 | 모든 소비 본부 + AI QA | 하위 호환, Schema Version |
| `orchestration/` | CEO Office 또는 Workflow Owner | 호출되는 본부 전원 + AI QA | 권한 Gate와 실패 기본값 유지 |
| `skills/` | 업무를 소유한 본부 | Agent Workforce + AI QA, Risk 영향 시 리스크본부 | 불변 Version, Eval, 승인과 Rollback Target |
| `supabase/migrations/` | Schema 소유 본부 | DB Owner + Risk/QA, 회계 관련 시 회계본부 | 단일 순서, RLS, Rollback 계획 |
| `timescaledb/migrations/` | 리서치본부 | 퀀트본부 + AI QA | Point-in-Time, Dedup, Retention |
| `integrations/ls-openapi/` | 리서치본부 | 트레이딩·리스크 소비 계약 검토 | Raw 이벤트와 Canonical Event 분리 |
| `apps/` | 도현님(공통 Frontend Platform 기술 DRI), 영주님(Live Office Business Owner) | 모든 Command·Read Model 소유 본부, 동규님 Risk·QA | UI는 Risk·OMS·Ledger 규칙을 구현하지 않음 |
| `infrastructure/` | Platform Owner | Security + 서비스 Owner | Secret과 Service Identity 분리 |

한 명이 두 본부를 담당해도 두 본부 폴더와 승인 역할을 합치지 않는다. 도현님이 트레이딩과 회계를 담당해도 주문 생성과 공식 원장 확정은 별도 PR Review와 Service Identity를 사용한다. 동규님이 리스크와 QA를 담당해도 Risk Decision과 Audit Finding의 승인 권한을 하나로 합치지 않는다.

## 7. 현재 경로에서 목표 경로로의 이전 지도

| 구 경로 | 목표 | 소유자 | 이전 시 필수 조치 | 상태 |
|---|---|---|---|---|
| `orchestration/hermes/ceo-agent/` | `departments/00-ceo-office/hermes/` | 영주님 | Workflow와 Sync Script 경로 동시 수정 | 완료, 구 경로는 삭제됨 |
| `orchestration/hermes/research-department/` | `departments/01-research/hermes/` | 재일님 | Runtime Profile 동기화 검증 | 완료 (`sync_hermes_profiles.sh push`로 검증) |
| `fetch_news.py` | `departments/01-research/api/external_sources.py` | 재일님 | 상주 수집 대신 요청형 `news_search` MCP로 전환 | 완료, 구 경로와 뉴스 Collector는 삭제됨 |
| `references/` | `departments/01-research/references/` 또는 공용 `references/` | 재일님 | 저작권·출처 Registry 확인 후 결정 | 미결정 — 이동하지 않음 |
| `orchestration/hermes/trading-department/` | `departments/02-trading/hermes/` | 도현님 | Workflow 참조 동시 수정 | 완료 |
| `trading/`, `execution/` | `departments/02-trading/{contracts,oms,broker}/` | 도현님 | Import 호환층과 OMS Replay Test | 완료, 구 경로는 삭제됨. Replay Test는 여전히 자체 점검 스크립트뿐 |
| `orchestration/hermes/risk-management/` | `departments/03-risk/hermes/` | 동규님 | Risk Tool Allowlist 재검증 | 완료, Tool Allowlist 재검증은 별도 확인 필요 |
| `skills/agentic-rag/` | 공용 `skills/` 경계 유지, Package별 Domain Owner Metadata 추가 | 동규님 | Risk Source와 QA 재사용 Eval을 분리하고 Registry Version 연결 | 미착수 — 경로는 그대로, Metadata 추가 안 함 |
| `orchestration/hermes/quant-backtest-department/` | `departments/04-quant-backtest/hermes/` | 재일님 | Dataset·Registry 경계와 함께 이동 | 완료 (Dataset·Registry는 아직 코드 없음) |
| `orchestration/hermes/accounting-portfolio-department/` | `departments/05-accounting-portfolio/hermes/` | 도현님 | Ledger 권한과 Service Identity 재검증 | 완료, Service Identity 재검증은 별도 확인 필요 |
| `accounting/` | `departments/05-accounting-portfolio/{ledger,reconciliation}/` | 도현님 | 분개 재구축·대사 Test 유지 | 완료, 자체 점검 통과 확인함. 구 경로는 삭제됨 |
| `orchestration/hermes/qa-department/` | `departments/06-ai-qa-audit/hermes/` | 동규님 | 독립 Reviewer와 Finding 권한 유지 | 완료 |
| `orchestration/hermes/hr-department/` | `departments/07-agent-workforce/hermes/` | 영주님 | 제7 투자 본부로 오해하지 않도록 README 유지 | 완료, README에 명시 |
| `multi-agent-workflow.yaml` | `orchestration/workflows/investment-case.yaml` 등으로 분리 | 영주님 + 관련 본부 | 5개 Workflow를 개별 파일로 분리하고 Contract Test 추가 | 미착수 — 내부 경로 참조만 갱신, 파일 분리는 안 함 |
| `db/` | 제거 또는 본부 Prototype Archive | 도현님 + DB Owner | Supabase 기준과 차이 분석 후에만 처리 | 미착수 (11절 단계 4) |

## 8. 의존성 방향

허용되는 기본 방향은 다음과 같다.

```text
Department Agent
  -> Department API or Tool
  -> Shared Contract
  -> Deterministic Service
  -> Canonical Database

Department A
  -> Contract/API/Event
  -> Department B
```

금지하는 방향은 다음과 같다.

- 한 본부 Agent가 다른 본부 내부 Python Module 또는 DB Table을 직접 호출
- Research·Trading Agent가 Risk Decision, Order State, Position 또는 Ledger를 직접 수정
- 회계본부가 Signal을 생성하거나 트레이딩본부가 Official NAV를 확정
- 인사팀이 Agent 권한을 직접 생성하거나 자기 Candidate의 QA Gate를 통과
- QA가 감사 대상 원본을 수정하거나 자기 Finding을 단독 종료
- 같은 Domain Schema를 두 Migration 계열에서 동시에 적용

### 8.1 Hermes 자기 개선 Artifact 경계

Hermes의 재귀적 자기 개선은 Runtime Memory 폴더를 Git에 넣는 방식으로 구현하지 않는다. 후보, 검증, 배포와 관찰 책임을 다음처럼 분리한다.

| Artifact | 목표 위치 | Source of Truth | 책임 |
|---|---|---|---|
| Memory·Session | 각 Runtime의 격리된 Hermes Namespace | Hermes Runtime + 보존 정책 | 해당 본부, 다른 본부 Raw Memory 접근 금지 |
| Improvement Candidate | `departments/07-agent-workforce/improvements/`의 Service 코드 | Supabase `workforce`·`audit` | 요청 본부가 근거 제출, 인사팀이 Lifecycle 관리 |
| Skill Package | 공용 `skills/packages/` | Git Commit + Skill Registry | 업무 소유 본부가 작성, AI QA가 Eval |
| Golden·Adversarial Fixture | 본부 Test 또는 `skills/eval-fixtures/` | Git Commit + Dataset Manifest | AI QA가 독립 유지 |
| Profile Version | `departments/07-agent-workforce/profiles/` | Supabase Registry + 승인된 Git Artifact | 인사팀 설계, QA·CEO Gate |
| Shadow·Deployment Adapter | `departments/07-agent-workforce/deployments/` | Deployment Event와 Runtime Version | 인사팀 조정, Platform/IAM만 활성화 |
| Workflow Version | `orchestration/workflows/` | Git Commit + Governance Decision | CEO Office와 영향 본부 공동 Review |

`skills/catalog/`에는 Package 이름, Owner, 입력·출력 Schema, Tool Scope, Eval Set, 현재 Champion과 Rollback Version을 둔다. Candidate 상태와 점수는 변경 가능한 운영 데이터이므로 Markdown이나 YAML을 원장으로 사용하지 않고 Supabase에 기록한다. `skills/packages/`에는 Secret, Runtime Memory, Session DB, 현재 Position·PnL·Risk 값 또는 승인되지 않은 Production Credential을 넣지 않는다.

`ImprovementCandidate`가 승인되면 Git Artifact와 Registry Version을 함께 고정하고, Runtime에는 Content Hash가 일치하는 Artifact만 배포한다. Rollback은 이전 Git Version, Profile·Skill Registry와 Deployment Event를 함께 되돌리며 감사 기록은 삭제하지 않는다. 전체 상태 전이는 [마스터 플랜 5.10](../HEDGE_FUND_MASTER_PLAN.md#510-hermes-memory-기반-조직-재귀적-자기-개선)을 따른다.

## 9. Database 기준

현재 DB 기준은 다음처럼 고정한다.

1. 전사 운영 DB의 Canonical Schema는 `supabase/migrations/`다.
2. 고빈도 시장 시계열의 Canonical Schema는 `timescaledb/migrations/`다.
3. `db/001_execution.sql`부터 `db/004_seed.sql`까지는 D0-D2 Prototype이다.
4. `db/`와 `supabase/migrations/`는 동일한 빈 DB에 함께 적용하지 않는다. `funds`, `books`, `orders`, `ledger_accounts` 등 같은 개념의 위치와 계약이 다르다.
5. Prototype 기능을 통합 기준으로 옮길 때는 Table 단위 복사가 아니라 Schema Diff, 데이터 변환, 권한/RLS, Runtime Test를 포함한 Migration PR로 처리한다.
6. 본부 폴더 재배치는 DB Schema 소유권을 바꾸지 않는다. Migration의 물리적 위치와 Domain Owner를 분리해서 관리한다.

## 10. 변경과 Review 규칙

| 변경 종류 | 최소 Reviewer |
|---|---|
| 본부 내부 Prompt·Service·Test | 해당 본부 Owner |
| 본부 간 Contract | 생산 본부 + 소비 본부 + AI QA |
| Risk Rule·Kill State | 리스크본부 + AI QA |
| OMS·Broker 상태 | 트레이딩본부 + 리스크본부 + 회계본부 |
| Ledger·NAV | 회계본부 + AI QA, Risk 영향 시 리스크본부 |
| Agent Profile 권한 | 요청 본부 + Agent Workforce + AI QA |
| Skill·Prompt·Model Version | 요청 본부 + Agent Workforce + AI QA, 권한·Risk 영향 시 리스크본부 |
| 자기 개선 Workflow·배포 Adapter | CEO Office + Agent Workforce + AI QA + 영향 본부 |
| DB Migration | Domain Owner + DB Owner + AI QA |
| 폴더 이동·Import 변경 | 영향받는 모든 Owner + CI 통과 |

실제 폴더 이동 PR은 기능 변경과 섞지 않는다. `git mv`, Import·Workflow 경로 수정, 호환 Wrapper, Test 수정만 포함하고 Domain 동작 변경은 별도 PR로 분리한다.

## 11. 단계적 이전 계획

### 단계 0. 문서 기준 확정 — 완료

- 이 문서와 마스터 플랜에 목표 구조와 소유권을 확정한다.
- 현재 경로와 목표 경로를 명확히 구분한다.
- 실행 파일은 이동하지 않는다.

### 단계 1. Department Scaffold — 완료

- `departments/`와 8개 조직 폴더를 만든다.
- 각 폴더에 Owner, Mission, Input/Output, Test와 Handoff를 담은 `README.md`를 둔다.
- 공용 `skills/`와 인사팀 `improvements/`, `deployments/`, QA `evals/`의 빈 경계만 만들고 Runtime Memory는 옮기지 않는다.
- 빈 Python Package를 대량 생성하지 않는다. — 실제 코드가 있는 `hermes/`, `contracts/`, `oms/`, `broker/`,
  `ledger/`, `reconciliation/`, `collectors/`만 만들었고 나머지(`engine/`, `evals/`, `profiles/` 등)는 코드가
  생길 때까지 비워둔다.

### 단계 2. Hermes Profile 이동 — 완료

- Profile을 조직 폴더의 `hermes/`로 이동한다.
- `multi-agent-workflow.yaml`, `scripts/sync_hermes_profiles.sh`, `CLAUDE.md`와 CI 참조를 같은 PR에서 수정한다.
- 8개 `config.yaml`과 `SOUL.md` 존재, YAML Parse, Timeout·Persona 참조를 자동 검사한다. — YAML Parse는
  수동으로 8개 전부 확인함. Timeout·Persona 참조 자동 검사(CI)는 단계 5로 남는다.

### 단계 3. 본부 코드 이동 — 완료

- Research, Trading, Accounting 순으로 현재 구현이 있는 코드부터 이동한다.
- 기존 CLI 경로는 임시 Wrapper로 유지하고 제거 날짜를 기록한다. — `runpy.run_path` 기반 Wrapper를 만들어
  이동 직후 새 경로·구 경로 양쪽에서 통과를 확인했고, 제거 예정일(2026-10-31)도 기록했으나 실제로는
  2026-07-30에 구 경로(`orchestration/hermes/`, `trading/`, `execution/`, `accounting/`, `fetch_news.py`)를
  예정보다 일찍 완전히 삭제했다. 지금은 `departments/<n>/`만 유효한 경로다.
- Import와 실행 명령을 바꾼 뒤 Unit·Replay Test를 통과한다. — 5개 자체 점검 스크립트(계약/OMS/Paper
  Broker/원장/대사) 전부 통과 확인함. `tests/schema/`는 이동 대상이 아니라 영향 없음.

### 단계 4. DB Prototype 통합

- `db/`와 `supabase/migrations/`의 Schema Diff를 작성한다.
- Prototype에서 필요한 동작을 Canonical Migration과 Service에 이식한다.
- 모든 Runtime Test가 통과한 뒤 `db/`를 Archive 또는 제거한다.

### 단계 5. 구조 Gate

- 본부 간 직접 Import와 내부 DB 접근을 CI에서 탐지한다.
- CODEOWNERS 또는 동등한 Review 정책을 실제 GitHub 계정과 합의 후 추가한다.
- 문서 링크, Workflow Profile 경로, Mermaid, YAML, Python Import와 SQL Migration을 CI에서 검사한다.

### 단계 6. AI Office Frontend 이전

- 이전 완료 전에는 `ai-office/`를 실행 기준으로 유지하고 `apps/operator-web/`를 동시에 운영하지 않는다.
- `DEMO` Mode를 동결한 뒤 8개 조직, REST Snapshot과 WebSocket Event Store를 fixture 환경에서 연결한다.
  사용자 로그인·세션은 구현하지 않는다.
- `apps/operator-web/` 이동은 Import, Lockfile, Cloudflare·Hosting 설정, Docker와 CI 경로를 같은 PR에서 수정한다.
- Pixel Office는 Domain Event의 Projection만 담당하고 Browser에서 Risk, OMS, Ledger와 거래 DB를 직접 수정하지 않는다.
- 상세 단계와 완료 조건은 [AI Office Frontend Plan](AI_OFFICE_FRONTEND_PLAN.md)을 따른다.

## 12. 전체 교차 점검 결과

이번 점검은 Markdown만 수정한다. 따라서 비 Markdown 파일에서 발견한 문제는 문서에 공개하되 이 변경에서 고치지 않는다.

| 항목 | 판정 | 이번 처리 |
|---|---|---|
| 마스터 플랜의 Agent Workforce 인사팀 추가 | 정합 | CEO 직속 Shared Service이며 제7 투자 본부가 아니라는 기존 원칙과 일치 |
| 6개 본부 + CEO + 인사팀 = 8개 Hermes Supervisor | 정합 | 조직 문서, Profile 수와 일치 |
| README의 “실행 코드 없음” | 불일치 | 실제 구현 현황으로 수정 |
| 마스터 플랜과 README의 저장소 구조 | 불일치 | 본부 소유 중심 목표 구조로 교체하고 현재/목표를 분리 |
| Research `SOUL.md`의 `market_data.json` | 불일치 | 삭제된 파일 참조를 `market-api`와 `fetch_news.py` 기준으로 수정 |
| `db/`와 `supabase/migrations/` | 충돌 위험 | Canonical/Prototype 지위를 명시하고 병행 적용 금지 |
| `multi-agent-workflow.yaml`의 “시장 데이터 소스 미확정” | 불일치, 비 MD | LS증권 가격 Source가 확정 기준이며 YAML 후속 수정 필요 |
| 실시간 Workflow의 OMS 명시 단계 | 누락, 비 MD | Risk 승인과 Accounting 사이 Deterministic OMS/Fill 단계 추가가 후속 필요 |
| `skills/agentic-rag/src/graph.py`의 이전 YAML 파일명 | 오래된 설명, 비 MD | `orchestration/hermes/.../config.yaml` 참조로 후속 수정 필요 |
| `db/*.sql` 상단 팀 문서 경로 | 오래된 설명, 비 MD | `docs/05-teams/` 경로로 후속 수정 필요 |
| `config.yaml`의 Timeout 일치 주석 | 오래된 설명, 비 MD | Profile 기본 Timeout과 Workflow Step Timeout은 의미가 다르므로 YAML 주석 후속 수정 필요 |
| `agent_evolution_cycle`과 Hermes 조직 학습 | 부분 정합, 비 MD | Profile 개선 Prototype은 존재하나 Improvement Registry, Skill Write Gate, 독립 Eval Runner와 Scorecard 연결 필요 |
| Bedrock/Ollama 목표와 현재 Nous Profile | 상태 구분 필요 | 현재 Profile은 개발 Runtime, Bedrock/Ollama는 목표 Model Gateway임을 문서에 명시 |
| LS Open API 문서 | 정합 | REST·WebSocket 42개 API 묶음과 365개 TR 참조가 문서 지도에 연결됨 |
| `ai-office`와 8개 조직 | 부분 정합 | 8개 조직·2개 층 전환 완료, Backend 조직 Registry 기반 배치는 미완료 |
| `ai-office`와 금융 Source of Truth | 부분 연결 | DEMO BFF·Trading/Portfolio Fixture는 존재하며 공식 Snapshot·WebSocket와 Kanban Status Bridge는 미완료. 외부 사용자 로그인·세션은 범위 밖 |

## 13. 완료 기준

문서 단계는 다음 조건을 만족하면 완료다.

- 마스터 플랜, README, 본 문서와 네 팀 가이드가 같은 조직명·담당자·목표 경로를 사용한다.
- 현재 경로를 목표 경로처럼 표현하지 않는다.
- `supabase/migrations/`, `timescaledb/migrations/`와 `db/`의 지위가 명확하다.
- 모든 Markdown 상대 링크가 존재한다.
- 전체 Markdown에 H1 하나와 균형 잡힌 코드 펜스가 있다.
- 비 Markdown 후속 문제는 담당과 수정 조건이 기록되어 있다.
- `ai-office/`의 현재 Demo 경계와 `apps/operator-web/` 목표 경로가 구분되고 Frontend가 금융 Source of Truth가 아님이 명시된다.

실제 구조 이전은 이 문서의 단계 1부터 별도 PR로 진행하며, 각 단계가 끝날 때 현재 경로 표와 이전 지도를 갱신한다.
