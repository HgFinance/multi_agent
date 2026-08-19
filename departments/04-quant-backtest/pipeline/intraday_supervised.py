"""Frozen, cost-aware supervised control for the intraday AST factory.

The control is deliberately small and auditable.  It is not an alternative
promotion path and it never fits on evaluation rows.  A fixed public feature
set is mapped to three labels on sessions strictly preceding the evaluated
slice:

* future mid-price markout (continuous),
* executable net bps per opportunity (continuous; a passive non-fill is zero),
* whether the opportunity produces positive executable net bps (binary).

The resulting ridge model is a *teacher/control*.  The factory compares an AST,
the teacher, and an AST-gated teacher on the same replay.  Only an independently
frozen future experiment may turn one of those comparisons into evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import struct
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import numpy as np

from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    LEGACY_FEATURE_WINDOW_CONTRACT,
    PRIMITIVE_WINDOWS_SECONDS,
    STATE_FIELDS,
    WINDOWED_FIELDS,
)
from intraday_microstructure import (
    FEATURE_CUBE_BOUNDARY,
    FEATURE_CUBE_VERSION,
    IntradaySample,
)


TEACHER_VERSION = "krx-cost-aware-linear-teacher-v3"
EXPLICIT_WINDOW_TEACHER_VERSION = "krx-cost-aware-linear-teacher-v4"
LABEL_VERSION = "krx-executable-opportunity-label-v2"
RIDGE_FRACTION = 0.05
MIN_OBSERVATIONS = 1_000
MIN_INSTRUMENTS = 2
# The imported store currently has 61 sessions and the governed release gate
# consumes 60 as OOS, leaving one strictly prior calibration session.  The
# teacher is a diagnostic control only, so one session may fit it when there are
# many instruments/rows; its report marks that temporal breadth explicitly and
# it can never promote a candidate.
MIN_SESSIONS = 1
CALIBRATION_ATTESTATION_VERSION = "calibration-attestation-v1"
CALIBRATION_ROW_DIGEST_VERSION = "calibration-row-multiset-sha256-v1"

# Public, causally observable controls.  Optional v2 microstructure fields are
# read with getattr so an older frozen sample-cache remains readable.  Missing
# values get an explicit indicator instead of being silently treated as facts.
BASE_FEATURES = (
    "queue_imbalance_l1",
    "queue_imbalance_l10",
    "microprice_offset_bps",
    "trade_flow_imbalance",
    "normalized_quote_ofi",
    "spread_bps",
    "book_depth_l1",
    "book_depth_l10",
    "trade_count",
    "quote_count",
    "trade_intensity",
    "realized_volatility_bps",
    "quote_age_ms",
    "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10",
    "depth_imbalance_slope",
    "quote_ofi_depth_divergence",
    "quote_event_transition_count",
    "normalized_quote_ofi_per_event",
    "signed_trade_volume",
    "trade_volume",
    "trade_side_known_ratio",
    "quote_ofi_per_trade_volume",
)
LOG1P_FEATURES = frozenset({
    "book_depth_l1", "book_depth_l10", "trade_count", "quote_count",
    "trade_intensity", "quote_age_ms", "quote_event_transition_count",
    "signed_trade_volume", "trade_volume",
})

# The explicit-window teacher is deliberately a different frozen model
# contract.  State fields occur once at the decision snapshot; event-derived
# fields occur at every primitive window.  Every raw value has its own missing
# indicator, so a missing short window cannot masquerade as an observed zero.
EXPLICIT_LOG1P_FIELDS = LOG1P_FEATURES | frozenset({
    "bid_depth_l1", "ask_depth_l1", "quote_event_ofi",
    "multi_level_quote_ofi_l10",
})


def explicit_raw_feature_names() -> tuple[str, ...]:
    state = tuple(f"state:{field}" for field in sorted(STATE_FIELDS))
    windowed = tuple(
        f"window:{field}@{seconds}s"
        for field in sorted(WINDOWED_FIELDS)
        for seconds in PRIMITIVE_WINDOWS_SECONDS
    )
    return (*state, *windowed)


def explicit_feature_names() -> tuple[str, ...]:
    raw = explicit_raw_feature_names()
    return (*raw, *(f"{name}__missing" for name in raw))


def explicit_feature_cube_spec_hash() -> str:
    payload = json.dumps({
        "version": FEATURE_CUBE_VERSION,
        "feature_window_contract_version": EXPLICIT_FEATURE_WINDOW_CONTRACT,
        "windows_seconds": PRIMITIVE_WINDOWS_SECONDS,
        "windowed_fields": sorted(WINDOWED_FIELDS),
        "boundary": FEATURE_CUBE_BOUNDARY,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def explicit_feature_spec_hash() -> str:
    payload = json.dumps({
        "version": EXPLICIT_WINDOW_TEACHER_VERSION,
        "feature_window_contract_version": EXPLICIT_FEATURE_WINDOW_CONTRACT,
        "feature_cube_spec_hash": explicit_feature_cube_spec_hash(),
        "features": explicit_feature_names(),
        "log1p": sorted(EXPLICIT_LOG1P_FIELDS),
        "ridge_fraction": RIDGE_FRACTION,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _canonical_hash(value) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_list(values: Iterable[str], *, field: str) -> list[str]:
    rows = [str(value) for value in values]
    if not rows or rows != sorted(set(rows)):
        raise ValueError(f"{field} must be a non-empty sorted unique list")
    return rows


def _identity_list_hash(values: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(
        list(values), separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


def attach_calibration_attestation(
        report: dict, *, planned_session_ids: Sequence[str],
        planned_instruments: Sequence[str], sample_contract: dict,
        source_contract: dict, calibration_evidence: dict) -> dict:
    """Attach a MODEL_CANDIDATE-only proof without changing legacy hashes.

    ``model_fingerprint`` deliberately remains the v3/v4 byte contract used by
    historical AST and forward artifacts.  The sidecar binds all calibration
    membership, statistics, row content and source clocks needed by the new
    independent model lane.  Legacy readers may ignore it; MODEL_CANDIDATE
    verifies it with :func:`verify_calibration_attestation`.
    """
    frozen = json.loads(json.dumps(
        report or {}, sort_keys=True, separators=(",", ":"), allow_nan=False))
    if str(frozen.get("status") or "").upper() != "PASS":
        raise ValueError("only a usable frozen teacher can be attested")
    planned_sessions = _identity_list(
        planned_session_ids, field="planned calibration sessions")
    planned = _identity_list(
        planned_instruments, field="planned calibration instruments")
    contributing_sessions = _identity_list(
        frozen.get("session_ids") or [],
        field="contributing calibration sessions")
    if not isinstance(calibration_evidence, dict):
        raise ValueError("teacher calibration sidecar evidence is required")
    evidence = json.loads(json.dumps(
        calibration_evidence, sort_keys=True, separators=(",", ":"),
        allow_nan=False))
    contributing = _identity_list(
        evidence.get("instrument_ids") or [],
        field="contributing calibration instruments")
    if not set(contributing_sessions).issubset(planned_sessions):
        raise ValueError("contributing sessions are outside the frozen plan")
    if not set(contributing).issubset(planned):
        raise ValueError("contributing instruments are outside the frozen plan")
    observations = frozen.get("observations")
    class_counts = frozen.get("class_counts") or {}
    if (isinstance(observations, bool) or not isinstance(observations, int)
            or observations < 1 or set(class_counts) != {
                "ENTER_LONG", "DOWN_ABSTAIN", "UP_BUT_COSTLY_ABSTAIN"}
            or any(isinstance(value, bool) or not isinstance(value, int)
                   or value < 0 for value in class_counts.values())
            or sum(class_counts.values()) != observations):
        raise ValueError("teacher calibration statistics are inconsistent")
    row_digest = evidence.get("calibration_row_digest") or {}
    if (row_digest.get("version") != CALIBRATION_ROW_DIGEST_VERSION
            or row_digest.get("rows") != observations):
        raise ValueError("teacher lacks an exact streaming calibration digest")
    for key in ("xor_sha256", "sum_sha256"):
        value = str(row_digest.get(key) or "").lower()
        if (len(value) != 64 or any(char not in "0123456789abcdef"
                                   for char in value)):
            raise ValueError("teacher calibration row digest is invalid")
    if not isinstance(sample_contract, dict) or not sample_contract:
        raise ValueError("calibration sample contract is required")
    if not isinstance(source_contract, dict) or not source_contract:
        raise ValueError("calibration source contract is required")
    cutoff_text = str(source_contract.get("knowledge_cutoff") or "")
    if not cutoff_text:
        raise ValueError("calibration source knowledge cutoff is required")
    try:
        cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "calibration source knowledge cutoff must be ISO-8601") from exc
    if cutoff.tzinfo is None:
        raise ValueError("calibration source knowledge cutoff must be aware")
    if not any(source_contract.get(key) for key in (
            "source_content_fingerprint", "source_lineage_fingerprint")):
        raise ValueError("calibration source content fingerprint is required")
    for key in ("source_content_fingerprint", "source_lineage_fingerprint"):
        if source_contract.get(key) is None:
            continue
        value = str(source_contract[key]).lower()
        if (len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError(
                f"calibration source {key} must be one sha256 digest")
    # A canonical JSON round trip both validates finiteness and detaches caller
    # dictionaries from the attested evidence.
    sample_contract = json.loads(json.dumps(
        sample_contract, sort_keys=True, separators=(",", ":"),
        allow_nan=False))
    source_contract = json.loads(json.dumps(
        source_contract, sort_keys=True, separators=(",", ":"),
        allow_nan=False))
    fingerprints = frozen.get("calibration_fingerprints") or {}
    if (int(frozen.get("sessions") or -1) != len(contributing_sessions)
            or int(frozen.get("instruments") or -1) != len(contributing)
            or fingerprints.get("sessions") !=
            _identity_list_hash(contributing_sessions)
            or fingerprints.get("instruments") !=
            _identity_list_hash(contributing)):
        raise ValueError("teacher calibration membership hashes do not verify")
    payload = {
        "version": CALIBRATION_ATTESTATION_VERSION,
        "teacher_model_fingerprint": frozen.get("model_fingerprint"),
        "teacher_version": frozen.get("version"),
        "session_identity": {
            "planned_ids": planned_sessions,
            "planned_count": len(planned_sessions),
            "planned_hash": _identity_list_hash(planned_sessions),
            "contributing_ids": contributing_sessions,
            "contributing_count": len(contributing_sessions),
            "contributing_hash": _identity_list_hash(contributing_sessions),
        },
        "instrument_identity": {
            "planned_ids": planned,
            "planned_count": len(planned),
            "planned_hash": _identity_list_hash(planned),
            "contributing_ids": contributing,
            "contributing_count": len(contributing),
            "contributing_hash": _identity_list_hash(contributing),
        },
        "statistics": {
            "observations": observations,
            "class_counts": dict(class_counts),
        },
        "contracts": {
            "feature_spec_hash": frozen.get("feature_spec_hash"),
            "feature_cube_spec_hash": frozen.get("feature_cube_spec_hash"),
            "feature_window_contract_version": frozen.get(
                "feature_window_contract_version"),
            "label_version": frozen.get("label_version"),
            "target": frozen.get("target"),
            "horizon_seconds": frozen.get("horizon_seconds"),
            "execution": frozen.get("execution"),
            "cost_inputs": frozen.get("cost_inputs") or {},
            "sample_contract": sample_contract,
        },
        "calibration_content": row_digest,
        "source_contract": source_contract,
    }
    payload["attestation_hash"] = _canonical_hash(payload)
    frozen["calibration_attestation"] = payload
    return frozen


def verify_calibration_attestation(report: dict) -> dict:
    """Verify and return the sidecar required by MODEL_CANDIDATE."""
    if not isinstance(report, dict):
        raise ValueError("teacher report must be an object")
    attestation = report.get("calibration_attestation")
    if not isinstance(attestation, dict):
        raise ValueError("MODEL_CANDIDATE requires calibration-attestation-v1")
    detached = json.loads(json.dumps(
        attestation, sort_keys=True, separators=(",", ":"), allow_nan=False))
    supplied_hash = detached.pop("attestation_hash", None)
    if (detached.get("version") != CALIBRATION_ATTESTATION_VERSION
            or supplied_hash != _canonical_hash(detached)):
        raise ValueError("calibration attestation hash does not verify")
    if detached.get("teacher_model_fingerprint") != report.get(
            "model_fingerprint"):
        raise ValueError("calibration attestation belongs to another teacher")
    sessions = detached.get("session_identity") or {}
    instruments = detached.get("instrument_identity") or {}
    for identity, name in ((sessions, "session"),
                           (instruments, "instrument")):
        for role in ("planned", "contributing"):
            values = _identity_list(
                identity.get(f"{role}_ids") or [],
                field=f"attested {role} calibration {name}s")
            if (identity.get(f"{role}_count") != len(values)
                    or identity.get(f"{role}_hash") !=
                    _identity_list_hash(values)):
                raise ValueError(
                    f"attested calibration {name} identity does not verify")
    contributing_sessions = sessions["contributing_ids"]
    contributing_instruments = instruments["contributing_ids"]
    fingerprints = report.get("calibration_fingerprints") or {}
    statistics = detached.get("statistics") or {}
    if (report.get("session_ids") != contributing_sessions
            or report.get("sessions") != len(contributing_sessions)
            or report.get("instruments") != len(contributing_instruments)
            or fingerprints.get("sessions") !=
            _identity_list_hash(contributing_sessions)
            or fingerprints.get("instruments") !=
            _identity_list_hash(contributing_instruments)
            or report.get("observations") != statistics.get("observations")
            or report.get("class_counts") != statistics.get("class_counts")
            ):
        raise ValueError("teacher report differs from calibration attestation")
    expected_contract = {
        "feature_spec_hash": report.get("feature_spec_hash"),
        "feature_cube_spec_hash": report.get("feature_cube_spec_hash"),
        "feature_window_contract_version": report.get(
            "feature_window_contract_version"),
        "label_version": report.get("label_version"),
        "target": report.get("target"),
        "horizon_seconds": report.get("horizon_seconds"),
        "execution": report.get("execution"),
        "cost_inputs": report.get("cost_inputs") or {},
        "sample_contract": (detached.get("contracts") or {}).get(
            "sample_contract"),
    }
    if detached.get("contracts") != expected_contract:
        raise ValueError("teacher contract differs from calibration attestation")
    if (not set(contributing_sessions).issubset(sessions["planned_ids"])
            or not set(contributing_instruments).issubset(
                instruments["planned_ids"])):
        raise ValueError("attested contributors are outside the frozen plan")
    return {**detached, "attestation_hash": supplied_hash}


def feature_vector(sample: IntradaySample) -> tuple[float, ...]:
    """Return a fixed feature vector with one missing flag per raw feature."""
    values: list[float] = []
    missing: list[float] = []
    for name in BASE_FEATURES:
        value = _finite(getattr(sample, name, None))
        missing.append(1.0 if value is None else 0.0)
        value = 0.0 if value is None else value
        if name in LOG1P_FEATURES:
            value = math.copysign(math.log1p(abs(value)), value)
        # A bad upstream spike must not make the deterministic normal equations
        # non-finite.  This is a preregistered numerical bound, not OOS tuning.
        values.append(max(-1_000_000.0, min(1_000_000.0, value)))
    return tuple([*values, *missing])


def feature_names() -> tuple[str, ...]:
    return tuple([*BASE_FEATURES, *(f"{name}__missing" for name in BASE_FEATURES)])


def feature_spec_hash() -> str:
    payload = json.dumps({
        "version": TEACHER_VERSION,
        "features": feature_names(),
        "log1p": sorted(LOG1P_FEATURES),
        "ridge_fraction": RIDGE_FRACTION,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _decision_index_fingerprint(samples: Sequence[IntradaySample]) -> str:
    payload = json.dumps([
        [sample.instrument_id, sample.decision_time.isoformat()]
        for sample in samples
    ], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _validate_explicit_feature_cube(
        samples: Sequence[IntradaySample], feature_cube) -> None:
    if feature_cube is None:
        raise ValueError("explicit-window teacher requires a feature cube")
    try:
        spec = feature_cube.spec
        row_count = int(feature_cube.row_count)
        fingerprint = str(feature_cube.decision_index_fingerprint)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("explicit-window teacher feature cube is invalid") from exc
    if row_count != len(samples):
        raise ValueError("explicit-window teacher feature cube is misaligned")
    if fingerprint != _decision_index_fingerprint(samples):
        raise ValueError(
            "explicit-window teacher feature cube decision index is misaligned")
    expected = {
        "version": FEATURE_CUBE_VERSION,
        "feature_window_contract_version": EXPLICIT_FEATURE_WINDOW_CONTRACT,
        "windows_seconds": tuple(PRIMITIVE_WINDOWS_SECONDS),
        "windowed_fields": tuple(sorted(WINDOWED_FIELDS)),
        "boundary": FEATURE_CUBE_BOUNDARY,
    }
    try:
        actual = {
            "version": getattr(spec, "version", None),
            "feature_window_contract_version": getattr(
                spec, "feature_window_contract_version", None),
            "windows_seconds": tuple(getattr(spec, "windows_seconds", ())),
            "windowed_fields": tuple(getattr(spec, "windowed_fields", ())),
            "boundary": getattr(spec, "boundary", None),
        }
    except TypeError as exc:
        raise ValueError(
            "explicit-window teacher feature cube spec changed") from exc
    if actual != expected:
        raise ValueError("explicit-window teacher feature cube spec changed")
    try:
        for field in sorted(WINDOWED_FIELDS):
            for seconds in PRIMITIVE_WINDOWS_SECONDS:
                if len(feature_cube.column(field, seconds)) != len(samples):
                    raise ValueError(
                        "explicit-window teacher feature cube is misaligned")
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError(
            "explicit-window teacher feature cube is incomplete") from exc


def _bounded_feature_value(name: str, raw, *, log1p_fields) -> tuple[float, float]:
    value = _finite(raw)
    missing = 1.0 if value is None else 0.0
    value = 0.0 if value is None else value
    if name in log1p_fields:
        value = math.copysign(math.log1p(abs(value)), value)
    return max(-1_000_000.0, min(1_000_000.0, value)), missing


def explicit_feature_vector(
        sample: IntradaySample, feature_cube, row_index: int) -> tuple[float, ...]:
    """Return the v4 state-plus-all-windows vector for one aligned row."""
    values: list[float] = []
    missing: list[float] = []
    for field in sorted(STATE_FIELDS):
        value, absent = _bounded_feature_value(
            field, getattr(sample, field, None),
            log1p_fields=EXPLICIT_LOG1P_FIELDS)
        values.append(value)
        missing.append(absent)
    for field in sorted(WINDOWED_FIELDS):
        for seconds in PRIMITIVE_WINDOWS_SECONDS:
            value, absent = _bounded_feature_value(
                field, feature_cube.value(field, seconds, row_index),
                log1p_fields=EXPLICIT_LOG1P_FIELDS)
            values.append(value)
            missing.append(absent)
    return tuple([*values, *missing])


def _label(sample: IntradaySample, horizon_seconds: int):
    return next((row for row in sample.labels
                 if row.horizon_seconds == horizon_seconds), None)


def executable_target(sample: IntradaySample, *, horizon_seconds: int,
                      execution: str) -> tuple[float, float, float] | None:
    """Return markout, net-per-opportunity, and positive-net class."""
    label = _label(sample, horizon_seconds)
    if label is None:
        return None
    execution = str(execution).upper()
    if execution == "TAKER":
        net = _finite(label.long_taker_net_bps)
    elif execution == "PASSIVE_FIFO_LOWER_BOUND":
        passive = _finite(label.long_passive_net_bps)
        net = 0.0 if passive is None else passive
    else:
        raise ValueError(f"unsupported execution={execution!r}")
    markout = _finite(label.long_mid_markout_bps)
    if markout is None or net is None:
        return None
    return markout, net, 1.0 if net > 0.0 else 0.0


def direction_class(sample: IntradaySample, *, horizon_seconds: int,
                    execution: str) -> str | None:
    target = executable_target(sample, horizon_seconds=horizon_seconds,
                               execution=execution)
    if target is None:
        return None
    markout, net, _positive = target
    if net > 0.0:
        return "ENTER_LONG"
    # This lane is long-only until point-in-time borrow data exists.  A negative
    # label is useful supervision but cannot silently authorize a short trade.
    if markout < 0.0:
        return "DOWN_ABSTAIN"
    return "UP_BUT_COSTLY_ABSTAIN"


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Deterministic partial-pivot Gaussian elimination for a tiny system."""
    n = len(rhs)
    augmented = [list(row) + [float(value)]
                 for row, value in zip(matrix, rhs)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if not math.isfinite(augmented[pivot][col]) or \
                abs(augmented[pivot][col]) <= 1e-12:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0.0:
                continue
            augmented[row] = [left - factor * right
                              for left, right in zip(augmented[row],
                                                     augmented[col])]
    out = [augmented[index][-1] for index in range(n)]
    return out if all(math.isfinite(value) for value in out) else None


@dataclass(frozen=True, slots=True)
class FrozenRidge:
    target: str
    intercept: float
    coefficients: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def predict(self, vector: Sequence[float]) -> float:
        if len(vector) != len(self.coefficients):
            raise ValueError("teacher feature dimension changed after freeze")
        return self.intercept + math.fsum(
            coefficient * ((float(value) - mean) / scale)
            for value, mean, scale, coefficient in zip(
                vector, self.means, self.scales, self.coefficients))

    def as_dict(self) -> dict:
        """Return every frozen parameter needed to reproduce predictions."""
        return {
            "target": self.target,
            "intercept": self.intercept,
            "coefficients": list(self.coefficients),
            "means": list(self.means),
            "scales": list(self.scales),
        }


class _Moments:
    def __init__(self, dimension: int):
        self.dimension = dimension
        self.count = 0
        self.sum_x = [0.0] * dimension
        self.sum_xx = [[0.0] * dimension for _ in range(dimension)]
        self.sum_y: dict[str, float] = {}
        self.sum_xy: dict[str, list[float]] = {}

    def add(self, vector: Sequence[float], targets: dict[str, float]) -> None:
        if len(vector) != self.dimension:
            raise ValueError("teacher feature dimension mismatch")
        self.count += 1
        for left, value in enumerate(vector):
            value = float(value)
            self.sum_x[left] += value
            for right in range(left, self.dimension):
                product = value * float(vector[right])
                self.sum_xx[left][right] += product
                if right != left:
                    self.sum_xx[right][left] += product
        for target, raw in targets.items():
            value = float(raw)
            self.sum_y[target] = self.sum_y.get(target, 0.0) + value
            row = self.sum_xy.setdefault(target, [0.0] * self.dimension)
            for index, feature in enumerate(vector):
                row[index] += float(feature) * value

    def freeze(self, target: str) -> FrozenRidge | None:
        if self.count <= 0 or target not in self.sum_y:
            return None
        n = float(self.count)
        means = [value / n for value in self.sum_x]
        scales = [math.sqrt(max(1e-12,
                                self.sum_xx[index][index] / n - mean * mean))
                  for index, mean in enumerate(means)]
        mean_y = self.sum_y[target] / n
        matrix = [[0.0] * self.dimension for _ in range(self.dimension)]
        rhs = [0.0] * self.dimension
        for left in range(self.dimension):
            rhs[left] = ((self.sum_xy[target][left] -
                          means[left] * self.sum_y[target]) / scales[left])
            for right in range(self.dimension):
                centred = (self.sum_xx[left][right] -
                            n * means[left] * means[right])
                matrix[left][right] = centred / (scales[left] * scales[right])
            matrix[left][left] += RIDGE_FRACTION * n
        coefficients = _solve(matrix, rhs)
        if coefficients is None:
            return None
        return FrozenRidge(
            target=target, intercept=mean_y, coefficients=tuple(coefficients),
            means=tuple(means), scales=tuple(scales))


class _NumpyMoments:
    """Vectorized sufficient statistics for the 244-dimensional v4 teacher."""

    def __init__(self, dimension: int):
        self.dimension = dimension
        self.count = 0
        self.sum_x = np.zeros(dimension, dtype=np.float64)
        self.sum_xx = np.zeros((dimension, dimension), dtype=np.float64)
        self.sum_y: dict[str, float] = {}
        self.sum_xy: dict[str, np.ndarray] = {}

    def add_batch(self, vectors: Sequence[Sequence[float]],
                  targets: dict[str, Sequence[float]]) -> None:
        if not vectors:
            return
        matrix = np.asarray(vectors, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self.dimension:
            raise ValueError("teacher feature dimension mismatch")
        if not np.isfinite(matrix).all():
            raise ValueError("teacher features must be finite after normalization")
        rows = int(matrix.shape[0])
        self.count += rows
        self.sum_x += np.sum(matrix, axis=0, dtype=np.float64)
        self.sum_xx += matrix.T @ matrix
        for target, raw_values in targets.items():
            values = np.asarray(raw_values, dtype=np.float64)
            if values.shape != (rows,) or not np.isfinite(values).all():
                raise ValueError("teacher target batch is invalid")
            self.sum_y[target] = self.sum_y.get(target, 0.0) + float(
                np.sum(values, dtype=np.float64))
            row = self.sum_xy.setdefault(
                target, np.zeros(self.dimension, dtype=np.float64))
            row += matrix.T @ values

    def freeze_many(self, targets: Sequence[str]) -> dict[str, FrozenRidge] | None:
        if self.count <= 0 or any(target not in self.sum_y for target in targets):
            return None
        n = float(self.count)
        means = self.sum_x / n
        variances = np.maximum(1e-12, np.diag(self.sum_xx) / n - means * means)
        scales = np.sqrt(variances)
        centred = self.sum_xx - n * np.outer(means, means)
        matrix = centred / np.outer(scales, scales)
        matrix[np.diag_indices(self.dimension)] += RIDGE_FRACTION * n
        right_hand_sides = []
        mean_targets = []
        for target in targets:
            mean_y = self.sum_y[target] / n
            rhs = (self.sum_xy[target] - means * self.sum_y[target]) / scales
            right_hand_sides.append(rhs)
            mean_targets.append(mean_y)
        try:
            coefficients = np.linalg.solve(
                matrix, np.column_stack(right_hand_sides))
        except np.linalg.LinAlgError:
            return None
        if not np.isfinite(coefficients).all():
            return None
        frozen: dict[str, FrozenRidge] = {}
        for index, target in enumerate(targets):
            frozen[target] = FrozenRidge(
                target=target,
                intercept=float(mean_targets[index]),
                coefficients=tuple(float(value)
                                   for value in coefficients[:, index]),
                means=tuple(float(value) for value in means),
                scales=tuple(float(value) for value in scales),
            )
        return frozen


class CostAwareTeacher:
    """Streaming calibration and immutable OOS prediction control."""

    def __init__(self, *, horizon_seconds: int, execution: str,
                 cost_inputs: dict | None = None,
                 feature_window_contract_version: str =
                 LEGACY_FEATURE_WINDOW_CONTRACT):
        self.horizon_seconds = int(horizon_seconds)
        self.execution = str(execution).upper()
        if self.execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
            raise ValueError(f"unsupported execution={self.execution!r}")
        if feature_window_contract_version not in {
                LEGACY_FEATURE_WINDOW_CONTRACT,
                EXPLICIT_FEATURE_WINDOW_CONTRACT}:
            raise ValueError("unsupported teacher feature-window contract")
        self.feature_window_contract_version = feature_window_contract_version
        self.teacher_version = (
            EXPLICIT_WINDOW_TEACHER_VERSION
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else TEACHER_VERSION)
        self._feature_names = (
            explicit_feature_names()
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else feature_names())
        self._feature_spec_hash = (
            explicit_feature_spec_hash()
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else feature_spec_hash())
        self._feature_cube_spec_hash = (
            explicit_feature_cube_spec_hash()
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else None)
        # Keep the exact preregistered cost inputs beside the fitted model.  The
        # labels already contain executable net returns, but without these
        # inputs a stored coefficient vector cannot be tied back to the cost
        # regime that produced its targets.
        try:
            encoded_costs = json.dumps(
                cost_inputs or {}, sort_keys=True, separators=(",", ":"),
                allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("cost_inputs must be finite JSON data") from exc
        self.cost_inputs = json.loads(encoded_costs)
        self._moments = (
            _NumpyMoments(len(self._feature_names))
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT
            else _Moments(len(self._feature_names)))
        self.instruments: set[str] = set()
        self.sessions: set[str] = set()
        self.class_counts = {
            "ENTER_LONG": 0,
            "DOWN_ABSTAIN": 0,
            "UP_BUT_COSTLY_ABSTAIN": 0,
        }
        self._models: dict[str, FrozenRidge] = {}
        self.status = "PENDING"
        self._restored_report: dict | None = None
        self._restored_calibration_evidence: dict | None = None
        self._frozen_model_fingerprint: str | None = None
        self._frozen_live_parameters_fingerprint: str | None = None
        self._calibration_row_count = 0
        self._calibration_row_xor = 0
        self._calibration_row_sum = 0

    @property
    def models(self) -> Mapping[str, FrozenRidge]:
        """Read-only view of live parameters; terminal models are immutable."""
        return MappingProxyType(self._models)

    def _calibration_row_digest_report(self) -> dict:
        return {
            "version": CALIBRATION_ROW_DIGEST_VERSION,
            "rows": self._calibration_row_count,
            "xor_sha256": f"{self._calibration_row_xor:064x}",
            "sum_sha256": f"{self._calibration_row_sum:064x}",
        }

    def calibration_evidence(self) -> dict:
        """Return the MODEL_CANDIDATE sidecar, outside legacy teacher JSON."""
        if self._restored_calibration_evidence is not None:
            return json.loads(json.dumps(self._restored_calibration_evidence))
        return {
            "version": "teacher-calibration-sidecar-v1",
            "instrument_ids": sorted(self.instruments),
            "session_ids": sorted(self.sessions),
            "observations": self._moments.count,
            "class_counts": dict(self.class_counts),
            "calibration_row_digest": self._calibration_row_digest_report(),
        }

    def _record_calibration_row(
            self, sample: IntradaySample, vector: Sequence[float],
            target: tuple[float, float, float]) -> None:
        digest = hashlib.sha256()
        digest.update((CALIBRATION_ROW_DIGEST_VERSION + "\0").encode())
        for value in (
                str(sample.instrument_id), sample.decision_time.isoformat(),
                sample.entry_time.isoformat(),
                sample.source_quote_event_time.isoformat()):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        digest.update(struct.pack(">ddd", *(float(value) for value in target)))
        digest.update(np.asarray(
            tuple(float(value) for value in vector),
            dtype=">f8").tobytes(order="C"))
        token = int.from_bytes(digest.digest(), "big")
        self._calibration_row_count += 1
        self._calibration_row_xor ^= token
        self._calibration_row_sum = (
            self._calibration_row_sum + token) % (1 << 256)

    def _live_parameters_fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(b"frozen-ridge-live-parameters-v1\0")
        digest.update(self.status.encode("utf-8"))
        digest.update(b"\0")
        for target in sorted(self._models):
            model = self._models[target]
            digest.update(target.encode("utf-8"))
            digest.update(b"\0")
            digest.update(struct.pack(">d", float(model.intercept)))
            for values in (model.coefficients, model.means, model.scales):
                digest.update(np.asarray(values, dtype=">f8").tobytes(order="C"))
        return digest.hexdigest()

    def _verify_live_parameters(self) -> None:
        if self.status == "PENDING":
            return
        expected = self._frozen_live_parameters_fingerprint
        if expected is None or self._live_parameters_fingerprint() != expected:
            raise ValueError("frozen teacher live parameters changed")

    def training_contract_key(self) -> tuple:
        """Return the exact contract under which calibration rows are fungible.

        A population may fit one teacher for several symbolic candidates only
        when every input that changes features or labels is identical.  The
        canonical cost JSON is included even though executable labels already
        contain costs: it is part of the frozen evidence identity and prevents
        a model from crossing cost regimes by accident.
        """
        cost_identity = json.dumps(
            self.cost_inputs, sort_keys=True, separators=(",", ":"),
            allow_nan=False)
        if self.feature_window_contract_version == \
                LEGACY_FEATURE_WINDOW_CONTRACT:
            # Preserve the v3 grouping identity exactly.
            return (
                TEACHER_VERSION,
                LABEL_VERSION,
                feature_spec_hash(),
                self.horizon_seconds,
                self.execution,
                cost_identity,
            )
        return (
            self.teacher_version,
            LABEL_VERSION,
            self._feature_spec_hash,
            self.feature_window_contract_version,
            self._feature_cube_spec_hash,
            self.horizon_seconds,
            self.execution,
            cost_identity,
        )

    def is_fresh(self) -> bool:
        """Whether a population may safely use this instance as a follower."""
        return (
            self.status == "PENDING"
            and self._moments.count == 0
            and not self.instruments
            and not self.sessions
            and not self._models
            and self._restored_report is None
        )

    def prediction_identity(self) -> tuple:
        """Identity for sharing a pure OOS prediction pass.

        Configuration equality alone is insufficient: two restored teachers
        may have been fitted on different calibration rows.  Terminal models
        are therefore grouped by the verified model fingerprint.  Every
        pending teacher predicts only ``None``, so its unfinished moments do
        not affect this identity.
        """
        if self.status == "PENDING":
            model_identity = "PENDING_NO_PREDICTIONS"
        else:
            self._verify_live_parameters()
            if self._frozen_model_fingerprint is None:
                raise ValueError("terminal teacher lacks a frozen fingerprint")
            model_identity = self._frozen_model_fingerprint
        return (*self.training_contract_key(), self.status, model_identity)

    def restore(self, report: dict, *, calibration_evidence: dict | None = None
                ) -> dict:
        """Restore an already frozen calibration artifact without fitting.

        Forward confirmation must use the exact model that was frozen before
        the independent sessions existed.  Replaying calibration rows here
        would make a retry depend on mutable history, while fitting on forward
        rows would be direct leakage.  The stored model fingerprint is therefore
        verified before its parameters are admitted.
        """
        if self.status != "PENDING" or self._moments.count:
            raise ValueError("teacher restore requires a fresh unfitted instance")
        if not isinstance(report, dict):
            raise ValueError("frozen teacher report must be an object")
        required = {
            "version": self.teacher_version,
            "label_version": LABEL_VERSION,
            "feature_spec_hash": self._feature_spec_hash,
            "horizon_seconds": self.horizon_seconds,
            "execution": self.execution,
        }
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            required.update({
                "feature_window_contract_version":
                    EXPLICIT_FEATURE_WINDOW_CONTRACT,
                "feature_cube_spec_hash": self._feature_cube_spec_hash,
            })
        for key, expected in required.items():
            if report.get(key) != expected:
                raise ValueError(f"frozen teacher {key} does not match runtime")
        if report.get("features") != list(self._feature_names):
            raise ValueError("frozen teacher feature order does not match runtime")
        if report.get("cost_inputs") != self.cost_inputs:
            raise ValueError("frozen teacher cost inputs do not match runtime")
        status = str(report.get("status") or "").upper()
        if status not in {
                "PASS", "INSUFFICIENT_CALIBRATION", "SINGULAR_CALIBRATION"}:
            raise ValueError("frozen teacher status is not terminal")
        parameters = report.get("model_parameters") or {}
        models: dict[str, FrozenRidge] = {}
        if status == "PASS":
            if set(parameters) != {"markout_bps", "net_bps", "positive_net"}:
                raise ValueError("frozen teacher is missing a required target")
            dimension = len(self._feature_names)
            for target in sorted(parameters):
                raw = parameters[target]
                if not isinstance(raw, dict) or raw.get("target") != target:
                    raise ValueError("frozen teacher target metadata is invalid")
                try:
                    intercept = float(raw["intercept"])
                    coefficients = tuple(float(v) for v in raw["coefficients"])
                    means = tuple(float(v) for v in raw["means"])
                    scales = tuple(float(v) for v in raw["scales"])
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("frozen teacher parameters are invalid") from exc
                values = (intercept, *coefficients, *means, *scales)
                if (len(coefficients) != dimension or len(means) != dimension
                        or len(scales) != dimension
                        or not all(math.isfinite(v) for v in values)
                        or any(scale <= 0.0 for scale in scales)):
                    raise ValueError("frozen teacher parameter dimensions are invalid")
                models[target] = FrozenRidge(
                    target=target, intercept=intercept,
                    coefficients=coefficients, means=means, scales=scales)
        elif parameters:
            raise ValueError("unusable frozen teacher must not carry models")

        model_payload = {
            "version": report.get("version"),
            "label_version": report.get("label_version"),
            "feature_spec_hash": report.get("feature_spec_hash"),
            "status": status,
            "horizon_seconds": report.get("horizon_seconds"),
            "execution": report.get("execution"),
            "cost_inputs": report.get("cost_inputs"),
            "observations": report.get("observations"),
            "calibration_fingerprints": report.get(
                "calibration_fingerprints") or {},
            "models": parameters,
        }
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            model_payload.update({
                "feature_window_contract_version": report.get(
                    "feature_window_contract_version"),
                "feature_cube_spec_hash": report.get(
                    "feature_cube_spec_hash"),
            })
        calculated = hashlib.sha256(json.dumps(
            model_payload, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        if calculated != report.get("model_fingerprint"):
            raise ValueError("frozen teacher model fingerprint does not verify")
        self._models = dict(models)
        self.status = status
        self._frozen_model_fingerprint = str(report["model_fingerprint"])
        self._frozen_live_parameters_fingerprint = \
            self._live_parameters_fingerprint()
        # JSON round-trip prevents a caller from mutating the evidence object
        # after it has been admitted to the forward evaluator.
        self._restored_report = json.loads(json.dumps(
            report, sort_keys=True, separators=(",", ":"), allow_nan=False))
        if calibration_evidence is not None:
            self._restored_calibration_evidence = json.loads(json.dumps(
                calibration_evidence, sort_keys=True, separators=(",", ":"),
                allow_nan=False))
        return self.report()

    def calibrate(self, instrument_id: str,
                  samples: Iterable[IntradaySample], *, feature_cube=None) -> None:
        if self.status != "PENDING":
            raise ValueError("teacher is already frozen")
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            rows = list(samples)
            _validate_explicit_feature_cube(rows, feature_cube)
            vectors: list[tuple[float, ...]] = []
            targets = {
                "markout_bps": [],
                "net_bps": [],
                "positive_net": [],
            }
            contributed = False
            for index, sample in enumerate(rows):
                target = executable_target(
                    sample, horizon_seconds=self.horizon_seconds,
                    execution=self.execution)
                if target is None:
                    continue
                markout, net, positive = target
                vector = explicit_feature_vector(sample, feature_cube, index)
                vectors.append(vector)
                targets["markout_bps"].append(markout)
                targets["net_bps"].append(net)
                targets["positive_net"].append(positive)
                self._record_calibration_row(
                    sample, vector, (markout, net, positive))
                label = direction_class(
                    sample, horizon_seconds=self.horizon_seconds,
                    execution=self.execution)
                if label is not None:
                    self.class_counts[label] += 1
                self.sessions.add(sample.decision_time.date().isoformat())
                contributed = True
            self._moments.add_batch(vectors, targets)
            if contributed:
                self.instruments.add(str(instrument_id))
            return

        # This v3 path intentionally remains byte/behavior compatible.
        contributed = False
        for sample in samples:
            target = executable_target(
                sample, horizon_seconds=self.horizon_seconds,
                execution=self.execution)
            if target is None:
                continue
            markout, net, positive = target
            vector = feature_vector(sample)
            self._record_calibration_row(
                sample, vector, (markout, net, positive))
            self._moments.add(vector, {
                "markout_bps": markout,
                "net_bps": net,
                "positive_net": positive,
            })
            label = direction_class(
                sample, horizon_seconds=self.horizon_seconds,
                execution=self.execution)
            if label is not None:
                self.class_counts[label] += 1
            self.sessions.add(sample.decision_time.date().isoformat())
            contributed = True
        if contributed:
            self.instruments.add(str(instrument_id))

    def freeze(self) -> dict:
        if self.status != "PENDING":
            return self.report()
        sufficient = (
            self._moments.count >= MIN_OBSERVATIONS
            and len(self.instruments) >= MIN_INSTRUMENTS
            and len(self.sessions) >= MIN_SESSIONS
        )
        if not sufficient:
            self.status = "INSUFFICIENT_CALIBRATION"
            report = self.report()
            self._frozen_model_fingerprint = report["model_fingerprint"]
            self._frozen_live_parameters_fingerprint = \
                self._live_parameters_fingerprint()
            return report
        targets = ("markout_bps", "net_bps", "positive_net")
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            models = self._moments.freeze_many(targets)
            if models is None:
                self.status = "SINGULAR_CALIBRATION"
                self._models = {}
                report = self.report()
                self._frozen_model_fingerprint = report["model_fingerprint"]
                self._frozen_live_parameters_fingerprint = \
                    self._live_parameters_fingerprint()
                return report
            self._models.update(models)
        else:
            for target in targets:
                model = self._moments.freeze(target)
                if model is None:
                    self.status = "SINGULAR_CALIBRATION"
                    self._models = {}
                    report = self.report()
                    self._frozen_model_fingerprint = report["model_fingerprint"]
                    self._frozen_live_parameters_fingerprint = \
                        self._live_parameters_fingerprint()
                    return report
                self._models[target] = model
        self.status = "PASS"
        report = self.report()
        self._frozen_model_fingerprint = report["model_fingerprint"]
        self._frozen_live_parameters_fingerprint = \
            self._live_parameters_fingerprint()
        return report

    def predict(self, samples: Iterable[IntradaySample], *,
                feature_cube=None) -> list[dict | None]:
        self._verify_live_parameters()
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            rows = list(samples)
            _validate_explicit_feature_cube(rows, feature_cube)
            if self.status != "PASS":
                return [None for _ in rows]
            predictions = []
            for index, sample in enumerate(rows):
                vector = explicit_feature_vector(sample, feature_cube, index)
                probability = self._models["positive_net"].predict(vector)
                predictions.append({
                    "expected_markout_bps":
                        self._models["markout_bps"].predict(vector),
                    "expected_net_bps": self._models["net_bps"].predict(vector),
                    "positive_net_probability": max(
                        0.0, min(1.0, probability)),
                })
            return predictions

        # Keep v3's iterable consumption and values exactly as before.
        if self.status != "PASS":
            return [None for _ in samples]
        rows = []
        for sample in samples:
            vector = feature_vector(sample)
            probability = self._models["positive_net"].predict(vector)
            rows.append({
                "expected_markout_bps": self._models["markout_bps"].predict(vector),
                "expected_net_bps": self._models["net_bps"].predict(vector),
                "positive_net_probability": max(0.0, min(1.0, probability)),
            })
        return rows

    def report(self) -> dict:
        if self._restored_report is not None:
            return json.loads(json.dumps(self._restored_report))
        parameters = {
            target: self._models[target].as_dict()
            for target in sorted(self._models)
        }
        calibration_fingerprints = {
            "sessions": hashlib.sha256(json.dumps(
                sorted(self.sessions), separators=(",", ":")).encode()).hexdigest(),
            "instruments": hashlib.sha256(json.dumps(
                sorted(self.instruments), separators=(",", ":")).encode()).hexdigest(),
        }
        model_payload = {
            "version": self.teacher_version,
            "label_version": LABEL_VERSION,
            "feature_spec_hash": self._feature_spec_hash,
            "status": self.status,
            "horizon_seconds": self.horizon_seconds,
            "execution": self.execution,
            "cost_inputs": self.cost_inputs,
            "observations": self._moments.count,
            "calibration_fingerprints": calibration_fingerprints,
            "models": parameters,
        }
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            model_payload.update({
                "feature_window_contract_version":
                    self.feature_window_contract_version,
                "feature_cube_spec_hash": self._feature_cube_spec_hash,
            })
        model_fingerprint = hashlib.sha256(json.dumps(
            model_payload, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        report = {
            "version": self.teacher_version,
            "label_version": LABEL_VERSION,
            "status": self.status,
            "feature_spec_hash": self._feature_spec_hash,
            "features": list(self._feature_names),
            "target": "EXECUTABLE_NET_BPS_PER_OPPORTUNITY",
            "secondary_targets": ["MID_MARKOUT_BPS", "POSITIVE_NET_CLASS"],
            "horizon_seconds": self.horizon_seconds,
            "execution": self.execution,
            "cost_inputs": self.cost_inputs,
            "observations": self._moments.count,
            "instruments": len(self.instruments),
            "sessions": len(self.sessions),
            "session_ids": sorted(self.sessions),
            "calibration_fingerprints": calibration_fingerprints,
            "class_counts": dict(self.class_counts),
            "ridge_fraction": RIDGE_FRACTION,
            "model_parameters": parameters,
            "model_fingerprint": model_fingerprint,
            "single_session_calibration_warning": len(self.sessions) < 2,
            "fixed_feature_set": True,
            "hyperparameter_search": False,
            "oos_fit_forbidden": True,
            "control_only": True,
            "promotion_authority": False,
        }
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            report.update({
                "feature_window_contract_version":
                    self.feature_window_contract_version,
                "feature_cube_spec_hash": self._feature_cube_spec_hash,
            })
        return report
