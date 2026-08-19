"""Factory/user-query routing and liaison capability boundaries."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
for path in (FACTORY, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import factory_autopilot as autopilot  # noqa: E402


def test_factory_cards_are_stamped_and_cannot_use_query_profiles(monkeypatch) -> None:
    observed: list[list[str]] = []

    def run(argv, **_kwargs):
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout="Created t_test", stderr="")

    monkeypatch.setattr(autopilot.subprocess, "run", run)
    autopilot._create_card(
        title="factory test",
        body="payload",
        assignee=autopilot.RESEARCH_ASSIGNEE,
        key="factory-test",
        dry_run=False,
    )
    body = observed[0][observed[0].index("--body") + 1]
    assert body.startswith("origin=factory\nworkflow_plane=alpha-factory\n")
    assert "user_query_routing=forbidden" in body
    assert "factory_assignee=research-department" in body

    try:
        autopilot._create_card(
            title="misroute",
            body="payload",
            assignee="research-liaison",
            key="factory-misroute",
            dry_run=True,
        )
    except ValueError as exc:
        assert "lab profile" in str(exc)
    else:
        raise AssertionError("factory card accepted a user-query liaison profile")


def test_liaison_profiles_only_bind_the_read_only_research_mcp() -> None:
    paths = (
        ROOT / "departments/01-research/hermes-liaison/config.yaml",
        ROOT / "departments/04-quant-backtest/hermes-liaison/config.yaml",
    )
    for path in paths:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(config["mcp_servers"]) == {"research"}
        server = config["mcp_servers"]["research"]
        assert server["url"] == "http://research-liaison-mcp:8037/mcp"
        assert "TIMESCALE_DATABASE_URL" not in (config.get("env") or {})
        forbidden = set(config["agent"]["forbidden_tools"])
        assert {
            "strategy.promote",
            "factory_submit_leads",
            "factory_submit_proposal",
            "run_research_workers",
        } <= forbidden


def test_profile_sync_includes_both_query_liaisons() -> None:
    script = (ROOT / "scripts/sync_hermes_profiles.sh").read_text(encoding="utf-8")
    assert "research-liaison:01-research" in script
    assert "quant-liaison:04-quant-backtest" in script
    assert '"$SRC_ROOT/$folder/hermes-liaison"' in script
