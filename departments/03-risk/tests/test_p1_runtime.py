from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p1.instrument_repository import (
    InstrumentMappingRepositoryError,
    PostgresInstrumentMappingRepository,
)
from p1.runtime import ExternalRiskRuntimeConfig, RiskExternalRuntimeError


def test_runtime_config_requires_explicit_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISK_FUND_ID", str(uuid4()))
    monkeypatch.delenv("RISK_INSTRUMENT_MAPPINGS_JSON", raising=False)

    with pytest.raises(RiskExternalRuntimeError, match="MAPPINGS"):
        ExternalRiskRuntimeConfig.from_env()


def test_runtime_config_parses_non_secret_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISK_FUND_ID", str(uuid4()))
    monkeypatch.setenv(
        "RISK_INSTRUMENT_MAPPINGS_JSON",
        json.dumps([{"broker_symbol": "AAPL", "instrument_id": str(uuid4())}]),
    )
    monkeypatch.setenv("RISK_STRESS_SCENARIOS_JSON", '{"shock": {"AAPL": -0.2}}')

    config = ExternalRiskRuntimeConfig.from_env(as_of=datetime.now(timezone.utc))

    assert config.mappings[0].broker_symbol == "AAPL"
    assert config.stress_scenarios["shock"]["AAPL"] == -0.2


class _Cursor:
    def __init__(self) -> None:
        self.query = ""
        self.params = None

    def execute(self, query: str, params: object) -> None:
        self.query = query
        self.params = params

    def fetchall(self) -> list[tuple[str, object, str]]:
        return [("AAPL", uuid4(), "EQUITY")]

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def test_instrument_repository_requires_complete_canonical_mapping() -> None:
    repository = PostgresInstrumentMappingRepository(_Connection())

    with pytest.raises(InstrumentMappingRepositoryError, match="MSFT"):
        repository.resolve(["AAPL", "MSFT"])
