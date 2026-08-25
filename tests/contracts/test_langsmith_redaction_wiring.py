"""Deployment contract for payload-free LangSmith automatic callbacks."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_every_traced_service_hides_callback_inputs_and_outputs() -> None:
    document = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    traced = {
        name: service["environment"]
        for name, service in document["services"].items()
        if "LANGSMITH_TRACING" in (service.get("environment") or {})
    }

    assert traced
    for name, environment in traced.items():
        assert environment["LANGSMITH_HIDE_INPUTS"] == "true", name
        assert environment["LANGSMITH_HIDE_OUTPUTS"] == "true", name
