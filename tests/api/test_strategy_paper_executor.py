from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from apps.api.strategy_paper_executor import (
    PaperSignalRuntime,
    _aggregate_3m,
    _aggregate_tick_rows_3m,
    _has_sufficient_bars,
    _read_bundle,
    _sma,
)


def _minute_rows(count: int) -> list[dict[str, object]]:
    start = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "bucket_time": (start + timedelta(minutes=index)).isoformat(),
            "open": str(index + 1),
            "high": str(index + 1.5),
            "low": str(index + 0.5),
            "close": str(index + 1),
            "is_final": True,
        }
        for index in range(count)
    ]


def test_aggregate_3m_requires_three_complete_one_minute_rows() -> None:
    rows = _minute_rows(7)
    bars = _aggregate_3m(rows)

    assert len(bars) == 2
    assert bars[0]["open"] == "1"
    assert bars[0]["close"] == "3"
    assert bars[0]["high"] == "3.5"
    assert _sma([1, 2, 3, 4, 5], 5) == 3


def test_aggregate_3m_rejects_duplicate_or_missing_minutes() -> None:
    rows = _minute_rows(3)
    assert len(_aggregate_3m([rows[0], rows[1], rows[1]])) == 0
    assert len(_aggregate_3m([rows[0], rows[2], rows[2]])) == 0


def test_sma_window_accepts_valid_observation_bars_across_session_gaps() -> None:
    rows = _minute_rows(180)
    rows[90:180] = [
        {**row, "bucket_time": (datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()}
        for index, row in enumerate(rows[90:180])
    ]
    bars = _aggregate_3m(rows)

    assert len(bars) == 60
    assert _has_sufficient_bars(bars, 60)


def test_tick_aggregation_uses_real_trades_without_forward_filling() -> None:
    rows = [
        {"event_time": "2026-08-27T00:00:10+00:00", "price": "100", "quantity": "2"},
        {"event_time": "2026-08-27T00:02:20+00:00", "price": "103", "quantity": "1"},
        # The 00:03 bucket has one real trade and is still a valid observation.
        {"event_time": "2026-08-27T00:03:10+00:00", "price": "102", "quantity": "4"},
    ]

    bars = _aggregate_tick_rows_3m(
        rows, now=datetime(2026, 8, 27, 0, 10, tzinfo=timezone.utc)
    )

    assert [bar["bucket_time"] for bar in bars] == [
        "2026-08-27T00:00:00+00:00",
        "2026-08-27T00:03:00+00:00",
    ]
    assert bars[0]["open"] == "100"
    assert bars[0]["high"] == "103"
    assert bars[0]["close"] == "103"
    assert bars[0]["volume"] == "3"
    assert bars[1]["trade_count"] == 1


def test_bundle_hash_and_sma_runtime_emit_one_signal_only(tmp_path: Path, monkeypatch) -> None:
    bundle = {
        "schema": "autonomous-strategy-paper-bundle.v1",
        "bundle_version": "sma-alignment-3m-v1",
        "deployment_id": "deployment-0123456789abcdef01234567",
        "request_id": "research-runtime-02",
        "symbols": ["000660"],
        "mode": "PAPER",
        "strategy": {
            "kind": "SMA_ALIGNMENT",
            "timeframe": "3M",
            "fast": 5,
            "mid": 20,
            "slow": 60,
        },
        "execution": {"orders_enabled": False, "signal_only": True},
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    expected_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert _read_bundle(bundle_path, expected_hash)["mode"] == "PAPER"

    monkeypatch.setattr(
        "apps.api.strategy_paper_executor._fetch_1m",
        lambda *_args, **_kwargs: _minute_rows(180),
    )
    runtime = PaperSignalRuntime(bundle, state_dir=tmp_path / "state", market_api="http://market-api:8036")
    runtime.poll_once()

    assert runtime.state["status"] == "RUNNING"
    assert runtime.state["execution_status"] == "SIGNAL_ONLY"
    assert runtime.state["signals_generated"] == 1
    assert runtime.state["last_signal"]["action"] == "BUY"
    assert (tmp_path / "state" / f"{bundle['deployment_id']}.signals.jsonl").exists()

    restarted = PaperSignalRuntime(
        bundle, state_dir=tmp_path / "state", market_api="http://market-api:8036"
    )
    restarted.poll_once()
    assert restarted.state["signals_generated"] == 1
    assert len(
        (tmp_path / "state" / f"{bundle['deployment_id']}.signals.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ) == 1


def test_paper_ordering_bundle_submits_one_idempotent_order_per_signal(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = {
        "schema": "autonomous-strategy-paper-bundle.v1",
        "bundle_version": "sma-alignment-3m-v1",
        "deployment_id": "deployment-0123456789abcdef01234567",
        "request_id": "research-runtime-03",
        "symbols": ["000660"],
        "mode": "PAPER",
        "strategy": {
            "kind": "SMA_ALIGNMENT",
            "timeframe": "3M",
            "fast": 5,
            "mid": 20,
            "slow": 60,
        },
        "execution": {
            "orders_enabled": True,
            "signal_only": False,
            "order_quantity": "1",
            "trading_route": "strategy-runtime-control -> Trading PAPER directive -> LS PAPER",
        },
    }
    calls: list[dict[str, object]] = []

    class Gateway:
        def submit(self, **kwargs):
            calls.append(kwargs)
            return {
                "execution_status": "PAPER_ORDER_SUBMITTED",
                "directive": {
                    "directive_id": "directive-1",
                    "state": "IN_PROGRESS",
                    "legs": [{"state": "ACKNOWLEDGED"}],
                },
            }

    monkeypatch.setattr(
        "apps.api.strategy_paper_executor._fetch_1m",
        lambda *_args, **_kwargs: _minute_rows(180),
    )
    runtime = PaperSignalRuntime(
        bundle,
        state_dir=tmp_path / "state",
        market_api="http://market-api:8036",
        order_gateway=Gateway(),
    )
    runtime.poll_once()
    runtime.poll_once()

    assert runtime.state["execution_status"] == "PAPER_ORDERING"
    assert runtime.state["orders_enabled"] is True
    assert len(calls) == 1
    assert calls[0]["side"] == "BUY"
    assert calls[0]["quantity"] == Decimal("1")
    event = json.loads(
        (tmp_path / "state" / f"{bundle['deployment_id']}.signals.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert event["order"]["directive_id"] == "directive-1"
