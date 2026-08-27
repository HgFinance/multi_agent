from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apps.api.strategy_paper_executor import (
    PaperSignalRuntime,
    _aggregate_3m,
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
