from __future__ import annotations

from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from wiki_reader import read_bounded


def _write_page(
    wiki_dir: Path,
    page_id: str,
    *,
    effective_from: str,
    effective_to: str = "",
    body: str = "근거 본문입니다.",
) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / f"{page_id}.md").write_text(
        "\n".join(
            [
                "---",
                f"effective_from: {effective_from}",
                f"effective_to: {effective_to}",
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_read_bounded_applies_inclusive_point_in_time_window(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "active", effective_from="2026-01-01")
    _write_page(wiki_dir, "future", effective_from="2026-08-08")
    _write_page(
        wiki_dir,
        "expired",
        effective_from="2025-01-01",
        effective_to="2026-08-06",
    )
    _write_page(
        wiki_dir,
        "boundary",
        effective_from="2025-01-01",
        effective_to="2026-08-07",
    )

    result = read_bounded(
        "근거",
        ["active", "future", "expired", "boundary"],
        wiki_dir=wiki_dir,
        tmax=10,
        as_of="2026-08-07",
    )

    assert result.pages_visited == ["active", "boundary"]
    assert "future" not in result.context
    assert "expired" not in result.context


def test_temporally_invalid_seed_cannot_traverse_to_an_active_page(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "active", effective_from="2026-01-01")
    _write_page(
        wiki_dir,
        "future",
        effective_from="2027-01-01",
        body="미래 문서\n- [[active]] (CITES): 미래 문서",
    )

    result = read_bounded(
        "미래 문서",
        ["future"],
        wiki_dir=wiki_dir,
        as_of="2026-08-07",
    )

    assert result.pages_visited == []
    assert result.context == ""


def test_reader_keeps_two_separated_windows_for_multiple_conditions() -> None:
    result = read_bounded(
        "단기매매차익 반환청구권은 이익을 얻은 날부터 얼마 동안 행사할 수 있나",
        ["자본시장법_제172조_내부자의단기매매차익반환"],
        as_of="2026-08-26",
    )

    assert "6개월 이내" in result.context
    assert "2년 이내" in result.context


def test_missing_or_invalid_as_of_fails_closed_before_reading(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="as_of is required"):
        read_bounded("질문", ["page"], wiki_dir=tmp_path)

    with pytest.raises(ValueError, match="ISO date"):
        read_bounded("질문", ["page"], wiki_dir=tmp_path, as_of="not-a-date")


def test_missing_or_malformed_effective_from_is_not_visible(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    _write_page(wiki_dir, "missing", effective_from="")
    _write_page(wiki_dir, "malformed", effective_from="not-a-date")

    result = read_bounded(
        "근거",
        ["missing", "malformed"],
        wiki_dir=wiki_dir,
        as_of="2026-08-07",
    )

    assert result.pages_visited == []
    assert result.context == ""


def test_citation_aliases_accept_unambiguous_document_and_clause_ids(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "page-a.md").write_text(
        "---\neffective_from: 2026-01-01\neffective_to: \n"
        "document_id: doc-a\nclause_id: 제1조\n---\n\n근거 본문입니다.\n",
        encoding="utf-8",
    )

    from wiki_reader import citation_aliases

    aliases = citation_aliases(["page-a"], wiki_dir=wiki_dir)

    assert aliases["page-a"] == "page-a"
    assert aliases["doc-a"] == "page-a"
    assert aliases["제1조"] == "page-a"


def test_empty_effective_to_does_not_consume_following_frontmatter_fields(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "page-a.md").write_text(
        "---\neffective_from: 2026-01-01\neffective_to: \n"
        "document_id: doc-a\n---\n\n근거 본문입니다.\n",
        encoding="utf-8",
    )

    result = read_bounded("근거", ["page-a"], wiki_dir=wiki_dir, as_of="2026-08-26")

    assert result.pages_visited == ["page-a"]
