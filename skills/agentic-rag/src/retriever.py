"""Vector DB connection and search for the agentic-rag skill.

Baseline implementation: a small local, file-persisted cosine-similarity index
over OpenAI embeddings. No server required, so this runs end-to-end today.

This is a deliberate MVP choice, not the final architecture — HEDGE_FUND_MASTER_PLAN.md
13.1 names pgvector as the target Vector Search backend. Once Supabase/pgvector is
actually provisioned (see TEAM_*_GUIDE.md), swap this module's search() implementation
for a pgvector query against `research.evidence_chunks` and keep the same interface
(DocumentChunk in, list[ScoredChunk] out) so nodes.py does not need to change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from langsmith.wrappers import wrap_openai
from openai import OpenAI

EMBEDDING_MODEL = os.environ.get("AGENTIC_RAG_EMBEDDING_MODEL", "text-embedding-3-small")
CACHE_VERSION = "v1"


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    document_type: str
    title: str
    text: str
    version: str
    effective_from: str | None
    effective_to: str | None
    source_path: str


@dataclass
class ScoredChunk:
    chunk: DocumentChunk
    score: float


def _split_front_matter(raw: str) -> tuple[dict, str]:
    """Split a corpus markdown file into (front_matter_dict, body_text)."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    fm_block = raw[3:end].strip()
    body = raw[end + 4:].lstrip("\n")
    meta = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"')
        meta[key.strip()] = None if value == "null" else value
    return meta, body


def _chunk_body(body: str, max_chars: int = 800) -> list[str]:
    """Split on markdown ## headers first, then hard-wrap long sections."""
    sections = re.split(r"\n(?=## )", body.strip())
    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append(section)
            continue
        paras = section.split("\n\n")
        buf = ""
        for para in paras:
            if len(buf) + len(para) + 2 > max_chars and buf:
                chunks.append(buf.strip())
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            chunks.append(buf.strip())
    return chunks


def load_corpus(corpus_dir: Path) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for path in sorted(corpus_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = _split_front_matter(raw)
        title_match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        for i, text in enumerate(_chunk_body(body)):
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{meta.get('document_id', path.stem)}::{i}",
                    document_id=meta.get("document_id", path.stem),
                    document_type=meta.get("document_type", "unknown"),
                    title=title,
                    text=text,
                    version=meta.get("version", "unknown"),
                    effective_from=meta.get("effective_from"),
                    effective_to=meta.get("effective_to"),
                    source_path=str(path),
                )
            )
    return chunks


class LocalVectorIndex:
    """Embeds a corpus once, caches vectors on disk, and answers cosine-sim queries."""

    def __init__(self, corpus_dir: Path, cache_path: Path | None = None):
        self.corpus_dir = corpus_dir
        self.cache_path = cache_path or corpus_dir / ".embedding_cache.json"
        self._client = wrap_openai(OpenAI())  # LANGSMITH_TRACING 켜졌을 때만 토큰/비용 추적, 꺼지면 그대로 통과
        self.chunks = load_corpus(corpus_dir)
        self._embeddings = self._load_or_build_embeddings()

    def _content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(CACHE_VERSION.encode())
        h.update(EMBEDDING_MODEL.encode())
        for c in self.chunks:
            h.update(c.chunk_id.encode())
            h.update(c.text.encode())
        return h.hexdigest()

    def _embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        return [d.embedding for d in resp.data]

    def _load_or_build_embeddings(self) -> np.ndarray:
        current_hash = self._content_hash()
        if self.cache_path.exists():
            cached = json.loads(self.cache_path.read_text())
            if cached.get("content_hash") == current_hash:
                return np.array(cached["vectors"], dtype=np.float32)
        vectors = self._embed([c.text for c in self.chunks])
        self.cache_path.write_text(
            json.dumps({"content_hash": current_hash, "vectors": vectors})
        )
        return np.array(vectors, dtype=np.float32)

    def search(self, query: str, top_k: int = 4) -> list[ScoredChunk]:
        query_vec = np.array(self._embed([query])[0], dtype=np.float32)
        sims = self._cosine_sim(self._embeddings, query_vec)
        top_idx = np.argsort(-sims)[:top_k]
        return [ScoredChunk(chunk=self.chunks[i], score=float(sims[i])) for i in top_idx]

    @staticmethod
    def _cosine_sim(matrix: np.ndarray, vec: np.ndarray) -> np.ndarray:
        matrix_norm = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-8)
        vec_norm = vec / (np.linalg.norm(vec) + 1e-8)
        return matrix_norm @ vec_norm
