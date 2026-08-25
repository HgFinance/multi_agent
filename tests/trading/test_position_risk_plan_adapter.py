from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[2]
    / "departments"
    / "02-trading"
    / "rules"
    / "position_risk_plan_adapter.py"
)
spec = importlib.util.spec_from_file_location("position_risk_plan_adapter", MODULE)
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


def _plan() -> dict:
    return {
        "schema_version": "risk.position-risk-plan.v1",
        "risk_plan_id": "1dc772a0-1775-4f3b-9434-a6d24897c349",
        "instrument_id": "75c24c42-eb12-469d-a494-01a2b936a348",
        "input_hash": "a" * 64,
        "execution_mode": "PAPER",
        "state": "ACTIVE",
        "action": "PROPOSE",
        "stop_price": "1500000",
        "take_profit_price": "1650000",
        "quantity_cap": "10",
        "current_quantity": "3",
        "expires_at": "2026-08-25T15:30:00+09:00",
    }


def test_active_plan_creates_two_valid_idempotent_candidates():
    first = adapter.build_paper_rule_bundle(_plan(), symbol="000660")
    second = adapter.build_paper_rule_bundle(_plan(), symbol="000660")
    assert first == second
    assert len(first.candidates) == 2
    assert {item.leg for item in first.candidates} == {"STOP", "TAKE_PROFIT"}
    assert first.candidates[0].candidate["action"]["sizing"]["value"] == "3"
    assert first.candidates[0].client_request_id != first.candidates[1].client_request_id


def test_plan_conversion_fails_closed_without_position_or_active_state():
    no_position = _plan()
    no_position["current_quantity"] = "0"
    with pytest.raises(ValueError, match="positive protected position"):
        adapter.build_paper_rule_bundle(no_position, symbol="000660")

    inactive = _plan()
    inactive["state"] = "VALIDATED"
    with pytest.raises(ValueError, match="ACTIVE"):
        adapter.build_paper_rule_bundle(inactive, symbol="000660")
