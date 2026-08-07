"""3개 비교 Arm 진입점 — plain RAG(A) vs LLM-wiki BM25-only(B) vs LLM-wiki grep-first(C).

세 Arm 모두 같은 원본 코퍼스(law.go.kr 실측 조문, data/raw/*.json)에서 출발하고,
같은 persona 프롬프트(`PERSONA_PROMPTS["compliance-policy-agent"]`)와 같은
verdict JSON shape({"verdict","cited_documents","rationale","confidence","escalate"})를
써서 F1/EM으로 공정 비교한다.

Arm A는 `skills/agentic-rag/src/graph.py`의 `run_compliance_check`를 그대로 재사용한다
(청킹 방식만 이 실험의 flat corpus로 새로 만듦 — retriever.py의 frontmatter 계약을
그대로 지킨다). Arm B/C는 seed 탐색(bm25.py/grep_seed.py) → wiki_reader.py의 bounded
read → 아래 `_generate_verdict`(nodes.py와 동일한 CircuitBreaker/RedisJsonCache 패턴을
독립적으로 재사용 — private 함수를 다른 모듈 밖에서 끌어쓰지 않는다)로 이어진다.

# ponytail: PIT(Point-in-Time) 필터는 Arm A(nodes.py의 retrieve_node)에만 있고 Arm B/C
# 위키 리더에는 없다. 이번 golden set 코퍼스는 전부 이미 시행 중인 조문만 써서 결과에
# 영향이 없지만, 미래 시행일 조문이 코퍼스에 들어가면 wiki_reader에도 as_of 필터를
# 추가해야 한다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # data/*.py 형제 모듈(bm25 등)
_RISK_ROOT = Path(__file__).resolve().parents[2]  # departments/03-risk
_AGENTIC_RAG_ROOT = Path(__file__).resolve().parents[4] / "skills" / "agentic-rag"
for path in (_RISK_ROOT, _AGENTIC_RAG_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.graph import run_compliance_check  # noqa: E402
from src.nodes import MAX_CONTEXT_CHARS, PERSONA_PROMPTS  # noqa: E402
from src.resilience import CircuitBreaker, RedisJsonCache, emit_metric  # noqa: E402

from bm25 import BM25Index  # noqa: E402
from grep_seed import grep_seed  # noqa: E402
from wiki_reader import read_bounded  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"
FLAT_CORPUS_DIR = Path(__file__).resolve().parent / "data" / "flat_corpus"

PERSONA = "compliance-policy-agent"
MODEL = os.environ.get("LLM_WIKI_GENERATE_MODEL", "gpt-4o-mini")
_BREAKER = CircuitBreaker("llm-wiki-arms", failure_threshold=3, recovery_timeout_seconds=30)
_CACHE = RedisJsonCache("risk-qa:llm-wiki:generate", ttl_seconds=7 * 24 * 3600)


def _wrap_query(query: str, mandate: str) -> str:
    return f"{mandate}\n\n{query}" if mandate else query


def build_flat_corpus(raw_dir: Path = RAW_DIR, out_dir: Path = FLAT_CORPUS_DIR) -> Path:
    """data/raw/*.json -> retriever.py의 flat frontmatter 코퍼스 계약(Arm A 전용)."""

    out_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(raw_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        frontmatter = "\n".join(
            [
                "---",
                f"document_id: {doc['doc_id']}",
                f"document_type: {doc['source']}",
                f"version: {doc['effective_from'] or 'unknown'}",
                f"effective_from: {doc['effective_from'] or ''}",
                "effective_to:",
                "---",
                "",
            ]
        )
        body = f"# {doc['title']}\n\n{doc['text']}\n"
        (out_dir / f"{doc['page_id']}.md").write_text(frontmatter + body, encoding="utf-8")
    return out_dir


def plain_rag_answer(query: str, as_of: str, mandate: str = "") -> dict[str, Any]:
    """Arm A — 기존 flat-chunk RAG 그래프를 이 실험의 실측 코퍼스로 그대로 재사용.

    context_chars는 nodes.py의 실제 chunk 텍스트를 그대로 재노출하지 않는
    run_compliance_check() 반환값 한도 안에서의 근사치다 — relevant_documents 개수 x
    800자(_chunk_body의 chunk 상한)로 상한을 추정한다(exact 아님, 참고 지표용).
    """

    if not FLAT_CORPUS_DIR.exists() or not any(FLAT_CORPUS_DIR.glob("*.md")):
        build_flat_corpus()
    result = run_compliance_check(
        _wrap_query(query, mandate), as_of, corpus_dir=FLAT_CORPUS_DIR, persona=PERSONA
    )
    estimated_chars = min(len(result["relevant_documents"]) * 800, MAX_CONTEXT_CHARS)
    return {
        "answer": result["answer"],
        "context_chars": estimated_chars,
        "pages_visited": [d["document_id"] for d in result["relevant_documents"]],
    }


def _bm25_index() -> BM25Index:
    documents = {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(WIKI_DIR.glob("*.md"))
    }
    return BM25Index(documents)


def _generate_verdict(query: str, context: str) -> dict[str, Any]:
    """wiki_reader의 bounded context로 verdict JSON을 만든다. 빈 컨텍스트는 fail-closed."""

    prompts = PERSONA_PROMPTS[PERSONA]
    if not context.strip():
        return {
            "verdict": prompts["no_evidence_verdict"],
            "cited_documents": [],
            "rationale": prompts["no_evidence_rationale"],
            "confidence": 0.0,
            "escalate": True,
        }

    system = prompts["generate_system"]
    user = f"{prompts['query_label']}:\n{query}\n\n{prompts['docs_label']}:\n{context}"
    fingerprint = _CACHE.fingerprint(MODEL, system, user)
    cached = _CACHE.get(fingerprint)
    if isinstance(cached, dict):
        emit_metric("llm_wiki_generate_cache_hit")
        return cached

    try:
        from openai import OpenAI

        response = _BREAKER.call(
            lambda: OpenAI().chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        emit_metric("llm_wiki_generate_failure", error=type(exc).__name__)
        return {
            "verdict": prompts["no_evidence_verdict"],
            "cited_documents": [],
            "rationale": f"External model unavailable ({type(exc).__name__}); deterministic escalation required.",
            "confidence": 0.0,
            "escalate": True,
        }

    _CACHE.set(fingerprint, result)
    return result


def llm_wiki_bm25_answer(query: str, as_of: str, mandate: str = "") -> dict[str, Any]:
    """Arm B — BM25 단독 seed → wiki_reader bounded read → generate."""

    full_query = _wrap_query(query, mandate)
    top = _bm25_index().score(full_query, top_k=1)
    seeds = [page_id for page_id, _score in top]
    read = read_bounded(full_query, seeds)
    return {
        "answer": _generate_verdict(full_query, read.context),
        "context_chars": len(read.context),
        "pages_visited": read.pages_visited,
    }


def llm_wiki_grep_bm25_answer(query: str, as_of: str, mandate: str = "") -> dict[str, Any]:
    """Arm C — grep-first(조항번호 정확 매칭), 실패 시 BM25 폴백 → bounded read → generate."""

    full_query = _wrap_query(query, mandate)
    seeds = grep_seed(full_query)
    if not seeds:
        top = _bm25_index().score(full_query, top_k=1)
        seeds = [page_id for page_id, _score in top]
    read = read_bounded(full_query, seeds)
    return {
        "answer": _generate_verdict(full_query, read.context),
        "context_chars": len(read.context),
        "pages_visited": read.pages_visited,
    }


if __name__ == "__main__":
    built = build_flat_corpus()
    flat_files = sorted(p.stem for p in built.glob("*.md"))
    assert flat_files, "flat corpus가 비어 있음"
    assert "자본시장법_제178조_부정거래행위등의금지" in flat_files, flat_files

    index = _bm25_index()
    top = index.score("제178조 부정거래행위 부정한 수단")
    assert top and top[0][0] == "자본시장법_제178조_부정거래행위등의금지", top

    empty_answer = _generate_verdict("아무 질문", "")
    assert empty_answer["verdict"] == PERSONA_PROMPTS[PERSONA]["no_evidence_verdict"]
    assert empty_answer["escalate"] is True

    print("arms self-check OK:", flat_files[:3], "...")
