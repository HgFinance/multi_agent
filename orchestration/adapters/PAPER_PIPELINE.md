# Full paper adapter

`paper-e2e` remains a tool-free Hermes smoke check. The `paper` adapter is
the read-only handoff path for one investment case:

```text
Research employees -> research_packet
Trading contract   -> order_intent
Risk employees + deterministic Risk Engine -> risk_decision
OMS/Fill projection -> execution_result (never submitted)
Accounting projection -> accounting_snapshot (never posted)
CEO Luna -> ceo_case_summary (always non-binding)
CEO response delivery -> QA employees + deterministic Evidence QA (post-response audit, async)
```

## Execution modes

- `test`: deterministic seven-boundary contract fixture. It runs the response
  path `research → trading → risk → oms-fill-gate → accounting → CEO response`
  and records `qa-audit` as `QUEUED_ASYNC` after that response. It never calls
  Ollama, Hermes, Notion, Redis, Postgres, a broker, or a ledger, and its CEO
  result is explicitly non-binding.
- `paper`: invokes the configured LangGraph Workers and Hermes adapters where
  available, but keeps OMS, broker, ledger, and database effects disabled.
  Dependency failures remain visible as `DEGRADED` and fail closed to
  `HOLD`/`ESCALATE`.
- `paper-e2e`: verifies the boundary adapters and Hermes smoke contract only;
  it is not a full Worker execution.
- `production`/`live`: requires explicit approved department adapters. Missing
  adapters return `BLOCKED` with the workflow's safe failure action; no
  automatic production adapter is inferred from paper or test mode.

The `test` mode is the acceptance check for pipeline wiring. It proves
contract propagation and safety metadata, not market-data correctness,
portfolio exposure, policy compliance, or investment performance.

부서 실행 계층은 모든 부서에서 동일하다. Hermes Agent가 부서장을 맡고, 부서 직원은 역할별 독립 LangGraph Worker Graph와 Runtime이 선택한 Worker 모델을 사용한다. Worker는 허용된 도구 결과를 `worker-context.v1`로 만들어 부서장에게 전달할 뿐이며, Risk/QA 결정론 엔진의 바인딩 판정이나 CEO의 최종 비바인딩 종합을 대체하지 않는다.

전 부서 Worker 계약과 HR의 active/conditional/paused/retired 운영 기준은 [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](../../docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)에, 모델 선택 기준은 [WORKER_MODEL_MATRIX.md](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)에 고정한다.

`strategy-research`, `workforce-management`, `agent-evolution`의 Quant·HR·QA·CEO review step도 동일한 Worker Registry를 사용한다. 이 보조 cycle의 paper 산출물은 `PAPER_CONTEXT_ONLY`이며 Production 승격·권한 부여·Profile 변경·Rollback을 수행하지 않는다.

Use it from the repository root:

```bash
source ~/claude/bin/activate
python -m orchestration.workflows.runner \
  --workflow investment-case \
  --mode paper \
  --symbol AAPL \
  --quantity 100 \
  --limit-price 200.00 \
  --json
```

Before running, synchronize the Git-managed CEO profile to the local Hermes
runtime. The command never grants live order or ledger permissions:

```bash
./scripts/sync_hermes_profiles.sh push
hermes --profile ceo-agent auth status openai-codex
```

If Research, Risk, or CEO dependencies fail, the adapter records a
degraded result and preserves `HOLD / ESCALATE`. It does not convert a
fallback into approval.

The QA audit receives the same CEO input and response after the response is
persisted. QA findings are recorded as `PASS`/`WARN`/`FAIL` or `ESCALATE`; they
do not delay, rewrite, or cancel the already delivered CEO response. Pre-submit
safety remains the deterministic Risk/OMS boundary.

## Risk/QA runtime evidence

Risk and QA are loaded through isolated department adapters. This is required
because both departments have local top-level modules such as `reporting.py`,
while Risk also lazily imports its local `api/app.py`. The adapter temporarily
binds the correct department import paths and Hermes profile for each call, then
restores the process state.

The CEO handoff keeps the following non-secret evidence for both departments:

- LangGraph entered or not, pipeline status, trace/input hash, and replay journal metadata.
- Executed, failed, and conditionally skipped employee personas.
- Source/runtime Hermes profile and model, config-match status, skill count, memory count, and supervisor call status.
- Deterministic verdict, reason codes, fallback node/error, and safe action.

`DEGRADED`, `FAILED`, and `HALTED` evidence always wins over a superficial
handler completion status. A failed Risk/QA node remains `HOLD`, `REJECT`, or
`ESCALATE`; it is never promoted to approval by the paper adapter.
