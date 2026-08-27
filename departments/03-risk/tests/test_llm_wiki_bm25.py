from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from bm25 import BM25Index, tokenize


def test_tokenize_splits_korean_words() -> None:
    assert tokenize("제178조 부정거래행위 금지") == ["제178조", "부정거래행위", "금지"]


def test_tokenize_is_case_insensitive() -> None:
    assert tokenize("Capital Markets") == tokenize("capital markets")


def test_bm25_ranks_document_with_rare_matching_terms_first() -> None:
    corpus = {
        "미공개중요정보": "미공개중요정보 이용행위 금지 조항입니다",
        "시세조종": "시세조종행위 등의 금지에 관한 조항입니다",
        "부정거래": "부정거래행위 등의 금지, 부정한 수단과 계획",
    }
    index = BM25Index(corpus)

    top = index.score("부정거래 부정한 수단")

    assert top[0][0] == "부정거래"


def test_bm25_empty_corpus_returns_no_scores() -> None:
    assert BM25Index({}).score("아무 질문") == []


def test_bm25_empty_query_returns_no_scores() -> None:
    index = BM25Index({"a": "부정거래행위 금지"})
    assert index.score("") == []


def test_bm25_query_with_no_overlap_returns_no_scores() -> None:
    index = BM25Index({"a": "부정거래행위 금지"})
    assert index.score("완전히 관련 없는 단어") == []
