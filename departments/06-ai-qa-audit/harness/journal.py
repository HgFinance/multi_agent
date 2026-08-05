"""QA/Audit 실행 로그·리플레이·리뷰 계약.

Hermes QA 부서가 오케스트레이터이고 각 직원은 LangGraph 실행 단위다. QA는
주문·원장·Risk Limit을 소유하지 않으며, 감사 가능한 원문 이벤트만 기록한다.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

SECRET_FIELDS = frozenset(
    {"api_key", "apikey", "authorization", "password", "secret", "token", "private_key"}
)


class LogEventType(StrEnum):
    INPUT_SNAPSHOT = "InputSnapshot"
    AGENT_OUTPUT = "AgentOutput"
    VALIDATION = "Validation"
    DECISION = "Decision"
    ORDER = "Order"
    FILL = "Fill"


@dataclass(frozen=True, slots=True)
class LogEvent:
    event_id: str
    event_type: LogEventType
    run_id: str
    trace_id: str
    department: str
    hermes_profile: str
    employee_profile: str
    executor: str
    occurred_at: str
    as_of: str | None
    asset: str | None
    inputs_hash: str
    schema_id: str | None = None
    schema_valid: bool | None = None
    domain_valid: bool | None = None
    failed_rule: str | None = None
    retry_count: int = 0
    fallback_reason: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    parameter_version: str | None = None
    rationale: str | None = None
    evidence_refs: tuple[str, ...] = ()
    constraints_applied: tuple[str, ...] = ()
    order_id: str | None = None
    fill_id: str | None = None
    output_hash: str | None = None
    summary: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReplayReport:
    run_id: str
    input_hash_match: bool
    version_match: bool
    output_match: bool
    decision_match: bool
    diffs: tuple[str, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def jsonl_sink(path: str | Path) -> Callable[[LogEvent], None]:
    """Return a secret-safe append-only JSONL sink for a runtime log."""

    target = Path(path)

    def _sink(event: LogEvent) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), sort_keys=True, default=str))
            handle.write("\n")

    return _sink


def _contains_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower().replace("-", "_") in SECRET_FIELDS
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value)
    return ()


class RunJournal:
    """Append-only journal with deterministic replay and review summaries."""

    def __init__(
        self,
        *,
        department: str = "qa-department",
        hermes_profile: str = "qa-department",
        sink: Callable[[LogEvent], None] | None = None,
    ) -> None:
        self.department = department
        self.hermes_profile = hermes_profile
        self._sink = sink
        self._events: list[LogEvent] = []

    @property
    def events(self) -> tuple[LogEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        event_type: LogEventType,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        inputs_hash: str,
        raw: Mapping[str, Any] | None = None,
        as_of: str | None = None,
        asset: str | None = None,
        schema_id: str | None = None,
        schema_valid: bool | None = None,
        domain_valid: bool | None = None,
        failed_rule: str | None = None,
        retry_count: int = 0,
        fallback_reason: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        parameter_version: str | None = None,
        rationale: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        constraints_applied: tuple[str, ...] = (),
        order_id: str | None = None,
        fill_id: str | None = None,
        output_hash: str | None = None,
        summary: str = "",
    ) -> LogEvent:
        if not run_id.strip() or not trace_id.strip() or retry_count < 0:
            raise ValueError("run_id, trace_id and retry_count are required")
        safe_raw = dict(raw or {})
        if _contains_secret(safe_raw):
            raise ValueError("secret_field_in_log_payload")
        if event_type is LogEventType.ORDER and not order_id:
            raise ValueError("order_id_required_for_order_event")
        if event_type is LogEventType.FILL and not fill_id:
            raise ValueError("fill_id_required_for_fill_event")
        event = LogEvent(
            event_id=str(uuid4()),
            event_type=event_type,
            run_id=run_id,
            trace_id=trace_id,
            department=self.department,
            hermes_profile=self.hermes_profile,
            employee_profile=employee_profile,
            executor="langgraph",
            occurred_at=_now(),
            as_of=as_of,
            asset=asset,
            inputs_hash=inputs_hash,
            schema_id=schema_id,
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            retry_count=retry_count,
            fallback_reason=fallback_reason,
            model_version=model_version,
            prompt_version=prompt_version,
            parameter_version=parameter_version,
            rationale=rationale,
            evidence_refs=tuple(evidence_refs),
            constraints_applied=tuple(constraints_applied),
            order_id=order_id,
            fill_id=fill_id,
            output_hash=output_hash,
            summary=summary,
            raw=safe_raw,
        )
        self._events.append(event)
        if self._sink is not None:
            self._sink(event)
        return event

    def input_snapshot(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> LogEvent:
        return self.append(
            LogEventType.INPUT_SNAPSHOT,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=canonical_hash(payload),
            raw={"payload": dict(payload)},
            **kwargs,
        )

    def agent_output(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        output: Mapping[str, Any],
        inputs_hash: str,
        **kwargs: Any,
    ) -> LogEvent:
        metadata = dict(kwargs)
        metadata.setdefault(
            "rationale", str(output["rationale"]) if output.get("rationale") else None
        )
        metadata.setdefault("evidence_refs", _string_tuple(output.get("evidence_refs")))
        metadata.setdefault(
            "constraints_applied", _string_tuple(output.get("constraints_applied"))
        )
        return self.append(
            LogEventType.AGENT_OUTPUT,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=inputs_hash,
            output_hash=canonical_hash(output),
            raw=dict(output),
            **metadata,
        )

    def validation(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        inputs_hash: str,
        **kwargs: Any,
    ) -> LogEvent:
        return self.append(
            LogEventType.VALIDATION,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=inputs_hash,
            **kwargs,
        )

    def decision(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        inputs_hash: str,
        output: Mapping[str, Any],
        **kwargs: Any,
    ) -> LogEvent:
        return self.append(
            LogEventType.DECISION,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=inputs_hash,
            raw=dict(output),
            **kwargs,
        )

    def order(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        inputs_hash: str,
        order_id: str,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> LogEvent:
        return self.append(
            LogEventType.ORDER,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=inputs_hash,
            order_id=order_id,
            raw=dict(payload),
            **kwargs,
        )

    def fill(
        self,
        *,
        run_id: str,
        trace_id: str,
        employee_profile: str,
        inputs_hash: str,
        fill_id: str,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> LogEvent:
        return self.append(
            LogEventType.FILL,
            run_id=run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            inputs_hash=inputs_hash,
            fill_id=fill_id,
            raw=dict(payload),
            **kwargs,
        )

    def events_for_run(self, run_id: str) -> tuple[LogEvent, ...]:
        return tuple(event for event in self._events if event.run_id == run_id)

    def to_jsonl(self, run_id: str | None = None) -> str:
        events = self._events if run_id is None else self.events_for_run(run_id)
        return "\n".join(
            json.dumps(asdict(event), sort_keys=True, default=str) for event in events
        )

    def replay(
        self, run_id: str, executor: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> ReplayReport:
        events = self.events_for_run(run_id)
        input_event = next(
            (
                event
                for event in events
                if event.event_type is LogEventType.INPUT_SNAPSHOT
            ),
            None,
        )
        output_event = next(
            (
                event
                for event in reversed(events)
                if event.event_type is LogEventType.AGENT_OUTPUT
            ),
            None,
        )
        decision_event = next(
            (
                event
                for event in reversed(events)
                if event.event_type is LogEventType.DECISION
            ),
            None,
        )
        if input_event is None or output_event is None:
            return ReplayReport(
                run_id, False, False, False, False, ("replay_inputs_or_output_missing",)
            )
        payload = input_event.raw.get("payload")
        if not isinstance(payload, Mapping):
            return ReplayReport(
                run_id, False, False, False, False, ("replay_payload_missing",)
            )
        replayed = dict(executor(payload))
        diffs: list[str] = []
        input_hash_match = canonical_hash(payload) == input_event.inputs_hash
        output_match = canonical_hash(replayed) == output_event.output_hash
        version_match = (
            len(
                {
                    (event.model_version, event.prompt_version, event.parameter_version)
                    for event in events
                    if event.event_type is LogEventType.AGENT_OUTPUT
                }
            )
            <= 1
        )
        decision_match = decision_event is None or replayed.get(
            "decision", replayed.get("verdict")
        ) == decision_event.raw.get("decision", decision_event.raw.get("verdict"))
        if not input_hash_match:
            diffs.append("inputs_hash")
        if not version_match:
            diffs.append("version_lineage")
        if not output_match:
            diffs.append("agent_output")
        if not decision_match:
            diffs.append("decision")
        return ReplayReport(
            run_id,
            input_hash_match,
            version_match,
            output_match,
            decision_match,
            tuple(diffs),
        )

    def review(self, run_id: str | None = None) -> dict[str, Any]:
        events = self._events if run_id is None else list(self.events_for_run(run_id))
        validations = [
            event for event in events if event.event_type is LogEventType.VALIDATION
        ]
        decisions = [
            event for event in events if event.event_type is LogEventType.DECISION
        ]
        fallbacks = [event for event in decisions if event.fallback_reason]
        failed_rules = Counter(
            event.failed_rule for event in validations if event.failed_rule
        )
        return {
            "run_count": len({event.run_id for event in events}),
            "event_count": len(events),
            "validation_failure_count": sum(
                event.domain_valid is False or event.schema_valid is False
                for event in validations
            ),
            "fallback_rate": round(len(fallbacks) / len(decisions), 6)
            if decisions
            else 0.0,
            "fallback_reasons": dict(failed_rules.most_common()),
            "replay_ready": bool(events)
            and any(
                event.event_type is LogEventType.INPUT_SNAPSHOT for event in events
            ),
        }
