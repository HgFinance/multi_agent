"""Independent, frozen supervised-model candidate lane.

The intraday ridge teacher started as an AST diagnostic control.  That remains
true: this module does not grant the teacher, an AST, or an LLM promotion or
order authority.  It merely gives one already-frozen teacher its own bounded
OOS measurement lane so an unrelated symbolic calibration failure cannot erase
the model's observations.

Calibration is a resource preflight, never evidence.  Every entry decision is
made by a model frozen on strictly earlier sessions, over a fixed feature,
label, cost, split, and threshold contract.  Historical discovery/FULL results
can at most nominate a separately governed forward confirmation.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from statistics import fmean
from typing import Sequence

from intraday_alpha_ast import EXPLICIT_FEATURE_WINDOW_CONTRACT
from intraday_candidate import (
    DEFAULT_CRITERIA,
    KST,
    _capital_peaks,
    _folds as _contiguous_session_blocks,
    _observations,
    _prepare_sample_sequence,
    _stationary_mean,
)
from intraday_microstructure import IntradayLaneSpec, IntradaySample, audit_causality
from intraday_supervised import (
    CostAwareTeacher,
    _decision_index_fingerprint,
    executable_target,
    verify_calibration_attestation,
)
from overfit_stats import deflated_sharpe, stationary_bootstrap_indices


MODEL_CANDIDATE_VERSION = "krx-frozen-ridge-model-candidate-v1"
MODEL_CANDIDATE_RESULT_NAMESPACE = "MODEL_CANDIDATE"
MODEL_CANDIDATE_FAILURE_MEMORY_VERSION = \
    "krx-model-candidate-failure-memory-v1"
MODEL_CANDIDATE_MULTIPLE_TESTING_VERSION = \
    "krx-model-candidate-bonferroni-stationary-v1"
FULL_EVIDENCE_SCOPE = "FULL_60"
DISCOVERY_EVIDENCE_SCOPES = frozenset({"DISCOVERY_6", "VALIDATION_20"})
MAX_CAUSALITY_EXAMPLES = 20
DEFAULT_BOOTSTRAP_DRAWS = 10_000
MIN_POSITIVE_INSTRUMENT_RATIO = 0.60
BOOTSTRAP_RESOLUTION_FAILURE = \
    "MODEL_BONFERRONI_BOOTSTRAP_RESOLUTION_INSUFFICIENT"
MODEL_TEACHER_ATTESTATION_MISSING = "MODEL_TEACHER_ATTESTATION_MISSING"
MODEL_SPLIT_VERSION = "model-candidate-pit-split-v1"


def _canonical_hash(value) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_hash(value: str, field: str) -> str:
    text = str(value or "").lower()
    if (len(text) != 64
            or any(character not in "0123456789abcdef" for character in text)):
        raise ValueError(f"{field} must be one sha256 hex digest")
    return text


def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _selection_adjusted_interval(
        values: Sequence[float], *, declared_trials: int,
        n_boot: int = DEFAULT_BOOTSTRAP_DRAWS) -> dict:
    """Family-wise session-mean interval without inventing trial dispersion.

    The model family currently has one frozen member per experiment, while the
    append-only ledger can reveal that the architecture has been inspected in
    earlier experiments.  A Bonferroni alpha allocation is conservative and
    does not require fabricated historical return vectors or a guessed
    cross-model Sharpe standard deviation.
    """
    trials = int(declared_trials)
    if trials < 1:
        raise ValueError("declared model trials must be positive")
    finite = [float(value) for value in values]
    if len(finite) < 2 or not all(math.isfinite(value) for value in finite):
        return {
            "low_bps": None,
            "high_bps": None,
            "reason": "need at least two finite scheduled session returns",
            "family_wise_alpha": 0.05,
            "per_candidate_two_sided_alpha": 0.05 / trials,
            "declared_trials": trials,
            "draws": 0,
        }
    alpha = 0.05 / trials
    tail_probability = alpha / 2.0
    if (isinstance(n_boot, bool) or not isinstance(n_boot, int)
            or n_boot < 1):
        raise ValueError("bootstrap draws must be a positive integer")
    finite_resolution = 1.0 / n_boot
    tail_rank = tail_probability * n_boot
    if tail_rank < 1.0 - 1e-12:
        return {
            "low_bps": None,
            "high_bps": None,
            "status": "UNRESOLVED_FINITE_MONTE_CARLO_TAIL",
            "failure_code": BOOTSTRAP_RESOLUTION_FAILURE,
            "reason": (
                "Bonferroni tail probability is below finite bootstrap "
                "resolution; no active lower confidence bound exists"),
            "active_gate": False,
            "family_wise_alpha": 0.05,
            "per_candidate_two_sided_alpha": alpha,
            "per_tail_probability": tail_probability,
            "declared_trials": trials,
            "method": "STATIONARY_BOOTSTRAP_BONFERRONI",
            "draws": n_boot,
            "finite_draw_resolution": finite_resolution,
            "minimum_draws_required": math.ceil(
                1.0 / tail_probability - 1e-12),
        }
    means = []
    for path in stationary_bootstrap_indices(
            len(finite), n_boot=n_boot, restart_probability=0.25,
            seed=20260819):
        means.append(math.fsum(finite[index] for index in path) / len(path))
    means.sort()
    # Inverse empirical CDF with conservative equal tails.  At the exact
    # resolution boundary (tail*n == 1), the lower bound must be the minimum
    # draw and the upper bound the maximum; using ``int(tail*n)`` as a zero-
    # based index would incorrectly discard that sole adverse draw.
    tail_order = max(1, int(math.ceil(tail_rank - 1e-12)))
    lower_index = tail_order - 1
    upper_index = len(means) - tail_order
    return {
        "low_bps": means[lower_index],
        "high_bps": means[upper_index],
        "family_wise_alpha": 0.05,
        "per_candidate_two_sided_alpha": alpha,
        "declared_trials": trials,
        "method": "STATIONARY_BOOTSTRAP_BONFERRONI",
        "restart_probability": 0.25,
        "expected_block_length_sessions": 4.0,
        "draws": len(means),
        "seed": 20260819,
        "finite_draw_resolution": 1.0 / len(means),
        "per_tail_probability": tail_probability,
        "status": "PASS",
        "failure_code": None,
        "active_gate": True,
    }


def model_split_manifest(
        *, calibration_sessions: Sequence[str],
        contributing_calibration_sessions: Sequence[str],
        evaluation_sessions: Sequence[str],
        calibration_instruments: Sequence[str],
        contributing_calibration_instruments: Sequence[str],
        evaluation_instruments: Sequence[str], spec: IntradayLaneSpec,
        rung: str) -> dict:
    """Return the exact PIT split, deriving rather than asserting its order."""
    planned_sessions = tuple(str(value) for value in calibration_sessions)
    contributing_sessions = tuple(
        str(value) for value in contributing_calibration_sessions)
    oos_sessions = tuple(str(value) for value in evaluation_sessions)
    planned_instruments = tuple(str(value) for value in calibration_instruments)
    contributing_instruments = tuple(
        str(value) for value in contributing_calibration_instruments)
    oos_instruments = tuple(str(value) for value in evaluation_instruments)
    strictly_precedes = bool(
        planned_sessions and oos_sessions
        and max(planned_sessions) < min(oos_sessions)
        and (not contributing_sessions
             or max(contributing_sessions) < min(oos_sessions)))
    return {
        "version": MODEL_SPLIT_VERSION,
        "rung": str(rung).upper(),
        "calibration_sessions": list(planned_sessions),
        "contributing_calibration_sessions": list(contributing_sessions),
        "evaluation_sessions": list(oos_sessions),
        "calibration_instruments": list(planned_instruments),
        "contributing_calibration_instruments": list(
            contributing_instruments),
        "evaluation_instruments": list(oos_instruments),
        "temporal_boundary": {
            "latest_planned_calibration_session": (
                max(planned_sessions) if planned_sessions else None),
            "latest_contributing_calibration_session": (
                max(contributing_sessions) if contributing_sessions else None),
            "earliest_evaluation_session": (
                min(oos_sessions) if oos_sessions else None),
            "calibration_strictly_precedes_evaluation": strictly_precedes,
        },
        "purge_gap_seconds": spec.purge_gap.total_seconds(),
        "maximum_label_horizon_seconds": max(spec.horizons_seconds),
    }


class ModelCandidateAccumulator:
    """Evaluate one immutable teacher independently from every AST result."""

    def __init__(
            self, *, teacher_report: dict, spec: IntradayLaneSpec,
            horizon_seconds: int, execution: str,
            minimum_predicted_edge_bps: float,
            feature_window_contract_version: str,
            expected_calibration_sessions: Sequence[str],
            expected_calibration_instruments: Sequence[str],
            expected_evaluation_sessions: Sequence[str],
            expected_instruments: Sequence[str], evidence_scope: str,
            configuration_hash: str, data_hash: str, split_hash: str,
            sampling_execution_manifest: dict,
            declared_model_trials: int = 1,
            selection_count_components: dict | None = None,
            criteria: dict | None = None):
        self.spec = spec
        self.horizon_seconds = int(horizon_seconds)
        self.execution = str(execution).upper()
        self.minimum_predicted_edge_bps = float(minimum_predicted_edge_bps)
        if (not math.isfinite(self.minimum_predicted_edge_bps)
                or self.minimum_predicted_edge_bps < 0.0):
            raise ValueError(
                "model minimum predicted edge must be finite and non-negative")
        self.feature_window_contract_version = str(
            feature_window_contract_version)
        self.evidence_scope = str(evidence_scope).upper()
        if (self.evidence_scope not in DISCOVERY_EVIDENCE_SCOPES
                and self.evidence_scope != FULL_EVIDENCE_SCOPE):
            raise ValueError("unsupported model candidate evidence scope")
        if (not isinstance(sampling_execution_manifest, dict)
                or not sampling_execution_manifest):
            raise ValueError("model sampling/execution manifest is required")
        self.sampling_execution_manifest = json.loads(json.dumps(
            sampling_execution_manifest, sort_keys=True,
            separators=(",", ":"), allow_nan=False))
        supplied_configuration_hash = _require_hash(
            configuration_hash, "configuration_hash")
        calculated_configuration_hash = _canonical_hash(
            self.sampling_execution_manifest)
        if supplied_configuration_hash != calculated_configuration_hash:
            raise ValueError(
                "configuration_hash does not seal sampling/execution manifest")
        self.configuration_hash = supplied_configuration_hash
        self.data_hash = _require_hash(data_hash, "data_hash")
        self.split_hash = _require_hash(split_hash, "split_hash")
        self.declared_model_trials = int(declared_model_trials)
        if self.declared_model_trials < 1:
            raise ValueError("declared_model_trials must be positive")
        components = selection_count_components or {
            "explicit_declared_total": self.declared_model_trials,
            "declared_total": self.declared_model_trials,
        }
        if (not isinstance(components, dict)
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or value < 0 for value in components.values())
                or int(components.get("declared_total") or -1) !=
                self.declared_model_trials):
            raise ValueError(
                "selection_count_components must reconcile to declared trials")
        self.selection_count_components = dict(components)
        self.rules = {**DEFAULT_CRITERIA, **(criteria or {})}
        self.expected_sessions = tuple(str(value)
                                       for value in expected_evaluation_sessions)
        self.expected_instruments = tuple(str(value)
                                          for value in expected_instruments)
        self.expected_calibration_sessions = tuple(
            str(value) for value in expected_calibration_sessions)
        self.expected_calibration_instruments = tuple(
            str(value) for value in expected_calibration_instruments)
        if (not self.expected_sessions
                or len(set(self.expected_sessions)) != len(self.expected_sessions)
                or list(self.expected_sessions) != sorted(self.expected_sessions)):
            raise ValueError(
                "model evaluation sessions must be unique and chronological")
        if (not self.expected_instruments
                or len(set(self.expected_instruments)) !=
                len(self.expected_instruments)):
            raise ValueError("model evaluation instruments must be unique")
        if (not self.expected_calibration_sessions
                or self.expected_calibration_sessions != tuple(sorted(set(
                    self.expected_calibration_sessions)))):
            raise ValueError(
                "model calibration sessions must be unique and chronological")
        if (not self.expected_calibration_instruments
                or self.expected_calibration_instruments != tuple(sorted(set(
                    self.expected_calibration_instruments)))):
            raise ValueError(
                "model calibration instruments must be sorted and unique")
        self._expected_session_set = set(self.expected_sessions)
        self._expected_instrument_set = set(self.expected_instruments)

        self.teacher_report = json.loads(json.dumps(
            teacher_report or {}, sort_keys=True, separators=(",", ":"),
            allow_nan=False))
        self.teacher_status = str(
            self.teacher_report.get("status") or "MISSING").upper()
        self.teacher: CostAwareTeacher | None = None
        self.calibration_attestation: dict | None = None
        self.teacher_preflight_failure: str | None = None
        if self.teacher_status == "PASS":
            if not isinstance(self.teacher_report.get(
                    "calibration_attestation"), dict):
                # Old v3/v4 artifacts remain valid for AST diagnostics and the
                # existing forward reader.  They simply cannot enter the new
                # independent model evidence namespace without its sidecar.
                self.teacher_preflight_failure = \
                    MODEL_TEACHER_ATTESTATION_MISSING
            else:
                self.calibration_attestation = verify_calibration_attestation(
                    self.teacher_report)
        if self.calibration_attestation is not None:
            attested_sessions = self.calibration_attestation[
                "session_identity"]
            attested_instruments = self.calibration_attestation[
                "instrument_identity"]
            if (attested_sessions["planned_ids"] !=
                    list(self.expected_calibration_sessions)
                    or attested_instruments["planned_ids"] !=
                    list(self.expected_calibration_instruments)):
                raise ValueError(
                    "calibration attestation differs from the frozen split")
            sample_contract = (self.calibration_attestation.get(
                "contracts") or {}).get("sample_contract")
            if sample_contract != self.sampling_execution_manifest:
                raise ValueError(
                    "attested sample contract differs from model configuration")
            if self.data_hash != _canonical_hash(
                    self.calibration_attestation.get("source_contract") or {}):
                raise ValueError(
                    "data_hash does not seal attested calibration source")
            self.teacher = CostAwareTeacher(
                horizon_seconds=self.horizon_seconds,
                execution=self.execution,
                cost_inputs={
                    "fee_bps_per_side": float(self.spec.fee_bps_per_side),
                    "maker_fee_bps_per_side": float(
                        self.spec.maker_fee_bps_per_side),
                    "passive_nonfill_net_bps_per_opportunity": 0.0,
                },
                feature_window_contract_version=
                self.feature_window_contract_version)
            self.teacher.restore(self.teacher_report)

        calibration_sessions = tuple(str(value) for value in
                                     self.teacher_report.get("session_ids") or [])
        contributing_instruments = tuple(str(value) for value in (
            ((self.calibration_attestation or {}).get(
                "instrument_identity") or {}).get("contributing_ids") or []))
        split_manifest = model_split_manifest(
            calibration_sessions=self.expected_calibration_sessions,
            contributing_calibration_sessions=calibration_sessions,
            evaluation_sessions=self.expected_sessions,
            calibration_instruments=self.expected_calibration_instruments,
            contributing_calibration_instruments=contributing_instruments,
            evaluation_instruments=self.expected_instruments,
            spec=self.spec, rung=self.evidence_scope)
        if split_manifest["temporal_boundary"][
                "calibration_strictly_precedes_evaluation"] is not True:
            raise ValueError(
                "model calibration sessions must strictly precede OOS sessions")
        if self.split_hash != _canonical_hash(split_manifest):
            raise ValueError("split_hash does not seal the exact PIT split")
        self.split_manifest = split_manifest
        self.calibration_sessions = calibration_sessions
        feature_hash = _require_hash(
            self.teacher_report.get("feature_spec_hash"),
            "teacher feature_spec_hash") if self.teacher is not None else None
        model_hash = _require_hash(
            self.teacher_report.get("model_fingerprint"),
            "teacher model_fingerprint") if self.teacher is not None else None
        self.label_spec_hash = _canonical_hash({
            "label_version": self.teacher_report.get("label_version"),
            "target": self.teacher_report.get("target"),
            "horizon_seconds": self.horizon_seconds,
            "execution": self.execution,
            "cost_inputs": self.teacher_report.get("cost_inputs") or {},
        })
        self.model_candidate_id = _canonical_hash({
            "version": MODEL_CANDIDATE_VERSION,
            "model_fingerprint": model_hash,
            "feature_spec_hash": feature_hash,
            "label_spec_hash": self.label_spec_hash,
            "configuration_hash": self.configuration_hash,
            "data_hash": self.data_hash,
            "split_hash": self.split_hash,
            "calibration_attestation_hash": (
                (self.calibration_attestation or {}).get("attestation_hash")),
            "minimum_predicted_edge_bps": self.minimum_predicted_edge_bps,
            "declared_model_trials": self.declared_model_trials,
            "selection_count_components": self.selection_count_components,
            "evidence_scope": self.evidence_scope,
        })

        self.requested_instruments: set[str] = set()
        self.sampled_instruments: set[str] = set()
        self.opportunity_instruments: set[str] = set()
        self.session_net_sum: dict[str, float] = defaultdict(float)
        self.session_capital_deltas: dict[str, dict[float, int]] = defaultdict(
            lambda: defaultdict(int))
        self.opportunities = 0
        self.fills = 0
        self.net_sum = 0.0
        self.fill_net_sum = 0.0
        self.instrument_net_sum: dict[str, float] = defaultdict(float)
        self.instrument_opportunities: dict[str, int] = defaultdict(int)
        self.prediction_count = 0
        self.markout_squared_error = 0.0
        self.net_squared_error = 0.0
        self.brier_sum = 0.0
        self.causality_counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
        self.causality_examples: list[dict] = []
        self.replayed_instruments_by_session: dict[str, set[str]] = {
            session: set() for session in self.expected_sessions
        }
        self._row_identity_xor = 0
        self._row_identity_sum = 0
        self._row_identity_cells = 0
        self.schedule_sessions(self.expected_sessions)

    @property
    def resource_preflight_passed(self) -> bool:
        return self.teacher is not None and self.teacher_status == "PASS"

    def schedule_sessions(self, sessions: Sequence[str]) -> None:
        for raw in sessions:
            session = str(raw)
            if session not in self._expected_session_set:
                raise ValueError("model schedule contains a non-frozen session")
            self.session_net_sum.setdefault(session, 0.0)
            self.session_capital_deltas[session]

    def add(self, instrument_id: str,
            samples: Sequence[IntradaySample], *,
            evaluation_session: str | None = None) -> None:
        """Predict one replay slice without fitting or retaining raw rows."""
        instrument = str(instrument_id)
        ordered, feature_cube = _prepare_sample_sequence(samples)
        if self.teacher is None:
            predictions = [None for _ in ordered]
        else:
            if self.feature_window_contract_version == \
                    EXPLICIT_FEATURE_WINDOW_CONTRACT:
                predictions = self.teacher.predict(
                    ordered, feature_cube=feature_cube)
            else:
                predictions = self.teacher.predict(ordered)
        targets = [executable_target(
            sample, horizon_seconds=self.horizon_seconds,
            execution=self.execution) for sample in ordered]
        audit = audit_causality(ordered, self.spec)
        row_identity = (
            str(feature_cube.decision_index_fingerprint)
            if feature_cube is not None else
            _decision_index_fingerprint(ordered))
        self.add_prepared(
            instrument, ordered, audit, predictions=predictions,
            targets=targets, evaluation_session=evaluation_session,
            row_identity=row_identity, feature_cube=feature_cube)

    def add_prepared(
            self, instrument_id: str, samples: Sequence[IntradaySample],
            audit: dict, *, predictions: Sequence[dict | None],
            targets: Sequence[tuple[float, float, float] | None],
            evaluation_session: str | None = None,
            row_identity: str | None = None, feature_cube=None) -> None:
        """Verify and consume a shared, already predicted replay slice.

        ``CandidatePopulationAccumulator`` uses this entry point to reuse the
        exact row sequence and, for v4, the already-built feature cube.  The
        frozen teacher prediction and executable target are independently
        recomputed here before any evidence state changes.  No AST value or
        AST gate is accepted.
        """
        instrument = str(instrument_id)
        if instrument not in self._expected_instrument_set:
            raise ValueError("model replay instrument is outside frozen split")
        ordered = list(samples)
        if any(str(sample.instrument_id) != instrument for sample in ordered):
            raise ValueError("model replay instrument key does not match sample")
        if len(predictions) != len(ordered) or len(targets) != len(ordered):
            raise ValueError("model predictions or targets are misaligned")
        observed_sessions = {
            sample.decision_time.astimezone(KST).date().isoformat()
            for sample in ordered
        }
        declared_session = (str(evaluation_session)
                            if evaluation_session is not None else None)
        if declared_session is None and len(observed_sessions) == 1:
            declared_session = next(iter(observed_sessions))
        if declared_session not in self._expected_session_set:
            raise ValueError(
                "model replay cell lacks one frozen evaluation session")
        if observed_sessions and observed_sessions != {declared_session}:
            raise ValueError(
                "model replay slice crosses its declared evaluation session")
        expected_row_identity = (
            str(feature_cube.decision_index_fingerprint)
            if feature_cube is not None else
            _decision_index_fingerprint(ordered))
        identity = str(row_identity or "").lower()
        if (len(identity) != 16
                or any(character not in "0123456789abcdef"
                       for character in identity)):
            raise ValueError(
                "model replay lacks the immutable decision-row identity")
        if identity != expected_row_identity:
            raise ValueError(
                "model replay decision-row identity differs from samples")
        trusted_audit = audit_causality(ordered, self.spec)
        if (str((audit or {}).get("status") or "FAIL").upper() !=
                str(trusted_audit.get("status") or "FAIL").upper()):
            raise ValueError("shared model replay causality audit changed")
        # The caller's audit is only a consistency assertion.  From this
        # point onward every status, finding, and bounded example comes from
        # the independently recomputed audit.
        audit = trusted_audit
        if self.teacher is not None:
            # Caller-supplied predictions/targets are only a performance hint,
            # never evidence.  Recompute from the attested frozen teacher and
            # the exact samples at this boundary, using the existing aligned
            # feature cube rather than rebuilding it.
            if self.feature_window_contract_version == \
                    EXPLICIT_FEATURE_WINDOW_CONTRACT:
                trusted_predictions = self.teacher.predict(
                    ordered, feature_cube=feature_cube)
            else:
                if feature_cube is not None:
                    raise ValueError(
                        "legacy model replay must not carry a feature cube")
                trusted_predictions = self.teacher.predict(ordered)
            trusted_targets = [executable_target(
                sample, horizon_seconds=self.horizon_seconds,
                execution=self.execution) for sample in ordered]
            if (list(predictions) != trusted_predictions
                    or list(targets) != trusted_targets):
                raise ValueError(
                    "shared model predictions or targets failed recomputation")
            predictions = trusted_predictions
            targets = trusted_targets
        replayed = self.replayed_instruments_by_session[declared_session]
        if instrument in replayed:
            raise ValueError("model replay cell was consumed more than once")
        replayed.add(instrument)
        token = int.from_bytes(hashlib.sha256(
            (declared_session + "\0" + instrument + "\0" + identity +
             "\0" + str(len(ordered))).encode("utf-8")).digest(), "big")
        self._row_identity_xor ^= token
        self._row_identity_sum = (self._row_identity_sum + token) % (1 << 256)
        self._row_identity_cells += 1
        self.requested_instruments.add(instrument)
        if self.teacher is None:
            # A failed/missing calibration artifact is a resource-stop record,
            # never an invitation to consume predictions supplied by a caller.
            return
        # ``add_prepared`` is also a trust boundary: callers may share a pure
        # prediction pass, but the live frozen parameters must still verify at
        # the moment those predictions enter MODEL_CANDIDATE statistics.
        observed_identity = self.teacher.prediction_identity()
        if observed_identity[-1] != self.teacher_report.get(
                "model_fingerprint"):
            raise ValueError("model candidate live teacher identity changed")
        if not observed_sessions.issubset(self._expected_session_set):
            raise ValueError("model replay observed a non-frozen OOS session")
        if ordered:
            self.sampled_instruments.add(instrument)

        audit_status = str((audit or {}).get("status") or "FAIL").upper()
        if audit_status not in self.causality_counts:
            audit_status = "FAIL"
        self.causality_counts[audit_status] += 1
        if audit_status != "PASS" and len(self.causality_examples) < \
                MAX_CAUSALITY_EXAMPLES:
            self.causality_examples.append({
                "instrument_id": instrument,
                "status": audit_status,
                "findings": list(
                    (audit or {}).get("findings") or [])[:10],
            })

        values = [None if row is None else row.get("expected_net_bps")
                  for row in predictions]
        observations = _observations(
            ordered, values, horizon_seconds=self.horizon_seconds,
            threshold=self.minimum_predicted_edge_bps,
            execution=self.execution, position_mode="LONG_ONLY",
            entry_policy="POSITIVE_SCORE",
            fee_bps_per_side=self.spec.fee_bps_per_side,
            maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
            minimum_predicted_edge_bps=0.0)
        if observations:
            self.opportunity_instruments.add(instrument)
        for row in observations:
            self.opportunities += 1
            net = float(row["net_bps_per_opportunity"])
            self.net_sum += net
            self.instrument_net_sum[instrument] += net
            self.instrument_opportunities[instrument] += 1
            if row["net_bps_per_fill"] is not None:
                self.fills += 1
                self.fill_net_sum += float(row["net_bps_per_fill"])
            session = str(row["session"])
            self.session_net_sum[session] += net
            start = float(row["capital_start_timestamp"])
            end = float(row["capital_end_timestamp"])
            self.session_capital_deltas[session][start] += 1
            self.session_capital_deltas[session][end] -= 1

        for prediction, target in zip(predictions, targets):
            if prediction is None or target is None:
                continue
            markout, net, positive = target
            self.prediction_count += 1
            self.markout_squared_error += (
                float(prediction["expected_markout_bps"]) - markout) ** 2
            self.net_squared_error += (
                float(prediction["expected_net_bps"]) - net) ** 2
            self.brier_sum += (
                float(prediction["positive_net_probability"]) - positive) ** 2

    def _lineage(self) -> dict:
        return {
            "model_candidate_id": self.model_candidate_id,
            "model_fingerprint": self.teacher_report.get("model_fingerprint"),
            "teacher_version": self.teacher_report.get("version"),
            "feature_spec_hash": self.teacher_report.get("feature_spec_hash"),
            "feature_cube_spec_hash": self.teacher_report.get(
                "feature_cube_spec_hash"),
            "label_spec_hash": self.label_spec_hash,
            "label_version": self.teacher_report.get("label_version"),
            "configuration_hash": self.configuration_hash,
            "data_hash": self.data_hash,
            "split_hash": self.split_hash,
            "calibration_attestation_hash": (
                (self.calibration_attestation or {}).get("attestation_hash")),
            "hash_contract": "CANONICAL_JSON_SHA256_V1",
        }

    def finish(self) -> dict:
        calibration_observations = self.teacher_report.get("observations")
        calibration_class_counts = dict(
            self.teacher_report.get("class_counts") or {})
        calibration_enter_long_count = calibration_class_counts.get(
            "ENTER_LONG")
        calibration_enter_long_rate = None
        if (_finite_number(calibration_observations)
                and int(calibration_observations) > 0
                and _finite_number(calibration_enter_long_count)):
            calibration_enter_long_rate = (
                int(calibration_enter_long_count) /
                int(calibration_observations))
        calibration_session_count = int(
            self.teacher_report.get("sessions") or
            len(self.calibration_sessions))
        single_session_calibration = calibration_session_count < 2
        peaks = _capital_peaks(self.session_capital_deltas)
        session_returns = {
            session: self.session_net_sum[session] / max(1, peak)
            for session, peak in sorted(peaks.items())
        }
        values = [float(session_returns[session])
                  for session in self.expected_sessions]
        chronological_oos_blocks = [{
            "block": row["fold"],
            "start_session": row["start_session"],
            "end_session": row["end_session"],
            "sessions": row["sessions"],
            "mean_net_bps": row["mean_net_bps"],
            "positive": row["positive"],
            "method": "CONTIGUOUS_NON_OVERLAPPING_OOS_BLOCK",
        } for row in _contiguous_session_blocks(session_returns)]
        positive_chronological_oos_block_ratio = (
            sum(bool(row["positive"]) for row in chronological_oos_blocks) /
            len(chronological_oos_blocks)
            if chronological_oos_blocks else None)
        unadjusted_ci = _stationary_mean(values)
        adjusted_ci = _selection_adjusted_interval(
            values, declared_trials=self.declared_model_trials)
        # One frozen architecture is the only observed model vector in this
        # experiment.  DSR is reported for descriptive continuity, while the
        # release gate uses the selection-adjusted stationary interval above.
        dsr = deflated_sharpe(
            [value / 10_000.0 for value in values],
            trials=1, trial_sharpe_std=0.0, effective_trials=1.0,
            periods=252)
        expected_requested = len(self.expected_instruments)
        observed_requested = len(self.requested_instruments)
        sampled = len(self.sampled_instruments)
        coverage = sampled / expected_requested if expected_requested else 0.0
        instrument_means = {
            instrument: self.instrument_net_sum[instrument] /
            self.instrument_opportunities[instrument]
            for instrument in sorted(self.instrument_opportunities)
            if self.instrument_opportunities[instrument] > 0
        }
        positive_instrument_ratio = (
            sum(value > 0.0 for value in instrument_means.values()) /
            len(instrument_means) if instrument_means else None)
        summary = {
            "sessions": len(session_returns),
            "instruments": len(self.opportunity_instruments),
            "instruments_requested": expected_requested,
            "instruments_replay_requested": observed_requested,
            "instruments_with_samples": sampled,
            "instrument_coverage": coverage,
            "opportunities": self.opportunities,
            "fills": self.fills,
            "fill_rate": (self.fills / self.opportunities
                          if self.opportunities else None),
            "mean_net_bps_per_opportunity": (
                self.net_sum / self.opportunities
                if self.opportunities else None),
            "mean_net_bps_per_fill": (
                self.fill_net_sum / self.fills if self.fills else None),
            "session_mean_net_bps": fmean(values) if values else None,
            "session_net_ci_low_bps": unadjusted_ci.get("ci_low_bps"),
            "session_net_ci_high_bps": unadjusted_ci.get("ci_high_bps"),
            "selection_adjusted_session_ci_low_bps": adjusted_ci.get(
                "low_bps"),
            "selection_adjusted_session_ci_high_bps": adjusted_ci.get(
                "high_bps"),
            "positive_chronological_oos_block_ratio":
                positive_chronological_oos_block_ratio,
            "positive_instrument_ratio": positive_instrument_ratio,
            "worst_instrument_mean_net_bps_per_opportunity": (
                min(instrument_means.values()) if instrument_means else None),
            "sharpe": dsr.get("sharpe"),
            "deflated_sharpe": None,
            "single_model_null_dsr_diagnostic": dsr.get(
                "deflated_sharpe"),
            "max_concurrent_opportunities": max(peaks.values(), default=0),
        }
        failures: list[str] = []
        if not self.resource_preflight_passed:
            failures.append("MODEL_CALIBRATION_PREFLIGHT_NOT_PASS")
        if self.teacher_preflight_failure:
            failures.append(self.teacher_preflight_failure)
        if list(session_returns) != list(self.expected_sessions):
            failures.append("MODEL_OOS_SESSION_VECTOR_NOT_EXACT")
        if self.requested_instruments != self._expected_instrument_set:
            failures.append("MODEL_OOS_INSTRUMENT_VECTOR_NOT_EXACT")
        replay_cell_count = sum(
            len(values) for values in
            self.replayed_instruments_by_session.values())
        expected_replay_cell_count = (
            len(self.expected_sessions) * len(self.expected_instruments))
        row_identity_fingerprint = _canonical_hash({
            "cells": self._row_identity_cells,
            "xor": f"{self._row_identity_xor:064x}",
            "sum": f"{self._row_identity_sum:064x}",
        })
        if (replay_cell_count != expected_replay_cell_count
                or any(values != self._expected_instrument_set
                       for values in
                       self.replayed_instruments_by_session.values())):
            failures.append("MODEL_OOS_REPLAY_CELL_SET_NOT_EXACT")
        if self.causality_counts["FAIL"]:
            failures.append("MODEL_CAUSALITY_NOT_PASS")
        if not self.opportunities:
            failures.append("MODEL_NO_EXECUTABLE_OPPORTUNITIES")

        if self.evidence_scope == FULL_EVIDENCE_SCOPE:
            for metric, rule in (("sessions", "min_sessions"),
                                 ("instruments", "min_instruments"),
                                 ("opportunities", "min_opportunities")):
                if int(summary[metric] or 0) < int(self.rules[rule]):
                    failures.append(f"MODEL_{metric.upper()}_BELOW_MINIMUM")
            if coverage < float(self.rules["min_instrument_coverage"]):
                failures.append("MODEL_INSTRUMENT_COVERAGE_BELOW_MINIMUM")
            mean_net = summary["mean_net_bps_per_opportunity"]
            if (not _finite_number(mean_net)
                    or float(mean_net) <= float(
                        self.rules["min_mean_net_bps_per_opportunity"])):
                failures.append("MODEL_COST_NET_EDGE_NOT_POSITIVE")
            adjusted_low = summary[
                "selection_adjusted_session_ci_low_bps"]
            if adjusted_ci.get("failure_code"):
                failures.append(str(adjusted_ci["failure_code"]))
            if not _finite_number(adjusted_low) or float(adjusted_low) <= 0.0:
                failures.append("MODEL_SELECTION_ADJUSTED_CI_CROSSES_ZERO")
            if (not _finite_number(
                    positive_chronological_oos_block_ratio)
                    or float(positive_chronological_oos_block_ratio) < float(
                        self.rules["min_positive_session_ratio"])):
                failures.append(
                    "MODEL_CHRONOLOGICAL_OOS_BLOCKS_FRAGILE")
            if (not _finite_number(positive_instrument_ratio)
                    or float(positive_instrument_ratio) <
                    MIN_POSITIVE_INSTRUMENT_RATIO):
                failures.append("MODEL_CROSS_INSTRUMENT_FRAGILE")
            if self.execution == "PASSIVE_FIFO_LOWER_BOUND" and (
                    not _finite_number(summary["fill_rate"])
                    or float(summary["fill_rate"]) < float(
                        self.rules["min_passive_fill_rate"])):
                failures.append("MODEL_PASSIVE_FILL_RATE_TOO_LOW")

        failures = list(dict.fromkeys(failures))
        data_failures = {
            "MODEL_OOS_SESSION_VECTOR_NOT_EXACT",
            "MODEL_OOS_INSTRUMENT_VECTOR_NOT_EXACT",
            "MODEL_OOS_REPLAY_CELL_SET_NOT_EXACT",
            "MODEL_CAUSALITY_NOT_PASS",
        }
        if (not self.resource_preflight_passed or not self.opportunities
                or bool(data_failures.intersection(failures))):
            decision = "NO_EVIDENCE"
        elif self.evidence_scope in DISCOVERY_EVIDENCE_SCOPES:
            decision = "DISCOVERY_MEASURED"
        elif failures:
            decision = "HOLD"
        else:
            decision = "NOMINATE_FORWARD"
        failure_memory = {
            "version": MODEL_CANDIDATE_FAILURE_MEMORY_VERSION,
            "lane": MODEL_CANDIDATE_RESULT_NAMESPACE,
            "model_candidate_id": self.model_candidate_id,
            "failures": failures,
            "classification": failures[0] if failures else "NO_FAILURE",
            "summary": {
                key: summary.get(key) for key in (
                    "sessions", "instruments", "opportunities",
                    "instrument_coverage", "mean_net_bps_per_opportunity",
                    "selection_adjusted_session_ci_low_bps",
                    "positive_chronological_oos_block_ratio",
                    "positive_instrument_ratio")
            },
            "lineage": {
                "configuration_hash": self.configuration_hash,
                "data_hash": self.data_hash,
                "split_hash": self.split_hash,
                "decision_row_identity_fingerprint":
                    row_identity_fingerprint,
            },
            "reusable_for_model_evolution": True,
            "reusable_for_ast_evolution": False,
            "raw_data_absence_inferred": False,
            "promotion_authority": False,
        }
        return {
            "version": MODEL_CANDIDATE_VERSION,
            "result_namespace": MODEL_CANDIDATE_RESULT_NAMESPACE,
            "evidence_scope": self.evidence_scope,
            "lineage": self._lineage(),
            "resource_preflight": {
                "status": "PASS" if self.resource_preflight_passed else "FAIL",
                "failure_code": self.teacher_preflight_failure,
                "teacher_calibration_status": self.teacher_status,
                "teacher_calibration_observations":
                    calibration_observations,
                "teacher_calibration_session_count":
                    calibration_session_count,
                "teacher_calibration_sessions": list(
                    self.calibration_sessions),
                "teacher_calibration_class_counts":
                    calibration_class_counts,
                "teacher_calibration_enter_long_count":
                    calibration_enter_long_count,
                "teacher_calibration_enter_long_rate":
                    calibration_enter_long_rate,
                "rare_enter_long_class_warning": (
                    calibration_enter_long_rate is not None
                    and calibration_enter_long_rate < 0.01),
                "single_session_calibration_warning":
                    single_session_calibration,
                "regime_limitation": (
                    "SINGLE_SESSION_CALIBRATION_CANNOT_ESTABLISH_"
                    "REGIME_ROBUSTNESS"
                    if single_session_calibration else
                    "CALIBRATION_SESSION_COUNT_RECORDED;_OOS_STILL_"
                    "REQUIRED_FOR_REGIME_EVIDENCE"),
                "purpose": "COMPUTE_ALLOCATION_ONLY",
                "alpha_evidence": False,
                "promotion_authority": False,
            },
            "evaluation_design": {
                "scheduled_oos_sessions": len(self.expected_sessions),
                "calibration_session_count": calibration_session_count,
                "calibration_and_oos_roles_separate": True,
                "interpretation": (
                    "COST_NET_GENERALIZATION_TEST_OF_ONE_FROZEN_"
                    "CALIBRATION_REGIME;_NOT_MULTI_REGIME_TRAINING_EVIDENCE"
                    if single_session_calibration else
                    "COST_NET_GENERALIZATION_TEST_OF_A_FROZEN_"
                    "CALIBRATION_ARTIFACT;_NOT_ADDITIONAL_TRAINING"),
                "accuracy_is_promotion_gate": False,
            },
            "frozen_contract": {
                "features": "FROZEN_TEACHER_FEATURE_SPEC_HASH",
                "feature_selection": "NONE_FIXED_PUBLIC_SPEC",
                "missing_value_policy":
                    "FROZEN_ZERO_PLUS_PER_COORDINATE_MISSING_FLAG",
                "normalization":
                    "CALIBRATION_MEANS_AND_SCALES_FROZEN_BEFORE_OOS",
                "labels": "EXECUTABLE_NET_BPS_PER_OPPORTUNITY",
                "split": "STRICTLY_PRIOR_CALIBRATION_THEN_FROZEN_OOS",
                "purge_gap_seconds": self.spec.purge_gap.total_seconds(),
                "maximum_label_horizon_seconds": max(
                    self.spec.horizons_seconds),
                "execution": self.execution,
                "horizon_seconds": self.horizon_seconds,
                "cost_inputs": self.teacher_report.get("cost_inputs") or {},
                "entry_hurdle": (
                    "FROZEN_EXPECTED_EXECUTABLE_NET_BPS_PER_OPPORTUNITY > "
                    "minimum_predicted_edge_bps"),
                "minimum_predicted_edge_bps":
                    self.minimum_predicted_edge_bps,
                "positive_class_model":
                    "FROZEN_CALIBRATION_ONLY_RIDGE",
                "class_counts_source": "CALIBRATION_ONLY",
                "threshold_source":
                    "FROZEN_CONFIGURATION_BEFORE_OOS",
                "minimum_positive_instrument_ratio":
                    MIN_POSITIVE_INSTRUMENT_RATIO,
                "hyperparameter_search": False,
                "oos_fit_forbidden": True,
                "oos_threshold_tuning_forbidden": True,
                "oos_feature_selection_forbidden": True,
                "sampling_execution_manifest": json.loads(json.dumps(
                    self.sampling_execution_manifest)),
                "calibration_attestation_hash": (
                    (self.calibration_attestation or {}).get(
                        "attestation_hash")),
                "split_manifest": json.loads(json.dumps(
                    self.split_manifest)),
            },
            "selection_record": {
                "version": MODEL_CANDIDATE_MULTIPLE_TESTING_VERSION,
                "declared_model_trials": self.declared_model_trials,
                "count_components": dict(self.selection_count_components),
                "current_frozen_model_variants": 1,
                "selection_count_policy":
                    "APPEND_ONLY_INTRADAY_TRIAL_COUNT_UPPER_BOUND",
                "historical_return_vectors_fabricated": False,
                "cross_model_dispersion_fabricated": False,
                "dsr_gate_status": (
                    "NOT_USED_CROSS_MODEL_DISPERSION_UNAVAILABLE; "
                    "BONFERRONI_SESSION_INTERVAL_IS_THE_ACTIVE_GATE"),
                "adjusted_interval": adjusted_ci,
                "pbo": "NOT_APPLICABLE_SINGLE_FROZEN_MODEL_IN_THIS_RUNG",
            },
            "prediction_diagnostics": {
                "observations": self.prediction_count,
                "markout_rmse_bps": math.sqrt(
                    self.markout_squared_error / self.prediction_count)
                if self.prediction_count else None,
                "executable_net_rmse_bps": math.sqrt(
                    self.net_squared_error / self.prediction_count)
                if self.prediction_count else None,
                "positive_net_brier": self.brier_sum / self.prediction_count
                if self.prediction_count else None,
            },
            "causality": {
                "counts_by_status": dict(self.causality_counts),
                "bounded_examples": list(self.causality_examples),
                "examples_truncated": sum(self.causality_counts.values()) >
                    len(self.causality_examples) + self.causality_counts["PASS"],
            },
            "replay_completeness": {
                "expected_sessions": len(self.expected_sessions),
                "expected_instruments": len(self.expected_instruments),
                "expected_cells": expected_replay_cell_count,
                "observed_cells": replay_cell_count,
                "exact": replay_cell_count == expected_replay_cell_count
                    and all(values == self._expected_instrument_set
                            for values in
                            self.replayed_instruments_by_session.values()),
                "decision_row_identity_contract":
                    "COMMUTATIVE_CELL_SHA256_XOR0_SUM1_V1",
                "decision_row_identity_fingerprint":
                    row_identity_fingerprint,
            },
            "chronological_oos_blocks": chronological_oos_blocks,
            "session_returns_bps": session_returns,
            "summary": summary,
            "failed_criteria": failures,
            "failure_memory": failure_memory,
            "decision": decision,
            "ast_dependency": False,
            "ast_gate_authority": False,
            "independent_confirmation": False,
            "historical_search_exposed": True,
            "forward_new_sessions_required": decision == "NOMINATE_FORWARD",
            "promotion_authority": False,
            "order_authority": False,
        }


def discovery_resource_gate(report: dict, *, minimum_opportunities: int) -> dict:
    """Bound expensive replay allocation; never make an alpha claim."""
    summary = report.get("summary") or {}
    opportunities = summary.get("opportunities")
    upper = summary.get("session_net_ci_high_bps")
    preflight = (report.get("resource_preflight") or {}).get("status") == "PASS"
    replay_exact = (report.get("replay_completeness") or {}).get(
        "exact") is True
    causality_counts = ((report.get("causality") or {}).get(
        "counts_by_status") or {})
    causality_failures = int(causality_counts.get("FAIL") or 0)
    passed = bool(
        preflight
        and replay_exact
        and causality_failures == 0
        and _finite_number(opportunities)
        and float(opportunities) >= int(minimum_opportunities)
        and _finite_number(upper)
        and float(upper) > 0.0)
    return {
        "version": "krx-model-candidate-discovery-resource-gate-v1",
        "pass": passed,
        "minimum_opportunities": int(minimum_opportunities),
        "observed_opportunities": opportunities,
        "futility_rule": "STATIONARY_BOOTSTRAP_UCB_MUST_EXCEED_ZERO",
        "observed_session_ucb_bps": upper,
        "teacher_calibration_is_resource_preflight_only": True,
        "exact_replay_required": True,
        "exact_replay_observed": replay_exact,
        "causality_failures": causality_failures,
        "alpha_evidence": False,
        "promotion_authority": False,
    }


def _self_check() -> None:
    digest = _canonical_hash({"lane": MODEL_CANDIDATE_RESULT_NAMESPACE})
    assert len(digest) == 64
    interval = _selection_adjusted_interval(
        [1.0, 1.0, 1.0], declared_trials=2, n_boot=100)
    assert interval["low_bps"] == 1.0
    assert interval["per_candidate_two_sided_alpha"] == 0.025
    assert discovery_resource_gate({
        "resource_preflight": {"status": "PASS"},
        "replay_completeness": {"exact": True},
        "causality": {"counts_by_status": {"FAIL": 0}},
        "summary": {"opportunities": 100, "session_net_ci_high_bps": 0.1},
    }, minimum_opportunities=100)["pass"] is True


if __name__ == "__main__":
    _self_check()
    print("intraday_model_candidate self-check passed")
