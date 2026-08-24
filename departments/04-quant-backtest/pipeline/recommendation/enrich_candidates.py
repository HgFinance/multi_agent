"""1.5층 - 1층이 좁힌 후보에만 수급·공매도·밸류 축을 붙이고 카드를 렌더한다.

LS 조회는 종목당 3회(t1717 수급 / 공매도 / t3320 밸류)이고 초당 1건이라
후보 N개면 3N 초 · 3N 회다. 전 종목(2,694)에는 8,082회가 필요해 하루 캡
2,000 을 넘는다 - 이 파일이 1층 뒤에만 오는 이유다.

ls_mcp_server 안에서 돌아야 한다(LS 자격이 거기에만 있다).
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, "/app/departments/01-research/api")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ls_mcp_server as ls

from instrument_scoring import (
    COMPOSITE_OK,
    STATUS_OK,
    AxisScore,
    abstain,
    blend_axes,
    flow_axis,
    short_axis,
)

IN_PATH = os.environ.get("SCREEN_OUT", "/tmp/candidates.json")
MAX_CANDIDATES = int(os.environ.get("ENRICH_MAX", "6"))


def valuation_axis(fundamental: dict | None, reason: str = "") -> AxisScore:
    """예상PER 과 PBR 로 매우 거친 밸류 점수. 업종 상대비교는 아직 없다.

    t3320 은 종목 하나씩만 준다 - 업종 peer 전체를 부르면 후보 6개에
    수백 회가 나간다. 그래서 지금은 절대 수준만 보고, detail 에 업종명을
    남겨 2층 LLM 이 상대비교를 서술로 보완하게 한다. 축 이름을 valuation 으로
    두되 이 한계를 reason 에 적어 둔다.
    """
    if not fundamental:
        return abstain("valuation", reason or "t3320 응답 없음")
    try:
        fwd_per = float(fundamental.get("예상PER") or 0)
        pbr = float(fundamental.get("PBR") or 0)
        roe = float(fundamental.get("ROE") or 0)
    except (TypeError, ValueError):
        return abstain("valuation", "밸류 필드 파싱 실패")
    if fwd_per <= 0 or pbr <= 0:
        return abstain("valuation", "예상PER 또는 PBR 이 유효하지 않다")

    # PER 10 을 중립으로 두고 로그 거리로 접는다. ROE 는 가점.
    import math
    per_score = -math.tanh(math.log(fwd_per / 10.0) * 0.8)
    pbr_score = -math.tanh(math.log(max(pbr, 0.05) / 1.5) * 0.6)
    roe_score = math.tanh((roe - 8.0) / 12.0)
    value = max(-1.0, min(1.0, 0.45 * per_score + 0.25 * pbr_score + 0.30 * roe_score))
    return AxisScore(
        axis="valuation", status=STATUS_OK, value=value,
        detail={"예상PER": fwd_per, "PBR": pbr, "ROE": roe,
                "업종": fundamental.get("업종"), "시장": fundamental.get("시장"),
                "외국인비율_pct": fundamental.get("외국인비율_pct"),
                "note": "업종 상대비교 미적용(t3320 종목당 1회 제약)"},
        evidence_refs=(f"ls:t3320:{fundamental.get('citation','')}",),
    )


def render(card: dict) -> str:
    c = card
    p = c["plan"]
    comp = c["composite"]
    lines = []
    lines.append("=" * 78)
    head = f"{c['symbol']}  ·  {c.get('업종') or '업종 미상'}  ·  {c.get('시장') or ''}"
    lines.append(head)
    if comp["status"] == COMPOSITE_OK:
        stars = "★" * max(1, min(3, round((comp["display"] - 50) / 12))) if comp["display"] > 50 else "☆"
        lines.append(f"종합 {comp['display']}/100   추천강도 {stars}   "
                     f"현재가 {c['last_close']:,.0f}   유효축 {comp['effective_weight']:.0%}")
    else:
        lines.append(f"종합 산출 안 함 - {comp['reason']}")
    lines.append("")
    lines.append("가격 계획")
    lines.append(f"  진입   {p['entry_low']:,.0f} ~ {p['entry_high']:,.0f}")
    if p["supports"]:
        lines.append("  지지   " + " / ".join(
            f"S{i+1} {s['price']:,.0f}(터치 {s['touches']})" for i, s in enumerate(p["supports"])))
    if p["resistances"]:
        lines.append("  저항   " + " / ".join(
            f"R{i+1} {r['price']:,.0f}(터치 {r['touches']})" for i, r in enumerate(p["resistances"])))
    up = p["target"] / c["last_close"] - 1
    dn = p["stop"] / c["last_close"] - 1
    lines.append(f"  목표   {p['target']:,.0f}  ({up:+.1%})   ← {p['target_basis']}")
    lines.append(f"  손절   {p['stop']:,.0f}  ({dn:+.1%})   ← {p['stop_basis']}")
    lines.append(f"  손익비 {p['reward_risk']}   ATR {c['atr']:,.0f}")
    lines.append("")
    lines.append("점수 분해")
    for ax in c["axes"]:
        contrib = comp.get("contributions", {}).get(ax["axis"])
        if ax["status"] == STATUS_OK:
            lines.append(f"  {ax['axis']:<12} {ax['value']:+.3f}  기여 {contrib:+.3f}  {ax['summary']}")
        else:
            lines.append(f"  {ax['axis']:<12}    —      —        {ax['status']} · {ax['reason']}")
    if comp.get("unreported"):
        lines.append(f"  (미보고 축: {', '.join(comp['unreported'])} - 분모에는 포함)")
    return "\n".join(lines)


def _summary(axis_name: str, ax: AxisScore) -> str:
    d = ax.detail
    if axis_name == "momentum":
        return (f"20일 {d['ret_20']:+.1%}(상위 {1-d['ret_20_pct']:.0%}) "
                f"60일 {d['ret_60']:+.1%} 거래대금 {d['turnover_ratio']:.2f}배")
    if axis_name == "flow":
        return (f"외인 매수 {d['foreign_buy_streak']}일/매도 {d['foreign_sell_streak']}일, "
                f"기관 매수 {d['inst_buy_streak']}일/매도 {d['inst_sell_streak']}일")
    if axis_name == "short":
        return (f"공매도 비중 {d['latest_pct']}% (기준 {d['baseline_median_pct']}%, "
                f"{d['surge_ratio']}배)")
    if axis_name == "valuation":
        return f"예상PER {d['예상PER']} PBR {d['PBR']} ROE {d['ROE']}"
    return ""


def main() -> int:
    payload = json.load(open(IN_PATH, encoding="utf-8"))
    candidates = payload["candidates"][:MAX_CANDIDATES]
    print(f"as_of {payload['as_of']}  유니버스 {payload['universe_total']} -> "
          f"채점 {payload['universe_scored']} -> 후보 {len(payload['candidates'])} "
          f"-> 정밀조회 {len(candidates)}")
    print(f"1층 탈락: {payload['screen_rejected']}")
    print(f"가격계획 기각으로 건너뛴 상위 종목: {len(payload['skipped_before_quota'])}\n")

    calls = 0
    cards = []
    for c in candidates:
        symbol = c["symbol"]
        # 방어 - 문자가 섞인 코드는 LS 조회에서 DART 색인으로 새어 멈춘다.
        if not (len(symbol) == 6 and symbol.isdigit()):
            print(f"skip {symbol}: 보통주 코드가 아니다(LS 조회 불가)")
            continue
        axes: list[AxisScore] = []

        mom = AxisScore("momentum", STATUS_OK, c["momentum"], c["momentum_detail"],
                        ("market.market_bars:1D",))
        axes.append(mom)

        adv = None
        try:
            flow_raw = ls.investor_flow(symbol, 25); calls += 1
            rows = flow_raw.get("items", [])
            vols = [float(r.get("volume") or 0) for r in rows if r.get("volume")]
            adv = sum(vols) / len(vols) if vols else None
            axes.append(flow_axis(rows, avg_daily_volume=adv))
        except Exception as exc:
            axes.append(abstain("flow", f"{type(exc).__name__}: {str(exc)[:80]}"))

        try:
            short_raw = ls.short_selling(symbol, 25); calls += 1
            axes.append(short_axis(short_raw.get("items", [])))
        except Exception as exc:
            axes.append(abstain("short", f"{type(exc).__name__}: {str(exc)[:80]}"))

        fundamental = None
        try:
            fundamental = ls.stock_fundamental(symbol); calls += 1
            axes.append(valuation_axis(fundamental))
        except Exception as exc:
            axes.append(abstain("valuation", f"{type(exc).__name__}: {str(exc)[:80]}"))

        # 2층(LLM) 축 - 아직 안 붙였다. 기권으로 정직하게 남긴다.
        axes.append(abstain("news", "2층 미구현 - 뉴스 판정 아직 없음"))
        axes.append(abstain("disclosure", "2층 미구현 - 공시 판정 아직 없음"))
        axes.append(abstain("theme", "수집 소스 없음", no_source=True))

        comp = blend_axes(axes)
        cards.append({
            "symbol": symbol,
            "company": c.get("company", ""),
            "last_close": c["last_close"],
            "atr": c["atr"],
            "plan": c["plan"],
            "업종": (fundamental or {}).get("업종"),
            "시장": (fundamental or {}).get("시장"),
            "axes": [
                {"axis": a.axis, "status": a.status, "value": a.value,
                 "reason": a.reason, "summary": _summary(a.axis, a) if a.status == STATUS_OK else ""}
                for a in axes
            ],
            "composite": {
                "status": comp.status, "value": comp.value, "display": comp.display,
                "effective_weight": comp.effective_weight,
                "contributions": comp.contributions,
                "unreported": list(comp.unreported), "reason": comp.reason,
            },
        })
        time.sleep(0.2)

    ranked = sorted(
        cards,
        key=lambda c: (c["composite"]["value"] is None, -(c["composite"]["value"] or 0)),
    )
    for card in ranked:
        print(render(card))
    print("=" * 78)
    print(f"LS 조회 {calls}회 사용 (일 캡 {ls.LS_DAILY_CAP})")
    with open("/tmp/cards.json", "w", encoding="utf-8") as fh:
        json.dump(ranked, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
