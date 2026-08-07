"""Read-only Pinecone adapter for the Risk policy namespace.

The namespace is deliberately fixed in this module. Callers cannot redirect a
Risk query to QA or another tenant by passing a request parameter.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Self

import httpx

RISK_POLICY_NAMESPACE = "risk-compliance-policy"


class PineconeQueryError(RuntimeError):
    """Raised when the Risk policy index cannot be queried safely."""


class PineconeConfigurationError(PineconeQueryError):
    """Raised when the Risk Pinecone data-plane settings are incomplete."""


@dataclass(frozen=True)
class PineconeConfig:
    api_key: str
    index_host: str
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> PineconeConfig:
        try:
            timeout = float(os.getenv("PINECONE_TIMEOUT_SECONDS", "8"))
        except ValueError as exc:
            raise PineconeConfigurationError(
                "PINECONE_TIMEOUT_SECONDS must be a positive number"
            ) from exc
        return cls(
            api_key=os.getenv("PINECONE_API_KEY", "").strip(),
            index_host=os.getenv("PINECONE_INDEX_HOST", "").strip().rstrip("/"),
            timeout_seconds=timeout,
        )

    def validate(self) -> None:
        if not self.api_key or not self.index_host:
            raise PineconeConfigurationError("PINECONE_NOT_CONFIGURED")
        if self.timeout_seconds <= 0:
            raise PineconeConfigurationError(
                "PINECONE_TIMEOUT_SECONDS must be positive"
            )


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            try:
                return date.fromisoformat(normalized[:10])
            except ValueError:
                return None
    return None


def _is_point_in_time(metadata: Mapping[str, Any], as_of: date) -> bool:
    effective_from = _as_date(metadata.get("effective_from"))
    effective_to = _as_date(metadata.get("effective_to"))
    if effective_from is None:
        return False
    return effective_from <= as_of and (effective_to is None or as_of <= effective_to)


class PineconeEvidenceClient:
    """Minimal read-only query client for ``risk-compliance-policy``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        index_host: str | None = None,
        timeout_seconds: float = 8.0,
        client: httpx.Client | None = None,
    ) -> None:
        config = PineconeConfig(
            api_key=os.getenv("PINECONE_API_KEY", "").strip()
            if api_key is None
            else api_key,
            index_host=os.getenv("PINECONE_INDEX_HOST", "").strip().rstrip("/")
            if index_host is None
            else index_host.rstrip("/"),
            timeout_seconds=timeout_seconds,
        )
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    @classmethod
    def from_env(cls) -> PineconeEvidenceClient:
        config = PineconeConfig.from_env()
        return cls(
            api_key=config.api_key,
            index_host=config.index_host,
            timeout_seconds=config.timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def query(
        self,
        vector: Sequence[float],
        *,
        as_of: date | str,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Query and return only valid point-in-time Risk evidence matches."""

        self.config.validate()
        if not vector or not all(isinstance(value, (int, float)) for value in vector):
            raise ValueError("query vector must contain numeric values")
        if top_k < 1 or top_k > 20:
            raise ValueError("top_k must be between 1 and 20")
        as_of_date = _as_date(as_of)
        if as_of_date is None:
            raise ValueError("as_of must be an ISO date or datetime")

        try:
            response = self._client.post(
                f"{self.config.index_host}/query",
                headers={
                    "Api-Key": self.config.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "vector": list(vector),
                    "topK": top_k,
                    "namespace": RISK_POLICY_NAMESPACE,
                    "includeMetadata": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PineconeQueryError("Pinecone Risk policy query failed") from exc

        if not isinstance(payload, dict) or not isinstance(
            payload.get("matches"), list
        ):
            raise PineconeQueryError("Pinecone response has invalid matches shape")

        matches: list[dict[str, Any]] = []
        for raw_match in payload["matches"]:
            if not isinstance(raw_match, dict):
                continue
            raw_metadata = raw_match.get("metadata")
            if not isinstance(raw_metadata, dict):
                continue
            if not _is_point_in_time(raw_metadata, as_of_date):
                continue
            required = ("chunk_id", "document_id", "version")
            if not all(str(raw_metadata.get(key, "")).strip() for key in required):
                continue
            matches.append(raw_match)
        return matches


__all__ = [
    "RISK_POLICY_NAMESPACE",
    "PineconeConfig",
    "PineconeConfigurationError",
    "PineconeEvidenceClient",
    "PineconeQueryError",
]
