"""Pydantic-backed Risk policy retrieval tool."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Literal

from integrations.pinecone_client import (
    RISK_POLICY_NAMESPACE,
    PineconeEvidenceClient,
    PineconeQueryError,
)
from pydantic import BaseModel, ConfigDict, Field


class EvidenceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    clause_id: str | None = None
    effective_from: date
    effective_to: date | None = None
    authority: str | None = None
    document_type: str | None = None
    title: str | None = None
    source_url: str | None = None
    text: str | None = None


class EvidenceMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    text: str = ""
    metadata: EvidenceMetadata


class RiskPolicyQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandate_id: str | None = None
    query: str = Field(min_length=1, max_length=4000)
    query_vector: list[float] | None = Field(default=None, min_length=1)
    as_of: date
    top_k: int = Field(default=8, ge=1, le=20)
    document_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)


class RiskPolicyQueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: Literal["risk-compliance-policy"] = RISK_POLICY_NAMESPACE
    matches: list[EvidenceMatch] = Field(default_factory=list)
    retrieved_at: datetime
    status: Literal["OK", "NO_MATCH", "UNAVAILABLE", "INCONCLUSIVE"]
    error_code: str | None = None


EmbeddingProvider = Callable[[str], list[float]]


def query_pinecone_risk_policy(
    request: RiskPolicyQueryInput,
    *,
    client: PineconeEvidenceClient | None = None,
    embedder: EmbeddingProvider | None = None,
) -> RiskPolicyQueryOutput:
    """Retrieve valid Risk policy evidence without allowing namespace override."""

    if request.query_vector is None and embedder is None:
        return RiskPolicyQueryOutput(
            retrieved_at=datetime.now(timezone.utc),
            status="INCONCLUSIVE",
            error_code="EMBEDDING_PROVIDER_NOT_CONFIGURED",
        )

    owns_client = client is None
    pinecone = client or PineconeEvidenceClient.from_env()
    try:
        vector = request.query_vector or embedder(request.query)  # type: ignore[misc]
        raw_matches = pinecone.query(
            vector,
            as_of=request.as_of,
            top_k=request.top_k,
        )
        matches: list[EvidenceMatch] = []
        for match in raw_matches:
            metadata = match.get("metadata") or {}
            if (
                request.document_ids
                and metadata.get("document_id") not in request.document_ids
            ):
                continue
            if (
                request.clause_ids
                and metadata.get("clause_id") not in request.clause_ids
            ):
                continue
            matches.append(
                EvidenceMatch(
                    match_id=str(match.get("id", "")),
                    score=float(match.get("score", 0)),
                    text=str(metadata.get("text", "")),
                    metadata=EvidenceMetadata.model_validate(metadata),
                )
            )
        return RiskPolicyQueryOutput(
            retrieved_at=datetime.now(timezone.utc),
            status="OK" if matches else "NO_MATCH",
            matches=matches,
        )
    except (PineconeQueryError, ValueError, TypeError) as exc:
        return RiskPolicyQueryOutput(
            retrieved_at=datetime.now(timezone.utc),
            status="UNAVAILABLE"
            if isinstance(exc, PineconeQueryError)
            else "INCONCLUSIVE",
            error_code=type(exc).__name__,
        )
    finally:
        if owns_client:
            pinecone.close()


__all__ = [
    "EvidenceMatch",
    "EvidenceMetadata",
    "RiskPolicyQueryInput",
    "RiskPolicyQueryOutput",
    "query_pinecone_risk_policy",
]
