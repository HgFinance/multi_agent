"""추천 카드 -> 사용자 답변. 모든 문장에 **근거 등급**을 붙인다.

## 왜 등급제인가

2026 연구가 지적하는 LLM 금융조언의 핵심 실패는 "데이터가 낡았든 검증이 안
됐든 **같은 확신으로 답한다**"는 것이다. 처방은 "답변 조각마다 신뢰도 지표"다.
CFA Standard V(B) 도 "투자 프로세스의 중대한 리스크와 **한계**"를 밝히라고 한다.

우리 경우 이게 장식이 아니다. 2026-08-25 백테스트 결과:
  - 모멘텀 랭킹: 초과수익 +0.07%, t=+0.12 -> **엣지 없음**
  - 반전 신호: IC -0.08 은 재현되나 손익으로 전이 안 됨(낙폭 -59%)
따라서 종합점수를 "추천 강도"처럼 보여주면 **없는 근거를 있는 것처럼 파는 것**이
된다. 등급을 붙이면 같은 카드가 정직해진다.

## 다섯 등급

| 등급 | 뜻 | 예 |
|---|---|---|
| MEASURED | 예측력이 백테스트로 측정됨 | (현재 해당 없음) |
| OBSERVED | 데이터에서 직접 읽은 사실 | 종가, 외국인 순매수량, 공매도 비중, 스윙 접점 |
| DERIVED | 관측에서 **명시된 규칙**으로 계산 | ATR, 목표가, 손절가 |
| JUDGED | LLM 의 서술 판단 | 뉴스 호재/악재 |
| UNVALIDATED | 계산은 되나 예측력 미측정 | 종합점수, 축 가중치 |

DERIVED 는 "규칙은 재현 가능하나 그 규칙이 수익을 낸다는 근거는 별개"라는 뜻이다.
목표가가 여기 속한다 - 스윙 저항에서 계산했으니 재현되지만, 도달률은 31% 였다.

## 규제 요건 (자본시장법 설명의무·위험고지·적합성)

- 위험과 **최대 손실**을 수치로 밝힌다
- 투자 기간을 명시한다
- 논지가 깨지는 조건(무효화 조건)을 적는다
- 적합성: 투자성향과 맞는지 별도로 표시한다
- 이 산출물이 투자권유인지 정보제공인지 명확히 한다
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MEASURED = "MEASURED"
OBSERVED = "OBSERVED"
DERIVED = "DERIVED"
JUDGED = "JUDGED"
UNVALIDATED = "UNVALIDATED"

TIER_ORDER = (MEASURED, OBSERVED, DERIVED, JUDGED, UNVALIDATED)
TIER_LABEL = {
    MEASURED: "측정됨",
    OBSERVED: "관측됨",
    DERIVED: "산출됨",
    JUDGED: "판단",
    UNVALIDATED: "미검증",
}
# 이 등급 이하만 있으면 '투자권유'로 부를 수 없다.
ADVICE_REQUIRES = MEASURED


@dataclass(frozen=True)
class Claim:
    """답변의 한 문장. 등급과 출처가 반드시 붙는다."""

    text: str
    tier: str
    source: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIER_ORDER:
            raise ValueError(f"알 수 없는 근거 등급: {self.tier}")
        if not self.text.strip():
            raise ValueError("빈 주장은 담지 않는다")
        if self.tier in (OBSERVED, MEASURED) and not self.source.strip():
            # 사실을 주장하면서 출처가 없으면 확인할 방법이 없다.
            raise ValueError(f"{self.tier} 는 출처가 필요하다: {self.text[:40]}")

    def render(self) -> str:
        src = f"  [{self.source}]" if self.source else ""
        return f"[{TIER_LABEL[self.tier]}] {self.text}{src}"


@dataclass
class Section:
    title: str
    claims: list[Claim] = field(default_factory=list)

    def add(self, text: str, tier: str, source: str = "") -> None:
        self.claims.append(Claim(text, tier, source))


def _fmt(v: Any, suffix: str = "") -> str:
    try:
        return f"{float(v):,.0f}{suffix}"
    except (TypeError, ValueError):
        return f"{v}{suffix}"


def build_answer(card: dict, *, as_of: str, profile: dict | None = None) -> dict:
    """카드 하나를 등급이 붙은 답변 구조로 바꾼다."""
    sections: list[Section] = []
    plan = card.get("plan") or {}
    comp = card.get("composite") or {}
    last = card.get("last_close")
    symbol, company = card.get("symbol", ""), card.get("company", "")

    # ── 1. 무엇을 보고 있나 ──────────────────────────────────────────────
    s = Section("종목")
    s.add(f"{company}({symbol}) 종가 {_fmt(last, '원')}, 기준일 {as_of}",
          OBSERVED, "market.market_bars 1D")
    if card.get("업종"):
        s.add(f"업종 {card['업종']}", OBSERVED, "ls:t3320")
    sections.append(s)

    # ── 2. 관측된 사실 ───────────────────────────────────────────────────
    s = Section("관측된 사실")
    for a in card.get("axes", []):
        if a.get("status") != "OK" or not a.get("summary"):
            continue
        src = {"flow": "ls:t1717", "short": "ls:t1927",
               "valuation": "ls:t3320", "momentum": "market.market_bars 1D",
               "theme": "ls:t1532", "ownership": "dart:지분공시"}.get(a["axis"])
        if src:
            s.add(a["summary"], OBSERVED, src)
    sections.append(s)

    # ── 3. 뉴스·공시 판단 ────────────────────────────────────────────────
    nar = card.get("narrative") or {}
    if nar.get("news") or nar.get("disclosures"):
        s = Section("뉴스·공시")
        # 호재/악재 판정을 **안 한** 항목은 JUDGED 가 아니다. "이런 제목의
        # 기사가 있다"는 관측된 사실이고, 판단 등급을 붙이면 하지도 않은
        # 판단을 한 것처럼 보인다(대화 경로는 2층 LLM 판정을 돌리지 않는다).
        for item, limit in ((nar.get("news") or [], 4),
                            (nar.get("disclosures") or [], 3)):
            for it in item[:limit]:
                pol = str(it.get("polarity") or "").strip()
                title = str(it.get("title", ""))[:60]
                src = str(it.get("evidence_id") or it.get("citation") or "")
                if pol and pol != "미판정":
                    s.add(f"[{pol}] {title} — {it.get('why','')}", JUDGED, src)
                elif src:
                    s.add(f"{title} (호재/악재 미판정)", OBSERVED, src)
        sections.append(s)

    # ── 3.5 매집 근거 ────────────────────────────────────────────────────
    # 지분공시는 "누가 얼마나 샀나"가 **공시 원문에 적힌 사실**이다. 접수번호를
    # 실어 확인 경로를 남긴다 - 이게 없으면 예측과 구분되지 않는다.
    own = card.get("ownership") or {}
    if own.get("evidence"):
        s = Section("매집 근거 (지분공시)")
        for e in own["evidence"][:4]:
            change = e.get("ratio_change_pp")
            change_txt = f"{change:+.2f}%p" if isinstance(change, (int, float)) else "?"
            s.add(f"{e.get('filed_at','')} {e.get('holder','')} {change_txt} "
                  f"(보유 {e.get('ratio_after_pct')}%) — {e.get('reason','')}"
                  f" [{e.get('reason_class')}]",
                  OBSERVED, f"dart:{e.get('rcept_no')}")
        s.add("지분공시는 후행 지표다 — 5% 룰은 5영업일 내 보고라 공시 시점엔 "
              "이미 매수가 끝나 있다.", OBSERVED, "자본시장법 제147조")
        sections.append(s)

    # ── 4. 가격 계획 ─────────────────────────────────────────────────────
    if plan.get("target") is not None:
        s = Section("가격 계획")
        for lv in (plan.get("supports") or [])[:2]:
            s.add(f"지지 {_fmt(lv['price'])} — 스윙 저점 {lv['touches']}회 접점",
                  OBSERVED, "market.market_bars 1D")
        for lv in (plan.get("resistances") or [])[:2]:
            s.add(f"저항 {_fmt(lv['price'])} — 스윙 고점 {lv['touches']}회 접점",
                  OBSERVED, "market.market_bars 1D")
        s.add(f"진입 {_fmt(plan.get('entry_low'))}~{_fmt(plan.get('entry_high'))}, "
              f"목표 {_fmt(plan.get('target'))}, 손절 {_fmt(plan.get('stop'))} "
              f"(손익비 {plan.get('reward_risk')})", DERIVED,
              f"{plan.get('target_basis','')} / {plan.get('stop_basis','')}")
        sections.append(s)

    # ── 5. 위험 (자본시장법 위험고지) ────────────────────────────────────
    s = Section("위험")
    if plan.get("stop") is not None and last:
        loss = plan["stop"] / float(last) - 1.0
        s.add(f"손절가 도달 시 손실 {loss:.1%}. 갭 하락하면 그보다 커질 수 있다.",
              DERIVED, "손절가 대비 현재가")
    s.add("과거 검증에서 목표가 선도달 31.0%, 손절 선도달 62.1%였다. "
          "이 계획대로 해도 손절이 먼저 닿을 확률이 두 배다.",
          MEASURED, "backtest_momentum.py dev 2016-2022, n=5,674")
    s.add("종목 선별 랭킹은 초과수익이 확인되지 않았다"
          "(초과 +0.07%, t=+0.12, 왕복 30bp 기준).",
          MEASURED, "backtest_reversal_pnl.py dev, n=83 리밸런스")
    if own.get("evidence"):
        s.add("'기관·내부자가 샀다'가 '오른다'는 뜻이 아니다. 그 관계는 "
              "측정한 적이 없다.", UNVALIDATED, "")
    sections.append(s)

    # ── 6. 무효화 조건 ───────────────────────────────────────────────────
    s = Section("무효화 조건")
    if plan.get("stop") is not None:
        s.add(f"종가가 {_fmt(plan['stop'])} 아래로 마감하면 이 계획은 무효다.",
              DERIVED, "손절 규칙")
    for a in card.get("axes", []):
        if a.get("axis") == "flow" and a.get("status") == "OK" and (a.get("value") or 0) > 0:
            s.add("외국인·기관 순매수 추세가 끊기면 근거 하나가 사라진다.",
                  DERIVED, "수급 축 정의")
    sections.append(s)

    # ── 7. 종합점수 - 반드시 미검증으로 ──────────────────────────────────
    s = Section("종합점수")
    if comp.get("status") == "OK":
        s.add(f"{comp.get('display')}/100 (유효축 {comp.get('effective_weight', 0):.0%}). "
              "축 가중치는 임의로 정한 값이고 예측력이 측정되지 않았다 — "
              "순위 비교용이지 수익 예측이 아니다.", UNVALIDATED, "")
    elif card.get("ownership"):
        # 관측 기반 추천은 애초에 점수를 만들지 않는다. blend_axes 의
        # "미보고 축" 문구를 그대로 내면 축이 빠진 것처럼 읽힌다 - 실제로는
        # 위 '관측된 사실' 절에 사실로 실려 있고 가중합만 안 한 것이다.
        s.add("산출하지 않음 — 이 추천의 근거는 관측이지 예측이 아니다. "
              "축을 가중합해 점수를 만들면 예측력이 있는 것처럼 보인다.",
              OBSERVED, "관측 기반 추천 정책")
    else:
        s.add(f"산출하지 않음 — {comp.get('reason','')}", OBSERVED, "blend_axes")
    for a in card.get("axes", []):
        if a.get("status") != "OK":
            s.add(f"{a['axis']} 축 제외: {a.get('reason','')}", OBSERVED, "축 상태")
    sections.append(s)

    tiers_present = {c.tier for sec in sections for c in sec.claims}
    # 예측력이 측정된 **긍정** 근거가 없으면 투자권유가 아니다.
    positive_measured = any(
        c.tier == MEASURED and "확인되지 않" not in c.text and "손절이 먼저" not in c.text
        for sec in sections for c in sec.claims)
    kind = "투자권유" if positive_measured else "정보제공"

    return {
        "symbol": symbol,
        "company": company,
        "as_of": as_of,
        "kind": kind,
        "sections": [{"title": s.title,
                      "claims": [{"text": c.text, "tier": c.tier, "source": c.source}
                                 for c in s.claims]}
                     for s in sections if s.claims],
        "tiers_present": sorted(tiers_present, key=TIER_ORDER.index),
        "suitability": _suitability(card, profile),
        "disclaimer": (
            "이 산출물은 정보제공이며 투자권유가 아니다. 종목 선별 랭킹의 초과수익이 "
            "검증되지 않았고, 유니버스에 상장폐지 종목이 없어 과거 통계가 위쪽으로 "
            "치우쳐 있다. 투자 결정과 그 결과는 이용자 책임이다."
            if kind == "정보제공" else
            "투자에는 원금 손실 위험이 있다."),
    }


def _suitability(card: dict, profile: dict | None) -> dict:
    """적합성 - 투자성향과 맞는지. 프로필이 없으면 '판정 불가'다(통과가 아니다)."""
    if not profile:
        return {"status": "UNKNOWN",
                "reason": "투자성향 정보가 없어 적합성을 판정할 수 없다"}
    plan = card.get("plan") or {}
    risk_pct = plan.get("risk_pct")
    tolerance = profile.get("max_drawdown_pct")
    if risk_pct is None or tolerance is None:
        return {"status": "UNKNOWN", "reason": "손실허용도 또는 손절폭 정보 없음"}
    if float(risk_pct) > float(tolerance):
        return {"status": "UNSUITABLE",
                "reason": f"손절폭 {float(risk_pct):.1%} > 허용 낙폭 {float(tolerance):.1%}"}
    return {"status": "SUITABLE",
            "reason": f"손절폭 {float(risk_pct):.1%} <= 허용 낙폭 {float(tolerance):.1%}"}


def render(answer: dict) -> str:
    out = [f"{answer['company']}({answer['symbol']})  ·  기준 {answer['as_of']}",
           f"구분: {answer['kind']}", ""]
    for sec in answer["sections"]:
        out.append(f"── {sec['title']} " + "─" * max(0, 60 - len(sec["title"])))
        for c in sec["claims"]:
            src = f"   [{c['source']}]" if c["source"] else ""
            out.append(f"  [{TIER_LABEL[c['tier']]}] {c['text']}{src}")
        out.append("")
    su = answer["suitability"]
    out.append(f"── 적합성 ─── {su['status']}: {su['reason']}")
    out.append("")
    out.append(f"※ {answer['disclaimer']}")
    return "\n".join(out)


# ── 자체 점검 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 등급 없는 사실 주장은 못 만든다
    try:
        Claim("종가 1000원", OBSERVED)
        raise AssertionError("출처 없는 OBSERVED 를 통과시켰다")
    except ValueError:
        pass
    try:
        Claim("아무말", "GUESS")
        raise AssertionError("모르는 등급을 통과시켰다")
    except ValueError:
        pass
    # JUDGED/UNVALIDATED 는 출처가 없어도 된다(LLM 서술·임의 가중치)
    Claim("호재로 보인다", JUDGED)
    Claim("72/100", UNVALIDATED)

    card = {
        "symbol": "005930", "company": "삼성전자", "업종": "FICS 반도체",
        "last_close": 257000,
        "axes": [
            {"axis": "flow", "status": "OK", "value": -0.4,
             "summary": "외인 3일 순매도, 기관 5일 순매도", "reason": ""},
            {"axis": "theme", "status": "NO_SOURCE", "value": None,
             "summary": "", "reason": "수집 소스 없음"},
        ],
        "plan": {"entry_low": 249000, "entry_high": 258000, "target": 285000,
                 "stop": 246000, "reward_risk": 1.9, "risk_pct": 0.043,
                 "target_basis": "저항 285,000", "stop_basis": "지지 251,500 하단",
                 "supports": [{"price": 251500, "touches": 12}],
                 "resistances": [{"price": 285000, "touches": 2}]},
        "composite": {"status": "OK", "display": 72, "effective_weight": 0.64},
        "narrative": {"news": [{"polarity": "호재", "title": "HBM 수주 확대",
                                "why": "메모리 가격 협상력 상승", "evidence_id": "n1"}],
                      "disclosures": []},
    }

    ans = build_answer(card, as_of="2026-08-25")
    # 예측력이 측정된 긍정 근거가 없으므로 투자권유가 아니다
    assert ans["kind"] == "정보제공", ans["kind"]
    assert "투자권유가 아니다" in ans["disclaimer"]
    # 종합점수는 반드시 미검증 등급이다
    score_claims = [c for s in ans["sections"] if s["title"] == "종합점수"
                    for c in s["claims"]]
    assert score_claims[0]["tier"] == UNVALIDATED, score_claims[0]
    # 백테스트 사실이 위험 절에 MEASURED 로 들어간다
    risk = [c for s in ans["sections"] if s["title"] == "위험" for c in s["claims"]]
    assert any(c["tier"] == MEASURED and "31.0%" in c["text"] for c in risk), risk
    assert any("초과수익이 확인되지 않았다" in c["text"] for c in risk)
    # 프로필이 없으면 적합성은 통과가 아니라 판정불가
    assert ans["suitability"]["status"] == "UNKNOWN"

    # 프로필이 있으면 판정한다
    ok = build_answer(card, as_of="2026-08-25", profile={"max_drawdown_pct": 0.10})
    assert ok["suitability"]["status"] == "SUITABLE", ok["suitability"]
    tight = build_answer(card, as_of="2026-08-25", profile={"max_drawdown_pct": 0.02})
    assert tight["suitability"]["status"] == "UNSUITABLE", tight["suitability"]

    # 죽은 축은 답변에 이유가 남는다
    assert any("theme" in c["text"] and "소스" in c["text"]
               for s in ans["sections"] if s["title"] == "종합점수"
               for c in s["claims"])

    txt = render(ans)
    assert "[미검증]" in txt and "[관측됨]" in txt and "[측정됨]" in txt
    print(txt)
    print("\nanswer_builder self-check OK")


# ────────────────────────────────────────────────────────────────────────────
# 대화 경로 어댑터 - gather_holdings_evidence 출력에서 같은 답변을 만든다
#
# 배치(추천 카드)와 대화(종목 질의)가 **같은 조립기**를 써야 한다. 두 곳에서
# 따로 문장을 만들면 같은 종목에 다른 숫자·다른 등급이 나간다.
# ────────────────────────────────────────────────────────────────────────────

def _worker_summary(worker_result: Any) -> str:
    """워커 결과에서 서술 한 줄을 꺼낸다. 모양이 달라도 죽지 않는다."""
    if isinstance(worker_result, str):
        return worker_result.strip()
    if not isinstance(worker_result, dict):
        return ""
    for key in ("summary", "answer", "text"):
        v = worker_result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in worker_result.values():
        if isinstance(v, dict):
            got = _worker_summary(v)
            if got:
                return got
    return ""


def from_holdings_evidence(evidence: Mapping[str, Any], *, as_of: str,
                           company: str = "", worker_result: Any = None,
                           profile: dict | None = None) -> dict:
    """`gather_holdings_evidence` 출력 -> 등급 답변.

    워커(LLM)의 서술은 **JUDGED 등급 한 줄**로만 들어간다. 숫자는 전부
    evidence 에서 오고, 워커가 숫자를 말했더라도 그건 근거가 아니라 서술이다.
    """
    lv = dict(evidence.get("price_levels") or {})
    sources = evidence.get("sources") or {}
    card: dict[str, Any] = {
        "symbol": str(evidence.get("symbol", "")),
        "company": (company or str(evidence.get("company") or "")
                    or str(evidence.get("symbol", ""))),
        "last_close": lv.get("last_close"),
        "atr": lv.get("atr"),
        "axes": [],
        "composite": {"status": "INSUFFICIENT",
                      "reason": "대화 경로는 축 채점을 돌리지 않는다"
                                "(수급·밸류는 배치 경로에서 붙는다)"},
        "narrative": {
            "news": [{"polarity": "미판정", "title": n.get("title", ""),
                      "why": "", "evidence_id": n.get("evidence_id")}
                     for n in (evidence.get("news_headlines") or [])[:4]],
            "disclosures": [{"polarity": "미판정", "title": d.get("title", ""),
                             "why": "", "evidence_id": d.get("evidence_id")}
                            for d in (evidence.get("disclosures_7d") or [])[:3]],
        },
    }
    if lv.get("status") == "OK":
        card["plan"] = lv
    elif lv.get("supports") or lv.get("resistances"):
        # 계획은 성립 안 해도 지지·저항은 관측된 사실이라 버리지 않는다.
        card["plan"] = {"supports": lv.get("supports") or [],
                        "resistances": lv.get("resistances") or [],
                        "target": None}

    ans = build_answer(card, as_of=as_of, profile=profile)

    # 워커 서술을 JUDGED 로 얹는다
    text = _worker_summary(worker_result)
    if text:
        ans["sections"].insert(
            2, {"title": "애널리스트 서술",
                "claims": [{"text": text[:600], "tier": JUDGED,
                            "source": "holdings-analyst-worker"}]})

    # 계획이 기각됐으면 그 사유를 위험 절에 남긴다 - 조용히 빠지면 안 된다.
    if lv.get("status") and lv["status"] != "OK":
        for sec in ans["sections"]:
            if sec["title"] == "위험":
                sec["claims"].insert(0, {
                    "text": f"가격 계획을 내지 않았다 — {lv.get('reason') or lv['status']}",
                    "tier": DERIVED, "source": "market-api /levels"})
                break

    # tiers_present 는 build_answer 안에서 계산돼서, 위에서 끼워 넣은 절이
    # 빠져 있다. 답변에 실제로 무슨 등급이 들어 있는지를 보고하는 필드라
    # 어긋나면 그 자체가 거짓말이 된다 - 조립이 끝난 뒤 다시 센다.
    ans["tiers_present"] = sorted(
        {c["tier"] for sec in ans["sections"] for c in sec["claims"]},
        key=TIER_ORDER.index)
    ans["source_status"] = {k: (v or {}).get("status") for k, v in sources.items()}
    return ans
