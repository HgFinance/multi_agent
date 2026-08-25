from __future__ import annotations

import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT))
sys.path.insert(0, str(TRADING_ROOT / "api"))

from conditional_rule_routes import (  # noqa: E402
    _assert_confirmed_rule_quantity,
)
from rules.admission import (  # noqa: E402
    ConditionalRuleAdmissionError,
    _assert_recent_evaluation,
    _conditional_order_payload,
    _fresh_proof_jti,
)
from directives.contracts import DirectiveAction, UserDirectiveRequest  # noqa: E402
from directives.market_data import (  # noqa: E402
    MarketDataError,
    TrustedQuote,
    validate_quote,
)
from directives.repository import InstrumentRef  # noqa: E402
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


def test_confirmed_limit_price_is_passed_to_the_paper_directive() -> None:
    base = _spec(sizing_type="FIXED_SHARES", sizing_value="1")
    spec = ConditionalRuleSpec.model_validate(
        {
            **base.model_dump(mode="json"),
            "action": {
                "side": "SELL",
                "sizing": {"type": "FIXED_SHARES", "value": "1"},
                "order_type": "LIMIT",
                "limit_price": "299500",
            },
        }
    )

    payload = _conditional_order_payload(spec, quantity=Decimal("1"))

    assert payload["order_type"] == "LIMIT"
    assert payload["limit_price"] == "299500"


def test_conditional_evaluation_must_still_be_recent_at_trading_admission() -> None:
    spec = _spec(sizing_type="FIXED_SHARES", sizing_value="1")
    now = datetime(2026, 8, 24, 3, 30, tzinfo=timezone.utc)

    _assert_recent_evaluation(spec, now - timedelta(seconds=29), now=now)
    with pytest.raises(ConditionalRuleAdmissionError) as raised:
        _assert_recent_evaluation(spec, now - timedelta(seconds=31), now=now)

    assert raised.value.code == "TRADING_CONDITIONAL_RULE_EVALUATION_STALE"


def test_each_conditional_submission_attempt_gets_a_fresh_one_use_proof() -> None:
    execution_id = uuid4()

    first = _fresh_proof_jti(execution_id)
    second = _fresh_proof_jti(execution_id)

    assert first.startswith(f"conditional-rule:{execution_id}:")
    assert second.startswith(f"conditional-rule:{execution_id}:")
    assert first != second


def test_conditional_limit_quote_can_use_rule_lifetime_cap_only_with_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_MARKET_QUOTE_MAX_AGE_SECONDS", "30")
    now = datetime(2026, 8, 24, 3, 30, tzinfo=timezone.utc)
    instrument = InstrumentRef(uuid4(), "000660", Decimal("1"), None, "KRW")
    quote = TrustedQuote(
        str(instrument.instrument_id),
        instrument.symbol,
        now - timedelta(seconds=100),
        Decimal("1678000"),
        Decimal("1679000"),
        Decimal("100"),
        Decimal("100"),
        "fixture",
    )

    with pytest.raises(MarketDataError) as raised:
        validate_quote(quote, instrument, now=now)
    assert raised.value.code == "TRADING_MARKET_QUOTE_STALE"

    assert (
        validate_quote(
            quote,
            instrument,
            now=now,
            max_age_seconds=600,
        )
        is quote
    )
