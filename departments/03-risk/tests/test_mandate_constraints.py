from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "departments/03-risk/engine"))
sys.path.insert(0, str(ROOT / "departments/02-trading/contracts"))

from contracts import (
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from risk_engine import (
    CounterpartyHealth,
    CounterpartyStatus,
    LimitSet,
    MandateScope,
    MarketStatus,
    PortfolioState,
    RejectReason,
    RiskContext,
    RiskEngine,
    TradingState,
)


def _context(*, mandate: MandateScope, portfolio: PortfolioState) -> RiskContext:
    now = datetime.now(timezone.utc)
    return RiskContext(
        mandate=mandate,
        limits=LimitSet(
            soft_single_issuer_pct=Decimal("0.20"),
            hard_single_issuer_pct=Decimal("0.30"),
            max_daily_turnover_notional=Decimal(100000000),
            max_daily_order_count=50,
            max_daily_loss=Decimal(10000000),
            max_drawdown_pct=Decimal("0.20"),
        ),
        restricted_items=(),
        portfolio=portfolio,
        market_status=MarketStatus(tradable=True, reason=""),
        counterparty=CounterpartyStatus(
            broker_adapter="paper",
            health=CounterpartyHealth.OK,
        ),
        trading_state=TradingState.ENABLED,
        as_of=now,
    )


def _intent(instrument_id, *, quantity="100") -> OrderIntent:
    now = datetime.now(timezone.utc)
    return OrderIntent(
        trade_case_id=uuid4(),
        fund_id=uuid4(),
        book_id=uuid4(),
        strategy_id=uuid4(),
        instrument_id=instrument_id,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(70000),
        time_in_force=TimeInForce.DAY,
        snapshot=MarketSnapshot(
            market_snapshot_id="mandate-test",
            as_of=now,
            bid=Decimal(69900),
            ask=Decimal(70000),
        ),
        created_at=now,
        valid_until=now + timedelta(hours=1),
        idempotency_key=f"mandate-{instrument_id}",
        created_by="trader-pm-agent",
        trace_id="mandate-test-trace",
    )


def _portfolio(instrument_id, **overrides) -> PortfolioState:
    values = {
        "fund_id": uuid4(),
        "cash": Decimal(100000000),
        "buying_power": Decimal(100000000),
        "gross_exposure": Decimal(100000000),
        "equity": Decimal(1000000000),
        "peak_equity": Decimal(1000000000),
        "issuer_of": {instrument_id: "ISSUER"},
        "issuer_exposure": {"ISSUER": Decimal(1000000)},
        "instrument_asset_class": {instrument_id: "LEVERAGED_ETF"},
        "instrument_sector": {instrument_id: "TECH"},
        "sector_exposure": {"TECH": Decimal(99000000)},
    }
    values.update(overrides)
    return PortfolioState(**values)


def test_forbidden_asset_class_is_binding():
    instrument_id = uuid4()
    mandate = MandateScope(
        fund_id=uuid4(),
        allowed_instrument_ids=None,
        min_order_notional=Decimal(100000),
        max_order_notional=Decimal(50000000),
        forbidden_asset_classes=frozenset({"LEVERAGED_ETF"}),
    )
    result = RiskEngine().check_order(
        _intent(instrument_id),
        _context(mandate=mandate, portfolio=_portfolio(instrument_id)),
    )
    assert result.decision.verdict.value == "reject"
    assert RejectReason.FORBIDDEN_ASSET_CLASS in result.reason_codes


def test_sector_weight_is_resized_before_order_is_approved():
    instrument_id = uuid4()
    mandate = MandateScope(
        fund_id=uuid4(),
        allowed_instrument_ids=None,
        min_order_notional=Decimal(100000),
        max_order_notional=Decimal(50000000),
        max_sector_weight=Decimal("0.10"),
    )
    result = RiskEngine().check_order(
        _intent(instrument_id),
        _context(mandate=mandate, portfolio=_portfolio(instrument_id)),
    )
    assert result.decision.approved_quantity < Decimal(100)
    assert RejectReason.SECTOR_LIMIT in result.reason_codes


def test_missing_metadata_fails_closed_when_sector_limit_is_configured():
    instrument_id = uuid4()
    mandate = MandateScope(
        fund_id=uuid4(),
        allowed_instrument_ids=None,
        min_order_notional=Decimal(100000),
        max_order_notional=Decimal(50000000),
        max_sector_weight=Decimal("0.10"),
    )
    portfolio = _portfolio(instrument_id, instrument_sector={})
    result = RiskEngine().check_order(
        _intent(instrument_id), _context(mandate=mandate, portfolio=portfolio)
    )
    assert result.decision.verdict.value == "reject"
    assert RejectReason.MANDATE_METADATA_MISSING in result.reason_codes


def test_nine_risk_presets_and_alignment_are_deterministic():
    sys.path.insert(0, str(ROOT / "departments/03-risk"))
    from mandate_presets import (
        RISK_PRESETS,
        PresetAlignment,
        resolve_risk_preset,
        validate_preset_alignment,
    )

    assert len(RISK_PRESETS) == 9
    preset = resolve_risk_preset("RISK_SEEKING", "BEGINNER")
    status, violations = validate_preset_alignment(
        mindset="RISK_SEEKING",
        experience="BEGINNER",
        max_instrument_weight=preset.max_instrument_weight,
        max_sector_weight=preset.max_sector_weight,
        max_gross_exposure=preset.max_gross_exposure,
        max_concurrent_positions=preset.max_concurrent_positions,
    )
    assert status is PresetAlignment.MATCHED
    assert violations == ()

    status, violations = validate_preset_alignment(
        mindset="SAFETY_FIRST",
        experience="BEGINNER",
        max_instrument_weight=Decimal("0.20"),
        max_sector_weight=Decimal("0.30"),
        max_gross_exposure=Decimal("1.00"),
        max_concurrent_positions=8,
    )
    assert status is PresetAlignment.REQUIRES_RISK_REVIEW
    assert "max_instrument_weight" in violations
