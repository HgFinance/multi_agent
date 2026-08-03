#!/usr/bin/env python3
"""도구 러너 - ToolBox 의 계약을 실제 API 호출로 채운다.

담당: 재일 (리서치본부)
근거: RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 6.2절(역할별 Retrieval),
      RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 4.1절(Evidence before Narrative)
      재일님 지시 2026-08-03 "에이전트들이 쥐고 있는 도구들을 잘 활용했으면 좋겠음"

▶ 러너가 지키는 세 가지
  1. **페르소나를 밝힌다.** api_client.get_json(persona=...) - Tool Gateway 가
     익명 호출을 403 으로 막는다. 헤더 없이 부르면 강제 모드에서 죽는다.
  2. **numbers 를 낸다.** 가져온 수치가 number_guard 풀에 들어가야 분석가가
     그것을 서술에 쓸 수 있다. 이게 도구 계층의 존재 이유다.
  3. **evidence_refs 를 낸다.** 문서를 가져왔으면 document_id 를 그대로 단다.
     이게 있어야 주장이 inference 가 아니라 fact 가 된다.

▶ PIT 를 러너가 지킨다
  evidence/pit_manifest.py 가 Replay 금지로 선언한 Tool(market-api 5종)은
  run_mode=REPLAY 에서 호출 자체가 예외다. 러너가 그 판정을 우회하지 않는다 -
  우회하면 Manifest 가 장식이 된다.

▶ 실패는 ToolResult(ok=False, reason=...) 다
  예외를 밖으로 던지지 않는다(ToolBox.call 이 흡수한다). 다만 **사유 없는
  실패는 만들 수 없다** - ToolResult 가 그것을 강제한다.

실행: python agents/tool_runners.py     # 자체 점검 (네트워크 없음 - 가짜 응답)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "evidence"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from analyst_toolbox import ToolResult  # noqa: E402

RUNNERS_VERSION = "research-tool-runners-v1"

RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035").rstrip("/")
MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036").rstrip("/")


def _get(url: str, persona: str, get: Callable | None = None):
    if get is not None:
        return get(url)
    from api_client import get_json

    return get_json(url, persona=persona, timeout=30)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # NaN 제외


def _guard(service: str, path: str, run_mode: str) -> None:
    """PIT Manifest 판정을 러너가 우회하지 않는다."""
    from pit_manifest import ensure_callable

    ensure_callable(service, path, run_mode=run_mode)


# ---------------------------------------------------------------------------
# research-api 러너
# ---------------------------------------------------------------------------

def financial_history(symbol: str, limit: int = 12, *, run_mode: str = "LIVE",
                      get: Callable | None = None) -> ToolResult:
    """과거 보고기간의 재무 항목. 추세인지 일회성인지 가른다."""
    _guard("research-api", "/evidence/financials", run_mode)
    rows = _get(f"{RESEARCH_API}/evidence/financials?symbol={symbol}&limit={limit}",
                "fundamental-analyst", get) or []
    if not rows:
        return ToolResult(tool="financial_history", ok=False,
                          reason=f"{symbol} 재무 이력 0행 - 판단 재료가 없다")
    periods = sorted({str(r.get("period_end") or "")[:10] for r in rows if r.get("period_end")})
    nums: dict[str, float] = {"보고기간수": float(len(periods))}
    # 같은 계정의 시계열을 만든다 - 한 시점 비교로는 추세를 말할 수 없다
    by_account: dict[str, list] = {}
    for r in rows:
        acc = str(r.get("account_name") or r.get("account") or "").strip()
        v = _num(r.get("value") or r.get("amount"))
        if acc and v is not None:
            by_account.setdefault(acc, []).append((str(r.get("period_end"))[:10], v))
    for acc, series in list(by_account.items())[:6]:
        series.sort()
        if len(series) >= 2:
            first, last = series[0][1], series[-1][1]
            if first:
                nums[f"{acc}_기간변화_pct"] = round((last - first) / abs(first) * 100.0, 2)
    return ToolResult(tool="financial_history", ok=True,
                      data={"periods": periods, "accounts": sorted(by_account)[:12]},
                      numbers=nums)


def disclosure_detail(symbol: str, days: int = 30, *, run_mode: str = "LIVE",
                      get: Callable | None = None) -> ToolResult:
    """공시 목록과 중요도 분류. 재무 변화의 배경을 준다."""
    _guard("research-api", "/evidence/disclosures", run_mode)
    rows = _get(f"{RESEARCH_API}/evidence/disclosures?symbol={symbol}&days={days}&limit=30",
                "fundamental-analyst", get) or []
    if not rows:
        return ToolResult(tool="disclosure_detail", ok=False,
                          reason=f"{symbol} 최근 {days}일 공시 0건")
    try:
        from disclosure_events import classify
        levels = [classify(str(r.get("title") or "")) for r in rows]
        material = sum(1 for x in levels if getattr(x, "level", str(x)) == "MATERIAL")
    except Exception:      # noqa: BLE001 - 분류기가 없어도 목록은 준다
        material = 0
    return ToolResult(
        tool="disclosure_detail", ok=True,
        data={"titles": [str(r.get("title"))[:80] for r in rows[:10]]},
        numbers={"공시건수": float(len(rows)), "중요공시건수": float(material)},
        evidence_refs=tuple(str(r["document_id"]) for r in rows
                            if r.get("document_id"))[:20])


def story_cluster(symbol: str, hours: float = 48.0, *, run_mode: str = "LIVE",
                  get: Callable | None = None) -> ToolResult:
    """같은 사건을 다룬 기사 묶음. **단일 매체를 사실로 올리지 않기 위해** 쓴다."""
    _guard("research-api", "/evidence/stories", run_mode)
    raw = _get(f"{RESEARCH_API}/evidence/stories?symbol={symbol}&hours={hours}",
               "news-sentiment-analyst", get)
    # 실측 2026-08-03: 이 엔드포인트는 목록이 아니라 {stories:[...], total:n} 을
    # 낸다. 목록으로 가정하면 문자열 키를 .get 해서 죽는다 - 두 모양을 흡수한다.
    rows = raw.get("stories") if isinstance(raw, dict) else raw
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return ToolResult(tool="story_cluster", ok=False,
                          reason=f"{symbol} 최근 {hours}시간 스토리 0건")
    sizes = [_num(r.get("size")) or float(len(r.get("members") or []) or 1)
             for r in rows]
    multi = sum(1 for s in sizes if s and s >= 2)
    # 독립 출처 수 - **단일 매체를 사실로 올리지 않기 위한** 핵심 수치다
    src = max((len(r.get("sources") or []) for r in rows), default=0)
    return ToolResult(
        tool="story_cluster", ok=True,
        data={"clusters": len(rows),
              "titles": [str(r.get("representative_title") or r.get("title"))[:70]
                         for r in rows[:8]]},
        numbers={"스토리수": float(len(rows)),
                 "복수기사스토리수": float(multi),
                 "최대묶음크기": float(max(sizes) if sizes else 0),
                 "최대독립출처수": float(src)},
        evidence_refs=tuple(
            str(m) for r in rows
            for m in (r.get("member_document_ids") or r.get("members") or [])
            if isinstance(m, str))[:20])


def news_window(symbol: str, hours: float = 72.0, *, run_mode: str = "LIVE",
                get: Callable | None = None) -> ToolResult:
    """더 넓은 시간창의 기사. 창을 넓힌 사실을 서술에 남겨야 한다."""
    _guard("research-api", "/evidence/news", run_mode)
    rows = _get(f"{RESEARCH_API}/evidence/news?symbol={symbol}&hours={hours}&limit=50",
                "news-sentiment-analyst", get) or []
    if not rows:
        return ToolResult(tool="news_window", ok=False,
                          reason=f"{symbol} 최근 {hours}시간 기사 0건")
    dedicated = sum(1 for r in rows if str(r.get("relation_type")) == "DEDICATED")
    authorized = sum(1 for r in rows if r.get("production_authorized"))
    return ToolResult(
        tool="news_window", ok=True,
        data={"window_hours": hours,
              "titles": [str(r.get("title"))[:70] for r in rows[:8]]},
        numbers={"기사수": float(len(rows)), "전용기사수": float(dedicated),
                 "운영근거가능기사수": float(authorized)},
        evidence_refs=tuple(str(r["document_id"]) for r in rows
                            if r.get("document_id"))[:20])


def macro_series(code: str, days: int = 90, *, run_mode: str = "LIVE",
                 persona: str = "sector-regime-analyst",
                 get: Callable | None = None) -> ToolResult:
    """거시·스타일 지수 관측치(PIT). 두 번째 계열이 동의하는지 확인용."""
    _guard("research-api", "/macro/observations", run_mode)
    rows = _get(# ?code= 는 422 다 - API 는 codes= 를 받는다(실측 2026-08-03:
    # RES-09 의 유일한 도구가 매번 실패하고 있었다)
    f"{RESEARCH_API}/macro/observations?codes={code}&days={days}",
                persona, get) or []
    # ▶ 같은 period 에 **개정본이 여러 행** 온다(vintage_date desc, revision desc).
    #   period 별로 첫 행(최신 vintage)만 남기지 않으면 개정 횟수가 많은 달이
    #   평균을 끌고, latest 가 최신 period 의 **옛 개정치**가 될 수 있다.
    by_period: dict[str, float] = {}
    for r in rows:
        v = _num(r.get("value"))
        if v is None:
            continue
        key = str(r.get("period") or r.get("observed_at") or len(by_period))
        by_period.setdefault(key, v)          # 첫 행 = 최신 vintage
    vals = [by_period[k] for k in sorted(by_period)]
    if not vals:
        return ToolResult(tool="macro_series", ok=False,
                          reason=f"{code} 최근 {days}일 관측치 0건")
    latest = vals[-1]
    avg = sum(vals) / len(vals)
    return ToolResult(
        tool="macro_series", ok=True,
        data={"code": code, "n": len(vals),
              "latest_date": str((rows[-1] or {}).get("observed_at")
                                 or (rows[-1] or {}).get("period"))[:10]},
        numbers={f"{code}_최신": round(latest, 4),
                 f"{code}_평균{days}d": round(avg, 4),
                 f"{code}_평균대비_pct": round((latest - avg) / abs(avg) * 100.0, 2)
                 if avg else 0.0})


# ---------------------------------------------------------------------------
# market-api 러너 - **Replay 금지 대상이다**(pit_manifest)
# ---------------------------------------------------------------------------

def price_history(symbol: str, limit: int = 120, *, run_mode: str = "LIVE",
                  to: str = "", get: Callable | None = None) -> ToolResult:
    """더 긴 일봉. 장기 추세와 단기 신호가 어긋나는지 본다.

    ▶ /bars 는 Manifest 상 SUPPORTED 다(`to` 로 끝 시각을 자를 수 있다). 그런데
      **러너가 to 를 안 넘기면 Replay 에서도 최신 봉을 받는다** - 엔드포인트가
      지원한다는 것과 호출자가 쓴다는 것은 다르다. Manifest 는 전자만 보증한다.
      그래서 REPLAY 에서 to 없이 부르는 것을 여기서 막는다(자체점검이 잡았다).
    """
    _guard("market-api", "/bars/{symbol}", run_mode)
    if str(run_mode).upper() == "REPLAY" and not to:
        from pit_manifest import PitViolation

        raise PitViolation(
            "REPLAY 에서 price_history 를 to 없이 부를 수 없다 - /bars 가 시간 "
            "컷오프를 지원해도 넘기지 않으면 최신 봉이 와서 look-ahead 가 된다")
    q = f"&to={to}" if to else ""
    bars = _get(f"{MARKET_API}/bars/{symbol}?interval=1D&limit={limit}"
                f"&source=ls_chart{q}", "technical-analyst", get) or []
    # ▶ 순서를 가정하지 않는다 (2026-08-03, 리포트가 잡아낸 결함)
    #   /bars 는 **최신순**으로 온다. closes[-1] 을 최신으로 쓰면 120봉 조회에서
    #   가장 오래된 종가(491,500원)를 "최신종가"로 냈고, 같은 Packet 안에
    #   확정치 388,000원과 나란히 실려 총괄이 그 불일치를 지적했다.
    #   compute_price_context 는 이미 bucket_time 으로 정렬한다 - 러너만 안 했다.
    rows = sorted((b for b in bars if b.get("close") is not None),
                  key=lambda b: str(b.get("bucket_time", "")))   # 과거 -> 최신
    closes = [v for v in (_num(b.get("close")) for b in rows) if v is not None]
    if len(closes) < 2:
        return ToolResult(tool="price_history", ok=False,
                          reason=f"{symbol} 일봉 {len(closes)}개 - 추세 계산 불가")
    last = closes[-1]
    nums = {"봉수": float(len(closes)), "최신종가": last,
            "구간최고": max(closes), "구간최저": min(closes)}
    if closes[0]:
        nums["구간수익률_pct"] = round((last - closes[0]) / abs(closes[0]) * 100.0, 2)
    hi, lo = max(closes), min(closes)
    if hi != lo:
        nums["구간내위치_pct"] = round((last - lo) / (hi - lo) * 100.0, 1)
    return ToolResult(tool="price_history", ok=True,
                      data={"bars_used": len(closes)}, numbers=nums)


def breadth_history(days: int = 60, *, run_mode: str = "LIVE",
                    get: Callable | None = None) -> ToolResult:
    """시장 폭 과거치. **비교 기준 없는 단일 값은 판정이 아니다.**"""
    _guard("market-api", "/breadth", run_mode)
    rows = _get(f"{MARKET_API}/breadth?days={days}", "sector-regime-analyst", get)
    rows = rows if isinstance(rows, list) else [rows] if rows else []
    # ▶ **정렬을 가정하지 않는다.** /breadth 는 `order by event_time desc` 라
    #   최신이 rows[0] 이다. 예전엔 ratios[-1] 을 최신으로 읽어 60일 조회에서
    #   **가장 오래된 폭 비율**을 "현재" 로 냈다 - /bars 와 같은 사고다.
    keyed = sorted(
        ((str((r or {}).get("event_time") or ""), _num((r or {}).get("ad_ratio")))
         for r in rows),
        key=lambda kv: kv[0])
    ratios = [v for _, v in keyed if v is not None]
    if not ratios:
        return ToolResult(tool="breadth_history", ok=False,
                          reason=f"최근 {days}일 breadth 관측 0건")
    latest, avg = ratios[-1], sum(ratios) / len(ratios)
    return ToolResult(tool="breadth_history", ok=True,
                      data={"n": len(ratios)},
                      numbers={"ad_ratio_최신": round(latest, 4),
                               f"ad_ratio_평균{days}d": round(avg, 4),
                               "ad_ratio_평균대비_배": round(latest / avg, 3) if avg else 0.0})


def micro_history(symbol: str, date: str = "", *, run_mode: str = "LIVE",
                  get: Callable | None = None) -> ToolResult:
    """과거 세션 미시구조. 오늘이 얼마나 이례적인지 말하기 위해 쓴다."""
    _guard("market-api", "/microstructure/{symbol}", run_mode)
    # ?date= 는 무시된다 - API 는 trade_date= 를 받는다.
    # 그래서 "과거 세션 비교" 가 매번 오늘 자신과의 비교였다.
    q = f"?trade_date={date}" if date else ""
    r = _get(f"{MARKET_API}/microstructure/{symbol}{q}", "microstructure-analyst", get)
    if not r:
        return ToolResult(tool="micro_history", ok=False,
                          reason=f"{symbol} {date or '최근'} 미시구조 없음")
    nums = {k: v for k, v in ((k, _num(v)) for k, v in (r or {}).items())
            if v is not None}
    if not nums:
        return ToolResult(tool="micro_history", ok=False,
                          reason=f"{symbol} 미시구조 응답에 수치가 없다")
    return ToolResult(tool="micro_history", ok=True,
                      data={"date": date or "최근"}, numbers=nums)


ALL_RUNNERS: dict[str, Callable[..., ToolResult]] = {
    "financial_history": financial_history,
    "disclosure_detail": disclosure_detail,
    "story_cluster": story_cluster,
    "news_window": news_window,
    "macro_series": macro_series,
    "price_history": price_history,
    "breadth_history": breadth_history,
    "micro_history": micro_history,
}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음 (가짜 응답 주입)
# ---------------------------------------------------------------------------

def _check_every_declared_tool_has_a_runner():
    """PERSONA_TOOLS 가 선언한 label 이 전부 구현돼 있는가.

    없으면 ToolBox 가 조용히 안 담는다 - 선언만 늘고 도구는 없는 상태가 된다.
    """
    from analyst_toolbox import PERSONA_TOOLS

    declared = {label for tools in PERSONA_TOOLS.values() for _, label, *_ in tools}
    missing = sorted(declared - set(ALL_RUNNERS))
    assert not missing, f"선언은 됐는데 러너가 없다: {missing}"
    print(f"  선언 {len(declared)}개 전부 구현  OK")


def _check_numbers_and_refs():
    rows = [{"document_id": "d1", "title": "단일판매공급계약", "relation_type": "DEDICATED",
             "production_authorized": True},
            {"document_id": "d2", "title": "주주총회소집결의", "relation_type": "MENTIONS",
             "production_authorized": False}]
    r = disclosure_detail("005380", get=lambda u: rows)
    assert r.ok and r.numbers["공시건수"] == 2.0
    assert r.evidence_refs == ("d1", "d2"), r.evidence_refs

    n = news_window("005380", get=lambda u: rows)
    assert n.numbers["전용기사수"] == 1.0 and n.numbers["운영근거가능기사수"] == 1.0
    # 실측 모양: /evidence/stories 는 목록이 아니라 {stories:[...]} 를 낸다
    wrapped = {"total": 2, "stories": [
        {"size": 21, "sources": ["naver_apihub", "ls_news"],
         "representative_title": "현대차·기아 7월 판매", "members": ["m1", "m2"]},
        {"size": 1, "sources": ["naver_apihub"], "representative_title": "단독 보도"}]}
    sc = story_cluster("005380", get=lambda u: wrapped)
    assert sc.ok, sc.reason
    assert sc.numbers["스토리수"] == 2.0 and sc.numbers["복수기사스토리수"] == 1.0
    assert sc.numbers["최대독립출처수"] == 2.0, sc.numbers
    assert sc.evidence_refs == ("m1", "m2"), sc.evidence_refs
    # 목록으로 와도 죽지 않는다
    assert story_cluster("005380", get=lambda u: []).ok is False
    print("  수치·근거 동시 산출      OK")


def _check_empty_is_failure_not_zero():
    """행 0 을 '정상 0' 으로 위장하지 않는다 (fail-closed)."""
    for fn, args in ((disclosure_detail, ("005380",)), (story_cluster, ("005380",)),
                     (news_window, ("005380",)), (financial_history, ("005380",))):
        r = fn(*args, get=lambda u: [])
        assert r.ok is False and r.reason, f"{fn.__name__} 이 0행을 통과시켰다"
    r = price_history("005380", get=lambda u: [{"close": 1000.0}])
    assert r.ok is False and "추세 계산 불가" in r.reason
    print("  0행 = 실패(위장 없음)    OK")


def _check_derived_numbers_are_comparisons():
    """단일 값이 아니라 **비교**를 낸다 - 그게 판단을 바꾼다."""
    # ▶ /bars 는 **최신순**으로 온다. 순서를 가정하면 가장 오래된 종가를
    #   '최신종가' 로 낸다(2026-08-03 실측: 491,500 vs 확정치 388,000).
    newest_first = [{"bucket_time": "2026-07-30", "close": 105.0},
                    {"bucket_time": "2026-07-29", "close": 90.0},
                    {"bucket_time": "2026-07-28", "close": 110.0},
                    {"bucket_time": "2026-07-27", "close": 100.0}]
    r = price_history("005380", get=lambda u: newest_first)
    assert r.numbers["최신종가"] == 105.0, r.numbers
    assert r.numbers["구간수익률_pct"] == 5.0, r.numbers
    assert r.numbers["구간내위치_pct"] == 75.0, r.numbers
    # 오래된순으로 와도 같은 답이어야 한다
    r2 = price_history("005380", get=lambda u: list(reversed(newest_first)))
    assert r2.numbers == r.numbers, (r.numbers, r2.numbers)

    br = breadth_history(get=lambda u: [{"ad_ratio": 1.0}, {"ad_ratio": 2.0},
                                        {"ad_ratio": 4.0}])
    assert br.numbers["ad_ratio_최신"] == 4.0
    assert br.numbers["ad_ratio_평균대비_배"] == round(4.0 / (7 / 3), 3), br.numbers
    print("  비교 수치 산출           OK")


def _check_replay_is_blocked():
    """PIT Manifest 를 러너가 우회하지 않는가."""
    from pit_manifest import PitViolation

    # Manifest 가 UNSUPPORTED 로 선언한 것은 Replay 에서 호출 자체가 막힌다
    for fn, args in ((breadth_history, ()), (micro_history, ("005380",))):
        try:
            fn(*args, run_mode="REPLAY", get=lambda u: [])
            raise AssertionError(f"{fn.__name__} 이 REPLAY 에서 통과했다")
        except PitViolation as e:
            assert "look-ahead" in str(e)

    # ▶ /bars 는 SUPPORTED 라 Manifest 는 통과시킨다. 그러나 **러너가 to 를 안
    #   넘기면 최신 봉이 온다** - 지원한다는 것과 쓴다는 것은 다르다.
    try:
        price_history("005380", run_mode="REPLAY", get=lambda u: [])
        raise AssertionError("to 없이 REPLAY 에서 price_history 가 통과했다")
    except PitViolation as e:
        assert "to 없이" in str(e), str(e)
    # to 를 넘기면 통과한다
    r = price_history("005380", run_mode="REPLAY", to="2026-06-01",
                      get=lambda u: [{"close": 1.0}, {"close": 2.0}])
    assert r.ok is True, r
    # research-api 는 as_of 를 받으므로 Replay 에서도 허용된다
    r = news_window("005380", run_mode="REPLAY", get=lambda u: [])
    assert r.ok is False and "0건" in r.reason      # PitViolation 이 아니라 빈 결과
    print("  Replay 금지 준수         OK")


def _check_macro_needs_values():
    r = macro_series("GPR", get=lambda u: [{"value": None}, {"value": "x"}])
    assert r.ok is False, "값이 없는데 통과했다"
    ok = macro_series("GPR", get=lambda u: [{"value": 100.0, "observed_at": "2026-07-01"},
                                            {"value": 150.0, "observed_at": "2026-08-01"}])
    assert ok.numbers["GPR_최신"] == 150.0
    assert ok.numbers["GPR_평균대비_pct"] == 20.0, ok.numbers
    print("  거시 계열 비교           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{RUNNERS_VERSION} 자체 점검 (네트워크 없음)")
    _check_every_declared_tool_has_a_runner()
    _check_numbers_and_refs()
    _check_empty_is_failure_not_zero()
    _check_derived_numbers_are_comparisons()
    _check_replay_is_blocked()
    _check_macro_needs_values()
    print("도구 러너 6개 영역 통과.")
