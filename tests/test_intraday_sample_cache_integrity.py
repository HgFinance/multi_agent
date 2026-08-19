from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
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

from intraday_microstructure import (EXTERNAL_EVENT_SOURCE, FeatureCubeSpec,
                                      HorizonLabel, IntradayLaneSpec,
                                      IntradaySample, IntradaySampleBatch,
                                      MultiScaleFeatureCube)  # noqa: E402
from intraday_sample_cache import (CACHE_VERSION, SampleCache,
                                   identity)  # noqa: E402


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


def batch(samples: tuple[IntradaySample, ...] | None = None) -> \
        IntradaySampleBatch:
    rows = (sample(),) if samples is None else samples
    spec = FeatureCubeSpec()
    index_blob = json.dumps([
        [row.instrument_id, row.decision_time.isoformat()] for row in rows
    ], separators=(",", ":"))
    decision_fingerprint = hashlib.sha256(
        index_blob.encode()).hexdigest()[:16]
    columns = tuple(
        (field, seconds, tuple(
            float(column_index + row_index)
            for row_index, _ in enumerate(rows)))
        for column_index, (field, seconds) in enumerate(
            (field, seconds)
            for field in spec.windowed_fields
            for seconds in spec.windows_seconds)
    )
    return IntradaySampleBatch(
        rows,
        MultiScaleFeatureCube(
            spec=spec,
            row_count=len(rows),
            decision_index_fingerprint=decision_fingerprint,
            columns=columns,
        ),
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


def test_v4_cache_persists_and_verifies_legacy_logical_payload_contract(
    tmp_path: Path,
) -> None:
    cache = SampleCache("a" * 64, root=tmp_path)
    samples = [sample()]

    assert cache.store("2026-08-14", "005930", samples) is True
    path = cache.path_for("2026-08-14", "005930")
    metadata = pq.read_table(path).schema.metadata or {}

    assert CACHE_VERSION == "intraday-discovery-sample-cache-v4"
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


def test_batch_cache_round_trips_nonempty_and_empty_complete_feature_cubes(
    tmp_path: Path,
) -> None:
    cache = SampleCache("d" * 64, root=tmp_path)
    populated = batch()

    assert cache.store_batch("2026-08-14", "005930", populated) is True
    loaded = cache.load_batch(
        "2026-08-14", "005930", expected_spec=FeatureCubeSpec())
    assert loaded == populated
    table = pq.read_table(cache.path_for("2026-08-14", "005930"))
    metadata = table.schema.metadata or {}
    manifest = json.loads(metadata[b"intraday_feature_cube_columns"])
    assert len(manifest) == (
        len(FeatureCubeSpec().windowed_fields)
        * len(FeatureCubeSpec().windows_seconds))
    assert all(item["parquet_column"] in table.column_names
               for item in manifest)
    assert metadata[b"evidence_authority"] == b"NONE"

    empty = batch(())
    assert cache.store_batch("2026-08-14", "000020", empty) is True
    empty_table = pq.read_table(cache.path_for("2026-08-14", "000020"))
    empty_manifest = json.loads(
        (empty_table.schema.metadata or {})[b"intraday_feature_cube_columns"])
    assert empty_table.num_rows == 0
    assert all(item["parquet_column"] in empty_table.column_names
               for item in empty_manifest)
    assert cache.load_batch(
        "2026-08-14", "000020", expected_spec=FeatureCubeSpec()) == empty


def test_batch_cache_identity_is_distinct_and_binds_feature_cube_contract() -> None:
    kwargs = {
        "spec": IntradayLaneSpec(horizons_seconds=(5,)),
        "event_source": EXTERNAL_EVENT_SOURCE,
        "execution_model": "TAKER",
        "source_lineage": [{"source": "ext_src.quotes", "rows": 10}],
    }
    legacy = identity(**kwargs)
    explicit = identity(**kwargs, feature_cube_spec=FeatureCubeSpec())
    assert legacy != explicit
    assert explicit == identity(
        **kwargs, feature_cube_spec=FeatureCubeSpec())


def test_batch_cache_rejects_tampered_cube_column(tmp_path: Path) -> None:
    cache = SampleCache("e" * 64, root=tmp_path)
    assert cache.store_batch("2026-08-14", "005930", batch()) is True
    path = cache.path_for("2026-08-14", "005930")
    metadata = pq.read_table(path).schema.metadata or {}
    column_name = json.loads(
        metadata[b"intraday_feature_cube_columns"])[0]["parquet_column"]

    def tamper(rows: list[dict]) -> None:
        rows[0][column_name] = 999.0

    rewrite(path, mutate_rows=tamper)
    assert cache.load_batch(
        "2026-08-14", "005930", expected_spec=FeatureCubeSpec()) is None


def test_batch_cache_rejects_tampered_spec(tmp_path: Path) -> None:
    cache = SampleCache("f" * 64, root=tmp_path)
    assert cache.store_batch("2026-08-14", "005930", batch()) is True
    path = cache.path_for("2026-08-14", "005930")

    def tamper(metadata: dict[bytes, bytes]) -> None:
        raw = json.loads(metadata[b"intraday_feature_cube_spec"])
        raw["boundary"] = "[decision-W,decision]"
        metadata[b"intraday_feature_cube_spec"] = json.dumps(
            raw, sort_keys=True, separators=(",", ":")).encode()

    rewrite(path, mutate_metadata=tamper)
    assert cache.load_batch(
        "2026-08-14", "005930", expected_spec=FeatureCubeSpec()) is None


@pytest.mark.parametrize(
    ("metadata_key", "metadata_value"),
    [
        (b"intraday_decision_index_fingerprint", b"tampered"),
        (b"intraday_logical_payload_fingerprint", b"0" * 64),
        (b"intraday_cache_payload_kind", b"LEGACY_SAMPLE_LIST_V1"),
    ],
)
def test_batch_cache_rejects_tampered_integrity_metadata(
    tmp_path: Path, metadata_key: bytes, metadata_value: bytes,
) -> None:
    cache = SampleCache("1" * 64, root=tmp_path)
    assert cache.store_batch("2026-08-14", "005930", batch()) is True
    path = cache.path_for("2026-08-14", "005930")

    def tamper(metadata: dict[bytes, bytes]) -> None:
        metadata[metadata_key] = metadata_value

    rewrite(path, mutate_metadata=tamper)
    assert cache.load_batch(
        "2026-08-14", "005930", expected_spec=FeatureCubeSpec()) is None
