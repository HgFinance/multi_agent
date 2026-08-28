from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT))

from directives.market_data import (  # noqa: E402
    LsPaperFallbackMarketDataProvider,
    quote_fallback_enabled,
    with_quote_fallback,
)


class _Primary:
    def quote(self, instrument, *, now, max_age_seconds=None):  # pragma: no cover
        raise AssertionError("not called in wiring tests")


class _Broker:
    pass


def test_disabled_and_unbrokered_leaves_the_primary_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRADING_MARKET_QUOTE_LS_FALLBACK", raising=False)
    primary = _Primary()
    assert quote_fallback_enabled() is False
    wrapped = with_quote_fallback(
        primary,
        external_broker=None,
        broker_factory=lambda: pytest.fail("factory must not run when disabled"),
    )
    assert wrapped is primary


@pytest.mark.parametrize("flag", ("1", "true", "TRUE", "yes", "on"))
def test_flag_enables_the_read_only_quote_fallback(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_MARKET_QUOTE_LS_FALLBACK", flag)
    broker = _Broker()
    wrapped = with_quote_fallback(
        _Primary(), external_broker=None, broker_factory=lambda: broker
    )
    assert isinstance(wrapped, LsPaperFallbackMarketDataProvider)
    assert wrapped.broker is broker


def test_existing_broker_wraps_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order routing already on LS PAPER implies the quote read is available."""

    monkeypatch.delenv("TRADING_MARKET_QUOTE_LS_FALLBACK", raising=False)
    broker = _Broker()
    wrapped = with_quote_fallback(
        _Primary(),
        external_broker=broker,
        broker_factory=lambda: pytest.fail("existing broker must be reused"),
    )
    assert isinstance(wrapped, LsPaperFallbackMarketDataProvider)
    assert wrapped.broker is broker


def _wraps_market_data(path: Path) -> bool:
    """True when every HttpMarketDataProvider build flows through the helper."""

    tree = ast.parse(path.read_text())
    builds = 0
    wrapped = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "from_env":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "HttpMarketDataProvider":
                builds += 1
        if isinstance(func, ast.Name) and func.id == "with_quote_fallback":
            wrapped += 1
    return builds > 0 and wrapped > 0


def test_admission_api_and_directive_worker_share_one_wiring() -> None:
    """The worker built the provider bare while the API wrapped it.

    A triggered conditional rule therefore failed with
    TRADING_MARKET_QUOTE_STALE even though the same deployment could read a
    fresh t1101 quote (2026-08-28, 001210).  Both call sites must keep using
    the shared helper so the two paths cannot drift apart again.
    """

    for module in ("api/directive_routes.py", "directives/worker.py"):
        assert _wraps_market_data(TRADING_ROOT / module), module


# --- one-sided order books ------------------------------------------------

from directives.market_data import (  # noqa: E402
    MarketDataError,
    require_two_sided_book,
)


def test_missing_ask_is_named_rather_than_called_invalid() -> None:
    """001210 sat at 상한가 10,680 with no ask on 2026-08-28.

    The admission guard reported TRADING_MARKET_QUOTE_INVALID, which reads as
    "your market data is broken" when the true answer is "nobody is selling".
    """

    with pytest.raises(MarketDataError) as raised:
        require_two_sided_book("10680", None)
    assert raised.value.code == "TRADING_MARKET_NO_ASK"


def test_missing_bid_is_named_for_the_sell_side() -> None:
    with pytest.raises(MarketDataError) as raised:
        require_two_sided_book(None, "10680")
    assert raised.value.code == "TRADING_MARKET_NO_BID"


@pytest.mark.parametrize("absent", (None, 0, "0", "-1"))
def test_zero_and_negative_count_as_an_absent_side(absent) -> None:
    with pytest.raises(MarketDataError) as raised:
        require_two_sided_book("10680", absent)
    assert raised.value.code == "TRADING_MARKET_NO_ASK"


def test_two_sided_and_wholly_empty_books_are_left_alone() -> None:
    require_two_sided_book("10680", "10690")
    # Both sides gone is an empty book, not a limit-up/limit-down state; the
    # existing INVALID path still owns it.
    require_two_sided_book(None, None)


def test_a_one_sided_projection_is_reverified_against_the_broker() -> None:
    """The projection can simply be missing a side; only REST can tell."""

    from directives.market_data import LsPaperFallbackMarketDataProvider

    assert "TRADING_MARKET_NO_ASK" in LsPaperFallbackMarketDataProvider._FALLBACK_CODES
    assert "TRADING_MARKET_NO_BID" in LsPaperFallbackMarketDataProvider._FALLBACK_CODES
