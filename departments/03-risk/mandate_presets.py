"""Risk-owned deterministic presets and mandate-limit alignment checks.

The UI may prefill these values, but Risk remains the authoritative validator.
Stricter user limits are accepted. Looser-than-preset limits are never silently
relaxed; they require explicit Risk review.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PresetAlignment(StrEnum):
    MATCHED = "MATCHED"
    TIGHTER = "TIGHTER"
    REQUIRES_RISK_REVIEW = "REQUIRES_RISK_REVIEW"


@dataclass(frozen=True)
class RiskPreset:
    preset_version: str
    mindset: str
    experience: str
    max_instrument_weight: Decimal
    max_sector_weight: Decimal
    max_gross_exposure: Decimal
    max_concurrent_positions: int
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    trade_risk_budget_min_pct: Decimal
    trade_risk_budget_max_pct: Decimal

    def as_dict(self) -> dict[str, str | int]:
        return {
            "preset_version": self.preset_version,
            "mindset": self.mindset,
            "experience": self.experience,
            "max_instrument_weight": str(self.max_instrument_weight),
            "max_sector_weight": str(self.max_sector_weight),
            "max_gross_exposure": str(self.max_gross_exposure),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_daily_loss_pct": str(self.max_daily_loss_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "trade_risk_budget_min_pct": str(self.trade_risk_budget_min_pct),
            "trade_risk_budget_max_pct": str(self.trade_risk_budget_max_pct),
        }


PRESET_VERSION = "risk-mandate-presets.2026-08-25.v1"


def _preset(
    mindset: str,
    experience: str,
    instrument: str,
    sector: str,
    gross: str,
    positions: int,
    daily_loss: str,
    drawdown: str,
    trade_risk_min: str,
    trade_risk_max: str,
) -> RiskPreset:
    return RiskPreset(
        preset_version=PRESET_VERSION,
        mindset=mindset,
        experience=experience,
        max_instrument_weight=Decimal(instrument),
        max_sector_weight=Decimal(sector),
        max_gross_exposure=Decimal(gross),
        max_concurrent_positions=positions,
        max_daily_loss_pct=Decimal(daily_loss),
        max_drawdown_pct=Decimal(drawdown),
        trade_risk_budget_min_pct=Decimal(trade_risk_min),
        trade_risk_budget_max_pct=Decimal(trade_risk_max),
    )


# 3 x 3 matrix. Beginner + risk seeking is intentionally bounded by the
# beginner ceiling because effective risk score is min(mindset, experience).
_BY_EFFECTIVE_SCORE = {
    1: ("0.10", "0.25", "1.00", 5, "0.02", "0.15", "0.0025", "0.0050"),
    2: ("0.15", "0.35", "1.50", 8, "0.03", "0.20", "0.0050", "0.0100"),
    3: ("0.25", "0.50", "2.50", 12, "0.05", "0.35", "0.0100", "0.0200"),
}
_MINDSET_SCORE = {"SAFETY_FIRST": 1, "BALANCED": 2, "RISK_SEEKING": 3}
_EXPERIENCE_SCORE = {"BEGINNER": 1, "INTERMEDIATE": 2, "EXPERIENCED": 3}


def _build_presets() -> Mapping[tuple[str, str], RiskPreset]:
    presets: dict[tuple[str, str], RiskPreset] = {}
    for experience, experience_score in _EXPERIENCE_SCORE.items():
        for mindset, mindset_score in _MINDSET_SCORE.items():
            values = _BY_EFFECTIVE_SCORE[min(experience_score, mindset_score)]
            presets[(experience, mindset)] = _preset(
                mindset, experience, *values
            )
    return presets


RISK_PRESETS: Mapping[tuple[str, str], RiskPreset] = _build_presets()


def resolve_risk_preset(mindset: str, experience: str) -> RiskPreset:
    try:
        return RISK_PRESETS[(str(experience).upper(), str(mindset).upper())]
    except KeyError as exc:
        raise ValueError(
            f"unsupported mindset/experience pair: {mindset}/{experience}"
        ) from exc


def validate_preset_alignment(
    *,
    mindset: str,
    experience: str,
    max_instrument_weight: Decimal,
    max_sector_weight: Decimal,
    max_gross_exposure: Decimal,
    max_concurrent_positions: int,
    max_daily_loss_pct: Decimal | None = None,
    max_drawdown_pct: Decimal | None = None,
    trade_risk_budget_pct: Decimal | None = None,
) -> tuple[PresetAlignment, tuple[str, ...]]:
    """Classify a mandate without changing any user value."""

    preset = resolve_risk_preset(mindset, experience)
    violations: list[str] = []
    if max_instrument_weight > preset.max_instrument_weight:
        violations.append("max_instrument_weight")
    if max_sector_weight > preset.max_sector_weight:
        violations.append("max_sector_weight")
    if max_gross_exposure > preset.max_gross_exposure:
        violations.append("max_gross_exposure")
    if max_concurrent_positions > preset.max_concurrent_positions:
        violations.append("max_concurrent_positions")
    if max_daily_loss_pct is not None and max_daily_loss_pct > preset.max_daily_loss_pct:
        violations.append("max_daily_loss_pct")
    if max_drawdown_pct is not None and max_drawdown_pct > preset.max_drawdown_pct:
        violations.append("max_drawdown_pct")
    if (
        trade_risk_budget_pct is not None
        and trade_risk_budget_pct > preset.trade_risk_budget_max_pct
    ):
        violations.append("trade_risk_budget_pct")
    if violations:
        return PresetAlignment.REQUIRES_RISK_REVIEW, tuple(violations)

    tighter = (
        max_instrument_weight < preset.max_instrument_weight
        or max_sector_weight < preset.max_sector_weight
        or max_gross_exposure < preset.max_gross_exposure
        or max_concurrent_positions < preset.max_concurrent_positions
        or (
            max_daily_loss_pct is not None
            and max_daily_loss_pct < preset.max_daily_loss_pct
        )
        or (
            max_drawdown_pct is not None
            and max_drawdown_pct < preset.max_drawdown_pct
        )
        or (
            trade_risk_budget_pct is not None
            and trade_risk_budget_pct < preset.trade_risk_budget_max_pct
        )
    )
    return (
        PresetAlignment.TIGHTER if tighter else PresetAlignment.MATCHED,
        (),
    )


__all__ = [
    "PRESET_VERSION",
    "RISK_PRESETS",
    "PresetAlignment",
    "RiskPreset",
    "resolve_risk_preset",
    "validate_preset_alignment",
]
