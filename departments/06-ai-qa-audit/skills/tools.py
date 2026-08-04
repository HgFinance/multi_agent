"""Allow-listed Tool adapters for AI-QA Workers."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .contracts import QASkillContext, QASkillResult, make_result
from .guards import normalize_tool_output

QATool = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolInvocation:
    result: QASkillResult
    latency_ms: int


def invoke_tool(
    tool: QATool,
    payload: dict[str, Any],
    context: QASkillContext,
    *,
    tool_name: str,
) -> ToolInvocation:
    started = time.perf_counter()
    try:
        result = normalize_tool_output(tool(payload))
        result = result.model_copy(update={"tool_calls": [tool_name]})
    except Exception as exc:  # noqa: BLE001 - fail closed at the Tool boundary.
        result = make_result(
            "context.internal_api.v1",
            "ESCALATE",
            {"worker_id": context.worker_id},
            tool_calls=[tool_name],
            error_code=type(exc).__name__,
            escalate=True,
        )
    return ToolInvocation(
        result=result,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


class InternalHttpTool:
    """Schema-checked HTTP adapter with a fixed, environment-owned base URL."""

    def __init__(
        self,
        path: str,
        *,
        method: str = "POST",
        base_url_env: str = "QA_INTERNAL_API_URL",
    ) -> None:
        self.path = path
        self.method = method.upper()
        self.base_url_env = base_url_env

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_url = os.getenv(self.base_url_env)
        if not base_url:
            raise RuntimeError("INTERNAL_API_URL_NOT_CONFIGURED")
        url = urljoin(base_url.rstrip("/") + "/", self.path.lstrip("/"))
        timeout = float(os.getenv("QA_INTERNAL_API_TIMEOUT_SECONDS", "8"))
        response = httpx.request(self.method, url, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("INVALID_INTERNAL_API_RESPONSE")
        return body
