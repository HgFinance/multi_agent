# HgFinance Implementation Status

> **Status:** CANONICAL CURRENT · **reviewed:** 2026-08-26 UTC
> Compact status board. This file records implementation/readiness only; it does not own the architecture narrative. See [CURRENT_PROJECT_ARCHITECTURE.md](CURRENT_PROJECT_ARCHITECTURE.md) for the current system, [FINAL_RUNTIME_ARCHITECTURE.md](02-engineering/FINAL_RUNTIME_ARCHITECTURE.md) for execution contracts, and [WORKER_ROLE_BOUNDARIES.md](02-engineering/WORKER_ROLE_BOUNDARIES.md) for worker authority.

## Scope and evidence

Audit basis: executable code, Compose, registry, migrations and tests in the
working tree were inspected on the review date. Mutable `HEAD` and
ahead/behind values are not embedded in this current-status document; record
the exact deployed revision beside runtime evidence. No live AWS process, API,
database, broker account, or GPU verification was performed in this
documentation audit.

Status vocabulary:

- `IMPLEMENTED`: executable code/config and a repository contract exist.
- `PARTIAL`: implementation exists, but integration, deployment, or runtime proof is incomplete.
- `PLANNED`: design or acceptance target exists without the required implementation.
- `HISTORICAL`: retained record, not a current-state claim.
- Verification qualifiers: `CONFIGURED`, `TEST_VERIFIED`, `RESULT_ARTIFACT`, `RUNTIME_VERIFIED`.

## Current status board

| Area | Status | Evidence | Runtime verification | Gap | Next action |
|---|---|---|---|---|---|
| Source-of-truth alignment | CURRENT CHECKOUT REVIEWED | Executable code/config/tests inspected on the review date; exact revision is captured with deployment evidence rather than copied here | None in this checkout | Deployment revision may differ from the working tree | Record deployed revision with runtime evidence |
| Department heads and worker registry | IMPLEMENTED / TEST_VERIFIED | 8 Hermes profiles, 10 LLM-capable workers, 5 deterministic runners; `tests/test_worker_architecture.py`; department `config.yaml` and `employee_workers.py` | Current process topology not verified | End-to-end lifecycle is not one completed production path | Run environment-specific contract probes |
| Worker authority boundaries | IMPLEMENTED / TEST_VERIFIED | `WORKER_ROLE_BOUNDARIES.md`; deterministic Risk, OMS, ledger/NAV, reconciliation, QA and release-gate boundaries | External services not verified | Some cross-department acceptance scenarios remain partial | Keep authority checks in contract tests |
| Worker model serving | IMPLEMENTED / CONFIGURED | Compose, gateway and registry match the [Worker Model Matrix](02-engineering/WORKER_MODEL_MATRIX.md) AWQ path | AWS digest, startup, restart, API health, VRAM not verified | Effective environment override and live health require runtime evidence | Capture approved canary evidence separately |
| Adapter resolution | IMPLEMENTED / PARTIAL | vLLM LoRA plumbing, registry resolution, enabled/base fallback in runtime code | Adapter load/health not verified here | Promotion and rollback evidence is not complete | Validate adapter contract with a non-production test |
| General request routing and Kanban | PARTIAL | CEO direct/delegate paths, Hermes/Kanban contracts, department handoff code/docs | Full request lifecycle not verified | Cross-department persistence and synthesis remain incomplete | Close one end-to-end case with trace and provenance |
| General-response QA | IMPLEMENTED / PARTIAL | Conditional QA fan-out and asynchronous post-hoc governance path | Not runtime verified | Topology-specific delivery evidence | Preserve async behavior for ordinary responses |
| Blocking Risk/QA decision gates | IMPLEMENTED / PARTIAL | Risk/QA barriers in portfolio/decision workflows; deterministic fail-closed checks | Not runtime verified | Acceptance coverage across all decision paths | Test blocking vs async topology separately |
| Forward-QA dispatch and reproduction | IMPLEMENTED / PARTIAL | `qa_events/worker.py`, `reproduction_worker.py`, `docker-compose.yml`, `20260818000300_intraday_forward_qa_dispatch.sql`, tests | Container health and live lease/stream behavior not verified | Operational deployment and acceptance evidence | Complete forward-QA acceptance scenarios |
| Intraday quant experiment governance | IMPLEMENTED / PARTIAL | `intraday_experiment_runner.py`, `intraday_trial_ledger.py`, `intraday_candidate.py`, forward-confirmation/publish gates, tests | Market-data/runtime behavior not verified | Production promotion evidence is absent | Validate statistical and stock-scope gates with approved data |
| Research/evidence grounding | IMPLEMENTED / PARTIAL | Market-axis collectors plus request-time Research API/MCP providers, Agentic RAG, PIT/citation/provenance guards | Provider freshness and external API health not verified | Request-time qualitative evidence must not leak into historical backtests | Reproduce a grounded packet with provenance |
| Automated Trading/OMS lane | PARTIAL | `departments/02-trading/contracts`, Paper OMS and deterministic desk runner | No LIVE execution; no continuously operated automated-strategy lifecycle claim | StrategySignal → OrderIntent → Risk → order → fill lifecycle is not closed as one production path | Complete automated paper-only acceptance path |
| Local fixture PAPER directive | IMPLEMENTED / TEST_VERIFIED / PAPER ONLY | ADR-0007; deterministic verifier, durable directive service and LS PAPER adapter | Broker/account runtime health not verified; local UI exposure remains a product setting | No LIVE execution and no external-user login are in scope | Preserve the fixed-fixture, fail-closed PAPER boundary |
| Accounting/portfolio | PARTIAL | ledger, reconciliation, portfolio contracts under `departments/05-accounting-portfolio` | No official NAV/ledger runtime verification | Canonical posting/PnL lifecycle remains incomplete | Close deterministic ledger/reconciliation scenario |
| Data and storage plane | IMPLEMENTED / PARTIAL | Supabase/Postgres migrations, TimescaleDB migrations, Redis event/state code, SQLite support paths | No current service health check in this audit | Cross-store provenance and operational wiring remain partial | Verify ownership and trace continuity per workflow |
| External providers | IMPLEMENTED CODE / CONFIGURED | LS market data, LS PAPER adapter, OpenDART/NAVER/ECOS/FRED/Tavily request-time providers and market-axis collectors | Provider liveness/credentials not verified | Configured does not imply live; unsupported providers remain DISABLED/UNAVAILABLE | Record provider-specific smoke evidence |
| Quantization/LoRA/Hybrid quality evaluation | RESULT_ARTIFACT | FP8, AWQ, adapter-only and Hybrid result families under `benchmarks/quantization/results/`; each run owns raw/score/provenance | 2026-08-25 local inference artifacts exist; AWS deployment health is separate | Historical and current runs are not one comparable series | Use only provenance-matched comparisons |
| Quantization serving experiment | HISTORICAL + CURRENT CONFIG | FP8 is historical comparison/rollback evidence; current defaults belong to the Worker Model Matrix | Runtime metrics are not remeasured here | VRAM/KV/throughput/restart evidence is not a current runtime assertion | Keep measured artifacts separate from current config |
| QLoRA training and promotion pipeline | PARTIAL | Dataset preparation, arithmetic specialist adapter and selective serving path exist | Training/deployment lifecycle not runtime verified here | General reusable promotion, rollback and held-out enforcement remain incomplete | Complete promotion governance outside evaluation data |
| Held-out evaluation governance | IMPLEMENTED POLICY / PARTIAL | External-50, Internal-50 v1, Internal-50 v2 are evaluation-only families | Dataset lineage audit not rerun here | LoRA training pipeline still needs explicit exclusion checks | Enforce exclusion in dataset validation |

## 4.1 재일님: 리서치본부와 퀀트/백테스트본부

Current status is summarized in the board above. Detailed research/quant contracts and source ownership remain in the department guides, `CURRENT_PROJECT_ARCHITECTURE.md`, and the relevant engineering documents. This anchor is retained for existing department links.

## 4.2 도현님: 트레이딩본부, 회계/포트폴리오본부와 공통 Platform

Trading and accounting remain prototype/partial integration areas. Their deterministic authority boundaries are canonical in `WORKER_ROLE_BOUNDARIES.md`; this anchor is retained for existing department links.

## 4.3 동규님: 리스크본부와 AI QA/감사본부

Risk approval and QA verdict authority remain deterministic. Ordinary-response QA may be asynchronous; blocking decision/paper workflows retain explicit Risk/QA barriers. Forward-QA dispatch/reproduction is a separate main-tracked lane. This anchor is retained for existing department links.

## 4.4 영주님: CEO Office와 Agent Workforce 인사팀

CEO/Hermes coordination and workforce/profile management are implemented at the contract/profile level, while full production lifecycle completion remains partial. This anchor is retained for existing department links.

## 8. 2주 통합 실행 보드

The former detailed two-week execution board is intentionally not duplicated here. Use the current architecture and department-local plans for technical detail; use this status board for readiness. The legacy anchor is retained for existing backlog links.

## Historical records

Earlier dated execution audits, container counts, database row counts, and test snapshots remain available in Git history. They are historical evidence and must not be read as current runtime verification. The target-state plan remains [HEDGE_FUND_MASTER_PLAN.md](HEDGE_FUND_MASTER_PLAN.md).

## Canonical document map

| Concern | Canonical document |
|---|---|
| Current architecture and measured/qualified model history | [CURRENT_PROJECT_ARCHITECTURE.md](CURRENT_PROJECT_ARCHITECTURE.md) |
| Compact implementation/readiness board | This document |
| Runtime and environment contracts | [FINAL_RUNTIME_ARCHITECTURE.md](02-engineering/FINAL_RUNTIME_ARCHITECTURE.md) |
| Worker role, trigger, tool and authority detail | [WORKER_ROLE_BOUNDARIES.md](02-engineering/WORKER_ROLE_BOUNDARIES.md) |
| Model compatibility index | [WORKER_MODEL_MATRIX.md](02-engineering/WORKER_MODEL_MATRIX.md) |
