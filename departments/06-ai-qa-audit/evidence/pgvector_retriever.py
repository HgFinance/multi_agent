"""PIT-aware pgvector policy retrieval boundary for QA Agentic RAG."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

try:
    from .corpus_registry import PolicyCorpusError
except ImportError:  # pragma: no cover - direct FastAPI app import path
    from corpus_registry import PolicyCorpusError


class PgvectorRetrievalError(PolicyCorpusError):
    """Raised when the canonical pgvector function cannot be queried safely."""


def _vector_literal(values: Sequence[float]) -> str:
    if not values:
        raise PgvectorRetrievalError("query embedding is empty")
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise PgvectorRetrievalError("query embedding contains a non-number") from exc
    return "[" + ",".join(format(value, ".12g") for value in numbers) + "]"


class PgvectorPolicyRetriever:
    """Call the 1024-dimensional, service-role-only evidence RPC."""

    def __init__(self, connection: Any, embedder: Any) -> None:
        self._connection = connection
        self._embedder = embedder

    def search(
        self,
        query: str,
        *,
        as_of: datetime,
        allowed_license_scopes: Sequence[str] = ("internal",),
        limit: int = 20,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise PgvectorRetrievalError("query is required")
        if limit < 1 or limit > 100:
            raise PgvectorRetrievalError("limit must be between 1 and 100")
        vector = _vector_literal(self._embedder.embed([query])[0])
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                """
                select * from api.match_evidence_chunks(
                    %s::extensions.vector,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (vector, as_of, list(allowed_license_scopes), limit, min_similarity),
            )
            rows = cursor.fetchall()
        except Exception as exc:
            raise PgvectorRetrievalError("pgvector evidence retrieval failed") from exc
        finally:
            cursor.close()
        return [dict(row) if isinstance(row, dict) else {"row": row} for row in rows]
