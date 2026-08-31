"""제한 탐색 리더 (논문 §3.2 Composability) — seed 페이지 전체가 아니라

매칭 지점 주변 window + outgoing_links 스니펫만 읽는다. tool-call 예산
`Tmax`(방문 페이지 수 상한)와 빈 탐색 인내 `P`(스니펫에 새 정보가 없으면 중단)를
둔다. seed가 없으면 즉시 빈 컨텍스트를 반환한다(fail-closed — 없는 근거를
지어내지 않는다).
"""

from __future__ import annotations

from datetime import date, datetime
import os
import re
from dataclasses import dataclass, field
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
MAX_WINDOW_SNIPPETS = 2  # 서로 떨어진 핵심 일치 구간을 함께 보존하는 상한
DEFAULT_TMAX = 3  # seed 포함 최대 방문 페이지 수
DEFAULT_PATIENCE = 1  # 링크를 따라가도 새 스니펫이 없으면 몇 번까지 더 시도할지
# vLLM is served with a 4096-token window.  The generator prompt, mandate and
# JSON completion also consume that window, so the Wiki reader must not send a
# 4K-character context and rely on the server to reject it.  This is a context
# budget, not a retrieval budget: PIT/link traversal still records all visited
# pages, while the handoff is bounded and fail-closed.
GENERATION_CONTEXT_CHAR_BUDGET = int(
    os.environ.get("LLM_WIKI_GENERATION_CONTEXT_CHAR_BUDGET", "3200")
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n\n(.*)$", re.DOTALL)
_LINK_LINE_RE = re.compile(r"^- \[\[([^\]]+)\]\] \((\w+)\): (.+)$", re.MULTILINE)
_FRONTMATTER_FIELD_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<value>[^\r\n]*)$", re.MULTILINE
)


@dataclass(frozen=True)
class ReadResult:
    pages_visited: list[str]
    context: str  # 모델에 넘길, 사람이 읽는 그대로의 발췌 텍스트
    truncated: bool  # Tmax에 걸려 더 못 읽은 링크가 있었는지
    # `_load_page()`가 이미 읽은 frontmatter에서 만든 alias. 최종화 단계에서
    # 같은 페이지를 다시 읽지 않도록 reader의 단일 출구에 함께 실어 보낸다.
    citation_aliases: dict[str, str] = field(default_factory=dict)


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


def _frontmatter_value(frontmatter: str, key: str) -> str | None:
    """Read one scalar frontmatter field without adding a YAML dependency."""

    for match in _FRONTMATTER_FIELD_RE.finditer(frontmatter):
        if match.group("key") == key:
            value = match.group("value").strip()
            return value or None
    return None


def _parse_as_of(as_of: str) -> date:
    """Normalize the legal query's ISO date/datetime cutoff."""

    raw = str(as_of or "").strip()
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "as_of must be an ISO date (YYYY-MM-DD) or ISO datetime"
            ) from exc


def _page_is_visible(frontmatter: str, as_of: date) -> bool:
    """Apply an inclusive effective_from/effective_to PIT window.

    Missing or malformed effective_from is rejected. This keeps a page with
    unknown temporal provenance out of a point-in-time legal context instead of
    silently treating it as current. effective_to is inclusive, matching the
    existing Agentic RAG PIT contract.
    """

    effective_from_raw = _frontmatter_value(frontmatter, "effective_from")
    effective_to_raw = _frontmatter_value(frontmatter, "effective_to")
    if not effective_from_raw:
        return False
    try:
        effective_from = _parse_as_of(effective_from_raw)
        effective_to = _parse_as_of(effective_to_raw) if effective_to_raw else None
    except ValueError:
        return False
    if effective_from > as_of:
        return False
    return effective_to is None or as_of <= effective_to


def _window_around(body: str, query: str) -> str:
    """질문과 가장 많이 겹치는 조문 window를 선택한다.

    제목의 첫 단어만 기준으로 잡으면 제172조처럼 핵심 기한(⑤항)이 뒤쪽에
    있는 페이지에서 본문 앞부분만 전달될 수 있다. 모든 query term의 후보
    위치를 평가해 질문 term coverage가 가장 높은 window를 고른다. 관련 조항
    링크/JSON 메타데이터는 본문 검색 대상에서 제외해 링크의 반복 문구가
    실제 조문보다 우선되지 않도록 한다.
    """

    source_body = body.split("\n## 관련 조항", 1)[0]
    terms = [term for term in re.findall(r"\w+", query) if len(term) > 1 or term.isdigit()]
    if not source_body or not terms:
        return source_body[:WINDOW_CHARS].strip()

    positions: list[int] = []
    for term in dict.fromkeys(terms):
        start = 0
        while True:
            found = source_body.find(term, start)
            if found < 0:
                break
            positions.append(found)
            start = found + max(len(term), 1)

    if not positions:
        return source_body[:WINDOW_CHARS].strip()

    half = WINDOW_CHARS // 2
    candidates: list[tuple[int, int, int, str]] = []
    for idx in positions:
        start = max(0, idx - half)
        end = min(len(source_body), idx + half)
        snippet = source_body[start:end].strip()
        coverage = sum(1 for term in dict.fromkeys(terms) if term in snippet)
        # 같은 coverage면 본문 쪽 후보를 우선한다. 제목/조문 헤더만 맞는
        # 후보보다 실제 질문의 조건·기한이 있는 문장이 선택될 가능성이 높다.
        candidates.append((coverage, idx, start, snippet))

    ranked = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)
    lead_end = min(len(source_body), WINDOW_CHARS)
    selected: list[tuple[int, int, str]] = [(0, lead_end, source_body[:lead_end].strip())]
    for _coverage, _position, start, snippet in ranked:
        end = min(len(source_body), start + WINDOW_CHARS)
        if any(start < selected_end and selected_start < end for selected_start, selected_end, _ in selected):
            continue
        selected.append((start, end, snippet))
        if len(selected) >= MAX_WINDOW_SNIPPETS:
            break

    selected.sort(key=lambda item: item[0])
    return "\n…\n".join(snippet for _start, _end, snippet in selected)


def _outgoing_links(body: str) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3)) for m in _LINK_LINE_RE.finditer(body)]


def _record_page_aliases(
    aliases: dict[str, str],
    ambiguous: set[str],
    page_id: str,
    frontmatter: str | None = None,
) -> None:
    """Add one page's unambiguous identifiers to the shared alias map."""

    values = [page_id]
    if frontmatter is not None:
        values.extend(
            value
            for key in ("page_id", "document_id", "clause_id")
            if (value := _frontmatter_value(frontmatter, key))
        )
    for value in dict.fromkeys(values):
        if value in ambiguous:
            continue
        previous = aliases.get(value)
        if previous is not None and previous != page_id:
            aliases.pop(value, None)
            ambiguous.add(value)
            continue
        aliases[value] = page_id


def citation_aliases(
    page_ids: list[str], wiki_dir: Path = WIKI_DIR
) -> dict[str, str]:
    """Return unambiguous page/document/clause aliases for visited pages.

    The model is instructed to cite page_id, but accepting the same page's
    document_id or clause_id avoids rejecting a grounded answer solely because
    the model used another identifier printed in the same Wiki frontmatter.
    Ambiguous aliases are removed rather than guessed.
    """

    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()
    for page_id in dict.fromkeys(page_ids):
        loaded = _load_page(page_id, wiki_dir)
        frontmatter = loaded[0] if loaded is not None else None
        _record_page_aliases(aliases, ambiguous, page_id, frontmatter)
    return aliases


def resolve_citation(value: str, aliases: dict[str, str]) -> str | None:
    """Resolve one model citation without guessing a document.

    The model sees ``page_id=...`` in the context, but older prompts/models may
    return the same value with a ``doc_id=`` prefix or surrounding Markdown
    punctuation.  Only an exact, whitespace-normalized alias is accepted;
    unknown or ambiguous identifiers remain invalid and are fail-closed by the
    existing finalizer.
    """

    candidate = str(value or "").strip().strip("`[]() ")
    candidate = re.sub(r"^(?:page_id|doc_id|clause_id)\s*=\s*", "", candidate)
    if candidate in aliases:
        return aliases[candidate]
    compact = re.sub(r"\s+", "", candidate)
    matches = {
        page_id
        for alias, page_id in aliases.items()
        if re.sub(r"\s+", "", alias) == compact
    }
    return next(iter(matches)) if len(matches) == 1 else None


def read_bounded(
    query: str,
    seed_page_ids: list[str],
    wiki_dir: Path = WIKI_DIR,
    tmax: int = DEFAULT_TMAX,
    patience: int = DEFAULT_PATIENCE,
    *,
    as_of: str | None = None,
) -> ReadResult:
    """seed 페이지(들)에서 시작해 PIT-valid window + 링크만 bounded하게 읽는다.

    ``as_of`` is mandatory at runtime. A missing cutoff fails closed rather than
    allowing a caller to accidentally read a future or expired legal page.
    """

    cutoff = _parse_as_of(as_of) if as_of is not None else None
    if cutoff is None:
        raise ValueError("as_of is required for point-in-time Wiki reads")

    if not seed_page_ids:
        return ReadResult(pages_visited=[], context="", truncated=False)

    visited: list[str] = []
    chunks: list[str] = []
    aliases: dict[str, str] = {}
    ambiguous_aliases: set[str] = set()
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
        frontmatter, body = loaded
        if not _page_is_visible(frontmatter, cutoff):
            continue
        visited.append(page_id)
        _record_page_aliases(aliases, ambiguous_aliases, page_id, frontmatter)

        window = _window_around(body, query)
        links = _outgoing_links(body)
        gained_new_info = bool(window.strip())
        # Make provenance copyable by the model.  The finalizer still verifies
        # it against the aliases returned by this same read.
        chunk_lines = [f"### page_id={page_id}", window]
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

    context = "\n\n".join(chunks)
    if len(context) > GENERATION_CONTEXT_CHAR_BUDGET:
        context = context[:GENERATION_CONTEXT_CHAR_BUDGET]
        truncated = True

    return ReadResult(
        pages_visited=visited,
        context=context,
        truncated=truncated,
        citation_aliases=aliases,
    )


if __name__ == "__main__":
    result_no_seed = read_bounded("아무 질문", [], as_of="2026-08-07")
    assert result_no_seed.pages_visited == []
    assert result_no_seed.context == ""

    result = read_bounded(
        "제178조 부정거래행위",
        ["자본시장법_제178조_부정거래행위등의금지"],
        tmax=3,
        as_of="2026-08-07",
    )
    assert result.pages_visited[0] == "자본시장법_제178조_부정거래행위등의금지"
    assert "부정거래행위" in result.context
    assert len(result.pages_visited) <= 3, result.pages_visited

    result_tmax1 = read_bounded(
        "제178조 부정거래행위",
        ["자본시장법_제178조_부정거래행위등의금지"],
        tmax=1,
        as_of="2026-08-07",
    )
    assert result_tmax1.pages_visited == ["자본시장법_제178조_부정거래행위등의금지"]
    assert result_tmax1.truncated is True

    print(
        "wiki_reader self-check OK:",
        result.pages_visited,
        f"truncated={result.truncated}",
    )
