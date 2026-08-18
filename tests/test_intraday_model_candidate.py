from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_supervised as supervised
import intraday_candidate as candidate_module
import intraday_experiment_runner as runner
import intraday_model_candidate as model_candidate_module
from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    LEGACY_FEATURE_WINDOW_CONTRACT,
    PRIMITIVE_WINDOWS_SECONDS,
    WINDOWED_FIELDS,
)
from intraday_candidate import CandidateAccumulator, CandidatePopulationAccumulator
from intraday_microstructure import (
    COMPLETED_SECOND_POLICY,
    EXTERNAL_EVENT_SOURCE,
    FeatureCubeSpec,
    HorizonLabel,
    IntradayLaneSpec,
    IntradaySample,
    IntradaySampleBatch,
    MultiScaleFeatureCube,
    _decision_index_fingerprint,
    audit_causality,
)
from intraday_model_candidate import (
    BOOTSTRAP_RESOLUTION_FAILURE,
    ModelCandidateAccumulator,
    _selection_adjusted_interval,
    discovery_resource_gate,
    model_split_manifest,
)
from intraday_supervised import attach_calibration_attestation


def _hash(character: str) -> str:
    return character * 64


def _sample(instrument: str, at: datetime, *, signal: float = 1.0,
            markout: float = 3.0, net: float = 2.0) -> IntradaySample:
    label = HorizonLabel(
        horizon_seconds=5,
        exit_time=at + timedelta(seconds=5),
        future_mid=100.03,
        long_mid_markout_bps=markout,
        short_mid_markout_bps=-markout,
        long_taker_net_bps=net,
        short_taker_net_bps=-net,
        long_passive_filled=False,
        short_passive_filled=False,
        long_passive_fill_time=None,
        short_passive_fill_time=None,
        long_passive_net_bps=None,
        short_passive_net_bps=None,
    )
    return IntradaySample(
        instrument_id=instrument,
        decision_time=at,
        entry_time=at,
        source_quote_event_time=at,
        quote_age_ms=10.0,
        spread_bps=2.0,
        queue_imbalance_l1=signal,
        queue_imbalance_l10=signal / 2.0,
        microprice_offset_bps=signal,
        trade_flow_imbalance=signal,
        quote_event_ofi=signal * 10.0,
        normalized_quote_ofi=signal,
        bid_depth_l1=100.0,
        ask_depth_l1=100.0,
        book_depth_l1=200.0,
        book_depth_l10=2_000.0,
        trade_count=2,
        quote_count=3,
        trade_intensity=1.0,
        realized_volatility_bps=2.0,
        entry_bid_depth_l1=100.0,
        entry_ask_depth_l1=100.0,
        entry_bid=99.99,
        entry_ask=100.01,
        entry_mid=100.0,
        labels=(label,),
        multi_level_quote_ofi_l10=signal * 20.0,
        normalized_multi_level_quote_ofi_l10=signal,
        depth_imbalance_slope=signal,
        quote_ofi_depth_divergence=signal,
        quote_event_transition_count=4,
        normalized_quote_ofi_per_event=signal,
        signed_trade_volume=signal * 30.0,
        trade_volume=40.0,
        trade_side_known_ratio=0.75,
        quote_ofi_per_trade_volume=signal,
    )


def _explicit_batch(instrument: str, times: list[datetime], *,
                    offset: float = 0.0) -> IntradaySampleBatch:
    samples = tuple(
        _sample(instrument, at, signal=float(index + 1) + offset,
                markout=3.0, net=2.0)
        for index, at in enumerate(times)
    )
    columns = []
    for field_index, field in enumerate(sorted(WINDOWED_FIELDS)):
        for seconds in PRIMITIVE_WINDOWS_SECONDS:
            values = tuple(
                (field_index + 1) * 0.01 + seconds * 0.001
                + row_index * 0.1 + offset
                for row_index in range(len(samples)))
            columns.append((field, seconds, values))
    cube = MultiScaleFeatureCube(
        spec=FeatureCubeSpec(), row_count=len(samples),
        decision_index_fingerprint=_decision_index_fingerprint(samples),
        columns=tuple(columns))
    return IntradaySampleBatch(samples, cube)


def _teacher_report(monkeypatch, spec: IntradayLaneSpec, *,
                    markout: float = 3.0) -> dict:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    monkeypatch.setattr(candidate_module, "MIN_CALIBRATION_OBSERVATIONS", 4)
    monkeypatch.setattr(
        candidate_module, "MIN_CALIBRATION_NONZERO_SCORES", 2)
    monkeypatch.setattr(candidate_module, "MIN_CALIBRATION_INSTRUMENTS", 2)
    base = datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs={
            "fee_bps_per_side": float(spec.fee_bps_per_side),
            "maker_fee_bps_per_side": float(spec.maker_fee_bps_per_side),
            "passive_nonfill_net_bps_per_opportunity": 0.0,
        },
        feature_window_contract_version=LEGACY_FEATURE_WINDOW_CONTRACT)
    for offset, instrument in enumerate(("A", "B")):
        teacher.calibrate(instrument, [
            _sample(
                instrument, base + timedelta(seconds=5 * (offset * 3 + index)),
                signal=float(index + 1 + offset), markout=markout, net=2.0)
            for index in range(3)
        ])
    report = teacher.freeze()
    assert report["status"] == "PASS"
    report["_model_calibration_evidence"] = teacher.calibration_evidence()
    return report


def _model(report: dict, spec: IntradayLaneSpec, *, sessions: list[str],
           instruments: list[str], scope: str = "FULL_60",
           trials: int = 3, criteria: dict | None = None
           ) -> ModelCandidateAccumulator:
    clean_report = copy.deepcopy(report)
    calibration_evidence = clean_report.pop(
        "_model_calibration_evidence", None)
    if calibration_evidence is None:
        raise ValueError("test teacher report lacks calibration sidecar")
    calibration_sessions = sorted(
        str(value) for value in clean_report["session_ids"])
    calibration_instruments = sorted(
        str(value) for value in calibration_evidence["instrument_ids"])
    feature_contract = clean_report.get(
        "feature_window_contract_version",
        LEGACY_FEATURE_WINDOW_CONTRACT)
    sampling_manifest = {
        "version": "test-model-sampling-execution-manifest-v1",
        "spec": {
            "sample_interval_seconds": spec.sample_interval_seconds,
            "feature_lookback_seconds": spec.feature_lookback_seconds,
            "horizons_seconds": list(spec.horizons_seconds),
            "order_latency_ms": spec.order_latency_ms,
            "max_quote_age_seconds": spec.max_quote_age_seconds,
            "fee_bps_per_side": spec.fee_bps_per_side,
            "maker_fee_bps_per_side": spec.maker_fee_bps_per_side,
        },
        "feature_window_contract_version": feature_contract,
        "horizon_seconds": 5,
        "execution": "TAKER",
        "minimum_predicted_edge_bps": 0.1,
    }
    source_contract = {
        "version": "test-calibration-source-v1",
        "knowledge_cutoff": "2026-01-02T23:59:59+00:00",
        "source_lineage_fingerprint": _hash("d"),
    }
    attested = attach_calibration_attestation(
        clean_report, planned_session_ids=calibration_sessions,
        planned_instruments=calibration_instruments,
        sample_contract=sampling_manifest, source_contract=source_contract,
        calibration_evidence=calibration_evidence)
    split = model_split_manifest(
        calibration_sessions=calibration_sessions,
        contributing_calibration_sessions=clean_report["session_ids"],
        evaluation_sessions=sessions,
        calibration_instruments=calibration_instruments,
        contributing_calibration_instruments=calibration_evidence[
            "instrument_ids"],
        evaluation_instruments=instruments, spec=spec, rung=scope)
    return ModelCandidateAccumulator(
        teacher_report=attested, spec=spec, horizon_seconds=5,
        execution="TAKER", minimum_predicted_edge_bps=0.1,
        feature_window_contract_version=feature_contract,
        expected_calibration_sessions=calibration_sessions,
        expected_calibration_instruments=calibration_instruments,
        expected_evaluation_sessions=sessions,
        expected_instruments=instruments, evidence_scope=scope,
        configuration_hash=runner.stable_fingerprint(sampling_manifest),
        data_hash=runner.stable_fingerprint(source_contract),
        split_hash=runner.stable_fingerprint(split),
        sampling_execution_manifest=sampling_manifest,
        declared_model_trials=trials,
        criteria=criteria)


def _recreate(model: ModelCandidateAccumulator, *, teacher_report: dict,
              sampling_manifest: dict | None = None,
              configuration_hash: str | None = None,
              split_hash: str | None = None
              ) -> ModelCandidateAccumulator:
    manifest = sampling_manifest or model.sampling_execution_manifest
    return ModelCandidateAccumulator(
        teacher_report=teacher_report, spec=model.spec,
        horizon_seconds=model.horizon_seconds, execution=model.execution,
        minimum_predicted_edge_bps=model.minimum_predicted_edge_bps,
        feature_window_contract_version=
        model.feature_window_contract_version,
        expected_calibration_sessions=model.expected_calibration_sessions,
        expected_calibration_instruments=model.expected_calibration_instruments,
        expected_evaluation_sessions=model.expected_sessions,
        expected_instruments=model.expected_instruments,
        evidence_scope=model.evidence_scope,
        configuration_hash=configuration_hash or model.configuration_hash,
        data_hash=model.data_hash, split_hash=split_hash or model.split_hash,
        sampling_execution_manifest=manifest,
        declared_model_trials=model.declared_model_trials,
        selection_count_components=model.selection_count_components,
        criteria=model.rules)


def test_calibration_pass_is_preflight_not_alpha_or_promotion(monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    model = _model(
        teacher, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")

    report = model.finish()

    assert report["resource_preflight"]["status"] == "PASS"
    assert report["resource_preflight"]["alpha_evidence"] is False
    assert report["resource_preflight"][
        "teacher_calibration_observations"] == 6
    assert report["resource_preflight"][
        "teacher_calibration_session_count"] == 1
    assert report["resource_preflight"][
        "teacher_calibration_class_counts"]["ENTER_LONG"] == 6
    assert report["resource_preflight"][
        "teacher_calibration_enter_long_rate"] == 1.0
    assert report["resource_preflight"][
        "single_session_calibration_warning"] is True
    assert report["resource_preflight"]["regime_limitation"] == \
        "SINGLE_SESSION_CALIBRATION_CANNOT_ESTABLISH_REGIME_ROBUSTNESS"
    assert report["evaluation_design"]["scheduled_oos_sessions"] == 1
    assert report["evaluation_design"][
        "calibration_and_oos_roles_separate"] is True
    assert report["frozen_contract"]["class_counts_source"] == \
        "CALIBRATION_ONLY"
    assert report["frozen_contract"]["threshold_source"] == \
        "FROZEN_CONFIGURATION_BEFORE_OOS"
    assert report["decision"] == "NO_EVIDENCE"
    assert report["promotion_authority"] is False
    assert report["order_authority"] is False
    assert report["ast_dependency"] is False


def test_full_model_lane_has_cost_net_chronological_block_stability_and_adjustment(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    start = datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc)
    sessions = [(start + timedelta(days=index)).date().isoformat()
                for index in range(60)]
    model = _model(
        teacher, spec, sessions=sessions, instruments=["A", "B"], trials=7)
    frozen_before_oos = model.teacher.report()
    for index in range(60):
        at = start + timedelta(days=index)
        for instrument in ("A", "B"):
            model.add(instrument, [
                _sample(instrument, at, signal=1.0 + index / 100.0,
                        markout=3.0, net=2.0)
            ])

    report = model.finish()

    assert report["decision"] == "NOMINATE_FORWARD"
    assert report["summary"]["sessions"] == 60
    assert report["evaluation_design"]["scheduled_oos_sessions"] == 60
    assert report["evaluation_design"]["interpretation"] == \
        "COST_NET_GENERALIZATION_TEST_OF_ONE_FROZEN_CALIBRATION_REGIME;_" \
        "NOT_MULTI_REGIME_TRAINING_EVIDENCE"
    assert report["summary"]["opportunities"] == 120
    assert report["summary"]["mean_net_bps_per_opportunity"] == 2.0
    assert report["summary"][
        "selection_adjusted_session_ci_low_bps"] > 0.0
    assert report["summary"][
        "positive_chronological_oos_block_ratio"] == 1.0
    assert all(row["method"] ==
               "CONTIGUOUS_NON_OVERLAPPING_OOS_BLOCK"
               for row in report["chronological_oos_blocks"])
    assert report["summary"]["positive_instrument_ratio"] == 1.0
    assert report["selection_record"]["declared_model_trials"] == 7
    assert report["selection_record"]["historical_return_vectors_fabricated"] \
        is False
    assert report["summary"]["deflated_sharpe"] is None
    assert report["selection_record"]["dsr_gate_status"].startswith(
        "NOT_USED")
    assert report["selection_record"]["adjusted_interval"][
        "per_candidate_two_sided_alpha"] == pytest.approx(0.05 / 7)
    assert report["frozen_contract"]["oos_fit_forbidden"] is True
    assert report["frozen_contract"]["oos_threshold_tuning_forbidden"] is True
    assert report["frozen_contract"]["oos_feature_selection_forbidden"] is True
    assert report["frozen_contract"]["purge_gap_seconds"] == 10.0
    assert model.teacher.report() == frozen_before_oos
    assert report["frozen_contract"]["cost_inputs"][
        "fee_bps_per_side"] == 0.1
    assert report["failed_criteria"] == []
    assert all(len(report["lineage"][key]) == 64 for key in (
        "model_candidate_id", "model_fingerprint", "feature_spec_hash",
        "label_spec_hash", "configuration_hash", "data_hash", "split_hash"))


def test_ast_calibration_failure_does_not_erase_model_oos(monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    base = datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)
    candidate = CandidateAccumulator(
        expr={"op": "field", "field": "trade_flow_imbalance"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        coefficient_policy="STRUCTURE_ONLY",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        feature_window_contract_version=LEGACY_FEATURE_WINDOW_CONTRACT,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1})
    population = CandidatePopulationAccumulator({"PRIMARY": candidate})
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    monkeypatch.setattr(candidate_module, "MIN_CALIBRATION_OBSERVATIONS", 4)
    monkeypatch.setattr(
        candidate_module, "MIN_CALIBRATION_NONZERO_SCORES", 2)
    monkeypatch.setattr(candidate_module, "MIN_CALIBRATION_INSTRUMENTS", 2)
    # Positive AST scores with negative markouts force the symbolic coefficient
    # to zero, while the independently labelled executable-net model remains
    # usable.  The inconsistency is intentional: this is a lane-isolation test.
    for offset, instrument in enumerate(("A", "B")):
        population.calibrate(instrument, [
            _sample(
                instrument, base + timedelta(seconds=5 * (offset * 3 + index)),
                signal=float(index + 1), markout=-3.0, net=2.0)
            for index in range(3)
        ])
    calibration = population.freeze_calibration()
    assert calibration["PRIMARY"]["status"] == \
        "NON_POSITIVE_DIRECTIONAL_RELATION"
    assert calibration["PRIMARY"]["supervised_control"]["status"] == "PASS"
    model = _model(
        {
            **calibration["PRIMARY"]["supervised_control"],
            "_model_calibration_evidence": calibration["PRIMARY"][
                "supervised_control_calibration_evidence"],
        }, spec,
        sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6", trials=1)
    evaluation = [
        _sample("A", base + timedelta(days=1), signal=1.0,
                markout=3.0, net=2.0)
    ]
    # This mirrors the runtime's calibration-only AST fail-fast branch: only
    # the standalone model consumes the OOS rows.
    model.add("A", evaluation)

    ast_report = population.finish()["PRIMARY"]
    model_report = model.finish()
    assert ast_report["summary"]["opportunities"] == 0
    assert model_report["summary"]["opportunities"] == 1
    assert model_report["ast_dependency"] is False
    assert discovery_resource_gate(
        model_report, minimum_opportunities=1)["pass"] is False
    # One scheduled session cannot produce a stationary UCB; this is measured
    # OOS, not fabricated evidence or an allocation pass.
    assert model_report["decision"] == "DISCOVERY_MEASURED"


def test_positive_aggregate_cannot_hide_cross_instrument_fragility(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    start = datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc)
    sessions = [(start + timedelta(days=index)).date().isoformat()
                for index in range(60)]
    model = _model(
        teacher, spec, sessions=sessions, instruments=["A", "B"], trials=1)
    for index, session in enumerate(sessions):
        at = start + timedelta(days=index)
        model.add("A", [_sample("A", at, net=2.0)],
                  evaluation_session=session)
        model.add("B", [_sample("B", at, net=-1.0)],
                  evaluation_session=session)

    report = model.finish()

    assert report["summary"]["mean_net_bps_per_opportunity"] == 0.5
    assert report["summary"]["positive_instrument_ratio"] == 0.5
    assert "MODEL_CROSS_INSTRUMENT_FRAGILE" in report["failed_criteria"]
    assert report["decision"] == "HOLD"


def test_discovery_gate_fails_closed_on_missing_replay_cells(monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    start = datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc)
    sessions = [start.date().isoformat(),
                (start + timedelta(days=1)).date().isoformat()]
    model = _model(
        teacher, spec, sessions=sessions, instruments=["A", "B"],
        scope="DISCOVERY_6", trials=1)
    for index, session in enumerate(sessions):
        model.add("A", [_sample("A", start + timedelta(days=index), net=2.0)],
                  evaluation_session=session)

    report = model.finish()
    gate = discovery_resource_gate(report, minimum_opportunities=1)

    assert report["summary"]["session_net_ci_high_bps"] > 0.0
    assert "MODEL_OOS_REPLAY_CELL_SET_NOT_EXACT" in report["failed_criteria"]
    assert report["decision"] == "NO_EVIDENCE"
    assert gate["exact_replay_observed"] is False
    assert gate["pass"] is False


def test_population_reuses_rows_but_model_recomputes_without_ast_coupling(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    candidate = CandidateAccumulator(
        expr={"op": "field", "field": "trade_flow_imbalance"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        feature_window_contract_version=LEGACY_FEATURE_WINDOW_CONTRACT,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1})
    population = CandidatePopulationAccumulator({"PRIMARY": candidate})
    teacher = _teacher_report(monkeypatch, spec)
    candidate.teacher.restore(teacher)
    model = _model(
        teacher, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6", trials=1)
    primary_calls = 0
    model_calls = 0
    original_predict = candidate.teacher.predict
    assert model.teacher is not None
    original_model_predict = model.teacher.predict

    def counted_predict(*args, **kwargs):
        nonlocal primary_calls
        primary_calls += 1
        return original_predict(*args, **kwargs)

    def counted_model_predict(*args, **kwargs):
        nonlocal model_calls
        model_calls += 1
        return original_model_predict(*args, **kwargs)

    monkeypatch.setattr(candidate.teacher, "predict", counted_predict)
    monkeypatch.setattr(model.teacher, "predict", counted_model_predict)
    sample = _sample(
        "A", datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc), net=2.0)
    population.add("A", [sample], model_candidate=model)

    assert primary_calls == 1
    assert model_calls == 1
    assert model.finish()["summary"]["opportunities"] == 1


def test_legacy_shared_predictions_and_targets_are_recomputed_before_use(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    model = _model(
        _teacher_report(monkeypatch, spec), spec,
        sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6", trials=1)
    sample = _sample(
        "A", datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc), net=2.0)
    assert model.teacher is not None
    predictions = model.teacher.predict([sample])
    targets = [supervised.executable_target(
        sample, horizon_seconds=5, execution="TAKER")]
    assert predictions[0] is not None
    assert targets[0] is not None
    injected_predictions = copy.deepcopy(predictions)
    injected_predictions[0]["expected_net_bps"] += 1_000.0
    markout, net, positive = targets[0]
    injected_targets = [(markout, net + 1_000.0, positive)]

    with pytest.raises(
            ValueError, match="predictions or targets failed recomputation"):
        model.add_prepared(
            "A", [sample], {"status": "PASS", "findings": []},
            predictions=injected_predictions, targets=injected_targets,
            evaluation_session="2026-01-03",
            row_identity=_decision_index_fingerprint([sample]))

    assert model.requested_instruments == set()
    assert model.replayed_instruments_by_session["2026-01-03"] == set()
    assert model.opportunities == 0


def test_explicit_cube_shared_predictions_and_targets_are_recomputed_before_use(
        monkeypatch) -> None:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    base = datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs={
            "fee_bps_per_side": 0.1,
            "maker_fee_bps_per_side": 0.0,
            "passive_nonfill_net_bps_per_opportunity": 0.0,
        },
        feature_window_contract_version=EXPLICIT_FEATURE_WINDOW_CONTRACT)
    for batch in (
            _explicit_batch("A", [base + timedelta(seconds=5 * index)
                                  for index in range(3)]),
            _explicit_batch("B", [base + timedelta(seconds=20 + 5 * index)
                                  for index in range(3)], offset=0.5)):
        teacher.calibrate(
            batch[0].instrument_id, batch, feature_cube=batch.feature_cube)
    frozen = teacher.freeze()
    frozen["_model_calibration_evidence"] = teacher.calibration_evidence()
    model = _model(
        frozen, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6", trials=1)
    batch = _explicit_batch(
        "A", [base + timedelta(days=1)], offset=10_000.0)
    assert model.teacher is not None
    predictions = model.teacher.predict(
        list(batch), feature_cube=batch.feature_cube)
    targets = [supervised.executable_target(
        sample, horizon_seconds=5, execution="TAKER") for sample in batch]
    assert predictions[0] is not None
    assert targets[0] is not None
    injected_predictions = copy.deepcopy(predictions)
    injected_predictions[0]["expected_markout_bps"] -= 1_000.0
    markout, net, positive = targets[0]
    injected_targets = [(markout - 1_000.0, net, positive)]

    with pytest.raises(
            ValueError, match="predictions or targets failed recomputation"):
        model.add_prepared(
            "A", list(batch), {"status": "PASS", "findings": []},
            predictions=injected_predictions, targets=injected_targets,
            evaluation_session="2026-01-03",
            row_identity=batch.feature_cube.decision_index_fingerprint,
            feature_cube=batch.feature_cube)

    assert model.requested_instruments == set()
    assert model.replayed_instruments_by_session["2026-01-03"] == set()
    assert model.opportunities == 0


def test_model_split_is_fail_closed_on_calibration_overlap(monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    with pytest.raises(ValueError, match="strictly precede"):
        _model(
            teacher, spec, sessions=[teacher["session_ids"][0]],
            instruments=["A"], scope="DISCOVERY_6")


def test_runner_seals_model_config_data_and_split_hashes(monkeypatch) -> None:
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=30,
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    config = {
        "horizon_seconds": 5,
        "execution": "TAKER",
        "minimum_predicted_edge_bps": 0.1,
        "feature_window_contract_version": LEGACY_FEATURE_WINDOW_CONTRACT,
        "timestamp_policy": COMPLETED_SECOND_POLICY,
        "population_execution_model": "TAKER",
        "selection_adjusted_trials": 11,
    }
    selected = {
        "event_source": EXTERNAL_EVENT_SOURCE,
        "calibration_sessions": ["2026-01-02"],
    }
    teacher_evidence = teacher.pop("_model_calibration_evidence")
    model = runner._model_candidate_accumulator(
        config, spec,
        calibration={"PRIMARY": {
            "supervised_control": teacher,
            "supervised_control_calibration_evidence": teacher_evidence,
        }},
        selected=selected,
        source_lineage=[{"content_fingerprint": _hash("d")}],
        evaluation_sessions=["2026-01-03", "2026-01-04"],
        instruments=["A", "B"], rung="DISCOVERY_6",
        knowledge_cutoff=datetime(2026, 1, 2, 23, 0,
                                  tzinfo=timezone.utc))

    report = model.finish()
    assert report["selection_record"]["declared_model_trials"] == 12
    assert report["selection_record"]["count_components"] == {
        "append_only_ast_and_sidecar_exposures": 11,
        "append_only_model_candidate_exposures": 1,
        "declared_total": 12,
    }
    assert report["lineage"]["configuration_hash"] not in {
        report["lineage"]["data_hash"], report["lineage"]["split_hash"]}
    assert report["lineage"]["data_hash"] != report["lineage"]["split_hash"]


def test_runner_maps_legacy_pass_without_sidecar_to_typed_no_evidence(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=30,
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    legacy = _teacher_report(monkeypatch, spec)
    legacy.pop("_model_calibration_evidence")
    config = {
        "horizon_seconds": 5,
        "execution": "TAKER",
        "minimum_predicted_edge_bps": 0.1,
        "feature_window_contract_version": LEGACY_FEATURE_WINDOW_CONTRACT,
        "timestamp_policy": COMPLETED_SECOND_POLICY,
        "population_execution_model": "TAKER",
        "selection_adjusted_trials": 1,
    }
    model = runner._model_candidate_accumulator(
        config, spec,
        calibration={"PRIMARY": {"supervised_control": legacy}},
        selected={
            "event_source": EXTERNAL_EVENT_SOURCE,
            "calibration_sessions": ["2026-01-02"],
        },
        source_lineage=[{"content_fingerprint": _hash("d")}],
        evaluation_sessions=["2026-01-03"], instruments=["A", "B"],
        rung="DISCOVERY_6",
        knowledge_cutoff=datetime(2026, 1, 2, 23, 0,
                                  tzinfo=timezone.utc))

    report = model.finish()

    assert report["decision"] == "NO_EVIDENCE"
    assert report["resource_preflight"]["failure_code"] == \
        "MODEL_TEACHER_ATTESTATION_MISSING"
    assert "MODEL_TEACHER_ATTESTATION_MISSING" in report["failed_criteria"]


def test_model_allocation_never_rewrites_failed_ast_primary() -> None:
    ast = {
        "version": "ast-gate-v1",
        "primary_pass": False,
        "survivors": [],
        "promotion_authority": False,
    }
    model = {"pass": True, "promotion_authority": False}

    combined = runner._combined_discovery_gate(ast, model)

    assert combined["primary_pass"] is False
    assert combined["survivors"] == []
    assert combined["model_candidate_pass"] is True
    assert combined["allocation_pass"] is True
    assert combined["allocation_paths"] == ["MODEL_CANDIDATE"]
    assert combined["promotion_authority"] is False


def test_teacher_v4_missing_flags_and_normalization_remain_frozen_oos(
        monkeypatch) -> None:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    base = datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs={
            "fee_bps_per_side": 0.1,
            "maker_fee_bps_per_side": 0.0,
            "passive_nonfill_net_bps_per_opportunity": 0.0,
        },
        feature_window_contract_version=EXPLICIT_FEATURE_WINDOW_CONTRACT)
    for batch in (
            _explicit_batch("A", [base + timedelta(seconds=5 * index)
                                  for index in range(3)]),
            _explicit_batch("B", [base + timedelta(seconds=20 + 5 * index)
                                  for index in range(3)], offset=0.5)):
        teacher.calibrate(
            batch[0].instrument_id, batch, feature_cube=batch.feature_cube)
    frozen = teacher.freeze()
    frozen["_model_calibration_evidence"] = teacher.calibration_evidence()
    assert frozen["status"] == "PASS"
    assert len(frozen["features"]) == 244
    model = _model(
        frozen, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6", trials=1)
    frozen_before_oos = model.teacher.report()
    evaluation = _explicit_batch(
        "A", [base + timedelta(days=1)], offset=10_000.0)

    model.add("A", evaluation, evaluation_session="2026-01-03")
    report = model.finish()

    assert model.teacher.report() == frozen_before_oos
    assert report["frozen_contract"]["missing_value_policy"] == \
        "FROZEN_ZERO_PLUS_PER_COORDINATE_MISSING_FLAG"
    assert report["frozen_contract"]["normalization"] == \
        "CALIBRATION_MEANS_AND_SCALES_FROZEN_BEFORE_OOS"
    assert report["replay_completeness"]["exact"] is True
    assert len(report["replay_completeness"][
        "decision_row_identity_fingerprint"]) == 64


def test_attestation_blocks_session_id_rewrite_that_legacy_fingerprint_missed(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    model = _model(
        teacher, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")
    tampered = copy.deepcopy(model.teacher_report)
    original_fingerprint = tampered["model_fingerprint"]
    tampered["session_ids"] = ["2026-01-01"]
    tampered["sessions"] = 1

    assert tampered["model_fingerprint"] == original_fingerprint
    with pytest.raises(ValueError, match="differs from calibration attestation"):
        _recreate(model, teacher_report=tampered)


@pytest.mark.parametrize(
    "mutation", ["class_counts", "observations", "row_digest", "source"])
def test_attestation_blocks_statistics_and_source_rewrites(
        monkeypatch, mutation: str) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    model = _model(
        _teacher_report(monkeypatch, spec), spec,
        sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")
    tampered = copy.deepcopy(model.teacher_report)
    if mutation == "class_counts":
        tampered["class_counts"]["ENTER_LONG"] -= 1
    elif mutation == "observations":
        tampered["observations"] += 1
    elif mutation == "row_digest":
        tampered["calibration_attestation"]["calibration_content"][
            "xor_sha256"] = "0" * 64
    else:
        tampered["calibration_attestation"]["source_contract"][
            "knowledge_cutoff"] = "2026-01-01T00:00:00+00:00"

    with pytest.raises(ValueError, match="attestation"):
        _recreate(model, teacher_report=tampered)


def test_legacy_teacher_artifact_remains_restorable_but_not_model_eligible(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    report = _teacher_report(monkeypatch, spec)
    legacy = copy.deepcopy(report)
    legacy.pop("_model_calibration_evidence")
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs=legacy["cost_inputs"],
        feature_window_contract_version=LEGACY_FEATURE_WINDOW_CONTRACT)

    restored = teacher.restore(legacy)

    assert restored == legacy
    assert teacher.prediction_identity()[-1] == legacy["model_fingerprint"]
    valid_model = _model(
        report, spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")
    legacy_split = model_split_manifest(
        calibration_sessions=valid_model.expected_calibration_sessions,
        contributing_calibration_sessions=legacy["session_ids"],
        evaluation_sessions=valid_model.expected_sessions,
        calibration_instruments=valid_model.expected_calibration_instruments,
        contributing_calibration_instruments=[],
        evaluation_instruments=valid_model.expected_instruments,
        spec=spec, rung=valid_model.evidence_scope)
    legacy_model = _recreate(
        valid_model, teacher_report=legacy,
        split_hash=runner.stable_fingerprint(legacy_split))
    legacy_result = legacy_model.finish()
    assert legacy_result["decision"] == "NO_EVIDENCE"
    assert "MODEL_TEACHER_ATTESTATION_MISSING" in legacy_result[
        "failed_criteria"]
    assert legacy_result["resource_preflight"]["failure_code"] == \
        "MODEL_TEACHER_ATTESTATION_MISSING"


def test_live_teacher_mapping_and_cached_identity_cannot_be_mutated(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    model = _model(
        _teacher_report(monkeypatch, spec), spec,
        sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")
    teacher = model.teacher
    assert teacher is not None
    with pytest.raises(TypeError):
        teacher.models["net_bps"] = teacher.models["net_bps"]
    with pytest.raises(AttributeError):
        teacher.models = {}

    # Reproduce the old cached-fingerprint exploit even through a deliberate
    # private/frozen-dataclass bypass.  The shared boundary must recompute the
    # live parameter seal instead of trusting ``_frozen_model_fingerprint``.
    object.__setattr__(
        teacher._models["net_bps"], "intercept",
        teacher._models["net_bps"].intercept + 100.0)
    with pytest.raises(ValueError, match="live parameters changed"):
        teacher.prediction_identity()
    with pytest.raises(ValueError, match="live parameters changed"):
        model.add_prepared(
            "A", [], audit_causality([], spec),
            predictions=[], targets=[], evaluation_session="2026-01-03",
            row_identity=_decision_index_fingerprint([]))


def test_bonferroni_tail_below_monte_carlo_resolution_fails_closed() -> None:
    interval = _selection_adjusted_interval(
        [1.0] * 60, declared_trials=251, n_boot=10_000)

    assert interval["status"] == "UNRESOLVED_FINITE_MONTE_CARLO_TAIL"
    assert interval["failure_code"] == BOOTSTRAP_RESOLUTION_FAILURE
    assert interval["active_gate"] is False
    assert interval["low_bps"] is None
    assert interval["high_bps"] is None
    assert interval["minimum_draws_required"] > interval["draws"]


def test_bonferroni_exact_tail_resolution_keeps_the_adverse_extreme(
        monkeypatch) -> None:
    def fixed_bootstrap_indices(
            observations: int, *, n_boot: int,
            restart_probability: float, seed: int):
        assert observations == 2
        assert n_boot == 40
        assert restart_probability == 0.25
        assert seed == 20260819
        yield [0, 0]
        for _ in range(39):
            yield [1, 1]

    monkeypatch.setattr(
        model_candidate_module, "stationary_bootstrap_indices",
        fixed_bootstrap_indices)

    interval = _selection_adjusted_interval(
        [-1.0, 1.0], declared_trials=1, n_boot=40)

    assert interval["status"] == "PASS"
    assert interval["active_gate"] is True
    assert interval["per_tail_probability"] == pytest.approx(1.0 / 40.0)
    assert interval["low_bps"] == -1.0
    assert interval["high_bps"] == 1.0


def test_full_model_cannot_nominate_with_unresolved_bonferroni_tail(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, spec)
    start = datetime(2026, 1, 3, 1, 0, tzinfo=timezone.utc)
    sessions = [(start + timedelta(days=index)).date().isoformat()
                for index in range(60)]
    model = _model(
        teacher, spec, sessions=sessions, instruments=["A", "B"],
        trials=251)
    for index, session in enumerate(sessions):
        at = start + timedelta(days=index)
        for instrument in ("A", "B"):
            model.add(instrument, [_sample(instrument, at, net=2.0)],
                      evaluation_session=session)

    report = model.finish()

    assert report["decision"] == "HOLD"
    assert BOOTSTRAP_RESOLUTION_FAILURE in report["failed_criteria"]
    assert report["summary"][
        "selection_adjusted_session_ci_low_bps"] is None


def test_quote_age_and_complete_sampling_manifest_change_candidate_identity(
        monkeypatch) -> None:
    teacher_spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0,
        max_quote_age_seconds=1.0, fee_bps_per_side=0.1)
    teacher = _teacher_report(monkeypatch, teacher_spec)
    first = _model(
        teacher, teacher_spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")
    second_spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0,
        max_quote_age_seconds=60.0, fee_bps_per_side=0.1)
    second = _model(
        teacher, second_spec, sessions=["2026-01-03"], instruments=["A"],
        scope="DISCOVERY_6")

    assert first.configuration_hash != second.configuration_hash
    assert first.model_candidate_id != second.model_candidate_id
    tampered_manifest = copy.deepcopy(first.sampling_execution_manifest)
    tampered_manifest["spec"]["max_quote_age_seconds"] = 60.0
    with pytest.raises(ValueError, match="configuration_hash does not seal"):
        _recreate(
            first, teacher_report=first.teacher_report,
            sampling_manifest=tampered_manifest,
            configuration_hash=first.configuration_hash)


def test_model_only_continuation_permanently_fences_ast_authority() -> None:
    ast = {
        "version": "ast-gate-v1", "primary_pass": False,
        "survivors": ["linked-ast"], "promotion_authority": False,
    }
    model = {"pass": True, "promotion_authority": False}
    combined = runner._combined_discovery_gate(ast, model)
    next_config, manifest = runner._next_rung_config(
        {
            "screening_population": [{
                "ast_fingerprint": "linked-ast",
            }],
            "successive_halving_eta": 3,
            "screening_trial_exposure": 1,
        },
        {"screening_population": [{
            "ast_fingerprint": "linked-ast", "pareto_rank": 1,
            "residual_qd": {"elite": True},
            "summary": {"mean_net_bps_per_opportunity": 5.0},
            "complexity_nodes": 1,
        }]},
        combined, candidate_budget=1)

    assert combined["allocation_paths"] == ["MODEL_CANDIDATE"]
    assert combined["ast_diagnostic_only"] is True
    assert combined["ast_forward_eligible"] is False
    assert next_config["screening_population"] == []
    assert next_config["model_only_continuation"] is True
    assert manifest["ast_primary_diagnostic_only"] is True
    assert manifest["ast_primary_retained_when_model_allocates"] is False

    attempted_rescue = runner._combined_discovery_gate(
        {"primary_pass": True, "survivors": ["PRIMARY"],
         "promotion_authority": False},
        {"pass": True, "promotion_authority": False},
        ast_forward_eligible=next_config["ast_forward_eligible"])
    assert attempted_rescue["primary_measured_pass"] is True
    assert attempted_rescue["primary_pass"] is False
    assert attempted_rescue["allocation_paths"] == ["MODEL_CANDIDATE"]
    assert attempted_rescue["promotion_authority"] is False
