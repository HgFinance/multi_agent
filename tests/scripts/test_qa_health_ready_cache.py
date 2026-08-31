from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments" / "06-ai-qa-audit" / "api" / "app.py"


def _load_app():
    if str(API_PATH.parent) not in sys.path:
        sys.path.insert(0, str(API_PATH.parent))
    spec = importlib.util.spec_from_file_location(
        "qa_health_ready_cache_test_app", API_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_ready_single_flights_database_role_probe(monkeypatch):
    monkeypatch.setenv("RISK_QA_RUNTIME", "test")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("RISK_QA_DATABASE_URL", "")
    app = _load_app()
    calls = 0

    class Repository:
        def runtime_database_status(self):
            nonlocal calls
            calls += 1
            time.sleep(0.02)
            return {
                "session_user": "postgres",
                "current_user": "svc_qa_audit",
                "transaction_read_only": "off",
            }

    monkeypatch.setattr(app, "_DATABASE_URL", "dsn")
    monkeypatch.setattr(app, "_audit_repository", Repository())
    app._QA_READY_CACHE.clear()

    with ThreadPoolExecutor(max_workers=32) as executor:
        responses = list(executor.map(lambda _item: app.health_ready(), range(32)))

    assert calls == 1
    assert all(response["canonical_db"] == "READY" for response in responses)
    assert all(response["database_role"] == "svc_qa_audit" for response in responses)
