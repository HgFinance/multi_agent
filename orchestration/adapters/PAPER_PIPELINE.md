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
