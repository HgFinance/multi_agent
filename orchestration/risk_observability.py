"""Redacted, fail-open LangSmith spans shared by Risk and its projections."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import NAMESPACE_URL, uuid5

logger = logging.getLogger(__name__)

from orchestration.llm_observability import (
    _mark_langsmith_quota_pause,
    _structured_langsmith_client,
    langsmith_enabled,
    langsmith_project,
)
from orchestration.langsmith_egress import langsmith_egress_enabled

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
        "raw_payloads_sent",
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
_REQUEST_ID_RE = re.compile(
    r"(?:^|\n)(?:request_id|discord_request_id)=(?P<id>[^\s\n]+)",
    re.IGNORECASE,
)

_RISK_TRACE_SUCCESS_STATUSES = frozenset(
    {"completed", "success", "succeeded", "done", "ok"}
)
_DEFAULT_RISK_TRACE_SAMPLE_RATE = 0.10
_DEFAULT_RISK_TRACE_SLOW_MS = 45_000


def _bounded_float(environment: Mapping[str, str], key: str, default: float) -> float:
    try:
        return min(max(float(environment.get(key, default)), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


def _bounded_int(environment: Mapping[str, str], key: str, default: int) -> int:
    try:
        return max(int(environment.get(key, default)), 0)
    except (TypeError, ValueError):
        return default


def _is_legal_wiki_tool(name: Any) -> bool:
    normalized = str(name or "").casefold()
    return "query_risk_legal_wiki" in normalized or "legal_wiki" in normalized


def risk_trace_should_publish(
    *,
    task_id: str,
    request_id: str = "",
    status: str,
    error_code: str | None = None,
    return_code: int | None = None,
    tool_names: Sequence[str] = (),
    tool_error_count: int = 0,
    legal_wiki_call_count: int = 0,
    latency_ms: int = 0,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Apply Risk's one sampling policy to every canonical trace observer.

    Successful ordinary work is sampled deterministically by task/request ID.
    Non-success states, errors, legal-Wiki work, and slow work are always kept
    for diagnosis. A missing identity is kept too: dropping an uncorrelated
    trace would make incident reconstruction impossible.
    """

    normalized_status = str(status or "").strip().casefold()
    if (
        normalized_status not in _RISK_TRACE_SUCCESS_STATUSES
        or error_code
        or (return_code is not None and int(return_code) != 0)
        or int(tool_error_count or 0) > 0
        or int(legal_wiki_call_count or 0) > 0
        or any(_is_legal_wiki_tool(name) for name in tool_names)
    ):
        return True

    env = os.environ if environment is None else environment
    slow_ms = _bounded_int(
        env, "LANGSMITH_RISK_TRACE_SLOW_MS", _DEFAULT_RISK_TRACE_SLOW_MS
    )
    if int(latency_ms or 0) >= slow_ms:
        return True

    sample_rate = _bounded_float(
        env,
        "LANGSMITH_RISK_TRACE_SAMPLE_RATE",
        _DEFAULT_RISK_TRACE_SAMPLE_RATE,
    )
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False

    identity = str(task_id or request_id or "").strip()
    if not identity:
        return True
    digest = hashlib.sha256(f"risk-trace:{identity}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < sample_rate


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


def _request_id_from_task_body(*, root_id: str, task_body: str) -> str:
    """Keep Risk's auxiliary profile on the same request correlation key."""

    match = _REQUEST_ID_RE.search(str(task_body or ""))
    return str(match.group("id") if match else root_id).strip()[:160]


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
    task_body: str,
    run_id: str,
    root_id: str,
    session_id: str,
    log_dir: str | os.PathLike[str],
    started_ms: int,
    ended_ms: int,
    status: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Publish one redacted Risk profile under the canonical worker trace.

    The dispatcher worker already owns the Risk root. This profile remains a
    useful bounded child for token/context and tool timing, but it must never
    become a second root for the same task.
    """

    env = environment or os.environ
    if not (
        langsmith_egress_enabled(env)
        and (
            str(env.get("LANGSMITH_TRACING", "")).casefold()
            in {"1", "true", "yes", "on"}
            or str(env.get("HGFINANCE_LANGSMITH_PUBLISH_ENABLED", "")).casefold()
            in {"1", "true", "yes", "on"}
        )
        and str(env.get("LANGSMITH_API_KEY", "")).strip()
    ):
        return False
    if not str(run_id or "").strip():
        return False
    profile = profile_risk_hermes_session(log_dir, session_id)
    if not profile:
        return False
    request_id = _request_id_from_task_body(root_id=root_id, task_body=task_body)
    if not risk_trace_should_publish(
        task_id=task_id,
        request_id=request_id,
        status=status,
        tool_error_count=int(profile.get("tool_error_count", 0) or 0),
        legal_wiki_call_count=int(profile.get("legal_wiki_call_count", 0) or 0),
        latency_ms=max(int(ended_ms) - int(started_ms), 0),
        environment=env,
    ):
        logger.info(
            "langsmith-risk-profile-sampled task=%s sample_rate=%s",
            str(task_id)[:160],
            env.get("LANGSMITH_RISK_TRACE_SAMPLE_RATE", "0.05"),
        )
        return False
    try:
        from scripts.hermes_worker_observability import (
            department_worker_trace_identity,
        )

        worker_identity = department_worker_trace_identity(
            task_id=task_id,
            task_body=task_body,
            profile="risk-management",
            run_id=run_id,
            started_ms=started_ms,
        )
        parent_run_id = worker_identity["worker_run_id"]
        trace_id = worker_identity["trace_id"]
        parent_dotted_order = worker_identity["worker_dotted_order"]
    except (ImportError, TypeError, ValueError):
        # A missing canonical identity is unsafe: publishing a standalone
        # profile would recreate the duplicate root this observer is meant to
        # avoid.
        return False
    profile_run_id = uuid5(
        NAMESPACE_URL, f"hgfinance:risk-hermes:{task_id}:{session_id}"
    )
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
        "department": "risk",
        "stage": "risk",
        "profile": "risk-management",
        "task_id": str(task_id)[:160],
        "request_id": request_id,
        "root_id": str(root_id)[:160],
        "trace_id": str(trace_id)[:160],
        "parent_run_id": str(parent_run_id)[:160],
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
        "id": str(profile_run_id),
        "trace_id": str(trace_id),
        "dotted_order": f"{parent_dotted_order}.{stamp}{profile_run_id}",
        "name": "risk.hermes-worker-profile",
        "run_type": "chain",
        "session_name": str(env.get("LANGSMITH_PROJECT", "First"))[:120] or "First",
        "inputs": {
            "task_id": str(task_id)[:160],
            "request_id": request_id,
            "root_id": str(root_id)[:160],
            "trace_id": str(trace_id)[:160],
            "session_id": str(session_id)[:160],
        },
        "outputs": profile,
        "start_time": started,
        "end_time": ended,
        "extra": {"metadata": metadata},
        "tags": ["hgfinance", "risk", "hermes", "redacted", "worker-profile"],
        "parent_run_id": str(parent_run_id),
    }
    try:
        # Use the shared SDK queue rather than the old synchronous
        # ``/sessions`` + ``/runs/batch`` pair. This removes two legacy HTTP
        # calls and keeps Risk profile publication off the terminal path.
        _client().create_run(
            id=payload["id"],
            name=payload["name"],
            run_type=payload["run_type"],
            project_name=payload["session_name"],
            trace_id=payload["trace_id"],
            dotted_order=payload["dotted_order"],
            parent_run_id=payload["parent_run_id"],
            inputs=payload["inputs"],
            outputs=payload["outputs"],
            start_time=datetime.fromtimestamp(started / 1000, tz=timezone.utc),
            end_time=datetime.fromtimestamp(ended / 1000, tz=timezone.utc),
            extra=payload["extra"],
            tags=payload["tags"],
        )
        return True
    except Exception as exc:  # noqa: BLE001 - observer remains fail-open.
        _mark_langsmith_quota_pause(exc)
        return False


def record_risk_hermes_terminal_activity(
    *,
    event_id: str,
    task_id: str,
    root_id: str,
    request_id: str = "",
    status: str,
    started_ms: int = 0,
    ended_ms: int = 0,
    discord_status: str = "not_attempted",
    discord_channel_id: str | None = None,
    discord_thread_id: str | None = None,
    discord_message_id: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Send one redacted Hermes terminal receipt to the existing Risk API.

    This bridge is deliberately fail-open and carries no task body, result,
    model output, credentials, or trading instruction.  It makes the already
    existing Hermes -> supervisor -> Discord lifecycle visible to Risk API
    observability while leaving Risk's typed decision endpoints unchanged.
    """

    env = os.environ if environment is None else environment
    base_url = str(env.get("RISK_API_URL", "")).strip().rstrip("/")
    if not base_url or not str(event_id).strip() or not str(task_id).strip():
        return False

    started = max(int(started_ms or 0), 0)
    ended = max(int(ended_ms or 0), started)
    payload = {
        "schema_version": "risk.hermes-terminal-activity.v1",
        "event_id": str(event_id)[:240],
        "task_id": str(task_id)[:240],
        "root_id": str(root_id)[:240],
        "request_id": str(request_id)[:240] or None,
        "status": str(status)[:40],
        "duration_ms": max(ended - started, 0),
        "discord_status": str(discord_status or "not_attempted")[:40],
        "discord_channel_id": str(discord_channel_id)[:120]
        if discord_channel_id
        else None,
        "discord_thread_id": str(discord_thread_id)[:120]
        if discord_thread_id
        else None,
        "discord_message_id": str(discord_message_id)[:120]
        if discord_message_id
        else None,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "HgFinance-RiskHermesReceipt/1.0",
    }
    service_token = str(
        env.get("RISK_SERVICE_AUTH_TOKEN") or env.get("RISK_API_INTERNAL_TOKEN") or ""
    ).strip()
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"
    try:
        timeout = min(
            max(float(env.get("RISK_HERMES_RECEIPT_TIMEOUT_SECONDS", "0.5")), 0.1),
            2.0,
        )
    except (TypeError, ValueError):
        timeout = 0.5
    request = urllib_request.Request(
        f"{base_url}/risk/v1/observability/hermes",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib_error.URLError, TypeError, ValueError):
        # The Risk API is an observer for this receipt.  It must never turn a
        # completed Hermes answer or Discord delivery into a failed workflow.
        return False


def _enabled() -> bool:
    return langsmith_enabled()


def _safe(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) in _SAFE_KEYS
        and (value is None or isinstance(value, (str, int, float, bool)))
    }


@lru_cache(maxsize=1)
def _client():
    # Share the lifecycle client's queue with the root/worker publishers. A
    # second Client would create another background worker and another startup
    # capability probe in the same process.
    return _structured_langsmith_client()


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

        safe_metadata = _safe({**dict(metadata or {}), "raw_payloads_sent": False})
        context = trace(
            name,
            run_type="chain",
            inputs=_safe(inputs),
            project_name=langsmith_project("workflow"),
            tags=["hgfinance", "risk", "redacted"],
            metadata=safe_metadata,
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


__all__ = [
    "RISK_SPANS",
    "record_risk_hermes_terminal_activity",
    "risk_span",
    "set_risk_span_outputs",
]
