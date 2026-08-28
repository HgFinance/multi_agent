"""Reconcile the internal PAPER ledger to the LS PAPER broker account.

The broker snapshot is never written by overwriting an immutable Journal.
Differences become one balanced, content-addressed reconciliation Journal and
the normal ledger projection is rebuilt from Journals afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

HERE = Path(__file__).resolve().parent
ACCOUNTING_ROOT = HERE.parent
LEDGER_ROOT = ACCOUNTING_ROOT / "ledger"
if str(LEDGER_ROOT) not in sys.path:
    sys.path.insert(0, str(LEDGER_ROOT))

from ledger import (  # noqa: E402
    CAPITAL,
    CASH,
    PAYABLE,
    RECEIVABLE,
    SECURITIES,
    ZERO,
    Journal,
    JournalLine,
    Ledger,
)

from orchestration.service_health import probe_http, probe_postgres


def _load_ledger_repository():
    """Resolve the accounting repository without cross-department collisions."""

    try:
        from repository import LedgerRepository as repository_type  # noqa: E402

        return repository_type
    except ImportError:
        # Several legacy department entry points expose a top-level
        # ``repository`` module. If another department was imported first in
        # a long-lived process, load the accounting sibling by its file path
        # instead of accepting the wrong repository implementation.
        path = LEDGER_ROOT / "repository.py"
        spec = importlib.util.spec_from_file_location(
            "_hgfinance_accounting_ledger_repository", path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load accounting repository: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.LedgerRepository


LedgerRepository = _load_ledger_repository()


class LSPaperAlignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal
    average_cost: Decimal


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    cash: Decimal
    buying_power: Decimal
    positions: tuple[BrokerPosition, ...]
    observed_at: datetime
    account_masked: str
    source: str = "ls-openapi"

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "cash": str(self.cash),
            "buying_power": str(self.buying_power),
            "positions": [
                {
                    "symbol": item.symbol,
                    "quantity": str(item.quantity),
                    "average_cost": str(item.average_cost),
                }
                for item in sorted(self.positions, key=lambda row: row.symbol)
            ],
            "account_masked": self.account_masked,
            "source": self.source,
        }

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_payload(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LSPaperAlignmentError(f"broker snapshot {field} is invalid") from exc
    if not parsed.is_finite():
        raise LSPaperAlignmentError(f"broker snapshot {field} is invalid")
    return parsed


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LSPaperAlignmentError("broker snapshot endpoint is unavailable") from exc
    if not isinstance(body, dict):
        raise LSPaperAlignmentError("broker snapshot endpoint returned a non-object")
    return body


def fetch_broker_snapshot(base_url: str, *, timeout: float = 20.0) -> BrokerAccountSnapshot:
    root = base_url.rstrip("/")
    account = _get_json(root + "/ui/account/snapshot", timeout)
    if account.get("environment") != "PAPER":
        raise LSPaperAlignmentError("broker projection is not LS PAPER")
    if account.get("source") != "ls-openapi" or account.get("authoritative") is not False:
        raise LSPaperAlignmentError("broker account projection provenance is invalid")
    # The BFF account snapshot is the single broker aggregation/cache boundary.
    # Do not call /ui/portfolio/live here: that route also loads order history
    # and can issue a second, independently cached broker request.
    holdings = account.get("holdings")
    if not isinstance(holdings, dict) or holdings.get("error"):
        raise LSPaperAlignmentError("broker holdings are unavailable")
    # `synced` describes whether the realtime feed's local event projection
    # agrees with this broker snapshot.  It is not a validity flag for the
    # broker snapshot itself: a drifted local projection must be repaired by
    # reconciliation, not treated as a reason to block reconciliation.
    if holdings.get("synced") is None:
        raise LSPaperAlignmentError("broker holdings synchronization state is unknown")
    rows = holdings.get("rows")
    if not isinstance(rows, list):
        raise LSPaperAlignmentError("broker holdings rows are invalid")
    positions: list[BrokerPosition] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise LSPaperAlignmentError("broker holding row is invalid")
        symbol = str(raw.get("symbol") or "").strip().removeprefix("A")
        quantity = _decimal(raw.get("quantity"), "quantity")
        average_cost = _decimal(raw.get("average_cost"), "average_cost")
        if len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            raise LSPaperAlignmentError("broker holding symbol is invalid or duplicated")
        if quantity < 0 or average_cost < 0:
            raise LSPaperAlignmentError("broker holding amount is negative")
        seen.add(symbol)
        if quantity > 0:
            positions.append(BrokerPosition(symbol, quantity, average_cost))
    try:
        observed_at = datetime.fromisoformat(str(account.get("observed_at")))
    except (TypeError, ValueError) as exc:
        raise LSPaperAlignmentError("broker observation timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise LSPaperAlignmentError("broker observation timestamp is naive")
    masked = str(account.get("account_no_masked") or "")
    if not masked.startswith("****"):
        raise LSPaperAlignmentError("broker account identity is not masked")
    return BrokerAccountSnapshot(
        cash=_decimal(account.get("cash"), "cash"),
        buying_power=_decimal(account.get("buying_power"), "buying_power"),
        positions=tuple(positions),
        observed_at=observed_at.astimezone(timezone.utc),
        account_masked=masked,
    )


def build_alignment_journal(
    ledger: Ledger,
    snapshot: BrokerAccountSnapshot,
    instrument_ids: Mapping[str, UUID],
) -> Journal | None:
    current_positions, current_cash = ledger.rebuild()
    target_positions: dict[UUID, BrokerPosition] = {}
    for position in snapshot.positions:
        instrument_id = instrument_ids.get(position.symbol)
        if instrument_id is None:
            raise LSPaperAlignmentError(
                f"broker symbol {position.symbol} has no canonical instrument"
            )
        target_positions[instrument_id] = position

    lines: list[JournalLine] = []

    # Reset only instruments whose quantity or average cost differs.  A
    # credit followed by a debit is required when quantity is unchanged but
    # the authoritative broker average cost changed.
    for instrument_id in sorted(
        set(current_positions) | set(target_positions), key=str
    ):
        current = current_positions.get(instrument_id)
        target = target_positions.get(instrument_id)
        target_qty = target.quantity if target else ZERO
        target_avg = target.average_cost if target else ZERO
        current_qty = current.quantity if current else ZERO
        current_avg = current.average_cost if current else ZERO
        if current_qty == target_qty and current_avg == target_avg:
            continue
        if current and current.quantity > 0:
            cost = current.cost_basis
            if cost <= 0:
                raise LSPaperAlignmentError("current position has non-positive cost basis")
            lines.append(
                JournalLine(
                    SECURITIES,
                    credit=cost,
                    instrument_id=instrument_id,
                    quantity=-current.quantity,
                    unit_price=current.average_cost,
                )
            )
        if target and target.quantity > 0:
            cost = target.quantity * target.average_cost
            if cost <= 0:
                raise LSPaperAlignmentError("broker position has non-positive cost basis")
            lines.append(
                JournalLine(
                    SECURITIES,
                    debit=cost,
                    instrument_id=instrument_id,
                    quantity=target.quantity,
                    unit_price=target.average_cost,
                )
            )

    cash_delta = snapshot.cash - current_cash
    if cash_delta > 0:
        lines.append(JournalLine(CASH, debit=cash_delta))
    elif cash_delta < 0:
        lines.append(JournalLine(CASH, credit=-cash_delta))

    # Internal available cash is settled + unsettled - reservation.  LS
    # ``MnyOrdAbleAmt`` is the matching order-admission value, so reset the
    # receivable/payable projection to buying_power - cash.
    balances = ledger.trial_balance()
    current_receivable = balances.get(RECEIVABLE, ZERO)
    current_payable = balances.get(PAYABLE, ZERO)
    target_unsettled = snapshot.buying_power - snapshot.cash
    target_receivable = max(target_unsettled, ZERO)
    target_payable = min(target_unsettled, ZERO)
    if current_receivable != target_receivable or current_payable != target_payable:
        if current_receivable > 0:
            lines.append(JournalLine(RECEIVABLE, credit=current_receivable))
        if current_payable < 0:
            lines.append(JournalLine(PAYABLE, debit=-current_payable))
        if target_receivable > 0:
            lines.append(JournalLine(RECEIVABLE, debit=target_receivable))
        if target_payable < 0:
            lines.append(JournalLine(PAYABLE, credit=-target_payable))

    if not lines:
        return None
    debit = sum(line.debit for line in lines)
    credit = sum(line.credit for line in lines)
    if debit > credit:
        lines.append(JournalLine(CAPITAL, credit=debit - credit))
    elif credit > debit:
        lines.append(JournalLine(CAPITAL, debit=credit - debit))

    source_event_id = "ls-paper-account:" + snapshot.content_hash
    journal_id = uuid5(NAMESPACE_URL, "hgfinance:" + source_event_id)
    return Journal(
        journal_id=journal_id,
        fund_id=ledger.fund_id,
        book_id=ledger.book_id,
        event_type="PAPER_BROKER_RECONCILIATION",
        source_event_id=source_event_id,
        effective_at=snapshot.observed_at,
        accounting_date=snapshot.observed_at.date(),
        lines=lines,
        created_by_service="accounting-ls-paper-reconciler",
        trace_id=str(journal_id),
        reason="Align internal PAPER ledger projection to LS PAPER broker snapshot",
        metadata={
            "content_hash": snapshot.content_hash,
            "canonical": True,
            "broker": "ls-paper",
            "account_masked": snapshot.account_masked,
            "cash_semantics": "Dps",
            "available_cash_semantics": "MnyOrdAbleAmt",
        },
    )


def _stable_for_alignment(repo: LedgerRepository, fund_id: UUID, book_id: UUID) -> bool:
    grace = max(1, int(os.environ.get("LS_PAPER_RECONCILE_GRACE_SECONDS", "10")))
    with repo.cursor() as cur:
        cur.execute(
            """
            select not exists (
                     select 1 from execution.user_directives
                      where fund_id=%s and book_id=%s
                        and state in ('RECEIVED','RUNNING','IN_PROGRESS','UNKNOWN')
                   )
               and not exists (
                     select 1
                       from execution.paper_user_directive_fills f
                       join execution.user_directives d
                         on d.directive_id=f.directive_id
                      where d.fund_id=%s and d.book_id=%s
                        and f.accounting_acknowledged_at is null
                   )
               and coalesce((
                     select max(created_at) < now()-(%s * interval '1 second')
                       from accounting.journals
                      where fund_id=%s and book_id=%s
                   ),true)
            """,
            (fund_id, book_id, fund_id, book_id, grace, fund_id, book_id),
        )
        return bool(cur.fetchone()[0])


def reconcile_once(repo: LedgerRepository, snapshot: BrokerAccountSnapshot) -> dict[str, Any]:
    selected = repo.default_book()
    if selected is None:
        raise LSPaperAlignmentError("accounting default PAPER book is not unique")
    fund_id, book_id = selected
    if not _stable_for_alignment(repo, fund_id, book_id):
        return {"status": "deferred", "reason": "ledger-or-order-activity"}
    instrument_ids: dict[str, UUID] = {}
    for position in snapshot.positions:
        instrument_id = repo.instrument_by_symbol(
            position.symbol, as_of=snapshot.observed_at
        )
        if instrument_id is None:
            raise LSPaperAlignmentError(
                f"broker symbol {position.symbol} has no canonical instrument"
            )
        instrument_ids[position.symbol] = instrument_id
    ledger = repo.load(fund_id, book_id)
    journal = build_alignment_journal(ledger, snapshot, instrument_ids)
    if journal is None:
        return {"status": "matched", "content_hash": snapshot.content_hash}
    ledger.post(journal)
    repo.save_projection(ledger)
    rebuilt = repo.load(fund_id, book_id)
    positions, cash = rebuilt.rebuild()
    expected = {
        instrument_ids[item.symbol]: (item.quantity, item.average_cost)
        for item in snapshot.positions
    }
    actual = {
        instrument_id: (position.quantity, position.average_cost)
        for instrument_id, position in positions.items()
    }
    if cash != snapshot.cash or actual != expected:
        raise LSPaperAlignmentError("ledger projection did not converge to broker snapshot")
    return {
        "status": "aligned",
        "journal_id": str(journal.journal_id),
        "content_hash": snapshot.content_hash,
        "position_count": len(expected),
        "cash": str(snapshot.cash),
        "buying_power": str(snapshot.buying_power),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    interval = max(10, int(os.environ.get("LS_PAPER_RECONCILE_INTERVAL_SECONDS", "60")))
    base_url = os.environ.get("PORTFOLIO_BFF_INTERNAL_URL", "http://portfolio-bff:8000")
    if args.healthcheck:
        repo = LedgerRepository.from_env(required=True)
        if repo is None:  # pragma: no cover - required=True is the contract
            raise LSPaperAlignmentError("durable accounting repository is unavailable")
        repo.close()
        probe_postgres(dsn_env="DATABASE_URL", role_env="ACCOUNTING_DATABASE_ROLE")
        probe_http(base_url.rstrip("/") + "/health/ready")
        return 0
    repo = LedgerRepository.from_env(required=True)
    assert repo is not None
    try:
        while True:
            try:
                snapshot = fetch_broker_snapshot(base_url)
                result = reconcile_once(repo, snapshot)
                if result.get("status") != "matched":
                    print(json.dumps(result, sort_keys=True), flush=True)
            except Exception as exc:  # fail visible; never report a synthetic match
                print(
                    json.dumps(
                        {"status": "error", "error": type(exc).__name__, "message": str(exc)[:240]},
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(interval)
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
