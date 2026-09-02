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
    semantic = next(command for command in calls if command[:2] == ["docker", "exec"])
    assert semantic[2].startswith("hgfinance-portfolio-bff-readiness-canary-")
    assert semantic[3:5] == ["python", "-c"]
    assert "order_grammar.dynamic_universe" in semantic[5]


def test_trading_canary_does_not_run_portfolio_order_language(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        deployer,
        "_run",
        lambda command, *, capture=False: calls.append(command) or "",
    )
    monkeypatch.setattr(deployer, "_wait_healthy", lambda name, timeout: None)
    monkeypatch.setattr(deployer.subprocess, "run", lambda *args, **kwargs: None)

    deployer.deploy("trading-api", ["docker-compose.yml"], 30)

    assert not any(command[:2] == ["docker", "exec"] for command in calls)


def test_trading_worker_canary_pins_current_day_reconciliation(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        deployer,
        "_run",
        lambda command, *, capture=False: calls.append(command) or "",
    )
    monkeypatch.setattr(deployer, "_wait_healthy", lambda name, timeout: None)
    monkeypatch.setattr(deployer.subprocess, "run", lambda *args, **kwargs: None)

    deployer.deploy("trading-directive-worker", ["docker-compose.yml"], 30)

    semantic = next(command for command in calls if command[:2] == ["docker", "exec"])
    assert semantic[2].startswith(
        "hgfinance-trading-directive-worker-readiness-canary-"
    )
    assert semantic[3:5] == ["python", "-c"]
    assert "_today_execution_status_rows" in semantic[5]
    assert "acknowledge_broker_leg" in semantic[5]
    assert any(command[-2:] == ["build", "trading-directive-worker"] for command in calls)


def test_accounting_canary_pins_broker_time_fill_order(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        deployer,
        "_run",
        lambda command, *, capture=False: calls.append(command) or "",
    )
    monkeypatch.setattr(deployer, "_wait_healthy", lambda name, timeout: None)
    monkeypatch.setattr(deployer.subprocess, "run", lambda *args, **kwargs: None)

    deployer.deploy("accounting-ledger-consumer", ["docker-compose.yml"], 30)

    semantic = next(command for command in calls if command[:2] == ["docker", "exec"])
    assert semantic[2].startswith(
        "hgfinance-accounting-ledger-consumer-readiness-canary-"
    )
    assert "order by delivered.event_time, delivered.outbox_id" in semantic[5]
    assert any(
        command[-2:] == ["build", "accounting-ledger-consumer"]
        for command in calls
    )


def test_ai_office_canary_contains_active_order_controls(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        deployer,
        "_run",
        lambda command, *, capture=False: calls.append(command) or "",
    )
    monkeypatch.setattr(deployer, "_wait_healthy", lambda name, timeout: None)
    monkeypatch.setattr(deployer.subprocess, "run", lambda *args, **kwargs: None)

    deployer.deploy("ai-office", ["docker-compose.yml"], 30)

    semantic = next(command for command in calls if command[:2] == ["docker", "exec"])
    assert semantic[2].startswith("hgfinance-ai-office-readiness-canary-")
    assert semantic[3:5] == ["node", "-e"]
    assert "수정 저장" in semantic[5]
    assert "조건주문을 철회했습니다" in semantic[5]
