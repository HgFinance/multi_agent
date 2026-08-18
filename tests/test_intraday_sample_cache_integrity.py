from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sys

import pytest


pytest.importorskip("pyarrow")
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

from intraday_microstructure import HorizonLabel, IntradaySample  # noqa: E402
from intraday_sample_cache import CACHE_VERSION, SampleCache  # noqa: E402


UTC = timezone.utc
AT = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)


def sample() -> IntradaySample:
    label = HorizonLabel(
        horizon_seconds=5,
        exit_time=AT + timedelta(seconds=5),
        future_mid=100.02,
        long_mid_markout_bps=2.0,
        short_mid_markout_bps=-2.0,
        long_taker_net_bps=0.5,
        short_taker_net_bps=-3.5,
        long_passive_filled=True,
        short_passive_filled=False,
        long_passive_fill_time=AT + timedelta(seconds=1),
        short_passive_fill_time=None,
        long_passive_net_bps=1.0,
        short_passive_net_bps=None,
    )
    return IntradaySample(
        instrument_id="005930",
        decision_time=AT,
        entry_time=AT + timedelta(milliseconds=100),
        source_quote_event_time=AT - timedelta(milliseconds=50),
        quote_age_ms=150.0,
        spread_bps=2.0,
        queue_imbalance_l1=0.2,
        queue_imbalance_l10=0.1,
        microprice_offset_bps=0.5,
        trade_flow_imbalance=0.3,
        quote_event_ofi=None,
        normalized_quote_ofi=None,
        bid_depth_l1=100.0,
        ask_depth_l1=80.0,
        book_depth_l1=180.0,
        book_depth_l10=1800.0,
        trade_count=2,
        quote_count=3,
        trade_intensity=0.4,
        realized_volatility_bps=1.2,
        entry_bid_depth_l1=100.0,
        entry_ask_depth_l1=80.0,
        entry_bid=100.0,
        entry_ask=100.02,
        entry_mid=100.01,
        labels=(label,),
    )


def rewrite(
    path: Path,
    *,
    mutate_rows=None,
    mutate_metadata=None,
) -> None:
    table = pq.read_table(path)
    rows = table.to_pylist()
    metadata = dict(table.schema.metadata or {})
    if mutate_rows is not None:
        mutate_rows(rows)
    if mutate_metadata is not None:
        mutate_metadata(metadata)
    rewritten = pa.Table.from_pylist(rows).replace_schema_metadata(metadata)
    pq.write_table(rewritten, path, compression="zstd")


def test_v3_cache_persists_and_verifies_logical_payload_contract(
    tmp_path: Path,
) -> None:
    cache = SampleCache("a" * 64, root=tmp_path)
    samples = [sample()]

    assert cache.store("2026-08-14", "005930", samples) is True
    path = cache.path_for("2026-08-14", "005930")
    metadata = pq.read_table(path).schema.metadata or {}

    assert CACHE_VERSION == "intraday-discovery-sample-cache-v3"
    assert metadata[b"intraday_cache_version"] == CACHE_VERSION.encode()
    assert metadata[b"sample_count"] == b"1"
    fingerprint = metadata[b"intraday_logical_payload_fingerprint"].decode()
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert cache.load("2026-08-14", "005930") == samples

    assert cache.store("2026-08-14", "000020", []) is True
    empty_path = cache.path_for("2026-08-14", "000020")
    empty_metadata = pq.read_table(empty_path).schema.metadata or {}
    assert empty_metadata[b"sample_count"] == b"0"
    assert empty_metadata[b"intraday_logical_payload_fingerprint"] == \
        hashlib.sha256(b"[]").hexdigest().encode()
    assert cache.load("2026-08-14", "000020") == []


def test_valid_parquet_with_tampered_logical_content_is_a_cache_miss(
    tmp_path: Path,
) -> None:
    cache = SampleCache("b" * 64, root=tmp_path)
    assert cache.store("2026-08-14", "005930", [sample()]) is True
    path = cache.path_for("2026-08-14", "005930")

    def tamper(rows: list[dict]) -> None:
        rows[0]["spread_bps"] = 999.0

    rewrite(path, mutate_rows=tamper)
    assert pq.read_table(path).to_pylist()[0]["spread_bps"] == 999.0
    assert cache.load("2026-08-14", "005930") is None


@pytest.mark.parametrize(
    ("metadata_key", "metadata_value"),
    [
        (b"intraday_cache_version", b"intraday-discovery-sample-cache-v2"),
        (b"sample_count", b"2"),
        (b"sample_count", b"01"),
        (b"intraday_logical_payload_fingerprint", b""),
    ],
)
def test_stale_or_inconsistent_valid_parquet_is_a_cache_miss(
    tmp_path: Path,
    metadata_key: bytes,
    metadata_value: bytes,
) -> None:
    cache = SampleCache("c" * 64, root=tmp_path)
    assert cache.store("2026-08-14", "005930", [sample()]) is True
    path = cache.path_for("2026-08-14", "005930")

    def alter(metadata: dict[bytes, bytes]) -> None:
        metadata[metadata_key] = metadata_value

    rewrite(path, mutate_metadata=alter)
    assert pq.read_table(path).num_rows == 1
    assert cache.load("2026-08-14", "005930") is None
