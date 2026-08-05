"""Async adapter for department LangGraph pipeline APIs."""

from __future__ import annotations

from typing import Any

import httpx

from backend.app.core.config import Settings


class LangGraphServiceError(RuntimeError):
    """Upstream LangGraph could not be reached or returned an invalid response."""


class LangGraphService:
    """Keep upstream HTTP details out of controllers and easy to mock."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.langgraph_base_url.rstrip("/"),
            timeout=self._settings.langgraph_timeout_seconds,
            transport=self._transport,
        )

    async def health_check(self) -> dict[str, str | None]:
        try:
            async with self._client() as client:
                response = await client.get(self._settings.langgraph_health_path)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return {
                "status": "unavailable",
                "upstream": self._settings.langgraph_base_url,
                "detail": str(exc),
            }
        return {
            "status": "healthy",
            "upstream": self._settings.langgraph_base_url,
            "detail": None,
        }

    async def invoke(
        self,
        *,
        department: str,
        query: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.post(
                    self._settings.langgraph_invoke_path,
                    json={"department": department, "query": query, "context": context},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LangGraphServiceError(f"LangGraph upstream request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise LangGraphServiceError("LangGraph upstream response must be an object")
        return payload
