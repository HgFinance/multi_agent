# HgFinance Current Architecture

> **Status:** current repository snapshot · **reviewed:** 2026-08-18 KST
>
> **Source audit:** this checkout is `qa-department` at `3a6607f`, 7 commits behind
> `origin/main` at `4149454`. Current implementation claims in
> this document follow the newer `origin/main` executable/config state where
> the two differ. The model-serving files are identical between this branch
> and `origin/main`; newer main changes are primarily forward-QA, quant
> experiment, Compose, migration, and contract-test additions. Claims sourced
> from main are labeled `TRACKED_MAIN`; branch-only facts are `TRACKED_BRANCH`.
>
> This document is an implementation audit, not a target-state specification. The
> repository, its contracts, tests, and tracked configuration are the source of
> truth. `RUNTIME_VERIFIED` is reserved for evidence of an actual process/API/DB
> interaction; a profile, plan, or historical report is not runtime proof.

## 1. Executive Summary

HgFinance is an eight-department multi-agent financial/trading platform. The
current execution model separates department-head Hermes agents, conditional
LangGraph workers, and deterministic Python runners. The LLM interprets,
compares evidence, writes explanations, and proposes structured non-binding
outputs. Deterministic contracts and engines own exact calculations, risk
enforcement, accounting authority, QA gates, and state transitions.

The repository currently verifies the following shape:

- **8 department Hermes profiles** under `departments/*/hermes/`.
- **10 configured LLM workers** and **5 deterministic runners** in the current
  worker registry. Older role names remain in profiles as compatibility or
  audit aliases and must not be counted as active workers.
- Trading, risk, accounting, quant, and QA contracts and deterministic modules
  exist, but the repository does not prove one continuously operated,
  end-to-end production order lifecycle.
- Both this checkout and `origin/main` track the promoted
  **Qwen2.5-14B-Instruct-AWQ** worker model, served as
  `qwen2.5-14b-instruct-awq`, with 8192/0.85 serving defaults and FP8 KV
  cache. FP8/16384/0.90 is retained only where historical benchmark or
  runbook material explicitly describes the former baseline.
- `origin/main` tracks FP8/AWQ quality result artifacts. Infrastructure VRAM,
  KV-token, and throughput measurements are not present in the tracked result
  files and are not inferred here.

## 2. Why HgFinance Exists

The platform is intended to turn a user mandate and financial evidence into a
reviewable research or paper-trading workflow while preserving separation of
duties. A normal path is:

1. interpret the request and mandate;
2. route only the necessary departmental work;
3. gather point-in-time evidence and produce a research packet or proposal;
4. validate structured outputs, risk constraints, and evidence grounding;
5. create paper-trading state only through the approved deterministic path; and
6. expose a report/projection to the CEO and user.

The repository still describes the system as research/development and does not
authorize real-money execution by merely having an order or broker prototype.

## 3. System Architecture

```mermaid
flowchart LR
    U[User] --> C[CEO Hermes]
    C --> R[Department routing / Kanban]
    R --> H[Department Hermes head]
    H --> W[LangGraph worker or deterministic Python runner]
    W --> T[RAG / API / DB / trading / risk / accounting tools]
    T --> P[Validated packet or report]
    P --> C
    C --> U
    W -. fail-closed / escalate .-> Q[Risk and QA gates]
```

The persisted handoff boundary is `worker-context.v1`; cross-department
handoffs use `department-handoff.v1`. These are context and provenance
contracts, not transfers of authority. The workflow runner in
`orchestration/workflows/runner.py` treats a missing adapter as a failure or
safe stop; a dry run validates the plan and does not call a department.

Canonical logical department names are mapped in
`orchestration/canonical_profiles.py`. Legacy or unknown aliases are rejected
instead of being silently routed to a different department.

## 4. Eight-Department Organization

The table below is derived from the current `workers` and
`deterministic_workers` mappings in each `hermes/config.yaml`, together with
the corresponding `employee_workers.py` implementation.

| Department | Current worker registry | Execution classification | Current responsibility | Status |
|---|---|---|---|---|
| CEO Office | `executive-briefing-worker`; `ceo-runner` | LLM worker + deterministic runner | cross-department briefing, aggregation of existing decisions, missing-input detection | IMPLEMENTED |
| HR / Agent Workforce | `profile-architecture-worker` | conditional Local/shared LLM Worker | non-binding job-profile and evaluation-set proposals; no activation or provisioning | PARTIAL |
| Research | `competing-explanation-worker`; `holdings-analyst-worker` | conditional Local/shared LLM Workers | falsification/competing explanations and holdings questions | PARTIAL |
| Quant / Backtest | `strategy-author-worker`; `result-interpretation-worker` | conditional Local/shared LLM Workers | strategy authoring and result interpretation; deterministic PIT/backtest/lifecycle gates | PARTIAL |
| Accounting / Portfolio | `exception-investigation-worker`; `back-office-runner` | LLM worker + deterministic runner | investigate breaks/unexplained PnL; move deterministic ledger/NAV/report outputs | PARTIAL |
| Trading | `desk-runner` | deterministic Python runner; no fixed LLM worker | OrderIntent, execution constraints, paper OMS/broker path | PARTIAL |
| Risk | `compliance-policy-worker`; `risk-runner` | conditional Local/shared LLM Worker + deterministic runner | policy evidence and deterministic pre-trade risk enforcement | PARTIAL |
| AI-QA / Audit | `hallucination-critic-worker`; `incident-postmortem-worker`; `qa-runner` | conditional Local/shared LLM Workers + deterministic runner | evidence/model-risk/permission checks, conditional critique and incident analysis | PARTIAL |

This is **10 LLM workers + 5 deterministic runners**. The department-head
profiles are a separate layer: current profile configuration selects
`openai-codex` with `gpt-5.6-luna` where the head runtime is declared. Employee
runtime configuration selects LangGraph/Ollama with default `qwen3:1.7b` where
the department config declares that path. This distinction is enforced by
`tests/test_worker_architecture.py` and
`docs/02-engineering/WORKER_ROLE_BOUNDARIES.md`.

Important role clarifications:

- `bull-thesis-worker`, `bear-thesis-worker`, the former research/quant role
  names, and other names in `personalities` are not the active worker registry
  in this checkout. They are compatibility, legacy, or retired lineage unless
  the current `workers` mapping enables them.
- `02-trading/employee_workers.py` has no fixed LLM worker registry. Its
  temporary strategy helper is explicitly non-binding and cannot submit a
  live order or bypass Risk.
- The Risk and QA supervisors can synthesize and escalate, but their config
  forbids protected write tools. The binding owners are the deterministic
  engines/runners.

## 5. LLM vs Deterministic Responsibility Boundary

| Responsibility | Owner in the repository | Status |
|---|---|---|
| interpretation, competing explanations, evidence analysis, report prose | Hermes/conditional LangGraph workers | IMPLEMENTED/PARTIAL |
| point-in-time and citation checks | deterministic research/QA code, including `EvidenceQaEngine` | IMPLEMENTED |
| exact order and risk validation | `departments/03-risk/engine/risk_engine.py` and trading contracts/OMS | IMPLEMENTED |
| final `APPROVE` / `RESIZE` / `REJECT` risk verdict | deterministic Risk Engine | IMPLEMENTED |
| ledger posting, NAV/valuation, reconciliation | accounting ledger, fill consumer, valuation/reporting modules | IMPLEMENTED/PARTIAL |
| strategy experiment state and release gate | quant pipeline, PIT dataset and lifecycle modules | IMPLEMENTED/PARTIAL |
| QA PASS/WARN/FAIL, model-risk thresholds, permission checks | QA deterministic engines | IMPLEMENTED/PARTIAL |
| authority to promote a strategy or activate a worker/profile | QA/Risk/CEO/governance workflow, not an LLM worker | PARTIAL |

The trading contract makes `StrategySignal`, `OrderIntent`, `RiskDecision`, and
broker order states distinct. `OrderIntent` requires a valid snapshot and
evidence hash; `RiskDecision` enforces quantity/reason invariants. An order
cannot be treated as approved because an LLM produced a plausible narrative.

## 6. Request Lifecycle

```mermaid
sequenceDiagram
    participant User
    participant CEO as CEO Hermes
    participant Dept as Department Hermes
    participant Worker as Worker / Runner
    participant Gate as Risk / QA deterministic gates
    participant Store as Canonical stores / projections

    User->>CEO: request + mandate
    CEO->>CEO: classify direct answer or delegated work
    CEO->>Dept: worker-context.v1 / department handoff
    Dept->>Worker: allow-listed tools and bounded input
    Worker->>Gate: proposal, evidence, OrderIntent, or QA input
    Gate-->>Worker: PASS / HOLD / REJECT / ESCALATE
    Gate->>Store: only validated state transition or audit record
    Store-->>CEO: report / read model
    CEO-->>User: synthesis with uncertainty and provenance
```

The source code contains a CEO primary-result fast path in
`orchestration/adapters/ceo_supervisor.py`. It is an optimization for already
available results, not evidence that every request bypasses departmental
governance. High-risk decisions still require blocking Risk/QA conditions.
The repository does not contain a current measured latency report proving that
the Delegate initial ACK is still the dominant bottleneck.

### QA parallelism clarification

The repository has two different, intentional execution topologies:

| Topology | QA behavior | Evidence |
|---|---|---|
| General response workflow | QA is an independent asynchronous governance lane. CEO may synthesize terminal primary results before QA completes; QA is not a response-synthesis prerequisite. | `orchestration/adapters/ceo_supervisor.py` (`governance_plane=async_qa`, `primary_results_ready_fast_path`); `orchestration/ceo_workflow_scope.py` |
| QA department internals | Eligible conditional QA graphs fan out concurrently, then their reports are fanned in; deterministic `qa-runner` is added to the combined result. | `departments/06-ai-qa-audit/qa_employee_workers.py::run_employee_workers_async`, `asyncio.gather` |
| Blocking decision / paper pipeline | Department stages use explicit barriers. QA remains a blocking gate after the upstream Risk stage for this graph; it is not made asynchronous merely because general responses have an async QA lane. | `orchestration/workflows/portfolio_recommendation.py::build_portfolio_graph` |
| Intraday forward-QA lane (`origin/main`) | Accepted forward evidence is dispatched through a durable outbox/Redis stream, then independently reproduced by a lease-fenced QA worker. Scientific mismatches produce QA verdicts; this is separate from the general-response QA lane. | `departments/06-ai-qa-audit/qa_events/worker.py`, `reproduction_worker.py`, `docker-compose.yml`, `supabase/migrations/20260818000300_intraday_forward_qa_dispatch.sql` |

Therefore “QA is asynchronous” is only correct for the general response
governance lane. “QA is always after everything” is also incomplete: the
conditional QA workers themselves are parallelized, while the blocking graph
preserves its Risk → QA barrier.

## 7. Model Serving Architecture

### 7.1 Repository-verified serving path

```mermaid
flowchart LR
    L[LLM-capable workers] --> G[worker_model_gateway.py]
    G --> V[vLLM OpenAI-compatible endpoint]
    V --> F[TRACKED_MAIN: Qwen2.5-14B-Instruct-AWQ]
    G --> A[Registry-selected LoRA adapter when enabled]
    A --> E[AWQ base + adapter request]
```

`origin/main` commit `b3fb8c5` introduced the AWQ model plane in
`docker-compose.model.yml`, `departments/worker_model_gateway.py`, and
`departments/01-research/config/worker_model_registry.json`. The checked-out
branch contains the same executable serving defaults. Both configurations
enable LoRA with `max-loras=4`, `max-lora-rank=32`, and `max-cpu-loras=8`.

| Setting | `origin/main` tracked value | `qa-department` branch value | Evidence/status |
|---|---|---|---|
| base model directory | `Qwen2.5-14B-Instruct-AWQ` | `Qwen2.5-14B-Instruct-AWQ` | TRACKED_MAIN / TRACKED_BRANCH |
| served model name | `qwen2.5-14b-instruct-awq` | `qwen2.5-14b-instruct-awq` | TRACKED_MAIN / TRACKED_BRANCH |
| max model length | `8192` | `8192` | TRACKED_MAIN / TRACKED_BRANCH |
| GPU memory utilization | `0.85` | `0.85` | TRACKED_MAIN / TRACKED_BRANCH |
| KV cache dtype | `fp8` | `fp8` | TRACKED_MAIN and TRACKED_BRANCH |
| LoRA | enabled; 4/32/8 limits | enabled; 4/32/8 limits | IMPLEMENTED serving plumbing |
| actual AWS process health | not proven by repository artifact | not inspected | RUNTIME_VERIFIED unavailable |

`departments/worker_model_gateway.py`의 모듈 docstring에는 아직 FP8 표현이
남아 있지만, 실행 기본값·Compose·registry는 AWQ로 일치한다. 해당 docstring은
현재 model-plane source of truth가 아닌 stale comment로 분류한다.

## 8. FP8 → AWQ Optimization

**Status: COMPLETED infrastructure promotion in `origin/main`; runtime health
still requires external observation.**

Commit `b3fb8c5` (`deploy: promote AWQ worker model record v2 benchmark`) changes
the tracked model plane from FP8 to AWQ. The serving configuration and registry
are therefore `TRACKED_MAIN`; the older FP8 values remain `TRACKED_BRANCH` on
this checkout and are retained as the comparison baseline.

The repository tracks the following quality artifacts:

- `benchmarks/quantization/results/fp8_external50_final_v1.json`
- `benchmarks/quantization/results/awq_external50_final_v1.json`
- `benchmarks/quantization/results/fp8_internal50_score_v1.json`
- `benchmarks/quantization/results/awq_internal50_score_v1.json`
- `benchmarks/quantization/results/fp8_internal50_v2_score.json`
- `benchmarks/quantization/results/awq_internal50_v2_score.json`

The requested infrastructure values for model-load GiB, KV-cache GiB/tokens,
resident/free VRAM, C1/C2/C4 throughput, E2E, and restart/health were not found
in the tracked Git artifacts or history inspected here. They must not be
reconstructed from quality result files. “Theoretical concurrency” means a
KV/memory-based 8192-token capacity estimate, never a number of agents.

## 9. Quality Evaluation

Results below come from the immutable result files on `origin/main`; candidate
JSON is not used to infer scores. `External-50`, `Internal-50 v1`, and
`Internal-50 v2` remain held out from future LoRA train/dev data.

| Evaluation | FP8 | AWQ | Interpretation |
|---|---:|---:|---|
| External-50 | 37/50 (74%) | 37/50 (74%) | overall external accuracy preserved; derived subset, not an official leaderboard score |
| Internal-50 v1 | 15/50 (30%) | 12/50 (24%) | HgFinance Production Contract Adherence Benchmark; not pure base-model capability |
| Internal-50 v2 Employee Reasoning | 37/50 (74%) | 36/50 (72%) | employee reasoning decreased by 1 case; critical failures remained 1 → 1 with the same `v2-046` ID |

Internal-v1 critical failures are `20/24` for both base variants, so the
benchmark exposes substantial contract-adherence weakness rather than proving
production readiness. Internal-v2 financial arithmetic is weak for both
variants (`3/10` FP8, `2/10` AWQ); exact arithmetic should remain in
deterministic tools.

The tracked result paths are the benchmark evidence. No AWQ+LoRA result is
tracked yet, so the final comparison remains `FP8 | AWQ | AWQ + LoRA` with the
third column pending.

The evaluation separation remains architecturally important: External-50,
Internal-50 v1, and Internal-50 v2 must be held out from future adapter
training. Exact arithmetic should be tested against deterministic tools and
not used as evidence that an LLM is an accounting authority.

## 10. LoRA / QLoRA Training Architecture

**Status: PLANNED; training pipeline not found in the repository.**

The desired future topology is one shared base with department-specific
adapters, not one separately fine-tuned 14B model per department:

```mermaid
flowchart LR
    C[Common dataset] --> D[Common + department dataset]
    S[Department dataset] --> D
    D --> Q["QLoRA on original Qwen2.5-14B-Instruct lineage<br/>4-bit NF4"]
    Q --> A["Department adapter<br/>adapter_model.safetensors + adapter_config.json"]
    A --> AWS[AWS serving artifact]
    AWS --> E[FP8/AWQ/AWQ+LoRA evaluation gate]
    E --> P[Promotion only after QA/Risk/CEO gates]
```

The intended common behavior includes evidence-first responses,
fail-closed handling, no invented facts, role/authority boundaries,
deterministic-tool use for exact calculation, structured output discipline,
and no bypass of Risk/QA/accounting controls. Department data adds specialist
behavior. The production AWQ checkpoint must not be described as the QLoRA
training checkpoint unless an implementation file proves that workflow.

`origin/main` implements serving/registration plumbing in
`docker-compose.model.yml`, `departments/worker_model_gateway.py`, and
`departments/01-research/config/worker_model_registry.json`: vLLM LoRA is
enabled, adapter resolution is registry-controlled, and base-model fallback is
explicit. The registry currently records base-model entries rather than proving
that a department adapter is enabled in the runtime.

That plumbing is not a reusable Colab QLoRA training pipeline. No training
notebook, CSV schema validator, NF4 training script, or adapter promotion run
was found in tracked source; QLoRA training remains `PLANNED`.

## 11. Data & Infrastructure

| System | Current role | Status / source |
|---|---|---|
| Supabase PostgreSQL | canonical operational schemas, RLS, governance, accounting/risk/QA records | IMPLEMENTED/PARTIAL; `supabase/migrations/` and APIs |
| TimescaleDB | market ticks, quotes, bars, breadth, derivatives and market-data quality | IMPLEMENTED/PARTIAL; `timescaledb/migrations/`, market APIs |
| Redis | short-lived event/stream bus, leases, risk/QA/governance notifications, UI mirror | IMPLEMENTED in modules; persistence/replay boundaries remain partial |
| SQLite | local/demo read models and idempotency support, not the financial system of record | IMPLEMENTED as local support; `apps/api/portfolio_store.py`, `orchestration/discord_idempotency.py` |
| Parquet / object storage | documented long-term market/artifact storage path | PLANNED/DOCUMENTED; runtime use not verified here |
| Notion | report/projection destination, not a decision source | IMPLEMENTED as projection path; documented in QA/reporting docs |
| LangSmith / Langfuse | optional observability/evaluation integrations | PARTIAL; tracing defaults and access boundaries require runtime verification |
| Docker Compose | control-plane services plus separate GPU model-plane overlay | IMPLEMENTED as configuration; running state not verified here |
| Hermes | department-head profiles; tracked profiles differ from external runtime under `~/.hermes/profiles/` | IMPLEMENTED configuration; external runtime not verified |
| APIs / BFF | FastAPI department APIs and read-only AI Office/portfolio projections | PARTIAL; projection must not own broker, Risk, ledger, or NAV authority |
| External financial data | LS Open API, OpenDART, KRX, SerpApi and related collectors/registries | PARTIAL; credentials, freshness, and live availability are environment-dependent |

The repository distinguishes canonical PostgreSQL/TimescaleDB state from Redis
cache/event state, local SQLite/demo state, reports, and artifacts. The root
`db/001_execution.sql`–`db/004_seed.sql` prototype schema is not to be applied
with `supabase/migrations/` as if they were one database.

## 12. Governance and Safety Gates

```mermaid
flowchart LR
    R[Research: evidence / proposal] --> Q[Quant: PIT backtest / release gate]
    Q --> QA[QA reproduction and evidence gate]
    QA --> RK[Risk capability and compliance gate]
    RK --> T[Trading: OrderIntent / OMS]
    T --> A[Accounting: Fill / ledger / NAV / reconciliation]
    Q -. recommendation or backtest is not a live trade .-> T
    RK -. reject / resize / hold .-> T
```

The core safety rules are implemented or directly represented by contracts and
tests:

- Research produces evidence-linked research packets and experiment proposals;
  it does not create an order or approve a trade.
- Quant enforces point-in-time dataset checks and lifecycle gates. A release
  candidate is submitted to QA; Quant does not directly promote production.
- Trading accepts typed `OrderIntent` and cannot bypass a valid RiskDecision.
- Risk Engine enforces mandate, tradability, freshness, concentration,
  buying-power, state, and other constraints. Its binding result is
  `APPROVE`, `RESIZE`, or `REJECT`; a policy LLM is advisory.
- Accounting official figures remain in deterministic ledger, fill,
  valuation, reconciliation, and reporting code. The exception worker explains
  breaks; it does not edit official figures.
- QA fails or escalates unsupported facts, future evidence, numeric citation
  mismatch, contradiction, model-risk thresholds, forbidden tools, and
  candidate failures. `EvidenceQaEngine`, `ModelRiskEngine`, and `EvalRunner`
  are the relevant deterministic boundaries.
- CEO coordinates and synthesizes. It cannot submit orders, approve Risk,
  modify the ledger, finalize NAV, or close an audit finding.
- Failure paths use `HOLD`, `REJECT`, `ESCALATE`, `ENTRY_BLOCKED`, or `HALTED`
  rather than silently widening authority.

## 13. Current Bottlenecks

The repository supports these findings:

1. **End-to-end closure:** modules for Research, Quant, Risk, Trading,
   Accounting, and QA exist, but the current status document still marks the
   canonical cross-department order/fill/journal path as incomplete or
   historical. A fresh AWS/runtime probe is needed before calling it
   production-ready.
2. **Main integration drift:** the model plane is aligned on this branch and
`origin/main` is seven commits ahead with additional forward-QA
dispatch/reproduction, quant experiment, Compose, migration, and contract-test
changes. This document follows main for current architecture; runtime health
remains unverified.
3. **Governance integration:** QA/Risk engines and APIs are present, but active
   policy corpus, credentials, production evidence, and continuous runtime
   records are environment-dependent and not verifiable from this checkout.
4. **Benchmark reproducibility:** FP8/AWQ quality result artifacts are tracked
   on `origin/main`; the requested VRAM/KV/throughput/restart measurements are
   not tracked and must not be inferred from quality scores.
5. **Orchestration measurement:** direct/fast-path code exists, but the current
   checkout has no reliable measurement proving whether delegate initial ACK,
   GPU inference, or another step is the dominant latency bottleneck.

## 14. Current Implementation Status

| Area | Status | Evidence |
|---|---|---|
| 8 department profiles and current worker registry | IMPLEMENTED / TRACKED_MAIN | `departments/*/hermes/config.yaml`, `employee_workers.py`, `tests/test_worker_architecture.py`; 8 heads, 10 LLM-capable workers, 5 deterministic runners |
| worker context and department handoff contracts | IMPLEMENTED | `docs/02-engineering/contracts/worker-context.v1.json`, `department-handoff.v1.json` |
| deterministic trading contracts/OMS/paper broker | IMPLEMENTED / PARTIAL | `departments/02-trading/contracts`, `oms`, `broker`, paper-loop tests |
| deterministic Risk Engine and fail-closed state | IMPLEMENTED / PARTIAL | `departments/03-risk/engine`, `harness`, Risk tests |
| PIT quant dataset and strategy lifecycle | IMPLEMENTED / PARTIAL | `departments/04-quant-backtest/pipeline/pit_dataset.py`, `strategy_lifecycle.py` |
| deterministic accounting ledger/reconciliation/reporting | IMPLEMENTED / PARTIAL | `departments/05-accounting-portfolio`, accounting close-loop tests |
| QA evidence/model-risk/eval runners | IMPLEMENTED / PARTIAL | `departments/06-ai-qa-audit/evidence`, `model_risk.py`, `eval_runner.py` |
| forward-QA dispatch and lease-fenced reproduction | IMPLEMENTED / PARTIAL / TRACKED_MAIN | `departments/06-ai-qa-audit/qa_events/worker.py`, `reproduction_worker.py`, Compose service, forward-QA migrations and tests; runtime health not verified |
| intraday quant experiment/forward-confirmation gates | IMPLEMENTED / PARTIAL / TRACKED_MAIN | `departments/04-quant-backtest/pipeline/intraday_experiment_runner.py`, `intraday_trial_ledger.py`, `intraday_candidate.py`, release/publish tests; operational data not verified |
| external AWS AWQ serving checkpoint | TRACKED_MAIN; RUNTIME_VERIFIED unavailable | `origin/main` commit `b3fb8c5` and its Compose/gateway/registry changes; live AWS health is not proven |
| FP8/AWQ quality result table | TRACKED_MAIN | immutable result files under `benchmarks/quantization/results/`; no external runtime observation is claimed |
| VRAM/KV/throughput infrastructure table | NOT VERIFIED | requested memory/throughput/restart values are not present in tracked result artifacts or inspected history |
| reusable Common + Department QLoRA pipeline | PLANNED | no training notebook/script/validator found |
| continuously operated end-to-end production runtime | PARTIAL / NOT RUNTIME-VERIFIED | historical status records and code exist; current runtime evidence absent |

For status semantics, retain the repository’s distinction: `IMPLEMENTED` means
code/contract exists, `TEST_VERIFIED` means a current deterministic test was
run, and `RUNTIME_VERIFIED` requires an actual API/DB/process observation.

## 15. Next Milestones

1. Reconcile the tracked model-plane source of truth with the actual AWS
   runtime: record the AWQ model digest, served name, 8192/0.85 settings, LoRA
   limits, startup/restart behavior, and VRAM from the target environment.
2. Add immutable, hashed infrastructure benchmark manifests for FP8, AWQ,
   and AWQ+LoRA. Keep External-50, Internal-50 v1, and Internal-50 v2 held out
   from adapter training.
3. Establish the Common + Department QLoRA pipeline on the original Qwen
   lineage, with dataset validation/dedup, train/validation split, adapter
   manifest, and promotion rollback evidence.
4. Close the canonical `case_id`/`trace_id` path from research packet through
   RiskDecision, paper fill, journal, NAV/PnL, QA decision, and CEO projection.
5. Measure the request path end to end, including direct/delegate routing,
   initial ACK, worker queueing, TTFT, E2E, and post-hoc versus blocking QA.
6. Replace historical status tables with dated, reproducible runtime evidence;
   preserve historical snapshots separately rather than presenting them as
   current state.

## 16. Documentation Ownership

| Path | Role | Audit classification | Overlap/authority |
|---|---|---|---|
| `docs/README.md` | documentation portal | CURRENT | links only; does not own detailed current-state facts |
| `docs/CURRENT_PROJECT_ARCHITECTURE.md` | canonical current architecture | CURRENT / TRACKED_MAIN-aware | owns current architecture summary and source audit |
| `docs/PROJECT_IMPLEMENTATION_STATUS.md` | implementation/readiness board | CURRENT + HISTORICAL snapshots | owns status vocabulary and dated evidence; links here for architecture |
| `docs/02-engineering/FINAL_RUNTIME_ARCHITECTURE.md` | detailed runtime contracts | CURRENT detail / PARTIAL implementation | owns execution boundaries, retries, adapters, and gate topology |
| `docs/02-engineering/RISK_QA_DOCKER_RUNBOOK.md` | Risk/QA container and preflight procedure | DEPARTMENT RUNTIME DOC / TRACKED_MAIN | operational runbook; does not replace this architecture or status board |
| `docs/02-engineering/WORKER_ROLE_BOUNDARIES.md` | worker permissions and roles | CURRENT reference | owns detailed role/authority matrix |
| `docs/HEDGE_FUND_MASTER_PLAN.md` | target state and long-term plan | TARGET STATE / HISTORICAL snapshots | does not override current implementation evidence |
| `docs/02-engineering/CEO_CONVERSATIONAL_ROUTING_SPEC.md` | routing design and implementation notes | PARTIAL | department-local routing detail; current topology is cross-checked here |
| `docs/02-engineering/WORKER_MODEL_MATRIX.md` | model assignment target/history | HISTORICAL / TARGET | does not override main registry or serving config |
| `docs/02-engineering/RESEARCH_WORKER_AWS_RUNBOOK.md` | Research worker AWS procedure | DEPARTMENT DOC / not edited in this audit | ownership-sensitive; current model source remains Compose/gateway/registry |
| `docs/02-engineering/SYSTEM_WIRING_MAP.md` | dated wiring snapshot | HISTORICAL / PARTIAL | useful audit snapshot; not a live topology source |
| `docs/06-integrations/*` and generated provider references | provider/API reference | INTEGRATION REFERENCE | excluded from architecture consolidation |

No document was deleted in this audit. Potential archival/removal candidates
require a separate review because some contain historical rationale or dated
runtime evidence.

## 17. Evidence Index

- `CLAUDE.md`
- `docs/PROJECT_IMPLEMENTATION_STATUS.md`
- `docs/02-engineering/WORKER_ROLE_BOUNDARIES.md`
- `docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md`
- `docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md`
- `docs/02-engineering/FINAL_RUNTIME_ARCHITECTURE.md`
- `docs/02-engineering/SYSTEM_WIRING_MAP.md`
- `departments/*/hermes/config.yaml`
- `departments/*/employee_workers.py` and `departments/03-risk/risk_employee_workers.py`
- `departments/06-ai-qa-audit/qa_employee_workers.py`
- `departments/06-ai-qa-audit/qa_events/worker.py`
- `departments/06-ai-qa-audit/qa_events/reproduction_worker.py`
- `departments/02-trading/contracts/contracts.py`
- `departments/03-risk/engine/risk_engine.py`
- `departments/04-quant-backtest/pipeline/pit_dataset.py`
- `departments/04-quant-backtest/pipeline/strategy_lifecycle.py`
- `departments/04-quant-backtest/pipeline/intraday_experiment_runner.py`
- `departments/04-quant-backtest/pipeline/intraday_trial_ledger.py`
- `departments/04-quant-backtest/pipeline/intraday_candidate.py`
- `departments/05-accounting-portfolio/ledger/ledger.py`
- `departments/06-ai-qa-audit/evidence/evidence_qa_engine.py`
- `departments/06-ai-qa-audit/model_risk.py`
- `departments/06-ai-qa-audit/eval_runner.py`
- `orchestration/workflows/runner.py`
- `orchestration/adapters/ceo_supervisor.py`
- `docker-compose.model.yml`
- `docker-compose.yml` (`qa-reproduction-worker`, role-scoped QA/Quant services)
- `departments/worker_model_gateway.py`
- `departments/01-research/config/worker_model_registry.json`
- `scripts/model_plane/fetch_base_model.sh`
- `scripts/model_plane/quantize_fp8.py`
- `tests/test_worker_architecture.py`
