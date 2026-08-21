"""Keep the CEO worker's CLI tool surface intentionally narrow."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CEO_CONFIG = ROOT / "departments/00-ceo-office/hermes/config.yaml"


def test_ceo_cli_uses_only_kanban_toolset() -> None:
    config = yaml.safe_load(CEO_CONFIG.read_text(encoding="utf-8"))

    assert config["platform_toolsets"]["cli"] == ["kanban"]


def test_ceo_discord_tool_surface_remains_unchanged() -> None:
    config = yaml.safe_load(CEO_CONFIG.read_text(encoding="utf-8"))

    assert config["platform_toolsets"]["discord"] == [
        "kanban",
        "memory",
        "session_search",
        "skills",
        "clarify",
    ]
