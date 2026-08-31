from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments/01-research/api/main.py"


def _load_app():
    os.environ.setdefault("TOOL_GATEWAY_ENFORCE", "false")
    spec = importlib.util.spec_from_file_location("research_health_ready_test_app", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # QA and Research both expose a top-level ``evidence`` package when their
    # standalone API modules are loaded.  Keep this loader independent of
    # pytest import order and restore the caller's package afterwards.
    previous_evidence = {
        name: value
        for name, value in sys.modules.items()
        if name == "evidence" or name.startswith("evidence.")
    }
    for name in previous_evidence:
        sys.modules.pop(name, None)
    try:
        spec.loader.exec_module(module)
    finally:
        for name in tuple(sys.modules):
            if name == "evidence" or name.startswith("evidence."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_evidence)
    return module


def test_health_ready_uses_a_cached_bounded_schema_probe(monkeypatch):
    app = _load_app()
    calls = []

    def query(_sql, _params):
        calls.append(True)
        return [{
            "documents_ready": True,
            "financial_facts_ready": True,
            "macro_observations_ready": True,
        }]

    app._RESEARCH_READY_CACHE.clear()
    monkeypatch.setattr(app, "_query", query)
    monkeypatch.setenv("RESEARCH_HEALTH_READY_CACHE_SECONDS", "30")

    first = app.health_ready()
    assert first["status"] == "ready"
    assert first["canonical_db"] == "READY"
    assert app.health_ready() == first
    assert len(calls) == 1


def test_health_caches_expensive_diagnostic_projection(monkeypatch):
    app = _load_app()
    calls = []

    def query(_sql, _params):
        calls.append(True)
        return [{"domain": "financial_facts", "rows": 1, "last": None}]

    app._RESEARCH_DIAGNOSTIC_CACHE.clear()
    monkeypatch.setattr(app, "_query", query)
    monkeypatch.setenv("RESEARCH_HEALTH_CACHE_SECONDS", "30")

    assert app.health() == app.health()
    assert len(calls) == 1


def test_health_ready_fails_closed_for_an_incomplete_schema(monkeypatch):
    app = _load_app()
    app._RESEARCH_READY_CACHE.clear()
    monkeypatch.setattr(
        app,
        "_query",
        lambda _sql, _params: [{
            "documents_ready": True,
            "financial_facts_ready": False,
            "macro_observations_ready": True,
        }],
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.health_ready()
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error_code"] == "RuntimeError"
