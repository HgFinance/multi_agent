"""제한 탐색 리더 (논문 §3.2 Composability) — seed 페이지 전체가 아니라

매칭 지점 주변 window + outgoing_links 스니펫만 읽는다. tool-call 예산
`Tmax`(방문 페이지 수 상한)와 빈 탐색 인내 `P`(스니펫에 새 정보가 없으면 중단)를
둔다. seed가 없으면 즉시 빈 컨텍스트를 반환한다(fail-closed — 없는 근거를
지어내지 않는다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"

# 튜닝(2026-08-07): 판례(prec) 페이지는 [1][2][3][4] 요지가 <br/>만으로 이어진 긴
# 한 문단이라, pivot 용어가 앞쪽 요지에서 먼저 매칭되면 뒤쪽 요지의 핵심 결론
# 문장이 window 밖으로 밀려난다(2019도12887: pivot "이사회"~340자 뒤의 "한정 적극"
# 결론 문구가 400 밖으로 잘림, q14 semantic_f1 0.5). 400->800으로 넉넉하게 키운다
# — 코퍼스 최대 페이지(443조 본문)도 800*2=1600자면 대부분 커버되고, Arm A(flat
# chunk 800자/청크)의 청크 크기와도 같은 자릿수라 "맥락 절약" 취지를 크게 벗어나지
# 않는다.
WINDOW_CHARS = 800  # 매칭 지점 앞뒤로 읽는 문자 수 (전체 페이지가 아니라 "주변"만)
DEFAULT_TMAX = 3  # seed 포함 최대 방문 페이지 수
DEFAULT_PATIENCE = 1  # 링크를 따라가도 새 스니펫이 없으면 몇 번까지 더 시도할지

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n\n(.*)$", re.DOTALL)
_LINK_LINE_RE = re.compile(r"^- \[\[([^\]]+)\]\] \((\w+)\): (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ReadResult:
    pages_visited: list[str]
    context: str  # 모델에 넘길, 사람이 읽는 그대로의 발췌 텍스트
    truncated: bool  # Tmax에 걸려 더 못 읽은 링크가 있었는지


def _load_page(page_id: str, wiki_dir: Path) -> tuple[str, str] | None:
    path = wiki_dir / f"{page_id}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    frontmatter, body = match.groups()
    return frontmatter, body


def _window_around(body: str, query: str) -> str:
    """query 토큰이 처음 등장하는 지점 주변 WINDOW_CHARS만 자른다. 못 찾으면 앞부분."""

    terms = re.findall(r"\w+", query)
    idx = -1
    for term in terms:
        found = body.find(term)
        if found >= 0:
            idx = found
            break
    if idx < 0:
        idx = 0
    start = max(0, idx - WINDOW_CHARS // 2)
    end = min(len(body), idx + WINDOW_CHARS // 2)
    return body[start:end].strip()


def _outgoing_links(body: str) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3)) for m in _LINK_LINE_RE.finditer(body)]


def read_bounded(
    query: str,
    seed_page_ids: list[str],
    wiki_dir: Path = WIKI_DIR,
    tmax: int = DEFAULT_TMAX,
    patience: int = DEFAULT_PATIENCE,
) -> ReadResult:
    """seed 페이지(들)에서 시작해 window + 링크 스니펫만 bounded하게 읽는다."""

    if not seed_page_ids:
        return ReadResult(pages_visited=[], context="", truncated=False)

    visited: list[str] = []
    chunks: list[str] = []
    queue = list(seed_page_ids)
    empty_reads = 0
    truncated = False

    while queue and len(visited) < tmax:
        page_id = queue.pop(0)
        if page_id in visited:
            continue
        loaded = _load_page(page_id, wiki_dir)
        if loaded is None:
            continue
        _frontmatter, body = loaded
        visited.append(page_id)

        window = _window_around(body, query)
        links = _outgoing_links(body)
        gained_new_info = bool(window.strip())
        chunk_lines = [f"### {page_id}", window]
        if links:
            chunk_lines.append("관련 조항:")
            for target, relation, snippet in links:
                chunk_lines.append(f"- ({relation}) {target}: {snippet}")
                gained_new_info = True
        chunks.append("\n".join(chunk_lines))

        if not gained_new_info:
            empty_reads += 1
            if empty_reads > patience:
                break

        for target, _relation, _snippet in links:
            if target not in visited and target not in queue:
                queue.append(target)

    if queue and len(visited) >= tmax:
        truncated = True

    return ReadResult(
        pages_visited=visited, context="\n\n".join(chunks), truncated=truncated
    )


if __name__ == "__main__":
    result_no_seed = read_bounded("아무 질문", [])
    assert result_no_seed.pages_visited == []
    assert result_no_seed.context == ""

    result = read_bounded(
        "제178조 부정거래행위",
        ["자본시장법_제178조_부정거래행위등의금지"],
        tmax=3,
    )
    assert result.pages_visited[0] == "자본시장법_제178조_부정거래행위등의금지"
    assert "부정거래행위" in result.context
    assert len(result.pages_visited) <= 3, result.pages_visited

    result_tmax1 = read_bounded(
        "제178조 부정거래행위",
        ["자본시장법_제178조_부정거래행위등의금지"],
        tmax=1,
    )
    assert result_tmax1.pages_visited == ["자본시장법_제178조_부정거래행위등의금지"]
    assert result_tmax1.truncated is True

    print(
        "wiki_reader self-check OK:",
        result.pages_visited,
        f"truncated={result.truncated}",
    )
