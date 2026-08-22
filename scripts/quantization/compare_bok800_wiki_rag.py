#!/usr/bin/env python3
"""Compare source-preserving Wiki retrieval with deterministic glossary hits.

This is a retrieval-only diagnostic.  It deliberately does not call a model or
claim a quality score.  It uses the same frozen questions for both retrieval
paths and records the context candidates that would be sent to the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.quantization.glossary_rag import inject, load_glossary


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.casefold())


class BM25:
    def __init__(self, documents: dict[str, str]) -> None:
        self.ids = list(documents)
        self.tokens = [tokenize(documents[key]) for key in self.ids]
        self.lengths = [len(row) for row in self.tokens]
        self.avg_length = sum(self.lengths) / len(self.lengths) if self.lengths else 1
        self.term_freq = [Counter(row) for row in self.tokens]
        self.doc_freq = Counter()
        for row in self.term_freq:
            self.doc_freq.update(row.keys())

    def search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        query_terms = tokenize(query)
        if not query_terms:
            return []
        scores: list[tuple[str, float]] = []
        n_docs = len(self.ids)
        for index, frequencies in enumerate(self.term_freq):
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                df = self.doc_freq[term]
                idf = max(0.0, __import__("math").log(1 + (n_docs - df + 0.5) / (df + 0.5)))
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * self.lengths[index] / self.avg_length)
                score += idf * frequency * 2.5 / denominator
            if score > 0:
                scores.append((self.ids[index], score))
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return scores[:top_k]


def load_wiki_documents(root: Path) -> dict[str, str]:
    documents: dict[str, str] = {}
    for path in sorted((root / "wiki" / "entities").glob("*.md")):
        documents[path.stem] = path.read_text(encoding="utf-8")
    return documents


def load_queries(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        return [case for case in payload["cases"] if isinstance(case, dict)]
    if isinstance(payload, list):
        return [case for case in payload if isinstance(case, dict)]
    raise ValueError("query dataset must be a list or an object containing cases")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root", type=Path, required=True)
    parser.add_argument("--queries", type=Path, default=Path("benchmarks/quantization/internal50_v2_reasoning.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    wiki_documents = load_wiki_documents(args.knowledge_root)
    wiki_index = BM25(wiki_documents)
    glossary_path = args.knowledge_root / "glossary_rag_v1.json"
    glossary_hash, glossary_entries = load_glossary(glossary_path)
    queries = load_queries(args.queries)
    rows: list[dict[str, Any]] = []
    wiki_nonempty = 0
    rag_nonempty = 0
    for case in queries:
        query = str(case.get("question", case.get("query", "")))
        case_id = str(case.get("id", len(rows) + 1))

        wiki_started = time.perf_counter()
        wiki_hits = wiki_index.search(query, top_k=3)
        wiki_latency_ms = round((time.perf_counter() - wiki_started) * 1000, 4)

        rag_started = time.perf_counter()
        _injected, matched_terms = inject(query, glossary_entries)
        rag_latency_ms = round((time.perf_counter() - rag_started) * 1000, 4)
        if wiki_hits:
            wiki_nonempty += 1
        if matched_terms:
            rag_nonempty += 1
        rows.append(
            {
                "id": case_id,
                "question": query,
                "wiki": {
                    "hit": bool(wiki_hits),
                    "latency_ms": wiki_latency_ms,
                    "top_k": [{"page": page, "score": round(score, 6)} for page, score in wiki_hits],
                },
                "rag": {
                    "hit": bool(matched_terms),
                    "latency_ms": rag_latency_ms,
                    "matched_terms": matched_terms,
                    "glossary_sha256": glossary_hash,
                },
                "model_quality": None,
            }
        )

    result = {
        "schema_version": "bok800-wiki-rag-retrieval-comparison.v1",
        "status": "RETRIEVAL_ONLY",
        "same_questions": True,
        "query_dataset": str(args.queries),
        "query_dataset_sha256": hashlib.sha256(args.queries.read_bytes()).hexdigest(),
        "knowledge_root": str(args.knowledge_root),
        "wiki_documents": len(wiki_documents),
        "glossary_entries": len(glossary_entries),
        "glossary_sha256": glossary_hash,
        "summary": {
            "cases": len(rows),
            "wiki_nonempty_hits": wiki_nonempty,
            "wiki_hit_rate": wiki_nonempty / len(rows) if rows else None,
            "rag_nonempty_hits": rag_nonempty,
            "rag_hit_rate": rag_nonempty / len(rows) if rows else None,
        },
        "quality_scores": None,
        "rows": rows,
        "next_step": "Run model responses with the same base model, prompt, and runtime before comparing quality.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = [
        "# BOK 800 Wiki vs RAG Retrieval Diagnostic",
        "",
        "> Retrieval-only diagnostic. No model quality score is claimed.",
        "",
        f"- Questions: `{len(rows)}`",
        f"- Wiki documents: `{len(wiki_documents)}`",
        f"- RAG glossary entries: `{len(glossary_entries)}`",
        f"- Glossary SHA256: `{glossary_hash}`",
        f"- Wiki non-empty hit rate: `{result['summary']['wiki_hit_rate']}`",
        f"- RAG non-empty hit rate: `{result['summary']['rag_hit_rate']}`",
        "",
        "| Question | Wiki top page | RAG matched terms | Model quality |",
        "|---|---|---|---|",
    ]
    for row in rows:
        wiki_top = row["wiki"]["top_k"][0]["page"] if row["wiki"]["top_k"] else "MISS"
        terms = ", ".join(row["rag"]["matched_terms"]) or "MISS"
        md.append(f"| {row['id']} | `{wiki_top}` | `{terms}` | NOT EXECUTED |")
    md.extend(["", "Quality remains unexecuted until both paths run against the same AWQ endpoint.", ""])
    args.output.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
