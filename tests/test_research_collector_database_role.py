from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path("departments/01-research/collectors/source_registry.py")
    spec = importlib.util.spec_from_file_location("research_source_registry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_role_is_attached_to_only_the_selected_control_dsn() -> None:
    module = _module()
    control = module._pg_runtime_role(
        "postgresql://runtime:secret@timescaledb:5432/control?keepalives=1",
        "svc_research_collector",
    )
    market = "postgresql://runtime:secret@timescaledb:5432/market"

    assert "options=-c%20role%3Dsvc_research_collector" in control
    assert "keepalives=1&options=" in control
    assert "options=" not in market


@pytest.mark.parametrize("role", ["svc-research", "postgres;drop role x", "UPPER"])
def test_collector_role_rejects_unreviewed_role_syntax(role: str) -> None:
    module = _module()
    with pytest.raises(ValueError):
        module._pg_runtime_role("postgresql://runtime@db/control", role)


def test_collector_role_does_not_override_existing_libpq_options() -> None:
    module = _module()
    with pytest.raises(ValueError):
        module._pg_runtime_role(
            "postgresql://runtime@db/control?options=-c%20role%3Dother",
            "svc_research_collector",
        )
