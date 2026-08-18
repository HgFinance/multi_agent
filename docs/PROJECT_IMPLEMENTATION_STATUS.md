# HgFinance Implementation Status

> Compact status board. This file records implementation/readiness only; it does not own the architecture narrative. See [CURRENT_PROJECT_ARCHITECTURE.md](CURRENT_PROJECT_ARCHITECTURE.md) for the current system, [FINAL_RUNTIME_ARCHITECTURE.md](02-engineering/FINAL_RUNTIME_ARCHITECTURE.md) for execution contracts, and [WORKER_ROLE_BOUNDARIES.md](02-engineering/WORKER_ROLE_BOUNDARIES.md) for worker authority.

## Scope and evidence

Audit basis: `qa-department` at `3a6607f` and `origin/main` at `4149454`. The branch is seven commits behind `origin/main`; the model-serving files checked in both refs are aligned on AWQ. Main-only forward-QA, quant-experiment, Compose, migration, and test changes are marked `TRACKED_MAIN`. No live AWS process, API, database, or GPU verification was performed in this audit.

Status vocabulary:

- `IMPLEMENTED`: executable code/config and a repository contract exist.
- `PARTIAL`: implementation exists, but integration, deployment, or runtime proof is incomplete.
- `PLANNED`: design or acceptance target exists without the required implementation.
- `HISTORICAL`: retained record, not a current-state claim.
- Verification qualifiers: `TRACKED_MAIN`, `TRACKED_BRANCH`, `TEST_VERIFIED`, `RUNTIME_VERIFIED`.

## Current status board

| Area | Status | Evidence | Runtime verification | Gap | Next action |
|---|---|---|---|---|---|
| Source-of-truth alignment | PARTIAL / TRACKED_MAIN | `qa-department` `3a6607f`; `origin/main` `4149454`; branch 7 commits behind | None in this checkout | Merge/review boundary remains external to this audit | Review main-derived documentation before branch integration |
| Department heads and worker registry | IMPLEMENTED / TEST_VERIFIED / TRACKED_MAIN | 8 Hermes profiles, 10 LLM-capable workers, 5 deterministic runners; `tests/test_worker_architecture.py`; department `config.yaml` and `employee_workers.py` | Current process topology not verified | End-to-end lifecycle is not one completed production path | Run environment-specific contract probes |
| Worker authority boundaries | IMPLEMENTED / TEST_VERIFIED | `WORKER_ROLE_BOUNDARIES.md`; deterministic Risk, OMS, ledger/NAV, reconciliation, QA and release-gate boundaries | External services not verified | Some cross-department acceptance scenarios remain partial | Keep authority checks in contract tests |
| Worker model serving | IMPLEMENTED / TRACKED_MAIN / TRACKED_BRANCH | Compose, `departments/worker_model_gateway.py`, and Research registry use Qwen2.5-14B-Instruct-AWQ, 8192, 0.85, KV FP8, LoRA 4/32/8 | AWS digest, startup, restart, API health, VRAM not verified | No current runtime evidence in repository | Capture approved canary evidence separately |
| Adapter resolution | IMPLEMENTED / PARTIAL | vLLM LoRA plumbing, registry resolution, enabled/base fallback in runtime code | Adapter load/health not verified here | Promotion and rollback evidence is not complete | Validate adapter contract with a non-production test |
| General request routing and Kanban | PARTIAL | CEO direct/delegate paths, Hermes/Kanban contracts, department handoff code/docs | Full request lifecycle not verified | Cross-department persistence and synthesis remain incomplete | Close one end-to-end case with trace and provenance |
| General-response QA | IMPLEMENTED / PARTIAL | Conditional QA fan-out and asynchronous post-hoc governance path | Not runtime verified | Topology-specific delivery evidence | Preserve async behavior for ordinary responses |
| Blocking Risk/QA decision gates | IMPLEMENTED / PARTIAL | Risk/QA barriers in portfolio/decision workflows; deterministic fail-closed checks | Not runtime verified | Acceptance coverage across all decision paths | Test blocking vs async topology separately |
| Forward-QA dispatch and reproduction | IMPLEMENTED / PARTIAL / TRACKED_MAIN | `qa_events/worker.py`, `reproduction_worker.py`, `docker-compose.yml`, `20260818000300_intraday_forward_qa_dispatch.sql`, tests | Container health and live lease/stream behavior not verified | Operational deployment and acceptance evidence | Complete main-tracked forward-QA acceptance scenarios |
| Intraday quant experiment governance | IMPLEMENTED / PARTIAL / TRACKED_MAIN | `intraday_experiment_runner.py`, `intraday_trial_ledger.py`, `intraday_candidate.py`, forward-confirmation/publish gates, tests | Market-data/runtime behavior not verified | Production promotion evidence is absent | Validate statistical and stock-scope gates with approved data |
| Research/evidence grounding | IMPLEMENTED / PARTIAL | collectors, Research API/MCP, Agentic RAG, PIT/citation/provenance guards | Provider freshness and external API health not verified | Artifact-to-trading handoff is incomplete | Reproduce a grounded packet with provenance |
| Trading/OMS | PARTIAL | `departments/02-trading/contracts`, Paper OMS, broker prototype, deterministic desk runner | No live execution; no production OMS claim | OrderIntent → order → fill lifecycle not closed | Complete paper-only acceptance path |
| Authenticated user PAPER directive | IMPLEMENTED / TEST_VERIFIED / PARTIAL RUNTIME | ADR-0007; CEO → Kanban → Trading Hermes → authenticated MCP → deterministic PAPER directive → Accounting ACK contracts and tests | Local/AWS runtime activation and one market-hours completion still pending | No verified market-hours fill-to-ledger sample yet | Activate the full runtime and capture one bounded PAPER smoke trace |
| Accounting/portfolio | PARTIAL | ledger, reconciliation, portfolio contracts under `departments/05-accounting-portfolio` | No official NAV/ledger runtime verification | Canonical posting/PnL lifecycle remains incomplete | Close deterministic ledger/reconciliation scenario |
| Data and storage plane | IMPLEMENTED / PARTIAL | Supabase/Postgres migrations, TimescaleDB migrations, Redis event/state code, SQLite support paths | No current service health check in this audit | Cross-store provenance and operational wiring remain partial | Verify ownership and trace continuity per workflow |
| External providers | IMPLEMENTED CODE / CONFIGURED | LS Open API, OpenDART, KRX, SerpApi and collectors present in repository | Provider liveness/credentials not verified | Configured does not imply live | Record provider-specific smoke evidence |
| FP8/AWQ quality evaluation | TRACKED_MAIN | External-50 and Internal-50 v1/v2 result artifacts under `benchmarks/quantization/results/` | No inference run in this audit | AWQ+LoRA result column is not tracked | Use the approved held-out protocol |
| Quantization serving experiment | HISTORICAL / TRACKED_MAIN | FP8 is a historical comparison/rollback baseline; AWQ is the tracked serving configuration | Runtime metrics are not remeasured here | VRAM/KV/throughput/restart evidence is not a current runtime assertion | Keep measured artifacts separate from current config |
| QLoRA training pipeline | PLANNED | Training intent and adapter naming are documented; reusable training implementation is not established as current code | None | Dataset validation, NF4 training, artifact promotion and gate are incomplete | Implement and validate outside held-out evaluation data |
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
