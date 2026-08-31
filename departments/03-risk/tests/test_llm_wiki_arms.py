from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from arms import (
    _LEGAL_VERDICT_SCHEMA,
    _LLM_WIKI_GENERATE_SYSTEM,
    _finalize_wiki_answer,
    PERSONA,
    _generate_verdict,
    build_flat_corpus,
)
from src.nodes import PERSONA_PROMPTS

from departments import worker_model_gateway


def test_build_flat_corpus_matches_retriever_frontmatter_contract(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    payload = {
        "source": "law",
        "doc_id": "law-1-178",
        "page_id": "자본시장법_제178조_부정거래행위등의금지",
        "clause_id": "제178조",
        "title": "부정거래행위 등의 금지",
        "authority": "금융위원회",
        "effective_from": "2026-01-01",
        "text": "부정한 수단, 계획 또는 기교를 사용하는 행위를 금지한다.",
        "origin_url": "https://example.test/178",
        "source_sha256": "sha256:deadbeef",
    }
    (raw_dir / "law-1-178.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    out_dir = tmp_path / "flat"

    build_flat_corpus(raw_dir=raw_dir, out_dir=out_dir)

    written = (out_dir / "자본시장법_제178조_부정거래행위등의금지.md").read_text(encoding="utf-8")
    assert written.startswith("---\n")
    assert "document_id: law-1-178\n" in written
    assert "document_type: law\n" in written
    assert "version: 2026-01-01\n" in written
    assert written.split("---\n", 2)[2].lstrip().startswith("# 부정거래행위 등의 금지")


def test_generate_verdict_fails_closed_on_empty_context() -> None:
    answer = _generate_verdict("아무 질문", "")

    prompts = PERSONA_PROMPTS[PERSONA]
    assert answer["verdict"] == prompts["no_evidence_verdict"]
    assert answer["cited_documents"] == []
    assert answer["escalate"] is True
    assert answer["confidence"] == 0.0


def test_llm_wiki_generate_system_extends_base_prompt_without_mutating_it() -> None:
    base = PERSONA_PROMPTS[PERSONA]["generate_system"]

    assert _LLM_WIKI_GENERATE_SYSTEM.startswith(base)
    assert _LLM_WIKI_GENERATE_SYSTEM != base
    assert "ambiguous" in _LLM_WIKI_GENERATE_SYSTEM
    assert PERSONA_PROMPTS[PERSONA]["generate_system"] == base  # 프로덕션 프롬프트 불변


def test_generate_verdict_uses_qwen_gateway_without_arithmetic_adapter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_MODEL_BASE_URL", "http://vllm:8000/v1")
    calls: list[dict] = []

    class Binding:
        provider = "vllm-openai"
        model = "qwen2.5-14b-instruct-awq"
        adapter_id = None

    def fake_worker_llm(system: str, prompt: str, *, json_schema=None) -> str:
        calls.append(
            {"system": system, "prompt": prompt, "json_schema": json_schema}
        )
        return json.dumps(
            {
                "verdict": "no_breach",
                "cited_documents": ["law:178"],
                "rationale": "제178조 근거",
                "confidence": 0.8,
                "escalate": False,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        worker_model_gateway,
        "resolve",
        lambda worker_id: Binding(),
    )
    monkeypatch.setattr(
        worker_model_gateway,
        "llm_for_worker",
        lambda worker_id: (fake_worker_llm, Binding()),
    )
    monkeypatch.setattr("arms._CACHE.get", lambda _fingerprint: None)
    monkeypatch.setattr("arms._CACHE.set", lambda _fingerprint, _value: None)

    result = _generate_verdict("제178조 질의", "제178조 근거 문서")

    assert result["verdict"] == "no_breach"
    assert result["cited_documents"] == ["law:178"]
    assert len(calls) == 1
    assert calls[0]["json_schema"] == _LEGAL_VERDICT_SCHEMA
    assert Binding.adapter_id is None


def test_wiki_answer_rejects_unvisited_citations_and_escalates() -> None:
    result = _finalize_wiki_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["not-visited"],
            "rationale": "제공된 근거만으로는 판단할 수 없다.",
            "confidence": 0.9,
            "escalate": False,
        },
        ["visited-page"],
    )

    assert result["verdict"] == "ambiguous"
    assert result["cited_documents"] == []
    assert result["confidence"] == 0.0
    assert result["escalate"] is True


def test_wiki_answer_requires_a_visited_citation_for_a_definitive_verdict() -> None:
    result = _finalize_wiki_answer(
        {
            "verdict": "breach",
            "cited_documents": [],
            "rationale": "제178조에 따른다.",
            "confidence": 0.8,
            "escalate": False,
        },
        ["visited-page"],
    )

    assert result["verdict"] == "ambiguous"
    assert result["escalate"] is True


def test_wiki_answer_preserves_grounded_verdict_and_only_visited_citations() -> None:
    result = _finalize_wiki_answer(
        {
            "verdict": "breach",
            "cited_documents": ["visited-page", "not-visited"],
            "rationale": "제178조에 따른다.",
            "confidence": 0.8,
            "escalate": False,
        },
        ["visited-page"],
    )

    assert result["verdict"] == "ambiguous"
    assert result["cited_documents"] == ["visited-page"]


def test_llm_wiki_prompt_requires_numeric_threshold_comparison() -> None:
    assert "M months/days with M less than or equal to N" in _LLM_WIKI_GENERATE_SYSTEM


def test_wiki_answer_fails_closed_on_numeric_threshold_reversal() -> None:
    result = _finalize_wiki_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["visited-page"],
            "rationale": "5개월은 6개월 이내 조건을 충족하지 못하므로 반환할 수 없다.",
            "confidence": 0.8,
            "escalate": False,
        },
        ["visited-page"],
        query="5개월 후 매도했을 때 반환청구할 수 있는가?",
        context="제172조는 6개월 이내에 매도하면 반환을 청구할 수 있다고 정한다.",
    )

    assert result["verdict"] == "ambiguous"
    assert result["confidence"] == 0.0
    assert result["escalate"] is True


def test_wiki_answer_fails_closed_when_rationale_prohibits_but_verdict_is_no_breach() -> None:
    result = _finalize_wiki_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["visited-page"],
            "rationale": "미공개정보를 받은 사람도 거래하면 안 되므로 금지된다. 해당하지 않는다.",
            "confidence": 0.8,
            "escalate": False,
        },
        ["visited-page"],
    )

    assert result["verdict"] == "ambiguous"
    assert result["escalate"] is True
