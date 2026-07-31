# Risk·QA Domain API 설계서

> 작성: 동규님 (Risk/QA Domain Owner) · 작성일: 2026-07-31
> 상위 계약: [MINIMUM_SERVICE_UNIT_SPEC.md](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md) §5/§8/§11 (Investment Case 데이터·Command·Event·API 계약),
> [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) §7 (FastAPI+Pydantic Backend, Hermes는 API/MCP 경계로만 통신)
>
> **이 문서가 하는 일과 안 하는 일**: `risk_engine.py`/`evidence_qa_engine.py` 등 이미 있는 결정론적 Python과
> 이미 구현된 Agentic RAG 그래프를 FastAPI로 감싸는 방법을 정의한다. 여기에 **PIKE-RAG L2(§3.7)와
> Hyper-Extraction(§3.8)의 틀도 미리 잡아뒀다** — 지금은 코드가 없지만, 트리거 조건이 충족됐을 때 바로
> 구현에 들어갈 수 있게 인터페이스 모양을 먼저 고정한 것이다(둘 다 상태를 "틀만 설계 — 미구현"으로 명시).
> **§7에 ai-office Frontend 연결(부서원·부서장 이동, 회의 시각화)도 같이 설계했다.** Risk가 "이미 이름
> 붙은" `POST /investment-cases/{case_id}/risk-check`를 구체화하는 부분은 상위 문서를 그대로 따르는 것이라
> 승인이 따로 필요 없다. QA의 Case 단위 게이트는 상위 문서에 아직 이름이 없어서 **§3.1에 제안(Proposed)으로
> 명시**했다 — 이 부분만 팀 승인이 필요하다. 마지막 §8에 무엇이 이미 확정이고 무엇이 제안/설계뿐인지 표로 정리했다.

---

## 0. 왜 API로 감싸나

지금 `risk_engine.py`/`evidence_qa_engine.py` 등 7개 모듈은 같은 Python 프로세스 안에서 직접 import해서
쓰는 라이브러리다. `TECH_STACK_DECISIONS.md` §7이 이미 "Hermes를 Domain Backend의 Python Environment에
직접 설치하지 않는다. 독립 Image와 API/MCP 경계로 통신한다"고 정해뒀다 — 즉 Hermes 프로필(다른 프로세스,
다른 머신일 수도 있음)이 이 엔진들을 부르려면 애초에 HTTP API가 있어야 한다. 지금 이 문서는 그 API의
모양을 정의하는 것부터 시작한다.

## 1. 공통 규약

### 1.1 경로와 버전

- Case에 종속된 판정: `MINIMUM_SERVICE_UNIT_SPEC.md` §11이 이미 정한 대로 `/investment-cases/{case_id}/...` 아래에 둔다.
- 부서가 단독 소유하는(= Case 하나에 안 묶이는) 자원: `/risk/v1/...`, `/qa/v1/...`.
- `v1`은 API Path Version. `calculation_version`(Risk)/`checker_version`(QA)은 이것과 별개로 판정 로직 버전을 가리킨다 — 코드에 이미 있는 필드, 섞지 않는다.

### 1.2 인증

각 결정론적 서비스는 이미 자기 `service_identity`를 갖고 있다(`RiskEngine(service_identity="svc_risk_engine")`,
`EvidenceQaEngine(service_identity="svc_qa_evaluator")`). 호출자(Hermes Specialist, 다른 부서 서비스)는 짧은
수명의 Service Token으로 인증하고, 토큰의 `sub`가 `trace_recorder.py`의 `agent_id`/`profile_version_id`와
연결되어야 Trace가 끊기지 않는다. Frontend·Browser는 이 API를 직접 호출하지 않는다 — `AI_OFFICE_FRONTEND_PLAN.md`
§6대로 FastAPI BFF가 유일한 진입점이다.

### 1.3 멱등성

Risk/QA 코드는 이미 멱등키가 될 수 있는 필드를 갖고 있다 — 새로 설계할 필요 없이 그대로 쓴다.

| 서비스 | 멱등키로 쓸 필드 | 이미 있는 동작 |
|---|---|---|
| `RiskEngine.check_order` | `risk_request_id`(호출자 지정 가능, optional) | 같은 값 재호출 시 같은 판정 재현(`input_hash` 검증) |
| `EvidenceQaEngine.check_artifact` | `qa_decision_id`(호출자 지정 가능, optional) | 위와 동일 |
| `TraceRecorder.start_run` | `(profile_version_id, input_hash)` | 이미 코드가 자동으로 처리 — 같은 조합이면 RUNNING 중인 기존 Run을 그대로 반환 |

`PUT /risk/v1/trading-state/{scope}`처럼 멱등키가 없는 상태 변경 Command는 `AI_OFFICE_FRONTEND_PLAN.md` §6
Command 봉투(`idempotency_key`, `expected_version`, `reason`)를 그대로 가져온다.

### 1.4 에러 봉투

```json
{
  "error_code": "RISK_STALE_SNAPSHOT",
  "message": "market snapshot is older than max_data_staleness_seconds",
  "detail": {"snapshot_age_seconds": 12},
  "trace_id": "..."
}
```

`error_code`는 각 엔진의 `RejectReason`/`CheckFailureReason`/`*Error` Enum·Exception 이름을 그대로 쓴다 —
새 Enum을 API 레이어에서 또 만들지 않는다.

---

## 2. Risk Domain API

### 2.1 Case에 종속된 판정

| Method/Path | 감싸는 함수 | 비고 |
|---|---|---|
| `POST /investment-cases/{case_id}/risk-check` | `RiskEngine.check_order(intent, ctx, risk_request_id)` | 이미 `MINIMUM_SERVICE_UNIT_SPEC.md` §11에 이름이 있음. Command: `EvaluateRisk`. 성공 시 Event `investment_case.risk_approved`/`_resized`/`_rejected` 중 하나를 낸다. |

**Request**

```json
{
  "risk_request_id": "uuid (optional, 멱등키)",
  "order_intent": {
    "order_intent_id": "uuid", "trade_case_id": "uuid", "fund_id": "uuid",
    "book_id": "uuid", "strategy_id": "uuid", "instrument_id": "uuid",
    "side": "BUY|SELL", "order_type": "...", "quantity": "100",
    "limit_price": "70000", "time_in_force": "DAY", "valid_until": "2026-07-31T06:00:00Z",
    "snapshot": { "...": "MarketSnapshot, contracts.py 그대로" }
  },
  "context": {
    "mandate": {"fund_id": "uuid", "allowed_instrument_ids": null, "min_order_notional": "0", "max_order_notional": "..."},
    "limits": {"soft_single_issuer_pct": "...", "hard_single_issuer_pct": "...", "max_daily_turnover_notional": "...", "max_daily_order_count": 0, "max_daily_loss": "...", "max_drawdown_pct": "..."},
    "restricted_items": [{"instrument_id": "uuid", "restriction_type": "...", "effective_from": "...", "effective_to": null}],
    "portfolio": {"fund_id": "uuid", "cash": "...", "buying_power": "...", "gross_exposure": "...", "positions": {}, "issuer_of": {}, "issuer_exposure": {}, "realized_pnl_today": "0", "unrealized_pnl_today": "0", "peak_equity": "...", "equity": "...", "orders_today": 0, "notional_traded_today": "0"},
    "market_status": {"tradable": true, "reason": ""},
    "counterparty": {"broker_adapter": "...", "health": "..."},
    "trading_state": "ENABLED|REDUCE_ONLY|ENTRY_BLOCKED|HALTED",
    "as_of": "2026-07-31T06:00:00Z"
  }
}
```

`context`는 지금 `RiskContext`가 스텁 값으로 채워지는 부분과 그대로 대응한다 — market-api/portfolio-api가
실연동되기 전까지는 호출자(현재는 자체 테스트, 나중엔 Trading 부서)가 이 Snapshot을 만들어서 보낸다.

**Response** — `RiskAssessment`를 그대로 직렬화.

```json
{
  "risk_request_id": "uuid",
  "decision": {"order_intent_id": "uuid", "verdict": "APPROVE|RESIZE|REJECT", "approved_quantity": "100", "...": "RiskDecision 필드"},
  "check_results": [{"check_name": "mandate_allowed_instrument", "passed": true, "detail": ""}],
  "reason_codes": ["..."],
  "calculation_version": "risk-p0-v1",
  "input_hash": "sha256...",
  "trading_state": "ENABLED",
  "approved_legs": [],
  "aggregate_exposure": {}
}
```

### 2.2 부서 단독 자원 — Trading State (Kill Switch)

| Method/Path | 감싸는 함수 | 권한 |
|---|---|---|
| `GET /risk/v1/trading-state/{scope}` | `get_state_fail_closed(scope)` | 아무 서비스나 조회 가능 (읽기 전용) |
| `GET /risk/v1/trading-state/{scope}/record` | `get_record(scope)` | 감사용 전체 기록(reason/set_by/timestamp 포함) |
| `PUT /risk/v1/trading-state/{scope}` | `set_state(scope, state, reason, set_by)` | **제한된 Command** — 아래 참고 |
| `DELETE /risk/v1/trading-state/{scope}` | `clear_state(scope)` | 위와 동일 |

`PUT`/`DELETE`는 CLAUDE.md "위험한 기능은 실패 시 거래 확대가 아니라 Entry 차단 방향" 원칙과 지난 세션에
정리한 kanban 통제 패턴을 그대로 적용한다 — `set_by`가 Authorized Operator Identity가 아니면 거절하고,
`AI_OFFICE_FRONTEND_PLAN.md` §6 Command 봉투(`reason` 필수, `expected_version`, Audit Event 기록)를 그대로 쓴다.
LLM이 이 엔드포인트를 직접 호출하지 않는다 — Kill Switch는 결정론적 Service만 실행한다는 원칙 그대로.

### 2.3 Compliance (Agentic RAG)

`skills/agentic-rag/src/graph.py`의 `run_compliance_check()`는 이미 구현·테스트 완료된 그래프다
(retrieve → grade → generate → hallucination_check → retry, 최대 3회). API 레이어는 이 함수를 얇게
감싸기만 하면 된다 — 새 판정 로직을 만드는 게 아니다.

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /risk/v1/compliance/check` | `run_compliance_check(query, as_of, corpus_dir, persona="compliance-policy-agent")` |

**Request**

```json
{ "query": "Can we open a new long position in SYMBOL_A today?", "as_of": "2026-07-31" }
```

**Response** — `run_compliance_check()`의 반환값 그대로.

```json
{
  "answer": {
    "verdict": "no_breach|breach|ambiguous",
    "cited_documents": ["policy-restricted-list-001"],
    "rationale": "...", "confidence": 0.0, "escalate": false
  },
  "grounded": true,
  "attempts": 1,
  "relevant_documents": [{"document_id": "...", "title": "...", "version": "...", "score": 0.0}]
}
```

`grounded: false`가 재시도 3회 후에도 남으면 API는 `escalate: true`인 채로 그대로 반환한다 — 호출자가
임의로 통과 처리하지 않는다(SKILL.md에 이미 있는 원칙).

### 2.4 Risk 부서 내 통신 (intra-department)

| 호출자 (Hermes Specialist) | 호출 대상 |
|---|---|
| `pre-trade-risk-analyst` | `POST /investment-cases/{case_id}/risk-check` |
| `market-liquidity-risk-agent` | `GET /risk/v1/trading-state/{scope}` (읽기), Position/Exposure는 아직 portfolio-api 스텁 |
| `compliance-policy-agent` | `POST /risk/v1/compliance/check` |
| `operational-counterparty-risk-agent` | `GET /risk/v1/trading-state/{scope}/record` (Broker 상태 이력 확인) |
| `risk-supervisor` | 위 결과들을 종합해 `investment_case.risk_*` Event를 해석·서술만 함 — 자체 API 호출 없음(팀 가이드: Limit 계산·판정 변경 안 함) |

## 3. QA Domain API

### 3.1 [제안] Case에 종속된 판정

> **여기부터 §3.1은 제안이다.** `MINIMUM_SERVICE_UNIT_SPEC.md` §8의 Command/Event 목록에 QA 게이트가
> 아직 없다 — Research→Trading→Risk 다음에 QA가 오는 CLAUDE.md `workflow` 순서와 어긋난다. 아래를
> 그 문서 §8/§11에 추가하는 걸 제안한다. 팀 승인 전엔 상위 문서를 고치지 않는다.

| Method/Path | 감싸는 함수 | 제안 Command | 제안 Event |
|---|---|---|---|
| `POST /investment-cases/{case_id}/qa-check` | `EvidenceQaEngine.check_artifact(artifact, ctx, qa_decision_id)` | `EvaluateEvidence` | `investment_case.qa_passed` / `investment_case.qa_warned` / `investment_case.qa_blocked` |

**Request**

```json
{
  "qa_decision_id": "uuid (optional, 멱등키)",
  "artifact": {
    "artifact_version_id": "uuid", "artifact_type": "research_packet|order_intent|...",
    "producer": "research-department", "fund_id": "uuid", "trace_id": "uuid",
    "claims": [{"claim_index": 0, "text": "...", "kind": "fact|inference|forecast|recommendation",
                "subject": "...", "numeric_value": "...", "unit": "...", "evidence_ids": ["uuid"],
                "acknowledges_uncertainty": false, "tool_source": null}],
    "tool_results": []
  },
  "context": {"decision_time": "2026-07-31T06:00:00Z"}
}
```

`context.evidence_store`는 API 요청에 안 넣는다 — QA 서비스가 자기 Evidence Store(추후 pgvector/Fact Store 연동)를
직접 조회한다. 지금은 서비스 내부에 미리 채워둔 스텁을 쓴다.

**Response** — `QaAssessment`를 그대로 직렬화 (`decision`, `claim_checks`, `findings`, `calculation_version`, `input_hash` 등).

### 3.2 부서 단독 자원 — Ops Health

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /qa/v1/ops/evaluate` | `OpsHealthMonitor.evaluate(metrics, thresholds, trace_id)` — Request: `AgentHealthMetrics`+`OpsThresholds`, Response: `OpsAssessment`(status/breaches/incident 초안) |

### 3.3 부서 단독 자원 — Agent/Tool Trace

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /qa/v1/runs` | `start_run(trace_id, agent_id, profile_version_id, input_hash, case_id?, fund_id?, model_id?)` |
| `POST /qa/v1/runs/{agent_run_id}/complete` | `complete_run(...)` |
| `POST /qa/v1/runs/{agent_run_id}/fail` | `fail_run(agent_run_id, error_code)` |
| `POST /qa/v1/runs/{agent_run_id}/timeout` | `timeout_run(agent_run_id)` |
| `POST /qa/v1/runs/{agent_run_id}/cancel` | `cancel_run(agent_run_id)` |
| `POST /qa/v1/runs/{agent_run_id}/tool-calls` | `record_tool_call(agent_run_id, tool_name, scope, input_hash, policy_version?)` |
| `POST /qa/v1/tool-calls/{tool_call_id}/allow` | `allow_tool_call(tool_call_id)` |
| `POST /qa/v1/tool-calls/{tool_call_id}/deny` | `deny_tool_call(tool_call_id, reason)` |
| `POST /qa/v1/tool-calls/{tool_call_id}/complete` | `complete_tool_call(...)` |
| `POST /qa/v1/tool-calls/{tool_call_id}/fail` | `fail_tool_call(...)` |

`POST /qa/v1/runs`는 각 부서 Hermes Supervisor가 자기 Specialist를 실행할 때마다 호출해야 하는
**부서간 공통 진입점**이다 — 6개 본부 전체가 QA API의 이 엔드포인트를 호출해야 Trace가 남는다(§4 참고).

### 3.4 부서 단독 자원 — Tool Permission

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /qa/v1/tool-permission/check` | `check_tool_permission(policy, tool_name)` — 순수 판정, 부수효과 없음 |
| `POST /qa/v1/runs/{agent_run_id}/tool-calls:checked` | `record_and_check_tool_call(...)` — Trace 기록과 판정을 한 번에 |
| `GET /qa/v1/tool-calls/unauthorized-count` | `count_unauthorized_calls(tool_calls)` |

### 3.5 부서 단독 자원 — Incident/Corrective Action

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /qa/v1/incidents/{incident_id}/events` | `add_event(incident_id, source, entry_type, summary, occurred_at, recorded_by, evidence?)` |
| `GET /qa/v1/incidents/{incident_id}/timeline` | `timeline_for(incident_id)` |
| `POST /qa/v1/corrective-actions` | `open_corrective_action(owner, action_plan, due_at, incident_id?, finding_id?)` |
| `POST /qa/v1/corrective-actions/{id}/start` | `start_action(...)` |
| `POST /qa/v1/corrective-actions/{id}/submit-for-verification` | `submit_for_verification(...)` |
| `POST /qa/v1/corrective-actions/{id}/verify-and-close` | `verify_and_close(id, verifier, verification)` — **API 레이어에서도 `verifier == owner`를 인증 토큰 기준으로 한 번 더 막는다** — 앱 로직만 믿지 않는다(같은 사람이 토큰만 바꿔 재요청하는 걸 막기 위함) |
| `POST /qa/v1/corrective-actions/{id}/cancel` | `cancel_action(...)` |

### 3.6 Evidence QA (Agentic RAG baseline)

Risk §2.3과 같은 그래프(`run_compliance_check`)를 `persona="evidence-qa-agent"`로 호출한다 — 이미 구현·
테스트 완료(SUPPORTED/PARTIAL/UNSUPPORTED/CONTRADICTED 4개 시나리오 실제 OpenAI 호출로 검증됨).

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /qa/v1/evidence/check` | `run_compliance_check(query, as_of, corpus_dir, persona="evidence-qa-agent")` |

**Request/Response** 형태는 §2.3과 동일하되 `verdict` 어휘만 `SUPPORTED\|PARTIAL\|UNSUPPORTED\|CONTRADICTED`다
(`evidence_qa_engine.py`의 `ClaimCheckResult`와 이름을 맞췄다). 이 엔드포인트는 근거 인용 보조 도구일 뿐,
Case의 최종 PASS/WARN/FAIL은 여전히 §3.1 `qa-check`(`EvidenceQaEngine.check_artifact`)가 결정한다.

### 3.7 [제안] PIKE-RAG L2 확장 — Evidence Store 규모 확대 대응

> **틀만 잡아둔다 — corpus/evidence/가 SAMPLE_PLACEHOLDER 3건인 지금은 미구현.** `rag_technique_assignment`의
> `upgrade_path`(config.yaml)에 이미 걸어둔 트리거 조건(종목당 여러 문서 × 다수 종목으로 corpus가 실제로
> 커질 때)을 충족하면 아래 계약대로 구현한다. §3.6의 단일 검색으로 recall이 부족해지는 지점부터 착수.

| Method/Path | 하는 일 |
|---|---|
| `POST /qa/v1/evidence/atomize` | (색인 단계) corpus 청크마다 "이 청크로 답할 수 있는 질문"을 LLM으로 미리 생성해 원자 질의 인덱스를 만든다(Knowledge Atomizing). `retriever.py`의 `LocalVectorIndex`와 별개의 2차 인덱스로 둔다. |
| `POST /qa/v1/evidence/check:decomposed` | 복잡한 다중 근거 질의를 원자 질의로 반복 분해(Knowledge-Aware Task Decomposition, 최대 `max_iterations`회)하며 검색한다. |

**`atomize` Request/Response**

```json
{ "corpus_dir": "corpus/evidence" }
```
```json
{ "corpus_version": "sha256...", "chunk_count": 3, "atomic_question_count": 11 }
```

**`check:decomposed` Request**

```json
{ "query": "...", "as_of": "2026-07-31", "max_iterations": 5 }
```

**`check:decomposed` Response** — §3.6과 같은 `answer`/`grounded` 구조에 분해 과정을 덧붙인다.

```json
{
  "answer": { "verdict": "SUPPORTED", "cited_documents": ["..."], "rationale": "...", "confidence": 0.0, "escalate": false },
  "grounded": true,
  "decomposition_trace": [
    {"iteration": 1, "atomic_queries": ["...", "..."], "selected_chunk_ids": ["..."]},
    {"iteration": 2, "atomic_queries": ["..."], "selected_chunk_ids": ["..."]}
  ]
}
```

`decomposition_trace`는 §8 Frontend 연결에서 진행률(iteration/max_iterations) 표시에 그대로 쓴다.

### 3.8 [제안] Hyper-Extraction — Incident 다중 원인 관계 추출

> **틀만 잡아둔다 — 실제 Incident가 몇 건뿐인 지금은 미구현.** `rag_technique_assignment`에 걸어둔 트리거
> 조건(다년치 Incident 축적 + 2개 이상 요인이 동시에 겹치는 패턴 반복)을 충족하면 착수. `incident_timeline.py`의
> `IncidentEventRecord`(FACT/INFERENCE 분리 기록)를 입력으로 삼는다 — 새 데이터 소스가 아니라 이미 기록된
> Timeline을 재료로 관계를 추출하는 것이다.

| Method/Path | 하는 일 |
|---|---|
| `POST /qa/v1/incidents/{incident_id}/extract-hypergraph` | Timeline의 FACT/INFERENCE 이벤트에서 N-ary(2개 이상 엔티티가 동시에 얽히는) 인과 관계를 LLM으로 추출한다. |
| `GET /qa/v1/incidents/{incident_id}/hypergraph` | 저장된 추출 결과 조회. |

**Response**

```json
{
  "incident_id": "uuid",
  "hyperedges": [
    {
      "hyperedge_id": "uuid",
      "relation": "joint_root_cause_of",
      "entity_refs": [
        {"type": "incident_event", "id": "uuid", "summary": "Feed 지연 감지"},
        {"type": "incident_event", "id": "uuid", "summary": "Threshold 오설정"},
        {"type": "incident_event", "id": "uuid", "summary": "승인 지연"}
      ],
      "target": {"type": "incident", "id": "uuid"},
      "confidence": 0.0,
      "rationale": "..."
    }
  ],
  "calculation_version": "hyper-extract-v1"
}
```

추출 결과는 그 자체로 확정된 Root Cause가 아니라 **INFERENCE다** — `incident_timeline.py`의 Fact/Inference
분리 원칙을 그대로 이어받는다. 이 결과는 자동으로 Incident를 종결하지 않고, `POST /qa/v1/corrective-actions`의
`action_plan` 초안으로만 들어가며 여전히 사람(incident-postmortem-agent → QA 검증)이 확인해야 한다.

### 3.9 QA 부서 내 통신 (intra-department)

| 호출자 (Hermes Specialist) | 호출 대상 |
|---|---|
| `evidence-qa-agent` | `POST /investment-cases/{case_id}/qa-check`, `POST /qa/v1/evidence/check`, (규모 확대 후) `.../check:decomposed` |
| `hallucination-critic` | `POST /investment-cases/{case_id}/qa-check`의 결과(UNSUPPORTED/CONTRADICTED)를 재사용 — 자체 호출 없음 |
| `agent-ops-monitor` | `POST /qa/v1/ops/evaluate` |
| `tool-permission-security-reviewer` | `POST /qa/v1/tool-permission/check`, `GET /qa/v1/tool-calls/unauthorized-count` |
| `incident-postmortem-agent` | `POST/GET /qa/v1/incidents/*`, `POST .../extract-hypergraph`(§3.8, 제안), `POST /qa/v1/corrective-actions/*` |
| `model-risk-agent` | 미구현 (Eval Harness 없음) — 이번 문서 범위 밖 |
| `internal-audit-agent` | 미구현 — 이번 문서 범위 밖 |
| `qa-audit-supervisor` | 위 결과들을 종합해 Finding Severity·Owner·Due Date를 정함 — 판정 자체는 안 바꿈 |

## 4. 부서 간 통신 (inter-department)

### 4.1 동기 호출 — Hot Path

Trading이 주문을 내기 전에는 **비동기 Event를 기다리지 않고** Risk를 동기 호출해야 한다(팀 가이드 2절
"Pre-trade Hot Path는 LLM 호출 없이 끝난다"와 같은 이유 — 응답을 못 받으면 주문을 못 낸다).

```
trading-department → POST /investment-cases/{case_id}/risk-check → risk-management
```

### 4.2 Domain Event — Case Stream

`MINIMUM_SERVICE_UNIT_SPEC.md` §8 Envelope을 그대로 쓴다. Risk가 이미 정의된 3개를 낸다.

```
investment_case.risk_approved
investment_case.risk_resized
investment_case.risk_rejected
```

QA는 §3.1에서 제안한 3개를 `qa-check` 완료 시 낸다(승인 전까지는 초안).

```
investment_case.qa_passed      # QaDecisionValue.PASS
investment_case.qa_warned      # QaDecisionValue.WARN
investment_case.qa_blocked     # QaDecisionValue.FAIL, decision.blocked == true
```

**소비자**

| Event | 소비 부서 | 용도 |
|---|---|---|
| `investment_case.risk_approved`/`_resized` | Trading(OMS 제출), Accounting(원장 반영 대기), QA(Trace 감사 대상으로 등록) | |
| `investment_case.risk_rejected` | Trading(주문 중단), CEO Office(집계·보고) | |
| `investment_case.qa_blocked` | 원 작성 본부(Finding 처리 의무 — 본인이 Finding을 닫을 순 없음), CEO Office | |

### 4.3 Domain Event — QA/Audit Stream (Case에 안 묶임)

QA는 6개 본부 전체를 상시 감사하는 Shared Service라서, 모든 Event가 하나의 `case_id`에 묶이지 않는다.
별도 Stream으로 둔다.

```
qa.finding.opened / qa.finding.escalated
qa.incident.opened / qa.incident.event_added
qa.corrective_action.opened / qa.corrective_action.verified
qa.ops.incident_drafted   # OpsAssessment에서 SEV1~SEV4 발생 시
```

이 Stream은 §3.3~3.5의 부서 단독 API가 호출될 때마다 QA 서비스가 직접 낸다 — 다른 부서가 명시적으로
발행할 일은 없다(QA가 관찰자이지, 다른 부서가 QA에게 통지하는 구조가 아니다).

### 4.4 비동기 Handoff — Case Stream/Hot Path에 안 맞는 일

정기 감사, 다부서 협업 Task처럼 즉시 응답이 필요 없는 일은 [ADR-0001](adr/0001-hermes-kanban-agent-status-bridge.md)에서
정리한 Hermes kanban Task로 나른다. Task body에 아래 최소 계약(§19.19 Department Handoff)을 JSON으로 넣는다.

```json
{
  "case_id": "IC-20260731-0001",
  "from_dept": "risk-management",
  "to_dept": "qa-department",
  "purpose": "Risk 승인 판정 근거를 QA가 독립 검증",
  "input_artifact_id": "risk-decision:R-4501",
  "required_output_schema": "QaAssessment",
  "due_time": "2026-07-31T09:00:00Z",
  "priority": "P1",
  "escalation": "due_time 초과 시 qa-audit-supervisor -> CEO"
}
```

이 Task의 `--parent`/`blocked`/`--verifier` 사용 규칙은 ADR-0001 §5를 그대로 따른다.

## 5. Supabase 연동 (지금은 미착수)

이 API들의 응답은 지금 순수 인메모리다. `risk.risk_decisions`/`audit.qa_decisions`/`audit.agent_runs`/
`audit.tool_calls`/`audit.incident_events`/`audit.corrective_actions`에 실제 INSERT하는 건 각 config.yaml의
`not_started` 목록에 이미 있는 항목이고(accounting.funds 의존 등으로 막힘), 이 API 설계서와는 별개 작업이다.
API가 먼저 서면 그 안에서 Supabase Write를 붙이는 순서를 권장한다 — API Contract가 먼저 고정돼야 어느
필드가 실제로 필요한지 확정되기 때문이다.

## 6. 인증·Rate Limit·Observability (자리만 잡아둠)

- 인증: Service Token 발급 주체 미정 (Supabase Auth 서비스 계정? 별도 발급기?) — 다음 결정 필요.
- Rate Limit: Pre-trade Hot Path(`risk-check`)는 P99 Latency가 KPI라 Rate Limit을 걸면 안 됨. `qa-check`는 비동기라 걸어도 됨.
- Observability: `TECH_STACK_DECISIONS.md` §7의 `structlog`/`opentelemetry-api`를 그대로 쓴다. 모든 응답에 `trace_id`를 실어서 `audit.agent_runs`/`audit.tool_calls`와 조인 가능하게 한다.

## 7. ai-office Frontend 연결

[ADR-0001](adr/0001-hermes-kanban-agent-status-bridge.md)이 이미 kanban Task 상태 → `AI_OFFICE_FRONTEND_PLAN.md`
§5.4 Agent 상태(`OFFLINE\|IDLE\|QUEUED\|RUNNING\|WAITING_APPROVAL\|BLOCKED\|DEGRADED\|ERROR`) 매핑과
`agent.status.v1` Event 발행 구조를 정의해뒀다. 여기서는 §2/§3에서 설계한 RAG·병렬 처리 파이프라인이
그 Event의 `payload` 안에서 구체적으로 어떻게 표현되는지, 그리고 ai-office가 이미 갖고 있는 화면 요소를
어떻게 재사용하는지를 정의한다. **DEMO 모드가 아니라 Phase UI-1 이후(PAPER) 설계다 — 지금 Scripted
Simulation에 바로 연결하는 게 아니다.**

### 7.1 부서원(Specialist) 이동 — RAG 단계별 위치

`agent.status.v1`의 `payload`에 `stage` 필드를 추가한다. Agent 상태 자체(§5.4의 8종)는 안 늘리고,
같은 `RUNNING` 안에서 `stage`로 세분화한다 — Frontend 계약을 어기지 않는 최소 확장이다.

| RAG 단계 | `payload.stage` | 화면 |
|---|---|---|
| §2.3/§3.6 baseline: retrieve/grade/generate | `"retrieve"` \| `"grade"` \| `"generate"` | 자기 데스크에서 `RUNNING` |
| hallucination_check 실패 → 재시도 | `"retry"`, `payload.attempt: N` | 데스크에 남아 `RUNNING` 유지, 말풍선에 "N/3회 재검토 중" |
| §3.7 PIKE-RAG 분해 루프 | `"decompose"`, `payload.iteration: N`, `payload.max_iterations: 5` | 데스크 ↔ "Evidence Archive" 지점 왕복 애니메이션 — 반복마다 한 번씩 |
| §3.8 Hyper-Extraction 추출 | `"extract"`, `payload.entities_visited: [...]` | Incident와 관련된 다른 부서원 데스크를 순서대로 순회 |

### 7.2 부서장(Supervisor) 이동과 "회의" — kanban swarm 시각화

지난번 kanban 통제 설계에서 `swarm`(병렬 Worker → Verifier → Synthesizer)을 쓰기로 한 부분이 ai-office
화면에서 "회의"로 보이는 지점이다. `world.ts`에 이미 있는 `MEETING_SEATS`/`CEO_REPORT_SPOT`을 그대로
재사용한다 — 새 방을 만들 필요가 없다(이 두 심볼은 지금 Demo의 회의 연출에도 이미 쓰이고 있다).

| kanban 상태 | Agent 상태 | 위치 |
|---|---|---|
| Worker Task들 `running` | 각자 `RUNNING` | 각자 데스크 |
| 모든 Worker `done`, Verifier Task `running` 전환 | Verifier `RUNNING`, 방금 끝난 Worker들 `IDLE` | 관련 인원 전원 `MEETING_SEATS`로 이동 — Verifier가 결과를 설명받는 모습 |
| Verifier `done`, Synthesizer Task `running` | Synthesizer `RUNNING` | `CEO_REPORT_SPOT`과 같은 유형의 "결과 종합" 지점 — QA/Risk 전용 지점은 아직 없음, §7.3 참고 |
| Case 단위 QA 게이트 `blocked(needs_input)` | `WAITING_APPROVAL` | §5.4에 이미 정의된 "회의실 또는 승인 표식" 그대로 |

이 표의 판단 근거(누가 Worker/Verifier가 될 수 있는지, 자기 산출물 자기 검증 금지)는 새로 정하지 않고
ADR-0001 §5를 그대로 따른다 — Frontend는 그 판단을 보여줄 뿐 판단하지 않는다.

### 7.3 건드려야 할 것 vs 건드리면 안 되는 것

- `agent.status.v1` payload 확장(§7.1)과 kanban→상태 매핑 확장(§7.2)은 **BFF/이벤트 스키마 쪽 작업**이라
  `ai-office/app/game/*`(엔진 보호 파일)를 안 건드린다.
- QA/Risk 전용 "결과 종합 지점"이 필요하면 `world.ts`에 좌표 하나 추가하는 정도의 변경인데, 이 파일은
  `ai-office/CLAUDE.md`가 명시적으로 보호한 엔진 파일이다 — 이건 이 문서 혼자 결정하지 않고, §11 소유권표의
  "Live Office" Business Owner(영주님)와 "공통 Frontend Platform" 담당(미지정)이 정할 일이다.
- 지금 할 수 있는 건 여기까지: Event Payload 계약을 문서로 고정해두는 것. 실제 좌표·애니메이션 구현은
  ADR-0001의 "공통 Frontend Platform" 담당자가 정해진 뒤 진행한다.

## 8. 확정 vs 제안 — 요약

| 항목 | 상태 | 근거 |
|---|---|---|
| `POST /investment-cases/{case_id}/risk-check` | 확정 (구체화만 함) | MINIMUM_SERVICE_UNIT_SPEC.md §11에 이미 이름 있음 |
| Risk 부서 단독 API(§2.2, §2.3) | 확정 (Risk 내부 소유) | 다른 본부·Case 계약과 안 겹침 |
| `POST /investment-cases/{case_id}/qa-check` + `EvaluateEvidence` Command + `qa_passed`/`qa_warned`/`qa_blocked` Event | **제안 — 팀 승인 필요** | MINIMUM_SERVICE_UNIT_SPEC.md §8 목록에 없음, workflow 순서(QA가 Risk 다음)와는 이미 일치 |
| QA 부서 단독 API(§3.2~3.6) | 확정 (QA 내부 소유) | 위와 동일 |
| PIKE-RAG L2(§3.7), Hyper-Extraction(§3.8) | **틀만 설계 — 미구현, 트리거 조건 대기** | `rag_technique_assignment.upgrade_path`(config.yaml)에 이미 걸어둔 조건 충족 시 착수 |
| QA/Audit Stream(§4.3) | 확정 (QA 내부 소유) | Case Stream과 분리된 QA 고유 채널 |
| kanban Handoff 스키마(§4.4) | 제안 (ADR-0001에 이미 상정) | ADR-0001 참고 |
| ai-office Frontend 연결(§7) | 설계만 함, 구현 담당 미지정 | AI_OFFICE_FRONTEND_PLAN.md §11 "공통 Frontend Platform" 미지정과 동일 사유 |

**다음 작업 제안 순서**: (1) `qa-check`를 §3.1대로 승인받아 MINIMUM_SERVICE_UNIT_SPEC.md에 반영 → (2) Risk/QA
각각 `risk_engine.py`/`evidence_qa_engine.py`를 감싸는 최소 FastAPI 서비스 하나씩 실제 코드로 작성(자체
점검용 `__main__` self-check를 pytest로 옮기는 김에 같이) → (3) `tool_allowlist`/`forbidden_tools`를
config.yaml에 채워 이 API들에 대한 Profile별 호출 권한을 명시 → (4) §2.3/§3.6 Agentic RAG 래퍼(이미 구현된
그래프를 감싸기만 하면 됨, 가장 저비용) → (5) Supabase 연동 → (6) PIKE-RAG/Hyper-Extraction은 트리거 조건
충족 여부를 분기마다 재확인 → (7) Frontend 연결은 담당자 배정 후.
