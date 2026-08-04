# Risk·QA TEST / PRODUCTION Pipeline Runbook

검토일: 2026-08-04  
상태: TEST E2E 구현, PRODUCTION 의도적 OFF

## 1. 실행 프로파일

| 프로파일 | 입력 | Head | 직원 | 외부 연결 | 상태 |
|---|---|---|---|---|---|
| `test` | synthetic `ResearchPacket v1` | 결정론적 Hermes-shaped stub | 결정론적 Qwen-shaped `LangGraph` Worker | 없음 | 실행 가능 |
| `production` | 승인된 Research/API/DB 입력 | 실제 Hermes Profile | 승인된 Ollama Worker | DB·Redis·외부 API 필요 | OFF/HOLD |

TEST는 운영 성공이 아니다. 계약, Graph topology, handoff, trace/replay, fallback을 검증하며 실제 주문·Risk 승인·QA PASS·원장 변경을 수행하지 않는다.

## 2. TEST E2E 흐름

```text
ResearchPacket fixture
  → packet contract / input_hash / PIT guard
  → Risk deterministic gate skeleton (binding=false)
  → risk-supervisor (Hermes-shaped Head Graph)
      → market-liquidity-worker (nested LangGraph)
      → pre-trade-risk-worker (peer context)
      → compliance-policy-worker (peer context)
      → derivatives-counterparty-worker (peer context)
  → risk-supervisor synthesis (non-binding)
  → Risk Head → QA Head department handoff
  → QA deterministic gate skeleton (binding=false)
  → qa-audit-supervisor (Hermes-shaped Head Graph)
      → evidence-qa-worker (nested LangGraph)
      → hallucination-critic-worker (peer context)
      → model-and-internal-audit-worker (peer context)
      → ops-and-permission-worker (peer context)
      → incident-postmortem-worker (peer context)
  → qa-audit-supervisor synthesis (non-binding)
  → test gate / trace-replay inspection
```

각 직원은 별도 `StateGraph`로 compile된다. 상위 Department Graph의 직원 노드는 해당 Worker Graph를 invoke한다. 직원 간 handoff에는 요약, confidence, evidence refs, status, input hash와 trace manifest만 전달하며 원문 Prompt·Secret·binding decision은 전달하지 않는다.

Head와 직원의 권한은 다음과 같이 고정한다.

- Head는 위임·취합·해석·에스컬레이션만 한다.
- Worker는 allow-listed Tool을 읽고 non-binding `worker-context.v1` 또는 `qa.worker-context.v1`만 만든다.
- Risk Engine이 binding `APPROVE|RESIZE|REJECT`를 소유한다.
- QA Evidence Engine이 binding `PASS|WARN|FAIL`를 소유한다.
- Head/Worker 모두 주문 제출, 원장 기록, Risk gate 우회, QA 판정 승격, Incident 종결을 할 수 없다.

## 3. 실행 코드

- TEST runner: [`scripts/run_risk_qa_test_pipeline.py`](../../scripts/run_risk_qa_test_pipeline.py)
- Pipeline contract: [`departments/risk_qa_testkit/pipeline.py`](../../departments/risk_qa_testkit/pipeline.py)
- Department parent graph: [`departments/risk_qa_testkit/department_graph.py`](../../departments/risk_qa_testkit/department_graph.py)
- Risk employee Graph: [`departments/03-risk/risk_employee_workers.py`](../../departments/03-risk/risk_employee_workers.py)
- QA employee Graph: [`departments/06-ai-qa-audit/qa_employee_workers.py`](../../departments/06-ai-qa-audit/qa_employee_workers.py)

실행:

```bash
python scripts/run_risk_qa_test_pipeline.py --mode test
python scripts/run_risk_qa_test_pipeline.py --mode production
python -m pytest tests/e2e/test_risk_qa_pipeline_profiles.py -q -p no:warnings
```

## 4. 기대 결과

TEST에서 다음을 확인한다.

- `pipeline_status=COMPLETED`
- `manual_review_required=true`는 fixture의 QA `WARN`을 의미하며, 파이프라인 실행 완료와 QA PASS를 혼동하지 않게 한다.
- Risk `risk-supervisor`가 4개 Worker를 위임하고 4개 Worker Graph가 실행됨
- QA `qa-audit-supervisor`가 5개 Worker를 위임하고 5개 Worker Graph가 실행됨
- Risk/QA 각각의 `handoffs`에 Head delegation 1개와 peer context handoff가 기록됨
- Risk Head → QA Head department handoff가 같은 `trace_id`와 `input_hash`로 기록됨
- 모든 Worker에 `skill_results`, `trace.events`, `trace_id`, `input_hash`가 존재함
- Risk/QA Head 결과 모두 `binding=false`
- QA fixture의 unsupported claim 때문에 deterministic QA gate는 `WARN`, QA Head는 `ESCALATE` advisory
- `safe_action=NO_ACTION`은 외부 side effect가 없다는 뜻이며 QA `WARN`을 PASS로 의미 변경하지 않음

PRODUCTION mode는 다음 값을 반환하고 Worker를 실행하지 않는다.

```json
{
  "pipeline_status": "OFF",
  "safe_action": "HOLD",
  "reason": "PRODUCTION_DISABLED_UNTIL_REAL_ADAPTER_ACCEPTANCE"
}
```

## 5. Fail-closed 규칙

- Head JSON schema 실패: `DEGRADED`, `ESCALATE`, Risk는 `HOLD`
- Worker Tool scope 실패·timeout·schema 실패: Worker `DEGRADED`, Head `ESCALATE`
- Trace/replay manifest 누락: 성공으로 승격하지 않음
- Risk gate 입력 오류: `HOLD`
- QA evidence 부족·모순·PIT 실패: `WARN|FAIL|ESCALATE`, PASS로 보정하지 않음
- 조건부 signal이 없으면 해당 Worker는 `not_executed`에 기록하며 성공한 것처럼 실행 수에 포함하지 않음

## 6. Production 전환 조건

다음 조건을 모두 별도 acceptance로 통과하기 전까지 Production flag와 credential을 만들지 않는다.

1. 실제 ResearchPacket v1 조회와 PIT/ACL 검증
2. 실제 Hermes Head adapter와 Ollama Worker timeout/retry/cost 검증
3. Risk deterministic gate와 QA Evidence Gate golden/replay 검증
4. 실제 정책 corpus 교체 및 citation/contradiction golden set 통과
5. Supabase migration/RLS, Redis Stream, Event idempotency/recovery 통합 검증
6. ACTIVE `LANGGRAPH` Profile FK와 `audit.agent_runs/tool_calls` persistence 검증
7. Production `RISK_QA_RUNTIME=production`, `QA_CHECK_CONTRACT_APPROVED=true` 및 별도 운영 승인

기존 본부 self-check의 `--run`은 `RISK_QA_PRODUCTION_ENABLED=true`가 없으면 실제 데이터를 사용하지 않고 종료한다.

## 7. Worker runtime smoke

TEST Graph는 Worker runtime을 명시적으로 구분한다.

```bash
python scripts/run_risk_qa_test_pipeline.py --mode test --worker-runtime deterministic
python scripts/run_risk_qa_test_pipeline.py --mode test --worker-runtime ollama
```

`ollama` 모드는 실제 로컬 `qwen3:1.7b` Worker를 호출하지만 Head는 TEST용 deterministic stub이며,
Production·주문·Risk binding decision·QA PASS·DB write를 활성화하지 않는다.

## 8. Canonical ResearchPacketV2

Risk/QA E2E의 authoritative input은 `ResearchPacketV2`다. `RiskQaPacket` envelope은 `artifact_id`,
`trace_id`, `input_hash`와 결정론적 Risk/QA read model만 추가하며 canonical Packet을 대체하지 않는다.
PIT와 canonical packet hash가 검증되지 않으면 파이프라인은 시작하지 않는다.

## 8.1 사용자 적합 포트폴리오 목록 TEST 경로

현재 제품 목적의 사용자 경로는 주문을 생성하는 Investment Case와 별도로 다음 TEST adapter를 사용한다.

```text
InvestorProfile
  → portfolio suitability deterministic match
  → Risk 읽기 전용 context / safe limit check
  → QA 읽기 전용 evidence·reproducibility check
  → 적합 포트폴리오 목록 + 제외 이유
```

실행 명령:

```bash
python scripts/run_portfolio_recommendation_test.py
python -m pytest tests/portfolio/test_suitability.py tests/e2e/test_portfolio_recommendation_pipeline.py -q -p no:warnings
```

이 경로의 `RISK_QA` 결과도 `binding=false`다. 적합 후보가 없으면 `NO_MATCH`와 `HOLD`를 반환하며 공격형 후보를 fallback으로 추천하지 않는다. QA `WARN`은 PASS로 올리지 않고 `manual_review_required=true`로 남긴다. 실제 사용자 프로필 저장, 후보 카탈로그 승인, 운영 Evidence와 API 연결은 Production 전환 조건에 포함되지 않은 별도 백로그다.

## 9. External integration probe

```bash
python scripts/run_risk_qa_integration_smoke.py
```

외부 연결은 별도 smoke로 실행한다. Production은 계속 OFF이며 Supabase Event는 transaction rollback,
Redis는 임시 Stream 삭제를 사용한다. 지원 환경변수는 `RESEARCH_API_URL`,
`RISK_QA_RESEARCH_PACKET_URL`, `RISK_QA_EVENT_REDIS_URL` 또는 `REDIS_URL`, `DATABASE_URL`이다.
미설정은 `SKIPPED`, 연결 실패는 `FAILED`로 남긴다.

## 10. Jaeil Research/Quant acceptance

```bash
python scripts/run_jaeil_p0_p2_checks.py
```

P0 contract self-check를 먼저 실행하고, P1/P2는 runtime evidence가 없으면 `DOCUMENTED_ONLY` 또는
`NOT_RUN`으로 남긴다. 이 가이드에는 P3 priority 항목이 없으며 `RQF-3`은 별도의 phase다.
## 11. 재일님 Production 실행 전 preflight

Production 실행 담당자는 먼저 아래 게이트를 통과시킨다. 이 명령은 주문, Ledger, Trading State 변경을 수행하지 않으며 credential 값도 출력하지 않는다.

```bash
python scripts/run_risk_qa_production_preflight.py \
  --as-of 2026-08-04T00:00:00+00:00
```

다음 조건 중 하나라도 실패하면 실행하지 않고 원인을 먼저 해결한다.

- `DATABASE_URL`, `REDIS_URL` 또는 `RISK_QA_EVENT_REDIS_URL`, `QA_POLICY_SOURCE_ID`, `OPENAI_API_KEY`
- `RISK_QA_RUNTIME=production`, `RISK_QA_PRODUCTION_ENABLED=true`
- `QA_CHECK_CONTRACT_APPROVED=true`, `QA_TRACE_PERSIST=true`, `RISK_REQUIRE_P1_ANALYTICS=true`
- 실제 정책 문서 경로 `QA_POLICY_CORPUS_DIR`가 설정되고 `SAMPLE_PLACEHOLDER`가 없음
- Supabase canonical table, RLS policy, active worker profile, PIT Fund/Policy/Portfolio/Market 데이터
- Redis PING 성공

정책 문서는 저장소 샘플을 운영 근거로 사용하지 않는다. 실제 문서를 별도 경로에 배치한 뒤 ingestion한다.

```bash
export QA_POLICY_CORPUS_DIR=/secure/path/policies
export QA_INGEST_MODE=production
python departments/06-ai-qa-audit/evidence/production_ingestion.py \
  "$QA_POLICY_CORPUS_DIR"
```

Risk/QA의 실제 DB write-through와 Redis 중복·재시작 검증은 별도 smoke에서 수행한다. 이 검증도 Broker 주문과 Ledger Posting은 호출하지 않는다.

```bash
python scripts/run_risk_qa_integration_smoke.py
```
