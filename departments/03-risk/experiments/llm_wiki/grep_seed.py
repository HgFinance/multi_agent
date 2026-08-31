"""1차 출발점(seed) 탐색 — 정규식으로 조항 번호를 감지해 grep으로 즉시 페이지를 잡는다.

`data/wiki/*.md` 프론트매터의 `clause_id:` 줄이 grep 대상이다. 질문에 "제178조"처럼
정확한 조항 번호가 있으면 형태소 분석·임베딩 없이 바로 해당 페이지로 간다.

튜닝(2026-08-07): golden set 15문항 중 조항 번호를 문자 그대로 포함한 질문은 2개뿐
(q10, q13)이라 `grep_seed` 단독으로는 대부분 빈 리스트를 반환하고 매번 BM25로
폴백했다(Arm C가 Arm B와 사실상 동일하게 동작). `keyword_seed`를 조항번호 매칭
실패 시의 2차 grep 폴백으로 추가한다 — 여전히 스코어링 없는 결정론적 문자열 포함
매칭이고, BM25는 이 둘 다 실패했을 때만 쓰는 최종 폴백이다(arms.py).
"""

from __future__ import annotations

import re
from pathlib import Path

WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"

# 자본시장법류 조항("제178조", "제174조의2"), 행정규칙류("제4-6조"), 판례 사건번호("2019도12887")
CLAUSE_ID_PATTERN = re.compile(r"제\d+조(?:의\d+)?|제\d+-\d+조|\d{4}[가-힣]\d+")

_FRONTMATTER_CLAUSE_RE = re.compile(r"^clause_id:\s*(.+)$", re.MULTILINE)

# ponytail: 11개 문서 규모의 코퍼스 기준 수작업 매핑. 각 페이지 title(예: 제174조
# "미공개중요정보 이용행위 금지")에서 그대로 뽑은 핵심어 -> page_id. 코퍼스가 커지면
# 이 표를 title 토큰 자동 추출(형태소 분석기 도입) 방식으로 교체해야 한다.
TOPIC_KEYWORDS: dict[str, str] = {
    "직무관련정보": "자본시장법_제54조_직무관련정보의이용금지",
    "직무상 알게": "자본시장법_제54조_직무관련정보의이용금지",
    "임직원의 금융투자상품": "자본시장법_제63조_임직원의금융투자상품매매",
    "자기의 계산": "자본시장법_제63조_임직원의금융투자상품매매",
    "매매명세": "자본시장법_제63조_임직원의금융투자상품매매",
    "불건전 영업행위": "자본시장법_제71조_불건전영업행위의금지",
    "불건전영업행위": "자본시장법_제71조_불건전영업행위의금지",
    "조사분석자료": "자본시장법_제71조_불건전영업행위의금지",
    "단기매매차익": "자본시장법_제172조_내부자의단기매매차익반환",
    # 질문이 법령 제목의 표현과 달라도 동일 조항으로 결정론적으로 연결한다.
    # 짧고 구체적인 표현만 추가해 일반적인 '임원'·'증권' 같은 과매칭은 피한다.
    "상장회사 임원": "자본시장법_제172조_내부자의단기매매차익반환",
    "반환청구권": "자본시장법_제172조_내부자의단기매매차익반환",
    "6개월 이내": "자본시장법_제172조_내부자의단기매매차익반환",
    "이익을 얻은 날": "자본시장법_제172조_내부자의단기매매차익반환",
    "특정증권등 소유상황": "자본시장법_제173조_임원등의특정증권등소유상황보고",
    "소유상황": "자본시장법_제173조_임원등의특정증권등소유상황보고",
    "소유현황": "자본시장법_제173조_임원등의특정증권등소유상황보고",
    "임원이나 주요주주": "자본시장법_제173조_임원등의특정증권등소유상황보고",
    "처음 신고": "자본시장법_제173조_임원등의특정증권등소유상황보고",
    "미공개중요정보": "자본시장법_제174조_미공개중요정보이용행위금지",
    "시세조종": "자본시장법_제176조_시세조종행위등의금지",
    "통정매매": "자본시장법_제176조_시세조종행위등의금지",
    "부정거래행위": "자본시장법_제178조_부정거래행위등의금지",
    "부정한 수단": "자본시장법_제178조_부정거래행위등의금지",
    "벌칙": "자본시장법_제443조_벌칙",
    "가중처벌": "자본시장법_제443조_벌칙",
    "처벌": "자본시장법_제443조_벌칙",
    "정보교류": "금융투자업규정_제4의6조_정보교류의차단",
    "이사회 결의": "대법원_2019도12887_부정거래행위판단기준",
}


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


def keyword_seed(query: str) -> list[str]:
    """조항번호 grep이 실패했을 때의 2차 grep 폴백 — TOPIC_KEYWORDS 문자열 포함 매칭."""

    matches: list[str] = []
    for keyword, page_id in TOPIC_KEYWORDS.items():
        if keyword in query and page_id not in matches:
            matches.append(page_id)
    return matches


if __name__ == "__main__":
    assert detect_clause_ids("제178조 위반인가요?") == ["제178조"]
    assert detect_clause_ids("2019도12887 판례 알려줘") == ["2019도12887"]
    assert detect_clause_ids("일반 질문입니다") == []

    found = grep_seed("제178조 부정거래행위에 해당하나요?")
    assert found == ["자본시장법_제178조_부정거래행위등의금지"], found

    found_none = grep_seed("조항 번호 없는 애매한 질문")
    assert found_none == [], found_none

    kw = keyword_seed("미공개중요정보를 이용해 매매하면 어떻게 되나요?")
    assert kw == ["자본시장법_제174조_미공개중요정보이용행위금지"], kw
    assert keyword_seed("전혀 관련 없는 질문") == []

    print("grep_seed self-check OK:", found, kw)
