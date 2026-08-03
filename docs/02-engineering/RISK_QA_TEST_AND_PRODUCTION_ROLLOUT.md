# Risk/QA Test·Production 전환 계획

최종 수정: 2026-08-02

이 문서는 Risk/QA의 현재 테스트 구현과 실제 운영 전환을 분리한다. 테스트용
부모 Fund/Policy는 `tests/schema/supabase_risk_qa_test_fixture.sql`에만 둔다.
운영 DB에 테스트 Fund를 삽입하지 않는다.

## 현재 적용하는 Test 버전

### Workforce Agent Profile

`20260802001600_risk_qa_runtime_registration.sql`은 감사·FK 호환을 위해 Risk 6개(`RSK-*`)와 QA 8개(`QAA-*`)의 `workforce.agent_profiles` 및 version row를 등록한다. 실제 실행 Registry는 Risk 4개 Worker와 QA 5개 Worker이며, 중복된 Profile ID는 호환 Alias다.

- profile: `PROBATION`
- profile version: `DRAFT`
- model environment: `DEVELOPMENT`, `PAPER`
- production credential·주문 제출·원장 기록 권한 없음
- 등록은 구현 완료 또는 운영 활성화를 의미하지 않음

이 상태로 `audit.agent_runs`와 `audit.tool_calls`의 FK 대상은 존재하지만,
실제 운영 호출은 Workforce 승인과 profile version 활성화 뒤에만 허용한다.

### Fund/Policy FK fixture

`tests/schema/supabase_risk_qa_test_fixture.sql`은 격리된 local/CI DB에서만
다음 부모 row를 만든다.

- `accounting.funds.fund_code = TEST-RISK-QA`
- `accounting.books.book_code = TEST-RISK-QA-BOOK`
- `risk.policies.policy_code = TEST-RISK-QA-BASELINE`

이를 통해 Risk Request → Risk Decision과 QA Artifact → QA Decision의 FK를
실제 스키마에서 검증한다. 운영 DB의 `accounting.funds`를 우회하거나 NULL로
채우지 않는다.

### Incident 부모 보장

QA Incident Event 또는 Corrective Action을 Postgres에 기록할 때 다음 순서를
하나의 transaction으로 보장한다.

1. `audit.incidents` 부모를 `incident_code` 기준으로 `INSERT ... ON CONFLICT`
   한다.
2. 같은 transaction에서 `audit.incident_events` 또는
   `audit.corrective_actions`를 삽입한다.
3. 어느 단계든 실패하면 전체 transaction을 rollback한다.

메모리 전용 테스트는 기존처럼 순수 상태 전이만 검증하고, Repository 경계의
DB 테스트는 parent auto-create와 rollback을 별도 시나리오로 검증한다.

### Test Observability

현재 테스트 버전은 외부 Collector/Prometheus 서버 없이 다음을 검증한다.

- pipeline latency sample
- p50/p99 계산
- fallback count/rate
- circuit breaker 상태(`CLOSED`, `OPEN`, `HALF_OPEN`)
- trace id와 stage/outcome 연결

OpenTelemetry SDK 또는 `prometheus-client`가 설치되고 환경변수가 켜진 경우에만
실제 exporter를 활성화한다. 라이브러리·Collector가 없으면 no-op이 되며,
안전한 Risk/QA 판정에는 영향을 주지 않는다.

## 실제 Production 버전 전환 조건

### Risk

1. `accounting.funds`·`books`·`risk.policies`를 Governance 승인 workflow로
   생성하고, `fund_id`·`policy_id`를 Risk Request에 필수로 전달한다.
2. Portfolio API와 Market Data API를 `as_of`/`observed_at` 포함 계약으로 연결한다.
3. Stress/VaR/Greeks를 별도 결정론적 계산 모듈로 구현하고, Risk Engine은
   계산 결과를 입력으로만 받아 동일 snapshot/config에서 재현되게 한다.
4. 실시간 계산 실패는 노출 확대가 아니라 `HALTED` 또는 `REJECT`로 처리한다.
5. RLS, service identity, idempotency, DB write와 OMS gate의 E2E를 실제
   Supabase/Redis 환경에서 통과시킨다.

### QA

1. `SAMPLE_PLACEHOLDER`를 제거하기 전에 문서 소유자·effective time·license
   scope를 포함한 Policy/Evidence ingestion 계약을 승인한다.
2. Notion/PDF/DB 원문을 `research.documents` → `document_versions` →
   `evidence_chunks`로 적재하고, PIT 필터와 pgvector index를 검증한다.
3. `qa-check` 상위 서비스 계약을 승인한 뒤 OMS workflow의 표준 QA gate로
   바인딩한다. QA 실패는 PASS로 변환하지 않는다.
4. `workforce.agent_profiles`의 실제 ACTIVE profile version과 model/eval
   manifest를 Workforce/QA 승인으로 생성한다.
5. 신규 Incident는 부모 row 생성, event, corrective action을 같은 DB
   transaction으로 기록하고, outbox/event delivery까지 멱등 검증한다.

### Monitoring

Production에서는 각 API/Worker에서 OTLP Collector로 Trace를 보내고,
Prometheus는 `/metrics` 또는 Collector 경유로 다음을 수집한다.

- `risk_qa_pipeline_duration_seconds`: stage별 p50/p99
- `risk_qa_fallback_total`: 부서·stage·error class별 fallback
- `risk_qa_circuit_breaker_state`: 외부 의존성별 현재 상태
- `risk_qa_incident_total`: severity/status별 Incident
- `risk_qa_qa_decision_total`, `risk_qa_risk_decision_total`: 판정 분포

Trace/metric label에는 prompt, 원문 evidence, API key, 계좌 잔고 원문을 넣지
않고 `trace_id`, department, stage, outcome, error class만 사용한다.
