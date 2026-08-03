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
