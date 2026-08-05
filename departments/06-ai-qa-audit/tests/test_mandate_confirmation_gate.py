from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "departments/00-ceo-office/src"))

from mandate.policy import (
    ApprovalRules,
    MandatePolicy,
    PaperOrderMode,
    RiskBounds,
    UniversePolicy,
)

from orchestration.contracts.mandate_confirmation import (
    evaluate_mandate_confirmation,
)


def _policy() -> dict:
    return MandatePolicy(
        allowed_assets=["A005930"],
        forbidden_assets=[],
        risk_bounds=RiskBounds(
            base_capital="100000000",
            currency="KRW",
            max_instrument_weight="0.05",
            max_sector_weight="0.20",
            max_gross_exposure="1.0",
            max_concurrent_positions=8,
            max_daily_loss="0.03",
            max_drawdown_pct="0.10",
        ),
        universe_policy=UniversePolicy(
            allowed_markets=["KRX"],
            allowed_asset_classes=["KOREA_EQUITY"],
            forbidden_asset_classes=["LEVERAGED_ETF"],
            excluded_sectors=["TOBACCO"],
            trading_start="09:00",
            trading_end="15:30",
        ),
        approval_rules=ApprovalRules(paper_order_mode=PaperOrderMode.USER_APPROVAL),
    ).model_dump(mode="json")


def test_proposal_without_user_confirmation_is_hold():
    result = evaluate_mandate_confirmation(
        proposal_id="proposal-1",
        policy_payload=_policy(),
        risk_approved=True,
        qa_approved=True,
        user_confirmed=False,
        user_confirmation_id=None,
        user_confirmed_at=None,
    )
    assert result.action == "HOLD"
    assert result.reason == "USER_CONFIRMATION_REQUIRED"


def test_all_gates_are_required_before_activation():
    result = evaluate_mandate_confirmation(
        proposal_id="proposal-2",
        policy_payload=_policy(),
        risk_approved=True,
        qa_approved=True,
        user_confirmed=True,
        user_confirmation_id="approval-2",
        user_confirmed_at=datetime.now(timezone.utc),
    )
    assert result.action == "ACTIVATE"
    assert result.reason == "ALL_CONFIRMATION_GATES_PASSED"


def test_invalid_policy_can_never_activate():
    payload = _policy()
    payload["risk_bounds"]["max_sector_weight"] = "0.01"
    result = evaluate_mandate_confirmation(
        proposal_id="proposal-3",
        policy_payload=payload,
        risk_approved=True,
        qa_approved=True,
        user_confirmed=True,
        user_confirmation_id="approval-3",
        user_confirmed_at=datetime.now(timezone.utc),
    )
    assert result.action == "HOLD"
    assert result.reason == "POLICY_SCHEMA_INVALID"
