from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from apps.api import strategy_runtime


def test_dynamic_paper_name_is_strictly_derived_from_deployment_id() -> None:
    assert (
        strategy_runtime._deployment_container_name(
            "deployment-0123456789abcdef01234567"
        )
        == "strategy-paper-0123456789abcdef01234567"
    )
    with pytest.raises(strategy_runtime.StrategyRuntimeError):
        strategy_runtime._deployment_container_name("strategy-paper-arbitrary")


def test_paper_power_controls_only_the_derived_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strategy_runtime, "STRATEGY_CONTAINER_CONTROL_ENABLED", True)
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        strategy_runtime,
        "container_status",
        lambda name: {"found": True, "running": name.endswith("67")},
    )

    def fake_docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["docker", *args], 0, "", "")

    monkeypatch.setattr(strategy_runtime, "_docker", fake_docker)
    result = strategy_runtime.power_paper_deployment(
        deployment_id="deployment-0123456789abcdef01234567", action="stop"
    )

    assert result["container_name"] == "strategy-paper-0123456789abcdef01234567"
    assert calls == [("stop", "strategy-paper-0123456789abcdef01234567")]


def test_paper_remove_is_idempotent_and_does_not_remove_state_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strategy_runtime, "STRATEGY_CONTAINER_CONTROL_ENABLED", True)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(strategy_runtime, "container_status", lambda _name: {"found": True})

    def fake_docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["docker", *args], 0, "", "")

    monkeypatch.setattr(strategy_runtime, "_docker", fake_docker)
    result = strategy_runtime.remove_paper_deployment(
        deployment_id="deployment-0123456789abcdef01234567"
    )

    assert result == {
        "deployment_id": "deployment-0123456789abcdef01234567",
        "container_name": "strategy-paper-0123456789abcdef01234567",
        "runtime_status": "REMOVED",
        "execution_status": "DISABLED",
    }
    assert calls == [
        ("rm", "-f", "strategy-paper-0123456789abcdef01234567")
    ]


def test_deploy_builds_a_restricted_signal_only_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_runtime, "STRATEGY_CONTAINER_CONTROL_ENABLED", True)
    monkeypatch.setattr(strategy_runtime, "STRATEGY_PAPER_IMAGE", "fixed-paper-image:test")
    calls: list[tuple[str, ...]] = []
    started = False

    def fake_status(name: str) -> dict[str, object]:
        if name == "hedgefund-strategy-runtime-control":
            return {"found": True, "running": True}
        return {"found": started, "running": started}

    monkeypatch.setattr(strategy_runtime, "container_status", fake_status)

    def fake_docker(*args: str) -> subprocess.CompletedProcess[str]:
        nonlocal started
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args[0] == "run":
            started = True
            return subprocess.CompletedProcess(["docker", *args], 0, "child-id", "")
        raise AssertionError(args)

    monkeypatch.setattr(strategy_runtime, "_docker", fake_docker)
    result = strategy_runtime.deploy_paper_bundle(
        deployment_id="deployment-0123456789abcdef01234567",
        request_id="research-runtime-01",
        bundle_path="/var/lib/autonomous-research/labs/research-runtime-01/deployments/bundles/deployment-0123456789abcdef01234567.json",
        bundle_hash="a" * 64,
    )

    run = calls[-1]
    assert result["execution_status"] == "SIGNAL_ONLY"
    assert "--read-only" in run
    assert "--cap-drop" in run and "ALL" in run
    assert "--network" in run
    assert "container:hedgefund-strategy-runtime-control" in run
    assert "--mount" in run
    assert not any("docker.sock" in arg for arg in run)
    assert "--expected-hash" in run and "a" * 64 in run


def test_runtime_control_requires_service_token_but_keeps_health_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api import strategy_runtime_server

    monkeypatch.delenv("STRATEGY_RUNTIME_SERVICE_TOKEN", raising=False)
    client = TestClient(strategy_runtime_server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/snapshot").json() == {
        "detail": "strategy_runtime_auth_unconfigured"
    }

    token = "t" * 32
    monkeypatch.setenv("STRATEGY_RUNTIME_SERVICE_TOKEN", token)
    monkeypatch.setattr(
        strategy_runtime_server.strategy_runtime,
        "strategy_snapshot",
        lambda: {"ok": True},
    )
    assert client.get("/snapshot").status_code == 401
    response = client.get(
        "/snapshot", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
