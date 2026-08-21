from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIAISON_ROOT = ROOT / "departments" / "01-research" / "hermes-liaison"


def test_research_liaison_declares_terminal_handoff_contract():
    soul = (LIAISON_ROOT / "SOUL.md").read_text()
    config = (LIAISON_ROOT / "config.yaml").read_text()

    assert "kanban_complete" in soul
    assert "kanban_block" in soul
    assert "result" in soul
    assert "먼저 답을 완성하고" in soul
    assert "mutation_authority: false" in config
    assert "research-liaison-mcp" in config
