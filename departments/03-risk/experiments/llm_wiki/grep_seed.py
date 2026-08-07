"""1차 출발점(seed) 탐색 — 정규식으로 조항 번호를 감지해 grep으로 즉시 페이지를 잡는다.

`data/wiki/*.md` 프론트매터의 `clause_id:` 줄이 grep 대상이다. 질문에 "제178조"처럼
정확한 조항 번호가 있으면 형태소 분석·임베딩 없이 바로 해당 페이지로 간다 — BM25는
이 매칭이 실패했을 때만 쓰는 2차 폴백이다(bm25.py).
"""

from __future__ import annotations

import re
from pathlib import Path

WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"

# 자본시장법류 조항("제178조", "제174조의2"), 행정규칙류("제4-6조"), 판례 사건번호("2019도12887")
CLAUSE_ID_PATTERN = re.compile(r"제\d+조(?:의\d+)?|제\d+-\d+조|\d{4}[가-힣]\d+")

_FRONTMATTER_CLAUSE_RE = re.compile(r"^clause_id:\s*(.+)$", re.MULTILINE)


def detect_clause_ids(query: str) -> list[str]:
    return CLAUSE_ID_PATTERN.findall(query)


def _page_clause_id(text: str) -> str | None:
    match = _FRONTMATTER_CLAUSE_RE.search(text)
    return match.group(1).strip() if match else None


def grep_seed(query: str, wiki_dir: Path = WIKI_DIR) -> list[str]:
    """질문에서 감지된 조항 번호와 frontmatter clause_id가 정확히 일치하는 page_id들."""

    clause_ids = set(detect_clause_ids(query))
    if not clause_ids:
        return []
    matches: list[str] = []
    for path in sorted(wiki_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        page_clause = _page_clause_id(text)
        if page_clause in clause_ids:
            matches.append(path.stem)
    return matches


if __name__ == "__main__":
    assert detect_clause_ids("제178조 위반인가요?") == ["제178조"]
    assert detect_clause_ids("2019도12887 판례 알려줘") == ["2019도12887"]
    assert detect_clause_ids("일반 질문입니다") == []

    found = grep_seed("제178조 부정거래행위에 해당하나요?")
    assert found == ["자본시장법_제178조_부정거래행위등의금지"], found

    found_none = grep_seed("조항 번호 없는 애매한 질문")
    assert found_none == [], found_none

    print("grep_seed self-check OK:", found)
