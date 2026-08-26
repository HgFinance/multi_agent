"""Redacted LangSmith observation for dispatcher-owned Accounting workers.

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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5


ACCOUNTING_PROFILE = "accounting-portfolio-department"
_PROFILE_RE = re.compile(r"(?:^|\s)-p\s+(?P<profile>[A-Za-z0-9._-]+)")
_MODEL_RE = re.compile(r"^\s*default:\s*([^#\s]+)", re.MULTILINE)
_PROVIDER_RE = re.compile(r"^\s*provider:\s*([^#\s]+)", re.MULTILINE)
_TOOL_RE = re.compile(r"⚡\s+(?P<name>[A-Za-z0-9_.-]+)")
_TOOL_SUMMARY_RE = re.compile(r"(?:\(|,|\s)(?P<count>\d+)\s+tool calls?\b", re.IGNORECASE)
_REASONING_RE = re.compile(r"Reasoning", re.IGNORECASE)
_ROOT_RE = re.compile(r"(?:workflow_root_task_id|root_task_id)=(?P<id>t_[A-Za-z0-9_-]+)")
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
    return uuid5(NAMESPACE_URL, f"hgfinance:accounting-worker:{seed}")


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


def _root_id(*, task_id: str, task_body: str) -> str:
    match = _ROOT_RE.search(task_body)
    if match:
        return match.group("id")
    # The direct CEO root itself has no workflow_root marker.
    return task_id if _TASK_RE.fullmatch(task_id) else _safe_id(task_id)


def _status(*, task_status: str, return_code: int) -> tuple[str, str | None]:
    normalized = str(task_status or "").casefold()
    if normalized in {"done", "completed", "archived"} and return_code == 0:
        return "completed", None
    if normalized in {"blocked", "gave_up", "timed_out", "crashed", "failed"}:
        return normalized, f"kanban_{normalized}"
    if return_code != 0:
        return "failed", f"worker_exit_{abs(int(return_code))}"
    return normalized or "completed", None


def _metadata(
    *,
    task_id: str,
    root_id: str,
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
) -> dict[str, Any]:
    return {
        "schema_version": "llm.accounting-worker.v1",
        "trace_kind": "department_worker",
        "observation_unit": observation_unit,
        "source": "kanban-dispatcher-worker-boundary",
        "department": "accounting-portfolio",
        "profile": profile,
        "task_id": task_id,
        "workflow_root_task_id": root_id,
        "kanban_run_id": run_id,
        "status": status,
        "error_code": error_code,
        "provider": provider,
        "model_name": model,
        "tool_names": list(tool_names),
        "tool_call_count": tool_count,
        "llm_turn_count_observed": llm_turn_count,
        "started_at_ms": int(started_ms),
        "completed_at_ms": int(ended_ms),
        "latency_ms": max(int(ended_ms) - int(started_ms), 0),
        "latency_scope": "worker_execution",
        "latency_available": True,
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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(run_uuid),
        "trace_id": str(trace_uuid),
        "dotted_order": dotted_order,
        "name": name,
        "run_type": run_type,
        "session_name": project_name,
        "inputs": {},
        "outputs": {},
        "start_time": int(started_ms),
        "end_time": int(ended_ms),
        "extra": {"metadata": dict(metadata)},
        "tags": ["hgfinance", "accounting", "redacted", "worker"],
    }
    if parent_run_id is not None:
        payload["parent_run_id"] = str(parent_run_id)
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


def publish_accounting_worker_trace(
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
    if profile != ACCOUNTING_PROFILE:
        return False

    root_id = _root_id(task_id=task_id, task_body=task_body)
    provider, model = _model_info(profile=profile, env=runtime_env, argv=argv)
    try:
        log_text = _log_path(task_id=task_id, env=runtime_env).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    tool_names, tool_count = _observed_tools(log_text)
    llm_turn_count = len(_REASONING_RE.findall(log_text))
    if llm_turn_count == 0 and return_code == 0:
        # The Hermes log may omit reasoning blocks in quiet mode. One completed
        # worker still proves that at least one model turn was executed.
        llm_turn_count = 1
    status, error_code = _status(task_status=task_status, return_code=return_code)
    base = _safe_id(f"{root_id}:{task_id}:{run_id}")
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
    )
    runs = [
        _run_payload(
            run_uuid=worker_uuid,
            trace_uuid=trace_uuid,
            dotted_order=worker_dotted,
            name="hgfinance.accounting.worker",
            run_type="chain",
            started_ms=started_ms,
            ended_ms=ended_ms,
            metadata=worker_metadata,
            project_name=project_name,
        ),
        _run_payload(
            run_uuid=model_uuid,
            trace_uuid=trace_uuid,
            dotted_order=f"{worker_dotted}.{_ls_time(started_ms)}{model_uuid}",
            name="hgfinance.accounting.llm",
            run_type="llm",
            started_ms=started_ms,
            ended_ms=ended_ms,
            metadata={
                **worker_metadata,
                "observation_unit": "model",
                "model_call_count_observed": llm_turn_count,
            },
            project_name=project_name,
            parent_run_id=worker_uuid,
        ),
    ]
    for index, tool_name in enumerate(tool_names):
        tool_uuid = _uuid(f"tool:{base}:{index}:{tool_name}")
        runs.append(
            _run_payload(
                run_uuid=tool_uuid,
                trace_uuid=trace_uuid,
                dotted_order=f"{worker_dotted}.{_ls_time(started_ms)}{tool_uuid}",
                name=f"hgfinance.accounting.tool.{tool_name}",
                run_type="tool",
                started_ms=started_ms,
                ended_ms=ended_ms,
                metadata={
                    **worker_metadata,
                    "observation_unit": "tool",
                    "tool_name": tool_name,
                    "tool_call_index": index,
                    "tool_latency_available": False,
                },
                project_name=project_name,
                parent_run_id=worker_uuid,
            )
        )
    return _post_batch(env=runtime_env, runs=runs)


__all__ = ["ACCOUNTING_PROFILE", "publish_accounting_worker_trace"]
