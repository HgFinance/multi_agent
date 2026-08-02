# Risk·QA 고도화 작업 요약

- 검토일: 2026-08-02 (KST)
- 검토 범위: `departments/03-risk/`, `departments/06-ai-qa-audit/`, 공용 `skills/agentic-rag/`
- 기준 커밋: `276f2e5` (`update risk, ai-qa, agnetic-rag, summary`)
- 이번 작업에서 애플리케이션 코드는 수정하지 않았으며, 본 보고서만 추가한다.

## 결론

Risk·QA의 권한 경계와 결정론적 판정 위임은 프로필과 코드에 반영되어 있다. Risk는 Risk Engine과 Trading State Store가 바인딩 판정을 담당하고, QA는 Evidence QA Engine·감사 컴포넌트의 결과를 해석·에스컬레이션한다. 두 부서 모두 주문 제출·원장 기록·상태 직접 변경 권한을 금지한다.

다만 모든 프로필이 완성된 실행 Agent는 아니다. 현재 구현된 핵심은 Risk의 P0 Pre-Trade Gate/Redis Trading State와 `compliance-policy-agent`, QA의 Evidence QA와 운영·권한·사건 감사 기능이다. `hallucination-critic`, Model Risk, Internal Audit 등은 프로필과 설계만 있거나 `not_started`로 명시된 상태다.

## Risk 반영 내역

참조: [Risk Hermes config](departments/03-risk/hermes/config.yaml), [Risk Engine](departments/03-risk/engine/risk_engine.py), [Trading State Store](departments/03-risk/engine/trading_state_store.py), [Risk Domain API](departments/03-risk/api/app.py)

- `risk-supervisor`, market/liquidity, derivatives/margin, compliance, pre-trade, operational/counterparty의 6개 프로필이 선언되어 있다.
- Pre-Trade Gate는 데이터 신선도, 시장 거래 가능 여부, Mandate/Restricted List, Notional, Buying Power/Oversell, Concentration, Turnover, Trading State/Loss/Drawdown, Counterparty Health를 결정론적으로 순서 검사한다.
- Soft Limit은 `RESIZE`, Hard Limit과 실패는 `REJECT`/진입 차단 방향으로 처리한다. `ENTRY_BLOCKED`, `REDUCE_ONLY`, `HALTED`에서는 축소 주문만 예외가 된다.
- 동일 Intent·Context·Policy Version에 대한 `input_hash` 재현성 원칙이 유지된다.
- Risk Agent는 근거와 권고만 생성하며 최종 `APPROVE/RESIZE/REJECT` 집행과 한도 관리는 Risk Engine에 남아 있다.
- Redis Trading State는 현재 상태를 읽고 쓰는 Hot State로 사용한다. Redis 장애나 불확실 상태는 `HALTED`로 fail-closed하며, Redis가 비어 있다는 이유로 제한 상태를 임의로 해제하지 않는다.
- Risk Domain API와 Redis event publisher/repository 배선이 추가되었지만 Supabase의 Risk 이력 저장은 `accounting.funds` FK 의존성으로 아직 완결되지 않았다.

## QA 반영 내역

참조: [QA Hermes config](departments/06-ai-qa-audit/hermes/config.yaml), [Evidence QA Engine](departments/06-ai-qa-audit/evidence/evidence_qa_engine.py), [QA API](departments/06-ai-qa-audit/api/app.py), [Audit repository](departments/06-ai-qa-audit/audit/repository.py)

- QA Supervisor, Evidence QA, Hallucination Critic, Model Risk, Internal Audit, Agent Ops Monitor, Tool Permission Security Reviewer, Incident Postmortem의 8개 프로필이 선언되어 있다.
- Evidence QA는 Schema, Evidence 존재/권한, PIT, 숫자·단위 인용 일치, Fact/Inference, 상충 근거, Tool 요약 변형, Unsupported Claim Block의 8단계 검사를 결정론적으로 수행한다.
- 근거 일부 무효는 `PARTIAL/WARN`, 근거 부재·상충·변형은 `UNSUPPORTED/CONTRADICTED` 및 `FAIL` 방향으로 처리한다. 실패를 통과로 바꾸는 fallback은 없다.
- `calculation_version`과 `input_hash`로 Risk Engine과 같은 재현성 경계를 둔다.
- Ops Health는 `HEALTHY/DEGRADED/CRITICAL` 및 SEV 등급을 계산하고, Trace/Incident/Tool Permission 컴포넌트는 사실 기록·권한 검증·사건 에스컬레이션을 담당한다.
- QA Redis Event Bus와 worker가 Risk→QA 도메인 이벤트 경로를 제공한다. Redis는 Canonical DB가 아니라 이벤트/Hot State 계층이며, 감사·거래 상태의 최종 원장을 대체하지 않는다.

## Redis 및 Circuit Breaker

공용 [resilience.py](skills/agentic-rag/src/resilience.py)와 [Agentic RAG nodes](skills/agentic-rag/src/nodes.py), [retriever](skills/agentic-rag/src/retriever.py)에 다음이 반영되어 있다.

- LLM Chat과 Embedding 호출에 별도 `CircuitBreaker`를 적용한다. 실패 임계치와 복구 대기 시간은 환경변수로 조정 가능하다.
- `RedisJsonCache`는 Prompt/Embedding/Metric 캐시를 best-effort로 제공한다. Redis 연결·직렬화 실패는 캐시 미스/로그로 처리하며 안전한 판정을 바꾸지 않는다.
- Prompt 크기는 `MAX_CONTEXT_CHARS`로 제한하고, 호출 latency·token·failure·cache hit·graph retrieval 지표를 구조화 로그로 남긴다. 원문 Prompt, Credential, Evidence 본문은 telemetry에 기록하지 않는다.
- Retriever/Grade/Generate/Graph 단계 장애는 `fallback=true`, `grounded=false`, `escalate=true`인 안전한 결과로 종료한다. 외부 모델 장애를 승인으로 간주하지 않으며, fallback 상태에서는 재시도 루프가 계속되지 않는다.
- 실제 Redis 통합 테스트는 환경변수로 지정된 Redis 호스트가 현재 DNS 해석되지 않아 skip되었다. 로컬 fake-client/Circuit Breaker/cache 동작 테스트는 통과했다.

## LightRAG·GraphRAG 구조 검토

- 현재 기본 LangGraph 흐름은 `retrieve → grade → generate → hallucination_check → retry`이며 최대 3회 재시도한다.
- Risk의 정책 RAG는 `compliance-policy-agent`, QA의 근거 RAG는 `evidence-qa-agent`까지 구현되어 있다. 두 코퍼스의 문서는 현재 `SAMPLE_PLACEHOLDER`이므로 실제 운영 정책·근거 판단에는 사용할 수 없다.
- `LocalVectorIndex.search()`의 `DocumentChunk → list[ScoredChunk]` 계약을 유지한 채 `AGENTIC_RAG_RETRIEVAL_MODE=graph|lightrag|graphrag|pike-rag`에서 질의 용어 기반 graph augmentation을 수행한다. 이는 LightRAG/GraphRAG로 교체할 수 있는 호환 경로이며, 실제 Entity/Summary Graph 저장소나 LightRAG 라이브러리를 도입한 완성 구현은 아니다.
- Risk 프로필은 정책 문서 규모·상호참조 증가 시 LightRAG(Entity/Summary level)로 교체하도록 기록하고, QA 프로필은 근거 corpus가 커지고 오탐/누락이 실측될 때 GraphRAG 계열을 검토하도록 기록한다. Hypergraph/Neo4j/실시간 OpenTelemetry·부하 테스트는 백로그다.
- `hallucination-critic`은 Evidence QA의 grounded 결과를 재사용하는 확장 대상으로 남아 있으며, 별도 독립 Agent 구현으로 오인하면 안 된다.

## 프로필·권한 반영 판정

| 점검 항목 | 판정 | 근거 |
|---|---|---|
| Risk/QA Hermes YAML 문법 및 프로필 선언 | PASS | 두 config를 YAML 파싱했고 Risk 6개, QA 8개 personality 확인 |
| 주문·원장·Risk 상태 권한 분리 | PASS | 각 config의 `forbidden_tools`, Domain API, 결정론적 Engine 위임 |
| Risk Agent의 바인딩 집행 금지 | PASS | Risk Engine이 최종 판정과 상태 enforcement 소유 |
| QA의 독립 검증 경계 | PASS | Evidence QA/감사 결과를 해석하며 주문·원장·Risk Limit을 직접 변경하지 않음 |
| Agentic RAG 구현 | PARTIAL | Risk compliance와 QA evidence까지 구현, QA hallucination critic 및 일부 전문 Agent는 미착수 |
| LightRAG/GraphRAG | PARTIAL | 인터페이스 보존형 검색 확장 경로는 있으나 실제 Graph backend는 백로그 |
| 운영 프로필 활성화 | PARTIAL | migration이 실행 가능 baseline만 ACTIVE로 구분하고 나머지는 PROBATION/미구현으로 보존 |

## 검증 결과

- `pytest -q departments/03-risk/tests departments/06-ai-qa-audit/tests`: **104 passed, 8 skipped, exit 0**.
  - skip 사유는 외부 Redis 통합 호스트 DNS 해석 실패이며, fake Redis·안전 fallback·Circuit Breaker·이벤트 계약 테스트는 통과했다.
- `python -m unittest discover -s tests/schema -p "test_*.py" -v`: **12개 중 11 passed, 1 failed**.
  - 실패는 새 `20260802001400_risk_qa_runtime_registration.sql` migration이 `tests/schema/test_schema_contract.py`의 고정 migration 목록에 아직 반영되지 않은 계약 테스트 불일치다.
  - 사용자 요청에 따라 이번 작업에서는 코드/테스트를 수정하지 않고 이 상태를 기록한다.
- `git diff --check`: 문서 작성 전 기준 통과.

## 후속 백로그

1. Schema contract test에 Runtime Registration migration을 반영하고 전체 migration 순서를 재검증한다.
2. `accounting.funds` FK 전제와 RLS를 해결한 뒤 Risk/QA 이력의 실제 Supabase round-trip을 검증한다.
3. 실제 정책·근거 문서로 `SAMPLE_PLACEHOLDER`를 교체하고 PIT·인용 검증 acceptance scenario를 다시 수행한다.
4. `hallucination-critic`, Model Risk, Internal Audit 및 Graph backend는 프로필 등록만으로 ACTIVE 승격하지 않고 독립 acceptance scenario 후 활성화한다.
5. Redis 통합 환경과 OpenTelemetry/Prometheus·부하 테스트 환경을 제공한 뒤 skip된 통합 시나리오를 재실행한다.
