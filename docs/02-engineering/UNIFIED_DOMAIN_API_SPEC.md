# HgFinance Unified Domain API Specification

문서 상태: 통합 기준안 v1.2 (실행 계약 연결)

기준일: 2026-08-10

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
| UI·Operator | Read-only Projection + 고정 local-fixture PAPER Command BFF | 원장·Risk·OMS·Credential을 소유하지 않으며, ADR-0007의 좁은 fixture authority만 전달. 로그인·세션을 만들지 않음 |

Worker를 별도 프로세스나 Queue로 분리해도 내부 계약은 worker-context.v1을 유지한다. Worker가 새로운 공개 Business API나 권한 우회 경로가 되어서는 안 된다.

## 2. 5개 명세군과 Route Namespace

5개 명세군은 실제로 여러 FastAPI와 BFF로 분리된다. 실행 Route의 전체 목록과 Method는 Route Registry에만 중복 기록한다.

| 명세군 | 주요 실행 서비스 | Route Namespace |
|---|---|---|
| Research·Quant | Research Evidence, Market Read, Research Workflow, Quant | Legacy Read, /research/v1, /quant/v1 |
| Trading | Paper OMS·Broker Adapter + 사용자 PAPER BFF | /trading/v1, /investment-cases/{case_id}/paper-orders, /ui/paper-orders, /trading/agent/order |
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

### 2.2 실행 Route와 구현 상태의 정본

App별 `actual_routes`, `planned_routes`, `excluded_routes`와 operation 수는
[Route Status Registry](contracts/route-registry.v1.json)가 소유하며 각 앱의
`app.openapi()`와 exact comparison으로 검증한다. 이 문서에는 변동 수치를 복제하지
않는다. 영역별 준비도와 Production 차단 조건은
[Project Implementation Status](../PROJECT_IMPLEMENTATION_STATUS.md)를 따른다.

## 3. 공통 API 계약

### 3.1 인증과 권한

- Production 목표는 Service Token과 mTLS다. Token Subject의 department, service, scopes를 검증한다.
- 현재 구현은 Risk Trading State 명령과 QA Corrective Action 종결에 대해 짧은 수명의 HS256 Bearer Service Token(`sub`, `department`, `service`, `scopes`, `exp`)을 검증한다. 전역 Issuer/JWKS·mTLS·IAM 및 `sub`와 `agent_id/profile_version_id` 매핑은 배포 전 연결 대상이며, 이 명령 단위 검증만으로 Production 인증 완료로 간주하지 않는다.
- Scope 형식은 department.resource.action.read 또는 department.resource.action이다. Fund·Book·Case 범위를 벗어나면 403이다.
- Browser와 Agent는 Domain API를 직접 공개 호출하지 않는다. BFF 또는 승인된 내부 Service/MCP Gateway를 사용한다. 로컬 fixture PAPER mutation은 Operator BFF 경계에서만 다룬다.
- CEO·Worker·LLM에게 Supabase Service Role, Broker Credential, LS Credential을 제공하지 않는다.
- Agent Tool은 allow-list된 제안·조회·검증 도구만 노출한다. 주문 Submit, Risk 결정 적용, Ledger Posting, 권한 Provisioning은 Agent Tool이 직접 수행하지 않는다.

#### 3.1.1 고정 local-fixture 사용자 PAPER authority

[ADR-0007](adr/0007-authenticated-user-paper-directive-authority.md)이 자동 주문과
사용자 직접 주문의 권한, admission, 집계 상태 및 broker 정본을 소유한다. 이 API
명세에는 전송 경계만 둔다.

- BFF는 요청 본문의 `user_id`가 아니라 고정 local fixture를 actor로 결합한다. 별도
  로그인·가입·세션은 만들지 않는다.
- `/trading/agent/order`도 같은 fixture 호환 ingress이며 Agent submit route가 아니다.
- mutation은 `Idempotency-Key`, `mode=PAPER`, 최소 claim의 내부 service proof를
  요구한다. 사용자 token이나 LS credential은 downstream으로 전달하지 않는다.
- Trading service는 LS PAPER 주문·취소·상태조회만 허용한다. LS LIVE mutation route는
  없다.
- Agent·alpha·전략 Worker가 만든 자동 주문 후보는 이 예외 경로가 아니며 기존 Risk
  Decision을 통과해야 한다.

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
      "producer_worker": "compliance-policy-worker",
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

위 흐름은 `AUTOMATED_STRATEGY` 주문에 대한 불변식이다. 고정 local-fixture 사용자의 명시적
PAPER 명령은 다음의 별도 전송 흐름이며 자동 전략과 권한 증거를 공유하지 않는다.

    User natural-language command → fixed fixture ID + exact Fund/Book admission
      → CEO Kanban root → Trading Hermes non-binding candidate
      → exact-text/digest/evidence deterministic verifier
      → current Fund/Book membership + mechanical admission + idempotency
      → durable USER_DIRECTIVE → LS PAPER adapter/OMS

세부 authority, mechanical admission, `SELL_ALL`·`CANCEL_ALL` 의미와 집계 상태는
[ADR-0007](adr/0007-authenticated-user-paper-directive-authority.md)을 따른다.

### 7.2 실패 폐쇄

- Research·Market PIT 실패, 근거 부족, stale DQ는 결과를 만들지 않고 422·503·BLOCKED 중 적절한 상태로 남긴다.
- 자동 주문에서 Risk Engine·P1/P2·Trading State 조회가 불가능하면 신규 진입을 차단하고 HOLD·ENTRY_BLOCKED·503을 반환한다. 사용자 PAPER 지시는 Risk verdict를 요구하지 않지만 fixture binding·내부 service proof·canonical account·durable store·mechanical admission 중 하나라도 불확실하면 동일하게 fail closed한다.
- QA timeout·grounded=false·UNSUPPORTED·citation/PIT 실패는 PASS가 아니다.
- Broker 무응답은 UNKNOWN이다. Accounting은 Fill 없이 Position·Cash·NAV를 변경하지 않는다.
- Ledger·Position·NAV·Risk State·Profile Lifecycle의 실패는 자동 승인·자동 승격·권한 확대 방향으로 fallback하지 않는다.

## 8. 권한 경계

| 대상 | 소유자 | 금지된 위임 |
|---|---|---|
| Risk 승인·Resize·Reject·Trading State | Risk Engine | Risk Agent·Trading·QA가 결정론 Gate를 대체 |
| 명시적 사용자 PAPER directive | 고정 데모 ID; BFF/parser는 전달·구조화, Trading Domain은 mechanical admission/실행 | Hermes·LLM·alpha·rebalancer가 지시를 만들거나 변경; Risk가 경제적 veto/resize; LIVE로 승격 |
| QA PASS/WARN/FAIL·Finding 종결 | QA Service + 독립 Verifier | Trading·Risk·CEO가 자기 승인을 대체 |
| OrderIntent·Broker Order | Trading OMS | trader-pm-agent가 Broker Submit 직접 수행 |
| Fill 기반 Journal·Position·NAV | Accounting Service | OrderIntent·Signal·CEO가 Ledger Posting |
| Mandate·Case·Approval | Governance/CEO Office | CEO가 Risk 승인·주문·NAV 확정 |
| Agent Profile·Access·Lifecycle | Workforce + IAM | HR Event가 실제 Credential을 직접 발급 |

CEO는 주문 제출, Risk 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 없다. Quant는 Production 승격을 직접 수행하지 않는다. Risk·QA·OMS·Ledger는 담당자가 같아도 권한을 합치지 않는다.

## 9. API Production 활성화 조건

영역별 현재 구현 상태와 준비도는
[Project Implementation Status](../PROJECT_IMPLEMENTATION_STATUS.md)가 소유한다. 이
문서에는 API·Event 경계에서 공통으로 확인할 활성화 조건만 둔다. 다음 조건이 모두
통과되기 전에는 Production 주문이나 공식 NAV를 활성화하지 않는다.

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
13. 자동 주문과 고정 local-fixture PAPER directive의 분리, batch 상태, LS PAPER/LIVE
    경계는 [ADR-0007](adr/0007-authenticated-user-paper-directive-authority.md)의
    acceptance 및 계약 테스트를 통과한다.

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

### 10.4 BFF Agent Status·Domain Projection·안전 Command

`agent.status.v1`은 BFF 내부의 Status Projector가 sequence를 부여해 `/ui/snapshot`의 `operations.agent_statuses`와 `/ws/operations`로 투영한다. WebSocket이 끊기거나 sequence gap이 감지되면 브라우저는 이벤트를 추측하지 않고 `/ui/snapshot`을 다시 읽는다.

대시보드 전용 Domain Read Model은 `/ui/research`, `/ui/strategy`, `/ui/risk`, `/ui/qa`, `/ui/risk-qa`에서 제공한다. 실제 runtime event가 관찰되지 않은 domain은 `DEGRADED`로 남으며 registry metadata를 실행 상태로 표시하지 않는다.

투자 본부 Agent 질의는 `/research/agent/ask`, `/trading/agent/ask`, `/risk/agent/ask`, `/quant/agent/ask`, `/accounting/agent/ask`, `/qa/agent/ask`에 부서별 Profile을 고정한다. `ENABLE_AGENT_ASK`가 꺼진 기본 환경에서는 모두 503이며, 요청 본문의 department로 임의 라우팅하지 않는다.

`POST /ui/commands/trading-state`는 `idempotency_key`·`expected_version`·Audit Event를 검증하는 승인 요청 접수 계약이다. 현재 BFF는 `PENDING_APPROVAL`과 `NOT_EXECUTED`만 반환하고 OMS·Risk Engine·Broker·Ledger를 변경하지 않는다.

### 10.5 BFF 유니버스·자유 질의 라우팅

`GET /ui/portfolio-universes`는 프론트가 종목 목록을 직접 하드코딩하지 않도록 백엔드가 소유한 선택 가능한 투자 유니버스 메타데이터를 반환한다. 현재 선택 가능한 유니버스는 국내 주식 `KOREA_EQUITY_WATCHLIST` 하나이며, 정적 목록은 DB가 없는 TEST 실행에서만 사용한다. 근거 없는 예상 수익률을 임의로 채우지 않는다.

`POST /ui/portfolio-recommendations`는 구조화된 적합성 입력과 함께 `universe_id`와 자유 형식 `query`를 받을 수 있다. 백엔드는 원문을 보존하고 안전한 범위의 CEO task plan을 만들어 필요한 부서만 Worker Graph를 호출한다. 결과의 `task_plan`에는 `original_query`, `rewritten_query`, `requested_departments`, `matched_terms`가 기록된다. Query rewriting은 부서 선택·설명에만 사용하며 주문, Risk 승인, 원장 변경을 만들 수 없다.

추천 결과의 `instrument_recommendations`는 `portfolio_id`, `symbol`, `exchange`, `name`, `asset_class`, `currency`, `target_weight`, `target_amount`, `expected_return`, `expected_return_status`, `expected_return_basis`, `data_status`, `evidence_refs`를 포함한다. `TEST` 또는 PIT 검증 시장 근거가 없는 상태에서 `expected_return`은 `null`이어야 하며 수익률 보장 문구를 표시하지 않는다.

같은 `POST /ui/portfolio-recommendations`는 다음 선택 입력도 받는다.

- `category`: `PORTFOLIO_RECOMMENDATION`, `MARKET_RESEARCH`, `RISK_REVIEW`, `TAX_LIQUIDITY`, `REBALANCING_PROPOSAL`, `STRATEGY_PROPOSAL` 중 하나다. **다만 서버는 이 목록을 `Literal`로 강제하지 않는다** — 목록 밖 문자열이 와도 422가 아니라 더 넓은 부서 집합으로 fallback하고, 응답 `task_plan.category_recognized: false`로 그 사실을 남긴다. 대화형 제품에서 새 의도가 표보다 먼저 도착하므로 질문을 통째로 실패시키지 않되, 조용히 넘기지도 않는다는 뜻이다(근거: [CEO_CONVERSATIONAL_ROUTING_SPEC.md](CEO_CONVERSATIONAL_ROUTING_SPEC.md) 2.5).
- `STRATEGY_PROPOSAL`은 `task_plan.workflow`가 `strategy-research`로 나온다 — 이 그래프가 정식 처리 주체가 아니라는 표시다. 현재 BFF는 아직 워크플로별 디스패치를 하지 않으므로 자문 전용 축소 집합(`research`·`qa`·`ceo`)만 실행된다(같은 문서 3.1).
- `include_stock`: 주식 종목·배분 표시 여부이며 기본값은 `true`다.
- `include_derivatives`: 파생상품 종목·배분 표시 여부이며 현재 국내 주식 전용 범위에서는 기본값이 `false`다.
- `query`: 사용자가 자유롭게 작성하는 투자 질문·조건이다. 빈 문자열이어도 되며, 카테고리와 구조화된 프로필만으로 기본 라우팅한다.

프론트엔드 공개 자산 Projection은 현재 국내 주식만 포함한다. 파생상품 토글은 계약 호환성을 위해 유지하지만 국내 주식 전용 유니버스에서는 후보를 만들지 않는다. 채권·현금성 자산은 `instrument_recommendations`와 사용자 화면에 노출하지 않는다. 두 토글이 모두 꺼지거나 PIT 국내 종목이 없으면 종목 결과는 `UNAVAILABLE`, `safe_action`은 `HOLD`가 된다.

CEO 라우터는 `category`를 최소 부서 집합의 시작점으로 삼고 `query`의 의도를
결정론적으로 정규화한다. 이 값은 부서·Worker 배정과 설명에만 사용한다.

`GET /ui/snapshot`의 `operations.runtime`는 `run_id`, `active_workers`, 부서별 `status`, `department_reports`, 최근 실행 메시지를 제공한다. 프론트엔드는 이를 Kanban Projection으로 표시하며, `SKIPPED` 부서는 요청 범위에 포함되지 않은 부서로 표시한다. 실행이 완료된 뒤에도 결과의 출처는 새 `run_id`로 식별하며 캐시된 금융 상태로 간주하지 않는다.

### 10.6 BFF 응답·수치·국내 유니버스 계약 보완

- BFF 포트폴리오 경로의 `POST /ui/portfolio-recommendations`, `GET /ui/portfolio-recommendations/{run_id}`, `POST /ui/portfolio-recommendations/{run_id}/approval`, `GET /ui/portfolio-universes`는 `apps/api/portfolio_schemas.py`의 Pydantic response DTO를 사용한다. 외부 응답 envelope와 `result` core는 `extra=forbid`이며, 새 필드는 DTO·OpenAPI·계약 테스트를 함께 갱신하는 additive change로만 추가한다.
- `max_drawdown_pct`는 퍼센트가 아닌 비율이다. `0.10`은 최대 손실률 10%를 뜻하며 `10`은 유효하지 않다. 허용 범위는 `0 < value <= 1`이다.
- 현재 제품 범위는 국내 주식이며, 기본 `KOREA_EQUITY_WATCHLIST`는 `KOREA_EQUITY`만 노출하고 채권·글로벌 주식·파생상품·현금성 자산을 후보 목록에 포함하지 않는다.
- Supabase live 실행에서는 `reference.instruments`와 `reference.instrument_symbols`를 `execution.market_snapshots`와 Point-in-Time 조인해 티커를 만든다. 연결 실패나 PIT 종목 부재 시 정적 TEST 카탈로그로 조용히 대체하지 않고 `UNAVAILABLE/HOLD`로 종료한다.
- 기본 로컬 BFF만 실행할 때는 `PORTFOLIO_WORKER_RUNTIME=ollama`, `OLLAMA_BASE_URL=http://localhost:11434/v1`, `OLLAMA_CHAT_MODEL=qwen3:1.7b`를 사용한다. 운영 model overlay는 Worker Model Gateway의 Qwen AWQ 좌표를 주입한다. 어느 경로든 모델 장애·계약 오류는 `DEGRADED/HOLD`이며 자동 승격하지 않는다.

### 10.7 BFF CEO Kanban 질의 Surface

CEO 자연어 질의와 그 실행 추적은 `/ui/ceo/*` Route가 담당한다. 일반 자문 경로는
**Hermes Kanban을 실행 Source of Truth로 삼는 읽기 전용 Projection + Task 생성
경계**다. 다만 명시적 주문 문장은 별도 `user_paper_order` 표시 root와
Trading primary를 durable DB에 먼저 결합한다. Trading Hermes 결과의
`binding: false` 해석 자체에는 주문 권한이 없으며, 서버의 exact-text 검증과 현재
Fund/Book 재인가를 모두 통과한 경우에만 PAPER OMS admission으로 이어진다.

| Route | Method | 소유 모듈 | 역할 |
|---|---|---|---|
| `/ui/ceo/ask` | POST | `ceo_mirror_api.py` | CEO root Kanban Task 생성. dedup + mirror event journal이 `ceo.ceo_query`를 감싼다 |
| `/ui/ceo/ingress` | POST | `ceo_mirror_api.py` | Web/Discord 공용 canonical ingress. 같은 dedup 경로 |
| `/ui/ceo/events` | GET·POST | `ceo_mirror_api.py` | request_id 기준 mirror event 조회 / sanitized event 발행 |
| `/ui/ceo/events/stream` | GET | `ceo_mirror_api.py` | 단명 SSE. 클라이언트는 마지막 event_id cursor로 재연결 |
| `/ui/ceo/tasks` | GET | `ceo.py` | 계정별 root Task 목록. `owner_id` 필터는 **서버가** 적용한다 |
| `/ui/ceo/tasks/{task_id}` | GET | `ceo.py` | 상태 + planning projection |
| `/ui/ceo/tasks/{task_id}/graph` | GET | `ceo.py` | 워크플로 그래프 node·edge |
| `/ui/ceo/tasks/{task_id}/result` | GET | `ceo.py` | Synthesis 요약·decision·QA verdict |
| `/ui/ceo/tasks/{task_id}/archive` | POST | `ceo.py` | Archive. **DELETE는 의도적으로 없다** - 기록을 지우지 않는다 |
| `/ui/paper-order-requests/{order_request_id}` | GET | `user_orders.py` | CEO→Trading→PAPER OMS 상태와 Accounting ACK까지 추적 |

- `POST /ui/ceo/ask`의 등록 지점은 `ceo_mirror_api.py` 하나다. `ceo.ceo_query`는 route를 스스로 등록하지 않는 순수 함수이며, 같은 (path, method)를 두 라우터가 나눠 갖고 등록 순서로 승부하는 구조를 금지한다(`tests/api/test_main_routes.py`가 중복 등록을 차단한다).
- `X-User-Id`는 인증이 아니다. 그럼에도 `owner_id` 필터링을 **서버 측에서** 수행한다 - 클라이언트가 전체 목록을 받아 걸러내면 다른 계정의 질의·답변 텍스트가 네트워크 응답에 그대로 실린다.
- root Task body의 `requested_by=` 줄이 없는 과거 Task는 "계정 불명"으로 남기고 어떤 `owner_id` 필터에도 포함하지 않는다. 기본값을 지어내지 않는다.
- Kanban CLI를 쓸 수 없으면 503이다. 목록·그래프·결과를 빈 값이나 캐시로 대체하지 않는다.

### 10.8 BFF Governance·적합성·계좌 Proxy Surface

아래 경로군은 BFF가 소유한 계산이 아니라 **Domain API로 넘기는 pass-through Projection**이다. BFF는 governance·accounting 원장을 직접 쓰지 않는다.

| Route 그룹 | Method | 대상 Domain |
|---|---|---|
| `/ui/mandates`, `/ui/mandates/{id}/current`, `/ui/mandates/{id}/versions`, `/ui/mandates/{id}/change-requests`, `/ui/mandates/by-fund/{fund_id}/current` | POST·GET | `GOVERNANCE_API_URL` (governance-api) |
| `/ui/mandate-cases/{id}/advance`, `/ui/mandate-cases/{id}/timeline` | POST·GET | governance-api |
| `/ui/mandate-approvals`, `/ui/mandate-approvals/{id}/decide` | GET·POST | governance-api |
| `/ui/mandate-assistant/suggest` | POST | governance-api (stateless 제안, 저장 없음) |
| `/ui/investor-profiles`, `/ui/investor-profiles/current` | POST·GET | `PORTFOLIO_API_URL` (accounting-api) |
| `/ui/account/snapshot` | GET | Broker(LS) 조회, `authoritative: false` |
| `/ui/risk/mandates/{mandate_id}/assess` | POST | `RISK_API_URL` |
| `/ui/qa/verifications/{verification_id}/assess` | POST | `QA_API_URL` |
| `/ui/commands/audit` | GET | BFF 내부 Command Audit Event 조회 |

- **투자성향(`mindset`)·투자경험(`experience`)은 Mandate가 아니라 적합성 프로필에 있다.** `accounting.investor_profiles`가 그 저장소이고 `/ui/investor-profiles/current`가 조회 경로다. `GET /ui/mandates/.../current`의 `policy`에는 숫자 한도(`risk_bounds`·`universe_policy` 등)만 있으며 성향·경험 필드가 존재하지 않는다 - 두 값을 같은 곳에서 찾지 않는다.
- Domain API 미설정·연결 실패는 인메모리 후퇴 없이 503(`governance_api_unavailable`/`portfolio_api_unavailable`)이다.
- `/ui/account/snapshot`은 Broker 조회 Projection이며 공식 NAV·원장 잔고가 아니다.

### 10.9 BFF Broker(LS) Read-only Projection Surface

대시보드의 계좌·주문·체결·원장·시장 상위는 이 세 경로에서 온다. **프론트엔드는 브로커를 알지 못한다** — LS OpenAPI를 직접 호출하지 않으며 `ai-office/` 어디에도 LS Credential이나 TR 코드가 없다. 화면은 접수·체결·정정·취소·거부라는 도메인 어휘만 받는다.

| Route | Method | 원천 | Cache |
|---|---|---|---|
| `/ui/portfolio/live` | GET | `CDPCQ04700`(거래내역) + `CSPAQ13700`(주문조회) + `SC0`(실시간 체결) 병합 | `LS_ORDER_HISTORY_CACHE_SECONDS` 기본 3초 |
| `/ui/portfolio/ledger` | GET | 계좌 거래내역 원장 + durable 저장분 | `ACCOUNTING_LEDGER_CACHE_SECONDS` 기본 60초 |
| `/ui/market/rankings` | GET | LS `/stock/high-item` (`kind`=volume·change·amount) | `LS_MARKET_RANKING_CACHE_SECONDS` 기본 15초 |

- 셋 다 `authoritative: false`다. 브로커 장부이지 우리 원장이 아니며 공식 NAV·원장 잔고를 대체하지 않는다(`/ui/account/snapshot`과 같은 규칙).
- **읽기 전용이다.** 이 Surface에는 주문 제출 경로가 없다. 사용자 PAPER 주문은 §3.1.1의 `USER_DIRECTIVE` authority를 따르는 별도 경로이며 여기에 섞지 않는다.
- `/ui/portfolio/live`(주문·보유)와 `/ui/portfolio/ledger`(확정 거래와 비용·세금)는 **원천이 다르므로 한 응답에 합치지 않는다.** 트레이딩과 회계가 각자 축으로 본다.
- 조회 실패는 502이며 "거래 없음"으로 위장하지 않는다. 빈 배열은 실제로 거래가 없다는 뜻이다.
- 금액·수량은 문자열로 내려간다. JavaScript number는 double이라 Decimal이 깨진다.

#### 10.9.1 활성화 Gate와 Credential 경계

각 경로는 독립 Gate를 쓴다: `/ui/portfolio/live`는 `ENABLE_LS_ORDER_EVENTS`, `/ui/portfolio/ledger`는 `ENABLE_LS_ACCOUNT_DATA`, `/ui/market/rankings`는 `ENABLE_LS_MARKET_DATA`다(모두 기본값 false). 값은 프로세스 기동 시 한 번만 읽으므로 변경 후 재시작이 필요하다.

Credential은 BFF 프로세스에만 존재한다. 주입 경로에 함정이 하나 있다:

- 저장소 루트 `.dockerignore`가 `.env*`를 제외하므로 **`.env`는 이미지에 들어가지 않는다.** 컨테이너 안에서 `apps/api/main.py`의 `load_dotenv`는 아무것도 읽지 못한다.
- 따라서 컨테이너 배포에서는 Compose `environment:`에 **명시적으로 나열한 키만** 프로세스에 도달한다. `.env`에만 값을 넣으면 로컬 host uvicorn에서는 동작하고 Docker/EB에서는 503이 되는 비대칭이 생긴다.
- 시장 순위와 계좌 조회는 단일 `LS_ENV`를 쓴다. 기본값은 `LIVE`이며 같은 환경의 App Key/Secret, REST URL, WebSocket URL만 선택한다.
- `LS_ENV=PAPER`면 `_PAPER` 접미사 자격만 사용하고, `LS_ENV=LIVE`면 접미사 없는 자격만 사용한다. 환경을 바꾼 뒤 이전 토큰이 재사용되지 않도록 토큰 캐시는 환경·App Key별로 분리한다.

#### 10.9.2 원장 durable 저장

체결일과 결제일(T+2) 사이에는 **어떤 브로커 조회로도 그 거래를 다시 가져올 수 없다.** 본 것을 적어 두지 않으면 날짜가 바뀔 때 장부가 빈다. `ACCOUNTING_LEDGER_DB`가 그 저장소이고, 컨테이너 교체로 사라지지 않도록 Compose가 `portfolio_runtime_data` 볼륨 안 경로로 고정한다(상대경로 금지).

이 SQLite는 회계본부의 공식 원장이 아니라 브로커가 말해 준 것을 잃지 않기 위한 보관분이다. `settlement` 필드가 `SETTLED`/`UNSETTLED`로 둘을 구분하며, 회계는 이 둘을 같은 줄로 취급하지 않는다.

## 11. 연계 문서

- [Route Status Registry](contracts/route-registry.v1.json)
- [Event Registry](contracts/event-registry.v1.json)
- [event-envelope.v1](contracts/event-envelope.v1.json)
- [worker-context.v1](contracts/worker-context.v1.json)
- [Risk Mandate Worker Flow](RISK_MANDATE_WORKER_FLOW.md)
- [Worker 역할·권한 경계](WORKER_ROLE_BOUNDARIES.md)
- [Department Worker Graph Architecture](DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)
- [Database Schema Foundation](../database/README.md)
- [Investment Minimum Service Unit Specification](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md)
- Research·Quant 계약은 이 문서의 Research/Quant 절과 실제 API를 기준으로 한다.
- Trading 계약은 이 문서의 Trading 절과 실제 API를 기준으로 한다.
- `governance-api`·`workforce-api` 계약은 이 문서의 Governance/Workforce 절과 실제 API를 기준으로 한다.
- Accounting/Portfolio 계약은 이 문서의 Accounting/Portfolio 절과 실제 API를 기준으로 한다.
