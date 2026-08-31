from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments" / "03-risk" / "api" / "app.py"


def _load_app():
    if str(API_PATH.parent) not in sys.path:
        sys.path.insert(0, str(API_PATH.parent))
    spec = importlib.util.spec_from_file_location(
        "risk_runtime_observability_cache_test_app", API_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_observability_single_flights_database_projection(monkeypatch):
    app = _load_app()
    calls = 0

    class Repository:
        def runtime_observability(self):
            nonlocal calls
            calls += 1
            time.sleep(0.02)
            return {
                "event_count": 1,
                "pipeline_count": 1,
                "fallback_count": 0,
                "p50_seconds": 0.01,
                "p99_seconds": 0.02,
                "latest_event_at": None,
                "position_plan_count": 0,
                "latest_position_plan_at": None,
            }

    monkeypatch.setattr(app, "_canonical_database_url", lambda: "dsn")
    monkeypatch.setattr(app, "_control_repository", Repository())
    app._RISK_RUNTIME_CACHE.clear()

    with ThreadPoolExecutor(max_workers=32) as executor:
        responses = list(executor.map(lambda _item: app.runtime_observability(), range(32)))

    assert calls == 1
    assert all(response["canonical_store"] == "READY" for response in responses)
    assert all(response["durable"]["event_count"] == 1 for response in responses)
