# HgFinance Unified Domain API Specification

문서 상태: 통합 기준안 v1.2 (실행 계약 연결)

기준일: 2026-08-04

소유: Platform/API Architecture

적용 범위: Research·Quant, Trading, Risk·QA, Governance·Workforce, Accounting·Portfolio

이 문서는 5개 Domain API 명세군을 하나의 통신 계약으로 통합한다. API가 Registry에 기록되어 있다는 사실은 Production 승인이나 실데이터 연결을 의미하지 않는다. 실제 Route와 Event의 구현 상태는 실행 가능한 Registry 및 계약 테스트를 기준으로 판단한다.

## 1. Source of Truth와 통신 계층

문서와 실행 계약의 관계는 다음과 같다.

| 계층 | 권위 | 책임 |
|---|---|---|
| 제품·조직·권한 원칙 | HEDGE_FUND_MASTER_PLAN.md와 하위 설계 문서 | 업무 의미와 SoD |
| 통합 통신 의미 | 이 문서 | 공통 Envelope, 흐름, 경계 |
| 실행 Route 상태 | [Route Status Registry](contracts/route-registry.v1.json) | actual·planned·excluded 구분 |
| 부서 간 Event 이름 | [Event Registry](contracts/event-registry.v1.json) | Producer·Consumer·Version·Alias |
| Event 공통 구조 | [event-envelope.v1 Schema](contracts/event-envelope.v1.json) | 필수 메타데이터와 Payload 참조 |
| 부서 내부 Worker 구조 | [worker-context.v1 Schema](contracts/worker-context.v1.json) | Head·Worker 실행 Context |
| 실행 검증 | tests/contracts/test_unified_api_contract.py | OpenAPI·Registry·Schema 불일치 차단 |

검증 명령:

    python -m pytest tests/contracts/test_unified_api_contract.py -q

Route 계약 테스트는 각 FastAPI의 app.openapi()를 별도 subprocess에서 읽는다. ready 앱의 visible operation이 Registry와 다르면 누락·초과·Method 불일치 모두 Test 실패다. Planned Route는 OpenAPI 비교 대상이 아니며 실제 Route와 겹치면 실패한다.

### 1.1 통신 경계

| 통신 | 계약 | 허용 범위 |
|---|---|---|
| 같은 부서 Head ↔ Worker | worker-context.v1 | 위임, 근거 수집, 비바인딩 결과 전달 |
| 부서 간 동기 호출 | Domain API DTO | allow-list된 조회, 계산, Gate, 상태 확인 |
| 부서 간 비동기 호출 | event-envelope-v1 | 상태 변경, 장시간 작업, 감사, 재처리 |
| UI·Operator | Read-only BFF Projection | 원장·Risk·OMS·Credential을 소유하지 않음 |

Worker를 별도 프로세스나 Queue로 분리해도 내부 계약은 worker-context.v1을 유지한다. Worker가 새로운 공개 Business API나 권한 우회 경로가 되어서는 안 된다.

## 2. 5개 명세군과 Route Namespace

5개 명세군은 실제로 여러 FastAPI와 BFF로 분리된다. 실행 Route의 전체 목록과 Method는 Route Registry에만 중복 기록한다.

| 명세군 | 주요 실행 서비스 | Route Namespace |
|---|---|---|
| Research·Quant | Research Evidence, Market Read, Research Workflow, Quant | Legacy Read, /research/v1, /quant/v1 |
| Trading | Paper OMS·Broker Adapter | /trading/v1, /investment-cases/{case_id}/paper-orders |
| Risk·QA | Risk API, QA Gate·Audit API | /risk/v1, /qa/v1, Case Risk/QA Gate |
| Governance·Workforce | CEO Governance, HR Workforce, Reporting | /governance/v1, /workforce/v1, /reporting/v1 |
| Accounting·Portfolio | Ledger·Valuation·Reconciliation Domain API, BFF Projection | /accounting/v1, BFF /accounting/v1/portfolio-snapshot |

### 2.1 Legacy와 신규 Route

- Research Evidence·Market의 현재 Read API는 /evidence/*, /macro/*, /snapshot/*, /bars/* service-local 경로를 유지한다.
- 신규 Command·Job API만 /research/v1/*와 /quant/v1/*를 사용한다. Legacy 경로를 신규 v1 경로로 추론하거나 자동 Alias로 취급하지 않는다.
- POST /investment-cases는 Minimum Service Unit의 Case 생성 Route다. /governance/v1/cases는 전사 Governance Case Root와 Artifact Pointer를 관리하며 투자 Case 생성 Route를 중복하지 않는다.
- /investment-cases/{case_id}/...는 이미 존재하는 Case에 종속된 Domain Action이다.
- apps/api의 GET /accounting/v1/portfolio-snapshot은 Accounting Domain의 쓰기 API가 아닌 read-only Projection이다.
- /metrics와 같은 운영 관측 Route는 include_in_schema=false이면 OpenAPI actual Route에서 제외하고 Registry의 excluded_routes에 기록한다.

### 2.2 현재 구현 요약

| App | OpenAPI 상태 | actual operation | 기본 상태 |
|---|---:|---:|---|
| research-evidence-api | ready | 9 | Legacy implemented |
| market-read-api | ready | 8 | Legacy implemented |
| trading-api | ready | 15 | Paper only |
| risk-api | ready | 10 | Production blocked |
| qa-api | ready | 29 | Production blocked |
| workforce-api | ready | 17 | Test implementation |
| governance-api | ready | 19 | Test implementation |
| operator-bff | ready | 5 | Projection/Test |
| Research Workflow, Quant, Accounting, MSU Case | planned | 0 | Planned |

현재 Registry에는 ready 앱의 112개 actual operation과 55개 planned operation이 분리되어 있다. Quant와 Accounting의 FastAPI Route는 계획에 기록되어 있지만 현재 구현 Route로 세지 않는다.

## 3. 공통 API 계약

### 3.1 인증과 권한

- Production 목표는 Service Token과 mTLS다. Token Subject의 department, service, scopes를 검증한다.
- 현재 구현은 Risk Trading State 명령과 QA Corrective Action 종결에 대해 짧은 수명의 HS256 Bearer Service Token(`sub`, `department`, `service`, `scopes`, `exp`)을 검증한다. 전역 Issuer/JWKS·mTLS·IAM 및 `sub`와 `agent_id/profile_version_id` 매핑은 배포 전 연결 대상이며, 이 명령 단위 검증만으로 Production 인증 완료로 간주하지 않는다.
- Scope 형식은 department.resource.action.read 또는 department.resource.action이다. Fund·Book·Case 범위를 벗어나면 403이다.
- Browser와 Agent는 Domain API를 직접 공개 호출하지 않는다. BFF 또는 승인된 내부 Service/MCP Gateway를 사용한다.
- CEO·Worker·LLM에게 Supabase Service Role, Broker Credential, LS Credential을 제공하지 않는다.
- Agent Tool은 allow-list된 제안·조회·검증 도구만 노출한다. 주문 Submit, Risk 결정 적용, Ledger Posting, 권한 Provisioning은 Agent Tool이 직접 수행하지 않는다.

### 3.2 멱등성과 동시성

- POST Command는 Idempotency-Key 또는 Domain별 동등 키를 요구한다.
- 같은 키와 같은 정규화 본문은 최초 결과를 재반환한다.
- 같은 키와 다른 본문은 409 IDEMPOTENCY_CONFLICT다.
- 상태 변경은 expected_version을 사용하고 버전 충돌은 409로 반환한다. 자동 재승격·자동 재시도 승인·자동 상태 복구는 금지한다.
- Domain 고유 키를 보존한다: Broker Event는 broker_event_id, Artifact는 content hash, Ledger Fill은 broker_fill_id, Corporate Action은 action_id.

### 3.3 시간·PIT·수치

- as_of, as_known_at, observed_at, occurred_at은 timezone-aware ISO-8601 UTC다. timezone 없는 값은 422다.
- Evidence·Market·Quant Dataset·Risk 계산은 요청의 PIT 시각을 초과하는 데이터를 사용하지 않는다.
- Price, quantity, amount, ratio, exposure, PnL, NAV는 JSON number로 전달하지 않고 도메인 Schema가 정한 decimal 문자열 또는 정밀 수치 타입을 사용한다.
- PIT 필터, citation 검증, numeric trace, 상태 전이, Risk 한도, Ledger 차대는 결정론적 코드가 담당한다. LLM은 관련성 판단과 서술만 수행한다.

### 3.4 공통 Error Envelope

모든 오류 응답은 다음 공통 필드를 가지며 Domain detail은 확장할 수 있다.

    {
      "error_code": "RISK_GATE_REJECTED",
      "message": "Risk 한도를 초과했습니다.",
      "detail": {"reason_codes": ["MAX_SYMBOL_WEIGHT"]},
      "request_id": "req_01K1",
      "trace_id": "trace_01K1"
    }

| HTTP | 의미 | 기본 처리 |
|---:|---|---|
| 400 | 도메인 규칙 위반 | 요청 거부 |
| 401 | 인증 실패 | 처리하지 않음 |
| 403 | Scope·SoD·Verifier 위반 | 처리하지 않음 |
| 404 | 자원 없음 | 추정하지 않음 |
| 409 | 상태·버전·멱등성 충돌 | 재조회 후 명시적 재시도 |
| 422 | Schema·시간·수치 오류 | 요청 수정 |
| 429 | Rate·동시 Job 한도 초과 | 제한된 재시도 |
| 503 | DB·Redis·Ollama·외부 Adapter 불가 | fail-closed |
| 504 | 내부 Timeout | HOLD 또는 ESCALATE |

## 4. 부서 내부 Head·Worker 계약

### 4.1 worker-context.v1

Worker Context는 같은 부서 Head와 독립 LangGraph Worker Graph 사이의 비바인딩 실행 결과다. 전체 원문 대신 참조·해시를 전달하며 승인·Posting·권한 부여·상태 전이를 의미하지 않는다.

    {
      "context_id": "uuid",
      "schema_version": "risk.worker-context.v1",
      "department": "risk-management",
      "trace_id": "uuid",
      "case_id": null,
      "producer_worker": "pre-trade-risk-worker",
      "consumer_worker": "risk-supervisor",
      "status": "COMPLETED",
      "advisory": {"summary": "Risk evidence collected", "suggested_verdict": "RESIZE"},
      "reason_codes": ["MAX_SYMBOL_WEIGHT"],
      "input_refs": [{"type": "ORDER_INTENT", "id": "uuid", "content_hash": "sha256:..."}],
      "output_refs": [{"type": "RISK_ASSESSMENT", "id": "uuid", "content_hash": "sha256:..."}],
      "profile_version": "risk-worker-profile-v3",
      "created_at": "2026-08-04T00:30:00Z"
    }

schema_version은 공통 worker-context.v1 또는 부서 Alias를 사용하고, profile_version은 실행 Profile의 계보를 나타낸다. 두 값을 같은 의미로 사용하지 않는다. case_id는 Case 종속 실행에서만 값이며, Mandate·Workforce·Reporting 실행은 null 또는 생략할 수 있다.

허용 상태는 COMPLETED, DEGRADED, ESCALATE, REJECTED, HOLD다. DEGRADED·ESCALATE를 Head synthesis만으로 PASS·APPROVE로 승격하지 않는다. 바인딩 결과가 필요하면 해당 Domain의 결정론 Service를 호출하고 decision_ref 또는 output_ref를 남긴다.

### 4.2 부서 내부 역할

| 부서 | Worker → Head | 바인딩 Service |
|---|---|---|
| Research | data·market·news·evidence → research head | Evidence·Market 조회와 ResearchPacket 저장 |
| Quant | hypothesis·feature·backtest·cost → quant head | PIT Dataset·Backtest·Experiment 상태 |
| Trading | thesis·proposal·constraint·execution → trading head | OMS·Risk Decision 기록·Broker Adapter |
| Risk | liquidity·pre-trade·compliance·derivatives → risk head | Risk Engine·P1/P2·Trading State |
| QA | evidence·hallucination·model-risk·audit·ops → QA head | Evidence QA·Verifier·Finding·Incident |
| Governance | executive worker → CEO head | Mandate·Case·Approval·Escalation |
| Workforce | planning·profile·selection·lifecycle → HR head | Profile·Access·Lifecycle API |
| Accounting | ledger·valuation·reconciliation·reporting → accounting head | Ledger·Position·NAV·Reconciliation |

## 5. Domain API 핵심 Route

아래는 의미상 핵심 Route다. Method와 전체 operation의 실행 여부는 Route Registry가 판정한다.

### 5.1 Research·Quant

- Research Read: GET /evidence/news, /evidence/disclosures, /evidence/financials, /evidence/search, /evidence/stories, /macro/observations, /universe/restrictions
- Market Read: GET /snapshot/{symbol}, /bars/{symbol}, /breadth, /dq/*, /regime/daily, /microstructure/{symbol}
- Research Workflow planned: POST /investment-cases/{case_id}/research, GET /research/v1/jobs/{job_id}, GET /research/v1/packets/{packet_id}
- Quant planned: POST /quant/v1/hypotheses, POST /quant/v1/dataset-builds, POST /quant/v1/experiments, POST /quant/v1/experiments/{experiment_id}/submit-to-qa
- Quant는 PIT Dataset 인증·Backtest·ExperimentCard를 소유한다. SUPPORTED는 Production 전략 승인이 아니며 QA·Risk·CEO 승인이 필요하다.

### 5.2 Trading

- POST /trading/v1/order-intents는 OrderIntent를 만들고, Risk Decision을 받기 전에는 Broker Order를 만들 수 없다.
- POST /trading/v1/order-intents/{order_intent_id}/risk-decision 결과는 APPROVED, RESIZED, REJECTED다.
- POST /trading/v1/orders는 READY_TO_SUBMIT와 유효한 Risk Decision을 다시 확인한다.
- Broker Event 중복은 broker_event_id로 멱등 처리한다. 응답이 없으면 UNKNOWN이며 FILLED·CANCELLED로 추정하지 않는다.
- Position·Cash·Ledger는 OrderIntent가 아니라 Fill Event에서만 변한다. 현재 구현은 Paper OMS다.

### 5.3 Risk·QA

- Risk 핵심: POST /investment-cases/{case_id}/risk-check, POST /risk/v1/p1/external-snapshot, POST /risk/v1/p2/derivatives-check, /risk/v1/trading-state/{scope}, POST /risk/v1/compliance/check
- Risk Agent는 근거와 권고만 만들고 APPROVE·RESIZE·REJECT와 한도 집행은 결정론적 Risk Engine이 담당한다.
- QA 핵심: POST /investment-cases/{case_id}/qa-check, POST /qa/v1/evidence/check, POST /qa/v1/model-risk/evaluate, POST /qa/v1/internal-audit/evaluate, POST /qa/v1/ops/evaluate
- QA PASS/WARN/FAIL은 독립 verifier, citation·PIT·grounding, 승인된 corpus/profile 조건을 통과해야 한다. QA는 Risk·OMS·Ledger 원장을 직접 수정하지 않는다.

### 5.4 Governance·Workforce·Reporting

- Governance는 Mandate·Case·Approval·Escalation의 소유자다. Case Root는 /governance/v1/cases다.
- Workforce는 Roster·Profile·Access Request·Lifecycle을 소유한다. 작성자와 승인자가 같으면 403이다.
- Reporting은 승인된 Snapshot으로 Report를 생성하며 원천 수치를 수정하지 않는다.
- 현재 구현과 계획의 차이는 governance-api와 workforce-api의 actual/planned Route Registry를 따른다.

### 5.5 Accounting·Portfolio

- planned Domain API의 핵심은 /accounting/v1/ledgers, /accounting/v1/ledgers/{ledger_id}/fills, /journals, /trial-balance, /positions, /valuations, /reconciliations다.
- Ledger는 이중분개와 차대 균형을 결정론적으로 검사한다. Posted Journal에는 PUT·PATCH·DELETE가 없고 reverse만 허용한다.
- Fill·Capital·Corporate Action의 Domain 멱등 키를 보존한다. 계산 NAV는 공식 NAV가 아니며 승인된 valuation Snapshot과 독립 검증이 필요하다.
- 현재 Operator BFF의 GET /accounting/v1/portfolio-snapshot은 read-only Projection이며 Accounting Ledger 쓰기 권한이 없다.

## 6. 부서 간 Event 계약

### 6.1 event-envelope-v1

공통 Envelope Schema는 [event-envelope.v1.json](contracts/event-envelope.v1.json)이다.

    {
      "event_id": "uuid",
      "event_type": "research.packet.v1",
      "schema_version": "event-envelope-v1",
      "case_id": "uuid",
      "trace_id": "uuid",
      "producer": "research-api",
      "occurred_at": "2026-08-04T00:35:00Z",
      "idempotency_key": "research:case:packet:v3",
      "payload_ref": {
        "artifact_type": "RESEARCH_PACKET",
        "artifact_id": "uuid",
        "artifact_schema": "research-contracts-v2",
        "content_hash": "sha256:..."
      }
    }

event_type 버전과 payload_ref.artifact_schema 버전은 분리한다. event_id와 idempotency_key는 소비자가 저장해 중복을 제거한다. Event Bus는 at-least-once를 전제로 하며 Outbox 또는 동등한 재시도 가능한 발행, DLQ, 원인 코드를 사용한다.

case_id는 Case 종속 Event에서 required이며, Mandate·Workforce·Reporting 등 Case 비종속 Event는 null 또는 생략한다. payload_ref는 저장된 Artifact·Snapshot·Decision을 참조할 때 사용한다.

### 6.2 Canonical Event와 Internal Event

| Event | 상태 | Producer → Consumer | 용도 |
|---|---|---|---|
| risk.decision.v1 | Internal implemented | Risk → Trading, QA, CEO | 내부 Risk Decision |
| qa.decision.v1 | Internal implemented | QA → Trading, CEO, Workforce | 내부 QA Decision |
| trading.order_intent.v1 | Internal implemented | Trading → Risk, QA, Accounting | OrderIntent 전달 |
| governance.mandate.changed.v1 | Implemented | Governance → Risk, Trading, Quant, QA | Mandate Version 적용 |
| governance.case.created.v1 | Implemented | Governance → 관련 Domain | Case Pointer 전달 |
| report.ready.v1 | Implemented | Reporting → CEO, BFF | Report 생성 완료 |
| workforce.*.v1 | Internal implemented | Workforce/QA → Workforce·Platform·QA | 인력·권한 상태 |
| research.packet.v1 | Planned canonical | Research → Quant, QA, Trading, CEO | ResearchPacket 발행 |
| quant.dataset.certified.v1 | Planned canonical | Quant → QA, Trading | PIT Dataset 인증 |
| quant.experiment.completed.v1 | Planned canonical | Quant → QA, CEO | ExperimentCard 완료 |
| investment_case.risk_approved/resized/rejected.v1 | Planned canonical | Risk → QA, Trading, CEO | Case Risk 결과 |
| investment_case.qa_passed/warned/blocked.v1 | Planned canonical | QA → Trading, CEO | Case QA Gate 결과 |
| trading.fill.v1 | Planned canonical | Trading → Accounting, QA | Broker Fill |
| portfolio.snapshot.v1, nav.official.v1 | Planned canonical | Accounting → CEO, QA, BFF | Portfolio·NAV Snapshot |
| qa.finding.opened.v1, governance.decision.v1 | Planned canonical | QA/Governance → Owner·CEO | Finding·Decision |

전체 Event의 source·status·alias는 [Event Registry](contracts/event-registry.v1.json)를 따른다. 버전 없는 investment_case.risk_approved와 investment_case.qa_passed 같은 이름은 호환 수신 Alias일 뿐 신규 발행 이름이 아니다.

### 6.3 Event 호환성 관리

- Platform/API Architecture는 공통 Envelope, Event 이름, Version, Alias, Deprecation 기간을 관리한다.
- Producer Domain은 Payload Schema와 artifact_schema를 소유한다.
- Consumer 계약 검토 없이 기존 Event 의미를 변경하지 않는다. Breaking Change는 새 .vN Event를 만든다.
- Alias는 수신 기간·Canonical Event·제거 예정일을 Registry에 기록한다.
- Event 처리 실패는 PASS·APPROVE로 fallback하지 않고 Retry·DLQ·ESCALATE 중 하나로 종료한다.

## 7. 업무 흐름

### 7.1 전략 연구와 실시간 운용을 분리한다

전략 연구·승격 흐름:

    Research Evidence/Packet → Quant Dataset/Backtest → QA Strategy Review
      → CEO·Risk·QA 승인 Strategy Bundle → Trading OrderIntent
      → Risk Engine → QA Case Gate → Broker Order → Fill
      → Accounting Ledger/Portfolio → CEO Report

실시간 운용 흐름:

    Research → Trading OrderIntent → Risk deterministic Gate
      → QA Case Gate → Broker ACK/FILL → Accounting

실시간 흐름에서 Risk 뒤 QA Gate를 거치며 QA PASS 없이 Broker Submit을 하지 않는다. 전략 연구 흐름에서 Quant의 SUPPORTED를 Production 주문 승인으로 취급하지 않는다. Research·Quant·QA는 서로의 원장을 직접 수정하지 않는다.

### 7.2 실패 폐쇄

- Research·Market PIT 실패, 근거 부족, stale DQ는 결과를 만들지 않고 422·503·BLOCKED 중 적절한 상태로 남긴다.
- Risk Engine·P1/P2·Trading State 조회가 불가능하면 신규 진입을 차단하고 HOLD·ENTRY_BLOCKED·503을 반환한다.
- QA timeout·grounded=false·UNSUPPORTED·citation/PIT 실패는 PASS가 아니다.
- Broker 무응답은 UNKNOWN이다. Accounting은 Fill 없이 Position·Cash·NAV를 변경하지 않는다.
- Ledger·Position·NAV·Risk State·Profile Lifecycle의 실패는 자동 승인·자동 승격·권한 확대 방향으로 fallback하지 않는다.

## 8. 권한 경계

| 대상 | 소유자 | 금지된 위임 |
|---|---|---|
| Risk 승인·Resize·Reject·Trading State | Risk Engine | Risk Agent·Trading·QA가 결정론 Gate를 대체 |
| QA PASS/WARN/FAIL·Finding 종결 | QA Service + 독립 Verifier | Trading·Risk·CEO가 자기 승인을 대체 |
| OrderIntent·Broker Order | Trading OMS | trader-pm-agent가 Broker Submit 직접 수행 |
| Fill 기반 Journal·Position·NAV | Accounting Service | OrderIntent·Signal·CEO가 Ledger Posting |
| Mandate·Case·Approval | Governance/CEO Office | CEO가 Risk 승인·주문·NAV 확정 |
| Agent Profile·Access·Lifecycle | Workforce + IAM | HR Event가 실제 Credential을 직접 발급 |

CEO는 주문 제출, Risk 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 없다. Quant는 Production 승격을 직접 수행하지 않는다. Risk·QA·OMS·Ledger는 담당자가 같아도 권한을 합치지 않는다.

## 9. 구현 상태와 Production 활성화 조건

| 영역 | 현재 상태 | Production 활성화 조건 |
|---|---|---|
| Research Evidence·Market | Legacy Read API 실행 | PIT·production_authorized 매핑·ACL·재시작 복구 |
| Research Workflow·Quant | Contract/Worker 중심, FastAPI Route planned | 영속 Job·Dataset·Artifact 저장과 상태 Migration |
| Trading | 결정론 Paper OMS API 실행 | 영속 OrderStore·Broker Adapter·Service Auth·실계정 차단 검증 |
| Risk | API·Test 실행, Production blocked | P1/P2 실데이터·Redis/DB Outbox·Risk Gate 장애 테스트 |
| QA | API·Test 실행, Production blocked | 승인 corpus/profile·독립 Verifier·write-through·SoD |
| Governance·Workforce | 일부 API 실행, 일부 Route planned | Transport·RLS·Committee/Plan 저장소·IAM 연계 |
| Accounting·Portfolio | Ledger/Reconciliation Prototype, Domain FastAPI planned | 단일 영속 Ledger·Market Mark·Reconciliation·공식 NAV 승인 |

다음 조건이 모두 통과되기 전에는 Production 주문이나 공식 NAV를 활성화하지 않는다.

1. Route Registry와 각 app.openapi()의 exact comparison이 CI에서 통과한다.
2. Event Registry의 Producer·Consumer가 event-envelope-v1 필드를 검증하고 Outbox·DLQ·중복 제거를 갖춘다.
3. Auth/mTLS/Scope/RLS와 Risk·QA·OMS·Ledger SoD가 독립 테스트로 확인된다.
4. PIT·citation·numeric trace·replay·state transition이 결정론 테스트를 통과한다.
5. Risk·QA·OMS·Ledger 의존성 장애가 신규 진입 차단, HOLD, UNKNOWN, ROLLBACK 방향으로 닫힌다.
6. Broker/LS Credential, Production RAG corpus, 공식 NAV, ACTIVE Profile은 별도 승인 없이는 연결하지 않는다.

## 10. 최소 인수 시나리오

1. 같은 Idempotency-Key와 동일 본문은 같은 Job·OrderIntent·Ledger 결과를 반환하고, 다른 본문은 409다.
2. Route Registry에서 Method를 GET에서 POST로 바꾸면 계약 테스트가 missing/extra로 실패한다.
3. Planned Route를 실제 FastAPI에 임의로 추가하거나 actual에 등록하지 않으면 planned 분리 테스트가 실패한다.
4. PIT를 초과하는 Evidence·Market·Dataset 입력은 422 또는 BLOCKED이고 FACT로 Publish되지 않는다.
5. Risk Engine 장애·P1/P2 미준비·Trading State 장애는 APPROVE가 아니라 503과 신규 진입 차단을 반환한다.
6. Risk REJECT 또는 승인 수량을 초과한 Broker Order는 OMS가 거부한다.
7. Broker Event를 재전송해도 Fill·Ledger Posting·Position 변경은 한 번만 반영되고 무응답은 UNKNOWN이다.
8. Fill 없이 Accounting Position·Cash·NAV가 변경되지 않으며 Posted Journal은 reverse만 가능하다.
9. QA grounded=false·UNSUPPORTED·citation/PIT 실패·timeout은 PASS가 아니다.
10. Worker DEGRADED/ESCALATE Context가 Head를 통과해도 APPROVE/PASS로 자동 승격되지 않는다.
11. HR 개선 Candidate의 작성자와 승인자가 같으면 403이고 Access Request가 Credential을 직접 만들지 않는다.
12. Event를 중복 소비해도 Ledger Posting·Profile Transition·QA Finding이 중복 생성되지 않는다.

### 10.1 Operator BFF의 부서·통신 Projection

`GET /ui/snapshot`은 기존 Trading·Portfolio Read Model에 선택적 `operations` 블록을 포함할 수 있다. 이 블록은 브라우저가 Hermes Profile, Worker Registry, Event Registry를 직접 읽지 않도록 하는 읽기 전용 Projection이다.

- `operations.schema_version`: `operator-operations.v1`
- `operations.departments`: Hermes Profile 이름, Worker 수·모델·output contract와 runtime 상태를 포함한다. Kanban Status Bridge와 heartbeat가 연결되지 않은 환경에서는 `status: OFFLINE`, `runtime_observed: false`를 사용한다.
- `operations.communications`: Event Registry의 producer·consumer·layer·status를 포함한다. Registry 항목은 live message가 아니므로 `live: false`, `transport: registry-only`로 표시한다.
- `operations.message_count`: 실제로 BFF가 관측한 live message 수이며 Registry 항목 수가 아니다.
- `operations.runtime_connected`와 `operations.event_bridge_connected`가 모두 `true`가 아니면 화면은 runtime 상태나 Event 수신을 정상 상태로 표시하지 않는다.

### 10.2 BFF 포트폴리오 추천 실행

사용자 적합성 입력은 `POST /ui/portfolio-recommendations`로 전달한다. BFF는 기존
`portfolio-recommendation-full` LangGraph를 비동기로 실행하고 `run_id`를 반환한다.

- 입력은 `mindset`, `experience`, `investment_horizon_years`, `max_drawdown_pct`,
  `investment_amount`, `currency`를 포함한다. 목표 금액은 서버가 결정론적으로 계산한다.
  `liquidity_need`는 선택적 내부 적합성 조건이며 생략하면 `MEDIUM`으로 정규화한다.
- 결과 조회는 `GET /ui/portfolio-recommendations/{run_id}` 또는 `GET /ui/snapshot`의
  `operations.runtime` projection을 사용한다.
- 추천 결과가 `MATCHED`이고 pipeline이 `COMPLETED`인 경우 `POST
  /ui/portfolio-recommendations/{run_id}/approval`에서 사용자가 `APPROVE` 또는
  `REJECT`할 수 있다. 이는 추천 자문 승인 기록이며 주문 승인과 다르다.
- 결과는 `portfolio suitability` 자문이며 `binding: false`, `production_enabled: false`,
  `manual_review_required: true`를 유지한다. 주문 제출·Risk 승인·Ledger Posting은 이 경로에서 수행하지 않는다.
- 실제 LangGraph run이 없을 때 `operations.runtime.status`는 `OFFLINE`이고, 브라우저는
  직원 이동·착석 작업·대화를 시작하지 않는다.
- 부서 간 handoff는 `operations.runtime.active_handoff`로 노출하며 producer/consumer
  부서장만 참여한다. Worker의 완료 요약은 줄바꿈을 제거한 단일 문장으로 `messages`에 기록한다.

### 10.3 BFF 연동 상태

`GET /ui/integrations`는 Notion·Discord·향후 외부 연동의 설정 준비 상태만 반환한다.
토큰·Webhook URL·파일 경로 등 비밀값은 응답에 포함하지 않는다. 브라우저는 이 Projection을
사용해 연결됨·미설정·OAuth 대기 상태를 표시하며, 외부 발행은 별도 Worker의 명시적 사용자
동작으로 제한한다.

## 11. 연계 문서

- [Route Status Registry](contracts/route-registry.v1.json)
- [Event Registry](contracts/event-registry.v1.json)
- [event-envelope.v1](contracts/event-envelope.v1.json)
- [worker-context.v1](contracts/worker-context.v1.json)
- [Risk·QA Domain API Specification](RISK_QA_DOMAIN_API_SPEC.md)
- [Worker 역할·권한 경계](WORKER_ROLE_BOUNDARIES.md)
- [Department Worker Graph Architecture](DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)
- [Database Schema Foundation](../database/README.md)
- [Investment Minimum Service Unit Specification](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md)
- RESEARCH_QUANT_DOMAIN_API_SPEC.md
- TRADING_DOMAIN_API_SPEC.md
- GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md
- ACCOUNTING_PORTFOLIO_DOMAIN_API_SPEC.md
