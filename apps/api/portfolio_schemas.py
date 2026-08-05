"""Stable, client-facing DTOs for the advisory portfolio BFF.

The runtime projection is intentionally process-local and contains evolving
worker/event details.  The BFF envelope and the result core are strict so
clients cannot silently depend on accidental fields.  Additive changes must
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
    suitability: dict[str, Any] = Field(default_factory=dict)
    task_plan: dict[str, Any] = Field(default_factory=dict)
    universe_id: str | None = None
    universe: PortfolioUniverseProjection | None = None
    instrument_recommendations: list[PortfolioInstrumentRecommendation] = Field(
        default_factory=list
    )
    instrument_recommendations_status: str = "UNAVAILABLE"
    unresolved_asset_classes: list[str] = Field(default_factory=list)
    asset_visibility: PortfolioAssetVisibility | None = None
    forecast_notice: str = ""
    risk_gate: dict[str, Any] = Field(default_factory=dict)
    qa_gate: dict[str, Any] = Field(default_factory=dict)
    degraded_departments: list[str] = Field(default_factory=list)
    worker_reports: list[dict[str, Any]] = Field(default_factory=list)
    department_reports: dict[str, Any] = Field(default_factory=dict)
    data_context: dict[str, Any] = Field(default_factory=dict)
    replay: dict[str, Any] = Field(default_factory=dict)
    pipeline_events: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_event_count: int = Field(default=0, ge=0)
    external_writes: bool = False
    user_approval: PortfolioApproval | None = None


class PortfolioRecommendationStatusResponse(_ApiModel):
    run_id: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    status: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    started_at: datetime | None = None
    updated_at: datetime
    profile_user_id: str = Field(min_length=1)
    active_workers: list[PortfolioActiveWorker] = Field(default_factory=list)
    departments: dict[str, PortfolioDepartmentRuntime] = Field(default_factory=dict)
    messages: list[PortfolioRuntimeMessage] = Field(default_factory=list)
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
