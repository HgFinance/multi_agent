from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


COLLECTORS = (
    Path(__file__).resolve().parents[1]
    / "departments"
    / "01-research"
    / "collectors"
)
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import ls_unified_parser as parser


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00088k", "00088K"),
        (" 00088K ", "00088K"),
        ("U00088k", "00088K"),
        ("A00088k", "00088K"),
    ],
)
def test_normalize_symbol_preserves_exact_alphanumeric_krx_code(
    raw: str, expected: str
) -> None:
    assert parser.normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "12345", "00088-", "X00088K", "not-a-code"]
)
def test_normalize_symbol_rejects_non_code_text(raw: str) -> None:
    assert parser.normalize_symbol(raw) is None


def test_tick_and_quote_parsers_keep_alphanumeric_symbol() -> None:
    received_at = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)
    tick = parser.parse_tick(
        parser._tick_body(shcode="U00088k"), received_at
    )
    quote = parser.parse_quote(
        parser._quote_body(shcode="A00088k"), received_at
    )

    assert tick is not None and tick.symbol == "00088K"
    assert quote is not None and quote.symbol == "00088K"
