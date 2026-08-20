from __future__ import annotations

import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT))
sys.path.insert(0, str(TRADING_ROOT / "api"))

from conditional_rule_routes import _assert_confirmed_rule_quantity  # noqa: E402
from directives.contracts import DirectiveAction, UserDirectiveRequest  # noqa: E402
from directives.service import DirectiveServiceError  # noqa: E402
from orchestration.conditional_rules import ConditionalRuleSpec  # noqa: E402


def _spec(*, sizing_type: str, sizing_value: str | None) -> ConditionalRuleSpec:
    sizing = {"type": sizing_type}
    if sizing_value is not None:
        sizing["value"] = sizing_value
    return ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
            },
            "action": {"side": "SELL", "sizing": sizing},
            "evaluation": {"clock": "QUOTE"},
            "expires_at": "2026-09-20T00:00:00+00:00",
            "raw_instruction_sha256": "0" * 64,
        }
    )


def _admission(spec: ConditionalRuleSpec, quantity: str):
    request = UserDirectiveRequest.model_validate(
        {
            "fund_id": spec.authority.fund_id,
            "book_id": spec.authority.book_id,
            "action": DirectiveAction.PLACE_ORDER,
            "instruction_ref": "conditional:test:v1",
            "idempotency_key": "conditional:test:execution",
            "payload": {
                "instrument_id": str(spec.instrument_id),
                "symbol": spec.symbol,
                "side": "SELL",
                "quantity": quantity,
                "order_type": "MARKET",
                "limit_price": None,
                "time_in_force": "DAY",
            },
        }
    )
    return SimpleNamespace(spec=spec, request=request)


class _Repository:
    def book_guard(self, *args):
        return nullcontext()

    def resolve_instrument(self, *args):
        return SimpleNamespace(lot_size=Decimal("1"))

    def sellable_quantity(self, *args):
        return Decimal("103")


def test_fixed_quantity_must_match_confirmed_rule() -> None:
    spec = _spec(sizing_type="FIXED_SHARES", sizing_value="2")

    _assert_confirmed_rule_quantity(_admission(spec, "2"), _Repository())
    with pytest.raises(DirectiveServiceError) as raised:
        _assert_confirmed_rule_quantity(_admission(spec, "20"), _Repository())

    assert raised.value.code == "TRADING_CONDITIONAL_RULE_QUANTITY_MISMATCH"


def test_position_percent_is_recomputed_from_canonical_sellable_quantity() -> None:
    spec = _spec(sizing_type="POSITION_PERCENT", sizing_value="0.20")

    _assert_confirmed_rule_quantity(_admission(spec, "20"), _Repository())
    with pytest.raises(DirectiveServiceError):
        _assert_confirmed_rule_quantity(_admission(spec, "21"), _Repository())
