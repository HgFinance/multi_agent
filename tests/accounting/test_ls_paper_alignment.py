from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
ACCOUNTING = ROOT / "departments" / "05-accounting-portfolio"
sys.path.insert(0, str(ACCOUNTING / "ledger"))
sys.path.insert(0, str(ACCOUNTING / "reconciliation"))

from ledger import Ledger  # noqa: E402
from ls_paper_alignment import (  # noqa: E402
    BrokerAccountSnapshot,
    BrokerPosition,
    build_alignment_journal,
)


NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


def _snapshot(*, cash="492731425", buying_power="491585201"):
    return BrokerAccountSnapshot(
        cash=Decimal(cash),
        buying_power=Decimal(buying_power),
        positions=(
            BrokerPosition("000660", Decimal(5), Decimal(1526000)),
            BrokerPosition("005930", Decimal(6), Decimal(269027)),
        ),
        observed_at=NOW,
        account_masked="****5601",
    )


def test_alignment_is_balanced_rebuildable_and_idempotent() -> None:
    fund, book = uuid4(), uuid4()
    hynix, samsung = uuid4(), uuid4()
    ledger = Ledger(fund, book)
    ledger.post_capital(Decimal(500_000_000), NOW, "seed")

    journal = build_alignment_journal(
        ledger,
        _snapshot(),
        {"000660": hynix, "005930": samsung},
    )
    assert journal is not None
    assert sum(line.debit for line in journal.lines) == sum(
        line.credit for line in journal.lines
    )
    ledger.post(journal)
    positions, cash = ledger.rebuild()
    assert cash == Decimal(492_731_425)
    assert positions[hynix].quantity == 5
    assert positions[hynix].average_cost == Decimal(1_526_000)
    assert positions[samsung].quantity == 6
    assert positions[samsung].average_cost == Decimal(269_027)
    balances = ledger.trial_balance()
    assert balances.get("1200", Decimal(0)) + balances.get("2000", Decimal(0)) == Decimal(-1_146_224)

    assert (
        build_alignment_journal(
            ledger,
            _snapshot(),
            {"000660": hynix, "005930": samsung},
        )
        is None
    )


def test_alignment_resets_existing_position_cost_without_overwriting_journal() -> None:
    fund, book, samsung = uuid4(), uuid4(), uuid4()
    ledger = Ledger(fund, book)
    ledger.post_capital(Decimal(500_000_000), NOW, "seed")

    class Fill:
        quantity = Decimal(2)
        price = Decimal(269_000)
        fee = Decimal(81)
        tax = Decimal(0)
        event_time = NOW
        broker_fill_id = "local-fill"
        fill_id = uuid4()

    from contracts import Side
    from ledger import Position

    ledger.post_fill(Fill(), Side.BUY, samsung, Position(samsung))
    snapshot = BrokerAccountSnapshot(
        cash=Decimal(499_000_000),
        buying_power=Decimal(498_500_000),
        positions=(BrokerPosition("005930", Decimal(6), Decimal(269_027)),),
        observed_at=NOW,
        account_masked="****5601",
    )
    before = len(ledger.journals)
    journal = build_alignment_journal(ledger, snapshot, {"005930": samsung})
    assert journal is not None
    ledger.post(journal)
    positions, cash = ledger.rebuild()
    assert len(ledger.journals) == before + 1
    assert positions[samsung].quantity == 6
    assert positions[samsung].average_cost == Decimal(269_027)
    assert cash == Decimal(499_000_000)
