#!/usr/bin/env python3
"""공시 사건 분류 - 무엇이 중요한 공시인가를 결정론으로 판정한다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 "공급 계약 공시 같은 중요한 내용만 분석하는
      에이전트 신설해야 할거 같은데".

▶ 왜 분류가 먼저인가
  실측(2026-08-03, 최근 30일 opendart 3,579건): 상위 유형이
  임원소유상황 406, 효력발생안내 289, 대량보유보고 230, 투자설명서 202…
  **대부분이 절차·형식 공시다.** 단일판매·공급계약이나 유상증자처럼 실제로
  주가에 영향을 주는 것은 소수인데, 지금은 전부 같은 무게로 뉴스 옆에 쌓인다.
  분석가를 붙이기 전에 "무엇을 볼 것인가"를 코드가 정해야 한다 - 그 판단을
  LLM 에 맡기면 매번 다르게 걸러지고, 왜 걸러졌는지 재검산이 안 된다.

▶ 등급 (severity)
  MATERIAL  주가에 직접 영향. 분석가가 반드시 본다.
  NOTABLE   맥락상 의미 있음. 여력이 있으면 본다.
  ROUTINE   절차·형식. 세기만 하고 분석하지 않는다.
  **ROUTINE 도 버리지 않는다** - 몇 건이 걸러졌는지 보고한다. 조용한 절단은
  "그날 공시가 없었다"와 구분되지 않는다.

▶ 제목만으로 판정한다
  단일판매·공급계약은 거래소(KIND) 공시라 DART 36개 구조화 API 에 **없다**
  (실측). 본문은 라이선스상 저장하지 않는다(가이드 3.3). 그래서 제목이
  유일하게 확실한 재료다. 제목에 없는 것을 추측하지 않는다 - 계약금액이
  필요하면 그건 별도 수집(KIND) 과제이지 여기서 지어낼 것이 아니다.

▶ 정정 공시를 원본과 같은 등급으로 본다
  "[기재정정]단일판매ㆍ공급계약체결" 은 여전히 공급계약 사건이다. 다만
  is_correction 으로 표시해 하류가 구분할 수 있게 한다.

실행: python evidence/disclosure_events.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import re
import sys

MODULE_VERSION = "research-disclosure-events-v1"

MATERIAL, NOTABLE, ROUTINE = "MATERIAL", "NOTABLE", "ROUTINE"

# (사건코드, 등급, 제목 정규식, 한 줄 설명)
# **순서가 판정 순서다** - 위에서 먼저 맞으면 거기서 끝난다. 구체적인 것을
# 위에 둔다(자기주식'취득'과 '처분'은 뜻이 반대라 따로 잡는다).
RULES: tuple[tuple[str, str, str, str], ...] = (
    # ── 매출에 직접 꽂히는 것 ───────────────────────────────────────────────
    ("SUPPLY_CONTRACT", MATERIAL, r"단일판매|공급계약",
     "단일판매·공급계약 - 매출로 직결된다. 계약금액/최근매출액 비율이 핵심"),
    ("EARNINGS", MATERIAL, r"영업\s*\(?잠정\)?실적|결산실적|매출액또는손익",
     "잠정실적 - 실적 서프라이즈의 1차 재료"),
    # ── 주주가치 희석·환원 ─────────────────────────────────────────────────
    ("RIGHTS_OFFERING", MATERIAL, r"유상증자결정|유상증자\s*결정",
     "유상증자 - 희석. 발행규모/시총 비율이 핵심"),
    ("CB_BW_ISSUE", MATERIAL, r"전환사채권?발행|신주인수권부사채권?발행|교환사채권?발행",
     "메자닌 발행 - 잠재 희석"),
    # 해지가 취득보다 **위에** 있어야 한다 - "자기주식취득신탁계약해지" 는
    # '자기주식취득' 을 포함하므로 아래에 두면 영원히 BUYBACK 으로 잡힌다
    # (실측 2026-08-03 자체 점검이 적발). 규칙 순서가 곧 판정 순서다.
    ("BUYBACK_CANCEL", NOTABLE, r"자기주식취득신탁계약해지",
     "자기주식 신탁 해지 - 환원 축소 신호일 수 있다"),
    ("BUYBACK", MATERIAL, r"자기주식취득",
     "자기주식 취득 - 주주환원(신탁 체결 포함)"),
    ("TREASURY_DISPOSAL", MATERIAL, r"자기주식처분",
     "자기주식 처분 - 유통물량 증가"),
    ("FREE_OFFERING", NOTABLE, r"무상증자",
     "무상증자 - 희석은 없으나 수급 이벤트"),
    ("CAPITAL_REDUCTION", MATERIAL, r"감자결정|감자\s*결정",
     "감자 - 자본 구조 변화"),
    # ── 존속·법적 위험 ─────────────────────────────────────────────────────
    ("DEFAULT", MATERIAL, r"부도발생|당좌거래정지",
     "부도 - 존속 위험"),
    ("INSOLVENCY", MATERIAL, r"회생절차|해산사유|채권은행등의관리절차개시",
     "회생·해산·관리절차 - 존속 위험"),
    ("BIZ_SUSPENSION", MATERIAL, r"영업정지",
     "영업정지 - 매출 중단"),
    ("LITIGATION", NOTABLE, r"소송등의제기|소송등의제기ㆍ신청",
     "소송 제기 - 금액에 따라 영향이 갈린다"),
    ("EMBEZZLEMENT", MATERIAL, r"횡령|배임",
     "횡령·배임 - 관리종목 지정 위험"),
    # ── 지배구조·구조 변경 ─────────────────────────────────────────────────
    ("MERGER", MATERIAL, r"회사합병결정|회사분할|주식교환|분할합병",
     "합병·분할 - 구조 변경"),
    ("BIZ_TRANSFER", MATERIAL, r"영업양수|영업양도",
     "영업 양수도 - 사업 구성 변경"),
    ("ASSET_TRANSFER", NOTABLE, r"유형자산\s*양[수도]|자산양수도",
     "자산 양수도 - 규모에 따라"),
    ("STAKE_CHANGE", NOTABLE, r"최대주주.*변경|경영권.*양도",
     "최대주주 변경 - 지배구조"),
    # ── 절차·형식 (세되 분석하지 않는다) ───────────────────────────────────
    ("INSIDER_HOLDING", ROUTINE, r"임원ㆍ?주요주주|특정증권등소유",
     "임원·주요주주 소유상황 - 정기 보고"),
    ("BULK_HOLDING", ROUTINE, r"대량보유상황보고",
     "5% 보고 - 지분 변동이나 대개 절차성"),
    ("PROSPECTUS", ROUTINE, r"투자설명서|일괄신고|증권발행실적|효력발생",
     "증권신고 절차 - 형식 공시"),
    ("IR", ROUTINE, r"기업설명회|IR개최",
     "IR 안내 - 일정 공지"),
    ("PAYMENT_INFO", ROUTINE, r"지급수단별|지급기간별",
     "하도급 지급 공시 - 정기 보고"),
    ("RELATED_PARTY", NOTABLE, r"특수관계인|계열회사와의상품",
     "특수관계인 거래 - 규모에 따라"),
)

_CORRECTION_RE = re.compile(r"\[기재정정\]|\[정정\]|정정공시")
_AUTONOMOUS_RE = re.compile(r"자율공시")
_FAIR_RE = re.compile(r"공정공시")

SEVERITY_ORDER = {MATERIAL: 0, NOTABLE: 1, ROUTINE: 2}


def _norm(title: str) -> str:
    """비교용 정규화 - 공백·중점을 지운다.

    DART 제목은 '단일판매ㆍ공급계약체결' 처럼 중점(ㆍ)이 섞이고 띄어쓰기가
    들쭉날쭉하다. 정규식마다 그것을 매번 처리하면 하나씩 빠뜨린다.
    """
    return re.sub(r"[\sㆍ·,]", "", title or "")


def classify(title: str) -> dict:
    """공시 제목 -> 사건 분류. 순수 함수라 자체 점검이 전부 검사한다.

    맞는 규칙이 없으면 UNKNOWN/ROUTINE 이다 - **모르는 것을 MATERIAL 로
    올리지 않는다.** 새 유형이 중요하면 규칙을 추가하는 것이 사람의 일이고,
    그 전까지 조용히 중요한 척하면 분석가가 쓰레기를 읽는다.
    """
    raw = title or ""
    n = _norm(raw)
    for code, sev, pattern, why in RULES:
        if re.search(_norm(pattern).replace("|", "|"), n) or re.search(pattern, n):
            return {
                "event_code": code,
                "severity": sev,
                "why": why,
                "is_correction": bool(_CORRECTION_RE.search(raw)),
                "is_autonomous": bool(_AUTONOMOUS_RE.search(raw)),
                "is_fair_disclosure": bool(_FAIR_RE.search(raw)),
                "title": raw,
            }
    return {"event_code": "UNKNOWN", "severity": ROUTINE,
            "why": "규칙에 없는 유형 - 중요하다고 판정하지 않는다(사람이 규칙 추가)",
            "is_correction": bool(_CORRECTION_RE.search(raw)),
            "is_autonomous": bool(_AUTONOMOUS_RE.search(raw)),
            "is_fair_disclosure": bool(_FAIR_RE.search(raw)),
            "title": raw}


def triage(docs, *, keep=(MATERIAL, NOTABLE)) -> dict:
    """문서 목록 -> 분석 대상과 통계.

    **걸러낸 수를 반드시 보고한다.** 조용한 절단은 "그날 공시가 없었다"와
    구분되지 않고, 그 둘을 섞으면 '공시가 없어서 조용한 날' 과 '분류기가
    막은 날' 이 같아 보인다.
    """
    kept, dropped = [], []
    by_code: dict[str, int] = {}
    by_sev: dict[str, int] = {MATERIAL: 0, NOTABLE: 0, ROUTINE: 0}
    for d in docs or []:
        c = classify(d.get("title") if isinstance(d, dict) else str(d))
        by_code[c["event_code"]] = by_code.get(c["event_code"], 0) + 1
        by_sev[c["severity"]] += 1
        row = {**(d if isinstance(d, dict) else {}), **c}
        (kept if c["severity"] in keep else dropped).append(row)
    kept.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], r["event_code"]))
    return {
        "kept": kept,
        "dropped_count": len(dropped),
        "total": len(docs or []),
        "by_severity": by_sev,
        "by_event": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        "keep_rule": f"severity in {list(keep)} - ROUTINE 은 세기만 하고 분석 제외",
    }


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음. **실제 DB 제목으로 검사한다**
# ---------------------------------------------------------------------------

# 2026-08-03 실측: research.documents 의 opendart 제목 원문
_REAL = [
    ("단일판매ㆍ공급계약체결", "SUPPLY_CONTRACT", MATERIAL),
    ("단일판매ㆍ공급계약체결(자율공시)", "SUPPLY_CONTRACT", MATERIAL),
    ("[기재정정]단일판매ㆍ공급계약체결", "SUPPLY_CONTRACT", MATERIAL),
    ("주요사항보고서(유상증자결정)", "RIGHTS_OFFERING", MATERIAL),
    ("주요사항보고서(자기주식취득신탁계약체결결정)", "BUYBACK", MATERIAL),
    ("주요사항보고서(자기주식취득신탁계약해지결정)", "BUYBACK_CANCEL", NOTABLE),
    ("연결재무제표기준영업(잠정)실적(공정공시)", "EARNINGS", MATERIAL),
    ("주요사항보고서(소송등의제기)", "LITIGATION", NOTABLE),
    ("임원ㆍ주요주주특정증권등소유상황보고서", "INSIDER_HOLDING", ROUTINE),
    ("주식등의대량보유상황보고서", "BULK_HOLDING", ROUTINE),
    ("효력발생안내", "PROSPECTUS", ROUTINE),
    ("투자설명서", "PROSPECTUS", ROUTINE),
    ("기업설명회(IR)개최(안내공시)", "IR", ROUTINE),
    ("지급수단별ㆍ지급기간별지급금액", "PAYMENT_INFO", ROUTINE),
]


def _check_real_titles():
    """실제 DB 제목이 의도한 등급으로 분류되는가 - 합성 제목으로만 검사하면
    현실의 중점·괄호·접두사를 놓친다."""
    bad = []
    for title, want_code, want_sev in _REAL:
        got = classify(title)
        if got["event_code"] != want_code or got["severity"] != want_sev:
            bad.append(f"{title[:30]} -> {got['event_code']}/{got['severity']} "
                       f"(기대 {want_code}/{want_sev})")
    assert not bad, "실측 제목 오분류:\n    " + "\n    ".join(bad)
    print(f"  실측 제목 {len(_REAL)}건 분류    OK")


def _check_correction_and_flags():
    c = classify("[기재정정]단일판매ㆍ공급계약체결")
    assert c["is_correction"] and c["severity"] == MATERIAL, \
        "정정이어도 사건 등급은 그대로다 - 정정본이 덜 중요한 것이 아니다"
    a = classify("단일판매ㆍ공급계약체결(자율공시)")
    assert a["is_autonomous"] and not a["is_correction"]
    f = classify("연결재무제표기준영업(잠정)실적(공정공시)")
    assert f["is_fair_disclosure"]
    print("  정정·자율·공정 표시      OK")


def _check_unknown_is_not_material():
    """모르는 유형을 중요하다고 하지 않는다 - 분석가가 쓰레기를 읽게 된다."""
    for t in ("듣도보도못한공시", "", None, "정기주주총회소집공고"):
        c = classify(t)
        assert c["severity"] == ROUTINE, (t, c)
    assert classify("듣도보도못한공시")["event_code"] == "UNKNOWN"
    print("  미지 유형 = ROUTINE      OK")


def _check_triage_reports_drops():
    docs = [{"title": t} for t, _c, _s in _REAL]
    r = triage(docs)
    assert r["total"] == len(_REAL)
    # MATERIAL·NOTABLE 만 남고 ROUTINE 은 세어진다
    assert all(x["severity"] in (MATERIAL, NOTABLE) for x in r["kept"])
    assert r["dropped_count"] == r["by_severity"][ROUTINE] > 0
    assert len(r["kept"]) + r["dropped_count"] == r["total"], "세다가 잃어버렸다"
    # 중요한 것이 위로 온다
    assert r["kept"][0]["severity"] == MATERIAL
    # 빈 입력도 정직하게
    e = triage([])
    assert e["total"] == 0 and e["kept"] == [] and e["dropped_count"] == 0
    print("  선별·누락 보고           OK")


def _check_rules_are_wellformed():
    """규칙표 자체의 무결성 - 코드 중복이나 잘못된 등급을 배포 전에 잡는다."""
    codes = [c for c, _s, _p, _w in RULES]
    assert len(codes) == len(set(codes)), "event_code 중복"
    for code, sev, pattern, why in RULES:
        assert sev in SEVERITY_ORDER, (code, sev)
        assert why.strip(), f"{code}: 설명 없는 규칙은 나중에 아무도 못 고친다"
        re.compile(pattern)          # 깨진 정규식이면 여기서 터진다
    print(f"  규칙표 무결성({len(RULES)}종)   OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_rules_are_wellformed()
    _check_real_titles()
    _check_correction_and_flags()
    _check_unknown_is_not_material()
    _check_triage_reports_drops()
    print("공시 분류 5개 영역 통과.")
