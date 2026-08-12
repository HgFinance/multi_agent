"""Allow-listed Tool adapters for Risk Workers."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .contracts import RiskSkillContext, RiskSkillResult, make_result
from .guards import normalize_tool_output

RiskTool = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolInvocation:
    result: RiskSkillResult
    latency_ms: int


def invoke_tool(
    tool: RiskTool,
    payload: dict[str, Any],
    context: RiskSkillContext,
    *,
    tool_name: str | Sequence[str],
) -> ToolInvocation:
    started = time.perf_counter()
    tool_names = [tool_name] if isinstance(tool_name, str) else list(tool_name)
    if not tool_names:
        tool_names = ["risk.tool.unknown"]
    try:
        result = normalize_tool_output(tool(payload))
        result = result.model_copy(update={"tool_calls": tool_names})
    except Exception as exc:  # noqa: BLE001 - fail closed at the Tool boundary.
        result = make_result(
            "context.internal_api.v1",
            "ESCALATE",
            {"worker_id": context.worker_id},
            tool_calls=tool_names,
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
        base_url_env: str = "RISK_INTERNAL_API_URL",
    ) -> None:
        self.path = path
        self.method = method.upper()
        self.base_url_env = base_url_env

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_url = os.getenv(self.base_url_env)
        if not base_url:
            raise RuntimeError("INTERNAL_API_URL_NOT_CONFIGURED")
        url = urljoin(base_url.rstrip("/") + "/", self.path.lstrip("/"))
        timeout = float(os.getenv("RISK_INTERNAL_API_TIMEOUT_SECONDS", "8"))
        response = httpx.request(self.method, url, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise TypeError("INVALID_INTERNAL_API_RESPONSE")
        return body
