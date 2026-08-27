"""Mechanical validation of artifacts authored by Strategy Hermes.

This module does not choose hypotheses or plans. It only normalizes files
already written by Hermes, appends immutable lab events, and applies the
evidence gates to result artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lab import ResearchLab
from models import ExperimentPlan, Hypothesis
from result import candidate_report, decision_for, parse_result


def sync_agent_artifacts(lab: ResearchLab) -> list[dict[str, Any]]:
    """Register new Hermes artifacts and return mechanical decisions."""

    _sync_hypotheses(lab)
    _sync_plans(lab)
    decisions: list[dict[str, Any]] = []
    for path in sorted(lab.results_dir.glob("*.json")):
        payload = _read_object(path)
        if any(
            event.get("event_type") == "EXPERIMENT_RESULT"
            and (event.get("payload") or {}).get("plan_id") == payload.get("plan_id")
            for event in lab.events()
        ):
            continue
        decisions.append(ingest_result(lab, path))
    return decisions


def ingest_result(lab: ResearchLab, path: Path) -> dict[str, Any]:
    payload = _read_object(path)
    plan = next((item for item in lab.plans() if item.get("plan_id") == payload.get("plan_id")), None)
    if plan is None:
        raise ValueError("result does not reference a registered experiment plan")
    result = parse_result(
        payload,
        expected_plan_id=plan.get("plan_id"),
        expected_preregistration_hash=plan.get("preregistration_hash"),
    )
    lab.record_result(result)
    decision, rationale = decision_for(result)
    lab.append_event("DECISION", {"plan_id": result.plan_id, "decision": decision, "rationale": rationale})
    if decision == "CANDIDATE":
        plan_payload = next((item for item in lab.plans() if item.get("plan_id") == result.plan_id), {})
        hypothesis_id = plan_payload.get("hypothesis_id")
        hypothesis = next(
            (item for item in _read_objects(lab.hypotheses_dir) if item.get("hypothesis_id") == hypothesis_id),
            {},
        )
        report = candidate_report(result, hypothesis=hypothesis, plan=plan_payload)
        lab._write_json(lab.root / "candidate.json", report)
        lab.append_event("CANDIDATE_PUBLISHED", report)
    return {"status": "RECORDED", "plan_id": result.plan_id, "decision": decision, "rationale": rationale}


def _sync_hypotheses(lab: ResearchLab) -> None:
    event_ids = {
        (event.get("payload") or {}).get("hypothesis_id")
        for event in lab.events()
        if event.get("event_type") == "HYPOTHESIS_CREATED"
    }
    for path in sorted(lab.hypotheses_dir.glob("*.json")):
        payload = _read_object(path)
        hypothesis_id = str(payload.get("hypothesis_id") or "")
        if hypothesis_id in event_ids:
            continue
        hypothesis = Hypothesis(
            hypothesis_id=hypothesis_id,
            statement=str(payload.get("statement") or ""),
            mechanism=str(payload.get("mechanism") or ""),
            expected_behavior=str(payload.get("expected_behavior") or ""),
            falsifiers=tuple(payload.get("falsifiers") or ()),
            dimensions=_mapping(payload.get("dimensions"), field_name="dimensions"),
            parent_id=payload.get("parent_id"),
            role=str(payload.get("role") or "explore"),
            created_at=str(payload.get("created_at") or ""),
        )
        lab.record_hypothesis(hypothesis)


def _sync_plans(lab: ResearchLab) -> None:
    event_ids = {
        (event.get("payload") or {}).get("plan_id")
        for event in lab.events()
        if event.get("event_type") == "PLAN_CREATED"
    }
    for path in sorted(lab.plans_dir.glob("*.json")):
        payload = _read_object(path)
        plan_id = str(payload.get("plan_id") or "")
        if plan_id in event_ids:
            continue
        plan = ExperimentPlan(
            plan_id=plan_id,
            hypothesis_id=str(payload.get("hypothesis_id") or ""),
            objective=str(payload.get("objective") or ""),
            method=str(payload.get("method") or ""),
            data_requirements=_texts_or_json(payload.get("data_requirements"), field_name="data_requirements"),
            splits=_texts_or_json(payload.get("splits"), field_name="splits"),
            cost_model=_text_or_json(payload.get("cost_model")),
            # The prompt historically described this as an "integer seed"
            # and direct Hermes runs emitted ``integer_seed``.  Keep the
            # persisted model canonical (``seed``), while accepting that
            # unambiguous legacy spelling at the ingestion boundary.
            seed=_seed_value(payload),
            signature=_mapping(payload.get("signature"), field_name="signature"),
            preregistration_hash=str(payload.get("preregistration_hash") or ""),
            status=str(payload.get("status") or "PLANNED"),
            created_at=str(payload.get("created_at") or ""),
        )
        lab.record_plan(plan)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_objects(directory: Path) -> list[dict[str, Any]]:
    return [_read_object(path) for path in sorted(directory.glob("*.json"))]


def _mapping(value: object, *, field_name: str) -> dict[str, str]:
    """Accept the structured form and Hermes' compact string form.

    The persisted plan contract allows a signature to be human-readable.  The
    old Python director emitted a mapping, while a direct Hermes session may
    reasonably emit a single string.  Normalize both without dropping the
    artifact or letting a legacy shape crash the whole worker cycle.
    """

    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, str) and value.strip():
        return {field_name: value.strip()}
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return {f"{field_name}_{index}": item for index, item in enumerate(items)}
    return {}


def _texts_or_json(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True),)
    text = str(value).strip()
    return (text,) if text else ()


def _text_or_json(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").strip()


def _seed_value(payload: dict[str, Any]) -> object:
    seed = payload.get("seed")
    integer_seed = payload.get("integer_seed")
    if seed is not None and integer_seed is not None and seed != integer_seed:
        raise ValueError("seed and integer_seed do not match")
    return seed if seed is not None else integer_seed


__all__ = ["ingest_result", "sync_agent_artifacts"]
