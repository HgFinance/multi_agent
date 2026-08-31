from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments/04-quant-backtest/api/quant_api.py"


def _load_app():
    spec = importlib.util.spec_from_file_location("quant_health_ready_test_app", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_shares_successful_db_probe(monkeypatch):
    app = _load_app()
    calls = []

    def query(_sql):
        calls.append(True)
        return [{"ok": 1}]

    app._QUANT_HEALTH_CACHE.clear()
    monkeypatch.setattr(app, "_query", query)
    monkeypatch.setenv("QUANT_HEALTH_CACHE_SECONDS", "5")

    assert app.health() == app.health()
    assert len(calls) == 1
