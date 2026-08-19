"""Shared, loss-averse helpers for terminal task projections.

이 모듈은 Hermes task의 durable marker와 최신 run metadata만 읽는다.
사용자에게 공개되는 projection에는 reasoning/chain-of-thought를 넣지 않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

# 패턴은 ceo_workflow_scope.read_marker 가 소유한다 - 여기서 다시 적지 않는다
# (2026-08-14: 같은 마커를 5곳에서 4가지 패턴으로 읽고 있었다).
from orchestration.ceo_workflow_scope import (
    BACKGROUND_RESEARCH_ROLE,
    CONTINUOUS_RESEARCH_MARKER,
    CONTINUOUS_RESEARCH_PLANE,
    read_marker,
)


class _Match:
    """read_marker 결과를 기존 `match.group(1)` 호출부와 맞춰 주는 얇은 껍데기."""

    def __init__(self, value: str) -> None:
        self._value = value

    def group(self, _index: int = 1) -> str:
        return self._value


def _search(body: object, key: str):
    value = read_marker(str(body or ""), key)
    return _Match(value) if value else None


_ROOT_KEY = "workflow_root_task_id"
_ROLE_KEY = "workflow_role"
_ACTION_RE = re.compile(r"(?m)^(?:action|workflow_action)=(\S+)\s*$")
_SUPERVISOR_MARKER = "hgfinance.ceo-supervisor.v1"
_SUPERVISOR_LINE_RE = re.compile(
    rf"^{re.escape(_SUPERVISOR_MARKER)}(?:\s+(?P<fields>.*))?$"
)
_METADATA_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$")
_MODE_KEY = "workflow_mode"
_PLANE_KEY = "workflow_plane"
_FORBIDDEN_KEYS = {
    "chain_of_thought",
    "cot",
    "hidden_reasoning",
    "reasoning",
    "thought",
    "thoughts",
    "scratchpad",
}


def as_mapping(value: Any) -> dict[str, Any]:
    """Decode the JSON-shaped metadata returned by different Hermes versions."""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or "")


def task_body(task: Mapping[str, Any]) -> str:
    return str(task.get("body") or "")


def workflow_root(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _ROOT_KEY)
    return match.group(1).strip() if match else None


def workflow_role(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _ROLE_KEY)
    return match.group(1).strip().casefold() if match else None


def _supervisor_marker_fields(task: Mapping[str, Any]) -> dict[str, str] | None:
    """Parse the exact durable supervisor marker, if present."""

    for raw_line in task_body(task).splitlines():
        match = _SUPERVISOR_LINE_RE.fullmatch(raw_line.strip())
        if match is None:
            continue
        fields_text = match.group("fields") or ""
        if not fields_text:
            # A bare supervisor marker is malformed; fail closed.
            return {}
        tokens = fields_text.split()
        if not all(_METADATA_FIELD_RE.fullmatch(token) for token in tokens):
            return {}
        return {
            key: value
            for key, value in (token.split("=", 1) for token in tokens)
        }
    return None


def action(task: Mapping[str, Any]) -> str | None:
    marker_fields = _supervisor_marker_fields(task)
    if marker_fields is not None:
        value = marker_fields.get("action") or marker_fields.get("workflow_action")
        return value.strip().upper() if value else None
    match = _ACTION_RE.search(task_body(task))
    return match.group(1).strip().upper() if match else None


def workflow_mode(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _MODE_KEY)
    return match.group(1).strip().casefold() if match else None


def workflow_plane(task: Mapping[str, Any]) -> str | None:
    match = _search(task_body(task), _PLANE_KEY)
    return match.group(1).strip().casefold() if match else None


def is_background_research(task: Mapping[str, Any]) -> bool:
    """Return whether a task belongs to the independent research plane.

    Continuous research deliberately has no request-workflow dependency.  The
    marker is checked before assignee/profile validation so a future
    research-intelligence profile cannot leak into a CEO workflow projection.
    """

    body = task_body(task)
    return (
        CONTINUOUS_RESEARCH_MARKER in body
        or workflow_plane(task) == CONTINUOUS_RESEARCH_PLANE
        or workflow_role(task) == BACKGROUND_RESEARCH_ROLE
    )


def is_request_scoped_role(
    task: Mapping[str, Any], root_task_id: str, role: str
) -> bool:
    """Match only a current-root task with the requested workflow role."""

    return (
        not is_background_research(task)
        and workflow_root(task) == root_task_id
        and workflow_role(task) == role.casefold()
    )


def merged_run_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    """Prefer durable task-run metadata over ingress-only fields."""

    merged: dict[str, Any] = {}
    for key in ("metadata", "task_run_metadata", "run_metadata"):
        merged.update(as_mapping(task.get(key)))
    task_run = task.get("task_run")
    if isinstance(task_run, Mapping):
        merged.update(as_mapping(task_run.get("metadata", task_run)))
    merged.update(as_mapping(merged.get("workflow_metadata")))
    runs = task.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        for run in runs:
            if isinstance(run, Mapping):
                run_metadata = as_mapping(run.get("metadata"))
                merged.update(run_metadata)
                merged.update(as_mapping(run_metadata.get("workflow_metadata")))
    return merged


def terminal_success(task: Mapping[str, Any]) -> bool:
    status = str(task.get("status") or "").casefold()
    outcome = str(task.get("outcome") or "").casefold()
    runs = task.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        for run in runs:
            if isinstance(run, Mapping):
                candidate = str(run.get("outcome") or run.get("status") or "").casefold()
                if candidate:
                    outcome = candidate
    return status in {"done", "completed"} and outcome not in {
        "failed",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("summary", "result", "final_answer", "message"):
            if value.get(key):
                return str(value[key])
    return str(value)


def summary(task: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    metadata = metadata or {}
    for key in (
        "final_answer",
        "synthesis_summary",
        "final_summary",
        "summary",
        "result",
    ):
        if metadata.get(key):
            return text_value(metadata[key])
    for key in ("latest_summary", "summary", "result"):
        if task.get(key):
            return text_value(task[key])
    return ""


def ids_from(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return (value,) if value else ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("id") or item.get("task_id")
        if item:
            result.append(str(item))
    return tuple(dict.fromkeys(result))


def safe_json(value: Any) -> Any:
    """Remove hidden reasoning recursively while retaining audit evidence."""

    if isinstance(value, Mapping):
        return {
            str(key): safe_json(item)
            for key, item in value.items()
            if str(key).casefold() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe_json(item) for item in value]
    return value


def iso_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "BACKGROUND_RESEARCH_ROLE",
    "CONTINUOUS_RESEARCH_MARKER",
    "CONTINUOUS_RESEARCH_PLANE",
    "action",
    "as_mapping",
    "ids_from",
    "is_background_research",
    "is_request_scoped_role",
    "iso_timestamp",
    "merged_run_metadata",
    "safe_json",
    "summary",
    "task_id",
    "terminal_success",
    "workflow_mode",
    "workflow_plane",
    "workflow_role",
    "workflow_root",
]
