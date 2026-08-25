#!/usr/bin/env python3
"""Bounded BOK-800 Wiki retrieval for the Hybrid quality benchmark.

The retriever deliberately indexes only the entity title, source definition,
and related-search terms.  Repeated frontmatter/source boilerplate would make
the retrieval-only hit rate look high while returning irrelevant pages.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}", re.UNICODE)
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n\n(.*)$", re.DOTALL)
SECTION_RE = re.compile(r"^## (.+?)\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "in", "is", "it", "of", "on",
    "or", "that", "the", "their", "this", "to", "was", "were", "what", "when",
    "which", "who", "with", "would", "year", "years",
}


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.casefold()) if token not in STOP_WORDS]


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    term: str
    definition: str
    related_terms: tuple[str, ...]
    source_page: int | None

    @property
    def index_text(self) -> str:
        return "\n".join((self.term, self.definition, *self.related_terms))


@dataclass(frozen=True)
class WikiHit:
    page_id: str
    term: str
    score: float
    relation: str


def _frontmatter_value(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", frontmatter, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _section(body: str, name: str) -> str:
    for match in SECTION_RE.finditer(body):
        if match.group(1).strip() == name:
            return match.group(2).strip()
    return ""


def _parse_page(path: Path) -> WikiPage:
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"invalid BOK Wiki page: {path}")
    frontmatter, body = match.groups()
    term = _frontmatter_value(frontmatter, "term") or path.stem
    source_page_raw = _frontmatter_value(frontmatter, "source_pdf_page")
    related = tuple(
        line[2:].strip()
        for line in _section(body, "연관검색어").splitlines()
        if line.startswith("- ") and line[2:].strip() and line[2:].strip() != "없음"
    )
    return WikiPage(
        page_id=path.stem,
        term=term,
        definition=_section(body, "원문 기반 정의"),
        related_terms=related,
        source_page=int(source_page_raw) if source_page_raw and source_page_raw.isdigit() else None,
    )


class Bok800WikiIndex:
    """Pure-Python BM25 seed retrieval plus bounded related-term traversal."""

    def __init__(self, pages: list[WikiPage], digest: str) -> None:
        self.pages = {page.page_id: page for page in pages}
        self.digest = digest
        self.ids = sorted(self.pages)
        self.tokens = [tokenize(self.pages[page_id].index_text) for page_id in self.ids]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg_length = sum(self.lengths) / len(self.lengths) if self.lengths else 1.0
        self.term_freq = [Counter(tokens) for tokens in self.tokens]
        self.doc_freq: Counter[str] = Counter()
        for frequencies in self.term_freq:
            self.doc_freq.update(frequencies.keys())
        self.term_to_page: dict[str, str] = {}
        for page in pages:
            self.term_to_page.setdefault(page.term.casefold(), page.page_id)

    def has_exact_term(self, query: str) -> bool:
        """Accept spacing/punctuation differences in a planned Korean term."""

        compact_query = "".join(TOKEN_RE.findall(query.casefold()))
        return any(
            bool(compact_term)
            and compact_term in compact_query
            for page in self.pages.values()
            for compact_term in ["".join(TOKEN_RE.findall(page.term.casefold()))]
        )

    def search(self, query: str, *, top_k: int = 1) -> list[tuple[str, float]]:
        query_terms = tokenize(query)
        if not query_terms:
            return []
        scored: list[tuple[str, float]] = []
        n_docs = len(self.ids)
        for index, frequencies in enumerate(self.term_freq):
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                df = self.doc_freq[term]
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * self.lengths[index] / self.avg_length
                )
                score += idf * frequency * 2.5 / denominator
            if score > 0:
                scored.append((self.ids[index], score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]

    def retrieve(self, query: str, *, top_k: int = 1, max_pages: int = 3) -> list[WikiHit]:
        seeds = self.search(query, top_k=top_k)
        queue: deque[tuple[str, float, str]] = deque(
            (page_id, score, "bm25_seed") for page_id, score in seeds
        )
        hits: list[WikiHit] = []
        visited: set[str] = set()
        while queue and len(hits) < max_pages:
            page_id, score, relation = queue.popleft()
            if page_id in visited:
                continue
            visited.add(page_id)
            page = self.pages[page_id]
            hits.append(WikiHit(page_id, page.term, score, relation))
            for related_term in page.related_terms:
                related_id = self.term_to_page.get(related_term.casefold())
                if related_id and related_id not in visited:
                    queue.append((related_id, 0.0, f"related_from:{page.term}"))
        return hits

    def inject(
        self,
        prompt: str,
        *,
        query: str,
        top_k: int = 1,
        max_pages: int = 3,
        max_chars_per_page: int = 600,
    ) -> tuple[str, dict[str, object]]:
        started = time.perf_counter()
        hits = self.retrieve(query, top_k=top_k, max_pages=max_pages)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 4)
        if not hits:
            return prompt, {
                "hit": False,
                "sha256": self.digest,
                "latency_ms": elapsed_ms,
                "pages": [],
                "terms": [],
                "context_chars": 0,
            }
        chunks = ["BOK800 WIKI (bounded retrieval; use only when relevant):"]
        for hit in hits:
            page = self.pages[hit.page_id]
            definition = page.definition[:max_chars_per_page].strip()
            chunks.append(
                f"- {page.term} [PDF p.{page.source_page}; {hit.relation}]: {definition}"
            )
        context = "\n".join(chunks)
        return f"{context}\n\n{prompt}", {
            "hit": True,
            "sha256": self.digest,
            "latency_ms": elapsed_ms,
            "pages": [
                {
                    "page_id": hit.page_id,
                    "term": hit.term,
                    "score": round(hit.score, 6),
                    "relation": hit.relation,
                }
                for hit in hits
            ],
            "terms": [hit.term for hit in hits],
            "context_chars": len(context),
        }


def load_bok800_wiki(root: Path) -> Bok800WikiIndex:
    paths = sorted((root / "wiki" / "entities").glob("*.md"))
    if not paths:
        raise ValueError(f"BOK Wiki entity pages not found under {root}")
    digest = hashlib.sha256()
    pages: list[WikiPage] = []
    for path in paths:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
        pages.append(_parse_page(path))
    return Bok800WikiIndex(pages, digest.hexdigest())
