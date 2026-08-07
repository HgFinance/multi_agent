from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.order_tools import (
    OrderComplianceInput,
    evaluate_order_compliance,
)


def _request(**overrides: object) -> OrderComplianceInput:
    payload: dict[str, object] = {
        "mandate_id": "MND-1",
        "symbol": "005930",
        "side": "BUY",
        "notional": Decimal(1000000),
        "current_position_notional": Decimal(1000000),
        "resulting_position_notional": Decimal(2000000),
        "current_exposure_pct": Decimal("0.10"),
        "resulting_exposure_pct": Decimal("0.20"),
        "max_exposure_pct": Decimal("0.30"),
        "portfolio_var": Decimal(10),
        "portfolio_var_limit": Decimal(20),
        "as_of": datetime(2026, 8, 7, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return OrderComplianceInput.model_validate(payload)


def test_order_compliance_approves_within_limits() -> None:
    result = evaluate_order_compliance(_request())

    assert result.verdict == "APPROVE"
    assert result.authoritative is True
    assert result.tool_name == "evaluate_order_compliance"


def test_order_compliance_rejects_var_breach_and_resizes_exposure_breach() -> None:
    result = evaluate_order_compliance(
        _request(
            resulting_exposure_pct=Decimal("0.40"),
            portfolio_var=Decimal(25),
        )
    )

    assert result.verdict == "REJECT"
    assert result.limit_breaches == [
        "TOTAL_EXPOSURE_LIMIT_BREACH",
        "VAR_LIMIT_BREACH",
    ]
