from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_supervised as supervised
from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    LEGACY_FEATURE_WINDOW_CONTRACT,
    PRIMITIVE_WINDOWS_SECONDS,
    WINDOWED_FIELDS,
)
from intraday_candidate import CandidateAccumulator, CandidatePopulationAccumulator
from intraday_microstructure import (
    FeatureCubeSpec,
    HorizonLabel,
    IntradayLaneSpec,
    IntradaySample,
    IntradaySampleBatch,
    MultiScaleFeatureCube,
    _decision_index_fingerprint,
)


def _sample(instrument: str, at: datetime, *, signal: float,
            net: float) -> IntradaySample:
    label = HorizonLabel(
        horizon_seconds=5,
        exit_time=at + timedelta(seconds=5),
        future_mid=100.0 + signal / 100.0,
        long_mid_markout_bps=signal,
        short_mid_markout_bps=-signal,
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
        spread_bps=1.0,
        queue_imbalance_l1=signal / 10.0,
        queue_imbalance_l10=signal / 20.0,
        microprice_offset_bps=signal,
        trade_flow_imbalance=signal / 5.0,
        quote_event_ofi=signal * 10.0,
        normalized_quote_ofi=signal / 7.0,
        bid_depth_l1=100.0,
        ask_depth_l1=90.0,
        book_depth_l1=190.0,
        book_depth_l10=1_900.0,
        trade_count=2,
        quote_count=3,
        trade_intensity=1.0,
        realized_volatility_bps=2.0,
        entry_bid_depth_l1=100.0,
        entry_ask_depth_l1=90.0,
        entry_bid=99.99,
        entry_ask=100.01,
        entry_mid=100.0,
        labels=(label,),
        multi_level_quote_ofi_l10=signal * 20.0,
        normalized_multi_level_quote_ofi_l10=signal / 9.0,
        depth_imbalance_slope=signal / 30.0,
        quote_ofi_depth_divergence=signal / 11.0,
        quote_event_transition_count=4,
        normalized_quote_ofi_per_event=signal / 13.0,
        signed_trade_volume=signal * 30.0,
        trade_volume=abs(signal) * 40.0 + 1.0,
        trade_side_known_ratio=0.75,
        quote_ofi_per_trade_volume=signal / 17.0,
    )


def _batch(instrument: str, times: list[datetime], *, offset: float = 0.0,
           mutate: tuple[str, int, int, float | None] | None = None
           ) -> IntradaySampleBatch:
    samples = tuple(
        _sample(instrument, at, signal=float(index + 1) + offset,
                net=float(index) - 0.25 + offset / 10.0)
        for index, at in enumerate(times)
    )
    columns = []
    for field_index, field in enumerate(sorted(WINDOWED_FIELDS)):
        for seconds in PRIMITIVE_WINDOWS_SECONDS:
            values = [
                (field_index + 1) * 0.01 + seconds * 0.001 +
                row_index * 0.1 + offset
                for row_index in range(len(samples))
            ]
            if mutate is not None:
                target_field, target_seconds, row_index, value = mutate
                if field == target_field and seconds == target_seconds:
                    values[row_index] = value
            columns.append((field, seconds, tuple(values)))
    cube = MultiScaleFeatureCube(
        spec=FeatureCubeSpec(),
        row_count=len(samples),
        decision_index_fingerprint=_decision_index_fingerprint(samples),
        columns=tuple(columns),
    )
    return IntradaySampleBatch(samples, cube)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def test_legacy_teacher_v3_report_remains_byte_compatible(monkeypatch) -> None:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        feature_window_contract_version=LEGACY_FEATURE_WINDOW_CONTRACT)
    for instrument, day in (("A", 0), ("B", 1)):
        rows = [
            _sample(instrument, base + timedelta(days=day, seconds=index * 5),
                    signal=float(index + 1), net=float(index) - 0.25)
            for index in range(3)
        ]
        teacher.calibrate(instrument, rows)

    report = teacher.freeze()

    assert supervised.TEACHER_VERSION == "krx-cost-aware-linear-teacher-v3"
    assert supervised.feature_spec_hash() == \
        "dda50925a11a0d8429ab0c3428cabc2d39a5ece2ea8f329eee98e0c40f2f976e"
    assert report["version"] == supervised.TEACHER_VERSION
    assert "feature_window_contract_version" not in report
    # Historical v3 used platform libm plus pure-Python Gaussian elimination.
    # Its Windows and Linux artifacts therefore differ by a few floating-point
    # ulps even though the legacy algorithm is unchanged.  Freeze both existing
    # platform-native byte identities; any third identity still requires a new
    # model contract instead of silently invalidating persisted controls.
    assert hashlib.sha256(_canonical(report).encode()).hexdigest() in {
        "72c03d7974e9e7e4577d4190e3a6f55d5db1a04272eb8ffb5dbd43537eadf311",
        "533b10a35bb0178dc5bc57da16eb769f5c9f6310ea50ac2bc13a44462eb37226",
    }


def test_explicit_vector_uses_every_coordinate_and_missing_indicator() -> None:
    at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    baseline = _batch("A", [at])
    changed = _batch(
        "A", [at], mutate=("trade_flow_imbalance", 300, 0, 987.0))
    missing = _batch(
        "A", [at], mutate=("trade_flow_imbalance", 300, 0, None))

    first = supervised.explicit_feature_vector(
        baseline[0], baseline.feature_cube, 0)
    second = supervised.explicit_feature_vector(
        changed[0], changed.feature_cube, 0)
    absent = supervised.explicit_feature_vector(
        missing[0], missing.feature_cube, 0)
    names = supervised.explicit_feature_names()
    coordinate = names.index("window:trade_flow_imbalance@300s")
    missing_coordinate = names.index(
        "window:trade_flow_imbalance@300s__missing")

    assert len(first) == len(names)
    assert len(names) == 2 * (
        len(supervised.STATE_FIELDS) +
        len(WINDOWED_FIELDS) * len(PRIMITIVE_WINDOWS_SECONDS))
    assert [index for index, pair in enumerate(zip(first, second))
            if pair[0] != pair[1]] == [coordinate]
    assert absent[coordinate] == 0.0
    assert absent[missing_coordinate] == 1.0
    assert first[names.index("window:trade_flow_imbalance@2s")] != \
        first[names.index("window:trade_flow_imbalance@5s")]


def test_explicit_teacher_fits_predicts_and_never_refits_oos(monkeypatch) -> None:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    training = [
        _batch("A", [base + timedelta(seconds=index * 5)
                     for index in range(3)]),
        _batch("B", [base + timedelta(days=1, seconds=index * 5)
                     for index in range(3)], offset=0.5),
    ]
    missing_cube_teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        feature_window_contract_version=EXPLICIT_FEATURE_WINDOW_CONTRACT)
    with pytest.raises(ValueError, match="requires a feature cube"):
        missing_cube_teacher.calibrate("A", list(training[0]))
    teacher = supervised.CostAwareTeacher(
        horizon_seconds=5, execution="TAKER",
        feature_window_contract_version=EXPLICIT_FEATURE_WINDOW_CONTRACT)
    training_identity = teacher.training_contract_key()
    assert EXPLICIT_FEATURE_WINDOW_CONTRACT in training_identity
    assert supervised.explicit_feature_cube_spec_hash() in training_identity
    assert supervised.explicit_feature_spec_hash() in training_identity
    for batch in training:
        teacher.calibrate(
            batch[0].instrument_id, batch, feature_cube=batch.feature_cube)
    frozen = teacher.freeze()

    assert frozen["status"] == "PASS"
    assert frozen["version"] == supervised.EXPLICIT_WINDOW_TEACHER_VERSION
    assert frozen["feature_window_contract_version"] == \
        EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert frozen["feature_cube_spec_hash"] == \
        supervised.explicit_feature_cube_spec_hash()
    assert len(frozen["features"]) == 244

    evaluation = _batch(
        "A", [base + timedelta(days=2, seconds=index * 5)
              for index in range(2)], offset=100.0)
    predictions = teacher.predict(
        evaluation, feature_cube=evaluation.feature_cube)
    assert len(predictions) == len(evaluation)
    assert all(row is not None for row in predictions)
    assert _canonical(teacher.report()) == _canonical(frozen)

    with pytest.raises(ValueError, match="requires a feature cube"):
        teacher.predict(list(evaluation))
    wrong_index = _batch(
        "B", [base + timedelta(days=2, seconds=index * 5)
              for index in range(2)])
    with pytest.raises(ValueError, match="decision index is misaligned"):
        teacher.predict(list(evaluation), feature_cube=wrong_index.feature_cube)


def test_explicit_population_sharing_preserves_context_alignment(monkeypatch) -> None:
    monkeypatch.setattr(supervised, "MIN_OBSERVATIONS", 4)
    monkeypatch.setattr(supervised, "MIN_INSTRUMENTS", 2)
    spec = IntradayLaneSpec(
        horizons_seconds=(5,), order_latency_ms=0, fee_bps_per_side=0.1)
    criteria = {
        "min_sessions": 1,
        "min_instruments": 1,
        "min_opportunities": 1,
        "min_deflated_sharpe": -100.0,
        "min_positive_session_ratio": 0.0,
    }

    def candidates() -> dict[str, CandidateAccumulator]:
        return {
            "OPEN": CandidateAccumulator(
                expr={"op": "field", "field": "trade_flow_imbalance",
                      "seconds": 2},
                spec=spec, horizon_seconds=5, execution="TAKER",
                feature_window_contract_version=
                EXPLICIT_FEATURE_WINDOW_CONTRACT,
                semantic_plan={"context": ("OPEN",)}, criteria=criteria),
            "CLOSE": CandidateAccumulator(
                expr={"op": "field", "field": "trade_flow_imbalance",
                      "seconds": 300},
                spec=spec, horizon_seconds=5, execution="TAKER",
                feature_window_contract_version=
                EXPLICIT_FEATURE_WINDOW_CONTRACT,
                semantic_plan={"context": ("CLOSE",)}, criteria=criteria),
        }

    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    calibration = [
        _batch("A", [base + timedelta(seconds=index * 5)
                     for index in range(3)]),
        _batch("B", [base + timedelta(days=1, seconds=index * 5)
                     for index in range(3)], offset=0.5),
    ]
    # 00:05 UTC is KRX OPEN and 06:00 UTC is KRX CLOSE.  The cube remains
    # aligned to both rows while each candidate selects a different subset.
    evaluation = _batch(
        "A", [base + timedelta(days=2, minutes=5),
              base + timedelta(days=2, hours=6)], offset=1.5)

    expected = {}
    for key, candidate in candidates().items():
        for batch in calibration:
            candidate.calibrate(
                batch[0].instrument_id, batch)
        candidate.freeze_calibration()
        candidate.add("A", evaluation)
        expected[key] = candidate.finish()

    population = CandidatePopulationAccumulator(candidates())
    for batch in calibration:
        population.calibrate(batch[0].instrument_id, batch)
    frozen = population.freeze_calibration()
    population.add("A", evaluation)
    actual = population.finish()

    assert frozen["OPEN"]["supervised_control"]["model_fingerprint"] == \
        frozen["CLOSE"]["supervised_control"]["model_fingerprint"]
    assert _canonical(actual) == _canonical(expected)
    assert all(report["supervised_control"]["promotion_authority"] is False
               for report in actual.values())
