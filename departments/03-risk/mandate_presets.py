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
    mindset: str
    experience: str
    max_instrument_weight: Decimal
    max_sector_weight: Decimal
    max_gross_exposure: Decimal
    max_concurrent_positions: int


def _preset(
    mindset: str,
    experience: str,
    instrument: str,
    sector: str,
    gross: str,
    positions: int,
) -> RiskPreset:
    return RiskPreset(
        mindset=mindset,
        experience=experience,
        max_instrument_weight=Decimal(instrument),
        max_sector_weight=Decimal(sector),
        max_gross_exposure=Decimal(gross),
        max_concurrent_positions=positions,
    )


# 3 x 3 matrix. Beginner + risk seeking is intentionally bounded by the
# beginner ceiling because effective risk score is min(mindset, experience).
RISK_PRESETS: Mapping[tuple[str, str], RiskPreset] = {
    ("BEGINNER", "SAFETY_FIRST"): _preset(
        "SAFETY_FIRST", "BEGINNER", "0.05", "0.20", "1.00", 8
    ),
    ("BEGINNER", "BALANCED"): _preset(
        "BALANCED", "BEGINNER", "0.05", "0.20", "1.00", 10
    ),
    ("BEGINNER", "RISK_SEEKING"): _preset(
        "RISK_SEEKING", "BEGINNER", "0.05", "0.20", "1.00", 8
    ),
    ("INTERMEDIATE", "SAFETY_FIRST"): _preset(
        "SAFETY_FIRST", "INTERMEDIATE", "0.10", "0.25", "1.00", 12
    ),
    ("INTERMEDIATE", "BALANCED"): _preset(
        "BALANCED", "INTERMEDIATE", "0.10", "0.30", "1.00", 15
    ),
    ("INTERMEDIATE", "RISK_SEEKING"): _preset(
        "RISK_SEEKING", "INTERMEDIATE", "0.10", "0.35", "1.00", 12
    ),
    ("EXPERIENCED", "SAFETY_FIRST"): _preset(
        "SAFETY_FIRST", "EXPERIENCED", "0.15", "0.30", "1.00", 15
    ),
    ("EXPERIENCED", "BALANCED"): _preset(
        "BALANCED", "EXPERIENCED", "0.15", "0.40", "1.00", 20
    ),
    ("EXPERIENCED", "RISK_SEEKING"): _preset(
        "RISK_SEEKING", "EXPERIENCED", "0.20", "0.50", "1.00", 25
    ),
}


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
    if violations:
        return PresetAlignment.REQUIRES_RISK_REVIEW, tuple(violations)

    tighter = (
        max_instrument_weight < preset.max_instrument_weight
        or max_sector_weight < preset.max_sector_weight
        or max_gross_exposure < preset.max_gross_exposure
        or max_concurrent_positions < preset.max_concurrent_positions
    )
    return (
        PresetAlignment.TIGHTER if tighter else PresetAlignment.MATCHED,
        (),
    )


__all__ = [
    "RISK_PRESETS",
    "PresetAlignment",
    "RiskPreset",
    "resolve_risk_preset",
    "validate_preset_alignment",
]
