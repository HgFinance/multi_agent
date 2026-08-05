from __future__ import annotations

import copy

import pytest

from departments.risk_qa_testkit import make_test_packet
from departments.risk_qa_testkit.replay import (
    ReplayValidationError,
    build_test_replay_bundle,
    validate_replay_bundle,
)


def test_replay_bundle_preserves_trace_hash_and_is_deterministic() -> None:
    packet = make_test_packet()
    bundle = build_test_replay_bundle(packet)

    first = validate_replay_bundle(bundle)
    second = validate_replay_bundle(bundle)

    assert first["status"] == "READY"
    assert first["replayable"] is True
    assert first["trace_id"] == packet.trace_id
    assert first["input_hash"] == packet.input_hash
    assert first["event_count"] == 2
    assert first["replay_hash"] == second["replay_hash"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("trace_id",), "other-trace"),
        (("risk_decision", "input_hash"), "other-hash"),
        (("events", 0, "payload", "risk_decision_id"), "other-decision"),
    ],
)
def test_replay_bundle_rejects_cross_domain_mismatch(path: tuple[object, ...], value: str) -> None:
    bundle = build_test_replay_bundle(make_test_packet())
    mutated = copy.deepcopy(bundle)
    target = mutated
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ReplayValidationError):
        validate_replay_bundle(mutated)


def test_replay_bundle_rejects_duplicate_event() -> None:
    bundle = build_test_replay_bundle(make_test_packet())
    bundle["events"].append(copy.deepcopy(bundle["events"][0]))

    with pytest.raises(ReplayValidationError, match="duplicate event_id"):
        validate_replay_bundle(bundle)
