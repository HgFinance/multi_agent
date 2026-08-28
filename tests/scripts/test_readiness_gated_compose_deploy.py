from __future__ import annotations

import sys

import scripts.readiness_gated_compose_deploy as deployer


def test_deploy_runs_contract_suite_before_build_or_replacement(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, capture: bool = False) -> str:
        calls.append(command)
        return ""

    monkeypatch.setattr(deployer, "_run", fake_run)
    monkeypatch.setattr(deployer, "_wait_healthy", lambda name, timeout: None)
    monkeypatch.setattr(deployer.subprocess, "run", lambda *args, **kwargs: None)

    deployer.deploy("portfolio-bff", ["docker-compose.yml"], 30)

    assert calls[0][-2:] == ["config", "--quiet"]
    assert calls[1] == [sys.executable, "-m", "pytest", "-q", "tests/contracts"]
    assert calls[2][-2:] == ["build", "portfolio-bff"]
    assert any(command[-1] == "portfolio-bff" for command in calls[3:])
