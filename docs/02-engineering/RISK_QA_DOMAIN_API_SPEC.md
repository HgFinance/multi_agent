# Risk·QA Domain API Specification

검토일: 2026-08-04
소유: Risk·QA Domain Owner
상태: TEST에서 실행 가능, PRODUCTION은 명시적 게이트가 없으면 비활성

이 문서는 Risk 본부와 AI-QA·감사 본부의 FastAPI 경계를 정의한다. 결정론적 판정은 각 Domain Engine이 소유하고, API는 입력 검증·호출·영속화·Event 발행·관측성만 담당한다. Worker Graph가 API를 우회해 Risk 결정, QA 판정, DB 상태 전이를 직접 수행하지 않는다.

## 0. 현재 구현 기준

### 0.1 구현 상태

| 영역 | 현재 상태 | 비고 |
|---|---|---|
| Risk `risk-check` | 구현·self-check 통과 | `RiskEngine.check_order`가 승인·축소·거부를 결정 |
| Risk P1 snapshot | 구현 | `/risk/v1/p1/external-snapshot`; 실제 DB/LS 연동은 환경 게이트 필요 |
| Risk P2 derivatives | 구현 | `/risk/v1/p2/derivatives-check`; 결정론적 margin·volatility·Greeks·stress gate |
| Risk Trading State | 구현 | Redis가 없으면 상태 API는 `503` fail-closed |
| Risk Compliance RAG | baseline 구현 | `skills/agentic-rag`의 PIT·citation·retry 계약 사용 |
| QA Evidence Gate | 구현·TEST 활성 | `/investment-cases/{case_id}/qa-check`; PRODUCTION은 별도 승인 플래그 필요 |
| QA Evidence RAG | baseline 구현 | `/qa/v1/evidence/check`; 실제 corpus가 `SAMPLE_PLACEHOLDER`이면 운영 근거로 사용 금지 |
| QA Model Risk | 결정론 API 구현 | `/qa/v1/model-risk/evaluate` |
| QA Internal Audit | 결정론 API 구현 | `/qa/v1/internal-audit/evaluate` |
| QA Worker Graph | TEST skeleton 구현 | 5개 Worker가 조건부 signal에 따라 실행되며 결과는 advisory·trace 용도 |
| Trace·Tool·Incident | API/인메모리 구현 | `DATABASE_URL`이 주입되면 write-through를 시도 |
| PIKE-RAG·Hyper-Extraction API | 미구현 | 현재는 Skill/Router 설계와 백로그만 존재 |

### 0.2 TEST와 PRODUCTION

두 가지 실행 경계를 혼동하지 않는다.

| 구분 | TEST | PRODUCTION |
|---|---|---|
| 입력 | synthetic `ResearchPacket v1` fixture | 승인된 Research/API/DB 입력 |
| LLM | 결정론적 Qwen-shaped test stub 또는 별도 Ollama 검증 | 승인된 Ollama Worker와 Hermes Head |
| DB/Event | 계약 검증용 optional adapter | Supabase·Redis·필수 migration·RLS·Event Bus 필수 |
| Risk/QA gate | `binding=false` skeleton; 실제 주문·원장 변경 없음 | acceptance와 승인된 adapter가 모두 있어야 활성 |
| QA `qa-check` | 기본 `RISK_QA_RUNTIME=test`에서 호출 가능 | `RISK_QA_RUNTIME=production` 및 `QA_CHECK_CONTRACT_APPROVED=true` 필요 |
| 실패 방향 | `DEGRADED`, `HOLD`, `ESCALATE` | 신규 진입 차단, `HOLD`, `REJECT`, `ESCALATE` |

통합 실행기는 다음 명령으로 관리한다.

```bash
python scripts/run_risk_qa_test_pipeline.py --mode test
python scripts/run_risk_qa_test_pipeline.py --mode production
```

현재 `--mode production`은 의도적으로 `OFF/HOLD`를 반환하고 Worker를 실행하지 않는다. 기존 본부 self-check의 `--run`도 `RISK_QA_PRODUCTION_ENABLED=true`가 없으면 실제 실행하지 않는다. 이 문서의 API가 존재한다는 사실만으로 Production이 승인된 것은 아니다.

### 0.3 실행 코드와 상위 계약

- Risk API: [`departments/03-risk/api/app.py`](../../departments/03-risk/api/app.py)
- Risk P1 API: [`departments/03-risk/api/p1_runtime_api.py`](../../departments/03-risk/api/p1_runtime_api.py)
- Risk P2 API: [`departments/03-risk/api/p2_derivatives_api.py`](../../departments/03-risk/api/p2_derivatives_api.py)
- QA API: [`departments/06-ai-qa-audit/api/app.py`](../../departments/06-ai-qa-audit/api/app.py)
- Worker contract: [`WORKER_ROLE_BOUNDARIES.md`](WORKER_ROLE_BOUNDARIES.md), [`WORKER_SKILL_REGISTRY.md`](WORKER_SKILL_REGISTRY.md)
- TEST/PRODUCTION runbook: [`RISK_QA_TEST_PRODUCTION_PIPELINE.md`](RISK_QA_TEST_PRODUCTION_PIPELINE.md)
- 상위 제품 계약: [`MINIMUM_SERVICE_UNIT_SPEC.md`](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md)

Pydantic 모델과 route decorator가 실행 가능한 API 계약의 최종 검증 지점이다. 이 문서는 흐름·권한·운영 조건을 설명하며, 코드의 필드 정의와 충돌하면 코드 및 상위 제품 계약을 먼저 확인한다.

## 1. 공통 규약

### 1.1 경로

| 용도 | 경로 |
|---|---|
| Investment Case에 종속된 판정 | `/investment-cases/{case_id}/...` |
| Risk 본부 소유 자원 | `/risk/v1/...` |
| QA 본부 소유 자원 | `/qa/v1/...` |
| Prometheus metrics | `/metrics` (OpenAPI 문서에서는 숨김) |

`v1`은 HTTP 경로 버전이고 `calculation_version` 또는 `checker_version`은 판정 로직 버전이다. 둘을 같은 값으로 취급하지 않는다.

### 1.2 ID·시간·해시

- UUID 필드는 UUID 형식으로 보낸다. `case_id`, `scope`, `employee_code`처럼 시스템 경계 식별자인 문자열은 별도 문자열 필드다.
- `as_of`, `decision_time`, `occurred_at`, `valid_until`은 timezone-aware ISO-8601을 사용한다.
- PIT 검증은 `as_of` 또는 `decision_time` 이후의 자료를 허용하지 않는 결정론적 Guard가 담당한다.
- `input_hash`는 원문 Prompt나 Secret이 아니라 입력 계약의 재현용 hash다.
- Trace 상관관계는 Risk의 `OrderIntent.trace_id`, QA의 `Artifact.trace_id`, Trace API의 `trace_id`를 사용한다. 모든 request body에 공통 `trace_id`가 자동으로 추가되는 것은 아니다.

### 1.3 권한 경계

- 현재 FastAPI 코드에는 전역 Service Token 검증기가 연결되어 있지 않다. 호출자 인증·`sub`와 `agent_id/profile_version_id`의 매핑은 배포 전 필수 작업이다.
- Browser/Frontend가 Domain API를 직접 호출하지 않고 BFF 또는 승인된 내부 서비스가 호출하는 것을 전제로 한다.
- `verify-and-close`는 선택적 `X-Auth-Subject`가 들어오면 `body.verifier`와 일치하는지만 추가 확인한다. 이것은 서명된 토큰 검증을 대체하지 않는다.
- LLM과 Worker는 trading-state 변경, 주문 제출, 원장 기록, QA 판정 승격을 직접 수행하지 않는다.

### 1.4 오류 응답

도메인 Exception과 Pydantic validation 오류는 다음 봉투를 사용한다.

```json
{
  "error_code": "RequestValidationError",
  "message": "요청 스키마 검증 실패",
  "detail": {"errors": []},
  "trace_id": null
}
```

현재 `HTTPException`을 직접 발생시키는 일부 경로(P1 gate, Redis record `404`, QA contract gate, verifier mismatch)는 FastAPI 기본 `{"detail": ...}` 형태를 유지한다. 이 차이는 현재 구현의 사실이며, Service Token 도입 시 공통 HTTPException handler로 통일하는 백로그다.

주요 상태 코드:

| 상태 | 의미 |
|---:|---|
| `200` | 결정·조회·상태 전이가 처리됨 |
| `403` | 호출자와 verifier가 불일치 |
| `404` | Trading State 기록 또는 Incident/Action 자원 없음 |
| `409` | Trace/Incident 상태 전이 충돌, 또는 P1 gate reject |
| `422` | 입력 검증 실패 또는 결정론 Engine rejection/error |
| `503` | Redis/DB/Event Bus/외부 runtime 미연결, 또는 Production gate 미승인 |

## 2. Risk Domain API

소스: [`departments/03-risk/api/app.py`](../../departments/03-risk/api/app.py), [`p1_runtime_api.py`](../../departments/03-risk/api/p1_runtime_api.py), [`p2_derivatives_api.py`](../../departments/03-risk/api/p2_derivatives_api.py)

### 2.1 Risk Case Gate

#### `POST /investment-cases/{case_id}/risk-check`

호출: `RiskEngine.check_order(order_intent, context, risk_request_id)`

Request의 최상위 필드:

```json
{
  "risk_request_id": "uuid (optional)",
  "order_intent": "OrderIntent",
  "context": "RiskContextIn"
}
```

`RiskContextIn`은 `mandate`, `limits`, `restricted_items`, `portfolio`, `market_status`, `counterparty`, `trading_state`, `as_of`를 포함한다. `order_intent`는 `OrderIntent` 계약을 그대로 사용하며 `trace_id`를 포함해야 Risk→QA Event 상관관계가 유지된다.

Response는 `RiskAssessment` 직렬화 결과다. 핵심 필드는 `risk_request_id`, `decision.verdict`(`APPROVE|RESIZE|REJECT`), `check_results`, `reason_codes`, `calculation_version`, `input_hash`, `trading_state`, `approved_legs`, `aggregate_exposure`다.

처리 규칙:

1. 결정은 `RiskEngine`만 수행한다. Worker의 자연어 권고는 binding decision이 아니다.
2. `RISK_REQUIRE_P1_ANALYTICS=true`이면 `p1_snapshot`이 필요하며 P1 gate가 `PASS`가 아니면 `503` 또는 `409`로 종료한다.
3. `DATABASE_URL`이 없으면 Risk decision은 인메모리로 반환된다. `DATABASE_URL`이 있으면 canonical DB 저장을 시도하고, Redis Event Bus가 없으면 성공으로 처리하지 않는다.
4. 성공 시 Risk decision Event를 발행한다. `case_id`, `risk_decision_id`, `risk_request_id`, `order_intent_id`, `decision`, `approved_quantity`, `input_hash`, `calculation_version`, `trace_id`를 envelope payload에 포함한다.
5. 실패 시 승인으로 추정하지 않는다. 주문 진입은 `HOLD/REJECT` 방향으로 남는다.

### 2.2 P1 외부 Risk Snapshot

#### `POST /risk/v1/p1/external-snapshot`

호출: P1 external runtime `collect_external_assessment`.

Request 핵심 필드:

```json
{
  "trace_id": "uuid",
  "fund_id": "uuid",
  "book_id": "uuid (optional)",
  "strategy_version_id": "uuid (optional)",
  "as_of": "timezone-aware datetime",
  "broker_symbols": ["AAPL"],
  "mappings": [],
  "stress_scenarios": {},
  "returns_by_symbol": {},
  "confidence": 0.99,
  "kill_switch_state": "ENABLED"
}
```

`LS_ENV=PAPER`에서는 request의 mapping을 사용할 수 있다. 그 외 환경은 governed mapping과 `DATABASE_URL`이 필요하다. 결과는 P1 risk assessment/snapshot과 `quality_status`, `input_hash`, `calculation_version`, `kill_switch_state`, `breaches`, `exposure_components`를 포함한다.

### 2.3 P2 Derivatives Gate

#### `POST /risk/v1/p2/derivatives-check`

호출: `calculate_derivative_snapshot` 및 `evaluate_derivative_gate`.

Request는 `trace_id`, `fund_id`, `as_of`, 하나 이상의 `positions`, `stress_shocks`, `margin_rates`, `vol_surface`, `max_abs_delta`, `max_abs_gamma`, `max_stress_loss`, `max_margin_requirement`를 포함한다. `positions`는 instrument, option type, quantity, spot, strike, expiry, rate, volatility, dividend yield, multiplier를 포함한다.

`as_of`가 timezone-aware가 아니면 `422`다. 결과는 snapshot과 gate verdict를 반환하며, delta/gamma/stress loss/margin limit을 넘으면 자동으로 승인하지 않는다.

### 2.4 Trading State / Kill Switch

| Method | Path | 처리 |
|---|---|---|
| `GET` | `/risk/v1/trading-state/{scope}` | Redis에서 현재 상태 조회; 장애 시 `503` fail-closed |
| `GET` | `/risk/v1/trading-state/{scope}/record` | 상태·reason·set_by·timestamp 기록 조회; 기록이 없으면 `404` |
| `PUT` | `/risk/v1/trading-state/{scope}` | `state`, `reason`, `set_by`로 상태 설정 |
| `DELETE` | `/risk/v1/trading-state/{scope}` | 상태 기록 제거 |

PUT request:

```json
{
  "state": "ENABLED|REDUCE_ONLY|ENTRY_BLOCKED|HALTED",
  "reason": "required",
  "set_by": "required"
}
```

현재 코드의 `set_by`는 문자열 필수값 검증까지 수행한다. 운영에서는 이를 승인된 Operator/Service Token subject 검증으로 강화해야 한다. LLM이나 Browser가 이 API를 직접 호출하는 것은 금지한다.

### 2.5 Risk Compliance RAG

#### `POST /risk/v1/compliance/check`

Request:

```json
{
  "query": "Can we open a new long position today?",
  "as_of": "2026-08-04"
}
```

`skills/agentic-rag`의 `compliance-policy-agent`를 호출한다. PIT filter, citation validation, hallucination check와 최대 3회 retry는 Graph 내부 계약이다. `grounded=false` 또는 `ambiguous`를 `no_breach`로 승격하지 않고 `escalate=true`로 전달한다. 실제 정책으로 교체되지 않은 `SAMPLE_PLACEHOLDER` corpus는 운영 판단에 사용하지 않는다.

### 2.6 Risk 관측성

| Method | Path | 응답 |
|---|---|---|
| `GET` | `/risk/v1/observability/rag` | retrieve/grade/generate/hallucination_check node별 latency 요약 |
| `GET` | `/risk/v1/observability/runtime` | Risk telemetry snapshot |
| `GET` | `/metrics` | Prometheus text; OpenAPI hidden |

### 2.7 Risk Worker의 API 사용 경계

| Worker | 호출 가능 API/Tool | 금지 |
|---|---|---|
| `market-liquidity-worker` | trading-state read, P1 snapshot, 승인된 market/portfolio adapter | 주문·상태 변경 |
| `pre-trade-risk-worker` | Risk case check, deterministic Risk Engine adapter | RAG·임의 외부 HTTP·주문 제출 |
| `compliance-policy-worker` | compliance check, evidence read-only adapter | 정책 판단을 APPROVE로 직접 확정 |
| `derivatives-counterparty-worker` | trading-state record, P2/market/portfolio read-only adapter | counterparty 상태를 임의 변경 |

모든 Worker의 LLM 호출은 allow-listed Ollama endpoint에 한정하며, Worker가 임의 URL·Broker·LS API·Supabase에 직접 접근하지 않는다.

## 3. QA Domain API

소스: [`departments/06-ai-qa-audit/api/app.py`](../../departments/06-ai-qa-audit/api/app.py)

### 3.1 Model Risk / Internal Audit 결정론 API

이 두 API는 현재 구현되어 있다. 이전 문서의 “미구현·Eval Harness 없음” 표기는 더 이상 현재 상태가 아니다. 다만 API가 존재하는 것과 Production Eval Harness/실제 모델 lineage가 준비된 것은 별개다.

#### `POST /qa/v1/model-risk/evaluate`

Request 필드:

```json
{
  "model_id": "uuid",
  "model_version": "required",
  "prompt_version": "required",
  "dataset_version": "required",
  "evaluation_count": 100,
  "accuracy": 0.95,
  "calibration_error": 0.05,
  "drift_score": 0.10,
  "protected_failure_rate": 0.00
}
```

점수 필드는 `0..1`, `evaluation_count`는 `0` 이상이다. Response는 `decision`, `reason_codes`, `calculation_version`, `input_hash`를 포함하는 Model Risk assessment다.

#### `POST /qa/v1/internal-audit/evaluate`

Request:

```json
{
  "events": [{"event_type": "...", "department": "qa"}],
  "expected_department": "qa"
}
```

Response는 `decision`, `findings`, `calculation_version`, `input_hash`를 포함한다. 감사 결과가 PASS가 아니면 Worker/Hermes가 자동 승인으로 처리하지 않고 QA supervisor 또는 사람 검토로 에스컬레이션한다.

### 3.2 QA Case Evidence Gate

#### `POST /investment-cases/{case_id}/qa-check`

호출: `EvidenceQaEngine.check_artifact(artifact, context, qa_decision_id)`.

Request 최상위 필드:

```json
{
  "qa_decision_id": "uuid (optional)",
  "artifact": "Artifact",
  "context": {"decision_time": "timezone-aware datetime"}
}
```

`Artifact`는 artifact version/type, producer, fund, `trace_id`, claims, evidence IDs와 tool results를 포함한다. `context.evidence_store`를 외부에서 주입하지 않는다. QA가 소유한 Evidence Store를 조회한다.

Response는 `QaAssessment`를 그대로 반환하며 `decision`은 `PASS|WARN|FAIL`, claim checks, findings, `checker_version`/계산 버전, `input_hash`를 포함한다. `UNSUPPORTED` 또는 `CONTRADICTED` claim이 있으면 PASS로 승격하지 않는다.

실행 게이트:

```text
RISK_QA_RUNTIME != production
  → TEST에서 허용

RISK_QA_RUNTIME == production
  → QA_CHECK_CONTRACT_APPROVED == true 필요
```

Production gate가 닫혀 있으면 `503 QA_CHECK_CONTRACT_NOT_APPROVED`다. `QA_CHECK_CONTRACT_APPROVED`는 TEST pipeline의 synthetic gate를 Production으로 바꾸는 flag가 아니다. 실제 corpus·DB·trace·acceptance가 별도로 필요하다.

### 3.3 Risk Event 소비

#### `POST /qa/v1/events/consume`

QA Worker가 Risk Decision Stream을 배치 소비한다.

```json
{
  "count": 10,
  "min_idle_ms": 0
}
```

`count`는 `1..100`, `min_idle_ms`는 `0` 이상이다. `RISK_QA_EVENT_REDIS_URL` 또는 `REDIS_URL`, QA consumer/group 설정이 필요하다. Event envelope와 Risk DB 연결이 유효하지 않으면 처리 성공으로 기록하지 않는다.

### 3.4 Evidence QA RAG

#### `POST /qa/v1/evidence/check`

Request:

```json
{
  "query": "주장과 근거가 일치하는가?",
  "as_of": "2026-08-04"
}
```

`persona="evidence-qa-agent"`로 Agentic RAG baseline을 호출한다. 응답은 Graph 반환값(`answer`, `grounded`, `attempts`, 관련 문서 등)을 그대로 전달한다. 근거 부족·citation 불일치·PIT 실패는 `UNSUPPORTED|CONTRADICTED|ESCALATE` 방향으로 처리한다.

#### `GET /qa/v1/evidence/corpus/status`

문서 원문을 반환하지 않고 `directory`, `document_count`, `placeholder_count`, `corpus_hash`, `ready`, `reason`만 반환한다. `ready=true`여도 운영 정책 교체·PIT·ACL·citation golden set을 통과했다는 뜻은 아니다.

### 3.5 Ops Health

#### `POST /qa/v1/ops/evaluate`

Request는 `metrics`, `thresholds`, optional `trace_id`다.

`metrics`는 scope, window start/end, request/error count, p95 latency, cost를 포함한다. `thresholds`는 max/critical error rate, max/critical p95 latency, cost limit을 포함한다. Response는 `status`, `breaches`, 필요 시 SEV incident 초안을 포함하는 `OpsAssessment`다.

### 3.6 Agent Run / Tool Trace

| Method | Path | 목적 |
|---|---|---|
| `POST` | `/qa/v1/runs` | `RUNNING` Agent Run 시작 |
| `POST` | `/qa/v1/runs/{agent_run_id}/complete` | 산출물·token/cost·trace URI 기록 |
| `POST` | `/qa/v1/runs/{agent_run_id}/fail` | error code와 함께 실패 전이 |
| `POST` | `/qa/v1/runs/{agent_run_id}/timeout` | timeout 전이 |
| `POST` | `/qa/v1/runs/{agent_run_id}/cancel` | cancel 전이 |
| `POST` | `/qa/v1/runs/{agent_run_id}/tool-calls` | Tool Call 기록 |
| `POST` | `/qa/v1/tool-calls/{tool_call_id}/allow` | Tool Call 허용 |
| `POST` | `/qa/v1/tool-calls/{tool_call_id}/deny` | reason과 함께 거부 |
| `POST` | `/qa/v1/tool-calls/{tool_call_id}/complete` | output hash 기록 |
| `POST` | `/qa/v1/tool-calls/{tool_call_id}/fail` | tool error 기록 |

`POST /qa/v1/runs`는 `trace_id`, `agent_id`, `profile_version_id`, `input_hash`를 필수로 받고 case/fund/model ID는 선택이다. Trace 전이는 역행하지 않는다. timeout/failure를 PASS 또는 성공으로 보정하지 않는다.

### 3.7 Tool Permission

| Method | Path | 목적 |
|---|---|---|
| `POST` | `/qa/v1/tool-permission/check` | allow-list에 따른 ALLOWED/DENIED 조회 |
| `POST` | `/qa/v1/runs/{agent_run_id}/tool-calls:checked` | 검사와 Tool Call 기록을 한 번에 수행 |
| `GET` | `/qa/v1/tool-calls/unauthorized-count` | 인메모리 recorder의 unauthorized count 조회 |

Policy는 `agent_id`, `profile_version_id`, `allowed_tools`를 포함한다. DENIED 결과는 우회·재시도로 허용으로 바꾸지 않는다.

### 3.8 Incident / Corrective Action

| Method | Path | 목적 |
|---|---|---|
| `POST` | `/qa/v1/incidents/{incident_id}/events` | FACT/INFERENCE incident event 추가 |
| `GET` | `/qa/v1/incidents/{incident_id}/timeline` | timeline 조회 |
| `POST` | `/qa/v1/corrective-actions` | corrective action 생성 |
| `POST` | `/qa/v1/corrective-actions/{id}/start` | 작업 시작 |
| `POST` | `/qa/v1/corrective-actions/{id}/submit-for-verification` | 검증 대기 전이 |
| `POST` | `/qa/v1/corrective-actions/{id}/verify-and-close` | 독립 verifier 검증·종료 |
| `POST` | `/qa/v1/corrective-actions/{id}/cancel` | reason과 함께 취소 |

`verify-and-close`의 owner와 verifier 분리, 상태 전이, Incident Timeline 검증은 결정론 코드가 담당한다. Hypergraph 추출 결과는 사람이 검증하기 전까지 root cause 확정이나 corrective action 종결 근거가 아니다.

### 3.9 QA 관측성

| Method | Path | 응답 |
|---|---|---|
| `GET` | `/qa/v1/observability/rag` | RAG node별 latency 요약 |
| `GET` | `/qa/v1/observability/runtime` | QA telemetry snapshot |
| `GET` | `/metrics` | Prometheus text; OpenAPI hidden |

## 4. QA/Risk Event 계약

### 4.1 Risk → QA

Risk Case Gate가 성공적으로 처리되면 다음 유형 중 하나를 발행한다.

```text
investment_case.risk_approved
investment_case.risk_resized
investment_case.risk_rejected
```

최소 payload에는 `case_id`, `risk_decision_id`, `risk_request_id`, `order_intent_id`, `decision`, `approved_quantity`, `input_hash`, `calculation_version`, `trace_id`가 포함된다.

### 4.2 QA Case Event

QA Evidence Gate 결과는 다음 의미를 가진다.

```text
investment_case.qa_passed   # PASS
investment_case.qa_warned   # WARN
investment_case.qa_blocked  # FAIL 또는 blocked=true
```

QA Event는 원 작성 부서가 Finding을 처리하도록 만들 뿐, QA가 다른 부서의 권한을 대신해 주문·원장·전략을 변경하게 만들지 않는다.

### 4.3 QA/Audit Stream

부서 전역 관찰 Event는 다음 범주를 사용한다.

```text
qa.finding.opened
qa.finding.escalated
qa.incident.opened
qa.incident.event_added
qa.corrective_action.opened
qa.corrective_action.verified
qa.ops.incident_drafted
```

실제 Redis Stream consumer·publisher가 설정되지 않은 TEST에서는 Event 외부 발행을 성공으로 간주하지 않고 in-memory 또는 fixture 검증으로 한정한다.

## 5. Persistence와 외부 연결

### 5.1 선택적 adapter의 현재 동작

| 기능 | 환경변수/연결 | 연결이 없을 때 |
|---|---|---|
| Risk decision canonical write | `DATABASE_URL` | in-memory 반환; Production 조건 미충족 |
| Risk↔QA Event | `RISK_QA_EVENT_REDIS_URL` 또는 `REDIS_URL` | DB write-through가 켜진 경우 `503` |
| Trading State | `REDIS_URL` | `503` fail-closed |
| QA audit/trace/incident write-through | `DATABASE_URL` | in-memory recorder/timeline |
| QA Event consume | Redis URL + consumer/group | `503` |
| Compliance/Evidence RAG | `OPENAI_API_KEY`, network, corpus | 호출 실패 또는 inconclusive; PASS 추정 금지 |
| Worker LLM | 승인된 `OLLAMA_BASE_URL` | TEST stub 외에는 실행 불가 |

현재 API는 환경변수를 자동으로 `.env`에서 읽지 않는다. 배포 런타임이 주입한 값만 사용한다.

### 5.2 Canonical DB 원칙

- Supabase canonical schema는 `supabase/migrations/`가 소유한다.
- QA/Risk audit·decision·event persistence migration이 적용되어야 Production write-through를 말할 수 있다.
- `db/001_execution.sql`~`db/004_seed.sql` Prototype SQL을 canonical Supabase migration과 같은 방식으로 적용하지 않는다.
- RAG 원문·embedding·PIT metadata는 Research/Evidence 저장 경계가 소유하고, QA/Risk는 승인된 read-only API 또는 repository를 사용한다.
- 별도 Qdrant/Neo4j를 현재 추가하지 않는다. Supabase pgvector와 관계형 projection을 우선 사용하고, 별도 Vector/Graph DB가 필요해도 재생성 가능한 projection으로만 둔다.

## 6. LangGraph·RAG 연동 규약

### 6.1 공통 Worker topology

```text
intake
  → schema / scope / PIT / freshness guard
  → allow-listed context tool
  → deterministic engine 또는 RAG route
  → Qwen structured advisory
  → citation / contradiction / numeric / schema verification
  → trace + replay manifest
  → retry 또는 human escalation
```

LLM은 관련성 판단과 비바인딩 서술만 수행한다. PIT filter, citation existence, limit check, status transition, verdict lock은 Python 결정론 코드가 수행한다.

### 6.2 Risk Worker API mapping

| Worker | 필수 LangGraph skill | HTTP/API |
|---|---|---|
| Market·Liquidity | freshness, snapshot fan-in, exposure summary, replay | trading-state read, P1 snapshot, 승인된 market/portfolio adapter |
| Pre-trade | contract validation, idempotency, Risk Engine adapter, fail-closed | `POST /investment-cases/{case_id}/risk-check`; RAG·임의 HTTP 금지 |
| Compliance Policy | PIT, hybrid retrieve, decompose, citation verify, grounded fallback | `POST /risk/v1/compliance/check`, Evidence read-only API |
| Derivatives·Counterparty | counterparty scope, P2 snapshot, deterministic gate, provenance | trading-state record, P2 derivatives, market/portfolio read |

### 6.3 QA Worker API mapping

| Worker | 필수 LangGraph skill | HTTP/API |
|---|---|---|
| Evidence·Citation QA | claim atomize, hybrid retrieve, citation/PIT/numeric verify | `qa-check`, `/qa/v1/evidence/check`, corpus status |
| Hallucination·Contradiction | re-retrieve, contradiction check, verdict lock | Evidence read-only API; 원 판정 임의 승격 금지 |
| Model Risk·Internal Audit | lineage traversal, replay, model/prompt/dataset join, SoD check | `/qa/v1/model-risk/evaluate`, `/qa/v1/internal-audit/evaluate` |
| Agent Ops·Tool Permission | health threshold, tool allow-list, trace state transition | ops, trace, tool-permission APIs |
| Incident·Postmortem | FACT/INFERENCE separation, timeline, human-reviewed graph candidate | incident/corrective-action APIs; Hyper-Extraction은 아직 HTTP 미구현 |

### 6.4 RAG 기술 도입 상태

현재 baseline은 `retrieve → grade → generate → hallucination_check → retry`다.

| 기술 | 현재 계약 | 도입 조건 |
|---|---|---|
| Adaptive route | Skill Router skeleton; deterministic signal 기반 | 문서 규모·복잡도 지표가 필요할 때 확장 |
| Hybrid/BM25/vector | baseline adapter와 PIT/citation 검증 | 실제 corpus golden set 통과 |
| Self-RAG | 제한적 sufficiency/재검색 Skill | grounded rate와 비용 측정 후 |
| PIKE-RAG | HTTP endpoint 미구현 | 복합 claim이 실제로 누적될 때 |
| LightRAG/GraphRAG | projection/관계 Skill 설계 단계 | entity/relation corpus와 ACL 계약 확정 후 |
| Hyper-Extraction | HTTP endpoint 미구현; Incident inference 후보만 백로그 | 다년 Incident와 human review 체계 확보 후 |

기법을 추가할 때에는 route·Skill registry·allow-list·trace schema·migration·golden/replay test를 함께 변경한다. LLM이 위 기술 중 하나를 임의 선택하거나, RAG 결과가 결정론 gate를 우회하게 만들지 않는다.

## 7. 구현하지 않은 항목과 Production 전환 조건

다음 항목은 현재 API에 이미 구현됐다고 표시하지 않는다.

- 전역 Service Token 발급·검증 및 `sub → agent/profile` 매핑
- 외부 P1의 non-PAPER governed ID/실제 LS API 연결
- 실제 정책 corpus ingestion, embedding, ACL, PIT golden set
- PIKE-RAG/LightRAG/Hyper-Extraction 전용 HTTP endpoint
- Production Ollama availability, timeout, rate/cost budget
- Supabase/Redis write-through의 skip 없는 통합 acceptance
- QA/Risk Event idempotency, replay, outbox/consumer recovery의 운영 검증

Production 전환은 아래 순서를 모두 만족해야 한다.

1. ResearchPacket v1의 source refs, claim `observed_at`, `as_known_at`, input hash, ACL을 검증한다.
2. Risk deterministic gate와 QA Evidence Gate의 golden/replay test를 통과한다.
3. 실제 corpus에서 citation precision, contradiction detection, grounded rate, escalation rate를 측정한다.
4. Supabase migration/RLS, Redis Stream, DB/Event timeout·idempotency를 통합 검증한다.
5. ACTIVE `LANGGRAPH` Worker Profile FK와 `audit.agent_runs/tool_calls` trace persistence를 검증한다.
6. Production flag와 credential은 별도 승인 후에만 주입한다.

## 8. 검증 명령

```bash
# API self-check
python departments/03-risk/api/app.py
python departments/06-ai-qa-audit/api/app.py

# Risk/QA unit + API + TEST pipeline
python -m pytest departments/03-risk/tests departments/06-ai-qa-audit/tests \
  tests/e2e/test_risk_qa_pipeline_profiles.py -q -p no:warnings

# Schema contract
python -m unittest discover -s tests/schema -p "test_*.py" -q

# Production이 꺼져 있는지 확인
python scripts/run_risk_qa_test_pipeline.py --mode production
```

TEST 성공은 파이프라인 배선·계약·Worker topology 검증을 의미한다. 실제 주문 승인, QA PASS, 원장 반영, Production RAG 신뢰성을 의미하지 않는다.

## 9. 현재 전체 Route Inventory

문서 누락을 막기 위한 2026-08-04 기준 route 목록이다.

### Risk

```text
POST   /investment-cases/{case_id}/risk-check
POST   /risk/v1/p1/external-snapshot
POST   /risk/v1/p2/derivatives-check
GET    /risk/v1/trading-state/{scope}
GET    /risk/v1/trading-state/{scope}/record
PUT    /risk/v1/trading-state/{scope}
DELETE /risk/v1/trading-state/{scope}
POST   /risk/v1/compliance/check
GET    /risk/v1/observability/rag
GET    /risk/v1/observability/runtime
GET    /metrics                  # include_in_schema=False
```

### QA

```text
POST /qa/v1/model-risk/evaluate
POST /qa/v1/internal-audit/evaluate
POST /investment-cases/{case_id}/qa-check
POST /qa/v1/events/consume
GET  /qa/v1/observability/rag
POST /qa/v1/ops/evaluate
POST /qa/v1/runs
POST /qa/v1/runs/{agent_run_id}/complete
POST /qa/v1/runs/{agent_run_id}/fail
POST /qa/v1/runs/{agent_run_id}/timeout
POST /qa/v1/runs/{agent_run_id}/cancel
POST /qa/v1/runs/{agent_run_id}/tool-calls
POST /qa/v1/tool-calls/{tool_call_id}/allow
POST /qa/v1/tool-calls/{tool_call_id}/deny
POST /qa/v1/tool-calls/{tool_call_id}/complete
POST /qa/v1/tool-calls/{tool_call_id}/fail
POST /qa/v1/tool-permission/check
POST /qa/v1/runs/{agent_run_id}/tool-calls:checked
GET  /qa/v1/tool-calls/unauthorized-count
POST /qa/v1/incidents/{incident_id}/events
GET  /qa/v1/incidents/{incident_id}/timeline
POST /qa/v1/corrective-actions
POST /qa/v1/corrective-actions/{corrective_action_id}/start
POST /qa/v1/corrective-actions/{corrective_action_id}/submit-for-verification
POST /qa/v1/corrective-actions/{corrective_action_id}/verify-and-close
POST /qa/v1/corrective-actions/{corrective_action_id}/cancel
POST /qa/v1/evidence/check
GET  /qa/v1/observability/runtime
GET  /qa/v1/evidence/corpus/status
GET  /metrics                  # include_in_schema=False
```

## 10. Department Head·Worker E2E 계약

API의 Domain Gate와 LangGraph 직원 실행은 별도 계층이다. TEST E2E에서는 다음 parent graph를 사용한다.

```text
Hermes-shaped department head
  → head delegation
  → independent employee LangGraph Worker Graphs
  → compact peer-context handoff
  → head synthesis (non-binding)
```

Risk parent graph는 `risk-supervisor`가 `market-liquidity-worker`, `pre-trade-risk-worker`, `compliance-policy-worker`, `derivatives-counterparty-worker`를 위임한다. QA parent graph는 `qa-audit-supervisor`가 `evidence-qa-worker`, `hallucination-critic-worker`, `model-and-internal-audit-worker`, `ops-and-permission-worker`, `incident-postmortem-worker`를 위임한다.

각 실행 결과에는 `head`, `workers`, `handoffs`, `not_executed`, `input_hash`, `runtime`이 포함된다. `head.binding`과 `runtime.binding`은 항상 `false`이며, Head/Worker 결과는 Risk Engine 또는 QA Evidence Engine의 binding 판정을 대체하지 않는다. Risk Head에서 QA Head로 넘어가는 department handoff는 동일한 `trace_id`, `artifact_id`, `input_hash`를 유지해야 한다.

상세 실행·검증 명령은 [`RISK_QA_TEST_PRODUCTION_PIPELINE.md`](RISK_QA_TEST_PRODUCTION_PIPELINE.md)를 따른다.
