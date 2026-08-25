# AI-QA 부서 LangGraph·RAG·Graph Stack

검토일: 2026-08-04  
상태: 설계 제안. 현재 구현·운영 활성화와 향후 확장을 구분한다.

AI-QA 부서는 Risk·Research·Trading·Quant·Accounting의 결과를 독립 검증한다. 부서장은 Hermes와 `openai-codex/gpt-5.6-luna`를 사용하고, 직원 Worker는 현재 Ollama `qwen3:1.7b`를 사용한다. Worker 결과는 `qa.worker-context.v1` advisory이며 `EvidenceQaEngine`, `ModelRiskEngine`, `InternalAuditEngine`, Permission Engine, Incident 상태 머신이 최종 통제한다.

**2026-08-06 tool 강등**: `evidence-qa-worker`·`model-and-internal-audit-worker`·`ops-and-permission-worker`는
결정론 `qa-runner` 하나로 합쳐졌다(`WORKER_SPECS` LLM Registry 밖, 매 케이스 항상 실행). 아래 표의 세
Worker 행은 강등 전 설계를 그대로 남긴 이력 기록이다 — 실제 실행 경로는
`departments/06-ai-qa-audit/qa_employee_workers.py`의 `qa_runner()`를 따른다. 남은 LLM Worker는
`hallucination-critic-worker`·`incident-postmortem-worker` 둘뿐이다.

## 1. 절대 경계

- QA Worker는 Risk 승인, 주문 제출, 원장 Posting, Finding 종료, Corrective Action 종료를 수행하지 않는다.
- `PASS/WARN/FAIL`, `ESCALATE`, PIT, citation, schema, threshold, allowlist 판정은 결정론적 코드가 소유한다.
- LLM은 claim decomposition, relevance grading, 근거 기반 서술, 원인 후보 추출만 수행한다.
- `UNSUPPORTED`·`CONTRADICTED`를 감지한 Hallucination Worker는 원래 판정을 뒤집지 않는다.
- Hypergraph 결과는 `INFERENCE`로만 저장하며 Incident를 자동 종결하지 않는다.
- Worker가 임의 URL, 검색엔진, Notion, Broker, LS API, Supabase에 직접 접근하지 않는다.

## 2. 공통 QA Graph

```text
artifact/case intake
  → auth/scope/PIT/schema 검증
  → deterministic QA Engine
  → signal router
       ├─ evidence retrieval
       ├─ hallucination review
       ├─ model/lineage graph
       ├─ ops/permission check
       └─ incident timeline/hypergraph
  → Qwen advisory 또는 원인 후보
  → citation·numeric·date·Fact/Inference 검증
  → audit trace와 claim evidence 저장
  → qa.worker-context.v1
  → 실패 시 ESCALATE + manual_review_required
```

QA에서 `AdaptiveRAG`는 LLM이 임의로 통제하는 Agent가 아니다. 입력 signal과 문서 관계, Incident 여부, `as_of`, license scope를 결정론적으로 확인한 뒤 검색 경로를 고른다.

## 3. 직원별 Stack

| Worker | LangGraph 스킬 | RAG/Graph 배정 | 허용 Tool/API | 저장소 경계 |
|---|---|---|---|---|
| `evidence-qa-worker` (강등, `qa-runner`로 흡수) | `claim_atomize`, `hybrid_retrieve`, `PIKE_decompose`, `SelfRAG_sufficiency`, `citation_verify`, `numeric_date_check` | Agentic RAG 기본. 복합 claim은 PIKE-RAG, cross-document entity는 LightRAG | `qa.evidence.check`, `POST /qa/v1/evidence/check`, 향후 `check:decomposed` | Research Evidence read-only, `audit.claim_checks`·`audit.qa_decisions`는 QA API를 통해 기록 |
| `hallucination-critic-worker` | `retrieve_again`, `contradiction_check`, `citation_audit`, `verdict_lock` | Self-RAG 형태의 제한된 재검색. Evidence Worker의 근거를 우선 재사용 | `qa.evidence.rag`, 내부 Evidence API | `UNSUPPORTED`·`CONTRADICTED` claim과 근거만 읽기 |
| `model-and-internal-audit-worker` (강등, `qa-runner`로 흡수) | `lineage_traversal`, `artifact_replay`, `model_prompt_dataset_join`, `SoD_check` | `audit.artifact_lineage` 기반 GraphRAG. Neo4j는 후순위 Projection | `/qa/v1/model-risk/evaluate`, `/qa/v1/internal-audit/evaluate` | `audit.artifact_versions`, `artifact_lineage`, `agent_runs`, `tool_calls`, `access_events`, Workforce read-only |
| `ops-and-permission-worker` (강등, `qa-runner`로 흡수) | `metric_threshold`, `trace_join`, `allowlist_check`, `anomaly_summary` | 기본 RAG 없음. 운영 로그 설명이 필요할 때만 제한적 AdaptiveRAG | `/qa/v1/ops/evaluate`, `/qa/v1/tool-permission/check`, OTEL/Prometheus 내부 API | `audit.agent_runs`, `audit.tool_calls`, `audit.access_events` read-only |
| `incident-postmortem-worker` | `timeline_normalize`, `fact_inference_split`, `entity_coreference`, `hyperedge_extract`, `human_review_gate` | HyperExtraction/Hypergraph. Self-RAG로 근거를 검증하고 LightRAG는 보조 | Incident Timeline API, 향후 `/qa/v1/incidents/{id}/extract-hypergraph` | `audit.incidents`, `incident_events`, `corrective_actions`, hyperedge 결과 |

## 4. Evidence·Citation RAG

```text
claim normalize
 → atomic claim/question 분해
 → observed_at/published_at/as_of/PIT filter
 → license·scope filter
 → vector + BM25 hybrid retrieval
 → 필요 시 entity/summary retrieval
 → Qwen relevance grade
 → EvidenceQaEngine의 결정론적 numeric/date/unit/citation 검증
 → SUPPORTED/PARTIAL/UNSUPPORTED/CONTRADICTED 보조 서술
```

현재 baseline은 `skills/agentic-rag/`의 Evidence corpus를 사용한다. 실제 운영에서는 `research.evidence_chunks`와 service-role 전용 `api.match_evidence_chunks(...)`를 연결해야 하며, `SAMPLE_PLACEHOLDER` corpus 결과를 운영 판단에 사용하지 않는다.

PIKE-RAG는 문서가 커졌을 때 1차 도입한다. 먼저 query decomposition, context stitching, multi-granularity retrieval만 도입하고 별도 Graph DB를 만들지 않는다. QA Evidence의 `check:decomposed` 응답은 각 iteration과 selected chunk ID를 남겨야 한다.

## 5. Model Risk·Internal Audit Graph

현재 관계형 테이블만으로도 다음 Graph를 만들 수 있다.

```text
Agent Profile
  └─ Profile Version
      └─ Model / Prompt / Skill / Tool Allowlist
          └─ Agent Run
              └─ Tool Call / Access Event

Artifact Version
  └─ parent-child artifact_lineage
      └─ Dataset / Strategy / Release Review / QA Decision
```

GraphRAG는 위 계보에서 “어떤 모델·Prompt·Dataset·Tool 권한이 어떤 결과에 영향을 줬는가”를 탐색하는 데만 사용한다. PASS/FAIL이나 권한 위반 여부는 각각 ModelRiskEngine과 Permission Engine이 결정한다.

초기에는 `audit.artifact_lineage`, `audit.agent_runs`, `audit.tool_calls`, `audit.access_events`를 SQL/관계형 Graph Projection으로 사용한다. traversal 성능과 관계 복잡도가 실제 병목이 된 뒤에만 Neo4j Projection을 검토한다.

## 6. Incident HyperExtraction

Incident Timeline의 FACT/INFERENCE 기록을 입력으로 받아 다음 흐름을 사용한다.

```text
incident timeline
 → entity/event normalization
 → candidate n-ary relation extraction
 → evidence/citation/self-check
 → audit.hyperedges + hyperedge_members 저장
 → human/QA verification
 → corrective action 초안
```

예를 들어 `Feed 지연 + Threshold 오설정 + 승인 지연 → Incident`는 일반적인 두 노드 Edge보다 Hyperedge가 적합하다. 단, 추출 결과는 확정 Root Cause가 아니라 `INFERENCE`다. `verify-and-close`는 Incident Worker가 호출하지 않는다.

## 7. HTTP·API 호출

| 호출 대상 | 호출 Worker | 목적 | 외부 Credential |
|---|---|---|---|
| Ollama OpenAI-compatible endpoint | 모든 Worker의 Python LLM Node | JSON advisory·분류·근거 서술 | 로컬 `OLLAMA_BASE_URL` |
| `POST /qa/v1/evidence/check` | Evidence Worker | Agentic Evidence RAG | 내부 Evidence/Embedding Gateway |
| `POST /qa/v1/model-risk/evaluate` | Model Risk Worker | 결정론적 Model Risk 평가 | 없음, 내부 인증 |
| `POST /qa/v1/internal-audit/evaluate` | Internal Audit Worker | SoD·재현성·계보 평가 | 없음, 내부 인증 |
| `POST /qa/v1/ops/evaluate` | Ops Worker | latency/error/cost threshold 평가 | OTEL/Prometheus 내부 접근 |
| `POST /qa/v1/tool-permission/check` | Permission Worker | Allowlist 위반 결정 | 없음, 내부 인증 |
| Incident Timeline API | Incident Worker | FACT/INFERENCE event·Action 초안 기록 | 없음, 내부 인증 |
| Redis | QA Worker/QA Event Consumer | Risk event, cache, retry state | Redis 설정으로 주입 |

Worker가 직접 쓰기를 수행하지 않는다. `qa.incident.record` 같은 Tool이 API와 Repository를 통해 허용된 Event만 기록하고, Finding/Corrective Action의 최종 상태 전이는 독립 검증 규칙을 거친다.

## 8. 저장소와 DB

### 현재 Canonical 저장소

- Evidence: `research.documents`, `research.document_versions`, `research.evidence_chunks`
- Vector query: `api.match_evidence_chunks(...)`
- QA 판정: `audit.claim_checks`, `audit.qa_decisions`, `audit.findings`
- 계보: `audit.artifact_versions`, `audit.artifact_lineage`
- 운영 Trace: `audit.agent_runs`, `audit.tool_calls`, `audit.access_events`
- Incident: `audit.incidents`, `audit.incident_events`, `audit.corrective_actions`
- Cache/Event: Redis

### 향후 최소 확장 테이블

```text
research.evidence_entities
research.evidence_relations
research.evidence_entity_mentions
research.evidence_graph_summaries

audit.rag_runs
audit.rag_retrievals
audit.rag_atomic_queries
audit.rag_graph_extractions
audit.hyperedges
audit.hyperedge_members
```

RAG 감사 테이블에는 원문 Prompt·Secret을 저장하지 않는다. `query_hash`, `input_hash`, profile/model/embedding version, retriever mode, `as_of`, chunk IDs, relation IDs, confidence, grounded, escalation, latency만 저장한다.

별도 Vector DB는 즉시 추가하지 않는다. Supabase pgvector가 Source of Truth이며, Qdrant나 Neo4j가 필요해져도 재생성 가능한 read Projection으로만 운용한다.

예외: `hallucination-critic-worker`가 참조하는 정적 규정·Incident 참고 코퍼스는
[ADR-0006](../../docs/02-engineering/adr/0006-pinecone-for-risk-qa-static-corpora.md)에
따라 Pinecone을 쓴다 — Order/Ledger에 SQL Join이 필요 없는 참조 데이터이기 때문이며,
QA 판정·Audit Trail(`audit.rag_runs` 등)의 Source of Truth는 계속 Supabase다.

## 9. 공통 안전·Acceptance

- Evidence가 없거나 citation/PIT/ACL 검증에 실패하면 `UNSUPPORTED` 또는 `ESCALATE`로 낮춘다.
- Qwen JSON schema 실패는 최대 3회 후 `DEGRADED + ESCALATE`다.
- `audit.agent_runs/tool_calls` 기록 실패는 PASS로 승격하지 않는다.
- `TIMED_OUT`을 `FAILED`나 `PASS`로 추정하지 않는다.
- Hyperedge는 사람이 검증하기 전까지 Incident 종료·Corrective Action 완료의 근거가 아니다.
- 새 RAG Tool은 `config.yaml`, `WORKER_SPECS`, API contract, audit trace, migration, 테스트를 함께 변경한다.

기법 도입 Acceptance는 retrieval recall/precision, citation precision/recall, grounded rate, contradiction detection, escalation rate, latency, token cost, human acceptance를 golden set과 replay로 측정한다.

## 10. 도입 순서

1. 현재 Agentic RAG와 EvidenceQaEngine의 결정론적 검증 경계를 유지한다.
2. Hybrid Vector + BM25 + PIT/ACL + citation verifier를 연결한다.
3. QA Evidence에 PIKE-style decomposition과 AdaptiveRAG Router를 추가한다.
4. Model Risk/Internal Audit에 기존 관계형 계보 기반 GraphRAG를 추가한다.
5. Incident 데이터가 충분히 쌓인 뒤 HyperExtraction을 human-reviewed inference로 추가한다.
6. `qwen3:1.7b`의 decomposition·entity extraction 품질을 benchmark하고, 모델 변경은 HR·QA·CEO 승인 후 수행한다.

참조: [QA Profile](hermes/config.yaml), [QA Worker Graph](qa_employee_workers.py), [Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md), [Unified Domain API](../../docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md), [Agentic RAG](../../skills/agentic-rag/SKILL.md).

## 11. 직원별 Required Skill ID

공통 계약은 [WORKER_SKILL_REGISTRY.md](../../docs/02-engineering/WORKER_SKILL_REGISTRY.md)를 따른다. 아래 목록은 QA 직원 Graph를 작성할 때 처음 고정할 Skill 집합이다. `evidence-qa-worker`/`model-and-internal-audit-worker`/`ops-and-permission-worker` 세 행은 2026-08-06 tool 강등 전 설계의 이력 기록이다 — 실제로는 LangGraph Skill 자체가 없는 결정론 `qa-runner`가 대신한다(3절 참고).

| Worker | Required Skill ID |
|---|---|
| `evidence-qa-worker` (강등, `qa-runner`로 흡수) | `guard.input_normalize.v1`, `guard.scope_check.v1`, `guard.pit_filter.v1`, `guard.prompt_injection_scan.v1`, `context.repository_read.v1`, `context.cache_read.v1`, `rag.route.v1`, `rag.hybrid_retrieve.v1`, `rag.rerank.v1`, `rag.decompose.v1`, `rag.context_stitch.v1`, `rag.entity_link.v1`, `rag.self_check.v1`, `advisory.grounded_summary.v1`, `verify.schema.v1`, `verify.citation.v1`, `verify.provenance_chain.v1`, `verify.numeric_temporal.v1`, `verify.contradiction.v1`, `audit.trace_record.v1`, `audit.cost_latency.v1`, `fallback.retry_budget.v1`, `fallback.human_escalation.v1` |
| `hallucination-critic-worker` | `guard.input_normalize.v1`, `guard.scope_check.v1`, `guard.prompt_injection_scan.v1`, `context.repository_read.v1`, `rag.hybrid_retrieve.v1`, `rag.self_check.v1`, `verify.schema.v1`, `verify.citation.v1`, `verify.provenance_chain.v1`, `verify.contradiction.v1`, `audit.trace_record.v1`, `fallback.human_escalation.v1` |
| `model-and-internal-audit-worker` (강등, `qa-runner`로 흡수) | `guard.input_normalize.v1`, `guard.scope_check.v1`, `context.repository_read.v1`, `rag.route.v1`, `rag.entity_link.v1`, `rag.graph_context.v1`, `calc.deterministic_gate.v1`, `verify.schema.v1`, `verify.provenance_chain.v1`, `verify.contradiction.v1`, `audit.trace_record.v1`, `audit.replay_manifest.v1`, `audit.cost_latency.v1`, `fallback.human_escalation.v1` |
| `ops-and-permission-worker` (강등, `qa-runner`로 흡수) | `guard.input_normalize.v1`, `guard.scope_check.v1`, `context.internal_api.v1`, `calc.deterministic_gate.v1`, `verify.schema.v1`, `audit.trace_record.v1`, `fallback.retry_budget.v1`, `fallback.human_escalation.v1` |
| `incident-postmortem-worker` | `guard.input_normalize.v1`, `guard.scope_check.v1`, `guard.prompt_injection_scan.v1`, `context.repository_read.v1`, `guard.pit_filter.v1`, `rag.route.v1`, `rag.entity_link.v1`, `rag.graph_context.v1`, `rag.hyper_extract.v1`, `rag.self_check.v1`, `verify.schema.v1`, `verify.citation.v1`, `verify.provenance_chain.v1`, `audit.trace_record.v1`, `audit.cost_latency.v1`, `fallback.human_escalation.v1` |

`rag.hyper_extract.v1`은 Incident 데이터가 충분히 쌓인 뒤 `incident-postmortem-worker`에만 추가한다. Hyperedge를 자동 Root Cause나 Finding close의 근거로 사용하지 않는다.

## 12. QA Python 구현 단위

| 구현 단위 | 우선 파일 | 책임 |
|---|---|---|
| Input/Output DTO | `departments/06-ai-qa-audit/skills/contracts.py` | `QASkillContext`, `QASkillResult`, `EvidenceRef`, `InferenceRecord` Schema |
| Guard Nodes | `departments/06-ai-qa-audit/skills/guards.py` | scope, PIT, redaction, artifact schema, source authority 확인 |
| Evidence RAG | `departments/06-ai-qa-audit/skills/evidence_rag.py` | hybrid/PIKE/LightRAG Retriever Adapter와 citation Context 생성 |
| Lineage Graph | `departments/06-ai-qa-audit/skills/lineage_graph.py` | `artifact_lineage`, model/prompt/dataset/tool 관계 조회 |
| Incident Graph | `departments/06-ai-qa-audit/skills/incident_graph.py` | Timeline에서 Hyperedge 후보를 만들고 `INFERENCE`로 표시 |
| QA Gate Adapter | `departments/06-ai-qa-audit/skills/qa_gate.py` | 기존 `evidence_qa_engine.py`, `model_risk.py`, `internal_audit.py` 호출 |
| Worker Graph Factory | `departments/06-ai-qa-audit/skills/graph_nodes.py` | Worker별 조건부 topology와 signal routing |
| Trace/Replay | `departments/06-ai-qa-audit/skills/trace.py` | `audit.agent_runs/tool_calls`, input/output hash, replay manifest |

기존 결정론적 Engine은 이 모듈에서 재구현하지 않고 Adapter로 호출한다. QA Worker가 `audit.qa_decisions`, `findings`, `corrective_actions`의 상태를 직접 바꾸지 않도록 API Tool을 별도로 제한한다.

## 13. QA별 Graph Acceptance

- Evidence QA: citation ID가 실제 PIT·license·scope 조건을 만족하지 않으면 `SUPPORTED`로 반환하지 않는다.
- Hallucination Critic: 원래 `UNSUPPORTED/CONTRADICTED` 판정을 `SUPPORTED`로 바꿀 수 없다.
- Model/Internal Audit: 관계형 Lineage 누락을 정상으로 추정하지 않고 `ESCALATE`한다.
- Ops/Permission: Tool allowlist 판정은 LLM 없이 결정되며, 예외·timeout은 `DENIED` 또는 `CRITICAL`로 처리한다.
- Incident: FACT와 INFERENCE를 분리하고, Hyperedge에는 근거 Event ID와 extraction version을 남긴다.
- 모든 Worker: `audit.agent_runs/tool_calls` 기록에 실패하면 QA PASS로 승격하지 않는다.

## 14. Skill 구현 우선순위

1. `contracts.py`, `guards.py`, `trace.py`, `fallback`을 먼저 구현한다.
2. Evidence Worker에 `rag.route`, `rag.hybrid_retrieve`, `verify.citation`을 연결한다.
3. Hallucination Critic에 `verify.contradiction`과 제한된 재검색만 연결한다.
4. Model/Internal Audit에 관계형 Lineage Graph Adapter를 연결한다.
5. Incident 데이터가 축적된 후 `rag.hyper_extract`를 human-review 흐름에 추가한다.

## 11. 현재 구현 상태 (2026-08-04)

- 구현됨: `SkillContext`/`SkillResult`, 입력·scope·PIT 가드, allow-listed Tool 경계, bounded RAG Router, retry·escalation 결과, trace/replay manifest.
- 구현됨: 다섯 명의 Worker Graph가 evidence boundary를 통과한 뒤에만 Qwen 요약을 호출하고, Worker report에 `skills`, `rag_plan`, `skill_results`, `trace`를 반환한다.
- 아직 비활성: 실제 Evidence Retriever·pgvector/Graph projection 연결, 내부 HTTP API 운영 URL, 실제 corpus와 golden set. `SAMPLE_PLACEHOLDER` 근거는 운영 QA PASS에 사용할 수 없다.
