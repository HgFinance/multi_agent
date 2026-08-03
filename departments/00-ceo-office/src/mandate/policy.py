# 담당자: 영주 (CEO Office)
# 근거: HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F01(사용자 Mandate),
#       TEAM_YOUNGJU_CEO_HR_GUIDE.md 4.2/5.1
#
# F01 "구조화 정책"의 Canonical Pydantic 계약.
# supabase/migrations/20260729000200_governance_workforce.sql 의
# governance.mandate_versions 는 risk_bounds / universe_policy / approval_rules /
# execution_rules 를 jsonb 로만 선언하고 내부 형태를 정의하지 않는다. 이 파일이 그
# jsonb 컬럼들의 내부 계약(=System Enforcement 가 쓰는 구조화 필드)을 정의한다.
#
# 설계 원칙(CLAUDE.md 개발 원칙 2, 9):
#   - LLM 출력/자연어가 아니라 이 Pydantic Schema 가 저장 가능 여부의 판정 기준이다.
#   - 값 범위와 "상호 모순"을 결정론적으로 검증해 잘못된 한도 조합을 저장 못 하게 한다.
#     (F01 완료조건: "잘못된 한도 조합을 저장할 수 없다.")

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class PaperOrderMode(str, Enum):
    """자동 Paper 주문 vs 사용자 승인 (F01 입력: '자동 Paper 주문 또는 사용자 승인')."""

    AUTO = "AUTO"
    USER_APPROVAL = "USER_APPROVAL"


# ---------------------------------------------------------------------------
# jsonb 컬럼별 하위 모델
# ---------------------------------------------------------------------------


class RiskBounds(BaseModel):
    """governance.mandate_versions.risk_bounds 의 내부 계약.

    모든 '비중(weight)'은 base_capital 대비 순노출 비율(0<w<=상한)로 해석한다.
    포함 관계: instrument <= sector <= gross_exposure (단일 종목은 자기 섹터 상한을,
    섹터는 전체 Gross 상한을 넘을 수 없다). daily_loss 는 base_capital 대비 비율이다.
    """

    model_config = ConfigDict(extra="forbid")

    base_capital: Decimal = Field(gt=0, description="Paper Capital 기준 자본")
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="base_capital 통화 (예: KRW)")

    max_instrument_weight: Decimal = Field(
        gt=0, le=1, description="종목 최대 비중 (base_capital 대비)"
    )
    max_sector_weight: Decimal = Field(
        gt=0, le=1, description="섹터 최대 비중 (base_capital 대비)"
    )
    max_gross_exposure: Decimal = Field(
        gt=0, description="Gross Exposure 상한 (base_capital 대비, 1.0=100%)"
    )
    max_concurrent_positions: int = Field(gt=0, description="최대 동시 Position 수")
    max_daily_loss: Decimal = Field(
        gt=0, le=1, description="일일 최대 손실 (base_capital 대비 비율)"
    )

    @model_validator(mode="after")
    def _check_weight_containment(self) -> RiskBounds:
        # 상호 모순 1: 단일 종목 상한이 섹터 상한보다 크다.
        if self.max_instrument_weight > self.max_sector_weight:
            raise ValueError(
                "max_instrument_weight 는 max_sector_weight 보다 클 수 없다 "
                f"({self.max_instrument_weight} > {self.max_sector_weight})"
            )
        # 상호 모순 2: 섹터 상한이 전체 Gross 상한보다 크다.
        if self.max_sector_weight > self.max_gross_exposure:
            raise ValueError(
                "max_sector_weight 는 max_gross_exposure 보다 클 수 없다 "
                f"({self.max_sector_weight} > {self.max_gross_exposure})"
            )
        return self


class UniversePolicy(BaseModel):
    """governance.mandate_versions.universe_policy 의 내부 계약."""

    model_config = ConfigDict(extra="forbid")

    allowed_markets: list[str] = Field(
        min_length=1, description="허용 시장 코드 (예: ['KRX'])"
    )
    trading_start: str = Field(
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="거래 시작 HH:MM (시장 로컬)"
    )
    trading_end: str = Field(
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="거래 종료 HH:MM (시장 로컬)"
    )

    @model_validator(mode="after")
    def _check_session(self) -> UniversePolicy:
        # 상호 모순 3: 거래 종료가 시작보다 빠르거나 같다.
        if self.trading_end <= self.trading_start:
            raise ValueError(
                "trading_end 는 trading_start 보다 늦어야 한다 "
                f"({self.trading_start} ~ {self.trading_end})"
            )
        # 상호 모순 4: 허용 시장 코드 중복.
        if len(set(self.allowed_markets)) != len(self.allowed_markets):
            raise ValueError("allowed_markets 에 중복 시장 코드가 있다")
        return self


class ApprovalRules(BaseModel):
    """governance.mandate_versions.approval_rules 의 내부 계약."""

    model_config = ConfigDict(extra="forbid")

    paper_order_mode: PaperOrderMode = Field(
        description="AUTO: 자동 Paper 주문 / USER_APPROVAL: 주문마다 사용자 승인"
    )
    # F01: 장중 Risk 확대(=loosen)는 사용자 재승인이 필요하다. 기본 True 를 끄지 못하게
    # 강제하지는 않지만, 끄려면 명시적으로 False 를 넣어야 하고 그 자체가 Version 변경이다.
    risk_expansion_requires_user_approval: bool = Field(
        default=True,
        description="장중 Risk 확대 시 사용자 재승인 요구 (F01)",
    )


class MandatePolicy(BaseModel):
    """mandate_versions 한 Version 의 구조화 정책 전체.

    상단 필드는 mandate_versions 의 개별 컬럼(allowed_assets/forbidden_assets)과,
    하위 모델은 jsonb 컬럼(risk_bounds/universe_policy/approval_rules)과 1:1 대응한다.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_assets: list[str] = Field(
        default_factory=list, description="허용 자산 instrument_id/코드"
    )
    forbidden_assets: list[str] = Field(
        default_factory=list, description="금지 자산 instrument_id/코드"
    )
    risk_bounds: RiskBounds
    universe_policy: UniversePolicy
    approval_rules: ApprovalRules

    @model_validator(mode="after")
    def _check_asset_lists(self) -> MandatePolicy:
        allowed = set(self.allowed_assets)
        forbidden = set(self.forbidden_assets)
        # 상호 모순 5: 같은 자산이 허용이자 금지.
        overlap = allowed & forbidden
        if overlap:
            raise ValueError(
                f"allowed_assets 와 forbidden_assets 가 겹친다: {sorted(overlap)}"
            )
        # 상호 모순 6: allowed_assets 를 명시했는데 전부 금지 목록이라 실거래 가능 자산이 0.
        if self.allowed_assets and allowed <= forbidden:
            raise ValueError("allowed_assets 가 전부 forbidden_assets 에 포함돼 거래 가능 자산이 없다")
        return self


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/policy.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pydantic import ValidationError

    def _valid_risk(**over) -> dict:
        base = dict(
            base_capital="100000000",
            currency="KRW",
            max_instrument_weight="0.1",
            max_sector_weight="0.3",
            max_gross_exposure="1.0",
            max_concurrent_positions=10,
            max_daily_loss="0.03",
        )
        base.update(over)
        return base

    def _valid_universe(**over) -> dict:
        base = dict(allowed_markets=["KRX"], trading_start="09:00", trading_end="15:30")
        base.update(over)
        return base

    def _valid_policy(**over) -> dict:
        base = dict(
            allowed_assets=["A005930"],
            forbidden_assets=["A000660"],
            risk_bounds=_valid_risk(),
            universe_policy=_valid_universe(),
            approval_rules=dict(paper_order_mode="USER_APPROVAL"),
        )
        base.update(over)
        return base

    # 1) 정상 정책은 통과한다.
    ok = MandatePolicy(**_valid_policy())
    assert ok.risk_bounds.max_instrument_weight == Decimal("0.1")
    assert ok.approval_rules.risk_expansion_requires_user_approval is True

    def _rejects(label: str, factory) -> None:
        try:
            factory()
        except (ValidationError, ValueError):
            return
        raise AssertionError(f"거부돼야 하는데 통과함: {label}")

    # 2) 값 범위 위반.
    _rejects("base_capital<=0", lambda: RiskBounds(**_valid_risk(base_capital="0")))
    _rejects("weight>1", lambda: RiskBounds(**_valid_risk(max_instrument_weight="1.5")))
    _rejects("daily_loss>1", lambda: RiskBounds(**_valid_risk(max_daily_loss="1.2")))
    _rejects("positions<=0", lambda: RiskBounds(**_valid_risk(max_concurrent_positions=0)))
    _rejects("bad currency", lambda: RiskBounds(**_valid_risk(currency="won")))

    # 3) 상호 모순 (핵심 F01 완료조건).
    _rejects(
        "instrument>sector",
        lambda: RiskBounds(**_valid_risk(max_instrument_weight="0.4", max_sector_weight="0.3")),
    )
    _rejects(
        "sector>gross",
        lambda: RiskBounds(**_valid_risk(max_sector_weight="0.9", max_gross_exposure="0.5")),
    )
    _rejects(
        "trading_end<=start",
        lambda: UniversePolicy(**_valid_universe(trading_start="15:30", trading_end="09:00")),
    )
    _rejects("empty markets", lambda: UniversePolicy(**_valid_universe(allowed_markets=[])))
    _rejects(
        "allowed∩forbidden",
        lambda: MandatePolicy(**_valid_policy(allowed_assets=["A005930"], forbidden_assets=["A005930"])),
    )
    _rejects(
        "unknown field",
        lambda: RiskBounds(**_valid_risk(leverage="2")),  # extra=forbid
    )

    print("policy.py 자체 점검 통과")
