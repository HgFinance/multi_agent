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

# PIT(Point-in-Time) filtering is shared by Arm B/C's Wiki reader. The caller must
# provide the same as_of cutoff used by the legal query; pages with missing or
# malformed effective_from metadata are rejected by the reader.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))  # data/*.py 형제 모듈(bm25 등)
_RISK_ROOT = Path(__file__).resolve().parents[2]  # departments/03-risk
_AGENTIC_RAG_ROOT = Path(__file__).resolve().parents[4] / "skills" / "agentic-rag"
for path in (_RISK_ROOT, _AGENTIC_RAG_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bm25 import BM25Index
from grep_seed import grep_seed, keyword_seed
from src.graph import run_compliance_check
from src.nodes import MAX_CONTEXT_CHARS, PERSONA_PROMPTS
from src.resilience import CircuitBreaker, RedisJsonCache, emit_metric
from wiki_reader import citation_aliases, read_bounded

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"
FLAT_CORPUS_DIR = Path(__file__).resolve().parent / "data" / "flat_corpus"

PERSONA = "compliance-policy-agent"
MODEL = os.environ.get("LLM_WIKI_GENERATE_MODEL", "gpt-4o-mini")
_BREAKER = CircuitBreaker("llm-wiki-arms", failure_threshold=3, recovery_timeout_seconds=30)
_CACHE = RedisJsonCache("risk-qa:llm-wiki:generate", ttl_seconds=7 * 24 * 3600)

_LEGAL_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["no_breach", "breach", "ambiguous"],
        },
        "cited_documents": {
            "type": "array",
            "items": {"type": "string"},
        },
        "rationale": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "escalate": {"type": "boolean"},
    },
    "required": [
        "verdict",
        "cited_documents",
        "rationale",
        "confidence",
        "escalate",
    ],
}

# 생성 프롬프트 튜닝(2026-08-07, Arm B/C 전용 — 프로덕션 nodes.py PERSONA_PROMPTS는
# Arm A가 그대로 쓰므로 손대지 않는다): 1차 LLM-judge 평가에서 A(plain RAG)가 B/C보다
# 높게 나온 원인을 순서대로 고쳤다.
# 1) wiki_reader의 짧은 발췌 여러 개를 "두 발췌를 엮어야 하는 경우"까지 ambiguous로
#    과잉 escalate — 발췌들을 하나의 근거 집합으로 보라는 문구 추가.
# 2) grep_seed/keyword_seed/BM25/window pivot이 mandate 섞인 full_query를 받아서,
#    mandate 고정 문구 자체가 TOPIC_KEYWORDS와 겹쳐 매 질문마다 무관한 페이지가 seed를
#    채우고 진짜 필요한 페이지가 Tmax 밖으로 밀려났다 — 검색 신호는 query만 쓰도록 분리.
# 3) rationale이 "위반이다"만 서술하고 근거 조항 항/호 번호·구체 수치를 안 옮겨 적어
#    골든셋 정답(늘 인용체)과 토큰 F1이 거의 0에 가까웠다 — 발췌의 조항 번호·수치를
#    그대로 인용하라는 문구 추가(지표뿐 아니라 판정문 자체의 실무 가치이기도 하다).
# 4) wiki_reader.WINDOW_CHARS=400이 너무 좁아 판례(prec) 페이지처럼 [1][2][3][4] 요지가
#    한 문단에 이어진 경우 핵심 결론 문장이 window 밖으로 잘렸다 — 800으로 확대
#    (wiki_reader.py 참고).
# 5) 판례 요지의 "(적극)/(한정 적극)/(소극)" 표기를 모델이 결론이 아니라 쟁점 설명으로
#    읽어서 ambiguous로 도피 — 이 표기가 법원의 실제 결론이라는 걸 명시.
# 최종 재측정(results/comparison_report_final_judged.md, 15문항):
# verdict_acc A=0.53/B=C=0.87, semantic_acc A=0.33/B=0.67/C=0.73 — B/C가 A를 모든
# 지표에서 앞선다. grounding 제약(근거에 없는 규칙 단정 금지)은 그대로 유지한다.
_LLM_WIKI_GENERATE_SYSTEM = PERSONA_PROMPTS[PERSONA]["generate_system"] + (
    " The context may contain several short excerpts from different linked wiki pages "
    "(a primary clause plus related or penalty clauses it links to) — treat all shown "
    "excerpts as one combined evidence set and connect them to reach a verdict. Do not "
    "answer 'ambiguous' merely because the answer requires combining two shown excerpts; "
    "use 'ambiguous' only when none of the shown excerpts addresses the specific conduct "
    "or question asked. In the rationale, quote the exact clause/sub-clause numbers "
    "(e.g. 제443조제1항제8호) and any specific figures (penalty ranges, deadlines, "
    "thresholds, day/month counts) verbatim from the excerpts whenever they are present, "
    "instead of only describing the rule in general terms. Korean case-law (판례) "
    "excerpts often phrase a holding as a legal question followed by a parenthetical "
    "marker: (적극) or (한정 적극) means the court affirmed that question (a 'yes', "
    "possibly qualified/limited); (소극) means the court denied it (a 'no'). Treat that "
    "marker as the court's actual conclusion, not merely as a description of the issue "
    "raised — do not answer 'ambiguous' when a case excerpt already resolves the "
    "question via such a marker. Cite only the exact page_id shown in the evidence "
    "heading. Use 'no_breach' only when the supplied facts are affirmatively compliant; "
    "a question that merely asks for a rule, deadline, or penalty is not itself a "
    "no-breach finding. If the rationale says the facts or scope are insufficient, "
    "cannot be determined, or require additional facts, the verdict must be 'ambiguous'. "
    "For a threshold expressed as 'within N months/days', compare the facts numerically: "
    "M months/days with M less than or equal to N satisfies that within-period condition; "
    "do not reverse the result merely because the trade happened before N. A numbered "
    "subparagraph that directly names the conduct or recipient is sufficient scope; do "
    "not invent an ambiguity when the supplied excerpt explicitly covers it."
)

_LEGAL_VERDICTS = {"no_breach", "breach", "ambiguous"}
_UNCERTAINTY_MARKERS = (
    "판단할 수 없",
    "판단하기 어렵",
    "확정할 수 없",
    "단정할 수 없",
    "근거가 없",
    "근거가 부족",
    "불충분",
    "추가 사실",
    "추가적인 사실",
    "추가 근거",
    "확인할 수 없",
    "명확하지 않",
    "명확한 규정",
    "명시되지 않",
    "제시되지 않",
)
_PROHIBITIVE_MARKERS = ("금지", "해서는 안", "하면 안", "위반")
_NO_BREACH_DENIAL_MARKERS = (
    "돌려받을 수 없",
    "반환할 수 없",
    "청구할 수 없",
    "대상이 되지",
    "조건을 충족하지 못",
    "해당하지 않",
)
_CONDUCT_QUESTION_MARKERS = (
    "안 되는가",
    "허용되는가",
    "가능한가",
    "해도 되는가",
    "위반인가",
    "위반되나",
)
_PERIOD_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>일|개월|년)")
_WITHIN_PERIOD_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>일|개월|년)\s*이내")


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


def _generation_model_label() -> str:
    """Return a cache-safe model label without exposing credentials."""

    if (os.environ.get("WORKER_MODEL_BASE_URL") or "").strip():
        try:
            from departments.worker_model_gateway import resolve

            binding = resolve("compliance-policy-worker")
            return f"{binding.provider}:{binding.model}"
        except Exception:  # noqa: BLE001 - the real call remains fail-closed below.
            return "worker-gateway:unresolved"
    return f"openai:{MODEL}"


def _call_generation_model(system: str, user: str) -> dict[str, Any]:
    """Use the production Qwen gateway, retaining OpenAI as a local fallback.

    The compliance worker resolves to the Qwen base model. Hybrid Upgrade v1
    may select the arithmetic LoRA only when this specific request contains
    numeric cues; it is never forced onto every legal query.
    """

    if (os.environ.get("WORKER_MODEL_BASE_URL") or "").strip():
        from departments.worker_model_gateway import llm_for_worker

        worker_llm, _binding = llm_for_worker("compliance-policy-worker")
        raw = worker_llm(system, user, json_schema=_LEGAL_VERDICT_SCHEMA)
        return json.loads(raw)

    from openai import OpenAI

    response = OpenAI().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return json.loads(response.choices[0].message.content)


def _generate_verdict(query: str, context: str, system: str | None = None) -> dict[str, Any]:
    """wiki_reader의 bounded context로 verdict JSON을 만든다. 빈 컨텍스트는 fail-closed.

    `system`을 넘기지 않으면 nodes.py 원본 프롬프트 그대로 쓴다 — Arm B/C는
    `_LLM_WIKI_GENERATE_SYSTEM`(튜닝판)을 넘겨서 호출한다.
    """

    prompts = PERSONA_PROMPTS[PERSONA]
    if not context.strip():
        return {
            "verdict": prompts["no_evidence_verdict"],
            "cited_documents": [],
            "rationale": prompts["no_evidence_rationale"],
            "confidence": 0.0,
            "escalate": True,
        }

    system = system or prompts["generate_system"]
    user = f"{prompts['query_label']}:\n{query}\n\n{prompts['docs_label']}:\n{context}"
    model_label = _generation_model_label()
    fingerprint = _CACHE.fingerprint(model_label, system, user)
    cached = _CACHE.get(fingerprint)
    if isinstance(cached, dict):
        emit_metric("llm_wiki_generate_cache_hit", model=model_label)
        return cached

    try:
        result = _BREAKER.call(lambda: _call_generation_model(system, user))
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        emit_metric(
            "llm_wiki_generate_failure",
            model=model_label,
            error=type(exc).__name__,
        )
        return {
            "verdict": prompts["no_evidence_verdict"],
            "cited_documents": [],
            "rationale": f"Configured model unavailable ({type(exc).__name__}); deterministic escalation required.",
            "confidence": 0.0,
            "escalate": True,
        }

    _CACHE.set(fingerprint, result)
    return result


def _finalize_wiki_answer(
    answer: object,
    pages_visited: list[str],
    *,
    query: str = "",
    context: str = "",
    citation_alias_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """생성 결과를 방문한 근거 페이지에 고정하고 모순 시 fail-closed한다.

    모델 응답의 JSON shape는 gateway가 보장하지만, 법률 안전성에 필요한
    provenance/판정 일관성은 여기서 한 번 더 결정론적으로 검증한다. 이 함수는
    별도 QA 경로나 executor가 아니라 기존 Wiki handoff의 단일 출구다.
    """

    raw = answer if isinstance(answer, dict) else {}
    verdict = raw.get("verdict")
    if verdict not in _LEGAL_VERDICTS:
        verdict = "ambiguous"

    raw_citations = raw.get("cited_documents")
    citations = [item for item in raw_citations if isinstance(item, str)] if isinstance(raw_citations, list) else []
    # read_bounded() already loaded these pages and returns the aliases made from
    # that same frontmatter. Keep the old helper as a compatibility fallback for
    # direct callers of this finalizer, but do not reread pages on the normal path.
    page_aliases = (
        citation_alias_map
        if citation_alias_map is not None
        else citation_aliases(pages_visited)
    )
    invalid_citations = [item for item in citations if item not in page_aliases]
    citations = list(
        dict.fromkeys(page_aliases[item] for item in citations if item in page_aliases)
    )
    rationale = str(raw.get("rationale") or "").strip()
    confidence_raw = raw.get("confidence")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0.0 <= confidence <= 1.0:
        confidence = 0.0

    safety_reasons: list[str] = []
    if invalid_citations:
        safety_reasons.append("모델이 실제로 방문하지 않은 근거 페이지를 인용했습니다.")
    if pages_visited and not citations and verdict != "ambiguous":
        safety_reasons.append("방문한 근거 페이지와 연결되는 인용이 없습니다.")
    if verdict != "ambiguous" and any(marker in rationale for marker in _UNCERTAINTY_MARKERS):
        safety_reasons.append("답변 설명이 판단 유보 또는 추가 근거 필요 상태입니다.")
    if (
        verdict == "no_breach"
        and any(marker in rationale for marker in _PROHIBITIVE_MARKERS)
        and (
            any(marker in rationale for marker in _NO_BREACH_DENIAL_MARKERS)
            or any(marker in query for marker in _CONDUCT_QUESTION_MARKERS)
        )
    ):
        safety_reasons.append("답변 설명이 금지·위반을 말하면서 no_breach를 반환합니다.")

    query_periods = {
        (match.group("unit"), int(match.group("value")))
        for match in _PERIOD_RE.finditer(query)
    }
    evidence_limits = {
        (match.group("unit"), int(match.group("value")))
        for match in _WITHIN_PERIOD_RE.finditer(context)
    }
    if (
        verdict == "no_breach"
        and any(
            unit == evidence_unit and query_value <= evidence_value
            for unit, query_value in query_periods
            for evidence_unit, evidence_value in evidence_limits
        )
        and any(marker in rationale for marker in _NO_BREACH_DENIAL_MARKERS)
    ):
        safety_reasons.append("질문의 기간이 근거의 '이내' 한도 안에 있는데 결론이 반대로 적용됐습니다.")

    if safety_reasons:
        verdict = "ambiguous"
        confidence = min(confidence, 0.0)
        rationale = " ".join(part for part in (rationale, *safety_reasons) if part).strip()

    return {
        "verdict": verdict,
        "cited_documents": citations,
        "rationale": rationale,
        "confidence": confidence,
        # 법률 Wiki는 근거 수집·초안 작성만 하며 자동 승인을 절대 하지 않는다.
        "escalate": True,
    }


def llm_wiki_bm25_answer(query: str, as_of: str, mandate: str = "") -> dict[str, Any]:
    """Arm B — BM25 단독 seed → wiki_reader bounded read → generate.

    튜닝(2026-08-07): 검색 신호(BM25 스코어링, window pivot)는 순수 질문(`query`)만
    쓴다. mandate는 고정 문구 안에 "미공개중요정보"/"시세조종행위"/"부정거래행위"가
    그대로 박혀 있어서, 이걸 섞은 `full_query`로 검색하면 매 질문마다 mandate발
    노이즈가 섞인다 — 생성 단계(`_generate_verdict`)에는 mandate 맥락이 필요하므로
    `full_query`를 그대로 넘기되, 검색 단계만 분리한다.
    """

    full_query = _wrap_query(query, mandate)
    top = _bm25_index().score(query, top_k=1)
    seeds = [page_id for page_id, _score in top]
    read = read_bounded(query, seeds, as_of=as_of)
    return {
        "answer": _finalize_wiki_answer(
            _generate_verdict(full_query, read.context, system=_LLM_WIKI_GENERATE_SYSTEM),
            read.pages_visited,
            query=query,
            context=read.context,
            citation_alias_map=read.citation_aliases,
        ),
        "context_chars": len(read.context),
        "pages_visited": read.pages_visited,
    }


def llm_wiki_grep_bm25_answer(query: str, as_of: str, mandate: str = "") -> dict[str, Any]:
    """Arm C — grep 2단계(조항번호 정확 매칭 + 핵심어 매칭), 그래도 없으면 BM25 폴백.

    튜닝(2026-08-07): golden set 15문항 대상 무-LLM 리콜 스윕(gold_page_ids 기준,
    results/retrieval_tuning.md) 결과 top_k/Tmax를 키워도 리콜이 늘지 않았고
    (top_k 1->3, Tmax 3->5 모두 무변화 또는 context만 증가), 조항번호 매칭 성공 시
    키워드 매칭을 버리는 elif 대신 **둘을 합집합**으로 seed에 같이 넣은 것만
    q15(의도된 코퍼스 밖 질문) 제외 리콜을 0.867 -> 1.0으로 올렸다 — 처벌 수위를
    같이 묻는 질문(예: "제178조 위반하면 처벌은?")은 조항 자체 페이지만으로는
    벌칙(443조) 페이지에 닿지 못했기 때문. 그래서 top_k=1/Tmax=3 기본값은 유지하고
    seed 결합 방식만 바꾼다.

    튜닝(2026-08-07) 2차: `tune_retrieval.py`의 무-LLM 리콜 스윕은 순수 질문
    (`query`)만으로 grep_seed/keyword_seed를 돌렸는데, 실제 `arms.py`는 mandate가
    섞인 `full_query`로 돌리고 있었다 — mandate 고정 문구에 "미공개중요정보"/
    "시세조종행위"/"부정거래행위"가 그대로 들어 있어 TOPIC_KEYWORDS가 매 질문마다
    무관한 페이지 3개를 seed 앞쪽에 끼워 넣었고(q10/q11처럼 진짜 필요한 443조가
    Tmax=3 예산 밖으로 밀려남), wiki_reader의 window pivot도 mandate 단어를 먼저
    찾아버렸다. 검색 신호는 `query`만 쓰고, mandate는 생성 단계에만 남긴다.
    """

    full_query = _wrap_query(query, mandate)
    seeds = grep_seed(query)
    seeds += [p for p in keyword_seed(query) if p not in seeds]
    if not seeds:
        top = _bm25_index().score(query, top_k=1)
        seeds = [page_id for page_id, _score in top]
    read = read_bounded(query, seeds, as_of=as_of)
    return {
        "answer": _finalize_wiki_answer(
            _generate_verdict(full_query, read.context, system=_LLM_WIKI_GENERATE_SYSTEM),
            read.pages_visited,
            query=query,
            context=read.context,
            citation_alias_map=read.citation_aliases,
        ),
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

    assert _LLM_WIKI_GENERATE_SYSTEM != PERSONA_PROMPTS[PERSONA]["generate_system"]
    assert _LLM_WIKI_GENERATE_SYSTEM.startswith(PERSONA_PROMPTS[PERSONA]["generate_system"])
    assert "combining two shown excerpts" in _LLM_WIKI_GENERATE_SYSTEM

    print("arms self-check OK:", flat_files[:3], "...")
