"""Policy/Evidence ingestion boundary for the QA department.

The default is production-safe: placeholder documents are rejected, the
source UUID and database DSN are mandatory, and all writes are transactional.
No credential or document body is printed by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

PLACEHOLDER_MARKER = "SAMPLE_PLACEHOLDER"
EMBEDDING_DIMENSIONS = int(os.environ.get("AGENTIC_RAG_EMBEDDING_DIMENSIONS", "1024"))


class IngestionError(RuntimeError):
    """Raised when production ingestion cannot safely continue."""


class EmbeddingProvider(Protocol):
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding of the configured dimension per text."""


@dataclass(frozen=True)
class PolicyChunk:
    external_id: str
    document_type: str
    title: str
    content: str
    chunk_index: int
    content_hash: str
    license_scope: str
    published_at: str | None
    observed_at: str
    source_path: str
    metadata: dict[str, Any]


def build_policy_chunks(
    corpus_dir: Path,
    *,
    allow_placeholders: bool = False,
    observed_at: datetime | None = None,
    max_chars: int = 800,
) -> tuple[PolicyChunk, ...]:
    """Parse Markdown policy files into deterministic, PIT-aware chunks."""

    if max_chars < 100:
        raise IngestionError("max_chars must be at least 100")
    paths = sorted(path for path in corpus_dir.glob("*.md") if path.is_file())
    if not paths:
        raise IngestionError(f"no Markdown policy files found in {corpus_dir}")
    observed = (observed_at or datetime.now(timezone.utc)).isoformat()
    chunks: list[PolicyChunk] = []
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        if PLACEHOLDER_MARKER in raw and not allow_placeholders:
            raise IngestionError(
                f"placeholder policy corpus is not allowed for production ingestion: {path.name}"
            )
        metadata, body = _front_matter(raw)
        title_match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        pieces = _chunk_text(body, max_chars=max_chars)
        if not pieces:
            raise IngestionError(f"policy document has no content: {path.name}")
        document_type = str(metadata.get("document_type") or "policy")
        license_scope = str(metadata.get("license_scope") or "internal")
        published_at = _optional_timestamp(metadata.get("published_at"))
        external_id = str(metadata.get("document_id") or path.name)
        for index, content in enumerate(pieces):
            chunks.append(
                PolicyChunk(
                    external_id=external_id,
                    document_type=document_type,
                    title=title,
                    content=content,
                    chunk_index=index,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    license_scope=license_scope,
                    published_at=published_at,
                    observed_at=observed,
                    source_path=str(path),
                    metadata={
                        "effective_from": metadata.get("effective_from"),
                        "effective_to": metadata.get("effective_to"),
                        "source_path": str(path),
                    },
                )
            )
    return tuple(chunks)


class OpenAIEmbeddingProvider:
    """Embedding adapter; the API key is read by the SDK and never exposed."""

    def __init__(
        self,
        *,
        model: str | None = None,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ) -> None:
        if dimensions <= 0:
            raise IngestionError("embedding dimensions must be positive")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on deployment image
            raise IngestionError("openai package is required for production ingestion") from exc
        self._client = OpenAI()
        self.model = model or os.environ.get("AGENTIC_RAG_EMBEDDING_MODEL", "text-embedding-3-small")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(
            model=self.model,
            input=list(texts),
            dimensions=self.dimensions,
        )
        vectors = [list(item.embedding) for item in response.data]
        if len(vectors) != len(texts) or any(len(vector) != self.dimensions for vector in vectors):
            raise IngestionError("embedding provider returned an unexpected vector shape")
        return vectors


class PgvectorEvidenceRepository:
    """Transactional writer for research.documents/document_versions/chunks."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def ingest(
        self,
        *,
        source_id: UUID,
        chunks: Sequence[PolicyChunk],
        embedder: EmbeddingProvider,
    ) -> int:
        if not chunks:
            raise IngestionError("cannot ingest an empty policy set")
        if embedder.dimensions != EMBEDDING_DIMENSIONS:
            raise IngestionError(
                f"embedding dimension {embedder.dimensions} does not match pgvector dimension "
                f"{EMBEDDING_DIMENSIONS}"
            )
        vectors = embedder.embed([chunk.content for chunk in chunks])
        if len(vectors) != len(chunks):
            raise IngestionError("embedding count does not match chunk count")

        inserted = 0
        cursor = self.connection.cursor()
        try:
            documents: dict[str, tuple[str, int]] = {}
            grouped: dict[str, list[tuple[int, PolicyChunk, list[float]]]] = {}
            for chunk, vector in zip(chunks, vectors):
                grouped.setdefault(chunk.external_id, []).append(
                    (chunk.chunk_index, chunk, vector)
                )
            for chunk in chunks:
                if chunk.external_id not in documents:
                    cursor.execute(
                        """
                        insert into research.documents
                            (source_id, external_id, document_type, title, language,
                             observed_at, published_at, status)
                        values (%s, %s, %s, %s, 'ko', %s, %s, 'ACTIVE')
                        on conflict (source_id, external_id) do update set
                            title = excluded.title,
                            document_type = excluded.document_type,
                            observed_at = excluded.observed_at,
                            published_at = excluded.published_at,
                            updated_at = now()
                        returning document_id, current_version
                        """,
                        (
                            str(source_id),
                            chunk.external_id,
                            chunk.document_type,
                            chunk.title,
                            chunk.observed_at,
                            chunk.published_at,
                        ),
                    )
                    row = cursor.fetchone()
                    if not row:
                        raise IngestionError("document upsert returned no row")
                    documents[chunk.external_id] = (str(row[0]), int(row[1]))

            for external_id, document_chunks in grouped.items():
                document_id, current_version = documents[external_id]
                document_hash = hashlib.sha256(
                    "|".join(chunk.content_hash for _, chunk, _ in document_chunks).encode()
                ).hexdigest()
                cursor.execute(
                    """
                    select document_version_id
                    from research.document_versions
                    where document_id = %s and content_hash = %s
                    """,
                    (document_id, document_hash),
                )
                existing = cursor.fetchone()
                if existing:
                    continue
                version = current_version + 1
                cursor.execute(
                    """
                    insert into research.document_versions
                        (document_id, version, content_hash, object_path, media_type,
                         byte_size, parser_name, parser_version, license_scope,
                         published_at, observed_at)
                    values (%s, %s, %s, %s, 'text/markdown', %s,
                            'qa-policy-ingestion', 'qa-policy-ingestion-v1',
                            %s, %s, %s)
                    returning document_version_id
                    """,
                    (
                        document_id,
                        version,
                        document_hash,
                        document_chunks[0][1].source_path,
                        sum(len(chunk.content.encode("utf-8")) for _, chunk, _ in document_chunks),
                        document_chunks[0][1].license_scope,
                        document_chunks[0][1].published_at,
                        document_chunks[0][1].observed_at,
                    ),
                )
                version_row = cursor.fetchone()
                if not version_row:
                    raise IngestionError("document version insert returned no row")
                version_id = str(version_row[0])
                for chunk_index, chunk, vector in document_chunks:
                    cursor.execute(
                        """
                        insert into research.evidence_chunks
                            (document_version_id, chunk_index, content, content_hash,
                             token_count, char_start, char_end, embedding, embedding_model,
                             embedding_version, license_scope, published_at, observed_at, metadata)
                        values (%s, %s, %s, %s, %s, 0, %s, %s::vector, %s, 'v1',
                                %s, %s, %s, %s::jsonb)
                        """,
                        (
                            version_id,
                            chunk_index,
                            chunk.content,
                            chunk.content_hash,
                            len(chunk.content.split()),
                            len(chunk.content),
                            _vector_literal(vector, embedder.dimensions),
                            os.environ.get("AGENTIC_RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
                            chunk.license_scope,
                            chunk.published_at,
                            chunk.observed_at,
                            _json(chunk.metadata),
                        ),
                    )
                cursor.execute(
                    "update research.documents set current_version = %s, updated_at = now() "
                    "where document_id = %s",
                    (version, document_id),
                )
                inserted += len(document_chunks)
            self.connection.commit()
            return inserted
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()


def _front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return {}, raw
    metadata: dict[str, str] = {}
    for line in raw[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, raw[end + 4 :].lstrip("\n")


def _chunk_text(body: str, *, max_chars: int) -> list[str]:
    sections = [part.strip() for part in re.split(r"\n(?=## )", body.strip()) if part.strip()]
    result: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            result.append(section)
            continue
        paragraphs = [part.strip() for part in section.split("\n\n") if part.strip()]
        buffer = ""
        for paragraph in paragraphs:
            if buffer and len(buffer) + len(paragraph) + 2 > max_chars:
                result.append(buffer)
                buffer = ""
            buffer = paragraph if not buffer else f"{buffer}\n\n{paragraph}"
        if buffer:
            result.append(buffer)
    return result


def _optional_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise IngestionError(f"invalid policy timestamp: {value}") from exc


def _vector_literal(vector: Sequence[float], dimensions: int) -> str:
    if len(vector) != dimensions:
        raise IngestionError("vector dimension mismatch")
    if any(not isinstance(value, (int, float)) or not isfinite(value) for value in vector):
        raise IngestionError("embedding contains a non-finite value")
    return "[" + ",".join(format(float(value), ".10g") for value in vector) + "]"


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Ingest real QA policy documents into pgvector")
    parser.add_argument("corpus_dir", type=Path)
    args = parser.parse_args()
    mode = os.environ.get("QA_INGEST_MODE", "production").strip().lower()
    chunks = build_policy_chunks(args.corpus_dir, allow_placeholders=mode == "test")
    source_id = os.environ.get("QA_POLICY_SOURCE_ID", "").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not source_id or not database_url:
        raise IngestionError("QA_POLICY_SOURCE_ID and DATABASE_URL are required")
    try:
        source_uuid = UUID(source_id)
    except ValueError as exc:
        raise IngestionError("QA_POLICY_SOURCE_ID must be a UUID") from exc
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise IngestionError("psycopg2 is required for pgvector ingestion") from exc
    connection = psycopg2.connect(database_url)
    try:
        inserted = PgvectorEvidenceRepository(connection).ingest(
            source_id=source_uuid,
            chunks=chunks,
            embedder=OpenAIEmbeddingProvider(),
        )
    finally:
        connection.close()
    print(f"qa policy ingestion complete: chunks_inserted={inserted}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
