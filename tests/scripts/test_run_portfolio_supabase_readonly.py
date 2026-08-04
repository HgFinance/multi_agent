"""CLI profile input validation tests."""

from __future__ import annotations

import importlib.util
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
