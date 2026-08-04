"""Shared Worker Registry async fan-out/fan-in acceptance test."""

from __future__ import annotations

import asyncio

from departments.employee_worker_runtime import WorkerSpec, run_worker_registry_async


def test_worker_registry_uses_async_independent_graphs() -> None:
    specs = (
        WorkerSpec(
            "worker-a",
            "Test research worker",
            ("test.read",),
            "always",
            ("payload",),
        ),
        WorkerSpec(
            "worker-b",
            "Test risk worker",
            ("test.read",),
            "always",
            ("payload",),
        ),
    )

    def read_context(payload):
        return {"payload": payload.get("payload"), "read_only": True}

    def deterministic_llm(system: str, prompt: str) -> str:
        return '{"summary":"async worker completed","confidence":0.9,"evidence_refs":["test:context"],"escalate":false}'

    result = asyncio.run(
        run_worker_registry_async(
            specs,
            {"payload": "fan-out-input"},
            tools={"worker-a": read_context, "worker-b": read_context},
            llm=deterministic_llm,
        )
    )

    assert result["runtime"]["topology"] == "async_fan_out_fan_in_independent_graphs"
    assert [item["worker_id"] for item in result["workers"]] == ["worker-a", "worker-b"]
    assert result["executed"] == ["worker-a", "worker-b"]
    assert result["failed"] == []
    assert result["binding"] is False
