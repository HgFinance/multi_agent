# Database ERD

> 전체 145개 Table을 한 그림에 넣지 않고 Domain Root와 실제 투자·주문 흐름을 중심으로 표시한다.

## 1. Domain 관계

```mermaid
erDiagram
    ACCOUNTING_FUNDS ||--o{ ACCOUNTING_BOOKS : contains
    ACCOUNTING_FUNDS ||--o{ GOVERNANCE_MANDATES : governed_by
    GOVERNANCE_MANDATES ||--o{ GOVERNANCE_MANDATE_VERSIONS : versions
    ACCOUNTING_FUNDS ||--o{ GOVERNANCE_CASES : owns
    GOVERNANCE_CASES ||--o| GOVERNANCE_INVESTMENT_CASES : specializes
    GOVERNANCE_CASES ||--o{ GOVERNANCE_CASE_EVENTS : records

    REFERENCE_ISSUERS ||--o{ REFERENCE_INSTRUMENTS : issues
    REFERENCE_INSTRUMENTS ||--o{ REFERENCE_INSTRUMENT_SYMBOLS : aliases
    REFERENCE_INSTRUMENTS ||--o| REFERENCE_DERIVATIVE_CONTRACTS : describes

    RESEARCH_DOCUMENTS ||--o{ RESEARCH_DOCUMENT_VERSIONS : versions
    RESEARCH_DOCUMENT_VERSIONS ||--o{ RESEARCH_EVIDENCE_CHUNKS : chunks
    GOVERNANCE_CASES ||--o{ RESEARCH_RESEARCH_PACKETS : produces
    RESEARCH_RESEARCH_PACKETS ||--o{ RESEARCH_PACKET_EVIDENCE : cites
    RESEARCH_EVIDENCE_CHUNKS ||--o{ RESEARCH_PACKET_EVIDENCE : supports

    QUANT_DATASET_MANIFESTS ||--o{ QUANT_EXPERIMENTS : feeds
    QUANT_EXPERIMENTS ||--o{ QUANT_BACKTEST_RUNS : validates
    QUANT_EXPERIMENTS ||--o{ STRATEGY_CANDIDATES : proposes
    STRATEGY_STRATEGIES ||--o{ STRATEGY_VERSIONS : versions
    STRATEGY_VERSIONS ||--o{ STRATEGY_DEPLOYMENTS : deploys
```

## 2. 투자 판단부터 회계까지

```mermaid
erDiagram
    GOVERNANCE_CASES ||--|| EXECUTION_TRADE_CASES : anchors
    GOVERNANCE_CASES ||--o{ RESEARCH_AGENT_DECISIONS : receives
    RESEARCH_AGENT_DECISIONS ||--o{ STRATEGY_SIGNALS : informs
    STRATEGY_VERSIONS ||--o{ STRATEGY_SIGNALS : emits
    STRATEGY_SIGNALS ||--o{ STRATEGY_SIGNAL_TARGETS : targets

    EXECUTION_TRADE_CASES ||--o{ EXECUTION_INTENT_GROUPS : groups
    EXECUTION_INTENT_GROUPS ||--o{ EXECUTION_ORDER_INTENTS : contains
    EXECUTION_INTENT_GROUPS ||--|| RISK_RISK_REQUESTS : assessed_by
    RISK_RISK_REQUESTS ||--o{ RISK_RISK_REQUEST_ITEMS : contains
    RISK_RISK_REQUESTS ||--o{ RISK_RISK_DECISIONS : decides

    EXECUTION_ORDER_INTENTS ||--o{ EXECUTION_ORDERS : submits
    EXECUTION_ORDERS ||--o{ EXECUTION_ORDER_EVENTS : changes
    EXECUTION_ORDERS ||--o{ EXECUTION_FILLS : fills

    EXECUTION_FILLS ||--o{ ACCOUNTING_JOURNALS : posts
    ACCOUNTING_JOURNALS ||--o{ ACCOUNTING_JOURNAL_LINES : balances
    ACCOUNTING_JOURNALS ||--o{ ACCOUNTING_POSITIONS : projects
    ACCOUNTING_JOURNALS ||--o{ ACCOUNTING_CASH_BALANCES : projects
    ACCOUNTING_POSITIONS ||--o{ ACCOUNTING_VALUATIONS : values
    ACCOUNTING_VALUATIONS ||--o{ ACCOUNTING_NAV_COMPONENTS : composes
    ACCOUNTING_NAV_RUNS ||--o{ ACCOUNTING_NAV_COMPONENTS : contains
```

`execution.fills -> accounting.journals`는 직접 FK가 아니라 `journals.event_type + source_event_id`의 멱등 계약으로 연결한다. Broker/회계 Event는 재처리될 수 있으므로 물리 FK보다 Event Identity를 기준으로 한다.

## 3. Agent와 감사

```mermaid
erDiagram
    WORKFORCE_DEPARTMENTS ||--o{ WORKFORCE_AGENT_PROFILES : employs
    WORKFORCE_ROLE_TEMPLATES ||--o{ WORKFORCE_AGENT_PROFILES : defines
    WORKFORCE_AGENT_PROFILES ||--o{ WORKFORCE_AGENT_PROFILE_VERSIONS : versions
    WORKFORCE_MODELS ||--o{ WORKFORCE_AGENT_PROFILE_VERSIONS : runs
    WORKFORCE_AGENT_PROFILE_VERSIONS ||--o{ WORKFORCE_AGENT_SKILL_ASSIGNMENTS : equips
    WORKFORCE_SKILLS ||--o{ WORKFORCE_AGENT_SKILL_ASSIGNMENTS : assigned
    WORKFORCE_AGENT_PROFILE_VERSIONS ||--o{ WORKFORCE_AGENT_TOOL_PERMISSIONS : permits
    WORKFORCE_TOOLS ||--o{ WORKFORCE_AGENT_TOOL_PERMISSIONS : controls

    WORKFORCE_AGENT_PROFILE_VERSIONS ||--o{ AUDIT_AGENT_RUNS : executes
    AUDIT_AGENT_RUNS ||--o{ AUDIT_TOOL_CALLS : invokes
    AUDIT_ARTIFACT_VERSIONS ||--o{ AUDIT_ARTIFACT_LINEAGE : parent
    AUDIT_ARTIFACT_VERSIONS ||--o{ AUDIT_ARTIFACT_LINEAGE : child
    AUDIT_ARTIFACT_VERSIONS ||--o{ AUDIT_CLAIM_CHECKS : verifies
    AUDIT_EVAL_SETS ||--o{ AUDIT_EVAL_RUNS : evaluates
    AUDIT_EVAL_RUNS ||--o{ AUDIT_EVAL_RESULTS : measures
```

## 4. Cross-DB 연결

```mermaid
flowchart LR
    INST["Supabase reference.instruments.instrument_id"] --> TICK["Timescale market_ticks.instrument_id"]
    INST --> QUOTE["Timescale market_quotes.instrument_id"]
    INST --> DERIV["Timescale derivative_snapshots.instrument_id"]

    TICK --> SNAP["Supabase execution.market_snapshots"]
    QUOTE --> SNAP
    SNAP --> INTENT["execution.order_intents.market_snapshot_id"]

    TICK --> DATASET["Parquet + quant.dataset_manifests"]
    QUOTE --> DATASET
    DATASET --> EXP["quant.experiments"]
```

Cross-DB Pointer는 실제 Row를 복제하지 않는다. Supabase에는 주문·판단 당시의 작은 Evidence Snapshot과 Timescale/Parquet Source Reference만 고정한다.
