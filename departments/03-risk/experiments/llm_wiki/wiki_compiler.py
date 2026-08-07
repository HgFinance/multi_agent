"""LLM 보조 위키 컴파일러 (논문 §3.1 COMPILEWIKIPAGES 방식).

data/raw/*.json 원문을 읽어 LLM으로 outgoing_links(다른 페이지와의 관계)를 추출하고,
data/wiki/<page_id>.md에 Obsidian 스타일 위키 페이지로 쓴다. 5단계 Error Book 전체를
가져오지 않는다 — 논문이 스스로 밝힌 대로 LLM 컴파일이 dangling-link·malformed-reference
오류의 주 원인이므로, Layer 1 Code Auto-fix에 해당하는 구조 검증 한 겹만 결정론
Python으로 둔다:
  1. target_page가 실제 코퍼스에 존재하지 않으면 드롭 (dangling link 방지)
  2. relation_type이 ALLOWED_RELATION_TYPES에 없으면 드롭
  3. snippet이 현재 페이지 본문에 실제로 등장하는 문자열이 아니면 드롭 (ungrounded link 방지)
LLM은 관련성 판단과 관계 서술에만 쓰고, 존재 여부·enum·grounding 판정은 항상 Python이 한다
(CLAUDE.md 원칙과 동일).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_RISK_ROOT = Path(__file__).resolve().parents[2]  # departments/03-risk
_AGENTIC_RAG_ROOT = Path(__file__).resolve().parents[4] / "skills" / "agentic-rag"
for path in (_RISK_ROOT, _AGENTIC_RAG_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.resilience import CircuitBreaker, RedisJsonCache, emit_metric  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"

ALLOWED_RELATION_TYPES = {
    "CITES",  # 이 조문이 다른 조문/규정을 인용
    "CITED_BY",  # 이 조문이 다른 문서(판례 등)에 의해 인용됨
    "PENALIZED_BY",  # 이 금지행위의 벌칙 조항
    "INTERPRETED_BY",  # 하위규정/행정규칙이 이 조문을 구체화
    "DEFINED_BY",  # 이 조문의 용어가 다른 조문에서 정의됨
    "RELATED_TO",  # 위 어느 것도 아니지만 같은 규제 맥락
}

MODEL = os.environ.get("LLM_WIKI_COMPILER_MODEL", "gpt-4o-mini")
_BREAKER = CircuitBreaker(
    "llm-wiki-compiler", failure_threshold=3, recovery_timeout_seconds=30
)
_CACHE = RedisJsonCache("risk-qa:llm-wiki:compile", ttl_seconds=7 * 24 * 3600)

_COMPILE_SYSTEM_PROMPT = (
    "You compile a single Korean financial-regulation article into a wiki page's "
    "outgoing links, in the style of an Obsidian knowledge base. You are given the "
    "article's own text and a closed list of candidate target pages (other articles/ "
    "regulations/precedents already in the corpus). "
    'Return JSON: {"outgoing_links": [{"target_page": str, "relation_type": str, '
    '"snippet": str}, ...]}. '
    "Rules: (1) target_page MUST be copied exactly from the candidate list — never "
    "invent a page that is not listed. (2) relation_type MUST be one of: "
    f"{sorted(ALLOWED_RELATION_TYPES)}. "
    "(3) snippet MUST be a short verbatim substring copied from the current article's "
    "own text that explains why the link exists — never paraphrase, never invent text "
    "not present in the article. (4) Only include a link when the current article's "
    "text actually references, is penalized by, or is directly related to the "
    "candidate page's topic. Omit weak or speculative links."
)


@dataclass(frozen=True)
class RawDoc:
    source: str
    doc_id: str
    page_id: str
    clause_id: str
    title: str
    authority: str
    effective_from: str | None
    text: str
    origin_url: str
    source_sha256: str


@dataclass(frozen=True)
class OutgoingLink:
    target_page: str
    relation_type: str
    snippet: str


def load_raw_docs(raw_dir: Path = RAW_DIR) -> list[RawDoc]:
    docs = []
    for path in sorted(raw_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        docs.append(RawDoc(**data))
    return docs


def _client() -> Any:
    from openai import OpenAI

    return OpenAI()


def _candidate_pages(current: RawDoc, all_docs: list[RawDoc]) -> list[dict[str, str]]:
    return [
        {"page_id": d.page_id, "title": d.title, "clause_id": d.clause_id}
        for d in all_docs
        if d.page_id != current.page_id
    ]


def _extract_links_llm(current: RawDoc, candidates: list[dict[str, str]]) -> list[dict]:
    """LLM 호출로 outgoing_links 후보를 뽑는다. 실패 시 빈 리스트(fail-closed)."""

    fingerprint = _CACHE.fingerprint(MODEL, current.page_id, current.source_sha256)
    cached = _CACHE.get(fingerprint)
    if isinstance(cached, list):
        emit_metric("llm_wiki_compile_cache_hit", page_id=current.page_id)
        return cached

    user_prompt = (
        f"Current article ({current.page_id}):\n{current.text}\n\n"
        f"Candidate target pages:\n{json.dumps(candidates, ensure_ascii=False)}"
    )
    try:
        response = _BREAKER.call(
            lambda: _client().chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[
                    {"role": "system", "content": _COMPILE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        emit_metric(
            "llm_wiki_compile_failure", page_id=current.page_id, error=type(exc).__name__
        )
        return []

    links = result.get("outgoing_links", [])
    if not isinstance(links, list):
        return []
    _CACHE.set(fingerprint, links)
    return links


def validate_links(
    current: RawDoc, raw_links: list[dict], valid_page_ids: set[str]
) -> list[OutgoingLink]:
    """Layer 1 구조 검증: dangling link, enum 위반, ungrounded snippet을 드롭한다."""

    validated: list[OutgoingLink] = []
    for link in raw_links:
        if not isinstance(link, dict):
            continue
        target = str(link.get("target_page", "")).strip()
        relation = str(link.get("relation_type", "")).strip()
        snippet = str(link.get("snippet", "")).strip()
        if not target or target == current.page_id:
            continue
        if target not in valid_page_ids:
            emit_metric("llm_wiki_link_dropped", reason="dangling", target=target)
            continue
        if relation not in ALLOWED_RELATION_TYPES:
            emit_metric("llm_wiki_link_dropped", reason="bad_relation_type", relation=relation)
            continue
        if not snippet or snippet not in current.text:
            emit_metric("llm_wiki_link_dropped", reason="ungrounded_snippet", target=target)
            continue
        validated.append(
            OutgoingLink(target_page=target, relation_type=relation, snippet=snippet)
        )
    return validated


def render_wiki_page(doc: RawDoc, links: list[OutgoingLink]) -> str:
    """flat frontmatter + 본문(위키링크 삽입) + grep 가능한 JSON 링크 메타데이터."""

    frontmatter_lines = [
        "---",
        f"document_id: {doc.doc_id}",
        f"page_id: {doc.page_id}",
        f"document_type: {doc.source}",
        f"title: {doc.title}",
        f"clause_id: {doc.clause_id}",
        f"authority: {doc.authority}",
        f"effective_from: {doc.effective_from or ''}",
        f"jurisdiction: KR",
        f"source_sha256: {doc.source_sha256}",
        f"origin_url: {doc.origin_url}",
        "---",
        "",
    ]
    body_lines = [f"# {doc.page_id}", "", doc.text, ""]
    if links:
        body_lines.append("## 관련 조항")
        body_lines.append("")
        for link in links:
            body_lines.append(f"- [[{link.target_page}]] ({link.relation_type}): {link.snippet}")
        body_lines.append("")

    link_metadata = {
        "current_page": doc.page_id,
        "outgoing_links": [
            {
                "target_page": link.target_page,
                "relation_type": link.relation_type,
                "snippet": link.snippet,
            }
            for link in links
        ],
    }
    metadata_block = [
        "```json",
        json.dumps(link_metadata, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(frontmatter_lines + body_lines + metadata_block)


def compile_corpus(
    raw_dir: Path = RAW_DIR, wiki_dir: Path = WIKI_DIR
) -> dict[str, int]:
    docs = load_raw_docs(raw_dir)
    valid_page_ids = {d.page_id for d in docs}
    wiki_dir.mkdir(parents=True, exist_ok=True)

    total_links = 0
    for doc in docs:
        candidates = _candidate_pages(doc, docs)
        raw_links = _extract_links_llm(doc, candidates)
        links = validate_links(doc, raw_links, valid_page_ids)
        total_links += len(links)
        page_text = render_wiki_page(doc, links)
        (wiki_dir / f"{doc.page_id}.md").write_text(page_text, encoding="utf-8")

    return {"pages": len(docs), "links": total_links}


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    stats = compile_corpus()
    print(f"위키 컴파일 완료: {stats['pages']}개 페이지, {stats['links']}개 검증된 링크 -> {WIKI_DIR}")


if __name__ == "__main__":
    main()
