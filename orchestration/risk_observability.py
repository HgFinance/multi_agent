"""Redacted, fail-open LangSmith spans shared by Risk and its projections."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

logger = logging.getLogger(__name__)

from orchestration.llm_observability import langsmith_project

RISK_SPANS = frozenset(
    {
        "risk.advisory",
        "risk.mandate-load",
        "risk.portfolio-snapshot",
        "risk.market-snapshot",
        "risk.regime-classification",
        "risk.stop-calculation",
        "risk.take-profit-calculation",
        "risk.constraint-validation",
        "risk.legal-wiki",
        "risk.discord-projection",
        "risk.notion-projection",
    }
)
_SAFE_KEYS = frozenset(
    {
        "task_id",
        "request_id",
        "root_id",
        "trace_id",
        "risk_plan_id",
        "mandate_version_id",
        "input_hash",
        "algorithm_version",
        "status",
        "stage",
        "target",
        "payload_hash",
        "duration_ms",
        "error",
        "error_code",
        "model",
        "tool",
        "query_mode",
        "input_chars",
        "as_of",
        "llm_wiki_invoked",
        "document_count",
        "page_count",
        "source_reference_count",
        "context_chars",
        "verdict",
        "confidence",
        "escalate",
    }
)

_SESSION_RE = re.compile(r"\[(?P<session>\d{8}_\d{6}_[A-Za-z0-9]+)\]")
_LLM_CALL_RE = re.compile(
    r"API call #(?P<call>\d+): model=(?P<model>[^ ]+) "
    r"provider=(?P<provider>[^ ]+) in=(?P<input>\d+) out=(?P<output>\d+) "
    r"total=\d+ latency=(?P<latency>[0-9.]+)s"
)
_TOOL_RE = re.compile(
    r"agent\.tool_executor: [Tt]ool (?P<tool>[A-Za-z0-9_.-]+) "
    r"(?P<outcome>completed|returned error) "
    r"\((?P<latency>[0-9.]+)s"
)
_LOG_TIME_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def _log_epoch_ms(line: str) -> int | None:
    match = _LOG_TIME_RE.match(line)
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            match.group("stamp"), "%Y-%m-%d %H:%M:%S,%f"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _langsmith_project_id(environment: Mapping[str, str]) -> str | None:
    """Resolve the LangSmith project UUID for direct batch ingestion.

    The v1 ``/runs/batch`` endpoint accepts ``session_name`` for compatibility,
    but current SmithDB indexing only associates the run when ``session_id`` is
    also present.  Resolution is observer-only and fail-open: an outage must
    never affect Risk completion or delivery.
    """

    project_name = str(environment.get("LANGSMITH_PROJECT") or "First").strip()
    api_key = str(environment.get("LANGSMITH_API_KEY") or "").strip()
    endpoint = str(
        environment.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com"
    ).rstrip("/")
    if not project_name or not api_key:
        return None
    request = urllib_request.Request(
        f"{endpoint}/sessions?name={quote(project_name)}&limit=1",
        headers={"Accept": "application/json", "x-api-key": api_key},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read() or b"[]")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or not payload:
        return None
    project_id = payload[0].get("id") if isinstance(payload[0], Mapping) else None
    return str(project_id or "").strip() or None


def profile_risk_hermes_session(
    log_dir: str | os.PathLike[str], session_id: str
) -> dict[str, Any]:
    """Extract bounded timing counters for one Risk Hermes session.

    Prompts, model responses, tool arguments and tool results are never
    returned. Session-less parallel tool completion lines are attributed only
    when no second Hermes session overlaps the selected log window.
    """

    session = str(session_id or "").strip()
    if not session:
        return {}
    records: list[str] = []
    source_files = 0
    try:
        paths = sorted(
            Path(log_dir).glob("agent.log*"),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(session in line for line in lines):
            source_files += 1
            records.extend(lines)
    selected = [index for index, line in enumerate(records) if f"[{session}]" in line]
    if not selected:
        return {}
    window = records[min(selected) : max(selected) + 1]
    overlapping_sessions = {
        match.group("session")
        for line in window
        for match in _SESSION_RE.finditer(line)
        if match.group("session") != session
    }

    llm_calls: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for line in window:
        tagged = f"[{session}]" in line
        llm_match = _LLM_CALL_RE.search(line) if tagged else None
        if llm_match:
            llm_calls.append(
                {
                    "call": int(llm_match.group("call")),
                    "model": llm_match.group("model")[:120],
                    "provider": llm_match.group("provider")[:120],
                    "input_tokens": int(llm_match.group("input")),
                    "output_tokens": int(llm_match.group("output")),
                    "latency_ms": round(float(llm_match.group("latency")) * 1000),
                    "completed_at_ms": _log_epoch_ms(line),
                }
            )
        tool_match = _TOOL_RE.search(line)
        if tool_match and (tagged or not overlapping_sessions):
            tools.append(
                {
                    "tool": tool_match.group("tool")[:120],
                    "status": (
                        "error"
                        if tool_match.group("outcome") == "returned error"
                        else "success"
                    ),
                    "latency_ms": round(float(tool_match.group("latency")) * 1000),
                }
            )
    if not llm_calls:
        return {}

    llm_latencies = [int(call["latency_ms"]) for call in llm_calls]
    tool_latencies = [int(tool["latency_ms"]) for tool in tools]
    tool_names = [str(tool["tool"]) for tool in tools]
    first_input = int(llm_calls[0]["input_tokens"])
    last_input = int(llm_calls[-1]["input_tokens"])
    return {
        "session_id": session,
        "provider": str(llm_calls[-1]["provider"]),
        "model": str(llm_calls[-1]["model"]),
        "llm_call_count": len(llm_calls),
        "llm_latency_ms_total": sum(llm_latencies),
        "llm_latency_ms_max": max(llm_latencies),
        "llm_input_tokens_first": first_input,
        "llm_input_tokens_last": last_input,
        "llm_context_growth_tokens": max(last_input - first_input, 0),
        "llm_output_tokens_total": sum(
            int(call["output_tokens"]) for call in llm_calls
        ),
        "tool_call_count": len(tools),
        "tool_latency_ms_total": sum(tool_latencies),
        "tool_latency_ms_max": max(tool_latencies, default=0),
        "tool_error_count": sum(tool["status"] == "error" for tool in tools),
        "legal_wiki_call_count": sum(
            "query_risk_legal_wiki" in name for name in tool_names
        ),
        "web_tool_call_count": sum(
            name in {"web_search", "web_extract"} for name in tool_names
        ),
        "code_tool_block_count": sum(
            tool["status"] == "error" and tool["tool"] in {"execute_code", "terminal"}
            for tool in tools
        ),
        "source_file_count": source_files,
        "concurrent_session_detected": bool(overlapping_sessions),
    }


def publish_risk_hermes_profile(
    *,
    task_id: str,
    root_id: str,
    session_id: str,
    log_dir: str | os.PathLike[str],
    started_ms: int,
    ended_ms: int,
    status: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Publish one idempotent, redacted Risk worker profile to LangSmith."""

    env = environment or os.environ
    if not (
        str(env.get("LANGSMITH_TRACING", "")).casefold() in {"1", "true", "yes", "on"}
        and str(env.get("LANGSMITH_API_KEY", "")).strip()
    ):
        return False
    profile = profile_risk_hermes_session(log_dir, session_id)
    if not profile:
        return False
    run_id = uuid5(NAMESPACE_URL, f"hgfinance:risk-hermes:{task_id}:{session_id}")
    started = max(int(started_ms), 0)
    ended = max(int(ended_ms), started)
    stamp = datetime.fromtimestamp(started / 1000, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    safe_status = str(status or "completed")[:80]
    metadata = {
        "schema_version": "risk.hermes-worker-profile.v1",
        "trace_kind": "department_worker_profile",
        "source": "risk-hermes-agent-log",
        "department": "risk-management",
        "profile": "risk-management",
        "task_id": str(task_id)[:160],
        "request_id": str(root_id)[:160],
        "root_id": str(root_id)[:160],
        "trace_id": str(run_id)[:160],
        "status": safe_status,
        "latency_ms": ended - started,
        "latency_scope": "worker_execution",
        "attempts": 1,
        "retries": 0,
        "tool_duration_total_ms": int(profile.get("tool_latency_ms_total", 0) or 0),
        "tool_latency_available": bool(profile.get("tool_latency_ms_total", 0)),
        "tool_timing_source": (
            "risk-hermes-agent-log"
            if profile.get("tool_latency_ms_total", 0)
            else "unavailable"
        ),
        "model_name": str(profile.get("model") or "unknown")[:120],
        "provider": str(profile.get("provider") or "unknown")[:120],
        "raw_payloads_sent": False,
        **profile,
    }
    payload = {
        "id": str(run_id),
        "trace_id": str(run_id),
        "dotted_order": f"{stamp}{run_id}",
        "name": "risk.hermes-worker-profile",
        "run_type": "chain",
        "session_name": str(env.get("LANGSMITH_PROJECT", "First"))[:120] or "First",
        "inputs": {
            "task_id": str(task_id)[:160],
            "request_id": str(root_id)[:160],
            "root_id": str(root_id)[:160],
            "trace_id": str(run_id)[:160],
            "session_id": str(session_id)[:160],
        },
        "outputs": profile,
        "start_time": started,
        "end_time": ended,
        "extra": {"metadata": metadata},
        "tags": ["hgfinance", "risk", "hermes", "redacted", "worker-profile"],
    }
    project_id = _langsmith_project_id(env)
    if project_id:
        payload["session_id"] = project_id
    endpoint = str(
        env.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    ).rstrip("/")
    request = urllib_request.Request(
        f"{endpoint}/runs/batch",
        data=json.dumps(
            {"post": [payload], "patch": []}, separators=(",", ":")
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": str(env.get("LANGSMITH_API_KEY", "")),
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=3.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib_error.URLError, ValueError):
        return False


def _enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    } and bool(os.getenv("LANGSMITH_API_KEY", "").strip())


def _safe(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) in _SAFE_KEYS
        and (value is None or isinstance(value, (str, int, float, bool)))
    }


@lru_cache(maxsize=1)
def _client():
    from langsmith import Client

    # risk_span accepts only the scalar allowlist above. Keeping these safe
    # structural fields visible makes the trace useful to QA without sending
    # raw questions, answers, portfolio payloads, or credentials.
    return Client(hide_inputs=False, hide_outputs=False, hide_metadata=False)


def set_risk_span_outputs(run: Any, outputs: Mapping[str, Any]) -> None:
    """Attach allowlisted output metadata without exposing model prose."""

    if run is not None:
        run.outputs = _safe(outputs)


@contextlib.contextmanager
def risk_span(
    name: str,
    metadata: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """Emit one redacted span without allowing telemetry to affect execution."""

    if name not in RISK_SPANS:
        raise ValueError(f"unregistered Risk span: {name}")
    if not _enabled():
        yield None
        return
    started = time.perf_counter()
    try:
        from langsmith import trace

        context = trace(
            name,
            run_type="chain",
            inputs=_safe(inputs),
            project_name=langsmith_project("workflow"),
            tags=["hgfinance", "risk", "redacted"],
            metadata=_safe(metadata),
            client=_client(),
        )
        run = context.__enter__()
    except Exception as exc:  # noqa: BLE001 - optional observer is fail-open
        logger.debug("Risk span unavailable: %s", type(exc).__name__)
        yield None
        return
    try:
        yield run
    except BaseException as exc:
        try:
            run.metadata.update(
                _safe(
                    {
                        "status": "error",
                        "error": type(exc).__name__,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    }
                )
            )
            context.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as observer_exc:  # noqa: BLE001
            logger.debug(
                "Risk span error close failed: %s", type(observer_exc).__name__
            )
        raise
    else:
        try:
            completion = {"duration_ms": int((time.perf_counter() - started) * 1000)}
            if run.metadata.get("status") in {None, "", "running"}:
                completion["status"] = "success"
            run.metadata.update(_safe(completion))
            context.__exit__(None, None, None)
        except Exception as observer_exc:  # noqa: BLE001
            logger.debug(
                "Risk span success close failed: %s", type(observer_exc).__name__
            )


__all__ = ["RISK_SPANS", "risk_span", "set_risk_span_outputs"]
