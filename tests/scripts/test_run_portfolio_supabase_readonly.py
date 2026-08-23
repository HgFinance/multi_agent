"""CLI profile input validation and preflight tests."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_portfolio_supabase_readonly.py"
SPEC = importlib.util.spec_from_file_location("portfolio_supabase_cli", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def test_load_profile_accepts_json_object() -> None:
    assert cli._load_profile('{"mindset":"BALANCED"}', None) == {"mindset": "BALANCED"}


def test_load_profile_accepts_pretty_json_file(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text('{\n  "mindset": "BALANCED",\n  "experience": "BEGINNER"\n}\n', encoding="utf-8")

    assert cli._load_profile(None, path)["experience"] == "BEGINNER"


def test_load_profile_explains_literal_newline_inside_value() -> None:
    with pytest.raises(SystemExit, match="Literal newlines"):
        cli._load_profile('{"user_id":"user-\n001"}', None)


def test_load_profile_requires_exactly_one_input() -> None:
    with pytest.raises(SystemExit, match="exactly one"):
        cli._load_profile(None, None)

    with pytest.raises(SystemExit, match="exactly one"):
        cli._load_profile("", Path("profile.json"))


def test_diagnose_only_does_not_require_a_profile(monkeypatch, capsys) -> None:
    before_database_url = os.environ.get("DATABASE_URL")
    before_tracing = os.environ.get("LANGSMITH_TRACING")

    class Diagnostics:
        status = "PASS"

        def as_dict(self):
            return {"status": self.status, "external_writes": False}

    class Snapshot:
        def as_pipeline_context(self):
            return {
                "source": "CONTROL_DB",
                "as_of": "2026-08-04T00:00:00+00:00",
                "quality_status": "PASS",
                "candidates": [{"portfolio_id": "balanced"}],
                "research": {"status": "REQUEST_TIME_MCP", "documents": []},
                "market": {"snapshots": [{}]},
                "reasons": [],
                "read_only": True,
                "external_writes": False,
            }

    class Adapter:
        async def diagnose_connection(self):
            return Diagnostics()

        async def load_snapshot(self, *, as_of):
            assert as_of == "2026-08-04T00:00:00+00:00"
            return Snapshot()

    monkeypatch.setattr(cli, "_load_readonly_adapter", lambda: Adapter())
    exit_code = asyncio.run(
        cli.main_async(
            [
                "--diagnose-only",
                "--as-of",
                "2026-08-04T00:00:00+00:00",
            ]
        )
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"candidate_count": 1' in output
    assert '"research_mode": "REQUEST_TIME_MCP"' in output
    assert os.environ.get("DATABASE_URL") == before_database_url
    assert os.environ.get("LANGSMITH_TRACING") == before_tracing
