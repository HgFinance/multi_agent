---
name: autonomous-quant-research
description: "Run or review persistent, evidence-gated quantitative strategy research from a natural-language objective, including resource discovery, competing hypotheses, adversarial validation, failure memory and lineage. Use for autonomous strategy research; do not use for ordinary trade advice or live order execution."
---

Owner: **Strategy Hermes**. The Research HQ profile may provide evidence and
market-data contracts, but it does not invoke this skill or execute the
strategy-research loop.

# Autonomous Quant Research

Use this skill when a research objective must become a reproducible strategy candidate. The objective is generalisation under realistic costs, not a high backtest score.

## Startup

1. Read the lab files in this order: `OBJECTIVE.md`, `STATE.md`, `RESOURCE_MAP.md`, `KNOWLEDGE.md`, `FAILURE_MEMORY.md`, and `EXPERIMENT_LOG.md`.
2. Inspect the repository and available data before choosing a method. Confirm point-in-time availability, universe membership, timestamps, missingness and execution assumptions.
3. Record multiple competing hypotheses with explicit mechanisms and falsifiers. Treat the hypothesis as unmeasured until an experiment produces evidence.
4. Select the next experiment by expected information gain. Change representation, label, sampling, horizon, regime or model family when the current branch repeats failures; do not hide repetition behind numeric retuning.

## Research loop

Follow: Observe → Understand → Hypothesise → Design → Implement → Run → Analyse → Critique → Learn → Next.

Every experiment must be tied to one preregistered plan. Keep code and generated artifacts under the lab's `experiments/` directory or in explicitly referenced repository paths. Record the exact data references, seed, split boundaries, costs, transformations and artifacts needed to reproduce it.

Before considering a result useful, check for leakage, point-in-time violations, post-publication universe bias, turnover and costs, out-of-sample performance, parameter sensitivity, time/regime/asset slices, delayed execution and leave-one-block-out or leave-one-asset-out failures. Analyse trade paths and failure modes, not only aggregate metrics. A missing measurement is unknown, never zero.

A robust result becomes an evidence-gated candidate report only. It is not permission to create an order, call an OMS, promote to paper/live trading, or change risk limits. If evidence is incomplete, write `BLOCKED`; if a premise is disproven, write `FAILED` or pivot to a linked hypothesis.

## Persistent state and anti-loop rules

The Strategy Hermes runtime owns the research turn. A thin lifecycle
supervisor only materializes intake, starts Hermes and validates the artifacts
Hermes wrote; it must not choose a hypothesis or plan for the agent.

For local artifact inspection, the legacy-compatible lab utility remains:

```text
runner.py init --lab-root <lab> --repo-root <repo> --goal "..."
runner.py cycle --lab-root <lab> --repo-root <repo>
runner.py ingest --lab-root <lab> <result.json>
```

For an interactive request, submit the sentence to `/ui/strategy-research/ask`. The API writes
only an intake manifest; the dedicated `strategy-hermes` service creates
`labs/<request_id>/` and starts a direct Hermes session for each persistent lab. Poll
`/ui/strategy-research/requests/<request_id>` for `QUEUED`, `RESEARCHING`, `BLOCKED` or
`CANDIDATE`. A `request_id` is the research-session identity: replaying it is idempotent, while
a new strategy objective must receive a new identifier.

## On-demand LS market data

Strategy Hermes receives a read-only LS REST credential boundary for one research turn. For
market data use only `departments/01-research/autonomous/ls_market_data.py` and its allow-list:
`t1665`, `t8410`, `t8411`, `t8412`, `t8451`, `t8452`, `t8453` on `/stock/chart`, plus the
allow-listed market-ranking TRs `t1441`, `t1444`, `t1452`, `t1463`, `t1466`, `t1481`,
`t1482`, `t1489`, and `t1492` on `/stock/high-item`. Select the
integrated `t8451/t8452/t8453` family when KRX+NXT coverage is required; use the non-integrated
family only when that distinction is part of the hypothesis. Query the smallest explicit symbol
set and date range that can answer the preregistered question, and respect the one-request-per-
second and 500-row limits through the adapter.

Keep returned rows in memory. If a dataframe needs a file, use `write_temp_json` and write only
below `$STRATEGY_MARKET_DATA_DIR`; that directory is unique to the Hermes turn and is deleted
when the process exits. Persist only the code, result, lineage, and non-sensitive `DataReceipt`
(TR, range, row count and hash). Never read or write `quant-data`, the legacy discovery cache,
collector backfill tables, market/research databases, or a persistent lab path for raw rows. Do
not print raw rows or credentials. A failed or incomplete LS response is `BLOCKED`, not an
empty dataset or a proxy substitution.

Do not import or recreate the retired strategy-factory contracts, Kanban board, factory bridge, factory runtime contract, or factory database as a shortcut. The file-backed lab is the source of research-session state; external systems may provide data or compute but may not silently rewrite evidence.

After each result, update knowledge and failure memory through the lab events. Maintain parent/child lineage for pivots and distinguish explore, challenge and exploit work. A good result must receive an adversarial challenge before it is reported as a candidate. See `references/artifact-schema.md` and `references/experiment-rubric.md` when constructing or reviewing artifacts.
