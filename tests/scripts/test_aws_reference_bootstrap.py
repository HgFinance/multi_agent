from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "aws_reference_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("aws_reference_bootstrap", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


@dataclass(frozen=True)
class _Session:
    trade_date: date
    is_trading_day: bool


@dataclass(frozen=True)
class _Draft:
    market: str
    content_hash: str
    sessions: tuple[_Session, ...]


class _Repository:
    def __init__(self) -> None:
        self.hashes: set[str] = set()
        self.calendar_calls: list[str] = []
        self.instruments: set[str] = set()

    def ingest_instruments(self, records, *, provider, as_of):
        assert provider == "LS"
        before = len(self.instruments)
        self.instruments.update(record.symbol for record in records)
        return SimpleNamespace(
            attempted=len(records),
            instruments_inserted=len(self.instruments) - before,
        )

    def upsert_calendar(self, draft):
        self.calendar_calls.append(draft.content_hash)
        created = draft.content_hash not in self.hashes
        self.hashes.add(draft.content_hash)
        return "version", len(draft.sessions) if created else 0, created


def _fake_modules() -> bootstrap.ReferenceModules:
    start = date(2026, 1, 1)
    today = date(2026, 8, 18)
    observed_sessions = tuple(
        _Session(start + timedelta(days=offset), True)
        for offset in range((today - start).days)
    )
    declared_sessions = observed_sessions + (_Session(today, True),)
    return bootstrap.ReferenceModules(
        fetch_stock_master=lambda _client: ([SimpleNamespace(symbol="005930")], 0),
        master_row_to_record=lambda row, *, as_of: SimpleNamespace(
            symbol=row.symbol, observed_at=as_of
        ),
        collect_calendar=lambda _client, *, start, end: _Draft(
            "KRX", "observed", observed_sessions
        ),
        build_declared_draft=lambda: _Draft(
            "KRX", "declared", declared_sessions
        ),
        verify_against_observed=lambda _draft, observed: (len(observed), 160),
        declared_from=start,
        declared_through=date(2026, 12, 31),
    )


def test_reference_catalog_and_calendar_are_idempotent(monkeypatch) -> None:
    repository = _Repository()
    modules = _fake_modules()
    monkeypatch.setattr(
        bootstrap,
        "_declared_calendar_exists",
        lambda repo, draft: draft.content_hash in repo.hashes,
    )

    for _ in range(2):
        rows, unclassified = bootstrap.ingest_instrument_master(
            client=object(),
            repository=repository,
            modules=modules,
            observed_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )
        overlap, trading_days = bootstrap.reconcile_verified_calendar(
            client=object(),
            repository=repository,
            modules=modules,
            today=date(2026, 8, 18),
        )
        assert (rows, unclassified) == (1, 0)
        assert overlap > 0 and trading_days == 160

    assert repository.instruments == {"005930"}
    # The bounded observed version is installed only once; the complete
    # declared version remains idempotent and latest-version readers stay safe.
    assert repository.calendar_calls == ["observed", "declared", "declared"]


def test_calendar_refuses_unreviewed_year() -> None:
    with pytest.raises(
        bootstrap.ReferenceBootstrapError,
        match="no reviewed declared calendar",
    ):
        bootstrap.reconcile_verified_calendar(
            client=object(),
            repository=_Repository(),
            modules=_fake_modules(),
            today=date(2027, 1, 2),
        )


def test_database_roles_require_distinct_databases_in_same_cluster(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HEDGEFUND_CONTROL_DB_NAME", "control")
    identities = {
        "control-dsn": ("control", "cluster-1"),
        "market-dsn": ("market", "cluster-1"),
    }
    monkeypatch.setattr(
        bootstrap, "_database_identity", lambda dsn: identities[dsn]
    )
    bootstrap.assert_database_roles("control-dsn", "market-dsn")

    identities["market-dsn"] = ("market", "cluster-2")
    with pytest.raises(bootstrap.ReferenceBootstrapError, match="one private cluster"):
        bootstrap.assert_database_roles("control-dsn", "market-dsn")


def test_script_has_no_order_or_broker_execution_surface() -> None:
    source = (bootstrap.ROOT / "scripts" / "aws_reference_bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "TRADING_BROKER_ADAPTER" not in source
    assert "submit_user_directive" not in source
    assert "place_order(" not in source


def test_readiness_counts_all_canonical_krx_stock_codes() -> None:
    source = (bootstrap.ROOT / "scripts" / "aws_reference_bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "sy.symbol ~ '^[0-9A-Z]{6}$'" in source
    assert "sy.symbol ~ '^[0-9]{6}$'" not in source


def test_main_sanitizes_driver_or_vendor_exception_details(
    monkeypatch, capsys
) -> None:
    secret = "do-not-print-this-dsn-password"
    monkeypatch.setenv(
        "CONTROL_DATABASE_URL", f"postgresql://postgres:{secret}@db/control"
    )
    monkeypatch.setenv(
        "MARKET_DATABASE_URL", f"postgresql://postgres:{secret}@db/market"
    )
    monkeypatch.setattr(
        bootstrap,
        "assert_database_roles",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    assert bootstrap.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "RuntimeError" in captured.err
