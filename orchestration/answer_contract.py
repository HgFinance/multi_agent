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

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# 근거 좌표로 인정하는 흔적. MCP 도구가 실제로 붙여 주는 형태들이다.
#   citation=<sha16> / TR 코드(t1717) / rcept_no / URL / accession
_EVIDENCE_PATTERNS = (
    re.compile(r"citation[\"'\s:=]+([0-9a-f]{8,})", re.IGNORECASE),
    re.compile(r"\bt\d{4}\b"),  # LS TR 코드
    re.compile(r"\brcept_no\b", re.IGNORECASE),  # DART 접수번호
    re.compile(r"https?://[^\s)\]]+"),
    re.compile(r"\baccession\b", re.IGNORECASE),  # SEC
    # Trading의 PAPER 검증은 외부 시장 TR이 아니라 CEO root에 고정된
    # 투자한도·회계 스냅샷을 근거로 한다. 원본을 재현할 수 있는 기록 해시만
    # 허용하며, 임의의 "근거" 단어만으로는 통과시키지 않는다.
    re.compile(
        r"(?:기록\s*해시|검증\s*기록\s*식별자)\s*[:：=]\s*[0-9a-f]{8,}",
        re.IGNORECASE,
    ),
    # PAPER execution reports cite the authoritative Trading row rather than
    # a market-data TR or URL.  The UUID remains a coordinate, not a claim.
    re.compile(
        r"\bdirective_id\s*[=:]\s*[0-9a-f]{8}-[0-9a-f-]{27,}",
        re.IGNORECASE,
    ),
)

# A failed retrieval is still reproducible when the attempt itself is
# bounded and recorded.  This is deliberately strict: a bare word such as
# ``unavailable`` must not turn an answer with no provenance into evidence.
_BOUNDED_RETRIEVAL_FIELDS = (
    "instrument",
    "requested_window",
    "source",
    "tr",
    "status",
    "queried_at",
    "extracted_at",
    "snapshot_hash",
)
_BOUNDED_RETRIEVAL_MARKER = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?retrieval_attempt[ \t]*:?[ \t]*\n?"
)
_BOUNDED_RETRIEVAL_FIELD = re.compile(
    r"(?im)^[ \t]*([a-z][a-z0-9_]*)[ \t]*[:=][ \t]*([^\n]+)[ \t]*$"
)


def _normalized_bounded_record(
    values: Mapping[str, object],
) -> dict[str, str] | None:
    record = {
        field: str(values.get(field) or "").strip()
        for field in _BOUNDED_RETRIEVAL_FIELDS
    }
    return record if all(record.values()) else None


# 시점 표기. "지금 기준"인지 "어느 거래일 기준"인지가 시세성 답의 생명이다.
_ASOF_PATTERNS = (
    # ISO timestamps use ``T`` immediately after the date, so a word-boundary
    # after YYYY-MM-DD would incorrectly reject values such as
    # ``2026-08-26T15:22:30Z``.
    re.compile(r"\b20\d{2}[-/.]?\d{2}[-/.]?\d{2}"),
    re.compile(
        r"queried_at|as[_\s-]?of|기준일|조회\s*시각|검증\s*시각|기준",
        re.IGNORECASE,
    ),
)

# "모르는 것을 밝힌" 흔적. 없음을 없다고 말하는 문장.
_HONESTY_PATTERNS = (
    re.compile(r"미집계|집계\s*전|장중", re.IGNORECASE),
    re.compile(
        r"미구현|미큐레이션|없다|없음|unavailable|not available",
        re.IGNORECASE,
    ),
    re.compile(
        r"확인되지\s*않|검증\s*불가|추정|한계|제한적|제한됨|"
        r"확정(?:하기)?(?:는|은)?\s*(?:어렵|불가)|판단\s*보류|"
        r"판단\s*할\s*수\s*없|제공되지\s*않|보고하지\s*않|"
        r"공식\s*(?:확정값|확정 자료)\s*(?:이|가)?\s*아니|"
        r"추가\s*(?:확인|검증)\s*(?:이|가)?\s*필요|"
        r"(?:확인|검증|대조)(?:하지\s*못|할\s*수\s*없)|"
        r"(?:확인|검증|대조)\s*(?:이|가)?\s*필요",
        re.IGNORECASE,
    ),
)

# 24자 = 숫자·단위·기준일이 함께 들어갈 수 없는 하한. 임계값을 높이면
# 짧지만 정확한 답("8/13 외인계 -401,379주")을 빈 답으로 몰게 된다.
MIN_BODY_CHARS = 24


def _bounded_retrieval_parts(
    text: str,
) -> tuple[dict[str, str] | None, int | None, int | None]:
    """Parse one record and return its source span for projection reuse."""

    marker = _BOUNDED_RETRIEVAL_MARKER.search(text)
    if marker is None:
        return None, None, None
    line_end = text.find("\n", marker.start())
    if line_end < 0:
        line_end = len(text)
    marker_line = text[marker.start() : line_end].strip()
    inline_payload = marker_line.partition(":")[2].strip()
    if inline_payload:
        try:
            decoded = json.loads(inline_payload)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            record = _normalized_bounded_record(decoded)
            if record is not None:
                end = line_end + (1 if line_end < len(text) else 0)
                return record, marker.start(), end
    fenced_payload = re.match(
        r"\s*```(?:json)?\s*(\{.*?\})\s*```",
        text[marker.end() :],
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_payload is not None:
        try:
            decoded = json.loads(fenced_payload.group(1))
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            record = _normalized_bounded_record(decoded)
            if record is not None:
                end = marker.end() + fenced_payload.end()
                return record, marker.start(), end
    json_payload = text[marker.end() :]
    leading_whitespace = len(json_payload) - len(json_payload.lstrip())
    json_payload = json_payload.lstrip()
    if json_payload.startswith("{"):
        try:
            decoded, consumed = json.JSONDecoder().raw_decode(json_payload)
        except json.JSONDecodeError:
            decoded = None
            consumed = 0
        if isinstance(decoded, Mapping):
            record = _normalized_bounded_record(decoded)
            if record is not None:
                end = marker.end() + leading_whitespace + consumed
                return record, marker.start(), end
    fields: dict[str, str] = {}
    end = marker.end()
    for line in text[marker.end() :].splitlines(keepends=True):
        if not line.strip():
            break
        field = _BOUNDED_RETRIEVAL_FIELD.fullmatch(line.rstrip("\r\n"))
        if field is None:
            break
        fields[field.group(1)] = field.group(2).strip()
        end += len(line)
    record = _normalized_bounded_record(fields)
    if record is None:
        return None, None, None
    return record, marker.start(), end


def bounded_retrieval_attempt(text: str) -> dict[str, str] | None:
    """Return one complete bounded retrieval-attempt record, if present.

    The record is valid for both available and unavailable data.  In the
    latter case ``source``, ``tr`` and ``snapshot_hash`` may explicitly be
    ``UNAVAILABLE``; the attempt still has to identify the target and both
    query/extraction times so an operator can reproduce the boundary.
    """

    record, _start, _end = _bounded_retrieval_parts(text)
    return record


def bounded_retrieval_attempt_from_metadata(
    metadata: Mapping[str, object],
) -> dict[str, str] | None:
    """Normalize the same record when Hermes kept it in run metadata.

    Older Quant workers put retrieval fields in ``task_runs.metadata`` while
    newer workers may include the machine-readable block in ``result``.  The
    projection boundary accepts both representations, but only when every
    bounded field is present.
    """

    aliases = {
        "instrument": ("instrument", "symbol", "ticker"),
        "requested_window": ("requested_window", "window"),
        "source": ("source", "data_source"),
        "tr": ("tr", "tr_code", "transaction_id"),
        "status": ("status", "data_status", "quality_status"),
        "queried_at": ("queried_at", "as_of", "observed_at"),
        "extracted_at": ("extracted_at", "retrieved_at"),
        "snapshot_hash": ("snapshot_hash", "content_hash", "input_hash"),
    }
    values = {
        field: next(
            (metadata.get(alias) for alias in names if metadata.get(alias)),
            "",
        )
        for field, names in aliases.items()
    }
    return _normalized_bounded_record(values)


def format_bounded_retrieval_attempt(record: Mapping[str, object]) -> str:
    """Render one normalized record for the final answer boundary."""

    values = {
        field: str(record.get(field) or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
        for field in _BOUNDED_RETRIEVAL_FIELDS
    }
    if not all(values.values()):
        return ""
    return "\n".join(
        ["retrieval_attempt:"]
        + [f"{field}={values[field]}" for field in _BOUNDED_RETRIEVAL_FIELDS]
    )


def strip_bounded_retrieval_attempt(text: str) -> str:
    """Remove the machine-readable record from manager-facing prose."""

    _record, start, end = _bounded_retrieval_parts(text)
    if start is None or end is None:
        return str(text or "").strip()
    return f"{text[:start]}\n{text[end:]}".strip()


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
    has_evidence = any(p.search(text) for p in _EVIDENCE_PATTERNS) or bool(
        bounded_retrieval_attempt(text)
    )
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


__all__ = [
    "MIN_BODY_CHARS",
    "AnswerGrade",
    "bounded_retrieval_attempt",
    "bounded_retrieval_attempt_from_metadata",
    "format_bounded_retrieval_attempt",
    "grade_answer",
    "strip_bounded_retrieval_attempt",
]
