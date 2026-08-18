from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import intraday_supervised as supervised_module
from intraday_microstructure import HorizonLabel, IntradaySample
from intraday_supervised import (CostAwareTeacher, direction_class,
                                 executable_target, feature_names,
                                 feature_spec_hash, feature_vector)


UTC = timezone.utc


def _sample(instrument: str, at: datetime, signal: float, *,
            passive_fill: bool = True) -> IntradaySample:
    markout = 4.0 * signal
    taker_net = markout - 2.0
    passive_net = markout - 0.5 if passive_fill else None
    label = HorizonLabel(
        horizon_seconds=5,
        exit_time=at + timedelta(seconds=5),
        future_mid=100.0 * (1.0 + markout / 10_000.0),
        long_mid_markout_bps=markout,
        short_mid_markout_bps=-markout,
        long_taker_net_bps=taker_net,
        short_taker_net_bps=-taker_net,
        long_passive_filled=passive_fill,
        short_passive_filled=False,
        long_passive_fill_time=(at + timedelta(seconds=1)
                                if passive_fill else None),
        short_passive_fill_time=None,
        long_passive_net_bps=passive_net,
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
        queue_imbalance_l10=signal * 0.8,
        microprice_offset_bps=signal,
        trade_flow_imbalance=signal,
        quote_event_ofi=100.0 * signal,
        normalized_quote_ofi=signal,
        bid_depth_l1=100.0,
        ask_depth_l1=100.0,
        book_depth_l1=200.0,
        book_depth_l10=2_000.0,
        trade_count=10,
        quote_count=20,
        trade_intensity=2.0,
        realized_volatility_bps=1.0,
        entry_bid_depth_l1=100.0,
        entry_ask_depth_l1=100.0,
        entry_bid=99.99,
        entry_ask=100.01,
        entry_mid=100.0,
        labels=(label,),
    )


def test_teacher_fits_only_calibration_and_freezes_cost_aware_labels() -> None:
    teacher = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs={"fee_bps_per_side": 1.0,
                     "maker_fee_bps_per_side": 0.0})
    start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    first = [_sample("A", start + timedelta(seconds=5 * index),
                     -1.0 + 2.0 * index / 599.0)
             for index in range(600)]
    second_start = start + timedelta(days=1)
    second = [_sample("B", second_start + timedelta(seconds=5 * index),
                      -1.0 + 2.0 * index / 599.0)
              for index in range(600)]
    teacher.calibrate("A", first)
    teacher.calibrate("B", second)
    report = teacher.freeze()

    assert report["status"] == "PASS"
    assert report["observations"] == 1_200
    assert report["sessions"] == 2
    assert report["oos_fit_forbidden"] is True
    assert len(feature_spec_hash()) == 64
    assert report["cost_inputs"]["fee_bps_per_side"] == 1.0
    assert len(report["model_fingerprint"]) == 64
    assert len(report["calibration_fingerprints"]["sessions"]) == 64
    assert len(report["calibration_fingerprints"]["instruments"]) == 64
    assert set(report["model_parameters"]) == {
        "markout_bps", "net_bps", "positive_net"}
    for model in report["model_parameters"].values():
        assert len(model["coefficients"]) == len(feature_names())
        assert len(model["means"]) == len(feature_names())
        assert len(model["scales"]) == len(feature_names())
    assert teacher.report()["model_fingerprint"] == report["model_fingerprint"]

    low, high = teacher.predict([
        _sample("C", start + timedelta(days=2), -0.8),
        _sample("C", start + timedelta(days=2, seconds=5), 0.8),
    ])
    assert low is not None and high is not None
    assert high["expected_net_bps"] > low["expected_net_bps"]
    assert 0.0 <= low["positive_net_probability"] <= 1.0
    assert 0.0 <= high["positive_net_probability"] <= 1.0

    # Freeze is immutable: evaluation cannot be smuggled back into calibration.
    try:
        teacher.calibrate("C", [_sample("C", start + timedelta(days=2), 1.0)])
    except ValueError as exc:
        assert "already frozen" in str(exc)
    else:
        raise AssertionError("teacher accepted OOS rows after freeze")


def test_teacher_restores_exact_frozen_artifact_and_rejects_tampering() -> None:
    costs = {"fee_bps_per_side": 1.0, "maker_fee_bps_per_side": 0.0}
    source = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    start = datetime(2026, 5, 18, tzinfo=UTC)
    for instrument, day in (("A", 0), ("B", 1)):
        source.calibrate(instrument, [
            _sample(instrument, start + timedelta(days=day, seconds=5 * index),
                    -1.0 + 2.0 * index / 599.0)
            for index in range(600)
        ])
    artifact = source.freeze()
    probe = [_sample("C", start + timedelta(days=2), 0.7)]

    restored = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    restored_report = restored.restore(artifact)
    assert restored_report["model_fingerprint"] == artifact["model_fingerprint"]
    assert restored.predict(probe) == source.predict(probe)
    try:
        restored.calibrate("C", probe)
    except ValueError as exc:
        assert "already frozen" in str(exc)
    else:
        raise AssertionError("restored teacher accepted forward rows as calibration")

    tampered = {**artifact, "model_parameters": {
        key: dict(value) for key, value in artifact["model_parameters"].items()
    }}
    tampered["model_parameters"]["net_bps"]["intercept"] += 1.0
    fresh = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    try:
        fresh.restore(tampered)
    except ValueError as exc:
        assert "fingerprint" in str(exc)
    else:
        raise AssertionError("teacher admitted tampered frozen parameters")


def test_teacher_sharing_identity_separates_contracts_and_frozen_models(
        monkeypatch) -> None:
    monkeypatch.setattr(supervised_module, "MIN_OBSERVATIONS", 4)
    costs = {"fee_bps_per_side": 1.0, "maker_fee_bps_per_side": 0.0}
    left = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    same = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    other_horizon = CostAwareTeacher(
        horizon_seconds=30, execution="TAKER", cost_inputs=costs)
    other_cost = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        cost_inputs={**costs, "fee_bps_per_side": 2.0})

    assert left.is_fresh() is True
    assert left.training_contract_key() == same.training_contract_key()
    assert left.training_contract_key() != other_horizon.training_contract_key()
    assert left.training_contract_key() != other_cost.training_contract_key()

    start = datetime(2026, 5, 18, tzinfo=UTC)
    right = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    for teacher, signals in (
            (left, (-1.0, -0.5, 0.5, 1.0)),
            (right, (-0.9, -0.1, 0.4, 0.7))):
        teacher.calibrate("A", [
            _sample("A", start, signals[0]),
            _sample("A", start + timedelta(seconds=5), signals[1]),
        ])
        teacher.calibrate("B", [
            _sample("B", start + timedelta(seconds=10), signals[2]),
            _sample("B", start + timedelta(seconds=15), signals[3]),
        ])
        teacher.freeze()

    assert left.training_contract_key() == right.training_contract_key()
    assert left.prediction_identity() != right.prediction_identity()
    restored = CostAwareTeacher(
        horizon_seconds=5, execution="TAKER", cost_inputs=costs)
    restored.restore(left.report())
    assert restored.prediction_identity() == left.prediction_identity()


def test_label_distinguishes_direction_from_executable_profit_and_nonfill() -> None:
    at = datetime(2026, 5, 18, tzinfo=UTC)
    costly_up = _sample("A", at, 0.25)
    assert direction_class(
        costly_up, horizon_seconds=5, execution="TAKER") == \
        "UP_BUT_COSTLY_ABSTAIN"

    nonfill = _sample("A", at, 1.0, passive_fill=False)
    markout, net, positive = executable_target(
        nonfill, horizon_seconds=5, execution="PASSIVE_FIFO_LOWER_BOUND")
    assert markout > 0.0
    assert net == 0.0
    assert positive == 0.0


def test_teacher_feature_vector_uses_new_microstructure_fields_and_missing_flags(
        ) -> None:
    base = _sample("A", datetime(2026, 5, 18, tzinfo=UTC), 0.2)
    rich = replace(
        base,
        multi_level_quote_ofi_l10=12.5,
        normalized_multi_level_quote_ofi_l10=0.25,
        depth_imbalance_slope=-0.5,
        quote_ofi_depth_divergence=0.75,
        quote_event_transition_count=9,
        normalized_quote_ofi_per_event=0.33,
        signed_trade_volume=-100.0,
        trade_volume=250.0,
        trade_side_known_ratio=0.8,
        quote_ofi_per_trade_volume=0.05,
    )
    names = feature_names()
    vector = feature_vector(rich)
    width = len(names) // 2
    for name in (
            "multi_level_quote_ofi_l10",
            "normalized_multi_level_quote_ofi_l10",
            "depth_imbalance_slope",
            "quote_ofi_depth_divergence",
            "quote_event_transition_count",
            "normalized_quote_ofi_per_event",
            "signed_trade_volume",
            "trade_volume",
            "trade_side_known_ratio",
            "quote_ofi_per_trade_volume"):
        index = names.index(name)
        assert vector[index] != 0.0
        assert vector[width + index] == 0.0

    missing = replace(rich, normalized_multi_level_quote_ofi_l10=None)
    missing_vector = feature_vector(missing)
    index = names.index("normalized_multi_level_quote_ofi_l10")
    assert missing_vector[index] == 0.0
    assert missing_vector[width + index] == 1.0
