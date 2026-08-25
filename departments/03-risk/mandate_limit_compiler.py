"""Compile one approved Mandate version into canonical Risk policy limits.

The compiler is deterministic and side-effect free.  Persistence is handled by
the Risk repository/DB transaction so a failed compilation can never partially
activate a policy.  Unversioned or unapproved mandates fail closed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mandate_presets import PRESET_VERSION, PresetAlignment, validate_preset_alignment

COMPILER_VERSION = "mandate-limit-compiler.v1"
_POLICY_NAMESPACE = UUID("364df2d5-f40a-4e30-a198-00b1a24cb2af")


class HardMandateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_capital: Decimal = Field(gt=0)
    max_instrument_weight: Decimal = Field(gt=0, le=1)
    max_sector_weight: Decimal = Field(gt=0, le=1)
    max_gross_exposure: Decimal = Field(gt=0)
    max_concurrent_positions: int = Field(gt=0)
    max_daily_loss_pct: Decimal = Field(gt=0, le=1)
    max_drawdown_pct: Decimal = Field(gt=0, le=1)
    trade_risk_budget_pct: Decimal = Field(gt=0, le=1)
    min_order_notional: Decimal = Field(default=Decimal("0"), ge=0)
    max_order_notional: Decimal | None = Field(default=None, gt=0)
    allowed_instrument_ids: list[UUID] | None = None
    allowed_asset_classes: list[str] | None = None
    forbidden_asset_classes: list[str] = Field(default_factory=list)
    preferred_sectors: list[str] = Field(default_factory=list)
    excluded_sectors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def monotonic_limits(self) -> "HardMandateLimits":
        if self.max_instrument_weight > self.max_sector_weight:
            raise ValueError("max_instrument_weight must not exceed max_sector_weight")
        if self.max_sector_weight > self.max_gross_exposure:
            raise ValueError("max_sector_weight must not exceed max_gross_exposure")
        if self.max_daily_loss_pct > self.max_drawdown_pct:
            raise ValueError("max_daily_loss_pct must not exceed max_drawdown_pct")
        return self


class MandateLimitCompilationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: UUID
    mandate_id: UUID
    mandate_version_id: UUID | Literal["unversioned"]
    mandate_version: int | None = Field(default=None, ge=1)
    mandate_status: Literal["ACTIVE", "DRAFT", "SUSPENDED", "RETIRED"]
    approval_status: Literal["APPROVED", "PENDING", "REJECTED", "MISSING"]
    mindset: str
    experience: str
    limits: HardMandateLimits
    effective_from: datetime
    trace_id: str = Field(min_length=1, max_length=128)


class CompiledLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    soft_limit: Decimal | None = None
    hard_limit: Decimal
    unit: Literal["FRACTION", "KRW", "COUNT"]
    source_field: str


class MandateLimitCompilation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["risk.mandate-limit-compilation.v1"] = (
        "risk.mandate-limit-compilation.v1"
    )
    status: Literal["COMPILED", "REQUIRES_USER_REVIEW"]
    policy_id: UUID | None = None
    policy_code: str = "USER_MANDATE"
    policy_version: int | None = None
    fund_id: UUID
    mandate_id: UUID
    mandate_version_id: str
    mindset: str
    experience: str
    preset_version: str = PRESET_VERSION
    compiler_version: str = COMPILER_VERSION
    alignment: PresetAlignment | None = None
    reason_codes: list[str] = Field(default_factory=list)
    limits: list[CompiledLimit] = Field(default_factory=list)
    effective_from: datetime
    input_hash: str
    content_hash: str | None = None
    policy_scope: dict[str, Any] = Field(default_factory=dict)
    policy_rules: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_jsonable(child) for child in value]
    return value


def _hash(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compile_mandate_limits(
    request: MandateLimitCompilationRequest | dict[str, Any],
) -> MandateLimitCompilation:
    item = (
        request
        if isinstance(request, MandateLimitCompilationRequest)
        else MandateLimitCompilationRequest.model_validate(request)
    )
    input_hash = _hash(item)
    common = dict(
        fund_id=item.fund_id,
        mandate_id=item.mandate_id,
        mandate_version_id=str(item.mandate_version_id),
        mindset=item.mindset,
        experience=item.experience,
        effective_from=item.effective_from,
        input_hash=input_hash,
        trace_id=item.trace_id,
    )
    if (
        item.mandate_version_id == "unversioned"
        or item.mandate_version is None
        or item.mandate_status != "ACTIVE"
        or item.approval_status != "APPROVED"
    ):
        reasons = []
        if item.mandate_version_id == "unversioned" or item.mandate_version is None:
            reasons.append("UNVERSIONED_MANDATE")
        if item.mandate_status != "ACTIVE":
            reasons.append("MANDATE_NOT_ACTIVE")
        if item.approval_status != "APPROVED":
            reasons.append("MANDATE_NOT_USER_APPROVED")
        return MandateLimitCompilation(
            status="REQUIRES_USER_REVIEW", reason_codes=reasons, **common
        )

    alignment, violations = validate_preset_alignment(
        mindset=item.mindset,
        experience=item.experience,
        max_instrument_weight=item.limits.max_instrument_weight,
        max_sector_weight=item.limits.max_sector_weight,
        max_gross_exposure=item.limits.max_gross_exposure,
        max_concurrent_positions=item.limits.max_concurrent_positions,
        max_daily_loss_pct=item.limits.max_daily_loss_pct,
        max_drawdown_pct=item.limits.max_drawdown_pct,
        trade_risk_budget_pct=item.limits.trade_risk_budget_pct,
    )
    if alignment == PresetAlignment.REQUIRES_RISK_REVIEW:
        return MandateLimitCompilation(
            status="REQUIRES_USER_REVIEW",
            alignment=alignment,
            reason_codes=[f"PRESET_LIMIT_EXCEEDED:{name}" for name in violations],
            **common,
        )

    limits = item.limits
    daily_loss_amount = (limits.base_capital * limits.max_daily_loss_pct).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    turnover = (limits.base_capital * limits.max_gross_exposure).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    compiled = [
        CompiledLimit(
            metric="single_issuer_pct",
            soft_limit=(limits.max_instrument_weight * Decimal("0.8")),
            hard_limit=limits.max_instrument_weight,
            unit="FRACTION",
            source_field="max_instrument_weight",
        ),
        CompiledLimit(metric="sector_weight", hard_limit=limits.max_sector_weight, unit="FRACTION", source_field="max_sector_weight"),
        CompiledLimit(metric="gross_exposure", hard_limit=limits.max_gross_exposure, unit="FRACTION", source_field="max_gross_exposure"),
        CompiledLimit(metric="concurrent_positions", hard_limit=Decimal(limits.max_concurrent_positions), unit="COUNT", source_field="max_concurrent_positions"),
        CompiledLimit(metric="daily_loss", hard_limit=daily_loss_amount, unit="KRW", source_field="base_capital*max_daily_loss_pct"),
        CompiledLimit(metric="drawdown_pct", hard_limit=limits.max_drawdown_pct, unit="FRACTION", source_field="max_drawdown_pct"),
        CompiledLimit(metric="trade_risk_budget_pct", hard_limit=limits.trade_risk_budget_pct, unit="FRACTION", source_field="trade_risk_budget_pct"),
        CompiledLimit(metric="daily_turnover_notional", hard_limit=turnover, unit="KRW", source_field="base_capital*max_gross_exposure"),
        CompiledLimit(metric="daily_order_count", hard_limit=Decimal(limits.max_concurrent_positions * 4), unit="COUNT", source_field="max_concurrent_positions*4"),
    ]
    policy_id = uuid5(
        _POLICY_NAMESPACE,
        f"{item.fund_id}:{item.mandate_id}:{item.mandate_version_id}",
    )
    content_hash = _hash(
        {
            "policy_id": policy_id,
            "mandate_version_id": item.mandate_version_id,
            "limits": compiled,
            "compiler_version": COMPILER_VERSION,
        }
    )
    max_order_notional = limits.max_order_notional or (
        limits.base_capital * limits.max_instrument_weight
    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    policy_scope = {
        "fund_id": str(item.fund_id),
        "mandate_id": str(item.mandate_id),
        "mandate_version_id": str(item.mandate_version_id),
        "preset_version": PRESET_VERSION,
        "allowed_instrument_ids": (
            [str(value) for value in limits.allowed_instrument_ids]
            if limits.allowed_instrument_ids is not None
            else None
        ),
    }
    policy_rules = {
        "min_order_notional": str(limits.min_order_notional),
        "max_order_notional": str(max_order_notional),
        "allowed_instrument_ids": policy_scope["allowed_instrument_ids"],
        "allowed_asset_classes": limits.allowed_asset_classes,
        "forbidden_asset_classes": limits.forbidden_asset_classes,
        "preferred_sectors": limits.preferred_sectors,
        "excluded_sectors": limits.excluded_sectors,
        "risk_bounds": {
            "max_instrument_weight": str(limits.max_instrument_weight),
            "max_sector_weight": str(limits.max_sector_weight),
            "max_gross_exposure": str(limits.max_gross_exposure),
            "max_concurrent_positions": limits.max_concurrent_positions,
            "max_daily_loss_pct": str(limits.max_daily_loss_pct),
            "max_drawdown_pct": str(limits.max_drawdown_pct),
            "trade_risk_budget_pct": str(limits.trade_risk_budget_pct),
        },
    }
    return MandateLimitCompilation(
        status="COMPILED",
        policy_id=policy_id,
        policy_version=item.mandate_version,
        alignment=alignment,
        limits=compiled,
        content_hash=content_hash,
        policy_scope=policy_scope,
        policy_rules=policy_rules,
        **common,
    )


__all__ = [
    "COMPILER_VERSION",
    "CompiledLimit",
    "HardMandateLimits",
    "MandateLimitCompilation",
    "MandateLimitCompilationRequest",
    "compile_mandate_limits",
]
