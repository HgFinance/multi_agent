# Risk·QA Worker Skill Registry

검토일: 2026-08-04  
상태: LangGraph 구현 기준 제안

이 문서는 Risk·AI-QA Worker가 공유할 수 있는 Skill ID와 Graph Node 계약을 정의한다. 직원 역할·Tool 권한의 Source of Truth는 여전히 각 부서 `hermes/config.yaml`, `WORKER_SPECS`, [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md)다. 이 문서는 각 Worker Graph를 작성할 때 재사용할 구현 단위를 정한다.

## 1. Skill 분류

| Prefix | 의미 | LLM 사용 |
|---|---|---|
| `guard.*` | 입력·권한·PIT·비밀·환경 경계 | 금지, 결정론적 |
| `context.*` | 내부 API·Repository에서 정형 Context 조회 | 금지, Tool Node |
| `calc.*` | Risk/QA 계산·Threshold·상태 전이 | 금지, 기존 엔진 호출 |
| `rag.*` | 검색 경로·분해·재검색·그래프 Context | 관련성 판단만 LLM 보조 가능 |
| `verify.*` | Schema·Citation·숫자·날짜·모순 검증 | 금지, 결정론적 |
| `advisory.*` | 근거 기반 비바인딩 설명 | Qwen 허용 |
| `audit.*` | Trace·Hash·Replay·Run 기록 | 금지, 결정론적 |
| `fallback.*` | Retry·Circuit Breaker·Escalation | 금지, 결정론적 |

## 2. 공통 Skill 계약

모든 Skill은 다음 입력을 받는다.

```python
class SkillContext(BaseModel):
    trace_id: UUID
    case_id: UUID | None
    worker_id: str
    profile_version: str
    as_of: datetime
    input_hash: str
    allowed_scopes: tuple[str, ...]
    timeout_ms: int
    attempt: int
```

모든 Skill은 다음 경계 결과를 반환한다.

```python
class SkillResult(BaseModel):
    skill_id: str
    status: Literal["COMPLETED", "DEGRADED", "ESCALATE"]
    output: dict[str, Any]
    evidence_refs: list[str] = []
    tool_calls: list[str] = []
    output_hash: str
    error_code: str | None = None
    escalate: bool = False
```

Graph Node는 `SkillResult.status != COMPLETED`를 성공으로 변환하지 않는다. Risk는 `HOLD/REJECT/HALTED`, QA는 `ESCALATE/manual_review_required`로 안전하게 종료한다.

## 3. 공통 필수 Skill ID

| Skill ID | Node 역할 | 구현 형태 | 실패 시 |
|---|---|---|---|
| `guard.input_normalize.v1` | Pydantic 입력·필수 필드·타입 정규화 | Python Node | `INVALID_INPUT` + 종료 |
| `guard.scope_check.v1` | fund/case/license/profile scope 확인 | Python Node | `SCOPE_DENIED` |
| `guard.pit_filter.v1` | `as_of`, `observed_at`, `published_at`, effective version 검사 | Python Node | 근거 제거 + escalate |
| `guard.secret_redaction.v1` | Prompt·Log·Trace의 Secret/원문 민감값 제거 | Python Node | 기록 중단 + escalate |
| `guard.prompt_injection_scan.v1` | 검색 문서의 명령형·권한 상승 문장을 데이터로 격리 | Python Node | 해당 chunk 제외 + trace |
| `context.internal_api.v1` | allow-listed API 호출, timeout, response schema 검증 | Tool Node | `DEPENDENCY_UNAVAILABLE` |
| `context.repository_read.v1` | 허용된 Repository read와 input hash 생성 | Tool Node | fail-closed |
| `context.cache_read.v1` | scope·version이 맞는 Redis cache만 재사용 | Tool Node | cache miss 후 재검색 |
| `calc.deterministic_gate.v1` | Risk/QA 기존 결정론 엔진 호출 | 기존 엔진 Adapter | 원래 엔진 상태 유지 |
| `rag.route.v1` | no-RAG/vector/PIKE/graph/hypergraph 경로 선택 | 결정론적 Router | vector 또는 no-RAG 기본값 |
| `rag.hybrid_retrieve.v1` | dense + lexical 검색과 RRF 결합 | Retriever Adapter | 검색 결과 없음 |
| `rag.rerank.v1` | cross-encoder 또는 규칙 기반 후보 재정렬 | Retriever Adapter | 원래 rank로 안전하게 fallback |
| `rag.decompose.v1` | 복합 claim을 원자 질문으로 분해 | Qwen + JSON Schema | 원 질문으로 1회 재시도 |
| `rag.context_stitch.v1` | 문서 전체·섹션·청크를 중복 없이 결합 | Python Node | context를 줄이고 escalate |
| `rag.entity_link.v1` | alias·entity를 canonical ID로 연결 | Dictionary + Qwen candidate | 미확정 Entity는 `UNKNOWN` |
| `rag.graph_context.v1` | entity/relation/summary 관계 Context 조회 | Graph Adapter | vector 결과만 사용 |
| `rag.hyper_extract.v1` | Timeline에서 N-ary relation 후보 추출 | Qwen + JSON Schema + human review | `INFERENCE`로만 저장 |
| `rag.self_check.v1` | 근거 충분성·재검색 필요성 판단 | Qwen 보조 + 규칙 | 최대 1회 재검색 |
| `verify.schema.v1` | Worker 출력 JSON/Pydantic 검증 | Python Node | `DEGRADED` |
| `verify.citation.v1` | cited ID가 실제 관련 근거인지 검증 | Python Node | `UNSUPPORTED/ESCALATE` |
| `verify.provenance_chain.v1` | 문서→버전→청크→Artifact 계보 검증 | Python Node | 근거 사용 중단 |
| `verify.numeric_temporal.v1` | 숫자·날짜·단위·PIT 일치 검사 | Python Node | QA WARN/FAIL |
| `verify.contradiction.v1` | claim과 evidence 간 모순 검사 | NLI/규칙 Adapter | QA FAIL/ESCALATE |
| `advisory.grounded_summary.v1` | 검증된 Context만 Qwen에게 전달해 요약 | Ollama LLM Node | confidence 0 + escalate |
| `audit.trace_record.v1` | trace/run/tool call/input/output hash 기록 | Repository/API Adapter | PASS 승격 금지 |
| `audit.replay_manifest.v1` | profile/model/prompt/data/algorithm version 기록 | Python Node | 재현 불가로 escalate |
| `audit.cost_latency.v1` | latency·token·retry·cache metric 기록 | Python Node | 운영 상태 DEGRADED |
| `fallback.retry_budget.v1` | attempt·backoff·circuit breaker 관리 | Python Node | 부서별 안전 fallback |
| `fallback.human_escalation.v1` | reason code·manual review·owner 생성 | Python Node | 마지막 종료 노드 |

## 4. Graph 작성 규칙

```text
guard.input_normalize
 → guard.scope_check
 → guard.pit_filter (문서/근거 입력이 있을 때)
 → context.internal_api 또는 context.repository_read
 → calc.deterministic_gate (해당 업무일 때)
 → rag.route (RAG 대상일 때)
 → rag.hybrid_retrieve / rag.decompose / rag.graph_context
 → advisory.grounded_summary
 → verify.schema
 → verify.citation / verify.numeric_temporal / verify.contradiction
 → audit.trace_record
 → fallback.human_escalation 또는 worker-context.v1
```

- Python Tool Node가 HTTP·DB·Redis를 호출하고, LLM Node는 URL과 Credential을 알지 못한다.
- 결정론적 검증을 LLM 생성보다 뒤로 미루지 않는다. 특히 Risk Hot Path에서는 `calc.deterministic_gate` 뒤에 advisory만 둔다.
- `retry`는 전체 Graph를 무한 반복하지 않고 Skill별 budget을 둔다. 기본 최대 3회다.
- 모든 Node는 `trace_id`, `input_hash`, `profile_version`, `algorithm_version`을 state에 보존한다.
- Worker Graph는 binding decision을 반환하지 않고 `worker-context.v1` 또는 `qa.worker-context.v1`만 반환한다.

## 5. 권장 Python 모듈 배치

현재 모듈을 대체하지 않고 Adapter/Skill 계층을 추가한다.

```text
departments/03-risk/skills/
  contracts.py          # SkillContext, SkillResult, worker output
  guards.py             # input/scope/PIT/redaction
  tools.py              # market/portfolio/trading-state API adapters
  rag_router.py         # compliance RAG Router/Retriever adapter
  trace.py              # audit/run/replay adapter

departments/06-ai-qa-audit/skills/
  contracts.py          # SkillContext, SkillResult, claim/evidence DTO
  guards.py             # scope/PIT/schema/redaction
  rag_router.py         # deterministic evidence retrieval/router adapter
  tools.py              # bounded QA tool adapters
  trace.py              # audit trace adapter

QA의 CEO post-response 판정은 `orchestration/langsmith_queries.py`의 공통
metadata-only reader와 `orchestration/adapters/qa_audit_projection.py`가 담당한다.
LangSmith 원문 payload나 별도 QA trace reader를 추가하지 않는다.
```

기존 `risk_engine.py`, `evidence_qa_engine.py`, `model_risk.py`, `internal_audit.py`, `tool_permission_check.py`, `incident_timeline.py`는 계산·판정 Owner다. 새 Skill이 같은 판정 로직을 복제하면 안 된다.

## 6. Tool/API 계약 필수 필드

모든 내부 Tool은 다음을 검증한다.

```text
request: trace_id, case_id, as_of, input_hash, profile_version, timeout_ms
response: status, calculation_version/checker_version, output_hash, reason_codes
```

필수 동작:

- HTTP timeout과 retry는 endpoint별로 제한한다.
- POST/Command는 idempotency key를 요구한다.
- GET/read는 Worker scope 밖의 Fund·Case를 반환하지 않는다.
- API 오류·빈 응답·stale 응답을 정상 데이터로 변환하지 않는다.
- 로그에는 Prompt, API key, Portfolio 원문, 전체 Evidence 원문을 남기지 않는다.

## 7. 테스트 Skill

각 Skill은 다음 테스트를 가진다.

| 테스트 | 검증 |
|---|---|
| `test_skill_contract.py` | 입력/출력 Schema와 version 필드 |
| `test_skill_fail_closed.py` | timeout, empty, malformed, unauthorized 입력 |
| `test_skill_replay.py` | 같은 input/profile/version의 동일 결과 |
| `test_skill_trace.py` | trace/run/tool call과 hash 연결 |
| `test_skill_pit.py` | 경계 날짜와 future evidence 차단 |
| `test_skill_rag_grounding.py` | citation 누락·허위 ID·모순 근거 차단 |
| `test_worker_topology.py` | 직원별 허용 Skill/Tool만 실행 |

외부 Redis, Ollama, Supabase 연결 테스트는 단위 테스트와 분리한다. 연결 불가 `skip`은 성공이 아니며 운영 Acceptance에 포함하지 않는다.

## 8. 도입 순서

1. `contracts.py`, `guards.py`, `trace.py`, `fallback`을 먼저 공통화한다.
2. 현재 결정론적 Engine을 `calc.deterministic_gate` Adapter로 감싼다.
3. Risk Compliance와 QA Evidence에만 `rag.route`와 `rag.hybrid_retrieve`를 붙인다.
4. PIKE decomposition, LightRAG graph, HyperExtraction은 각각 golden set과 human review 기준을 통과한 뒤 추가한다.
5. Worker 모델을 변경할 때는 `WORKER_MODEL_MATRIX.md`, Profile, Eval Set, QA 승인, Rollback 계획을 함께 갱신한다.

## 11. 현재 구현 상태 (2026-08-04)

- Risk·AI-QA의 `contracts.py`, `guards.py`, `tools.py`, `trace.py`, `rag_router.py` 기반이 구현되었다.
- 각 Worker Graph는 Skill ID를 state/report에 남기고, scope·Tool·RAG route 결과를 trace manifest로 연결한다.
- 실제 Retriever, 내부 API 운영 URL, pgvector/graph projection은 별도 통합 단계다. 이 단계에서는 외부 네트워크와 placeholder corpus에 의존하지 않는다.
