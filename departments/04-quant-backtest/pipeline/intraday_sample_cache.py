"""Bounded Parquet cache for the non-promoting intraday discovery panel.

The cache stores deterministic ``IntradaySample`` outputs and versioned
``IntradaySampleBatch`` feature cubes, not alpha scores or candidate decisions.
Its identity includes the complete lane specification, source lineage,
execution model and (for batches) feature-cube contract.  Confirmatory
all-universe evaluation continues to replay raw events and never treats this
cache as evidence.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from intraday_microstructure import (COMPLETED_SECOND_POLICY, EVENT_SOURCES,
                                      EXTERNAL_EVENT_SOURCE, FeatureCubeSpec,
                                      HorizonLabel, IntradayLaneSpec,
                                      IntradaySample, IntradaySampleBatch,
                                      LANE_VERSION, MultiScaleFeatureCube,
                                      RAW_EVENT_GRANULARITY,
                                      STRICT_TIMESTAMP_POLICY)


CACHE_VERSION = "intraday-discovery-sample-cache-v4"
DEFAULT_MAX_BYTES = 20 * 1024 ** 3
_LOGICAL_PAYLOAD_FINGERPRINT_KEY = \
    b"intraday_logical_payload_fingerprint"
_LEGACY_PAYLOAD_KIND = "LEGACY_SAMPLE_LIST_V1"
_BATCH_PAYLOAD_KIND = "EXPLICIT_WINDOW_SAMPLE_BATCH_V2"
_PAYLOAD_KIND_KEY = b"intraday_cache_payload_kind"
_CUBE_SPEC_KEY = b"intraday_feature_cube_spec"
_CUBE_COLUMNS_KEY = b"intraday_feature_cube_columns"
_DECISION_INDEX_FINGERPRINT_KEY = b"intraday_decision_index_fingerprint"
_CUBE_COLUMN_PREFIX = "__feature_cube__"
_SAMPLE_FIELDS = tuple(field.name for field in fields(IntradaySample)
                       if field.name != "labels")
_LABEL_DATETIME_FIELDS = (
    "exit_time", "long_passive_fill_time", "short_passive_fill_time",
    "long_passive_exit_time", "short_passive_exit_time",
)


def _canonical_logical_value(value):
    """Return the storage-independent JSON form used by cache integrity."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("intraday cache datetimes must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _canonical_logical_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_logical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("intraday cache values must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(
        f"unsupported intraday cache value: {type(value).__name__}")


def _logical_payload_fingerprint(samples: list[IntradaySample]) -> str:
    """Hash reconstructed samples, independent of Parquet byte encoding."""
    payload = [_canonical_logical_value(asdict(sample)) for sample in samples]
    blob = json.dumps(
        payload, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _batch_logical_payload_fingerprint(batch: IntradaySampleBatch) -> str:
    """Hash all batch semantics, independent of Parquet byte encoding."""
    payload = {
        "payload_kind": _BATCH_PAYLOAD_KIND,
        "samples": [
            _canonical_logical_value(asdict(sample))
            for sample in batch.samples
        ],
        "feature_cube": {
            "spec": _canonical_logical_value(batch.feature_cube.spec.as_dict()),
            "row_count": batch.feature_cube.row_count,
            "decision_index_fingerprint": (
                batch.feature_cube.decision_index_fingerprint),
            "columns": [
                [field, seconds, _canonical_logical_value(values)]
                for field, seconds, values in batch.feature_cube.columns
            ],
        },
    }
    blob = json.dumps(
        payload, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cube_column_name(field: str, seconds: int) -> str:
    return f"{_CUBE_COLUMN_PREFIX}{field}__{int(seconds)}s"


def _cube_column_manifest(spec: FeatureCubeSpec) -> list[dict]:
    return [
        {
            "field": field,
            "seconds": seconds,
            "parquet_column": _cube_column_name(field, seconds),
        }
        for field in spec.windowed_fields
        for seconds in spec.windows_seconds
    ]


def _canonical_json(value) -> str:
    return json.dumps(
        _canonical_logical_value(value), allow_nan=False,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_canonical_json(metadata: dict, key: bytes):
    raw = metadata.get(key, b"")
    if not raw:
        raise ValueError(f"missing intraday cache metadata {key!r}")
    value = json.loads(raw.decode())
    if raw.decode() != _canonical_json(value):
        raise ValueError(f"non-canonical intraday cache metadata {key!r}")
    return value


def identity(*, spec: IntradayLaneSpec, event_source: str,
             execution_model: str, source_lineage,
             timestamp_policy: str | None = None,
             feature_cube_spec: FeatureCubeSpec | None = None) -> str:
    normalized_source = str(event_source).upper()
    if normalized_source not in EVENT_SOURCES:
        raise ValueError(
            "intraday sample cache accepts raw quote/trade event sources only; "
            f"received {event_source!r}")
    policy = str(timestamp_policy or (
        COMPLETED_SECOND_POLICY
        if normalized_source == EXTERNAL_EVENT_SOURCE else
        STRICT_TIMESTAMP_POLICY)).upper()
    payload = {
        "cache_version": CACHE_VERSION,
        "lane_version": LANE_VERSION,
        "spec": asdict(spec),
        "event_source": normalized_source,
        "source_granularity": RAW_EVENT_GRANULARITY,
        "execution_model": str(execution_model),
        "timestamp_policy": policy,
        "clock_aggregation_version": (
            "completed-second-state-median-taker-envelope-v1"
            if policy == COMPLETED_SECOND_POLICY else None),
        "execution_contract": (
            "conditional-one-share-max-ask-min-bid-v1"
            if policy == COMPLETED_SECOND_POLICY else
            "visible-snapshot-depth-v1"),
        "source_lineage": source_lineage,
        "sample_payload_contract": (
            _BATCH_PAYLOAD_KIND if feature_cube_spec is not None else
            _LEGACY_PAYLOAD_KIND),
        "feature_cube_spec": (
            feature_cube_spec.as_dict()
            if feature_cube_spec is not None else None),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def default_root() -> Path:
    configured = os.environ.get("INTRADAY_SAMPLE_CACHE_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "quant-data" / \
        "intraday-discovery-cache"


def _safe_token(value: str, name: str) -> str:
    text = str(value)
    if not text or any(char not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
                       for char in text):
        raise ValueError(f"unsafe {name} for intraday cache: {value!r}")
    return text


class SampleCache:
    """Read/write exact discovery samples with atomic file replacement."""

    def __init__(self, cache_identity: str, *, root: Path | None = None):
        self.identity = _safe_token(cache_identity, "identity")
        self.root = (root or default_root()).resolve()
        self.namespace = self.root / CACHE_VERSION / self.identity

    def path_for(self, day, instrument_id: str) -> Path:
        day_token = _safe_token(str(day), "session")
        instrument = _safe_token(instrument_id, "instrument")
        return self.namespace / day_token / f"{instrument}.parquet"

    @staticmethod
    def _pyarrow():
        try:
            import pyarrow as pa  # noqa: PLC0415
            import pyarrow.parquet as pq  # noqa: PLC0415
        except ImportError:
            return None, None
        return pa, pq

    def _validated_table(self, day, instrument_id: str, *, payload_kind: str):
        pa, pq = self._pyarrow()
        if pa is None:
            return None
        path = self.path_for(day, instrument_id)
        if not path.is_file():
            return None
        table = pq.read_table(path)
        metadata = table.schema.metadata or {}
        if metadata.get(b"intraday_cache_identity", b"").decode() != \
                self.identity:
            return None
        if metadata.get(b"intraday_cache_version", b"").decode() != \
                CACHE_VERSION:
            return None
        if metadata.get(b"intraday_source_granularity", b"").decode() != \
                RAW_EVENT_GRANULARITY:
            return None
        if metadata.get(b"evidence_authority", b"").decode() != "NONE":
            return None
        if metadata.get(_PAYLOAD_KIND_KEY, b"").decode() != payload_kind:
            return None
        raw_count = metadata.get(b"sample_count", b"")
        if not raw_count.isdigit():
            return None
        sample_count = int(raw_count)
        if raw_count != str(sample_count).encode() or \
                table.num_rows != sample_count:
            return None
        empty = metadata.get(b"empty", b"")
        if empty not in {b"0", b"1"} or \
                (empty == b"1") != (sample_count == 0):
            return None
        return table, metadata, sample_count

    @staticmethod
    def _samples_from_table(table) -> list[IntradaySample]:
        if table.num_rows == 0:
            return []
        out = []
        sample_table = table.select([*_SAMPLE_FIELDS, "labels_json"])
        for row in sample_table.to_pylist():
            labels = []
            for raw in json.loads(row["labels_json"]):
                for key in _LABEL_DATETIME_FIELDS:
                    if raw.get(key) is not None:
                        raw[key] = datetime.fromisoformat(raw[key])
                labels.append(HorizonLabel(**raw))
            out.append(IntradaySample(
                **{key: row[key] for key in _SAMPLE_FIELDS},
                labels=tuple(labels)))
        return out

    @staticmethod
    def _sample_rows(samples) -> list[dict]:
        rows = []
        for sample in samples:
            row = {key: getattr(sample, key) for key in _SAMPLE_FIELDS}
            labels = []
            for label in sample.labels:
                raw = asdict(label)
                for key in _LABEL_DATETIME_FIELDS:
                    if raw.get(key) is not None:
                        raw[key] = raw[key].isoformat()
                labels.append(raw)
            row["labels_json"] = json.dumps(
                labels, sort_keys=True, separators=(",", ":"))
            rows.append(row)
        return rows

    def _write_table(self, path: Path, table, pq) -> bool:
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent,
            delete=False)
        temporary = Path(handle.name)
        handle.close()
        try:
            pq.write_table(table, temporary, compression="zstd",
                           use_dictionary=True)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def load(self, day, instrument_id: str) -> list[IntradaySample] | None:
        try:
            validated = self._validated_table(
                day, instrument_id, payload_kind=_LEGACY_PAYLOAD_KIND)
            if validated is None:
                return None
            table, metadata, _ = validated
            out = self._samples_from_table(table)
            expected_fingerprint = metadata.get(
                _LOGICAL_PAYLOAD_FINGERPRINT_KEY, b"").decode()
            if expected_fingerprint != _logical_payload_fingerprint(out):
                return None
            return out
        except (KeyError, OSError, UnicodeDecodeError, ValueError, TypeError,
                json.JSONDecodeError):
            # A partial/corrupt optimization artifact is a cache miss, never a
            # scientific failure. It will be atomically replaced on the miss.
            return None

    def store(self, day, instrument_id: str,
              samples: list[IntradaySample]) -> bool:
        pa, pq = self._pyarrow()
        if pa is None:
            return False
        path = self.path_for(day, instrument_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._sample_rows(samples)
        table = (pa.Table.from_pylist(rows) if rows else
                 pa.table({"_empty": pa.array([], type=pa.bool_())}))
        logical_payload_fingerprint = _logical_payload_fingerprint(samples)
        metadata = dict(table.schema.metadata or {})
        metadata.update({
            b"intraday_cache_identity": self.identity.encode(),
            b"intraday_cache_version": CACHE_VERSION.encode(),
            b"intraday_source_granularity": RAW_EVENT_GRANULARITY.encode(),
            b"evidence_authority": b"NONE",
            _PAYLOAD_KIND_KEY: _LEGACY_PAYLOAD_KIND.encode(),
            b"empty_semantics": (
                b"DERIVATION_PRODUCED_NO_SAMPLES_NOT_SOURCE_EMPTY"),
            b"sample_count": str(len(rows)).encode(),
            _LOGICAL_PAYLOAD_FINGERPRINT_KEY: (
                logical_payload_fingerprint.encode()),
            b"empty": b"1" if not rows else b"0",
        })
        table = table.replace_schema_metadata(metadata)
        return self._write_table(path, table, pq)

    def load_batch(
            self, day, instrument_id: str, *,
            expected_spec: FeatureCubeSpec) -> IntradaySampleBatch | None:
        """Load an explicit-window discovery batch or fail closed to a miss."""
        try:
            validated = self._validated_table(
                day, instrument_id, payload_kind=_BATCH_PAYLOAD_KIND)
            if validated is None:
                return None
            table, metadata, sample_count = validated
            raw_spec = _decode_canonical_json(metadata, _CUBE_SPEC_KEY)
            spec = FeatureCubeSpec(
                version=raw_spec["version"],
                feature_window_contract_version=raw_spec[
                    "feature_window_contract_version"],
                windows_seconds=tuple(raw_spec["windows_seconds"]),
                windowed_fields=tuple(raw_spec["windowed_fields"]),
                boundary=raw_spec["boundary"],
            )
            if spec != expected_spec:
                return None
            manifest = _decode_canonical_json(metadata, _CUBE_COLUMNS_KEY)
            expected_manifest = _cube_column_manifest(spec)
            if manifest != expected_manifest:
                return None
            cube_column_names = {
                str(item["parquet_column"]) for item in expected_manifest}
            if any(name.startswith(_CUBE_COLUMN_PREFIX)
                   and name not in cube_column_names
                   for name in table.column_names):
                return None
            if not cube_column_names.issubset(table.column_names):
                return None
            samples = self._samples_from_table(table)
            decision_fingerprint = metadata.get(
                _DECISION_INDEX_FINGERPRINT_KEY, b"").decode()
            if not decision_fingerprint:
                return None
            columns = tuple(
                (str(item["field"]), int(item["seconds"]), tuple(
                    table.column(str(item["parquet_column"])).to_pylist()))
                for item in expected_manifest
            )
            cube = MultiScaleFeatureCube(
                spec=spec,
                row_count=sample_count,
                decision_index_fingerprint=decision_fingerprint,
                columns=columns,
            )
            batch = IntradaySampleBatch(tuple(samples), cube)
            expected_fingerprint = metadata.get(
                _LOGICAL_PAYLOAD_FINGERPRINT_KEY, b"").decode()
            if expected_fingerprint != _batch_logical_payload_fingerprint(batch):
                return None
            return batch
        except (KeyError, OSError, UnicodeDecodeError, ValueError, TypeError,
                json.JSONDecodeError):
            return None

    def store_batch(self, day, instrument_id: str,
                    batch: IntradaySampleBatch) -> bool:
        """Atomically persist samples and the complete explicit-window cube."""
        if not isinstance(batch, IntradaySampleBatch):
            raise TypeError("intraday batch cache requires IntradaySampleBatch")
        pa, pq = self._pyarrow()
        if pa is None:
            return False
        path = self.path_for(day, instrument_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._sample_rows(batch.samples)
        manifest = _cube_column_manifest(batch.feature_cube.spec)
        for item, (_, _, values) in zip(
                manifest, batch.feature_cube.columns, strict=True):
            column_name = str(item["parquet_column"])
            for row, value in zip(rows, values, strict=True):
                row[column_name] = value
        if rows:
            table = pa.Table.from_pylist(rows)
        else:
            empty_columns = {"_empty": pa.array([], type=pa.bool_())}
            empty_columns.update({
                str(item["parquet_column"]): pa.array([])
                for item in manifest
            })
            table = pa.table(empty_columns)
        metadata = dict(table.schema.metadata or {})
        metadata.update({
            b"intraday_cache_identity": self.identity.encode(),
            b"intraday_cache_version": CACHE_VERSION.encode(),
            b"intraday_source_granularity": RAW_EVENT_GRANULARITY.encode(),
            b"evidence_authority": b"NONE",
            _PAYLOAD_KIND_KEY: _BATCH_PAYLOAD_KIND.encode(),
            _CUBE_SPEC_KEY: _canonical_json(
                batch.feature_cube.spec.as_dict()).encode(),
            _CUBE_COLUMNS_KEY: _canonical_json(manifest).encode(),
            _DECISION_INDEX_FINGERPRINT_KEY: (
                batch.feature_cube.decision_index_fingerprint.encode()),
            b"empty_semantics": (
                b"DERIVATION_PRODUCED_NO_SAMPLES_NOT_SOURCE_EMPTY"),
            b"sample_count": str(len(batch)).encode(),
            _LOGICAL_PAYLOAD_FINGERPRINT_KEY: (
                _batch_logical_payload_fingerprint(batch).encode()),
            b"empty": b"1" if not batch else b"0",
        })
        table = table.replace_schema_metadata(metadata)
        return self._write_table(path, table, pq)


def prune(*, root: Path | None = None,
          max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Evict oldest cache files only inside the dedicated cache directory."""
    target = (root or default_root()).resolve() / CACHE_VERSION
    if max_bytes < 1:
        raise ValueError("intraday cache max_bytes must be positive")
    if not target.is_dir():
        return {"files": 0, "bytes": 0, "removed": 0}
    files = [path for path in target.rglob("*.parquet") if path.is_file()]
    rows = sorted(((path.stat().st_mtime_ns, path.stat().st_size, path)
                   for path in files), key=lambda row: row[0])
    total = sum(row[1] for row in rows)
    removed = 0
    # Leave headroom so the next discovery panel does not immediately prune.
    goal = int(max_bytes * 0.90)
    for _, size, path in rows:
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
        if total <= goal:
            break
    return {"files": len(rows) - removed, "bytes": total,
            "removed": removed}
