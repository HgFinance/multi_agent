from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from grep_seed import detect_clause_ids, grep_seed  # noqa: E402


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
