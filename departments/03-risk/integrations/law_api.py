"""Read-only 국가법령정보센터 Open API client for Risk policy ingestion.

The client intentionally exposes only search and document retrieval. It does
not make policy decisions, mutate the source service, or write to a vector DB.
The OC credential is loaded from the environment and is never included in
logs or exception messages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx


class LawApiError(RuntimeError):
    """Base error for unavailable or malformed Law.go.kr responses."""


class LawApiConfigurationError(LawApiError):
    """Raised when the local Law.go.kr configuration is incomplete."""


@dataclass(frozen=True)
class LawApiConfig:
    """Environment-backed settings for the read-only Law.go.kr API."""

    enabled: bool
    base_url: str
    oc: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LawApiConfig:
        values = os.environ if env is None else env
        enabled = values.get("LAW_API_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        base_url = values.get("LAW_API_BASE_URL", "https://www.law.go.kr").strip()
        oc = values.get("LAW_API_OC", "").strip()
        try:
            timeout_seconds = float(values.get("LAW_API_TIMEOUT_SECONDS", "10"))
        except ValueError as exc:
            raise LawApiConfigurationError(
                "LAW_API_TIMEOUT_SECONDS must be a positive number"
            ) from exc

        if not base_url:
            raise LawApiConfigurationError("LAW_API_BASE_URL is required")
        if timeout_seconds <= 0:
            raise LawApiConfigurationError("LAW_API_TIMEOUT_SECONDS must be positive")
        if enabled and not oc:
            raise LawApiConfigurationError(
                "LAW_API_OC is required when Law API is enabled"
            )

        return cls(
            enabled=enabled,
            base_url=base_url.rstrip("/"),
            oc=oc,
            timeout_seconds=timeout_seconds,
        )


class LawApiClient:
    """Small read-only adapter for the documented JSON endpoints."""

    def __init__(
        self,
        config: LawApiConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    @classmethod
    def from_env(cls) -> LawApiClient:
        return cls(LawApiConfig.from_env())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int],
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise LawApiConfigurationError("Law.go.kr API is disabled")

        # OC is required by the service as a query parameter. Do not include
        # the resulting URL in errors/logs because it contains the credential.
        request_params: dict[str, str | int] = {
            "OC": self.config.oc,
            "type": "JSON",
            **params,
        }
        try:
            response = self._client.get(
                f"{self.config.base_url}{path}",
                params=request_params,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LawApiError("Law.go.kr JSON request failed") from exc

        if not isinstance(payload, dict):
            raise LawApiError("Law.go.kr response must be a JSON object")
        return payload

    def search_laws(
        self, query: str, *, page: int = 1, display: int = 20
    ) -> dict[str, Any]:
        """Search current laws using the Law.go.kr JSON search endpoint."""

        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if page < 1 or display < 1:
            raise ValueError("page and display must be positive")
        return self._get_json(
            "/DRF/lawSearch.do",
            params={
                "target": "law",
                "query": query,
                "page": page,
                "display": display,
            },
        )

    def get_law(self, law_id: str) -> dict[str, Any]:
        """Fetch a law body by its Law.go.kr identifier."""

        law_id = law_id.strip()
        if not law_id:
            raise ValueError("law_id must not be empty")
        return self._get_json(
            "/DRF/lawService.do",
            params={"target": "law", "ID": law_id},
        )


def credential_status(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Return presence-only configuration status; never return the OC value."""

    values = os.environ if env is None else env
    oc = values.get("LAW_API_OC", "").strip()
    return {
        "enabled": values.get("LAW_API_ENABLED", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        "configured": bool(oc),
        "secret_values_exposed": False,
    }


__all__ = [
    "LawApiClient",
    "LawApiConfig",
    "LawApiConfigurationError",
    "LawApiError",
    "credential_status",
]
