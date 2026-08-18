# Claude용 AWS 부서별 이관 실행계획

상태: 실행 지시안
기준일: 2026-08-13
대상 저장소: `HgFinance/multi_agent`
목표: 로컬에서 검증한 Pipeline 배선을 AWS Pilot에 보존하면서, 8개 부서를 한 번에 하나씩 독립 PR로 이관한다.

## 1. 이번 작업의 결론

전사 `docker-compose.yml`을 통째로 EC2에 복사하지 않는다. 현재 리서치본부에 구현된
수직 슬라이스를 골든 패턴으로 고정하고, 공통 AWS Runtime을 먼저 만든 다음 부서를
하나씩 붙인다.

```text
로컬 변경 기준선 보존
  -> 공통 AWS Runtime/배포 계약
  -> Research
  -> Quant-Backtest
  -> Risk
  -> AI QA/Audit
  -> Trading
  -> Accounting/Portfolio
  -> CEO Office
  -> Agent Workforce
  -> 전사 Integration/Paper Drill
```

각 부서는 `Image -> Head Hermes -> MCP/API -> Runner -> Tool/Evidence -> Worker ->
결정론 검증 -> 부서 결과 -> DB/Event`를 자기 PR 안에서 끝낸다. 다음 부서는 앞 부서의
버전된 Contract와 Fixture가 merge된 뒤 시작한다.

## 2. Claude가 먼저 읽을 파일

작업 시작 전에 아래 순서로 읽고, 충돌하면 위 문서를 우선한다.

1. `CLAUDE.md`
2. `docs/HEDGE_FUND_MASTER_PLAN.md`
3. `docs/02-engineering/FINAL_RUNTIME_ARCHITECTURE.md`
4. `docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md`
5. `docs/02-engineering/LOCAL_COMPOSE_RUNTIME_BASELINE.md`
6. `docs/PROJECT_IMPLEMENTATION_STATUS.md`
7. `docs/02-engineering/RESEARCH_WORKER_AWS_RUNBOOK.md`
8. `docs/03-data/AWS_DATA_MIGRATION_PLAN.md`
9. 작업 대상 부서의 `README.md`, `hermes/config.yaml`, `SOUL.md`, 팀 가이드

현재 기준은 Hermes Head 8개, LLM Worker 10명, 결정론 Runner 5개다. 오래된 문서의
19명 편제나 Trading LLM Worker 3명을 복원하지 않는다. 정본은 `CLAUDE.md`, 각 부서
`hermes/config.yaml`, `tests/test_worker_architecture.py`다.

## 3. 고정할 AWS Pilot 기준

이번 이관은 `FINAL_RUNTIME_ARCHITECTURE.md`의 단일 EC2 Pilot을 따른다.

- Compute: Ubuntu 24.04의 `g6.xlarge`, Docker Compose, NVIDIA L4 24GB
- Model Plane: 공용 vLLM `Qwen2.5-14B-Instruct FP8`; 부서마다 vLLM을 복제하지 않음
- Model artifact: S3가 정본, EBS `/opt/hgfinance/models`는 검증된 cache
- Operational SoR: 환경이 분리된 Supabase PostgreSQL
- Market time series: 현재 TimescaleDB 계약을 유지하되 AWS 물리 형태는 별도 승인 전 임의 변경 금지
- Queue/cache/event: 전용 Redis; 원장 또는 Canonical DB로 사용 금지
- Secret: 저장소와 Compose 평문 금지; 런타임 주입과 최소 Service Identity 사용
- Network: 외부 공개는 BFF/reverse proxy의 443만; 부서 API, Hermes, Redis, vLLM은 내부 전용
- Environment: 우선 `PRODUCTION_ADVISORY`; Broker 주문·Ledger 임의 쓰기·`PRODUCTION_LIVE` 승격 금지

`deploy/eb/`는 도현 파트 Paper 배포의 별도 산출물이다. 전사 기준선으로 확장하지 말고,
거기서 검증한 Trading Outbox와 Accounting Consumer 동작만 재사용한다. 전사 Pilot은
Linux EC2 Compose 기준의 별도 AWS overlay/bundle로 만든다.

## 4. PR 및 브랜치 운영

### PR-00: 로컬 배선 기준선 보존

현재 작업 트리에는 리서치 Evidence First, Worker Model Gateway, Research/Quant Liaison,
Factory loop 차단, Quant 복구·검증 변경이 섞여 있다. Claude는 이를 잃거나 AWS 변경과
섞지 않는다.

1. `git status`, `git diff --stat`, `git diff`로 사용자 변경을 목록화한다.
2. 관련 자체 점검과 Test를 재실행한다.
3. 사용자가 만든 변경을 임의로 되돌리거나 포맷팅하지 않는다.
4. 하나의 기준선 PR로 만들기 어렵다면 기능 단위 PR로 나누되, AWS 이관 PR보다 먼저 merge한다.
5. 기준선 commit SHA와 통과한 명령을 이후 AWS PR의 설명에 기록한다.

### 이후 원칙

- PR 하나에는 공통 기반 또는 부서 하나만 넣는다.
- Contract 생산 PR이 소비 PR보다 먼저 merge된다.
- 폴더 이동과 동작 변경을 같은 PR에 섞지 않는다.
- 각 PR은 `CONFIG_VERIFIED`, `TEST_VERIFIED`, `RUNTIME_VERIFIED`를 구분해 보고한다.
- `origin/main`에서 시작하고, 오래된 원격 부서 브랜치를 그대로 이어 쓰지 않는다.
- 실패한 검증, 누락 Credential, 미기동 외부 서비스는 성공처럼 기록하지 않는다.

## 5. Phase 0 — 공통 AWS Runtime

추천 브랜치: `feat/aws-pilot-runtime-baseline`

### 구현

- 로컬 `docker-compose.yml`은 개발 기준으로 보존하고 AWS 전용 overlay 또는 bundle을 추가한다.
- Windows `${USERPROFILE}` bind mount를 named volume 또는 명시적 Linux 경로로 교체한다.
- `host.docker.internal`, `extra_hosts: host-gateway`, 로컬 Claude CLI proxy 의존을 제거한다.
- Head Provider는 승인된 자동화 Adapter endpoint만 사용한다. Claude 구독 세션을 API key처럼 취급하지 않는다.
- 공용 vLLM/Worker Model Gateway, Redis, internal network, migration one-shot, reverse proxy 경계를 선언한다.
- 부서 내부 포트 publish를 제거하고 Compose service DNS로만 통신한다.
- `/health`는 process liveness, `/health/ready`는 DB/Redis/model 같은 dependency readiness로 분리한다.
- restart 뒤 Queue lease, idempotency key, event hash, correlation ID가 유지되는 공통 계약을 만든다.
- 환경 변수 이름과 필수/선택 여부만 `.env.example`에 기록하고 값은 넣지 않는다.
- 이미지에는 source revision과 build timestamp를 label로 남기고 mutable `latest`만으로 배포하지 않는다.
- 배포 전 `docker compose config`와 비밀·Windows 경로·host gateway 탐지 검사를 CI에 추가한다.

### Exit Gate

- Ubuntu EC2에서 공통 서비스가 재부팅 뒤 자동 복구된다.
- 외부에서 443 외 부서 포트, Redis, vLLM에 접근할 수 없다.
- DB 또는 model이 없을 때 관련 endpoint가 fail-closed하고 `/health`와 `/health/ready`가 구분된다.
- migration 실패 시 애플리케이션을 정상 상태로 표시하지 않는다.
- 로그에 Secret, prompt 원문, Broker token이 나오지 않는다.

## 6. 부서 공통 구현 템플릿

각 부서 PR에서 아래를 그대로 반복한다.

1. **Inventory**: 로컬 service, image, command, port, volume, env, DB schema, event, tool allowlist를 표로 작성한다.
2. **Contract**: 입력·출력 Pydantic schema, event version, idempotency key, timeout, retry, failure action을 고정한다.
3. **AWS adaptation**: Linux volume, internal DNS, Secret ref, health/readiness, resource limit을 적용한다.
4. **Vertical slice**: CEO/Kanban 또는 고정 Fixture에서 들어온 Task 하나가 최종 부서 산출까지 흐르게 한다.
5. **Persistence**: 결과 DB row와 event의 `case_id/task_id/correlation_id/content_hash`를 대조한다.
6. **Failure test**: DB, Redis, model, upstream API 중 하나를 끊어 안전한 결과가 나오는지 검증한다.
7. **Restart test**: 실행 중 container를 재시작해 중복 side effect 없이 resume/retry 되는지 확인한다.
8. **Security test**: 금지 Tool이 실제 등록되지 않았거나 인증·allowlist에서 차단되는지 확인한다.
9. **Evidence**: 명령, 핵심 로그, request/response, DB query 결과를 `artifacts/`의 작은 JSON/Markdown으로 남긴다.
10. **Status update**: 구현 상태 문서와 해당 Runbook을 같은 PR에서 갱신한다.

부서 완료의 최소 정의는 `image build 성공`이 아니다. AWS에서 한 Task가 실제 Runtime을
통과하고, 실패·재시작·권한 차단까지 증명돼야 `RUNTIME_VERIFIED`다.

## 7. 부서별 순서와 작업 카드

### D01 Research — 골든 수직 슬라이스 고정

추천 브랜치: `feat/aws-research-runtime`

현재 `RESEARCH_WORKER_AWS_RUNBOOK.md`의 구현을 골든 패턴으로 굳힌다.

- `research-api`, `market-api`, `research-mcp`, `research-hermes`, Research Liaison을 AWS overlay에 연결한다.
- `run_research_workers -> Worker Model Gateway -> vLLM`을 worker별 binding으로 실행한다.
- Worker 호출 전에 뉴스·공시·가격을 독립 수집하고 실패 source를 숨기지 않는다.
- Liaison에는 `run_research_packet`, `factory_submit_*` 같은 쓰기 도구가 등록되지 않음을 검증한다.
- `origin=user-query`와 factory origin을 구분해 자동 메시지 순환을 차단한다.
- S3 Parquet migration은 `TIMESTAMP(MICROS)`, `DECIMAL`, raw `select *`, content hash 재검증을 지킨다.
- 필수 2.5GB 계층을 먼저 옮기고, 원시 5~30일 범위는 승인 전 하드코딩하지 않는다.

Exit: 삼성전자 같은 단일 symbol 질의가 Evidence First를 거쳐
`research.worker-context.v1`을 만들고, source 일부 장애 시 `degraded/FAILED`가 그대로 보인다.

### D04 Quant-Backtest — Research 산출 소비

추천 브랜치: `feat/aws-quant-runtime`

- Research와 같은 공용 Worker Model Gateway를 쓰고 Quant worker binding만 분리한다.
- Factory Autopilot, Experiment Worker, Backtest Runner, Dataset Resolver를 AWS 내부망에 연결한다.
- S3 Dataset Manifest와 Supabase experiment/strategy registry를 hash로 연결한다.
- PIT 위반, 미래 데이터, 범위 밖 parameter는 실행 전에 결정론적으로 거절한다.
- stale hypothesis 회수 시 orphan `RUNNING` experiment도 제한된 zombie predicate로 닫는다.
- Quant는 `SHADOW/PAPER/PRODUCTION` 승격 권한을 갖지 않는다.

Exit: 고정 Research Packet이 Dataset -> Hypothesis -> Backtest -> Walk-Forward ->
Strategy Candidate까지 흐르고, 동일 입력 재실행 hash와 결과가 일치한다.

### D03 Risk — Binding Gate 분리

추천 브랜치: `feat/aws-risk-runtime`

- `risk-api`, `risk-hermes`, compliance worker, deterministic `risk-runner`를 연결한다.
- LLM은 근거·권고만 만들고 `risk.decision.v1`의 binding 판정은 결정론 Engine만 쓴다.
- Mandate version, PIT/staleness, exposure, stress, VaR, correlation, kill state를 입력 계약으로 검증한다.
- Trading/QA와 Service Auth token을 공유하지 않는다.
- Redis 장애, 정책 source 누락, 오래된 market snapshot은 승인으로 fallback하지 않고 HOLD/DENY/ESCALATE한다.

Exit: 고정 OrderIntent가 Risk Gate를 통과/거절하는 두 Fixture에서 DB row와 event hash가 일치하며,
Risk Hermes가 주문을 제출할 수 없음이 테스트로 증명된다.

### D06 AI QA/Audit — 독립 검증면

추천 브랜치: `feat/aws-qa-audit-runtime`

- `audit-api`, `qa-worker`, `qa-hermes`, eval/replay runner를 연결한다.
- Research/Quant/Risk artifact를 복사해 수정하지 않고 immutable ref로 읽는다.
- Hallucination critic과 incident postmortem worker를 공용 Model Plane에 worker별 binding으로 붙인다.
- Trace, tool call, model/prompt/dataset version과 permission finding을 남긴다.
- QA는 감사 대상 원본을 수정하거나 자기 Finding을 단독 종결하지 않는다.

Exit: PASS와 ESCALATE Fixture가 `qa.decision.v1`과 Finding을 만들고, 대상 artifact 변경 시
content hash 불일치로 실패한다.

### D02 Trading — Paper OMS와 Outbox

추천 브랜치: `feat/aws-trading-runtime`

- `deploy/eb/`에서 검증한 `trading-api`, Paper OMS, `trading-outbox-relay`를 Pilot overlay에 이식한다.
- 최신 편제대로 LLM Worker를 새로 만들지 않고 결정론 `desk-runner`를 사용한다.
- `OrderIntent != Order`; 승인된 Risk Decision 없이는 Paper Order를 생성하지 않는다.
- Order 상태 머신, idempotency, outbox `FOR UPDATE SKIP LOCKED`, retry/DLQ를 검증한다.
- Live Broker credential은 넣지 않고 `PRODUCTION_ADVISORY/PAPER`만 허용한다.

Exit: 승인 Fixture만 Order/Fill/Outbox event를 만들고, 거절 Fixture는 side effect 0이며,
relay 재시작 후에도 중복 Fill이 생기지 않는다.

### D05 Accounting/Portfolio — Fill에서 원장까지

추천 브랜치: `feat/aws-accounting-runtime`

- `accounting-api`, `accounting-ledger-consumer`, deterministic `back-office-runner`, exception worker를 연결한다.
- `SENT` Fill envelope를 분개, Position, Cash, PnL projection으로 멱등 처리한다.
- Position은 Fill 또는 승인된 Adjustment로만 바뀌게 한다.
- market mark가 없으면 NAV를 만들지 말고 HOLD 상태와 이유를 남긴다.
- Trading과 DB credential/role을 공유하지 않으며 원장 수정 endpoint를 외부 공개하지 않는다.

Exit: 한 Fill이 정확히 한 번 분개되고 재처리해도 잔액이 변하지 않으며, mark 유무에 따른
NAV 생성/보류가 구분된다.

### D00 CEO Office — 전사 조정과 최종 합성

추천 브랜치: `feat/aws-ceo-governance-runtime`

- `governance-api`, CEO Hermes, Kanban dispatcher/supervisor, watchdog, notification worker를 연결한다.
- Natural Language Query를 versioned Task로 만들고 필요한 부서만 동적으로 라우팅한다.
- `origin=user-query`를 자식 카드에 전파하고 factory-origin의 Liaison 재진입을 차단한다.
- timeout, retry, partial failure를 카드 상태와 최종 응답에 정직하게 반영한다.
- CEO는 주문 제출, Risk 승인, Ledger/NAV 수정, Finding 종결 권한을 갖지 않는다.

Exit: 사용자 질의 한 건이 필요한 부서만 거쳐 최종 합성되고, 부서 하나가 실패해도 실패 사실이
누락되지 않으며 무한 카드 순환이 없다.

### D07 Agent Workforce — 승인형 변경만

추천 브랜치: `feat/aws-workforce-runtime`

- Workforce API/event worker, HR Hermes, `profile-architecture-worker`, IAM provisioning 경계를 연결한다.
- 후보 생성 -> 독립 QA Eval -> CEO 승인 -> IAM provision -> Shadow 배포 순서를 강제한다.
- 자기 승인, 직접 permission grant, Production 즉시 활성화를 차단한다.
- Profile/Skill artifact는 Git hash와 Registry version을 함께 기록한다.
- 퇴직/이동 시 queue lease, token, secret, namespace 회수와 열린 case 인계를 검증한다.

Exit: 승인된 후보만 Shadow identity를 얻고, self-approval과 QA 근거 없는 후보는 side effect 없이 거절된다.

## 8. Phase 9 — 전사 Integration과 Paper Drill

추천 브랜치: `feat/aws-paper-integration`

### 시나리오

```text
User Query
 -> CEO Scope/Task
 -> Research Packet
 -> Quant Strategy Candidate
 -> QA Eval
 -> Risk Decision
 -> Trading Paper Order/Fill
 -> Accounting Ledger/Position/NAV-or-HOLD
 -> QA Trace/Finding
 -> CEO Final Synthesis
```

HR은 일반 투자 질의에 호출하지 않고 workforce event에서만 별도 cycle로 검증한다.

### 필수 Drill

- 같은 request를 2회 보내도 주문·Fill·분개가 중복되지 않음
- Redis, DB, model gateway, 한 부서 API를 각각 중단했을 때 안전한 실패
- 실행 중 container/EC2 재시작 후 queue와 state 복구
- 오래된 market data와 없는 policy/evidence에서 승인 금지
- S3 model/dataset hash 불일치에서 기동 또는 실행 거부
- Secret rotation 뒤 재기동 및 이전 credential 폐기
- 이전 image/tag로 rollback하고 DB/event audit history는 보존
- 최소 10거래일 Paper Dry Run 전에는 Live flag를 만들거나 켜지 않음

## 9. Claude에게 매 PR마다 줄 작업 프롬프트

아래 블록에서 `<DEPARTMENT>`와 `<BRANCH>`만 바꿔 사용한다.

```text
HgFinance/multi_agent 저장소의 <DEPARTMENT> 부서를 AWS Pilot로 이관하라.
브랜치는 <BRANCH>를 사용한다.

먼저 CLAUDE.md와 docs/02-engineering/AWS_DEPARTMENT_EXECUTION_PLAN_FOR_CLAUDE.md,
FINAL_RUNTIME_ARCHITECTURE.md, 대상 부서 README/config/SOUL을 읽어라.
현재 working tree의 사용자 변경을 보존하고, 관련 없는 파일은 수정하지 마라.

이번 PR 범위는 대상 부서 하나뿐이다. 로컬 docker-compose.yml 동작은 보존하고
AWS 전용 overlay/bundle에 Linux volume, internal DNS, Secret injection,
health/readiness, resource limit을 구현하라. 공용 Redis/vLLM/Worker Model Gateway를
부서마다 복제하지 마라. 다른 부서 내부 Python module이나 DB table을 직접 호출하지 말고
versioned Contract/API/Event만 사용하라.

작업 전 inventory와 gap을 작성한 뒤 구현하고, unit/contract/integration/AWS smoke,
dependency failure, restart/idempotency, forbidden-authority 테스트를 실행하라.
통과하지 않은 항목은 RUNTIME_VERIFIED로 표시하지 마라.

완료 보고에는 다음을 포함하라:
1) 변경 파일과 이유
2) 입력/출력/event/DB 계약
3) 실행한 명령과 결과
4) AWS에서 확인한 request_id/task_id/correlation_id 및 artifact hash
5) 실패 주입과 재시작 결과
6) 남은 blocker와 다음 부서가 소비할 고정 Contract
```

## 10. 중단 조건

Claude는 아래 상황에서 임의 우회 구현을 만들지 말고 `BLOCKED`로 보고한다.

- upstream Contract/version/Fixture가 없음
- AWS region, S3 bucket, TimescaleDB 물리 형태처럼 문서상 미결정인 값을 정해야 함
- Production Provider의 자동화 사용 계약 또는 credential 주입 방식이 승인되지 않음
- Live Broker credential이나 실주문 권한이 필요함
- Canonical `supabase/migrations/`와 prototype `db/`를 같은 DB에 적용해야만 진행 가능함
- 다른 부서 DB 직접 쓰기 또는 권한 합치기 없이는 진행할 수 없음
- 사용자 로컬 변경과 충돌해 보존할 수 없음

## 11. 최종 완료 체크리스트

- [ ] 로컬 배선 기준선이 GitHub PR/commit으로 보존됨
- [ ] 공통 AWS Runtime이 Ubuntu EC2에서 재부팅 후 복구됨
- [ ] 8개 Hermes Profile의 memory, credential, tool allowlist가 격리됨
- [ ] 10개 LLM Worker가 worker별 model/adapter metadata를 남김
- [ ] 5개 결정론 Runner가 binding decision 경계를 유지함
- [ ] 부서별 8개 PR이 독립적으로 rollback 가능함
- [ ] DB row, event, artifact의 correlation/hash가 전사 E2E에서 연결됨
- [ ] 실패·재시작·중복 요청·권한 우회 테스트가 통과함
- [ ] 10거래일 Paper Dry Run과 운영 Runbook이 완료됨
- [ ] `PRODUCTION_LIVE`는 별도 승인 전 OFF임
