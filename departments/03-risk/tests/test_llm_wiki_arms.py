from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from arms import (  # noqa: E402
    _LLM_WIKI_GENERATE_SYSTEM,
    _LEGAL_VERDICT_SCHEMA,
    PERSONA,
    _generate_verdict,
    build_flat_corpus,
)
from departments import worker_model_gateway
from src.nodes import PERSONA_PROMPTS  # noqa: E402


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
