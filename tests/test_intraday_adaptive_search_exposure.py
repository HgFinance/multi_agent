from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_experiment_runner as runner  # noqa: E402
from intraday_microstructure import (COMPLETED_SECOND_POLICY,  # noqa: E402
                                      EXTERNAL_EVENT_SOURCE)


def _config_and_spec():
    plan = {
        "event": "ORDER_FLOW",
        "context": ["TIGHT_SPREAD"],
        "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"],
        "direction": "FOLLOW",
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": 5,
    }
    flow = {"op": "sub", "args": [
        {"op": "field", "field": "trade_flow_imbalance"},
        {"op": "rolling_mean", "seconds": 30,
         "arg": {"op": "field", "field": "trade_flow_imbalance"}},
    ]}
    expression = {"op": "where", "condition": {"op": "lt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"},
    ]}, "then": {"op": "mul", "args": [
        flow, {"op": "field", "field": "realized_volatility_bps"},
    ]}, "else": {"const": 0, "unit": "BPS"}}
    config, spec = runner.config_from_edge({
        "research_lane": "INTRADAY_EVENT",
        "universe_key": "krx_all",
        "intraday_signal_expr": expression,
        "semantic_plan": plan,
        "horizon_seconds": 5,
        "execution": "TAKER",
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        "evaluation_days": 60,
        "data_source": EXTERNAL_EVENT_SOURCE,
        "resolved_data_contract": runner._expected_resolved_data_contract(
            EXTERNAL_EVENT_SOURCE),
    })
    config["timestamp_policy"] = COMPLETED_SECOND_POLICY
    return config, spec


def _fixture(*, ephemeral: str = "a",
             rung_name: str = runner.DISCOVERY_6) -> dict:
    config, spec = _config_and_spec()
    session_count = 6 if rung_name == runner.DISCOVERY_6 else 20
    sessions = [date(2026, 5, 4) + timedelta(days=index)
                for index in range(session_count)]
    session_names = [value.isoformat() for value in sessions]
    keys = ["005930", "000660", "035420"]
    reference_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]
    dataset_id = "10000000-0000-0000-0000-000000000001"
    session_fp = runner.stable_fingerprint(session_names)
    instrument_fp = runner.stable_fingerprint(sorted(reference_ids))
    rung = SimpleNamespace(
        rung=rung_name,
        dataset_id=dataset_id,
        planned_session_dates=tuple(sessions),
        session_set_fingerprint=session_fp,
        planned_instrument_ids=tuple(sorted(reference_ids)),
        planned_instrument_count=len(keys),
        instrument_set_fingerprint=instrument_fp,
        experiment_rung_id=ephemeral * 36,
        rung_plan_fingerprint=ephemeral * 64,
        candidate=SimpleNamespace(
            candidate_lineage_id=ephemeral * 36,
            root_lineage_id=ephemeral * 36),
    )
    panel = {
        "version": "krx-profiled-discovery-panel-v2",
        "mode": "NESTED_INFORMATION_AND_IDENTITY_HASH_GUARD",
        "information_rich": [keys[0]],
        "representative_guard": [keys[1]],
        "promotion_authority": False,
    }
    schedule = {
        "rung": rung,
        "keys": keys,
        "evaluation_keys": keys[:2],
        "panel": panel,
    }
    exposure = {
        "rung": rung_name,
        "dataset_id": dataset_id,
        "dataset_cutoff": "2026-05-30T06:31:00+00:00",
        "experiment_rung_id": ephemeral * 36,
        "candidate_lineage_id": ephemeral * 36,
        "root_lineage_id": ephemeral * 36,
        "rung_plan_fingerprint": ephemeral * 64,
        "session_count": len(sessions),
        "instrument_count": len(keys),
        "session_set_fingerprint": session_fp,
        "instrument_ids_fingerprint": instrument_fp,
        "sessions": [{
            "session": session,
            "access_fingerprint": ephemeral * 64,
            "evidence_fingerprint": ephemeral * 64,
            "session_content_fingerprint": f"{index + 1:064x}",
            "quote_rows": 1_000 + index,
            "trade_rows": 500 + index,
            "source_watermark": {
                "event_source": EXTERNAL_EVENT_SOURCE,
                "daily_manifest_rows": len(keys),
                "input_watermark": f"2026-05-{index + 4:02d}T06:30:00+00:00",
            },
        } for index, session in enumerate(session_names)],
    }
    screen = {
        "rung": rung_name,
        "evaluation_status": "EVALUATED",
        "sessions": session_names,
        "evaluated_sessions": session_names,
        "session_count": len(sessions),
        "evaluated_session_count": len(sessions),
        "instrument_count": 2,
        "instrument_fingerprint": hashlib.sha256(
            "|".join(keys[:2]).encode()).hexdigest(),
        "panel_manifest": panel,
    }
    selected = {
        "instruments": keys,
        "reference_instrument_ids": reference_ids,
        "reference_identity_fingerprint": "9" * 64,
    }
    cutoff = "2026-05-30T06:31:00+00:00"
    source_lineage = [{
        "source": "external.microstructure",
        "rows": 9_000,
        "max_source_watermark": cutoff,
        "content_fingerprint": "8" * 64,
    }]
    return {
        "config": config,
        "spec": spec,
        "selected": selected,
        "schedule_row": schedule,
        "exposure_manifest": exposure,
        "screen": screen,
        "dataset_id": dataset_id,
        "dataset_cutoff": cutoff,
        "event_source": EXTERNAL_EVENT_SOURCE,
        "source_lineage": source_lineage,
    }


def test_search_exposure_is_exact_and_stable_across_ledger_ids() -> None:
    first = runner._adaptive_rung_search_exposure(**_fixture(ephemeral="a"))
    retry = runner._adaptive_rung_search_exposure(**_fixture(ephemeral="b"))

    assert first == retry
    assert len(first["search_exposure_fingerprint"]) == 64
    canonical = {key: value for key, value in first.items()
                 if key != "search_exposure_fingerprint"}
    assert runner.stable_fingerprint(canonical) == \
        first["search_exposure_fingerprint"]
    assert first["version"] == runner.ADAPTIVE_SEARCH_EXPOSURE_VERSION
    assert first["evaluation"]["panel_replay_keys"] == ["005930", "000660"]
    assert first["evaluation"]["evaluated_session_count"] == 6
    assert first["content_evidence"][
        "panel_only_content_fingerprints_available"] is False
    assert first["content_evidence"][
        "conservative_full_universe_content_limitation"]
    assert len(first["content_evidence"]["per_session"]) == 6
    candidate = first["evaluator_contract"]["candidate_contracts"][0]
    assert candidate["clock_domains"]
    assert candidate["horizon_seconds"] == 5
    assert candidate["execution"] == "TAKER"
    assert first["promotion_authority"] is False


@pytest.mark.parametrize("mutation", [
    "session_count", "panel_fingerprint", "content_watermark",
    "universe_fingerprint",
])
def test_search_exposure_rejects_incomplete_or_mismatched_identity(
        mutation: str) -> None:
    values = _fixture()
    if mutation == "session_count":
        values["screen"]["evaluated_session_count"] = 5
    elif mutation == "panel_fingerprint":
        values["screen"]["instrument_fingerprint"] = "0" * 64
    elif mutation == "content_watermark":
        values["exposure_manifest"]["sessions"][0].pop("source_watermark")
    else:
        values["exposure_manifest"]["instrument_ids_fingerprint"] = "0" * 64

    with pytest.raises(RuntimeError):
        runner._adaptive_rung_search_exposure(**values)


def test_completed_rung_persists_one_aware_time_and_exposure_link() -> None:
    values = _fixture()
    completed = datetime(2026, 8, 18, 3, 4, 5, tzinfo=timezone.utc)
    report = {
        "summary": {},
        "screening_population": [],
        "multiple_testing": {"version": "test"},
    }
    row = runner._completed_adaptive_rung_evidence(
        report, completed_at=completed, **values)

    assert row["completed_at"] == completed.isoformat()
    assert row["candidate_evidence"][0]["observed_at"] == \
        row["completed_at"]
    assert row["candidate_evidence"][0][
        "search_exposure_fingerprint"] == row[
            "search_exposure_fingerprint"]
    assert row["candidate_evidence"][0]["evidence_scope"] == "F1"
    assert row["candidate_evidence"][0]["measurement_scope"] == \
        "ADAPTIVE_RUNG_MEASURED"
    assert row["promotion_authority"] is False

    with pytest.raises(RuntimeError, match="timezone aware"):
        runner._completed_adaptive_rung_evidence(
            report, completed_at=datetime(2026, 8, 18, 3, 4, 5), **values)


def test_validation_rung_is_linked_as_f2_without_promotion_authority() -> None:
    values = _fixture(rung_name=runner.VALIDATION_20)
    report = {
        "summary": {},
        "screening_population": [],
        "multiple_testing": {"version": "test"},
    }

    row = runner._completed_adaptive_rung_evidence(
        report, completed_at="2026-08-18T03:04:05+00:00", **values)

    assert row["rung"] == runner.VALIDATION_20
    assert row["search_exposure"]["evaluation"][
        "evaluated_session_count"] == 20
    assert row["candidate_evidence"][0]["evidence_scope"] == "F2"
    assert row["candidate_evidence"][0]["promotion_authority"] is False


def test_skipped_cost_rung_records_zero_evaluated_sessions_without_inference() -> None:
    values = _fixture()
    values["screen"]["evaluation_status"] = "SKIPPED_COST_INFEASIBLE"
    values["screen"]["evaluated_sessions"] = []
    values["screen"]["evaluated_session_count"] = 0

    exposure = runner._adaptive_rung_search_exposure(**values)

    assert exposure["evaluation"]["planned_session_count"] == 6
    assert exposure["evaluation"]["evaluated_session_count"] == 0
    assert exposure["evaluation"]["measurement_scope"] == \
        "CALIBRATION_ONLY_RESOURCE_STOP"
    assert exposure["promotion_authority"] is False
