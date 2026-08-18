"""Bounded Parquet cache for the non-promoting intraday discovery panel.

The cache stores deterministic ``IntradaySample`` outputs, not alpha scores or
candidate decisions.  Its identity includes the complete lane specification,
source lineage and execution model.  Confirmatory all-universe evaluation
continues to replay raw events and never treats this cache as evidence.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile

from intraday_microstructure import (HorizonLabel, IntradayLaneSpec,
                                      IntradaySample, LANE_VERSION)


CACHE_VERSION = "intraday-discovery-sample-cache-v1"
DEFAULT_MAX_BYTES = 20 * 1024 ** 3
_SAMPLE_FIELDS = tuple(field.name for field in fields(IntradaySample)
                       if field.name != "labels")


def identity(*, spec: IntradayLaneSpec, event_source: str,
             execution_model: str, source_lineage) -> str:
    payload = {
        "cache_version": CACHE_VERSION,
        "lane_version": LANE_VERSION,
        "spec": asdict(spec),
        "event_source": str(event_source),
        "execution_model": str(execution_model),
        "source_lineage": source_lineage,
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

    def load(self, day, instrument_id: str) -> list[IntradaySample] | None:
        pa, pq = self._pyarrow()
        if pa is None:
            return None
        path = self.path_for(day, instrument_id)
        if not path.is_file():
            return None
        try:
            table = pq.read_table(path)
            metadata = table.schema.metadata or {}
            if metadata.get(b"intraday_cache_identity", b"").decode() != \
                    self.identity:
                return None
            if metadata.get(b"empty", b"0") == b"1":
                return []
            out = []
            for row in table.to_pylist():
                labels = []
                for raw in json.loads(row.pop("labels_json")):
                    for key in ("exit_time", "long_passive_fill_time",
                                "short_passive_fill_time"):
                        if raw.get(key) is not None:
                            raw[key] = datetime.fromisoformat(raw[key])
                    labels.append(HorizonLabel(**raw))
                out.append(IntradaySample(
                    **{key: row[key] for key in _SAMPLE_FIELDS},
                    labels=tuple(labels)))
            return out
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
        rows = []
        for sample in samples:
            row = {key: getattr(sample, key) for key in _SAMPLE_FIELDS}
            labels = []
            for label in sample.labels:
                raw = asdict(label)
                for key in ("exit_time", "long_passive_fill_time",
                            "short_passive_fill_time"):
                    if raw[key] is not None:
                        raw[key] = raw[key].isoformat()
                labels.append(raw)
            row["labels_json"] = json.dumps(
                labels, sort_keys=True, separators=(",", ":"))
            rows.append(row)
        table = (pa.Table.from_pylist(rows) if rows else
                 pa.table({"_empty": pa.array([], type=pa.bool_())}))
        metadata = dict(table.schema.metadata or {})
        metadata.update({
            b"intraday_cache_identity": self.identity.encode(),
            b"intraday_cache_version": CACHE_VERSION.encode(),
            b"empty": b"1" if not rows else b"0",
        })
        table = table.replace_schema_metadata(metadata)
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
