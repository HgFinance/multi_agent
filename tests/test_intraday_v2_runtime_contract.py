from __future__ import annotations

from datetime import datetime, timezone
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

import intraday_experiment_runner as runner
import intraday_supervised as supervised
from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    PRIMITIVE_WINDOWS_SECONDS,
    WINDOWED_FIELDS,
)
from intraday_candidate import (
    EXPLICIT_WINDOW_EVALUATOR_VERSION,
    evaluate_candidate,
)
from intraday_microstructure import (
    FeatureCubeSpec,
    IntradayLaneSpec,
    IntradaySampleBatch,
    MultiScaleFeatureCube,
    STRICT_TIMESTAMP_POLICY,
    _decision_index_fingerprint,
)


def _explicit_edge() -> dict:
    return {
        "research_lane": "INTRADAY_EVENT",
        "universe_key": "krx_all",
        "feature_window_contract_version":
            EXPLICIT_FEATURE_WINDOW_CONTRACT,
        "intraday_signal_expr": {
            "op": "field", "field": "realized_volatility_bps",
            "seconds": 2,
        },
        "semantic_plan": {
            "event": "VOLATILITY_BURST",
            "context": ["ALL"],
            "qualities": ["PERSISTENCE"],
            "direction": "FOLLOW",
            "output": "TAKER_NET_PNL",
            "execution": "TAKER",
            "horizon_seconds": 5,
        },
        "horizon_seconds": 5,
        "execution": "TAKER",
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
    }


def _empty_batch() -> IntradaySampleBatch:
    samples = ()
    cube = MultiScaleFeatureCube(
        spec=FeatureCubeSpec(),
        row_count=0,
        decision_index_fingerprint=_decision_index_fingerprint(samples),
        columns=tuple(
            (field, seconds, ())
            for field in sorted(WINDOWED_FIELDS)
            for seconds in PRIMITIVE_WINDOWS_SECONDS
        ),
    )
    return IntradaySampleBatch(samples, cube)


def _primary_row(config: dict) -> dict:
    return {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
    }


def test_feature_cube_spec_json_round_trip_is_exact_and_fail_closed() -> None:
    canonical = FeatureCubeSpec().as_dict()
    decoded = json.loads(json.dumps(canonical))

    assert FeatureCubeSpec.from_dict(decoded).as_dict() == canonical

    mutable_windows = list(PRIMITIVE_WINDOWS_SECONDS)
    mutable_fields = sorted(WINDOWED_FIELDS)
    direct = FeatureCubeSpec(
        windows_seconds=mutable_windows, windowed_fields=mutable_fields)
    mutable_windows[0] = 999
    mutable_fields.pop()
    assert isinstance(direct.windows_seconds, tuple)
    assert isinstance(direct.windowed_fields, tuple)
    assert direct == FeatureCubeSpec()

    with pytest.raises(ValueError, match="keys changed"):
        FeatureCubeSpec.from_dict({**decoded, "unregistered": True})
    drifted = {**decoded, "windows_seconds": [
        3, *decoded["windows_seconds"][1:]]}
    with pytest.raises(ValueError, match="windows must equal"):
        FeatureCubeSpec.from_dict(drifted)
    fractional = {**decoded, "windows_seconds": [
        2.0, *decoded["windows_seconds"][1:]]}
    with pytest.raises(ValueError, match="integer sequence"):
        FeatureCubeSpec.from_dict(fractional)


def test_explicit_runner_binds_v4_teacher_model_and_replay_to_frozen_cube(
        monkeypatch) -> None:
    edge = _explicit_edge()
    edge["feature_cube_spec"] = json.loads(json.dumps(
        FeatureCubeSpec().as_dict()))
    config, spec = runner.config_from_edge(edge)
    config["timestamp_policy"] = STRICT_TIMESTAMP_POLICY
    feature, _label, model = runner._candidate_specs(
        config, _primary_row(config))

    assert feature["feature_cube"] == config["feature_cube_spec"]
    assert feature["feature_cube_spec_hash"] == \
        supervised.explicit_feature_cube_spec_hash()
    assert feature["teacher_version"] == \
        supervised.EXPLICIT_WINDOW_TEACHER_VERSION
    assert feature["teacher_feature_spec_hash"] == \
        supervised.explicit_feature_spec_hash()
    assert feature["teacher_features"] == list(
        supervised.explicit_feature_names())
    assert model["teacher_version"] == \
        supervised.EXPLICIT_WINDOW_TEACHER_VERSION
    assert model["feature_cube_spec_hash"] == \
        supervised.explicit_feature_cube_spec_hash()
    assert runner._qa_current_runtime_versions(config)["teacher_version"] == \
        supervised.EXPLICIT_WINDOW_TEACHER_VERSION

    captured = {}
    sentinel = object()

    def fake_build(quotes, trades, lane_spec, **kwargs):
        captured.update(kwargs)
        assert lane_spec is spec
        return sentinel

    monkeypatch.setattr(runner, "build_sample_batch", fake_build)
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert runner._build_runtime_samples(
        config, [], [], spec, start=start, end=start) is sentinel
    assert captured["cube_spec"].as_dict() == config["feature_cube_spec"]


def test_explicit_runner_rejects_frozen_cube_drift_before_replay() -> None:
    edge = _explicit_edge()
    drifted = json.loads(json.dumps(FeatureCubeSpec().as_dict()))
    drifted["windowed_fields"] = drifted["windowed_fields"][:-1]
    edge["feature_cube_spec"] = drifted

    with pytest.raises(ValueError, match="windowed fields"):
        runner.config_from_edge(edge)

    config, _ = runner.config_from_edge(_explicit_edge())
    config["feature_cube_spec"] = {
        **config["feature_cube_spec"], "boundary": "[decision-W,decision)"}
    with pytest.raises(ValueError, match="frozen feature_cube_spec"):
        runner._teacher_runtime_identity(config)

    with pytest.raises(ValueError, match="unsupported feature-window"):
        runner._qa_current_runtime_versions({
            "feature_window_contract_version": "future-contract-v99"})
    with pytest.raises(ValueError, match="unsupported feature-window"):
        runner._qa_current_runtime_versions({
            "feature_window_contract_version": ""})

    legacy_edge = {
        **_explicit_edge(),
        "feature_window_contract_version": None,
        "intraday_signal_expr": {
            "op": "field", "field": "realized_volatility_bps"},
        "feature_cube_spec": {},
    }
    with pytest.raises(ValueError, match="requires the explicit-window"):
        runner.config_from_edge(legacy_edge)


def test_public_evaluator_accepts_explicit_batch_and_separate_cube() -> None:
    batch = _empty_batch()
    spec = IntradayLaneSpec(horizons_seconds=(5,))
    kwargs = {
        "expr": {"op": "field", "field": "realized_volatility_bps",
                 "seconds": 2},
        "spec": spec,
        "horizon_seconds": 5,
        "execution": "TAKER",
        "feature_window_contract_version":
            EXPLICIT_FEATURE_WINDOW_CONTRACT,
    }

    direct = evaluate_candidate({"005930": batch}, **kwargs)
    separate = evaluate_candidate(
        {"005930": list(batch)},
        feature_cubes_by_instrument={"005930": batch.feature_cube},
        **kwargs,
    )

    assert direct["evaluator_version"] == EXPLICIT_WINDOW_EVALUATOR_VERSION
    assert direct["feature_window_contract_version"] == \
        EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert separate == direct

    with pytest.raises(ValueError, match="explicit-window contract"):
        evaluate_candidate(
            {"005930": list(batch)},
            expr={"op": "field", "field": "realized_volatility_bps"},
            spec=spec, horizon_seconds=5, execution="TAKER",
            feature_cubes_by_instrument={"005930": batch.feature_cube},
        )
