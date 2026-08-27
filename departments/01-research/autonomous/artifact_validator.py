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
            dimensions=dict(payload.get("dimensions") or {}),
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
            data_requirements=tuple(payload.get("data_requirements") or ()),
            splits=tuple(payload.get("splits") or ()),
            cost_model=str(payload.get("cost_model") or ""),
            seed=payload.get("seed"),
            signature=dict(payload.get("signature") or {}),
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


__all__ = ["ingest_result", "sync_agent_artifacts"]
