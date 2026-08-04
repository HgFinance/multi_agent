"""Deterministic investor-profile to portfolio suitability matching.

이 모듈은 사용자에게 보여줄 포트폴리오 후보 목록만 만든다. 주문, Risk 승인,
Position 변경은 소유하지 않는다. LLM이 성향을 추론하거나 후보를 생성하지
않으며, 입력된 프로필과 등록된 포트폴리오 메타데이터만 결정론적으로 비교한다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


CALCULATION_VERSION = "portfolio-suitability-v1"


class InvestmentMindset(StrEnum):
    """사용자가 설문으로 선택한 투자 성향."""

    SAFETY_FIRST = "SAFETY_FIRST"
    BALANCED = "BALANCED"
    RISK_SEEKING = "RISK_SEEKING"


class ExperienceLevel(StrEnum):
    """투자 상품과 시장을 접해본 정도."""

    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    EXPERIENCED = "EXPERIENCED"


class LiquidityNeed(StrEnum):
    """사용자가 자금을 현금화해야 하는 긴급도."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PortfolioRiskBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SuitabilityStatus(StrEnum):
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"


_MINDSET_SCORE = {
    InvestmentMindset.SAFETY_FIRST: 1,
    InvestmentMindset.BALANCED: 2,
    InvestmentMindset.RISK_SEEKING: 3,
}
_EXPERIENCE_SCORE = {
    ExperienceLevel.BEGINNER: 1,
    ExperienceLevel.INTERMEDIATE: 2,
    ExperienceLevel.EXPERIENCED: 3,
}
_RISK_SCORE = {
    PortfolioRiskBand.LOW: 1,
    PortfolioRiskBand.MEDIUM: 2,
    PortfolioRiskBand.HIGH: 3,
}
_MAX_LIQUIDITY_DAYS = {
    LiquidityNeed.HIGH: 7,
    LiquidityNeed.MEDIUM: 30,
    LiquidityNeed.LOW: 365,
}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


class InvestorProfile(BaseModel):
    """사용자 입력을 정규화한 적합성 판단 프로필."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    mindset: InvestmentMindset
    experience: ExperienceLevel
    investment_horizon_years: int = Field(ge=1, le=100)
    max_drawdown_pct: Decimal = Field(gt=0, le=Decimal("1"))
    liquidity_need: LiquidityNeed
    investment_amount: Decimal = Field(default=Decimal("1000000"), gt=0)
    currency: str = Field(default="KRW", pattern=r"^[A-Z]{3}$")
    as_of: datetime
    profile_version: int = Field(default=1, ge=1)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _aware(value)

    @property
    def effective_risk_score(self) -> int:
        """경험 수준을 넘는 위험을 자동 추천하지 않도록 상한을 적용한다."""

        return min(_MINDSET_SCORE[self.mindset], _EXPERIENCE_SCORE[self.experience])


class PortfolioCandidate(BaseModel):
    """사전에 등록된 포트폴리오 후보의 적합성 메타데이터."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    risk_band: PortfolioRiskBand
    minimum_experience: ExperienceLevel
    minimum_horizon_years: int = Field(ge=1, le=100)
    max_drawdown_pct: Decimal = Field(gt=0, le=Decimal("1"))
    max_exit_days: int = Field(ge=0, le=365)
    target_allocations: dict[str, Decimal] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("target_allocations")
    @classmethod
    def validate_target_allocations(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        if any(weight <= 0 or weight > Decimal("1") for weight in value.values()):
            raise ValueError("target allocation weights must be in (0, 1]")
        total = sum(value.values())
        if abs(total - Decimal("1")) > Decimal("0.0001"):
            raise ValueError("target allocation weights must sum to 1")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(not ref.strip() for ref in value):
            raise ValueError("evidence_refs cannot contain blank references")
        return value


class PortfolioRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: str
    name: str
    risk_band: PortfolioRiskBand
    fit_score: int = Field(ge=0, le=100)
    target_allocations: dict[str, Decimal]
    target_amounts: dict[str, Decimal]
    reasons: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class PortfolioExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: str
    reasons: list[str] = Field(min_length=1)


class SuitabilityResult(BaseModel):
    """사용자에게 전달할 후보 목록과 감사용 판정 메타데이터."""

    model_config = ConfigDict(extra="forbid")

    status: SuitabilityStatus
    calculation_version: str
    input_hash: str = Field(min_length=64, max_length=64)
    profile_user_id: str
    effective_risk_band: PortfolioRiskBand
    investment_amount: Decimal
    currency: str
    recommendations: list[PortfolioRecommendation]
    exclusions: list[PortfolioExclusion]
    manual_review_required: bool = True


def _canonical_input(profile: InvestorProfile, candidates: Sequence[PortfolioCandidate]) -> dict[str, Any]:
    return {
        "calculation_version": CALCULATION_VERSION,
        "profile": profile.model_dump(mode="json"),
        "candidates": [
            candidate.model_dump(mode="json")
            for candidate in sorted(candidates, key=lambda item: item.portfolio_id)
        ],
    }


def _input_hash(profile: InvestorProfile, candidates: Sequence[PortfolioCandidate]) -> str:
    encoded = json.dumps(
        _canonical_input(profile, candidates),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _risk_band(score: int) -> PortfolioRiskBand:
    return {
        1: PortfolioRiskBand.LOW,
        2: PortfolioRiskBand.MEDIUM,
        3: PortfolioRiskBand.HIGH,
    }[score]


def _fit_score(profile: InvestorProfile, candidate: PortfolioCandidate) -> int:
    score = 70
    if _RISK_SCORE[candidate.risk_band] == profile.effective_risk_score:
        score += 15
    if _EXPERIENCE_SCORE[candidate.minimum_experience] == _EXPERIENCE_SCORE[profile.experience]:
        score += 5
    if candidate.minimum_horizon_years == profile.investment_horizon_years:
        score += 5
    if candidate.max_exit_days == _MAX_LIQUIDITY_DAYS[profile.liquidity_need]:
        score += 5
    return min(score, 100)


def _exclusion_reasons(profile: InvestorProfile, candidate: PortfolioCandidate) -> list[str]:
    reasons: list[str] = []
    if candidate.as_of > profile.as_of:
        reasons.append("CANDIDATE_AFTER_PROFILE_AS_OF")
    if _RISK_SCORE[candidate.risk_band] > profile.effective_risk_score:
        reasons.append("RISK_BAND_EXCEEDS_EFFECTIVE_PROFILE_LIMIT")
    if _EXPERIENCE_SCORE[candidate.minimum_experience] > _EXPERIENCE_SCORE[profile.experience]:
        reasons.append("EXPERIENCE_REQUIREMENT_NOT_MET")
    if candidate.minimum_horizon_years > profile.investment_horizon_years:
        reasons.append("INVESTMENT_HORIZON_TOO_SHORT")
    if candidate.max_drawdown_pct > profile.max_drawdown_pct:
        reasons.append("MAX_DRAWDOWN_EXCEEDS_USER_TOLERANCE")
    if candidate.max_exit_days > _MAX_LIQUIDITY_DAYS[profile.liquidity_need]:
        reasons.append("LIQUIDITY_NEED_NOT_MET")
    return reasons


def _target_amounts(profile: InvestorProfile, candidate: PortfolioCandidate) -> dict[str, Decimal]:
    """Return deterministic currency amounts whose rounded total equals input amount."""

    allocations = list(candidate.target_allocations.items())
    amounts = {
        asset: (profile.investment_amount * weight).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        for asset, weight in allocations
    }
    remainder = profile.investment_amount.quantize(Decimal("0.01")) - sum(amounts.values(), Decimal("0"))
    if allocations:
        last_asset = allocations[-1][0]
        amounts[last_asset] += remainder
    return amounts


def recommend_portfolios(
    profile: InvestorProfile | Mapping[str, Any],
    candidates: Sequence[PortfolioCandidate | Mapping[str, Any]],
) -> SuitabilityResult:
    """Return a stable, evidence-linked portfolio list for one user profile.

    후보가 하나도 맞지 않으면 ``NO_MATCH``를 반환한다. 임의의 후보를 생성하거나
    위험한 후보를 낮춰서 통과시키지 않으며, 결과는 항상 사용자의 검토가 필요하다.
    """

    normalized_profile = InvestorProfile.model_validate(profile)
    normalized_candidates = tuple(PortfolioCandidate.model_validate(candidate) for candidate in candidates)
    if not normalized_candidates:
        raise ValueError("at least one portfolio candidate is required")

    input_hash = _input_hash(normalized_profile, normalized_candidates)
    recommendations: list[PortfolioRecommendation] = []
    exclusions: list[PortfolioExclusion] = []
    for candidate in normalized_candidates:
        reasons = _exclusion_reasons(normalized_profile, candidate)
        if reasons:
            exclusions.append(PortfolioExclusion(portfolio_id=candidate.portfolio_id, reasons=reasons))
            continue
        recommendations.append(
            PortfolioRecommendation(
                portfolio_id=candidate.portfolio_id,
                name=candidate.name,
                risk_band=candidate.risk_band,
                target_allocations=dict(candidate.target_allocations),
                target_amounts=_target_amounts(normalized_profile, candidate),
                fit_score=_fit_score(normalized_profile, candidate),
                reasons=[
                    "RISK_BAND_WITHIN_EFFECTIVE_PROFILE_LIMIT",
                    "EXPERIENCE_REQUIREMENT_MET",
                    "HORIZON_AND_LIQUIDITY_REQUIREMENTS_MET",
                    "DRAWDOWN_WITHIN_USER_TOLERANCE",
                ],
                evidence_refs=list(candidate.evidence_refs),
            )
        )

    recommendations.sort(key=lambda item: (-item.fit_score, item.portfolio_id))
    exclusions.sort(key=lambda item: item.portfolio_id)
    return SuitabilityResult(
        status=SuitabilityStatus.MATCHED if recommendations else SuitabilityStatus.NO_MATCH,
        calculation_version=CALCULATION_VERSION,
        input_hash=input_hash,
        profile_user_id=normalized_profile.user_id,
        effective_risk_band=_risk_band(normalized_profile.effective_risk_score),
        investment_amount=normalized_profile.investment_amount,
        currency=normalized_profile.currency,
        recommendations=recommendations,
        exclusions=exclusions,
    )


__all__ = [
    "CALCULATION_VERSION",
    "ExperienceLevel",
    "InvestmentMindset",
    "InvestorProfile",
    "LiquidityNeed",
    "PortfolioCandidate",
    "PortfolioExclusion",
    "PortfolioRecommendation",
    "PortfolioRiskBand",
    "SuitabilityResult",
    "SuitabilityStatus",
    "recommend_portfolios",
]
