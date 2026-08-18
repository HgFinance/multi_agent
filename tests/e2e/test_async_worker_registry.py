"""Shared Worker Registry async fan-out/fan-in acceptance test."""

from __future__ import annotations

import asyncio

from departments.employee_worker_runtime import (
    StructuredArtifactSpec,
    WorkerSpec,
    run_worker_registry_async,
)


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


def test_worker_registry_only_calls_workers_for_explicit_triggers() -> None:
    specs = (
        WorkerSpec("always-worker", "Always-on worker", ("test.read",)),
        WorkerSpec(
            "conditional-worker",
            "Conditionally requested worker",
            ("test.read",),
            "run_conditional_worker",
        ),
    )
    calls: list[str] = []

    def read_context(payload):
        return {"payload": payload, "read_only": True}

    def deterministic_llm(_system: str, prompt: str) -> str:
        worker_id = next(
            line.split(":", 1)[1].strip()
            for line in prompt.splitlines()
            if line.startswith("Worker id:")
        )
        calls.append(worker_id)
        return '{"summary":"triggered","confidence":0.9,"evidence_refs":["test:trigger"],"escalate":false}'

    result = asyncio.run(
        run_worker_registry_async(
            specs,
            {"run_conditional_worker": False},
            tools={"always-worker": read_context, "conditional-worker": read_context},
            llm=deterministic_llm,
        )
    )

    assert result["executed"] == ["always-worker"]
    assert result["not_executed"] == ["conditional-worker"]
    assert calls == ["always-worker"]
    assert result["failed"] == []


def _typed_skeptic_spec(max_attempts: int = 3) -> WorkerSpec:
    return WorkerSpec(
        "typed-skeptic",
        "Independent skeptic",
        ("test.read",),
        input_fields=("proposal_draft",),
        max_attempts=max_attempts,
        structured_artifact=StructuredArtifactSpec(
            key="skeptic_reviews",
            required_strings=("title", "competing_explanation", "verdict",
                              "falsification_test"),
            required_string_lists=("competing_codes",),
            enum_values=(("competing_codes", ("DATA_MINING", "COST_UNACCOUNTED")),
                         ("verdict", ("PROCEED", "STOP"))),
            many=True,
        ),
    )


def test_worker_registry_preserves_validated_typed_artifact() -> None:
    raw = (
        '{"summary":"reviewed","confidence":0.8,'
        '"evidence_refs":["proposal:draft"],"escalate":false,'
        '"skeptic_reviews":[{"title":"OFI edge",'
        '"competing_explanation":"Selection may explain the result",'
        '"competing_codes":["DATA_MINING"],"verdict":"PROCEED",'
        '"falsification_test":"Run nested walk-forward selection"}]}'
    )
    result = asyncio.run(run_worker_registry_async(
        (_typed_skeptic_spec(),), {"proposal_draft": "TITLE: OFI edge"},
        tools={"typed-skeptic": lambda payload: payload},
        llm=lambda _system, _prompt: raw,
    ))

    report = result["workers"][0]
    assert report["status"] == "COMPLETED"
    assert report["output"]["schema_valid"] is True
    assert report["output"]["skeptic_reviews"][0]["verdict"] == "PROCEED"


def test_worker_registry_retries_then_degrades_without_typed_artifact() -> None:
    calls = 0

    def generic_only(_system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        return ('{"summary":"prose only","confidence":0.8,'
                '"evidence_refs":["proposal:draft"],"escalate":false}')

    result = asyncio.run(run_worker_registry_async(
        (_typed_skeptic_spec(max_attempts=2),), {"proposal_draft": "draft"},
        tools={"typed-skeptic": lambda payload: payload}, llm=generic_only,
    ))

    assert calls == 2
    assert result["workers"][0]["status"] == "DEGRADED"
    assert result["failed"] == ["typed-skeptic"]


def test_worker_registry_gives_bounded_retry_schema_feedback() -> None:
    prompts: list[str] = []
    invalid = (
        '{"summary":"reviewed","confidence":0.8,'
        '"evidence_refs":["proposal:draft"],"escalate":false,'
        '"skeptic_reviews":[{"title":"OFI edge",'
        '"competing_explanation":"Selection may explain the result",'
        '"verdict":"PROCEED","falsification_test":["nested holdout"]}]}'
    )
    valid = (
        '{"summary":"reviewed","confidence":0.8,'
        '"evidence_refs":["proposal:draft"],"escalate":false,'
        '"skeptic_reviews":[{"title":"OFI edge",'
        '"competing_explanation":"Selection may explain the result",'
        '"competing_codes":["DATA_MINING"],"verdict":"PROCEED",'
        '"falsification_test":"nested holdout"}]}'
    )

    def repairing_llm(_system: str, prompt: str) -> str:
        prompts.append(prompt)
        return invalid if len(prompts) == 1 else valid

    result = asyncio.run(run_worker_registry_async(
        (_typed_skeptic_spec(max_attempts=2),), {"proposal_draft": "draft"},
        tools={"typed-skeptic": lambda payload: payload}, llm=repairing_llm,
    ))

    assert result["workers"][0]["status"] == "COMPLETED"
    assert result["workers"][0]["attempts"] == 2
    assert "previous JSON failed machine validation" in prompts[1]
    assert "Previous invalid JSON" in prompts[1]


def test_worker_registry_passes_decoder_schema_to_capable_gateway() -> None:
    seen: dict = {}

    def schema_llm(_system: str, _prompt: str, *, json_schema=None) -> str:
        seen["schema"] = json_schema
        return (
            '{"summary":"reviewed","confidence":0.8,'
            '"evidence_refs":["proposal:draft"],"escalate":false,'
            '"skeptic_reviews":[{"title":"OFI edge",'
            '"competing_explanation":"Selection may explain the result",'
            '"competing_codes":["DATA_MINING"],"verdict":"PROCEED",'
            '"falsification_test":"nested holdout"}]}'
        )

    schema_llm._json_schema_capable = True
    result = asyncio.run(run_worker_registry_async(
        (_typed_skeptic_spec(),), {"proposal_draft": "TITLE: OFI edge"},
        tools={"typed-skeptic": lambda payload: payload}, llm=schema_llm,
    ))

    assert result["workers"][0]["status"] == "COMPLETED"
    schema = seen["schema"]
    assert schema["additionalProperties"] is False
    assert "skeptic_reviews" in schema["required"]
    review = schema["properties"]["skeptic_reviews"]["items"]
    assert review["additionalProperties"] is False
    assert review["properties"]["competing_codes"]["items"]["enum"] == [
        "DATA_MINING", "COST_UNACCOUNTED"
    ]
    assert review["properties"]["verdict"]["enum"] == ["PROCEED", "STOP"]
