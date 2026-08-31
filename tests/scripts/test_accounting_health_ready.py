from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments" / "05-accounting-portfolio" / "api" / "app.py"
_ACCOUNTING_IMPORTS = (
    "contracts",
    "corporate_actions",
    "daily_report",
    "db_read_model",
    "financial_statements",
    "fill_consumer",
    "investor_profile_repository",
    "ledger",
    "portfolio",
    "recon_repository",
    "reconciliation",
    "repository",
    "suitability",
)


def _load_app():
    # This test must never create a durable connection or write to the Paper
    # ledger just by importing the API module.
    os.environ["ACCOUNTING_MODE"] = "OFFLINE"
    if str(API_PATH.parent) not in sys.path:
        sys.path.insert(0, str(API_PATH.parent))
    spec = importlib.util.spec_from_file_location(
        "accounting_health_ready_test_app", API_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    # Several department self-checks use direct imports such as ``repository``.
    # Keep those test-only module names from making this loader import the
    # similarly named QA repository when the full suite runs in one process.
    previous = {name: sys.modules.get(name) for name in _ACCOUNTING_IMPORTS}
    for name in _ACCOUNTING_IMPORTS:
        sys.modules.pop(name, None)
    try:
        spec.loader.exec_module(module)
    finally:
        for name in _ACCOUNTING_IMPORTS:
            sys.modules.pop(name, None)
            prior = previous[name]
            if prior is not None:
                sys.modules[name] = prior
    return module


def test_health_ready_uses_a_single_flight_readiness_cache(monkeypatch):
    app = _load_app()
    calls = []

    class Repository:
        def counts(self):
            calls.append(True)
            return 3, 4

    monkeypatch.setenv("ACCOUNTING_HEALTH_READY_CACHE_SECONDS", "2")
    app._ACCOUNTING_READY_CACHE.clear()
    monkeypatch.setattr(app, "_store_error", None)
    monkeypatch.setattr(app, "_repo", Repository())

    first = app.health_ready()
    second = app.health_ready()

    assert first == second
    assert first["ledgers"] == 3
    assert first["journals"] == 4
    assert len(calls) == 1
