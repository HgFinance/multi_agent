# Paper Case Reporting

`paper-e2e` is a non-mutating connection check. It invokes each configured
Hermes profile with a tool-free smoke prompt and validates the handoff order.
It does not run a market forecast, submit an order, create a fill, post a
ledger entry, or write Supabase/Redis/Notion state.

Generate one CEO-facing Markdown projection from the runner JSON:

```bash
source ~/claude/bin/activate
python -m orchestration.workflows.runner \
  --workflow investment-case \
  --mode paper-e2e \
  --symbol AAPL \
  --quantity 100 \
  --limit-price 200.00 \
  --json > /tmp/investment-case-paper.json

python -m orchestration.reports.paper_case \
 --run-json /tmp/investment-case-paper.json \
 --output orchestration/reports/paper_case_report_aapl-001.md

# 직원·Risk·QA 실행과 CEO Luna 종합까지 연결하는 읽기 전용 paper mode
python -m orchestration.workflows.runner \
 --workflow investment-case \
 --mode paper \
 --symbol AAPL \
 --quantity 100 \
 --limit-price 200.00 \
 --json > /tmp/investment-case-paper-domain.json
```

The report always marks its forecast as `SIMULATION_ONLY` and its CEO result
as non-binding. A complete smoke run can be `PAPER_CONNECTED`, but the CEO
paper decision remains `HOLD / ESCALATE` until real evidence and separately
approved production controls exist.

## Production adapter approval

Promotion is an explicit governance event, not a successful smoke test. The
candidate adapter must have an immutable artifact digest, declared contracts,
tool allowlist, timeout/retry and idempotency behavior, and a rollback owner.
QA independently verifies replay and no-side-effect behavior. Risk verifies
portfolio/market snapshots, limits, stress/VaR/Greeks, kill switch and
fail-closed behavior. An authorized operator records the approval scope and
expiry. IAM then deploys only that artifact through a shadow/canary gate. Any
failure produces `HOLD`, `REJECT`, or `ESCALATE`; no automatic promotion is
allowed.
