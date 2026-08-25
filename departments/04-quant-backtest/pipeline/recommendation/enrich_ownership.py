"""매집 후보에 수급·테마·밸류·가격계획을 붙인다. ls-mcp 안에서 돈다.

    docker exec hedgefund-ls-mcp python /tmp/enrich_ownership.py

입력  /tmp/ownership_scan.json   (scan_ownership.py 산출)
출력  /tmp/ownership_cards.json  (judge_candidates.py 가 그대로 먹는 모양)

**종합점수를 내지 않는다.** 이 추천의 근거는 관측이지 예측이 아니다 - 축을
가중합해 "72점" 같은 숫자를 만들면 예측력이 있는 것처럼 보인다. 순위는
`scan_ownership` 이 관측한 장내 순증 그대로다.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/app/departments/01-research/api")
sys.path.insert(0, "/app/departments/01-research/evidence")

import ls_mcp_server as ls

IN = os.environ.get("SCAN_OUT", "/tmp/ownership_scan.json")
OUT = os.environ.get("CARDS_OUT", "/tmp/ownership_cards.json")
MAX_N = int(os.environ.get("ENRICH_MAX", "6"))
MARKET_API = os.environ.get("MARKET_API_URL", "http://market-api:8036").rstrip("/")


def _levels(symbol: str) -> dict:
    """market-api 가 일봉에서 결정론으로 낸 지지·저항·목표·손절."""
    with urllib.request.urlopen(f"{MARKET_API}/levels/{symbol}", timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    t0 = time.time()
    scan = json.load(open(IN, encoding="utf-8"))

    # ▶ 기관 매수를 먼저 본다. 섞인 candidates 에서 상위 N 을 뽑으면 %p 가 큰
    #   지배주주 종목만 걸려서, 정작 "외부 기관이 돈 주고 샀다"는 신호가
    #   정밀조회에 한 번도 안 들어간다(실측 2026-08-25: 상위 5개 전부 계열).
    seen, cands = set(), []
    for bucket in ("by_institution", "by_controlling", "candidates"):
        for c in scan.get(bucket, []):
            if c["symbol"] in seen:
                continue
            seen.add(c["symbol"])
            c = dict(c, _bucket=bucket)
            cands.append(c)
            if len(cands) >= MAX_N:
                break
        if len(cands) >= MAX_N:
            break
    print(f"매집 후보 {len(scan.get('candidates', []))}개 중 {len(cands)}개 정밀조회 "
          f"(창 {scan['window']['begin']}~{scan['window']['end']})", flush=True)

    cards, calls = [], 0
    for c in cands:
        sym = c["symbol"]
        axes: list[dict] = []

        # ── 지분공시 매집 (관측) ─────────────────────────────────────────
        holders = ", ".join(c.get("buyers", [])[:3])
        types = c.get("by_buyer_type_pp") or {}
        type_txt = " ".join(f"{k}:{v:+.2f}%p" for k, v in types.items())
        own_summary = (f"지분공시 장내 순증 {c['net_market_buy_ratio_pp']:+.2f}%p"
                       f"{' (' + type_txt + ')' if type_txt else ''}, "
                       f"매수자 {c['buyer_count']}명({holders})")
        axes.append({
            "axis": "ownership", "status": "OK", "value": None, "reason": "",
            "summary": own_summary, "detail": c,
        })

        # ── 수급 (t1717) ────────────────────────────────────────────────
        try:
            flow = ls.investor_flow(sym, 25); calls += 1
            rows = flow.get("items", [])
            def streak(key, positive):
                """선두 미집계는 건너뛰고, 중간 구멍에서만 끊는다.

                t1717 은 장중에 그날 집계를 안 준다. 목록이 최신순이라 첫 행이
                늘 비어 있고, 거기서 끊으면 연속일수가 **항상 0** 이 된다
                (실측 2026-08-25에 그 상태였다).
                """
                n, started = 0, False
                for r in rows:
                    v = r.get(key)
                    if v is None:
                        if started:
                            break
                        continue
                    started = True
                    if (v > 0) if positive else (v < 0):
                        n += 1
                    else:
                        break
                return n
            fb, fs = streak("외인계", True), streak("외인계", False)
            ib, isell = streak("기관계", True), streak("기관계", False)
            axes.append({
                "axis": "flow", "status": "OK", "value": None, "reason": "",
                "summary": (f"외국인 매수 {fb}일/매도 {fs}일 연속, "
                            f"기관 매수 {ib}일/매도 {isell}일 연속"),
                "detail": {"foreign_buy_streak": fb, "foreign_sell_streak": fs,
                           "inst_buy_streak": ib, "inst_sell_streak": isell,
                           "citation": flow.get("citation")},
            })
        except Exception as exc:  # noqa: BLE001
            axes.append({"axis": "flow", "status": "ABSTAINED", "value": None,
                         "reason": f"{type(exc).__name__}: {str(exc)[:70]}", "summary": ""})

        # ── 테마 (t1532) ────────────────────────────────────────────────
        try:
            th = ls.stock_themes(sym); calls += 1
            names = [i["테마명"] for i in th.get("items", [])][:5]
            if names:
                axes.append({"axis": "theme", "status": "OK", "value": None, "reason": "",
                             "summary": f"편입 테마 {th['count']}개: {', '.join(names)}",
                             "detail": {"themes": th.get("items", [])[:8],
                                        "citation": th.get("citation")}})
            else:
                axes.append({"axis": "theme", "status": "ABSTAINED", "value": None,
                             "reason": "편입 테마 없음", "summary": ""})
        except Exception as exc:  # noqa: BLE001
            axes.append({"axis": "theme", "status": "ABSTAINED", "value": None,
                         "reason": f"{type(exc).__name__}: {str(exc)[:70]}", "summary": ""})

        # ── 밸류·업종 (t3320) ───────────────────────────────────────────
        fund = None
        try:
            fund = ls.stock_fundamental(sym); calls += 1
            cap = fund.get("시가총액_억원")
            # ▶ 규모를 같이 낸다. 1%p 는 시총 100억 회사와 20조 회사에서
            #   금액이 2,000배 차이다 - 숫자만 나란히 두면 오독한다.
            try:
                cap_won = float(str(cap).replace(",", "")) if cap else None
            except (TypeError, ValueError):
                cap_won = None
            est = (cap_won * c["net_market_buy_ratio_pp"] / 100.0
                   if cap_won else None)
            scale = (f" · 시총 {cap_won:,.0f}억, 매수 추정 {est:,.0f}억"
                     if est is not None else "")
            axes.append({"axis": "valuation", "status": "OK", "value": None, "reason": "",
                         "summary": (f"예상PER {fund.get('예상PER')} PBR {fund.get('PBR')} "
                                     f"ROE {fund.get('ROE')} 외국인비율 "
                                     f"{fund.get('외국인비율_pct')}%{scale}"),
                         "detail": {"citation": fund.get("citation"),
                                    "market_cap_100m_krw": cap_won,
                                    "est_buy_100m_krw": est}})
        except Exception as exc:  # noqa: BLE001
            axes.append({"axis": "valuation", "status": "ABSTAINED", "value": None,
                         "reason": f"{type(exc).__name__}: {str(exc)[:70]}", "summary": ""})

        # ── 가격 계획 (market-api /levels) ──────────────────────────────
        plan, last_close, atr = {}, None, None
        try:
            lv = _levels(sym)
            last_close, atr = lv.get("last_close"), lv.get("atr")
            plan = {k: lv.get(k) for k in
                    ("entry_low", "entry_high", "target", "stop", "reward_risk",
                     "risk_pct", "target_basis", "stop_basis", "supports",
                     "resistances")}
            plan["status"] = lv.get("status")
            plan["reason"] = lv.get("reason")
        except Exception as exc:  # noqa: BLE001
            plan = {"status": "UNAVAILABLE",
                    "reason": f"{type(exc).__name__}: {str(exc)[:70]}"}

        cards.append({
            "symbol": sym,
            "company": c.get("company", ""),
            "as_of": scan["window"]["end"],
            "last_close": last_close,
            "atr": atr,
            "업종": (fund or {}).get("업종"),
            "시장": (fund or {}).get("시장"),
            "plan": plan,
            "axes": axes,
            "ownership": c,
            "bucket": c.get("_bucket"),
            # 관측 기반 추천이라 예측 점수를 만들지 않는다.
            "composite": {"status": "INSUFFICIENT",
                          "reason": "관측 기반 추천 - 축을 가중합한 예측 점수를 내지 않는다"},
        })
        print(f"  {sym} {c.get('company','')[:12]} 축 {len(axes)}개", flush=True)
        time.sleep(0.2)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(cards, fh, ensure_ascii=False, indent=1)
    print(f"\n{len(cards)}건 -> {OUT}   LS 조회 {calls}회 (캡 {ls.LS_DAILY_CAP}) "
          f"{time.time()-t0:.0f}초")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
