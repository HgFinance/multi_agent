from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.contracts.runtime_service_registry import (
    RuntimeServiceRegistryError,
    load_runtime_service_registry,
    services_for_department,
)


ROOT = Path(__file__).resolve().parents[1]


def test_trading_deterministic_service_is_registered_once() -> None:
    registry = load_runtime_service_registry(ROOT)
    trading = services_for_department(registry, "trading")

    assert len(trading) == 1
    assert trading[0].service_id == "trading-directive-worker"
    assert trading[0].worker_id == "desk-runner"
    assert trading[0].kind == "deterministic"
    assert trading[0].trigger == "always"

    compose = (ROOT / "departments/02-trading/compose.yaml").read_text(encoding="utf-8")
    trading_worker = (ROOT / "departments/02-trading/employee_workers.py").read_text(
        encoding="utf-8"
    )
    assert "trading-directive-worker" in compose
    assert "desk-runner" in trading_worker


def test_runtime_service_registry_rejects_unknown_kind(tmp_path: Path) -> None:
    target = tmp_path / "orchestration/contracts/runtime_service_registry.v1.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": "hgfinance.runtime-service-registry.v1",
                "services": [
                    {
                        "department": "trading",
                        "service_id": "x",
                        "worker_id": "y",
                        "kind": "llm",
                        "trigger": "always",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeServiceRegistryError, match="service_kind"):
        load_runtime_service_registry(tmp_path)
