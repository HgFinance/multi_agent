"""Redacted LangSmith observation for dispatcher-owned Hermes workers.

The central Kanban dispatcher, rather than the department container, starts
the real Hermes worker.  This small boundary therefore owns the only reliable
place to attach the Kanban task identity to the worker process.  It deliberately
uses the LangSmith batch HTTP endpoint instead of importing the optional SDK:
the Hermes image does not ship that SDK, and installing it into the agent image
would widen the runtime surface.

Only bounded metadata is sent.  Task bodies, prompts, answers, tool arguments,
tool results, credentials, and log text never leave this process.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

ACCOUNTING_PROFILE = "accounting-portfolio-department"
QA_PROFILE = "qa-department"
# Keep one dispatcher-owned registry for every active non-CEO profile.  The
# CEO root has its own lifecycle trace, while all department workers use the
# same redacted worker/model/tool publisher below.
_PROFILE_SPECS = {
    ACCOUNTING_PROFILE: {
        "department": "accounting-portfolio",
        "schema_version": "llm.accounting-worker.v1",
        "name_prefix": "hgfinance.accounting",
    },
    QA_PROFILE: {
        "department": "qa",
        "schema_version": "llm.qa-worker.v1",
        "name_prefix": "hgfinance.qa",
    },
    "research-department": {
        "department": "research",
        "schema_version": "llm.research-worker.v1",
        "name_prefix": "hgfinance.research",
    },
    "research-liaison": {
        "department": "research",
        "schema_version": "llm.research-liaison-worker.v1",
        "name_prefix": "hgfinance.research-liaison",
    },
    "quant-backtest-department": {
        "department": "quant-backtest",
        "schema_version": "llm.quant-backtest-worker.v1",
        "name_prefix": "hgfinance.quant-backtest",
    },
    "quant-liaison": {
        "department": "quant-backtest",
        "schema_version": "llm.quant-liaison-worker.v1",
        "name_prefix": "hgfinance.quant-liaison",
    },
    "trading-department": {
        "department": "trading",
        "schema_version": "llm.trading-worker.v1",
        "name_prefix": "hgfinance.trading",
    },
    "risk-management": {
        "department": "risk",
        "schema_version": "llm.risk-worker.v1",
        "name_prefix": "hgfinance.risk",
    },
    "hr-department": {
        "department": "hr",
        "schema_version": "llm.hr-worker.v1",
        "name_prefix": "hgfinance.hr",
    },
}
_PROFILE_RE = re.compile(r"(?:^|\s)-p\s+(?P<profile>[A-Za-z0-9._-]+)")
_MODEL_RE = re.compile(r"^\s*default:\s*([^#\s]+)", re.MULTILINE)
_PROVIDER_RE = re.compile(r"^\s*provider:\s*([^#\s]+)", re.MULTILINE)
_TOOL_RE = re.compile(r"⚡\s+(?P<name>[A-Za-z0-9_.-]+)")
_TOOL_DURATION_RE = re.compile(
    r"⚡\s+(?P<name>[A-Za-z0-9_.-]+)\s+(?P<duration>\d+(?:\.\d+)?)s\b"
)
_TOOL_SUMMARY_RE = re.compile(r"(?:\(|,|\s)(?P<count>\d+)\s+tool calls?\b", re.IGNORECASE)
_TOOL_ERROR_RE = re.compile(
    r"(?:\bTool\s+[A-Za-z0-9_.-]+\s+returned\s+error\b|"
    r"\breturned_error\s*=|\btool[_\s-]*error\b|"
    r"\btool[_\s-]*(?:blocked|failed)\b)",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(r"Reasoning", re.IGNORECASE)
_ROOT_RE = re.compile(r"(?:workflow_root_task_id|root_task_id)=(?P<id>t_[A-Za-z0-9_-]+)")
_REQUEST_RE = re.compile(
    r"(?:^|\n)(?:request_id|discord_request_id)=(?P<id>[^\s\n]+)",
    re.IGNORECASE,
)
_TASK_RE = re.compile(r"\bt_[A-Za-z0-9_-]+\b")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _enabled(env: Mapping[str, str]) -> bool:
    return (
        str(env.get("LANGSMITH_TRACING", "")).casefold() in {"1", "true", "yes", "on"}
        and bool(str(env.get("LANGSMITH_API_KEY", "")).strip())
    )


def _safe_id(value: Any, *, limit: int = 160) -> str:
    return _SAFE_ID_RE.sub("_", str(value or "").strip())[:limit]


def _uuid(seed: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"hgfinance:department-worker:{seed}")


def _ls_time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )


def _profile_from_argv(argv: Sequence[str]) -> str:
    for index, value in enumerate(argv):
        if value in {"-p", "--profile"} and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
    match = _PROFILE_RE.search(" ".join(str(value) for value in argv))
    return match.group("profile") if match else ""


def _model_info(*, profile: str, env: Mapping[str, str], argv: Sequence[str]) -> tuple[str, str]:
    for index, value in enumerate(argv):
        if value in {"--model", "-m"} and index + 1 < len(argv):
            return "", _safe_id(argv[index + 1])

    home = Path(str(env.get("HERMES_HOME", "/opt/data")))
    candidates = (
        home / "profiles" / profile / "config.yaml",
        home / "config.yaml",
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        model = _MODEL_RE.search(text)
        provider = _PROVIDER_RE.search(text)
        if model:
            return (
                _safe_id(provider.group(1) if provider else ""),
                _safe_id(model.group(1)),
            )
    return "", "unknown"


def _log_path(*, task_id: str, env: Mapping[str, str]) -> Path:
    kanban_home = Path(str(env.get("HERMES_KANBAN_HOME", "/opt/kanban")))
    return kanban_home / "kanban" / "logs" / f"{task_id}.log"


def _observed_tools(log_text: str) -> tuple[list[str], int | None]:
    names: list[str] = []
    for match in _TOOL_RE.finditer(log_text):
        name = _safe_id(match.group("name"), limit=80)
        if name and name not in names:
            names.append(name)
    summary = _TOOL_SUMMARY_RE.search(log_text)
    tool_count = int(summary.group("count")) if summary else None
    if tool_count is None and names:
        tool_count = len(names)
    return names[:32], tool_count


def _observed_tool_stats(log_text: str) -> dict[str, tuple[int, int]]:
    """Return ``tool_name -> (call_count, total_duration_ms)`` from Hermes logs."""

    stats: dict[str, tuple[int, int]] = {}
    for match in _TOOL_DURATION_RE.finditer(log_text):
        name = _safe_id(match.group("name"), limit=80)
        if not name:
            continue
        try:
            duration_ms = max(0, int(float(match.group("duration")) * 1000))
        except (TypeError, ValueError):
            duration_ms = 0
        count, total = stats.get(name, (0, 0))
        stats[name] = (count + 1, total + duration_ms)
    return dict(list(stats.items())[:32])


def _observed_tool_error_count(log_text: str) -> int:
    """Count tool failures without copying the tool result or log text."""

    return min(len(_TOOL_ERROR_RE.findall(log_text)), 100)


def _task_attempt_count(
    db_path: str | os.PathLike[str] | None,
    task_id: str,
) -> int:
    """Read the bounded attempt count without changing the Kanban board."""

    if not db_path or not task_id:
        return 1
    db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        with sqlite3.connect(db_uri, uri=True, timeout=1.0) as conn:
            row = conn.execute(
                "SELECT count(*) FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        return max(1, min(int(row[0] if row else 1), 100))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 1


def worker_log_metrics(
    *, task_id: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read redacted timing counters from one persisted Hermes task log.

    This is the read half of the same worker-boundary contract used by
    ``publish_department_worker_trace``.  It returns labels and durations
    only; prompts, answers, arguments, and log lines never leave the helper.
    """

    runtime_env = env or os.environ
    try:
        log_text = _log_path(
            task_id=_safe_id(task_id, limit=160), env=runtime_env
        ).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    tool_names, tool_count = _observed_tools(log_text)
    tool_stats = _observed_tool_stats(log_text)
    tool_duration_total_ms = sum(total for _count, total in tool_stats.values())
    return {
        "tool_names": tool_names,
        "tool_calls": tool_count,
        "tool_duration_total_ms": tool_duration_total_ms,
        "tool_latency_available": tool_duration_total_ms > 0,
        "tool_timing_source": (
            "hermes-log-duration" if tool_duration_total_ms > 0 else "unavailable"
        ),
        "tool_error_count": _observed_tool_error_count(log_text),
        "llm_calls": len(_REASONING_RE.findall(log_text)) or None,
    }


def _root_id(*, task_id: str, task_body: str) -> str:
    match = _ROOT_RE.search(task_body)
    if match:
        return match.group("id")
    # The direct CEO root itself has no workflow_root marker.
    return task_id if _TASK_RE.fullmatch(task_id) else _safe_id(task_id)


def _request_id(*, root_id: str, task_body: str) -> str:
    match = _REQUEST_RE.search(task_body)
    return _safe_id(match.group("id"), limit=160) if match else root_id


def _status(*, task_status: str, return_code: int) -> tuple[str, str | None]:
    normalized = str(task_status or "").casefold()
    if normalized in {"done", "completed", "archived"} and return_code == 0:
        return "completed", None
    if normalized in {"blocked", "gave_up", "timed_out", "crashed", "failed"}:
        return normalized, f"kanban_{normalized}"
    if return_code < 0:
        return "failed", f"worker_signal_{abs(int(return_code))}"
    if return_code != 0:
        return "failed", f"worker_exit_{abs(int(return_code))}"
    return normalized or "completed", None


def _metadata(
    *,
    task_id: str,
    root_id: str,
    request_id: str,
    run_id: str,
    profile: str,
    provider: str,
    model: str,
    status: str,
    error_code: str | None,
    started_ms: int,
    ended_ms: int,
    tool_names: Sequence[str],
    tool_count: int | None,
    llm_turn_count: int,
    observation_unit: str,
    return_code: int,
    profile_spec: Mapping[str, str],
    trace_id: str,
    attempts: int,
    tool_duration_total_ms: int,
    tool_error_count: int,
    tool_latency_available: bool,
) -> dict[str, Any]:
    return {
        "schema_version": profile_spec["schema_version"],
        "trace_kind": "department_worker",
        "observation_unit": observation_unit,
        "source": "kanban-dispatcher-worker-boundary",
        "department": profile_spec["department"],
        "profile": profile,
        "task_id": task_id,
        "request_id": request_id,
        "root_id": root_id,
        "trace_id": trace_id,
        "workflow_root_task_id": root_id,
        "kanban_run_id": run_id,
        "status": status,
        "error_code": error_code,
        "provider": provider,
        "model_name": model,
        "tool_names": list(tool_names),
        "tool_call_count": tool_count,
        "tool_calls": tool_count,
        "tool_duration_total_ms": max(0, int(tool_duration_total_ms)),
        "tool_error_count": max(0, int(tool_error_count)),
        "llm_turn_count_observed": llm_turn_count,
        "llm_calls": max(0, int(llm_turn_count)),
        "attempts": max(1, int(attempts)),
        "retries": max(0, int(attempts) - 1),
        "started_at_ms": int(started_ms),
        "completed_at_ms": int(ended_ms),
        "latency_ms": max(int(ended_ms) - int(started_ms), 0),
        "latency_scope": "worker_execution",
        "latency_available": True,
        "tool_latency_available": bool(tool_latency_available),
        "tool_timing_source": (
            "hermes-log-duration" if tool_latency_available else "unavailable"
        ),
        "telemetry_completeness": (
            "runtime-and-boundary"
            if tool_names or llm_turn_count
            else "boundary-only"
        ),
        "return_code": int(return_code),
        "raw_payloads_sent": False,
    }


def _run_payload(
    *,
    run_uuid: UUID,
    trace_uuid: UUID,
    dotted_order: str,
    name: str,
    run_type: str,
    started_ms: int,
    ended_ms: int,
    metadata: Mapping[str, Any],
    project_name: str,
    parent_run_id: UUID | None = None,
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(run_uuid),
        "trace_id": str(trace_uuid),
        "dotted_order": dotted_order,
        "name": name,
        "run_type": run_type,
        "session_name": project_name,
        "inputs": dict(inputs or {}),
        "outputs": dict(outputs or {}),
        "start_time": int(started_ms),
        "end_time": int(ended_ms),
        "extra": {"metadata": dict(metadata)},
        "tags": [
            "hgfinance",
            str(metadata.get("department") or "department"),
            "redacted",
            "worker",
        ],
    }
    if parent_run_id is not None:
        payload["parent_run_id"] = str(parent_run_id)
    error_code = metadata.get("error_code")
    if error_code:
        # LangSmith derives the run status from this field. Keep the value to
        # a bounded internal code; raw worker output and task payloads never
        # enter the failure trace.
        payload["error"] = _safe_id(error_code, limit=80)
    return payload


def _post_batch(*, env: Mapping[str, str], runs: list[dict[str, Any]]) -> bool:
    if not runs or not _enabled(env):
        return False
    endpoint = str(env.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")).rstrip("/")
    body = json.dumps({"post": runs, "patch": []}, separators=(",", ":")).encode()
    request = urllib.request.Request(
        f"{endpoint}/runs/batch",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": str(env.get("LANGSMITH_API_KEY", "")),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        # Observability is fail-open. Never change the worker's business result.
        return False


def publish_department_worker_trace(
    *,
    task_id: str,
    task_body: str,
    task_status: str,
    run_id: str,
    return_code: int,
    started_ms: int,
    ended_ms: int,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> bool:
    """Publish one task-correlated worker/model/tool trace tree.

    The dispatcher calls this after the Hermes child exits.  It is deliberately
    one bounded batch request, so it cannot add one network round trip per tool
    or expose QA as a response dependency.
    """

    runtime_env = env or os.environ
    profile = _profile_from_argv(argv) or str(runtime_env.get("HERMES_PROFILE", ""))
    profile_spec = _PROFILE_SPECS.get(profile)
    if profile_spec is None:
        return False

    root_id = _root_id(task_id=task_id, task_body=task_body)
    request_id = _request_id(root_id=root_id, task_body=task_body)
    provider, model = _model_info(profile=profile, env=runtime_env, argv=argv)
    try:
        log_text = _log_path(task_id=task_id, env=runtime_env).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    tool_names, tool_count = _observed_tools(log_text)
    tool_stats = _observed_tool_stats(log_text)
    attempts = _task_attempt_count(runtime_env.get("HERMES_KANBAN_DB"), task_id)
    tool_duration_total_ms = sum(total for _count, total in tool_stats.values())
    tool_error_count = _observed_tool_error_count(log_text)
    tool_latency_available = tool_duration_total_ms > 0
    llm_turn_count = len(_REASONING_RE.findall(log_text))
    if llm_turn_count == 0 and return_code == 0:
        # The Hermes log may omit reasoning blocks in quiet mode. One completed
        # worker still proves that at least one model turn was executed.
        llm_turn_count = 1
    status, error_code = _status(task_status=task_status, return_code=return_code)
    base = _safe_id(f"{profile}:{root_id}:{task_id}:{run_id}")
    # This worker trace is intentionally task/attempt scoped. The CEO root
    # has its own lifecycle metric; the shared Kanban root/task metadata below
    # is the durable join key between the two planes. Making the worker run
    # itself the LangSmith trace root keeps dotted_order valid without inventing
    # a second synthetic root run.
    worker_uuid = _uuid(f"worker:{base}")
    trace_uuid = worker_uuid
    model_uuid = _uuid(f"model:{base}")
    stamp = _ls_time(started_ms)
    # LangSmith validates that the last dotted-order component equals the
    # current run ID. The trace ID is the stable workflow grouping key; the
    # worker ID is the root run's actual last component.
    worker_dotted = f"{stamp}{worker_uuid}"
    project_name = _safe_id(runtime_env.get("LANGSMITH_PROJECT", "First")) or "First"
    worker_metadata = _metadata(
        task_id=task_id,
        root_id=root_id,
        request_id=request_id,
        run_id=run_id,
        profile=profile,
        provider=provider,
        model=model,
        status=status,
        error_code=error_code,
        started_ms=started_ms,
        ended_ms=ended_ms,
        tool_names=tool_names,
        tool_count=tool_count,
        llm_turn_count=llm_turn_count,
        observation_unit="worker",
        return_code=return_code,
        profile_spec=profile_spec,
        trace_id=str(trace_uuid),
        attempts=attempts,
        tool_duration_total_ms=tool_duration_total_ms,
        tool_error_count=tool_error_count,
        tool_latency_available=tool_latency_available,
    )
    safe_inputs = {
        "task_id": task_id,
        "workflow_root_task_id": root_id,
        "kanban_run_id": run_id,
        "profile": profile,
        "task_body_present": bool(str(task_body).strip()),
        "task_body_length": len(str(task_body)),
        "request_id": request_id,
        "raw_payloads_sent": False,
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "llm_calls": llm_turn_count,
        "tool_calls": tool_count,
        "tool_error_count": tool_error_count,
        "tool_duration_total_ms": tool_duration_total_ms,
        "tool_latency_available": tool_latency_available,
        "tool_timing_source": (
            "hermes-log-duration" if tool_latency_available else "unavailable"
        ),
        "telemetry_completeness": worker_metadata["telemetry_completeness"],
    }
    safe_outputs = {
        "status": status,
        "error_code": error_code,
        "return_code": int(return_code),
        "latency_ms": max(int(ended_ms) - int(started_ms), 0),
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "llm_calls": llm_turn_count,
        "tool_calls": tool_count,
        "tool_error_count": tool_error_count,
        "tool_duration_total_ms": tool_duration_total_ms,
        "tool_latency_available": tool_latency_available,
        "tool_timing_source": (
            "hermes-log-duration" if tool_latency_available else "unavailable"
        ),
        "telemetry_completeness": worker_metadata["telemetry_completeness"],
        "raw_payloads_sent": False,
    }
    worker_latency_ms = max(int(ended_ms) - int(started_ms), 0)
    model_latency_ms = (
        max(0, worker_latency_ms - tool_duration_total_ms)
        if tool_latency_available
        else None
    )
    model_metadata = {
        **worker_metadata,
        "observation_unit": "model",
        "model_call_count_observed": llm_turn_count,
        "model_latency_ms": model_latency_ms,
        "latency_ms": model_latency_ms,
        "latency_scope": (
            "model_estimate" if model_latency_ms is not None else "unavailable"
        ),
        "latency_available": model_latency_ms is not None,
    }
    model_outputs = {
        **safe_outputs,
        "latency_ms": model_latency_ms,
        "latency_scope": model_metadata["latency_scope"],
        "latency_available": model_metadata["latency_available"],
    }
    runs = [
        _run_payload(
            run_uuid=worker_uuid,
            trace_uuid=trace_uuid,
            dotted_order=worker_dotted,
            name=f"{profile_spec['name_prefix']}.worker",
            run_type="chain",
            started_ms=started_ms,
            ended_ms=ended_ms,
            metadata=worker_metadata,
            project_name=project_name,
            inputs=safe_inputs,
            outputs=safe_outputs,
        ),
        _run_payload(
            run_uuid=model_uuid,
            trace_uuid=trace_uuid,
            dotted_order=f"{worker_dotted}.{_ls_time(started_ms)}{model_uuid}",
            name=f"{profile_spec['name_prefix']}.llm",
            run_type="llm",
            started_ms=started_ms,
            ended_ms=ended_ms,
            metadata=model_metadata,
            project_name=project_name,
            parent_run_id=worker_uuid,
            inputs=safe_inputs,
            outputs=model_outputs,
        ),
    ]
    tool_cursor_ms = int(started_ms)
    for index, tool_name in enumerate(tool_names):
        tool_uuid = _uuid(f"tool:{base}:{index}:{tool_name}")
        tool_count_for_name, tool_duration_ms = tool_stats.get(tool_name, (1, 0))
        tool_start_ms = tool_cursor_ms
        tool_end_ms = min(
            int(ended_ms),
            tool_start_ms + max(0, int(tool_duration_ms)),
        )
        tool_cursor_ms = tool_end_ms
        runs.append(
            _run_payload(
                run_uuid=tool_uuid,
                trace_uuid=trace_uuid,
                dotted_order=f"{worker_dotted}.{_ls_time(started_ms)}{tool_uuid}",
                name=f"{profile_spec['name_prefix']}.tool.{tool_name}",
                run_type="tool",
                started_ms=tool_start_ms,
                ended_ms=tool_end_ms,
                metadata={
                    **worker_metadata,
                    "observation_unit": "tool",
                    "tool_name": tool_name,
                    "tool_call_index": index,
                    "tool_call_count": tool_count_for_name,
                    "latency_ms": tool_duration_ms if tool_duration_ms > 0 else None,
                    "latency_scope": (
                        "tool_observation" if tool_duration_ms > 0 else "unavailable"
                    ),
                    "latency_available": tool_duration_ms > 0,
                    "tool_latency_ms": tool_duration_ms,
                    "tool_latency_available": tool_duration_ms > 0,
                    "tool_timing_source": (
                        "hermes-log-duration" if tool_duration_ms > 0 else "unavailable"
                    ),
                },
                project_name=project_name,
                parent_run_id=worker_uuid,
                inputs=safe_inputs,
                outputs={
                    **safe_outputs,
                    "latency_ms": (
                        tool_duration_ms if tool_duration_ms > 0 else None
                    ),
                    "latency_scope": (
                        "tool_observation" if tool_duration_ms > 0 else "unavailable"
                    ),
                    "latency_available": tool_duration_ms > 0,
                },
            )
        )
    return _post_batch(env=runtime_env, runs=runs)


def publish_accounting_worker_trace(**kwargs: Any) -> bool:
    """Backward-compatible Accounting entry point."""

    return publish_department_worker_trace(**kwargs)


__all__ = [
    "ACCOUNTING_PROFILE",
    "QA_PROFILE",
    "publish_accounting_worker_trace",
    "publish_department_worker_trace",
    "worker_log_metrics",
]
