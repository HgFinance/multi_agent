"""Stable, client-facing DTOs for the advisory portfolio BFF.

The runtime projection is durably persisted but still contains evolving
worker/event details. The BFF envelope and the result core are strict so
clients cannot silently depend on accidental fields. Additive changes must
update this module, OpenAPI and its contract tests together.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortfolioRecommendationStartResponse(_ApiModel):
    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    idempotent_replay: bool = False
    trace_id: str = ""
    case_id: str | None = None
    mandate_id: str | None = None
    mandate_version_id: str | None = None
    policy_hash: str | None = None
    input_hash: str = Field(min_length=1)


class PortfolioDepartmentRuntime(_ApiModel):
    department_code: str = Field(min_length=1)
    status: str = Field(min_length=1)
    current_stage: str | None = None
    active_worker_ids: list[str] = Field(default_factory=list)
    last_message: str | None = None
    updated_at: datetime


class PortfolioRuntimeMessage(_ApiModel):
    id: str = Field(min_length=1)
    occurred_at: datetime
    kind: str = Field(min_length=1)
    department_code: str | None = None
    worker_id: str | None = None
    text: str = Field(min_length=1)


class PortfolioActiveWorker(_ApiModel):
    worker_id: str = Field(min_length=1)
    department_code: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    role: str = Field(min_length=1)
    status: str = Field(min_length=1)
    started_at: datetime
    summary: str | None = None


class PortfolioHandoff(_ApiModel):
    from_department: str = Field(min_length=1)
    to_department: str = Field(min_length=1)
    from_head: str = Field(min_length=1)
    to_head: str = Field(min_length=1)
    status: str = Field(min_length=1)
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    occurred_at: datetime
    expires_at: float


class PortfolioApproval(_ApiModel):
    status: str = Field(min_length=1)
    binding: bool
    approved_at: datetime | None = None
    comment: str | None = None

class PortfolioRiskGate(_ApiModel):
    status: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    safe_action: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    data_quality: str = Field(min_length=1)
    binding: bool


class PortfolioQaGate(_ApiModel):
    status: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    binding: bool


class PortfolioDepartmentReport(_ApiModel):
    status: str = Field(min_length=1)
    legacy_status: str | None = None
    worker_ids: list[str] = Field(default_factory=list)
    executed: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    skipped_safe: int = Field(default=0, ge=0)
    not_requested: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    skip_reason: str | None = None
    failed: list[str] = Field(default_factory=list)
    binding: bool = False
    fan_out: bool = False
    fan_in: bool = False


class PortfolioWorkerReport(_ApiModel):
    stage: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    role: str | None = None
    technology: dict[str, object] = Field(default_factory=dict)
    status: str = Field(min_length=1)
    skip_reason: str | None = None
    execution_reason: str | None = None
    attempts: int = Field(default=0, ge=0)
    output: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    output_contract: str | None = None
    input_hash: str | None = None
    binding: bool = False
    contract_validation: dict[str, object] = Field(default_factory=dict)
    performance: dict[str, object] = Field(default_factory=dict)


class PortfolioDataContext(_ApiModel):
    source: str | None = None
    as_of: str | None = None
    quality_status: str | None = None
    reasons: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    read_only: bool = True
    external_writes: bool = False
    preflight: dict[str, object] = Field(default_factory=dict)
    research: dict[str, object] = Field(default_factory=dict)
    market: dict[str, object] = Field(default_factory=dict)
    accounting: dict[str, object] = Field(default_factory=dict)
    data_diagnostics: dict[str, object] = Field(default_factory=dict)
    candidates: list[dict[str, object]] = Field(default_factory=list)


class PortfolioPipelineEvent(_ApiModel):
    schema_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    department: str = Field(min_length=1)
    worker_id: str | None = None
    status: str = Field(min_length=1)
    input_hash: str | None = None
    output_contract: str | None = None
    retry_count: int = Field(default=0, ge=0)
    safe_action: str | None = None
    occurred_at: datetime
    summary: str = ""
    payload_hash: str | None = None
    handoff: dict[str, object] | None = None


class PortfolioReplayMetadata(_ApiModel):
    schema_id: str = Field(min_length=1)
    input_hash: str = Field(min_length=1)
    output_hash: str = Field(min_length=1)
    replayable: bool
    replay_scope: str = Field(min_length=1)
    excludes: list[str] = Field(default_factory=list)


class PortfolioRecommendationCandidate(_ApiModel):
    portfolio_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    risk_band: str = Field(min_length=1)
    fit_score: int = Field(ge=0, le=100)
    target_allocations: dict[str, str] = Field(default_factory=dict)
    target_amounts: dict[str, str] = Field(default_factory=dict)
    reasons: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    instrument_recommendations: list[PortfolioInstrumentRecommendation] = Field(default_factory=list)


class PortfolioExclusion(_ApiModel):
    portfolio_id: str = Field(min_length=1)
    reasons: list[str] = Field(min_length=1)


class PortfolioSuitability(_ApiModel):
    status: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    input_hash: str = Field(min_length=1)
    profile_user_id: str = Field(min_length=1)
    effective_risk_band: str = Field(min_length=1)
    investment_amount: str
    currency: str = Field(min_length=3, max_length=3)
    recommendations: list[PortfolioRecommendationCandidate] = Field(default_factory=list)
    exclusions: list[PortfolioExclusion] = Field(default_factory=list)
    instrument_recommendations: list[PortfolioInstrumentRecommendation] = Field(default_factory=list)
    manual_review_required: bool = True


class PortfolioTaskPlan(_ApiModel):
    mode: str = Field(min_length=1)
    category: str = Field(min_length=1)
    original_query: str = ""
    rewritten_query: str = Field(min_length=1)
    requested_departments: list[str] = Field(default_factory=list)
    routing_basis: str = Field(min_length=1)
    matched_terms: dict[str, list[str]] = Field(default_factory=dict)


class PortfolioInstrumentRecommendation(_ApiModel):
    portfolio_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    name: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    target_weight: str
    target_amount: str
    expected_return: str | float | None = None
    expected_return_status: str = Field(min_length=1)
    expected_return_basis: str = Field(min_length=1)
    data_status: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

PortfolioRecommendationCandidate.model_rebuild()
PortfolioSuitability.model_rebuild()


class PortfolioAssetVisibility(_ApiModel):
    include_stock: bool
    include_derivatives: bool
    bond_data_excluded: bool


class PortfolioUniverseProjection(_ApiModel):
    universe_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    status: str = Field(min_length=1)
    source: str | None = None


class PortfolioRecommendationResult(BaseModel):
    """Strict, typed result body for the portfolio recommendation client."""

    model_config = ConfigDict(extra="forbid")

    pipeline_status: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    case_id: str | None = None
    user_query: str = ""
    safe_action: str = Field(min_length=1)
    binding: bool = False
    production_enabled: bool = False
    manual_review_required: bool = True
    suitability: PortfolioSuitability | None = None
    task_plan: PortfolioTaskPlan | None = None
    universe_id: str | None = None
    universe: PortfolioUniverseProjection | None = None
    instrument_recommendations: list[PortfolioInstrumentRecommendation] = Field(
        default_factory=list
    )
    instrument_recommendations_status: str = "UNAVAILABLE"
    unresolved_asset_classes: list[str] = Field(default_factory=list)
    asset_visibility: PortfolioAssetVisibility | None = None
    forecast_notice: str = ""
    risk_gate: PortfolioRiskGate | None = None
    qa_gate: PortfolioQaGate | None = None
    degraded_departments: list[str] = Field(default_factory=list)
    worker_reports: list[PortfolioWorkerReport] = Field(default_factory=list)
    department_reports: dict[str, PortfolioDepartmentReport] = Field(default_factory=dict)
    data_context: PortfolioDataContext = Field(default_factory=PortfolioDataContext)
    replay: PortfolioReplayMetadata | None = None
    pipeline_events: list[PortfolioPipelineEvent] = Field(default_factory=list)
    pipeline_event_count: int = Field(default=0, ge=0)
    external_writes: bool = False
    user_approval: PortfolioApproval | None = None


class PortfolioRecommendationStatusResponse(_ApiModel):
    run_id: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    status: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    trace_id: str = ""
    case_id: str | None = None
    mandate_id: str | None = None
    mandate_version_id: str | None = None
    policy_hash: str | None = None
    input_hash: str = Field(min_length=1)
    started_at: datetime | None = None
    updated_at: datetime
    profile_user_id: str = Field(min_length=1)
    active_workers: list[PortfolioActiveWorker] = Field(default_factory=list)
    departments: dict[str, PortfolioDepartmentRuntime] = Field(default_factory=dict)
    messages: list[PortfolioRuntimeMessage] = Field(default_factory=list)
    performance_metrics: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_events: list[dict[str, Any]] = Field(default_factory=list)
    active_handoff: PortfolioHandoff | None = None
    result: PortfolioRecommendationResult | None = None
    approval: PortfolioApproval | None = None
    error: str | None = None


class PortfolioUniverseOption(_ApiModel):
    universe_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    status: str = Field(min_length=1)
    source: str | None = None
    instrument_count: int = Field(ge=0)


class PortfolioUniverseListResponse(_ApiModel):
    default_universe_id: str = Field(min_length=1)
    universes: list[PortfolioUniverseOption] = Field(default_factory=list)


__all__ = [
    "PortfolioRecommendationStartResponse",
    "PortfolioRecommendationStatusResponse",
    "PortfolioUniverseListResponse",
]
