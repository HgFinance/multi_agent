from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "departments" / "01-research" / "api" / "mcp_server.py"


def _load_mcp_server():
    spec = importlib.util.spec_from_file_location(
        "research_mcp_skeptic_cache_test", MCP_SERVER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, rows):
        self.cursor_instance = _Cursor(rows)

    def cursor(self):
        return self.cursor_instance


def _reviews():
    return [{
        "title": "Flow gate",
        "competing_explanation": "The result may be liquidity premium.",
        "competing_codes": ["LIQUIDITY_PREMIUM"],
        "verdict": "PROCEED",
        "falsification_test": "Neutralize spread and timestamp alignment.",
    }]


def test_exact_draft_cache_requires_current_contract_and_complete_title_set():
    mcp = _load_mcp_server()
    draft = "TITLE: Flow gate\nLEAD_IDS: lead-1"
    conn = _Conn([(
        "Flow gate",
        "The result may be liquidity premium.",
        ["LIQUIDITY_PREMIUM"],
        "PROCEED",
        "Neutralize spread and timestamp alignment.",
        "planner-1",
        "skeptic-1",
        datetime(2026, 8, 24, tzinfo=timezone.utc),
    )])

    reviews, metadata, error = mcp.load_cached_skeptic_reviews(conn, draft)

    assert not error
    assert reviews == _reviews()
    assert metadata["cache_key"] == mcp._text_digest(draft)
    sql, params = conn.cursor_instance.calls[0]
    assert "review_contract_version" in sql
    assert params == (mcp._text_digest(draft), mcp._SKEPTIC_REVIEW_CONTRACT_VERSION)


def test_cached_result_remains_non_binding_and_reusable_by_exact_digest(monkeypatch):
    mcp = _load_mcp_server()
    draft = "TITLE: Flow gate\nLEAD_IDS: lead-1"
    result = mcp._cached_skeptic_result(
        draft,
        _reviews(),
        {
            "cache_key": mcp._text_digest(draft),
            "source_skeptic_runs": ["skeptic-1"],
            "source_planner_runs": ["planner-1"],
            "created_at": "2026-08-24T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(mcp, "_WORKER_JOBS", {
        "job-1": {
            "job_id": "job-1",
            "payload_fields": ["proposal_draft"],
            "proposal_draft_sha256": mcp._text_digest(draft),
            "status": "COMPLETED",
            "result": result,
        }
    })

    reusable = mcp._reusable_skeptic_job(
        {"proposal_draft": draft}, draft
    )

    assert reusable and reusable["job_id"] == "job-1"
    assert result["binding"] is False
    assert result["cache_hit"] is True
    assert result["workers"][0]["output"]["schema_valid"] is True
