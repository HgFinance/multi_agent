"""Shared deterministic execution-cost policy for every PAPER order lane."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


DEFAULT_FEE_BPS = Decimal("1.5")
DEFAULT_SELL_TAX_BPS = Decimal("15")
DEFAULT_MAX_PARTICIPATION = Decimal("0.05")
WON = Decimal(1)


def participation_cap(
    available: Decimal,
    lot_size: Decimal,
    *,
    max_participation: Decimal = DEFAULT_MAX_PARTICIPATION,
) -> Decimal:
    if available <= 0 or lot_size <= 0:
        return Decimal(0)
    raw = (available * max_participation // lot_size) * lot_size
    # Preserve the canonical PaperBroker one-lot minimum for a non-empty L1.
    return max(raw, lot_size)


def fill_costs(
    quantity: Decimal,
    price: Decimal,
    side: str,
    *,
    fee_bps: Decimal = DEFAULT_FEE_BPS,
    sell_tax_bps: Decimal = DEFAULT_SELL_TAX_BPS,
) -> tuple[Decimal, Decimal]:
    notional = quantity * price
    fee = (notional * fee_bps / Decimal(10_000)).quantize(
        WON, rounding=ROUND_HALF_UP
    )
    tax = (
        (notional * sell_tax_bps / Decimal(10_000)).quantize(
            WON, rounding=ROUND_HALF_UP
        )
        if side == "SELL"
        else Decimal(0)
    )
    return fee, tax


__all__ = [
    "DEFAULT_FEE_BPS",
    "DEFAULT_MAX_PARTICIPATION",
    "DEFAULT_SELL_TAX_BPS",
    "fill_costs",
    "participation_cap",
]
