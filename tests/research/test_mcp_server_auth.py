from __future__ import annotations

import importlib.util
from pathlib import Path


def _research_mcp_server():
    path = (
        Path(__file__).resolve().parents[2]
        / "departments"
        / "01-research"
        / "api"
        / "mcp_server.py"
    )
    spec = importlib.util.spec_from_file_location("hgfinance_research_mcp_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internal_healthcheck_reuses_configured_mcp_bearer_without_disabling_auth() -> None:
    module = _research_mcp_server()

    assert module._healthcheck_headers("") == {}
    assert module._healthcheck_headers(None) == {}
    assert module._healthcheck_headers("  secret-token  ") == {
        "Authorization": "Bearer secret-token"
    }
    assert module.is_authorized(None, "secret-token") is False
