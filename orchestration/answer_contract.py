"""사용자에게 나가는 답변이 갖춰야 할 최소 조건.

▶ 왜 계약이 필요한가 (2026-08-14 실측)
  창구가 외국인 순매수 상위 10 종목 표를 만들고 종목별로 investor_flow 검증까지
  마쳤는데, 사용자 API 응답은 `result: null` 이었다. 2분 46초와 도구 41회가 통째로
  버려졌다. 원인은 에이전트가 `kanban_complete` 를 요약 한 줄로 먼저 부른 것이다.

  그런데 더 나쁜 경우가 있다: **본문은 있는데 근거가 없는 답**이다. 그건 그냥
  통과한다 - 아무도 안 보기 때문이다. QA 카드는 요약만 받았으므로 검증할 원문이
  없었고, 종합은 표를 다시 만들 수 없었다.

  그래서 "완료됐다"가 아니라 **"답으로서 성립하는가"** 를 기계가 본다.

▶ 무엇을 보는가 (전부 오늘 실측에서 나온 항목)
  1. 본문이 있는가          - 없으면 사용자는 빈 답을 받는다
  2. 근거 좌표가 있는가      - citation 해시·TR 코드·URL 중 하나. 없으면 재현 불가
  3. 시점이 있는가          - 시세성 답은 언제 기준인지 없으면 무의미하다
  4. 모르는 것을 말했는가    - 장중 미집계·미큐레이션처럼 **없음을 밝힌 흔적**

  4번이 특이한데, 오늘 가장 좋았던 답들의 공통점이었다. "8/14는 장중 미집계라
  순매수 0이 아니다", "t1637 에 있으나 실행 도구 미구현" 같은 문장이 있는 답은
  전부 검증을 통과했고, 없는 답은 수치를 지어낼 여지가 있었다.

▶ 무엇을 하지 않는가
  이 계약은 **차단기가 아니다.** 답을 막지 않고 등급과 사유를 붙인다. 막으면
  에이전트가 형식만 채우게 되고(모두가 citation 이라는 단어만 넣는다), 정작
  급한 사용자 답이 안 나간다. QA 와 종합이 무엇을 의심해야 하는지 알면 충분하다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 근거 좌표로 인정하는 흔적. MCP 도구가 실제로 붙여 주는 형태들이다.
#   citation=<sha16> / TR 코드(t1717) / rcept_no / URL / accession
_EVIDENCE_PATTERNS = (
    re.compile(r"citation[\"'\s:=]+([0-9a-f]{8,})", re.I),
    re.compile(r"\bt\d{4}\b"),  # LS TR 코드
    re.compile(r"\brcept_no\b", re.I),  # DART 접수번호
    re.compile(r"https?://[^\s)\]]+"),
    re.compile(r"\baccession\b", re.I),  # SEC
    # PAPER execution reports cite the authoritative Trading row rather than
    # a market-data TR or URL.  The UUID remains a coordinate, not a claim.
    re.compile(r"\bdirective_id\s*[=:]\s*[0-9a-f]{8}-[0-9a-f-]{27,}", re.I),
)

# 시점 표기. "지금 기준"인지 "어느 거래일 기준"인지가 시세성 답의 생명이다.
_ASOF_PATTERNS = (
    # ISO timestamps use ``T`` immediately after the date, so a word-boundary
    # after YYYY-MM-DD would incorrectly reject values such as
    # ``2026-08-26T15:22:30Z``.
    re.compile(r"\b20\d{2}[-/.]?\d{2}[-/.]?\d{2}"),
    re.compile(r"queried_at|as[_\s-]?of|기준일|조회\s*시각|검증\s*시각|기준", re.I),
)

# "모르는 것을 밝힌" 흔적. 없음을 없다고 말하는 문장.
_HONESTY_PATTERNS = (
    re.compile(r"미집계|집계\s*전|장중", re.I),
    re.compile(r"미구현|미큐레이션|없다|없음|unavailable|not available", re.I),
    re.compile(r"확인되지\s*않|검증\s*불가|추정|한계", re.I),
)

# 24자 = 숫자·단위·기준일이 함께 들어갈 수 없는 하한. 임계값을 높이면
# 짧지만 정확한 답("8/13 외인계 -401,379주")을 빈 답으로 몰게 된다.
MIN_BODY_CHARS = 24


@dataclass(frozen=True)
class AnswerGrade:
    """답변 한 건의 등급. 차단이 아니라 신호다."""

    has_body: bool
    has_evidence: bool
    has_as_of: bool
    states_unknowns: bool
    gaps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """사용자에게 내보낼 수 있는가 - 본문이 있으면 일단 답이다."""

        return self.has_body

    @property
    def trustworthy(self) -> bool:
        """근거까지 갖췄는가 - QA 가 검증을 시작할 수 있는 상태."""

        return self.has_body and self.has_evidence and self.has_as_of

    def as_payload(self) -> dict[str, object]:
        """QA·종합 카드에 실을 형태. 빈 값은 싣지 않는다(잡음 방지)."""

        payload: dict[str, object] = {
            "answer_usable": self.usable,
            "answer_trustworthy": self.trustworthy,
        }
        if self.gaps:
            payload["answer_gaps"] = list(self.gaps)
            payload["answer_gaps_note"] = (
                "이 답변에는 위 항목이 없다. 없는 것을 있다고 보충하지 말고, "
                "검증할 수 없는 수치는 통과시키지 마라."
            )
        return payload


def grade_answer(result: str, *, summary: str = "") -> AnswerGrade:
    """부서가 낸 답변 본문을 등급 매긴다. 판단은 하지 않고 형태만 본다."""

    body = str(result or "").strip()
    text = f"{body}\n{summary or ''}"
    has_body = len(body) >= MIN_BODY_CHARS
    has_evidence = any(p.search(text) for p in _EVIDENCE_PATTERNS)
    has_as_of = any(p.search(text) for p in _ASOF_PATTERNS)
    states_unknowns = any(p.search(text) for p in _HONESTY_PATTERNS)

    gaps: list[str] = []
    if not has_body:
        gaps.append("답변 본문 없음(result 가 비었거나 너무 짧다)")
    if not has_evidence:
        gaps.append("근거 좌표 없음(citation·TR코드·URL 중 어느 것도 없다)")
    if not has_as_of:
        gaps.append("기준 시점 없음(언제 기준인지 알 수 없다)")
    if has_body and not states_unknowns:
        # 없는 것을 밝히지 않은 답은 '전부 알아냈다'는 뜻이거나 한계를 숨긴 것이다.
        gaps.append("한계·미확인 항목 언급 없음(모르는 것을 밝혔는지 확인 필요)")

    return AnswerGrade(
        has_body=has_body,
        has_evidence=has_evidence,
        has_as_of=has_as_of,
        states_unknowns=states_unknowns,
        gaps=tuple(gaps),
    )


__all__ = ["AnswerGrade", "MIN_BODY_CHARS", "grade_answer"]
