from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from grep_seed import TOPIC_KEYWORDS, detect_clause_ids, grep_seed, keyword_seed  # noqa: E402


def _write_page(wiki_dir: Path, page_id: str, clause_id: str) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / f"{page_id}.md").write_text(
        f"---\nclause_id: {clause_id}\n---\n\n# {page_id}\n", encoding="utf-8"
    )


def test_detect_clause_ids_matches_article_and_case_numbers() -> None:
    assert detect_clause_ids("제178조 위반인가요?") == ["제178조"]
    assert detect_clause_ids("제174조의2는 뭔가요") == ["제174조의2"]
    assert detect_clause_ids("제4-6조 정보교류차단") == ["제4-6조"]
    assert detect_clause_ids("2019도12887 판례") == ["2019도12887"]
    assert detect_clause_ids("조항 번호 없는 질문") == []


def test_grep_seed_matches_exact_clause_id_in_frontmatter(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "자본시장법_제178조_부정거래행위등의금지", "제178조")
    _write_page(wiki_dir, "자본시장법_제176조_시세조종행위등의금지", "제176조")

    found = grep_seed("제178조 위반인가요?", wiki_dir=wiki_dir)
    assert found == ["자본시장법_제178조_부정거래행위등의금지"]


def test_grep_seed_returns_empty_when_no_clause_number_detected(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "자본시장법_제178조_부정거래행위등의금지", "제178조")

    assert grep_seed("애매한 일반 질문입니다", wiki_dir=wiki_dir) == []


def test_grep_seed_returns_empty_when_clause_number_not_in_corpus(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "자본시장법_제178조_부정거래행위등의금지", "제178조")

    assert grep_seed("제999조는 뭔가요?", wiki_dir=wiki_dir) == []


def test_keyword_seed_matches_topic_keyword_without_clause_number() -> None:
    found = keyword_seed("미공개중요정보를 이용해 매매하면 어떻게 되나요?")
    assert found == ["자본시장법_제174조_미공개중요정보이용행위금지"]


def test_keyword_seed_returns_empty_for_unrelated_query() -> None:
    assert keyword_seed("가상자산 상장 심사 기준이 궁금합니다") == []


def test_keyword_seed_dedupes_multiple_keywords_pointing_to_same_page() -> None:
    found = keyword_seed("부정거래행위는 부정한 수단을 쓰는 건가요?")
    assert found == ["자본시장법_제178조_부정거래행위등의금지"]


def test_keyword_seed_returns_multiple_pages_when_query_spans_two_topics() -> None:
    found = keyword_seed("미공개중요정보 이용행위를 하면 처벌은 어떻게 되나요?")
    assert found == [
        "자본시장법_제174조_미공개중요정보이용행위금지",
        "자본시장법_제443조_벌칙",
    ]


def test_topic_keywords_all_point_to_distinct_known_pages() -> None:
    assert len(set(TOPIC_KEYWORDS.values())) >= 8
