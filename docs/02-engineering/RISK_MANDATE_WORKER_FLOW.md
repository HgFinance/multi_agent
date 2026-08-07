# Risk Mandate Worker Flow

이 문서는 사용자 Mandate를 Risk 부서장(Hermes Agent + LLM)이 두 직원에게
top-down으로 전달하고, 결과를 `risk-assessment.v1`로 종합하는 실행 계약이다.

```text
사용자 화면
  -> CEO Router / Governance
  -> Risk Head (Hermes Agent + LLM)
  -> risk-runner
  -> compliance-policy-worker
  -> Risk Head fan-in
  -> 사용자 설명 / 승인 대기 / 보류 / 거절
```

## 1. 직원 경계

Risk 직원은 총 2명이며 Hermes Head는 직원 수에 포함하지 않는다.

### risk-runner

- LLM이 아닌 결정론적 Risk Gate다.
- Mandate, 주문, 포트폴리오 snapshot, VaR, 시장 상태를 검사한다.
- 단일 종목 비중, 총 익스포저, VaR, Drawdown, 금지 자산,
  주문 후 예상 포지션을 계산한다.
- `APPROVE`, `RESIZE`, `REJECT`, `HOLD`, `HALTED` 중 하나를 반환한다.
- 수치 판단의 authoritative source지만 주문을 직접 제출하지 않는다.

### compliance-policy-worker

- LLM 기반 정책·법령 근거 Analyst다.
- Pinecone의 `risk-compliance-policy` namespace만 읽는다.
- 실제 법령, 내부 정책, 사용자 Mandate, 시장 계산값을 구분한다.
- 근거와 metadata를 반환하지만 주문의 안전성을 최종 결정하지 않는다.
- 직접적인 법령 근거가 없으면 법률 위반으로 단정하지 않는다.

`risk-runner`의 수치 결과만 `authoritative: true`다. 직원과 Head는 주문을 직접
집행하지 않으므로 실행 권한은 `binding: false`다.

## 2. 사용자 입력과 Risk 입력

화면의 Mandate는 현재 위험 상태가 아니라 사용자가 정한 앞으로의 투자 규칙이다.

```json
{
  "mandate_id": "MND-001",
  "investor_profile": {
    "investment_goal": "장기적인 자산 가치 보존",
    "risk_tolerance": "CONSERVATIVE",
    "financial_experience_years": 0,
    "perceived_risk_awareness": true
  },
  "portfolio_constraints": {
    "base_capital": "100000000",
    "max_single_stock_weight": "0.30",
    "max_total_exposure": "2.00",
    "max_drawdown_limit": "-0.15"
  },
  "asset_policy": {
    "single_stocks": "ALLOWED",
    "etf": "ALLOWED",
    "leverage": "ALLOWED",
    "futures": "PROHIBITED",
    "options": "PROHIBITED",
    "crypto": "PROHIBITED"
  },
  "order_mode": "MANUAL_APPROVAL"
}
```

Risk Head는 여기에 `order`, `portfolio_snapshot`, `risk_snapshot`,
`market_snapshot`, `restricted_list`, `as_of`, `event_id`, `trace_id`를 추가한다.
Mandate만 있고 동적 상태가 없으면 VaR·익스포저·Drawdown을 추정하지 않고
`HOLD` 또는 `NEED_MORE_DATA`로 끝낸다.

## 3. Risk Head State

Head는 `risk-head-state.v1`를 관리한다.

```json
{
  "schema_version": "risk-head-state.v1",
  "run_id": "RUN-001",
  "trace_id": "TRACE-001",
  "request": {
    "intent": "PRE_TRADE",
    "user_question": "이 주문은 안전하고 법적으로 문제없나요?"
  },
  "mandate": {},
  "context": {
    "order": {},
    "portfolio_snapshot": {},
    "risk_snapshot": {},
    "market_snapshot": {},
    "as_of": "2026-08-07T10:00:00Z"
  },
  "routing": {},
  "worker_tasks": {},
  "worker_results": {},
  "decision": {},
  "escalation": {},
  "audit": {}
}
```

Head는 직원 결과를 다시 계산하지 않는다. `worker_results`에는 원본을 보존하고
`decision`에는 권한 우선순위에 따른 종합 결과만 기록한다.

## 4. 라우팅

| 사용자 의도 | risk-runner | compliance-policy-worker |
|---|---:|---:|
| 이 주문을 해도 되는가 | 필수 | 선택 |
| 법적으로 문제가 있는가 | 선택 | 필수 |
| Mandate 한도를 넘었는가 | 필수 | 선택 |
| 왜 주문이 막혔는가 | 필수 | 필요 시 |
| 법적으로 문제없고 안전한가 | 필수 | 필수 |
| 보수적 투자자의 레버리지 사용 | 필수 | 필수 |
| 실제 법령 조항 확인 | 불필요 | 필수 |
| Mandate 설정 저장 | 필수 필드·자료형·수치 범위 검증 | 자연어 의미의 모호성·설명 부족 자문 |

구조화된 입력이 자연어 라우팅보다 우선한다.

```text
order != null
  -> risk-runner REQUIRED

법령·규정·내부정책·법적 문제를 질문함
  -> compliance-policy-worker REQUIRED

두 조건 모두 충족
  -> 두 직원을 동일 snapshot으로 독립 fan-out
```

한 직원의 결과를 다른 직원의 입력으로 사용하지 않는다. 두 직원은 동일한
`input_hash`와 immutable context snapshot을 받는다.

## 5. 직원 Task와 질문

`risk-runner.task.v1`에는 다음을 넣는다.

```json
{
  "schema_version": "risk-runner.task.v1",
  "task_id": "TASK-RISK-001",
  "run_id": "RUN-001",
  "trace_id": "TRACE-001",
  "mandate_id": "MND-001",
  "as_of": "2026-08-07T10:00:00Z",
  "mandate": {},
  "order": {},
  "portfolio_snapshot": {},
  "risk_snapshot": {},
  "market_snapshot": {},
  "restricted_list": [],
  "input_hash": "sha256:..."
}
```

직원 질문은 다음과 같다.

```text
현재 snapshot과 Mandate를 기준으로 주문의 수치적 허용 여부를 계산하라.
법률 해석과 자연어 추론은 하지 말고 결정론적 결과와 검사값만 반환하라.
```

`compliance-policy.task.v1`에는 `question`, `order`, `mandate`, `as_of`,
`required_sources`, `input_hash`를 넣는다.

```text
이 주문에 직접 적용되는 법령·내부정책·사용자 Mandate 근거를 찾아라.
근거 유형을 구분하고, 직접적인 법령 근거가 없으면 법률 위반으로 단정하지 말라.
document_id, version, clause_id, effective_from, effective_to를 반환하라.
```

## 6. 직원 Output

`risk-runner.result.v1` 예시:

```json
{
  "worker_id": "risk-runner",
  "trace_id": "TRACE-001",
  "authoritative": true,
  "verdict": "REJECT",
  "reason_codes": [
    "SINGLE_STOCK_LIMIT_BREACH",
    "VAR_LIMIT_BREACH"
  ],
  "checks": [],
  "missing_observations": [],
  "order_submission_allowed": false,
  "tool_calls": ["evaluate_order_compliance"],
  "binding": false
}
```

`compliance-policy.result.v1` 예시:

```json
{
  "worker_id": "compliance-policy-worker",
  "trace_id": "TRACE-001",
  "authoritative": false,
  "status": "COMPLETED",
  "legal_status": "NO_DIRECT_LEGAL_BASIS",
 "policy_status": "NO_MATCH",
  "explanation": "단일 종목 제한은 사용자가 설정한 Mandate입니다.",
 "evidence": [],
 "mandate_observations": [],
 "clarification_questions": [],
  "tool_calls": ["query_pinecone_risk_policy"],
  "escalate": false,
  "binding": false
}
```

근거 유형은 반드시 다음처럼 구분한다.

```text
LEGAL_REQUIREMENT   실제 법령 조항
INTERNAL_POLICY     회사·펀드 내부 규정
USER_MANDATE        사용자가 설정한 규칙
MARKET_DATA         가격·보유비중·VaR 등 계산 입력
INFERENCE           모델의 해석
```

`USER_MANDATE_BREACH`를 `LEGAL_BREACH`로 변환하지 않는다.

## 7. Fan-in과 최종 Decision

Head의 권한 우선순위는 다음과 같다.

```text
Risk REJECT/HALTED
  -> execution_gate = REJECT/HALTED

Risk RESIZE
  -> execution_gate = RESIZE 또는 HOLD

Risk APPROVE + 사용자 Mandate 위반
  -> execution_gate = HOLD 또는 REJECT

Risk APPROVE + 법령 위반 근거 확인
  -> execution_gate = ESCALATE 또는 REJECT

Risk APPROVE + 직접적인 법령 근거 없음
  -> Risk 결과는 유지하되 법적 보증 표현 금지

필수 데이터 또는 worker 결과 없음
  -> APPROVE 금지, NEED_MORE_DATA/HOLD/ESCALATE
```

최종 내부 Decision 예시:

```json
{
  "risk_verdict": "REJECT",
  "compliance_verdict": "NO_DIRECT_LEGAL_BASIS",
  "mandate_verdict": "BREACH",
  "execution_gate": "REJECT",
  "order_submission_allowed": false,
  "approval_mode": "MANUAL_APPROVAL",
  "approval_status": "NOT_ELIGIBLE",
  "authoritative_source": "risk-runner",
  "decision_basis": [
    "SINGLE_STOCK_LIMIT_BREACH",
    "VAR_LIMIT_BREACH",
    "USER_MANDATE_BREACH"
  ],
  "binding": false
}
```

## 8. 표준 API와 사용자 응답

Risk Domain API는 `/risk/v1/mandates/{mandate_id}/assess`에서 다음 envelope을
반환한다.

```json
{
  "schema_version": "risk-assessment.v1",
  "run_id": "RUN-001",
  "trace_id": "TRACE-001",
  "mandate_id": "MND-001",
  "status": "COMPLETED",
  "decision": "REJECT",
  "authoritative_source": "risk-runner",
  "dispatch": {},
  "employees": {
    "risk-runner": {},
    "compliance-policy-worker": {}
  },
  "tool_calls": [
    "evaluate_order_compliance",
    "query_pinecone_risk_policy"
  ],
  "risk_head": {
    "binding": false,
    "manual_approval_required": true,
    "recommended_actions": []
  }
}
```

내부 State와 사용자용 문장은 분리한다.

```text
현재 주문은 실행할 수 없습니다.

- 주문 후 삼성전자 비중이 사용자가 설정한 30% 한도를 초과합니다.
- 현재 VaR도 설정한 한도를 초과합니다.
- 이 개인 주문을 직접 금지하는 법령 조항은 확인되지 않았습니다.
- 이번 보류는 법률 위반 판정이 아니라 사용자 규칙과 Risk 한도 초과 때문입니다.
```

## 9. Evidence와 실패 경계

Risk policy tool은 `risk-compliance-policy` namespace만 조회한다.
QA namespace로 fallback하지 않는다.

필수 metadata는 `chunk_id`, `document_id`, `version`, `clause_id`,
`effective_from`, `effective_to`, `authority`, `document_type`, `title`,
`source_url`이다. Point-in-Time 조건을 만족하지 않는 evidence는 승인 근거로
사용하지 않는다.

```text
Pinecone credential/timeout/응답 오류 -> UNAVAILABLE 또는 DEGRADED
embedding 또는 유효 evidence 없음    -> INCONCLUSIVE 또는 ESCALATE
Risk 데이터 없음                      -> HOLD 또는 NEED_MORE_DATA
```

모든 실패 경로에서 `APPROVE`로 fallback하지 않는다.

## 10. 구현 위치와 검증

주요 구현 위치:

- `departments/03-risk/risk_mandate_workers.py`
- `departments/03-risk/integrations/pinecone_client.py`
- `departments/03-risk/tools/policy_tools.py`
- `departments/03-risk/tools/order_tools.py`
- `departments/03-risk/api/app.py`

검증 명령:

```bash
source ~/claude/bin/activate
ruff check departments/03-risk
python -m pytest departments/03-risk/tests -q -rs
python -m pytest tests/api/test_risk_domain_mandate_api.py tests/api/test_risk_mandate_bff.py -q -rs
```

국가법령정보센터 API 수집·OCR·청킹·Pinecone upsert는 이 실행 흐름의 책임이
아니다. 법령 원문은 별도 수집 파이프라인에서 적재하고,
`compliance-policy-worker`는 적재된 Risk namespace를 읽기 전용으로 조회한다.
## 11. 상시 직원 호출과 역할별 검증 경계

Risk 사건이 생성되면 Head는 두 직원을 기본적으로 모두 호출한다. 그러나 두 직원은
서로 다른 검증을 수행하며, 한 직원이 다른 직원의 역할을 대체하지 않는다.

```text
Risk Head
├─ risk-runner: RISK_CHECK
│  └─ Mandate schema·수치·한도·주문 안전성 결정론적 검사
└─ compliance-policy-worker: query_mode
   └─ Mandate 의미 자문 또는 내부 정책·법률 근거 검토
```

`risk-runner`의 `RISK_CHECK`가 다음을 독점한다.

- 필수 필드·자료형·허용 범위 검증
- 단일 종목·섹터·총 익스포저·Drawdown·VaR 한도 계산
- 허용 자산·레버리지·주문 승인 방식의 결정론적 검사
- 현재 주문·포트폴리오의 사용자 Mandate 위반 판정
- `USER_MANDATE_BREACH`, `APPROVE`, `RESIZE`, `REJECT`, `HOLD` 등 authoritative 결과 생성

`compliance-policy-worker`는 위 항목을 계산하거나 판정하지 않는다. 특히
`USER_MANDATE_BREACH`를 생성하지 않으며, 주문 승인·거절도 하지 않는다.

`compliance-policy-worker` task에는 다음 routing 필드를 포함한다.

```json
{
  "query_mode": "MANDATE_REVIEW",
  "law_wiki_required": false,
  "source_targets": [],
  "execution_mode": "LIVE",
  "arms": []
}
```

허용되는 `query_mode`:

- `MANDATE_REVIEW`: 사용자가 작성한 목표·위험 선호·자산 정책의 자연어 의미가 모호하거나 서로 충돌하는지 자문한다. 법률·내부 정책 검색은 하지 않으며, 필수 필드·숫자 범위·주문 위반 여부는 검사하지 않는다.
- `RISK_POLICY_REVIEW`: 내부 Risk 정책·Restricted List 검토. 내부 정책만 검색.
- `LEGAL_QUERY`: 법령·행정규칙·법령해석례·판례 검색.
- `MIXED_REVIEW`: 동일 질문에 대한 내부 정책과 법률 근거를 함께 검토한다. Risk 수치 계산은 `risk-runner`가 별도로 수행한다.
- `NOT_APPLICABLE`: 정책·법률 검토가 현재 질문과 무관함.

`order`가 존재하면 `risk-runner`는 항상 호출한다. 사용자가 법률·규정·법적
문제를 질문했거나 `query_mode`가 `LEGAL_QUERY`/`MIXED_REVIEW`이면
`compliance-policy-worker`가 법률 LLM-Wiki를 사용한다. 자연어 분류보다
구조화된 `query_mode`와 `order != null` 규칙이 우선한다.

**직원 자체 라우팅**(`departments/03-risk/risk_employee_workers.py`,
`WorkerSpec.route_query_mode=True`): 부서장이 구조화된 `query_mode`를 이미
보내면 그대로 쓰고 LLM은 부르지 않는다(§4 구조화 우선). 없고 자연어 질문
(`compliance.query`/`compliance.question`)만 있으면 `compliance-policy-worker`의
LangGraph가 `route → tool → worker_llm → validate` 순서로 스스로 분류한다 —
라우팅 판단과 최종 서술 모두 **동일한 모델**(로컬 Ollama `qwen3:1.7b`,
`default_worker_llm`)이 맡고, 별도 분류 모델을 새로 두지 않는다. 라우팅이 고른
`query_mode`에 따라 `_compliance_tool`이 결정론적으로 근거를 채운다
(`LEGAL_QUERY`/`MIXED_REVIEW` → `tools/legal_wiki_tool.py`, 그 외 → 기존
evidence-passthrough). 이 tool의 결과(`tool_output`, LEGAL_QUERY의 경우 OpenAI
기반 `arms.py` 인용·판정 포함)는 다시 같은 LangGraph state로 들어가고, 같은
Ollama 모델이 이를 서술해 `summary`/`evidence_refs`/`escalate`와 함께
`query_mode`/`routing_rationale`을 포장해 Hermes 부서장에게 돌려준다.
라우팅 분류가 실패하면(JSON 파싱 실패 등) 범위를 좁히지 않고
`MIXED_REVIEW`로 fail-open한다 — 근거 없음을 위반 없음으로 단정하지 않는다는
§9 원칙과 동일하다.

## 12. 부서장 State의 routing 예시

법률 질의가 아닌 Mandate 저장:

```json
{
  "routing": {
    "risk_runner": {
      "dispatch": "REQUIRED",
      "mode": "RISK_CHECK"
    },
    "compliance_policy_worker": {
      "dispatch": "REQUIRED",
      "query_mode": "MANDATE_REVIEW",
      "law_wiki_required": false,
      "source_targets": []
    }
  }
}
```

법률·안전 혼합 질의:

```json
{
  "routing": {
    "risk_runner": {
      "dispatch": "REQUIRED",
      "mode": "FULL_RISK_CHECK"
    },
    "compliance_policy_worker": {
      "dispatch": "REQUIRED",
      "query_mode": "MIXED_REVIEW",
      "law_wiki_required": true,
      "source_targets": ["law", "admrul", "expc", "prec"]
    }
  }
}
```

## 13. 실험 Arm 실행 규칙

LLM-Wiki 실험에서는 법률 질의만 A/B/C 세 Arm에 동일 입력으로 fan-out한다.

```text
execution_mode = EXPERIMENT
  -> arms = ["A", "B", "C"]

execution_mode = LIVE
  -> 승인된 Arm 하나만 사용
```

이 예시에서 `risk-runner`는 Mandate의 구조·수치 검증을 수행하고,
`compliance-policy-worker`의 `MANDATE_REVIEW`는 자연어 표현의 모호성이나
설명 부족만 기록한다. 두 결과가 충돌하면 수치·한도에 관한 결정은 항상
`risk-runner` 결과를 따른다.

실험 결과가 나오기 전에는 `hermes/config.yaml`, worker registry,
`skills/agentic-rag` production wiring을 변경하지 않는다. 일반 Mandate 저장이나
법률과 무관한 Risk 질의는 LLM-Wiki 실험 Arm을 호출하지 않고
`MANDATE_REVIEW` 또는 `NOT_APPLICABLE` 결과를 남긴다. 이 결과는 Compliance Worker의
자문 기록일 뿐이며, Mandate 위반 판정은 `risk-runner`의 `RISK_CHECK`에서만 가져온다.

golden set(15문항) 평가에서 Arm C(grep+BM25 fallback)가 Arm A(plain RAG)를
verdict_acc(0.87 vs 0.53)·semantic_acc(0.73 vs 0.33) 전 지표에서 앞서
(`experiments/llm_wiki/results/comparison_report_final_judged.md`), `LEGAL_QUERY`/
`MIXED_REVIEW`의 검색 경로로 Arm C가 채택됐다. 이 wiring은
`departments/03-risk/tools/legal_wiki_tool.py`(얇은 wrapper) →
`risk_mandate_workers.py`의 `_legal_query()`에만 있고, `hermes/config.yaml`·worker
registry·`skills/agentic-rag`는 여전히 변경하지 않았다 — 즉 experiment 모듈
(`experiments/llm_wiki/arms.py`)을 이 wrapper가 직접 import하는 과도기 상태다.
`# ponytail` 주석대로, 다음 단계는 `experiments/llm_wiki`를 `skills/policy_rag.py`로
승격하는 것이다.
