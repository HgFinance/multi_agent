# Autonomous Research Pipeline Rebuild

Status: runtime cutover completed for the isolated research surface. Legacy source,
database migrations, and rollback evidence remain deliberately retained until their
shared dependencies are migrated.

## Objective

Build an evidence-driven, natural-language strategy research loop around Hermes. The reference point is [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading), whose public design connects natural-language research to data loaders, strategy generation, backtesting, reports, exports, persistent memory, skills and multi-agent workflows. This repository needs a stricter research boundary: a language model may discover and test ideas, but it cannot turn a promising backtest into an order or a production approval.

## Findings from the repository review

The former strategy factory is not one isolated service. It is a mesh of Python registries, database tables, Kanban cards, Docker services, runtime-contract files, strategy templates and deployment overlays. The same experiment can therefore be represented simultaneously as a factory proposal, a board card, a database row and a quant pipeline object. This creates competing sources of truth and makes a new autonomous loop collide with stale state.

The most dangerous coupling points are:

- `departments/01-research/factory/` and `departments/01-research/contracts/factory_contracts.py`;
- `departments/04-quant-backtest/pipeline/factory_bridge.py` and its legacy orchestrator/worker callers;
- the removed `Dockerfile.factory`, `scripts/factory_runtime_contract.py` and former factory compose services;
- the `factory-kanban-*`, `factory-autopilot`, `factory-experiment-worker` and factory-coupled watchdog services;
- the local and AWS compose overlays that recreate those services;
- tests and runbooks that assert the old board/image/service names.

Some quant backtest modules still import `strategy_templates.py`. That file is currently a shared execution dependency, not an isolated deletion target. It must not be deleted until its callers are replaced and the execution path has its own contract.

## New boundary

**Ownership clarification (2026-08-27):** the actual strategy researcher is
the independent `strategy-hermes` runtime. `departments/01-research/autonomous/`
is its logically owned lab/intake/storage/validation infrastructure; the
`01-research` path is retained only for repository placement and rollback
compatibility. Research HQ supplies evidence and market-data contracts but
does not invoke `runner.py`, create the lab, author strategy code, or run the
strategy backtest. See [`departments/01-research/autonomous/OWNERSHIP.md`](../../departments/01-research/autonomous/OWNERSHIP.md).

```text
natural-language objective
        ↓  POST /ui/strategy-research/ask
durable intake manifest
        ↓  Strategy Hermes worker (direct Hermes session)
file-backed research lab (labs/<request_id>/)
        ↓
Hermes + autonomous-quant-research skill
        ↓
resource map → competing hypotheses → preregistered plan
        ↓
isolated experiment and result artifact
        ↓
validator → adversarial director → lineage/failure memory
        ↓
evidence-gated candidate report (no live/order side effect)
```

The new lab uses `objective.json`, `hypotheses/`, `plans/`, `results/`, `agent-runs/`, an append-only `events.jsonl`, and the six human-readable research files required by the Hermes brief. It does not import the old factory contracts, bridge, Kanban board or database schema. The Hermes adapter accepts only a fixed binary invocation and never accepts a shell command from the objective.

The BFF centrally classifies strategy-generation sentences at both `/ui/ceo/ask` and the shared
Web/Discord `/ui/ceo/ingress` route. It records them through the independent strategy intake
contract; the control-room chat may use the same CEO-compatible endpoint, but it is not the
authority for routing. The opt-in
The dedicated `strategy-hermes` worker materializes one lab per `request_id`, starts a direct Hermes
research session, and leaves
the candidate and lineage under that lab. Status is read through the matching status route. The
worker reports `BLOCKED` errors durably so a transient failure is visible and retryable; it never
routes the request through CEO Kanban, the retired factory, order, broker or OMS paths.
Independent active labs run with the bounded `AUTONOMOUS_RESEARCH_MAX_CONCURRENCY` setting
(default 2); intake materialization and report ordering remain deterministic.

## Safe cutover sequence

Each row is a separate change and verification point. Stop if a precondition or verification check fails.

1. **Add and test the new lab.** Run unit tests, initialize a temporary lab, create a plan, and verify that an invalid/missing-cost/leaky result cannot become a candidate.
2. **Validate compose wiring without starting an agent.** Render compose configuration, build the new autonomous runtime image, and confirm it has no Docker socket, factory image, factory board or order-service mount.
3. **Run a bounded non-live smoke.** Use a temporary lab and a synthetic or already-approved historical fixture. Verify result ingestion, lineage, failure memory and candidate reporting. Do not connect an OMS or broker.
4. **[completed] Migrate unrelated consumers off `Dockerfile.factory`.** `strategy-runtime-control`, skill-evolution workers and the retained general queue watchdog use `Dockerfile.operations-runtime`; the retired factory image is not a Compose dependency.
5. **Stop old strategy-factory containers one at a time.** Inspect the exact container, mounts, labels, dependents and recent logs; stop/remove only the named factory worker/autopilot/dispatcher/init containers. Preserve the state and Kanban volumes until their retention decision is explicit.
6. **[completed for the current deployment] Remove old compose service definitions and overlays.** The factory image/services and local factory overlay are absent from the current Compose graph. Keep this retirement record and do not remove historical database migrations.
7. **Retire isolated factory code.** Delete only modules whose runtime callers are zero after the previous step. For every group, run import/compile tests and the relevant service smoke. Leave shared execution, market data, risk, order and paper-control paths intact.
8. **Retire images and volumes separately.** Remove the old image only after no service references it. Remove named volumes only after confirming that audit/history is exported or intentionally discarded; this is a separate, potentially destructive decision.

## Current retention policy

Retain legacy source, migrations and volumes as rollback evidence until their shared
dependencies are migrated. The current runtime has zero retired factory image/service
references; source-level factory names may remain only in the explicitly retained
legacy modules and historical migration/benchmark evidence. Do not run a broad
`docker compose down`, recursive deletion, `git reset`, or volume prune.
