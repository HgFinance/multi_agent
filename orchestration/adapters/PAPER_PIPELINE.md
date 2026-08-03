# Full paper adapter

`paper-e2e` remains a tool-free Hermes smoke check. The `paper` adapter is
the read-only handoff path for one investment case:

```text
Research employees -> research_packet
Trading contract   -> order_intent
Risk employees + deterministic Risk Engine -> risk_decision
QA employees + deterministic Evidence QA -> qa_assessment
OMS/Fill projection -> execution_result (never submitted)
Accounting projection -> accounting_snapshot (never posted)
CEO Luna -> ceo_case_summary (always non-binding)
```

부서 실행 계층은 모든 부서에서 동일하다. Hermes Agent가 Codex 또는 Claude Code에 연결된 부서장이며, 부서 직원은 역할별 독립 LangGraph Worker Graph와 Ollama `qwen3:8b`를 사용한다. Worker는 허용된 도구 결과를 `worker-context.v1`로 만들어 부서장에게 전달할 뿐이며, Risk/QA 결정론 엔진의 바인딩 판정이나 CEO의 최종 비바인딩 종합을 대체하지 않는다.

전 부서 Worker 계약과 HR의 active/conditional/paused/retired 운영 기준은 [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](../../docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)에, 모델 선택 기준은 [WORKER_MODEL_MATRIX.md](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)에 고정한다.

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

If Research, Risk, QA, or CEO dependencies fail, the adapter records a
degraded result and preserves `HOLD / ESCALATE`. It does not convert a
fallback into approval.

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
