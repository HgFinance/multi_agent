"""Risk/QA 공통 Replay 계약과 결정론적 검증기.

이 모듈은 운영 DB를 읽거나 결정을 새로 만들지 않는다. Research Packet,
Risk Decision, QA Decision, Redis Event Envelope를 하나의 trace로 재생할 수
있는지 검증하는 경계다. 실제 API/DB 왕복은 별도의 Runtime smoke가 증명해야
하며, 이 검증기 통과만으로 운영 승인을 의미하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from typing import Any


RISK_DECISION_EVENT = "risk.decision.v1"
QA_DECISION_EVENT = "qa.decision.v1"
REQUIRED_ENTITY_TYPES = {"research_packet", "risk_decision", "qa_decision"}
REQUIRED_EVENT_TYPES = {RISK_DECISION_EVENT, QA_DECISION_EVENT}


class ReplayValidationError(ValueError):
    """Replay bundle이 trace/hash/idempotency 계약을 위반했다."""


def _text(value: Any, field: str) -> str:
    if value is None:
        raise ReplayValidationError(f"{field} is required")
    normalized = str(value).strip()
    if not normalized:
        raise ReplayValidationError(f"{field} must not be empty")
    return normalized


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload", {})
    if not isinstance(payload, Mapping):
        raise ReplayValidationError("event.payload must be an object")
    return payload


def canonical_replay_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable, credential-free representation used for replay hashing."""

    entities = bundle.get("entities", [])
    events = bundle.get("events", [])
    if not isinstance(entities, Sequence) or isinstance(entities, (str, bytes)):
        raise ReplayValidationError("entities must be a list")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ReplayValidationError("events must be a list")
    return {
        "packet_id": _text(bundle.get("packet_id"), "packet_id"),
        "artifact_id": _text(bundle.get("artifact_id"), "artifact_id"),
        "case_id": _text(bundle.get("case_id"), "case_id"),
        "trace_id": _text(bundle.get("trace_id"), "trace_id"),
        "input_hash": _text(bundle.get("input_hash"), "input_hash"),
        "risk_decision": _canonical(bundle.get("risk_decision", {})),
        "qa_decision": _canonical(bundle.get("qa_decision", {})),
        "entities": sorted((_canonical(entity) for entity in entities), key=lambda item: str(item.get("entity_id", ""))),
        "events": sorted((_canonical(event) for event in events), key=lambda item: str(item.get("event_id", ""))),
    }


def validate_replay_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a cross-domain Replay bundle and return its deterministic report."""

    canonical = canonical_replay_payload(bundle)
    trace_id = canonical["trace_id"]
    input_hash = canonical["input_hash"]
    errors: list[str] = []

    risk_decision = canonical["risk_decision"]
    qa_decision = canonical["qa_decision"]
    if not isinstance(risk_decision, Mapping):
        errors.append("risk_decision must be an object")
        risk_decision = {}
    if not isinstance(qa_decision, Mapping):
        errors.append("qa_decision must be an object")
        qa_decision = {}

    risk_decision_id = str(risk_decision.get("risk_decision_id", "")).strip()
    qa_decision_id = str(qa_decision.get("qa_decision_id", "")).strip()
    if not risk_decision_id:
        errors.append("risk_decision.risk_decision_id is required")
    if not qa_decision_id:
        errors.append("qa_decision.qa_decision_id is required")

    for name, decision, decision_id in (
        ("risk_decision", risk_decision, risk_decision_id),
        ("qa_decision", qa_decision, qa_decision_id),
    ):
        if str(decision.get("trace_id", "")).strip() != trace_id:
            errors.append(f"{name}.trace_id does not match bundle.trace_id")
        if str(decision.get("input_hash", "")).strip() != input_hash:
            errors.append(f"{name}.input_hash does not match bundle.input_hash")
        if str(decision.get(f"{name}_id", "")).strip() != decision_id:
            errors.append(f"{name} id is not self-consistent")

    entities = canonical["entities"]
    entity_ids: set[str] = set()
    entity_types: set[str] = set()
    for index, entity in enumerate(entities):
        if not isinstance(entity, Mapping):
            errors.append(f"entities[{index}] must be an object")
            continue
        entity_id = str(entity.get("entity_id", "")).strip()
        entity_type = str(entity.get("entity_type", "")).strip()
        if not entity_id or not entity_type:
            errors.append(f"entities[{index}] requires entity_id and entity_type")
            continue
        if entity_id in entity_ids:
            errors.append(f"duplicate entity_id: {entity_id}")
        entity_ids.add(entity_id)
        entity_types.add(entity_type)
        if str(entity.get("trace_id", "")).strip() != trace_id:
            errors.append(f"entity {entity_id} trace_id does not match bundle")
        if str(entity.get("input_hash", "")).strip() != input_hash:
            errors.append(f"entity {entity_id} input_hash does not match bundle")

    missing_entity_types = REQUIRED_ENTITY_TYPES - entity_types
    if missing_entity_types:
        errors.append(f"missing entity types: {sorted(missing_entity_types)}")
    if risk_decision_id not in entity_ids:
        errors.append("risk_decision entity is missing")
    if qa_decision_id not in entity_ids:
        errors.append("qa_decision entity is missing")

    events = canonical["events"]
    event_ids: set[str] = set()
    event_types: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(f"events[{index}] must be an object")
            continue
        event_id = str(event.get("event_id", "")).strip()
        event_type = str(event.get("event_type", "")).strip()
        if not event_id or not event_type:
            errors.append(f"events[{index}] requires event_id and event_type")
            continue
        if event_id in event_ids:
            errors.append(f"duplicate event_id: {event_id}")
        event_ids.add(event_id)
        event_types.add(event_type)
        if str(event.get("trace_id", "")).strip() != trace_id:
            errors.append(f"event {event_id} trace_id does not match bundle")
        payload = _event_payload(event)
        if str(payload.get("trace_id", trace_id)).strip() != trace_id:
            errors.append(f"event {event_id} payload.trace_id does not match bundle")
        if str(payload.get("input_hash", input_hash)).strip() != input_hash:
            errors.append(f"event {event_id} payload.input_hash does not match bundle")
        if event_type == RISK_DECISION_EVENT and str(payload.get("risk_decision_id", "")).strip() != risk_decision_id:
            errors.append(f"event {event_id} does not reference risk decision")
        if event_type == QA_DECISION_EVENT and str(payload.get("qa_decision_id", "")).strip() != qa_decision_id:
            errors.append(f"event {event_id} does not reference QA decision")

    missing_event_types = REQUIRED_EVENT_TYPES - event_types
    if missing_event_types:
        errors.append(f"missing event types: {sorted(missing_event_types)}")

    if errors:
        raise ReplayValidationError("; ".join(errors))

    return {
        "status": "READY",
        "replayable": True,
        "packet_id": canonical["packet_id"],
        "case_id": canonical["case_id"],
        "trace_id": trace_id,
        "input_hash": input_hash,
        "entity_count": len(entities),
        "event_count": len(events),
        "replay_hash": _digest(canonical),
    }


def build_test_replay_bundle(packet: Any, *, risk_verdict: str = "REJECT", qa_decision: str = "WARN") -> dict[str, Any]:
    """Build a deterministic contract fixture from a canonical Risk/QA packet."""

    trace_id = _text(packet.trace_id, "packet.trace_id")
    input_hash = _text(packet.input_hash, "packet.input_hash")
    risk_decision_id = f"risk-decision:{trace_id}:{input_hash[:16]}"
    qa_decision_id = f"qa-decision:{trace_id}:{input_hash[:16]}"
    assessment = packet.risk_input.get("assessment") or {}
    order_intent_id = str(assessment.get("order_intent_id", ""))
    entities = [
        {"entity_type": "research_packet", "entity_id": packet.packet_id, "trace_id": trace_id, "input_hash": input_hash},
        {"entity_type": "risk_decision", "entity_id": risk_decision_id, "trace_id": trace_id, "input_hash": input_hash},
        {"entity_type": "qa_decision", "entity_id": qa_decision_id, "trace_id": trace_id, "input_hash": input_hash},
    ]
    if order_intent_id:
        entities.append({"entity_type": "order_intent", "entity_id": order_intent_id, "trace_id": trace_id, "input_hash": input_hash})
    return {
        "packet_id": packet.packet_id,
        "artifact_id": packet.artifact_id,
        "case_id": packet.case_id,
        "trace_id": trace_id,
        "input_hash": input_hash,
        "risk_decision": {"risk_decision_id": risk_decision_id, "decision": risk_verdict, "trace_id": trace_id, "input_hash": input_hash},
        "qa_decision": {"qa_decision_id": qa_decision_id, "decision": qa_decision, "trace_id": trace_id, "input_hash": input_hash},
        "entities": entities,
        "events": [
            {
                "event_id": f"risk-event:{trace_id}:{input_hash[:16]}",
                "event_type": RISK_DECISION_EVENT,
                "trace_id": trace_id,
                "payload": {"risk_decision_id": risk_decision_id, "input_hash": input_hash, "trace_id": trace_id},
            },
            {
                "event_id": qa_decision_id,
                "event_type": QA_DECISION_EVENT,
                "trace_id": trace_id,
                "payload": {"qa_decision_id": qa_decision_id, "input_hash": input_hash, "trace_id": trace_id},
            },
        ],
    }
