# Risk 부서 LangGraph·RAG Stack

검토일: 2026-08-04  
상태: 설계 제안. 현재 운영 활성화나 신규 Library 도입을 의미하지 않는다.

이 문서는 Risk 부서의 LangGraph 노드, RAG 기법, 내부 API 호출, 저장소 경계를
정의한다. 부서장·직원 Worker의 현행 모델, fallback과 실행 역할은
[Worker Model Matrix](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)와
[Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md)를 따른다.

**2026-08-06 tool 강등**: `core-risk-worker`·`derivatives-counterparty-worker`는 결정론 `risk-runner` 하나로
합쳐졌다(`WORKER_SPECS` LLM Registry 밖, 매 케이스 항상 실행). 아래 표의 두 Worker 행은 강등 전 설계를
그대로 남긴 이력 기록이다 — 실제 실행 경로는 `departments/03-risk/risk_employee_workers.py`의 `risk_runner()`를 따른다.

## 1. 절대 경계

- Worker는 `worker-context.v1` advisory만 생성한다.
- 주문 제출, Risk 승인·축소·거부, 한도 변경, 원장 수정은 Worker나 Hermes가 소유하지 않는다.
- 바인딩 판정은 `departments/03-risk/engine/risk_engine.py`가 소유한다.
- PIT, staleness, limit, idempotency, state transition은 결정론적 코드가 검사한다.
- LLM은 관련성 판단과 근거 기반 서술만 수행한다.
- LLM이 임의 URL, Broker, LS Open API, Supabase에 직접 접근하지 않는다.
- 모든 HTTP·DB·Redis 호출은 LangGraph의 allow-listed Tool Node와 내부 API를 통해 수행한다.

## 2. 공통 Worker Graph

현재 Worker 구현은 `tool → worker_llm → validate`의 최소 Graph다. RAG가 필요한 Worker는 아래 공통 Graph를 단계적으로 확장한다.

```text
intake
  → schema/auth/scope/as_of 검증
  → 결정론적 Risk Context Tool
  → AdaptiveRAG Router
       ├─ no-RAG / hot path
       ├─ hybrid vector retrieval
       ├─ PIKE-style atomic decomposition
       └─ LightRAG entity/summary retrieval
  → Qwen grade 또는 advisory summary
  → PIT·citation·schema·contradiction 검증
  → trace/input_hash/output_hash 기록
  → worker-context.v1
  → 실패 시 HOLD 또는 REJECT+HALTED
```

`AdaptiveRAG Router`는 LLM이 임의로 경로를 선택하게 두지 않는다. case type, query complexity, document relationship flag, `as_of`, 허용 scope를 결정론적으로 확인하고 기본값은 `no-RAG` 또는 일반 vector 검색으로 둔다.

## 3. 직원별 Stack

| Worker | LangGraph 스킬 | RAG 배정 | 허용 Tool/API | 저장소 경계 |
|---|---|---|---|---|
| `core-risk-worker` (강등, `risk-runner`로 흡수) | `freshness_guard`, `snapshot_fan_in`, `exposure_summary`, `liquidity_metric`, `replay`, `contract_validate`, `risk_engine_call`, `reason_code_map`, `idempotency_check`, `fail_closed` | Pre-trade에는 RAG를 사용하지 않는다. 사후 설명에 한해 AdaptiveRAG를 검토한다. 정책 문서는 Compliance Worker가 검증한다. | `risk.trading_state.read`, `risk.p1.snapshot`, `risk.case.check`, 내부 `market-api`·`portfolio-api`, `POST /investment-cases/{case_id}/risk-check` | `risk.input_snapshots`와 Exposure Snapshot read-only, Risk Engine을 통해서만 Risk Request/Decision에 접근 |
| `compliance-policy-worker` | `PIT_filter`, `hybrid_retrieve`, `claim_decompose`, `citation_verify`, `grounded_fallback`, `retry_budget` | Agentic RAG 기본. 문서가 커지면 PIKE-RAG, 다문서 관계가 반복되면 LightRAG | `risk.compliance.check`, `POST /risk/v1/compliance/check`, 내부 vector gateway | `research.documents`·`document_versions`·`evidence_chunks` read-only, 구조화 정책은 `risk.policies` |
| `derivatives-counterparty-worker` (강등, `risk-runner`로 흡수) | `snapshot_freshness`, `greeks_margin_gate`, `counterparty_state`, `stress_check`, `escalation` | 계산 경로에는 RAG를 사용하지 않는다. 계약·상대방 관계 설명이 필요할 때만 GraphRAG 후보 | `risk.trading_state.record.read`, P2 derivatives API, `market-api`, `portfolio-api` | `risk.derivative_snapshots`, `risk.input_snapshots`, 승인된 Exposure read-only |

### 3.1 Compliance Agentic RAG

```text
query normalize
 → policy type / fund scope / as_of 결정
 → PIT + license + ACL filter
 → vector/BM25 hybrid retrieval
 → 필요 시 atomic policy questions
 → 관련성 grade
 → Qwen grounded advisory
 → cited document 존재 여부와 PIT 재검증
```

문서가 없거나 버전이 맞지 않거나 인용이 검증되지 않으면 `ambiguous + escalate=true`로 종료한다. `breach`나 `no_breach`로 추정 승격하지 않는다.

### 3.2 Hot Path 금지

`risk-runner`(옛 `core-risk-worker`)와 결정론적 Risk Engine 사이에는 RAG, 외부 HTTP, 재시도형 LLM 호출을 넣지 않는다. `risk-runner`는 애초에 LLM을 호출하지 않는다(`llm: False`) — Risk Engine 결과를 그대로 옮기기만 한다.

## 4. HTTP·API 호출

| 호출 대상 | 호출 주체 | 목적 | 외부 Credential |
|---|---|---|---|
| Worker Model Gateway | LLM 사용 Worker의 Python Node | 구조화된 advisory 생성 | 모델 endpoint는 gateway 설정이 소유 |
| `GET /risk/v1/trading-state/{scope}` | Market/Liquidity Tool | 현재 Trading State 읽기 | 없음, 내부 인증 |
| `GET /risk/v1/trading-state/{scope}/record` | Counterparty Tool | 상태 변경 이력·미확정 상태 확인 | 없음, 내부 인증 |
| `POST /investment-cases/{case_id}/risk-check` | Pre-trade Tool | Risk Engine 계산 요청 | 없음, 내부 인증 |
| `POST /risk/v1/compliance/check` | Compliance Tool | Agentic RAG 실행 | Policy Store·Embedding Gateway는 내부 서비스 |
| `market-api`·`portfolio-api` | Market/Derivatives Tool | Snapshot·Exposure·Greeks 조회 | LS/Broker Credential은 API 뒤에만 존재 |
| Redis | Trading State·Projection·Cache Tool | 상태·이벤트·검색 cache | Redis URL은 Worker에 직접 노출하지 않고 서비스 설정으로 주입 |

각 Tool 요청에는 `trace_id`, `case_id`, `as_of`, `input_hash`, `profile_version`, `calculation_version`, timeout, idempotency key를 포함한다. Worker는 `PUT/DELETE trading-state` 같은 제한 Command를 직접 호출하지 않는다.

## 5. 저장소와 DB

### 현재 Canonical 저장소

- Vector source: `research.evidence_chunks.embedding` 1024차원과 `api.match_evidence_chunks(...)`
- Policy source: `research.documents`, `research.document_versions`, `research.evidence_chunks`, 구조화 정책 `risk.policies`
- Risk snapshot: `risk.input_snapshots`, `risk.derivative_snapshots`
- Execution/Risk audit: `audit.traces`, `audit.agent_runs`, `audit.tool_calls`, `risk.run_log_events`
- Cache/Event: Redis

Risk Worker는 Research 문서 테이블에 쓰지 않는다. 문서 수집·정정·임베딩은 Research/RAG Service가 소유하고 Risk는 허가된 API로 읽는다.

### 향후 추가할 최소 RAG 감사 테이블

```text
audit.rag_runs
audit.rag_retrievals
audit.rag_atomic_queries
audit.rag_graph_extractions
```

저장 값은 원문 Prompt나 Secret이 아니라 query/input hash, profile/model version, retriever mode, `as_of`, selected chunk IDs, score, attempt, grounded, escalation, latency다.

별도 Qdrant나 Neo4j를 즉시 추가하지 않는다. 먼저 Supabase pgvector와 관계형 graph projection으로 정확도·latency를 측정한다. 별도 DB가 필요해질 경우에도 Supabase가 Source of Truth이고 Projection은 재생성 가능해야 한다.

예외: `compliance-policy-worker`가 참조하는 정적 규정·정책 코퍼스는
[ADR-0006](../../docs/02-engineering/adr/0006-pinecone-for-risk-qa-static-corpora.md)에
따라 Pinecone을 쓴다 — Order/Ledger에 SQL Join이 필요 없는 참조 데이터이기 때문이며,
이 예외는 Risk Snapshot에 결합되는 evidence(pgvector 유지)에는 적용되지 않는다.

## 6. RAG 기술 도입 순서

1. 현재 Agentic RAG의 `retrieve → grade → generate → hallucination_check → retry`를 Risk Compliance에 유지한다.
2. Vector + BM25 + RRF + PIT/ACL filter + citation verifier를 공통 Retriever 계약으로 만든다.
3. Compliance 문서가 실제 문서로 교체되고 grounded 재시도가 증가하면 PIKE-style atomic decomposition을 도입한다.
4. 기업·상대방·규제기관·계약 간 cross-document 관계가 실제로 반복되면 LightRAG graph projection을 검토한다.
5. Hypergraph는 Risk hot path나 Compliance에 배정하지 않는다. N-ary 원인 추출이 필요한 Incident만 QA가 소유한다.

## 7. 실패 정책과 Acceptance

- Qwen JSON schema 실패: 최대 3회 시도 후 `DEGRADED`와 `HOLD/ESCALATE`
- Embedding·Retriever timeout: cached evidence만 사용하거나 `ambiguous + escalate`
- PIT·ACL·citation 실패: 근거 없는 승인 방향으로 fallback하지 않음
- Risk Engine 오류·Redis 오류·Snapshot stale: `HALTED` 또는 신규 진입 차단
- 새 Worker Tool은 `config.yaml`, `WORKER_SPECS`, API contract, audit trace, 테스트를 함께 변경해야 한다.

운영 전 Acceptance는 동일 입력의 `input_hash`가 같은 Risk 결과를 만들고, 검색된 근거·정책 버전·Tool Call·fallback 사유를 `trace_id`로 재현할 수 있는지 확인한다.

참조: [Risk Profile](hermes/config.yaml), [Risk Worker Graph](risk_employee_workers.py), [Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md), [Unified Domain API](../../docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md), [Agentic RAG](../../skills/agentic-rag/SKILL.md).

## 8. 직원별 Required Skill ID

공통 계약은 [WORKER_SKILL_REGISTRY.md](../../docs/02-engineering/WORKER_SKILL_REGISTRY.md)를 따른다. 아래 목록은 직원 Graph를 작성할 때 처음 고정할 Skill 집합이다. `core-risk-worker`/`derivatives-counterparty-worker` 두 행은 2026-08-06 tool 강등 전 설계의 이력 기록이다 — 실제로는 LangGraph Skill 자체가 없는 결정론 `risk-runner`가 대신한다(3절 참고).

| Worker | Required Skill ID |
|---|---|
| `core-risk-worker` (강등, `risk-runner`로 흡수) | `guard.input_normalize.v1`, `guard.scope_check.v1`, `context.internal_api.v1`, `context.repository_read.v1`, `context.cache_read.v1`, `guard.pit_filter.v1`, `calc.deterministic_gate.v1`, `advisory.grounded_summary.v1`, `verify.schema.v1`, `audit.trace_record.v1`, `audit.replay_manifest.v1`, `audit.cost_latency.v1`, `fallback.retry_budget.v1`, `fallback.human_escalation.v1` |
| `compliance-policy-worker` | `guard.input_normalize.v1`, `guard.scope_check.v1`, `guard.pit_filter.v1`, `guard.prompt_injection_scan.v1`, `context.repository_read.v1`, `context.cache_read.v1`, `rag.route.v1`, `rag.hybrid_retrieve.v1`, `rag.rerank.v1`, `rag.decompose.v1`, `rag.context_stitch.v1`, `rag.self_check.v1`, `advisory.grounded_summary.v1`, `verify.schema.v1`, `verify.citation.v1`, `verify.provenance_chain.v1`, `verify.numeric_temporal.v1`, `audit.trace_record.v1`, `audit.cost_latency.v1`, `fallback.retry_budget.v1`, `fallback.human_escalation.v1` |
| `derivatives-counterparty-worker` (강등, `risk-runner`로 흡수) | `guard.input_normalize.v1`, `guard.scope_check.v1`, `context.internal_api.v1`, `context.repository_read.v1`, `context.cache_read.v1`, `guard.pit_filter.v1`, `calc.deterministic_gate.v1`, `advisory.grounded_summary.v1`, `verify.schema.v1`, `verify.provenance_chain.v1`, `audit.trace_record.v1`, `audit.replay_manifest.v1`, `audit.cost_latency.v1`, `fallback.human_escalation.v1` |

`rag.graph_context.v1`은 Compliance 문서 간 관계가 실제로 증가한 뒤에만 Compliance Worker에 추가한다. Pre-trade와 계산 Hot Path에는 추가하지 않는다.

## 9. Risk Python 구현 단위

| 구현 단위 | 우선 파일 | 책임 |
|---|---|---|
| Input/Output DTO | `departments/03-risk/skills/contracts.py` | `RiskSkillContext`, `RiskSkillResult`, `RiskAdvisory` Pydantic Schema |
| Guard Nodes | `departments/03-risk/skills/guards.py` | scope, PIT, freshness, redaction, stale input 차단 |
| Context Tools | `departments/03-risk/skills/context_tools.py` | `risk.trading_state.read`, P1 snapshot, market/portfolio API Adapter |
| Risk Gate Adapter | `departments/03-risk/skills/risk_gate.py` | 기존 `engine/risk_engine.py` 호출만 담당. 계산 복제 금지 |
| Policy RAG | `departments/03-risk/skills/policy_rag.py` | `skills/agentic-rag`와 `api.match_evidence_chunks` Adapter |
| Worker Skill Router/Tools | `departments/03-risk/skills/rag_router.py`, `tools.py` | 활성 Worker의 허용 Skill topology와 adapter 경계 |
| Trace/Replay | `departments/03-risk/skills/trace.py` | `trace_id`, `input_hash`, `output_hash`, retry, fallback 기록 |

새 Python 모듈은 기존 `risk_employee_workers.py`가 소유한 Worker Registry를 우회하지 않는다. `WORKER_SPECS`의 Skill 목록과 `hermes/config.yaml`의 Tool allowlist가 일치해야 한다.

## 9.1 실행 코드와 기술 프로필 동기화

이 문서의 Worker별 Stack 표는 이제 [`departments/risk_qa_worker_profiles.py`](../../departments/risk_qa_worker_profiles.py)의 `RISK_WORKER_TECH`와 Risk `WORKER_SPECS`에 반영된 실행 메타데이터와 동기화한다. Worker 결과의 `technology`와 런타임의 `technology_profiles`에서 실제 사용 기술·입력·성과 지표를 확인할 수 있다. 역할 변경은 문서만 수정하지 말고 프로필·`WORKER_SPECS`·allowlist·계약 테스트를 함께 수정한다.

현재 활성화된 경로는 LangGraph guard/skill/tool adapter → Pydantic contract → 필요한
경우 Worker Model Gateway advisory → trace/replay다. pgvector/BM25, PIKE-RAG,
LightRAG, GraphRAG는 데이터·평가셋·비용 게이트를 통과하기 전까지 후보 설계이며
자동 활성화하지 않는다. 특히 pre-trade hot path에는 RAG·외부 HTTP·재시도형 LLM을
추가하지 않는다.

## 10. Risk별 Graph Acceptance

- Market/Liquidity: stale Snapshot, Trading State `HALTED`, API timeout은 모두 신규 진입 차단 방향이어야 한다.
- Pre-trade: 같은 Intent·RiskContext·Policy Version에서 Risk Engine 결과가 동일해야 한다. RAG 또는 Qwen 호출이 없어도 Gate가 동작해야 한다.
- Compliance: 미래 정책, 다른 Fund 정책, 인용되지 않은 정책 문장을 차단해야 한다. grounded=false는 성공이 아니라 `ambiguous/escalate`다.
- Derivatives/Counterparty: Greeks·margin·counterparty 상태가 하나라도 누락되면 승인 방향으로 보간하지 않는다.
- 모든 Worker: 허용되지 않은 Skill/Tool 호출, schema 오류, Trace 기록 오류는 `DEGRADED` 또는 `HOLD`로 종료한다.

## 8. 현재 구현 상태 (2026-08-04)

- 구현됨: `SkillContext`/`SkillResult`, 입력·scope·PIT/freshness 가드, allow-listed Tool 경계, bounded RAG Router, retry·escalation 결과, trace/replay manifest.
- 구현됨: 네 명의 Worker Graph가 위 경계를 통과한 뒤에만 Qwen 요약을 호출하고, Worker report에 `skills`, `rag_plan`, `skill_results`, `trace`를 반환한다.
- 아직 비활성: 실제 Retriever·pgvector/Graph projection 연결, 내부 HTTP API 운영 URL, 실제 정책 corpus 교체. 이 항목들은 placeholder corpus를 운영 근거로 사용하지 않도록 다음 단계로 남긴다.
