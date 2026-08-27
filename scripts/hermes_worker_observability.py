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
import logging
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
# Keep one dispatcher-owned registry for every active worker profile. The CEO
# ingress keeps its lifecycle trace, while CEO synthesis and department
# workers use the same redacted worker/model/tool publisher below; those are
# separate observation units, not duplicate workflow roots.
_PROFILE_SPECS = {
    "ceo-agent": {
        "department": "ceo",
        "schema_version": "llm.ceo-worker.v1",
        "name_prefix": "hgfinance.ceo",
    },
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
_LOG = logging.getLogger("hgfinance.hermes_worker_observability")
_LANGSMITH_USAGE_LIMITED = False
_PROFILE_RE = re.compile(r"(?:^|\s)-p\s+(?P<profile>[A-Za-z0-9._-]+)")
_MODEL_RE = re.compile(r"^\s*default:\s*([^#\s]+)", re.MULTILINE)
_PROVIDER_RE = re.compile(r"^\s*provider:\s*([^#\s]+)", re.MULTILINE)
_TOOL_RE = re.compile(r"⚡\s+(?P<name>[A-Za-z0-9_.-]+)")
_TOOL_DURATION_RE = re.compile(
    r"⚡\s+(?P<name>[A-Za-z0-9_.-]+)\s+(?P<duration>\d+(?:\.\d+)?)s\b"
)
# Hermes renders terminal/file tools with an icon and a label instead of the
# ``⚡ tool`` form. Keep the parser line-scoped and read only the final duration;
# command text and file contents are never copied into metadata.
_TOOL_LOG_LINE_RE = re.compile(
    r"^\s*┊\s+(?P<icon>[^\w\s])?\s*"
    r"(?P<label>[A-Za-z][A-Za-z0-9_.-]*|\$)(?=\s).*?"
    r"(?P<duration>\d+(?:\.\d+)?)s\b[^\n]*$",
    re.MULTILINE,
)
_TOOL_SUMMARY_RE = re.compile(r"(?:\(|,|\s)(?P<count>\d+)\s+tool calls?\b", re.IGNORECASE)
_TOOL_ERROR_RE = re.compile(
    r"(?:\bTool\s+[A-Za-z0-9_.-]+\s+returned\s+error\b|"
    r"\breturned_error\s*=|\btool[_\s-]*error\b|"
    r"\btool[_\s-]*(?:blocked|failed)\b|"
    r"\bError\s+executing\s+tool\b|"
    r"\bMCP\s+call\s+failed\b)",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(r"Reasoning", re.IGNORECASE)
_ROOT_RE = re.compile(r"(?:workflow_root_task_id|root_task_id)=(?P<id>t_[A-Za-z0-9_-]+)")
_LANGSMITH_RUN_RE = re.compile(
    r"(?:^|\s)langsmith_trace_run_id=(?P<id>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:$|\s)",
    re.IGNORECASE,
)
_LANGSMITH_CONTEXT_RE = re.compile(
    r"(?:^|\s)langsmith_trace_context=(?P<context>"
    r"\d{8}T\d{12}Z[0-9a-f-]{36})(?:$|\s)",
    re.IGNORECASE,
)
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


def department_worker_trace_identity(
    *, task_id: str, task_body: str, profile: str, run_id: str, started_ms: int
) -> dict[str, str]:
    """Return the canonical worker IDs shared by all worker observations.

    The dispatcher worker is the sole trace owner. Other bounded observations
    (for example the Risk Hermes log profile) must use this identity as a
    parent instead of creating a second LangSmith root.
    """

    root_id = _root_id(task_id=task_id, task_body=task_body)
    base = _safe_id(f"{profile}:{root_id}:{task_id}:{run_id}")
    parent_run_id, parent_dotted_order = _langsmith_parent(task_body)
    worker_uuid = _uuid(f"worker:{base}")
    trace_uuid = parent_run_id or worker_uuid
    stamp = _ls_time(started_ms)
    worker_dotted_order = (
        f"{parent_dotted_order}.{stamp}{worker_uuid}"
        if parent_dotted_order
        else f"{stamp}{worker_uuid}"
    )
    return {
        "seed": base,
        "root_id": root_id,
        "worker_run_id": str(worker_uuid),
        "trace_id": str(trace_uuid),
        "worker_dotted_order": worker_dotted_order,
    }


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
    for match in _TOOL_LOG_LINE_RE.finditer(log_text):
        if match.group("icon") == "⚡":
            # The legacy regex above already accounts for this rendering.
            continue
        label = match.group("label")
        name = "terminal" if label == "$" else _safe_id(label, limit=80)
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
    for match in _TOOL_LOG_LINE_RE.finditer(log_text):
        if match.group("icon") == "⚡":
            # The legacy regex above already accounts for this rendering.
            continue
        label = match.group("label")
        name = "terminal" if label == "$" else _safe_id(label, limit=80)
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
    tool_names, _tool_count = _observed_tools(log_text)
    tool_stats = _observed_tool_stats(log_text)
    tool_duration_total_ms = sum(total for _count, total in tool_stats.values())
    return {
        "tool_names": tool_names,
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


def _langsmith_parent(task_body: str) -> tuple[UUID | None, str | None]:
    """Read the BFF root run identity from the bounded task scope marker."""

    body = str(task_body or "")
    run_match = _LANGSMITH_RUN_RE.search(body)
    context_match = _LANGSMITH_CONTEXT_RE.search(body)
    try:
        run_id = UUID(run_match.group("id")) if run_match else None
    except (AttributeError, ValueError):
        run_id = None
    context = context_match.group("context") if context_match else None
    if run_id is None and context:
        try:
            run_id = UUID(context[-36:])
        except ValueError:
            context = None
    return run_id, context


def _status(
    *, task_status: str, return_code: int, profile: str = ""
) -> tuple[str, str | None]:
    normalized = str(task_status or "").casefold()
    if normalized in {"done", "completed", "archived"} and return_code == 0:
        return "completed", None
    if normalized == "blocked" and profile == "risk-management":
        # A user-input block is a valid business terminal state for Risk. Do
        # not turn it into a LangSmith execution error; the block kind remains
        # visible in the task metadata and CEO/Notion projections.
        return "blocked", None
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
    workflow_mode: str,
    analysis_mode: str | None,
    configured_max_turns: int,
) -> dict[str, Any]:
    return {
        "schema_version": profile_spec["schema_version"],
        "trace_kind": "department_worker",
        "observation_unit": observation_unit,
        "source": "kanban-dispatcher-worker-boundary",
        "department": profile_spec["department"],
        "stage": profile_spec["department"],
        "workflow_mode": workflow_mode,
        "analysis_mode": analysis_mode,
        "configured_max_turns": max(0, int(configured_max_turns)),
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
        "actual_turns": max(0, int(llm_turn_count)),
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
    global _LANGSMITH_USAGE_LIMITED
    if not runs or not _enabled(env) or _LANGSMITH_USAGE_LIMITED:
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
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                body = exc.read(512).decode("utf-8", "replace").casefold()
            except (OSError, UnicodeError):
                body = ""
            if "usage limit" in body or "unique traces" in body:
                _LANGSMITH_USAGE_LIMITED = True
                _LOG.warning(
                    "langsmith-worker-trace-write-blocked reason=tenant_usage_limit"
                )
        return False
    except (OSError, urllib.error.URLError, ValueError):
        # Observability is fail-open. Never change the worker's business result.
        return False


def _tool_trace_mode(env: Mapping[str, str]) -> str:
    """Return the bounded child-tool trace policy for this worker boundary."""

    mode = str(env.get("LANGSMITH_TOOL_TRACE_MODE", "full")).strip().casefold()
    return mode if mode in {"full", "aggregate", "sample"} else "full"


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

    identity = department_worker_trace_identity(
        task_id=task_id,
        task_body=task_body,
        profile=profile,
        run_id=run_id,
        started_ms=started_ms,
    )
    root_id = identity["root_id"]
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
    tool_trace_mode = _tool_trace_mode(runtime_env)
    if tool_trace_mode == "aggregate":
        published_tool_names: list[str] = []
    elif tool_trace_mode == "sample":
        published_tool_names = tool_names[:1]
    else:
        published_tool_names = tool_names
    llm_turn_count = len(_REASONING_RE.findall(log_text))
    if llm_turn_count == 0 and return_code == 0:
        # The Hermes log may omit reasoning blocks in quiet mode. One completed
        # worker still proves that at least one model turn was executed.
        llm_turn_count = 1
    status, error_code = _status(
        task_status=task_status,
        return_code=return_code,
        profile=profile,
    )
    if profile == "risk-management":
        from orchestration.risk_observability import risk_trace_should_publish

        if not risk_trace_should_publish(
            task_id=task_id,
            request_id=request_id,
            status=status,
            error_code=error_code,
            return_code=return_code,
            tool_names=tool_names,
            tool_error_count=tool_error_count,
            latency_ms=max(int(ended_ms) - int(started_ms), 0),
            environment=runtime_env,
        ):
            _LOG.info(
                "langsmith-risk-trace-sampled task=%s sample_rate=%s",
                _safe_id(task_id),
                runtime_env.get("LANGSMITH_RISK_TRACE_SAMPLE_RATE", "0.10"),
            )
            return False
    try:
        try:
            from qa_hermes_worker import task_execution_metadata
        except ImportError:
            from scripts.qa_hermes_worker import task_execution_metadata

        execution_metadata = task_execution_metadata(
            task_body,
            profile=profile,
            env=runtime_env,
        )
    except Exception:  # noqa: BLE001 - observability remains fail-open
        execution_metadata = {
            "workflow_mode": "unknown",
            "analysis_mode": None,
            "configured_max_turns": 0,
        }
    workflow_mode = str(execution_metadata.get("workflow_mode") or "unknown")
    analysis_mode = execution_metadata.get("analysis_mode")
    analysis_mode = str(analysis_mode) if analysis_mode else None
    configured_max_turns = max(
        0, int(execution_metadata.get("configured_max_turns") or 0)
    )
    base = identity["seed"]
    worker_uuid = UUID(identity["worker_run_id"])
    trace_uuid = UUID(identity["trace_id"])
    model_uuid = _uuid(f"model:{base}")
    worker_dotted = identity["worker_dotted_order"]
    parent_run_id, _parent_dotted_order = _langsmith_parent(task_body)
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
        workflow_mode=workflow_mode,
        analysis_mode=analysis_mode,
        configured_max_turns=configured_max_turns,
    )
    worker_metadata["tool_trace_mode"] = tool_trace_mode
    worker_metadata["tool_trace_published_count"] = len(published_tool_names)
    worker_metadata["tool_trace_aggregated"] = bool(tool_names)
    safe_inputs = {
        "task_id": task_id,
        "workflow_root_task_id": root_id,
        "kanban_run_id": run_id,
        "profile": profile,
        "workflow_mode": workflow_mode,
        "analysis_mode": analysis_mode,
        "configured_max_turns": configured_max_turns,
        "task_body_present": bool(str(task_body).strip()),
        "task_body_length": len(str(task_body)),
        "request_id": request_id,
        "raw_payloads_sent": False,
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "llm_calls": llm_turn_count,
        "actual_turns": llm_turn_count,
        "tool_calls": tool_count,
        "tool_error_count": tool_error_count,
        "tool_duration_total_ms": tool_duration_total_ms,
        "tool_latency_available": tool_latency_available,
        "tool_timing_source": (
            "hermes-log-duration" if tool_latency_available else "unavailable"
        ),
        "telemetry_completeness": worker_metadata["telemetry_completeness"],
        "tool_trace_mode": tool_trace_mode,
        "tool_trace_published_count": len(published_tool_names),
    }
    safe_outputs = {
        "status": status,
        "error_code": error_code,
        "return_code": int(return_code),
        "latency_ms": max(int(ended_ms) - int(started_ms), 0),
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "llm_calls": llm_turn_count,
        "actual_turns": llm_turn_count,
        "workflow_mode": workflow_mode,
        "analysis_mode": analysis_mode,
        "configured_max_turns": configured_max_turns,
        "tool_calls": tool_count,
        "tool_error_count": tool_error_count,
        "tool_duration_total_ms": tool_duration_total_ms,
        "tool_latency_available": tool_latency_available,
        "tool_timing_source": (
            "hermes-log-duration" if tool_latency_available else "unavailable"
        ),
        "telemetry_completeness": worker_metadata["telemetry_completeness"],
        "raw_payloads_sent": False,
        "tool_trace_mode": tool_trace_mode,
        "tool_trace_published_count": len(published_tool_names),
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
            parent_run_id=parent_run_id,
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
    for index, tool_name in enumerate(published_tool_names):
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


def publish_discord_worker_trace(
    *,
    message_id: str,
    profile: str,
    status: str,
    started_ms: int,
    ended_ms: int,
    session_id: str | None = None,
    return_code: int = 0,
    llm_calls: int = 1,
    error_count: int = 0,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Publish one redacted trace for a direct Discord Hermes turn.

    Kanban workers use :func:`publish_department_worker_trace`, which derives
    its identity from a task and persisted worker log. Direct Discord turns do
    not have either of those inputs, so this boundary records only the stable
    message/session coordinates and terminal timing. The same batch publisher,
    project, redaction policy, and profile registry are reused; no raw message,
    prompt, answer, tool argument, or tool result is sent.
    """

    runtime_env = env or os.environ
    profile_spec = _PROFILE_SPECS.get(str(profile).strip())
    safe_message_id = _safe_id(message_id, limit=160)
    if profile_spec is None or not safe_message_id:
        return False

    started = int(started_ms)
    ended = max(int(ended_ms), started)
    trace_uuid = _uuid(f"discord:{profile}:{safe_message_id}")
    worker_uuid = _uuid(f"discord-worker:{profile}:{safe_message_id}")
    worker_id = f"{profile}.discord"
    request_id = f"discord:{safe_message_id}"
    safe_status = _safe_id(status, limit=40) or "completed"
    safe_session_id = _safe_id(session_id, limit=160) if session_id else None
    metadata = {
        "schema_version": profile_spec["schema_version"],
        "trace_kind": "discord_worker",
        "observation_unit": "worker",
        "source": "hermes-discord-gateway",
        "department": profile_spec["department"],
        "stage": profile_spec["department"],
        "profile": str(profile),
        "worker_id": worker_id,
        "role": "department_head",
        "workflow_mode": "discord",
        "request_id": request_id,
        "trace_id": str(trace_uuid),
        "discord_message_id": safe_message_id,
        "session_id": safe_session_id,
        "status": safe_status,
        "return_code": int(return_code),
        "error_count": max(0, int(error_count)),
        "llm_calls": max(0, int(llm_calls)),
        "actual_turns": max(0, int(llm_calls)),
        "latency_ms": max(ended - started, 0),
        "latency_scope": "discord_turn",
        "tool_calls": 0,
        "tool_error_count": 0,
        "tool_latency_available": False,
        "tool_timing_source": "unavailable",
        "telemetry_completeness": "gateway-boundary",
        "raw_payloads_sent": False,
    }
    project_name = _safe_id(runtime_env.get("LANGSMITH_PROJECT", "First")) or "First"
    run = _run_payload(
        run_uuid=worker_uuid,
        trace_uuid=trace_uuid,
        dotted_order=f"{_ls_time(started)}{worker_uuid}",
        name=f"{profile_spec['name_prefix']}.discord",
        run_type="chain",
        started_ms=started,
        ended_ms=ended,
        metadata=metadata,
        project_name=project_name,
        inputs={
            "message_id": safe_message_id,
            "session_id_present": bool(safe_session_id),
            "raw_payloads_sent": False,
        },
        outputs={
            "status": safe_status,
            "return_code": int(return_code),
            "latency_ms": max(ended - started, 0),
            "llm_calls": max(0, int(llm_calls)),
            "raw_payloads_sent": False,
        },
    )
    return _post_batch(env=runtime_env, runs=[run])


def publish_accounting_worker_trace(**kwargs: Any) -> bool:
    """Backward-compatible Accounting entry point."""

    return publish_department_worker_trace(**kwargs)


__all__ = [
    "ACCOUNTING_PROFILE",
    "QA_PROFILE",
    "department_worker_trace_identity",
    "publish_accounting_worker_trace",
    "publish_department_worker_trace",
    "publish_discord_worker_trace",
    "worker_log_metrics",
]
