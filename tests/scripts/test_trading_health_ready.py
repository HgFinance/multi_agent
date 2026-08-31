from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments" / "02-trading" / "api" / "app.py"


def _load_app():
    os.environ.setdefault("PAPER_DB", "false")
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("TRADING_LEGACY_OFFLINE_MODE", "fixture")
    if str(API_PATH.parent) not in sys.path:
        sys.path.insert(0, str(API_PATH.parent))
    spec = importlib.util.spec_from_file_location("trading_health_ready_test_app", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_ready_uses_a_single_flight_readiness_cache(monkeypatch):
    app = _load_app()
    calls = []

    class Store:
        def readiness_counts(self):
            calls.append(True)
            return 3, 4

    monkeypatch.setenv("TRADING_HEALTH_READY_CACHE_SECONDS", "2")
    app._TRADING_READY_CACHE.clear()
    monkeypatch.setattr(app, "_paper_db_error", None)
    monkeypatch.setattr(app, "_paper_db_durable", True)
    monkeypatch.setattr(app, "_oms", SimpleNamespace(store=Store(), adapter="paper"))

    first = app.health_ready()
    second = app.health_ready()

    assert first == second
    assert first["intents"] == 3
    assert first["orders"] == 4
    assert len(calls) == 1
