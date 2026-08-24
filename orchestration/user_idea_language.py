#!/usr/bin/env python3
"""사용자 아이디어 후보의 **독립 검증기**. 원문을 다시 읽어 인용을 대조한다.

담당: 재일 (리서치본부 RES)
형판: `orchestration/user_order_language.py` (페이퍼 주문 경로). 같은 위험을
      이미 한 번 푼 자리이고, 그 해법을 여기서 되풀이한다.

▶ 이 모듈이 서는 자리
      사용자 원문 ─┬─→ LLM ─→ UserIdeaCandidate (비구속 제안)
                   │                 │
                   └────→ **이 모듈** ←┘   원문과 후보를 **둘 다** 받아
                                  │        인용 구간을 글자 그대로 대조
                                  ├─ 통과 → VerifiedIdeaLead → 공장 리드 블록
                                  └─ 거부 → IdeaRejection(사유 코드 + 되물음)

      LLM 이 만든 것을 LLM 없이 검사한다는 것이 핵심이다. 검사자가 같은
      모델이면 같은 착각을 공유한다.

▶ 왜 인용을 강제하는가 (공장 비용 구조)
      리드 하나는 시도 예산을 태우고 DSR/PBO 다중검정 회계에 잡힌다. 사용자가
      말하지 않은 경제 논리가 리드가 되면 **그 비용을 사용자 이름으로 치른다.**
      그래서 MECHANISM·COUNTERPARTY 같은 구조화 칸은 전부 원문의 구간을
      가리켜야 한다. 못 가리키면 리드가 아니라 **되묻는 응답**이 나간다.

▶ 무엇을 판정하지 않는가
      실행 가능성(AST_READY)은 여기서 안 본다. `lead_intake` 의 결정론 계약이
      이미 그것을 한다. 여기 책임은 하나 - "이 주장이 사용자의 말에서 나왔는가".

자체 점검:  python3 user_idea_language.py
"""

from __future__ import annotations

import re
import unicodedata

try:                                        # 배포본 경로
    from orchestration.contracts.user_idea_lead import (
        MAX_EVIDENCE_CHARS, MAX_IDEA_CHARS, IdeaEvidenceField, IdeaReasonCode,
        IdeaRejection, TextEvidence, UserIdeaCandidate, VerifiedIdeaLead,
        idea_text_sha256,
    )
except ImportError:                         # 자체 점검·개발 경로
    from user_idea_lead import (            # type: ignore[no-redef]
        MAX_EVIDENCE_CHARS, MAX_IDEA_CHARS, IdeaEvidenceField, IdeaReasonCode,
        IdeaRejection, TextEvidence, UserIdeaCandidate, VerifiedIdeaLead,
        idea_text_sha256,
    )

MODULE_VERSION = "user-idea-language-v1"

# 사용자 대화 URL. `check_link` 가 401 을 UNVERIFIED 로 보고 리드를 **살린다**
# (실측 2026-08-24: research-mcp 에서 이 경로가 401). 사용자의 그 대화가 곧
# 원문 출처이고, 나중에 알파를 감사할 때 여기로 되짚어 온다.
SOURCE_URL_TEMPLATE = "http://portfolio-bff:8000/ui/ceo/tasks/{task_id}/result"

# ── 발화 종류 판별 ──────────────────────────────────────────────────────────
# **원문을 보고** 판별한다. 후보가 스스로 신고한 종류는 믿지 않는다.
#
# ▶ 왜 질문을 거부하나
#   "이런 전략 어때?" 는 아이디어 제안이고, "OFI 가 뭐야?" 는 질문이다. 후자를
#   리드로 만들면 원장에 질문이 쌓인다. 다만 한국어 제안은 대개 의문형으로
#   끝나므로(-어때? -면 안 됨? -해보면?) **의문형이라는 이유만으로 거부하지
#   않는다.** 기전 서술이 함께 있으면 제안으로 본다.
_PROPOSAL_HINTS = re.compile(
    r"(전략|시그널|신호|알파|백테스트|검증|테스트|가설|만들어|해보|돌려|"
    r"strategy|signal|alpha|backtest|hypothesis)", re.I)
_MECHANISM_HINTS = re.compile(
    r"(때문|이유|왜냐|덕분|탓|유동성|호가|체결|수급|기관|외국인|개인|"
    r"마감|개장|변동성|스프레드|잔량|리밸런싱|because|liquidity|flow|spread)", re.I)

# 주문 지시 - 다른 경로(user_order_language)로 가야 한다.
_ORDER_INTENT = re.compile(
    r"(매수해|매도해|사줘|팔아|주문\s*(넣|해)|사자|손절해|익절해|"
    r"\bbuy\b|\bsell\b)\s*", re.I)

# 운영 요청 - 공장 아이디어가 아니다.
_OPERATIONAL = re.compile(
    r"(재시작|배포|로그\s*(봐|확인)|컨테이너|디스크|권한|재기동|"
    r"restart|deploy|docker|container)", re.I)

# 인용·예시 - 남의 말을 자기 주장으로 옮긴 것.
_QUOTED = re.compile(r"(라고\s*(하던|하네|한다|들었)|카더라|~라던데|"
                     r"논문에선|기사에선|according to)", re.I)

# 반증 가능성의 최소 신호: 방향·조건·시간 중 하나라도 있어야 잰다.
_FALSIFIABLE = re.compile(
    r"(오르|내리|상승|하락|반등|밀리|약하|강하|초과|미달|"
    r"다음\s*날|익일|장중|마감|개장|\d+\s*(분|시간|일|초)|"
    r"up|down|revert|fade|next\s*day|\bbps?\b)", re.I)

# 순수 가격 모양 - 기전 없는 차트 패턴은 리드가 아니다(공장 교리).
_PURE_PATTERN = re.compile(
    r"(골든크로스|데드크로스|헤드앤숄더|삼각수렴|지지선|저항선|"
    r"golden\s*cross|head\s*and\s*shoulders)", re.I)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _verify_evidence(raw: str, ev: TextEvidence) -> IdeaReasonCode | None:
    """인용 하나를 원문과 대조. 통과면 None."""
    if ev.end <= ev.start or ev.end > len(raw):
        return IdeaReasonCode.EVIDENCE_SPAN_INVALID
    if (ev.end - ev.start) > MAX_EVIDENCE_CHARS:
        return IdeaReasonCode.EVIDENCE_TOO_LONG
    if raw[ev.start:ev.end] != ev.text:
        return IdeaReasonCode.EVIDENCE_TEXT_MISMATCH
    return None


# 어느 칸이 인용을 **반드시** 가져야 하는가.
# MECHANISM 과 CLAIMED_EDGE 는 리드의 뼈대다 - 이 둘이 원문에 없으면 그
# 아이디어는 사용자 것이 아니라 LLM 것이다.
_REQUIRED_EVIDENCE = (IdeaEvidenceField.MECHANISM, IdeaEvidenceField.CLAIMED_EDGE)

# 값이 있으면 인용도 있어야 하는 칸(선택 칸이지만 지어내면 안 된다).
_EVIDENCE_IF_PRESENT = {
    "counterparty": IdeaEvidenceField.COUNTERPARTY,
    "market_context": IdeaEvidenceField.MARKET_CONTEXT,
    "failure_mode": IdeaEvidenceField.FAILURE_MODE,
    "observable_hint": IdeaEvidenceField.OBSERVABLE_HINT,
    "horizon_hint": IdeaEvidenceField.HORIZON_HINT,
}


def verify(raw_text: str, candidate: UserIdeaCandidate, *,
           root_task_id: str) -> VerifiedIdeaLead | IdeaRejection:
    """원문 + 후보 -> 검증된 리드 또는 거부. **부작용 없음**(순수 함수)."""
    codes: list[IdeaReasonCode] = []
    asks: list[str] = []
    raw = _norm(raw_text or "")

    # ① 무결성 - 같은 텍스트를 해석했는가
    if len(raw) > MAX_IDEA_CHARS:
        codes.append(IdeaReasonCode.IDEA_TEXT_TOO_LONG)
        asks.append(f"아이디어를 {MAX_IDEA_CHARS}자 안으로 줄여 주세요.")
    if idea_text_sha256(raw) != candidate.raw_text_sha256:
        # 다른 텍스트를 해석한 후보다. 여기서 멈춘다 - 인용 대조가 무의미하다.
        return IdeaRejection(
            reason_codes=(IdeaReasonCode.RAW_TEXT_HASH_MISMATCH,),
            needs_from_user=("해석 대상 원문이 바뀌었습니다. 다시 제안해 주세요.",))

    # ② 발화 종류 - 원문을 보고 판별한다
    if _ORDER_INTENT.search(raw):
        codes.append(IdeaReasonCode.ORDER_INTENT)
        asks.append("주문 지시로 보입니다. 주문은 페이퍼 주문 경로로 보내 주세요.")
    if _OPERATIONAL.search(raw) and not _PROPOSAL_HINTS.search(raw):
        codes.append(IdeaReasonCode.OPERATIONAL_REQUEST)
        asks.append("운영 요청으로 보입니다. 공장 아이디어 접수 대상이 아닙니다.")
    if _QUOTED.search(raw) and not _MECHANISM_HINTS.search(raw):
        codes.append(IdeaReasonCode.QUOTED_OR_EXAMPLE)
        asks.append("들은 이야기를 그대로 옮기신 것 같습니다. "
                    "왜 그런 수익이 남는다고 보시는지 본인 판단을 적어 주세요.")
    if not _PROPOSAL_HINTS.search(raw) and not _MECHANISM_HINTS.search(raw):
        codes.append(IdeaReasonCode.QUESTION_ONLY)
        asks.append("검증할 전략 제안이 아니라 질문으로 보입니다.")

    # ③ 내용 - 리드가 되기 위한 최소 골격
    if not _MECHANISM_HINTS.search(raw):
        codes.append(IdeaReasonCode.NO_MECHANISM)
        asks.append("왜 그 수익이 남는지(누가 왜 반대편에 서는지) 한 줄 적어 주세요.")
    if not _FALSIFIABLE.search(raw):
        codes.append(IdeaReasonCode.NO_FALSIFIABLE_CLAIM)
        asks.append("무엇이 얼마나 어느 기간에 움직인다는 것인지 적어 주세요 "
                    "- 그래야 틀렸을 때 틀렸다고 말할 수 있습니다.")
    if _PURE_PATTERN.search(raw) and not _MECHANISM_HINTS.search(raw):
        codes.append(IdeaReasonCode.PURE_PRICE_PATTERN)
        asks.append("가격 모양만으로는 리드가 되지 않습니다. "
                    "그 모양이 왜 생기는지(호가·수급·유동성) 적어 주세요.")
    if candidate.observable_hint is None and not _FALSIFIABLE.search(raw):
        codes.append(IdeaReasonCode.NO_OBSERVABLE)
        asks.append("무엇을 재면 되는지(호가 잔량·체결 강도 등) 알려 주세요.")

    # ④ 인용 대조 - **여기가 이 모듈의 존재 이유다**
    by_field: dict[IdeaEvidenceField, list[TextEvidence]] = {}
    for ev in candidate.evidence:
        bad = _verify_evidence(raw, ev)
        if bad is not None:
            if bad not in codes:
                codes.append(bad)
            continue
        by_field.setdefault(ev.field, []).append(ev)

    for required in _REQUIRED_EVIDENCE:
        if required not in by_field:
            if IdeaReasonCode.EVIDENCE_MISSING not in codes:
                codes.append(IdeaReasonCode.EVIDENCE_MISSING)
            asks.append(f"{required.value} 는 사용자 원문의 어느 대목에서 "
                        f"나왔는지 인용이 필요합니다.")

    for attr, field in _EVIDENCE_IF_PRESENT.items():
        if getattr(candidate, attr, None) and field not in by_field:
            if IdeaReasonCode.EVIDENCE_FIELD_MISMATCH not in codes:
                codes.append(IdeaReasonCode.EVIDENCE_FIELD_MISMATCH)
            asks.append(f"{field.value} 값을 채웠는데 원문 인용이 없습니다.")

    if codes:
        # 같은 문구가 여러 번 들어가지 않게 하되 순서는 유지한다.
        seen: set[str] = set()
        uniq = tuple(a for a in asks if not (a in seen or seen.add(a)))
        return IdeaRejection(reason_codes=tuple(codes), needs_from_user=uniq)

    return VerifiedIdeaLead(
        title=candidate.title,
        claimed_edge=candidate.claimed_edge,
        mechanism=candidate.mechanism,
        counterparty=candidate.counterparty,
        market_context=candidate.market_context,
        failure_mode=candidate.failure_mode,
        observable_hint=candidate.observable_hint,
        horizon_hint=candidate.horizon_hint,
        raw_text_sha256=candidate.raw_text_sha256,
        evidence=candidate.evidence,
        source_url=SOURCE_URL_TEMPLATE.format(task_id=root_task_id),
        root_task_id=root_task_id,
    )


# ── 리드 블록으로 옮기기 ────────────────────────────────────────────────────
def to_lead_block(lead: VerifiedIdeaLead) -> str:
    """`factory_submit_leads` 가 파싱하는 `KEY: value` 블록.

    ▶ READINESS 를 왜 `DATA_BLOCKED` 로 내보내나
      사용자의 서술은 거의 언제나 AST_READY 가 아니다(OBSERVABLES·
      CANDIDATE_SIGNAL_EXPR·SEMANTIC_PLAN·FORMULA_THESIS 전부 필요). 여기서
      AST_READY 라고 주장하면 `lead_intake` 가 정당하게 거부하고, 사용자는
      이유를 모르는 채 버려진다. **정직하게 DATA_BLOCKED 로 넣고**
      (status=BLOCKED, testability=VAGUE) 무엇이 없는지 MISSING_DATA 에
      적는다. 이후 공장 카드의 리서치 에이전트가 이 리드를 재료로 삼아
      실행 가능한 형태로 승격시킨다 - 그게 그 에이전트의 일이다.
    """
    lines = [
        f"TITLE: {lead.title}",
        f"URL: {lead.source_url}",
        f"MECHANISM: {lead.mechanism}",
        f"CLAIMED_EDGE: {lead.claimed_edge}",
        "READINESS: DATA_BLOCKED",
        "MISSING_DATA: 사용자 서술이라 실행 가능한 AST 가 아직 없다 - "
        "OBSERVABLES·CANDIDATE_SIGNAL_EXPR·SEMANTIC_PLAN·FORMULA_THESIS 필요",
    ]
    if lead.counterparty:
        lines.append(f"COUNTERPARTY: {lead.counterparty}")
    if lead.market_context:
        lines.append(f"MARKET_CONTEXT: {lead.market_context}")
    if lead.failure_mode:
        lines.append(f"FAILURE_MODE: {lead.failure_mode}")
    if lead.observable_hint or lead.horizon_hint:
        hint = " / ".join(x for x in (lead.observable_hint, lead.horizon_hint) if x)
        lines.append(f"TESTABLE_WITH: {hint}")
    # 발췌는 **사용자 원문 인용**이다 - 지어낸 요약이 아니라.
    quoted = " … ".join(e.text for e in lead.evidence[:4])
    if quoted:
        lines.append(f"EXCERPT: {quoted}")
    return "\n".join(lines)


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    fails = 0

    def ok(name: str, cond: bool):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    good_raw = ("마감 30분 전에 호가 잔량이 갑자기 얇아지는 종목은 "
                "기관 리밸런싱 물량 때문에 다음 날 개장에서 밀리는 것 같은데, "
                "이런 전략 백테스트 해볼 수 있을까?")
    h = idea_text_sha256(good_raw)

    def ev(field, needle):
        i = good_raw.index(needle)
        return TextEvidence(field=field, start=i, end=i + len(needle), text=needle)

    good = UserIdeaCandidate(
        raw_text_sha256=h,
        title="마감 전 호가 소멸 후 익일 개장 약세",
        claimed_edge="마감 30분 전 호가 잔량 급감 종목은 익일 개장에서 약세",
        mechanism="기관 리밸런싱 물량이 마감 전 유동성을 걷어가며, "
                  "그 잔여 물량이 익일 개장에 남아 가격을 누른다",
        counterparty="기관 리밸런싱 집행",
        observable_hint="호가 잔량",
        horizon_hint="다음 날 개장",
        evidence=(ev(IdeaEvidenceField.MECHANISM, "기관 리밸런싱 물량 때문에"),
                  ev(IdeaEvidenceField.CLAIMED_EDGE, "다음 날 개장에서 밀리는"),
                  ev(IdeaEvidenceField.COUNTERPARTY, "기관 리밸런싱 물량"),
                  ev(IdeaEvidenceField.OBSERVABLE_HINT, "호가 잔량"),
                  ev(IdeaEvidenceField.HORIZON_HINT, "다음 날 개장")),
    )
    r = verify(good_raw, good, root_task_id="t_abc123")
    ok("정상 아이디어는 통과한다", isinstance(r, VerifiedIdeaLead))
    ok("출처 URL 이 사용자 대화를 가리킨다",
       isinstance(r, VerifiedIdeaLead) and r.source_url.endswith("/t_abc123/result"))

    # 지어낸 인용
    forged = good.model_copy(update={"evidence": (
        TextEvidence(field=IdeaEvidenceField.MECHANISM, start=0, end=10,
                     text="외국인이 매도하기 때문"),
        ev(IdeaEvidenceField.CLAIMED_EDGE, "다음 날 개장에서 밀리는"))})
    r2 = verify(good_raw, forged, root_task_id="t_abc123")
    ok("원문에 없는 인용은 거부",
       isinstance(r2, IdeaRejection)
       and IdeaReasonCode.EVIDENCE_TEXT_MISMATCH in r2.reason_codes)

    # 필수 인용 누락
    r3 = verify(good_raw, good.model_copy(update={"evidence": ()}),
                root_task_id="t_abc123")
    ok("필수 인용이 없으면 거부",
       isinstance(r3, IdeaRejection)
       and IdeaReasonCode.EVIDENCE_MISSING in r3.reason_codes)
    ok("거부는 되물음을 함께 낸다",
       isinstance(r3, IdeaRejection) and len(r3.needs_from_user) > 0)

    # 다른 원문 해석
    r4 = verify("완전히 다른 텍스트", good, root_task_id="t_abc123")
    ok("원문 해시가 다르면 즉시 거부",
       isinstance(r4, IdeaRejection)
       and r4.reason_codes == (IdeaReasonCode.RAW_TEXT_HASH_MISMATCH,))

    # 주문 지시
    order_raw = "삼성전자 100주 매수해줘"
    r5 = verify(order_raw, UserIdeaCandidate(
        raw_text_sha256=idea_text_sha256(order_raw),
        title="삼성전자 매수 지시", claimed_edge="삼성전자를 매수한다",
        mechanism="x" * 25),
        root_task_id="t_x")
    ok("주문 지시는 다른 경로로 돌린다",
       isinstance(r5, IdeaRejection)
       and IdeaReasonCode.ORDER_INTENT in r5.reason_codes)

    # 순수 차트 패턴
    pat_raw = "골든크로스 나면 사는 전략 검증해줘"
    r6 = verify(pat_raw, UserIdeaCandidate(
        raw_text_sha256=idea_text_sha256(pat_raw),
        title="골든크로스", claimed_edge="골든크로스 매수", mechanism="y" * 25),
        root_task_id="t_x")
    ok("기전 없는 가격 모양은 거부",
       isinstance(r6, IdeaRejection)
       and IdeaReasonCode.PURE_PRICE_PATTERN in r6.reason_codes)

    # 리드 블록
    if isinstance(r, VerifiedIdeaLead):
        block = to_lead_block(r)
        ok("리드 블록에 필수 4개가 있다",
           all(k in block for k in ("TITLE:", "URL:", "MECHANISM:", "READINESS:")))
        ok("READINESS 는 정직하게 DATA_BLOCKED", "READINESS: DATA_BLOCKED" in block)
        ok("MISSING_DATA 를 함께 낸다", "MISSING_DATA:" in block)
        ok("EXCERPT 는 사용자 원문 인용", "기관 리밸런싱 물량 때문에" in block)

    # 비구속 못
    ok("후보는 타입 수준에서 비구속", good.binding is False)

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건", flush=True)
    return fails


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
