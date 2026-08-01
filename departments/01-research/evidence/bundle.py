#!/usr/bin/env python3
"""Evidence Bundle 조립기 - 리서치본부 파이프라인의 결정론 수집·계산 계층.

담당: 재일 (리서치/퀀트)
근거: 본부 파이프라인(scripts.py) 실측 사고 - Packet 초안에서 로컬 LLM(qwen)이
      000660 의 +27% 급등을 "하락"으로 서술했다. 원인은 등락 방향 판단을 LLM 에
      맡긴 것. 원칙(집계·계산은 결정론 코드, LLM 은 서술만)대로 등락률·수익률·
      레인지 위치를 이 모듈이 코드로 계산해 확정 수치로 프롬프트에 넘긴다.

계약 (파이프라인이 깨지면 안 된다):
  assemble_bundle(symbol) -> dict
    scripts.py assemble_evidence 의 기존 키(daily_closes_recent, last_trade,
    news_headlines, disclosures_7d)를 그대로 유지하고
    price_context, as_of 를 추가한다.

price_context 규칙 (지어내지 않는다 - 레포 핵심 원칙):
  - market-api /bars (interval=1D, source=ls_chart) 종가 시계열로만 계산한다.
  - 봉이 부족하면 해당 필드는 None, note 에 "미확인"으로 남긴다.
  - market-api 미가동/오류면 {"status": "UNAVAILABLE", "reason": ...} -
    판단 불가를 통과로 위장하지 않는다.

실행: python bundle.py   # 자체 점검 (네트워크 없음 - 가짜 응답)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

BUNDLE_VERSION = "evidence-bundle-v1"
KST = timezone(timedelta(hours=9))

MARKET_API_DEFAULT = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
RESEARCH_API_DEFAULT = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")

# 20거래일 수익률에 비교 기준일까지 21봉이 필요하다
PRICE_BARS_NEEDED = 21


def _http_get(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _pct(cur: float, base: Optional[float]) -> Optional[float]:
    # 기준가 0/결측은 데이터 오류다 - 수치로 위장하지 않고 None 으로 남긴다
    if base is None or base == 0:
        return None
    return round((cur - base) / base * 100.0, 2)


# ── 결정론 가격 컨텍스트 ───────────────────────────────────────────────────
def compute_price_context(bars: list[dict]) -> dict:
    """일봉 dict 목록(bucket_time·close 필수, 정렬 무관) -> 가격 컨텍스트.

    순수 함수 - 네트워크·시계 없이 입력만으로 계산한다(자체 점검 대상).
    """
    rows = sorted((b for b in bars if b.get("close") is not None),
                  key=lambda b: str(b["bucket_time"]), reverse=True)
    if not rows:
        return {"status": "UNAVAILABLE", "reason": "일봉이 0개다 - 등락률 계산 불가"}

    closes = [float(b["close"]) for b in rows]  # 최신 -> 과거
    last = closes[0]
    prev = closes[1] if len(closes) >= 2 else None

    chg_1d = _pct(last, prev)
    ret_5d = _pct(last, closes[5]) if len(closes) >= 6 else None
    ret_20d = _pct(last, closes[20]) if len(closes) >= 21 else None

    # 20거래일 레인지는 창이 꽉 찼을 때만 - 부분 창을 전체처럼 말하지 않는다
    high_20 = max(closes[:20]) if len(closes) >= 20 else None
    low_20 = min(closes[:20]) if len(closes) >= 20 else None
    pos_20 = None
    if high_20 is not None and low_20 is not None and high_20 != low_20:
        pos_20 = round((last - low_20) / (high_20 - low_20) * 100.0, 1)

    if chg_1d is None:
        direction = "미확인"
    elif chg_1d > 0:
        direction = "상승"
    elif chg_1d < 0:
        direction = "하락"
    else:
        direction = "보합"

    missing = [k for k, v in (("change_1d_pct", chg_1d),
                              ("return_5d_pct", ret_5d),
                              ("return_20d_pct", ret_20d),
                              ("range_position_20d_pct", pos_20)) if v is None]
    ctx = {
        "status": "OK" if not missing else "PARTIAL",
        "source": "market-api /bars interval=1D source=ls_chart (코드 계산)",
        "bars_used": len(closes),
        "last_close": last,
        "last_close_date": str(rows[0]["bucket_time"])[:10],
        "prev_close": prev,
        "change_1d_pct": chg_1d,
        "direction_1d": direction,
        "return_5d_pct": ret_5d,
        "return_20d_pct": ret_20d,
        "high_20d": high_20,
        "low_20d": low_20,
        "range_position_20d_pct": pos_20,
    }
    if missing:
        ctx["note"] = "봉 부족으로 미확인: " + ", ".join(missing)
    return ctx


def fetch_price_context(symbol: str, *, market_api: Optional[str] = None,
                        get: Callable = _http_get) -> dict:
    base = (market_api or MARKET_API_DEFAULT).rstrip("/")
    try:
        bars = get(f"{base}/bars/{symbol}?interval=1D"
                   f"&limit={PRICE_BARS_NEEDED}&source=ls_chart")
    except Exception as e:  # noqa: BLE001
        # 미가동을 통과로 위장하지 않는다 - 사유를 명시하고 UNAVAILABLE 로 끝낸다
        return {"status": "UNAVAILABLE",
                "reason": f"market-api /bars 호출 실패: {type(e).__name__}: {e}"}
    return compute_price_context(bars)


# ── Bundle 조립 (scripts.py assemble_evidence 이관) ────────────────────────
def assemble_bundle(symbol: str, *, market_api: Optional[str] = None,
                    research_api: Optional[str] = None,
                    get: Callable = _http_get) -> dict:
    """기존 evidence 계약 유지 + price_context/as_of 확장.

    news/disclosures/snapshot 실패는 그대로 전파한다(파이프라인이 크게 실패해야
    한다) - price_context 만 UNAVAILABLE 로 자기 기술한다.
    """
    m = (market_api or MARKET_API_DEFAULT).rstrip("/")
    r = (research_api or RESEARCH_API_DEFAULT).rstrip("/")
    bars = get(f"{m}/bars/{symbol}?interval=1D&limit=5")
    snap = get(f"{m}/snapshot/{symbol}")
    news = get(f"{r}/evidence/news?symbol={symbol}&hours=24&limit=8")
    disc = get(f"{r}/evidence/disclosures?symbol={symbol}&days=7&limit=5")
    return {
        "daily_closes_recent": [(b["bucket_time"][:10], float(b["close"]))
                                for b in bars],
        "last_trade": snap.get("last_trade"),
        "news_headlines": [{"id": i + 1, "title": n["title"],
                            "relation": n["relation_type"]}
                           for i, n in enumerate(news)],
        "disclosures_7d": [d_["title"] for d_ in disc],
        "price_context": fetch_price_context(symbol, market_api=m, get=get),
        "as_of": datetime.now(KST).isoformat(timespec="seconds"),
    }


# ── 서술 수치 재대조 - 라벨 오서술·수치 창작 가드 (RES-00 Packet 검증용) ──

_PCT_RE = re.compile(r"([+-]?\d{1,3}(?:\.\d{1,2})?)\s*%")
# 셈 단위가 붙은 정수만 검사한다 - 가격("1,718,000원")·날짜("30일")까지 잡으면
# 오탐이 검사를 무력화한다. 실측 근거: Packet 이 "advancers 297개" 처럼 확정치
# (296)에 없는 종목 수를 창작했는데 % 가 아니라 통과했다 (2026-08-01).
_COUNT_RE = re.compile(r"(\d{1,7})\s*(?:개|건|종목)")


def _collect_numbers(v, out: set) -> None:
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        out.add(round(float(v), 2))
    elif isinstance(v, dict):
        for x in v.values():
            _collect_numbers(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _collect_numbers(x, out)


def verify_narrative_numbers(text: str, confirmed: dict,
                             *, tolerance: float = 0.1) -> dict:
    """서술 속 % 수치가 코드 계산 확정치 집합 안에 있는지 검사한다.

    +29.95% 실측 사례의 후속 가드: 수치·부호 인용은 정확했지만 서술이 필드
    의미를 바꿔 말하는 문제가 남았다 - 최소한 **확정치에 없는 % 수치를
    창작하는 것**은 여기서 결정론으로 잡는다.

    한계(정직하게): 한국어는 "2.33% 하락"처럼 부호를 단어로 옮기므로
    절대값 일치를 허용한다. 부호 뒤집힘 자체는 이 검사가 아니라 각
    분석가의 모순 강등(verify)이 맡는다.
    """
    allowed: set = set()
    _collect_numbers(confirmed, allowed)
    nums = [float(m.group(1)) for m in _PCT_RE.finditer(text or "")]
    unmatched = [n for n in nums
                 if not any(abs(abs(n) - abs(a)) <= tolerance for a in allowed)]
    # 셈 단위 정수는 정확 일치만 - 종목/기사/공시 개수는 반올림 여지가 없다
    counts = [int(m.group(1)) for m in _COUNT_RE.finditer(text or "")]
    unmatched_counts = [c for c in counts if float(c) not in allowed]
    return {"checked": len(nums), "unmatched": unmatched,
            "checked_counts": len(counts), "unmatched_counts": unmatched_counts,
            "ok": not unmatched and not unmatched_counts}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
def _fake_bars(closes_latest_first: list[float]) -> list[dict]:
    d0 = datetime(2026, 7, 30, 15, tzinfo=timezone.utc)
    return [{"bucket_time": (d0 - timedelta(days=i)).isoformat(),
             "close": c, "source": "ls_chart", "is_final": True}
            for i, c in enumerate(closes_latest_first)]


def _check_surge_plus27():
    # 실측 사고 재현 - +27% 급등이 부호 그대로 +27.0 / "상승"으로 나와야 한다
    ctx = compute_price_context(_fake_bars([1270.0, 1000.0] + [1000.0] * 19))
    assert ctx["status"] == "OK", ctx
    assert ctx["change_1d_pct"] == 27.0 and ctx["change_1d_pct"] > 0, ctx
    assert ctx["direction_1d"] == "상승", ctx
    assert ctx["return_5d_pct"] == 27.0 and ctx["return_20d_pct"] == 27.0, ctx
    assert ctx["high_20d"] == 1270.0 and ctx["low_20d"] == 1000.0, ctx
    assert ctx["range_position_20d_pct"] == 100.0, ctx
    # 정렬 무관 - 과거->최신 순으로 넣어도 같은 결과
    asc = compute_price_context(list(reversed(_fake_bars([1270.0] + [1000.0] * 20))))
    assert asc["change_1d_pct"] == 27.0 and asc["direction_1d"] == "상승", asc
    print("  급등 +27% 부호/방향      OK")


def _check_drop():
    ctx = compute_price_context(_fake_bars([730.0, 1000.0] + [1000.0] * 19))
    assert ctx["change_1d_pct"] == -27.0 and ctx["change_1d_pct"] < 0, ctx
    assert ctx["direction_1d"] == "하락", ctx
    flat = compute_price_context(_fake_bars([1000.0, 1000.0] + [1000.0] * 19))
    assert flat["change_1d_pct"] == 0.0 and flat["direction_1d"] == "보합", flat
    print("  하락 부호 / 보합         OK")


def _check_insufficient_bars():
    # 3봉 - 5/20일 수익률·레인지는 None 이고 "미확인"으로 남아야 한다
    ctx = compute_price_context(_fake_bars([1100.0, 1000.0, 900.0]))
    assert ctx["status"] == "PARTIAL", ctx
    assert ctx["change_1d_pct"] == 10.0, ctx
    assert ctx["return_5d_pct"] is None and ctx["return_20d_pct"] is None, ctx
    assert ctx["high_20d"] is None and ctx["range_position_20d_pct"] is None, ctx
    assert "미확인" in ctx["note"], ctx
    # 1봉 - 전일이 없으니 등락률도 미확인
    one = compute_price_context(_fake_bars([1100.0]))
    assert one["change_1d_pct"] is None and one["direction_1d"] == "미확인", one
    # 0봉 - 계산 자체가 불가
    assert compute_price_context([])["status"] == "UNAVAILABLE"
    # 기준가 0 - 등락률을 지어내지 않는다
    zero = compute_price_context(_fake_bars([1100.0, 0.0]))
    assert zero["change_1d_pct"] is None and zero["direction_1d"] == "미확인", zero
    print("  봉 부족/결측 None 처리   OK")


def _check_api_unavailable():
    def down(url: str):
        raise OSError("connection refused")
    ctx = fetch_price_context("000660", market_api="http://127.0.0.1:1", get=down)
    assert ctx["status"] == "UNAVAILABLE", ctx
    assert "OSError" in ctx["reason"] and "connection refused" in ctx["reason"], ctx
    print("  API 불가 UNAVAILABLE     OK")


def _check_bundle_contract():
    d0 = _fake_bars([1270.0, 1000.0] + [1000.0] * 19)

    def fake_get(url: str):
        if "/bars/" in url and "source=ls_chart" in url:
            return d0
        if "/bars/" in url:
            return d0[:5]
        if "/snapshot/" in url:
            return {"symbol": "000660", "last_trade": {"price": "1270.0"}}
        if "/evidence/news" in url:
            return [{"title": "급등 뉴스", "relation_type": "direct"}]
        if "/evidence/disclosures" in url:
            return [{"title": "공시 1"}]
        raise AssertionError(f"예상 밖 URL: {url}")

    b = assemble_bundle("000660", market_api="http://x", research_api="http://y",
                        get=fake_get)
    # 기존 소비자(draft_packet)가 쓰던 키가 전부 살아 있어야 한다
    for k in ("daily_closes_recent", "last_trade", "news_headlines",
              "disclosures_7d", "price_context", "as_of"):
        assert k in b, f"{k} 누락"
    assert len(b["daily_closes_recent"]) == 5
    assert b["news_headlines"][0] == {"id": 1, "title": "급등 뉴스",
                                      "relation": "direct"}
    assert b["price_context"]["change_1d_pct"] == 27.0
    assert b["price_context"]["direction_1d"] == "상승"
    print("  Bundle 계약 유지+확장    OK")


def _check_narrative_numbers():
    """서술 수치 재대조 - 창작 수치는 잡고, 확정치 인용·절대값 표기는 통과."""
    confirmed = {"price_context": {"change_1d_pct": 29.95, "return_5d_pct": -2.33,
                                   "range_position_20d_pct": 35.9}}
    ok = verify_narrative_numbers("전일 대비 +29.95% 급등, 5일 기준 2.33% 하락", confirmed)
    assert ok["ok"] and ok["checked"] == 2, ok
    # 확정치에 없는 수치 창작 -> 적발
    bad = verify_narrative_numbers("최근 +12.5% 상승 흐름", confirmed)
    assert not bad["ok"] and bad["unmatched"] == [12.5], bad
    # 허용 오차 - 반올림 표기(29.9%)는 통과, 크게 다르면(28%) 적발
    assert verify_narrative_numbers("29.9% 상승", confirmed, tolerance=0.1)["ok"]
    assert not verify_narrative_numbers("28% 상승", confirmed)["ok"]
    # 수치 없는 서술·빈 문자열은 검사 0건으로 통과
    assert verify_narrative_numbers("추세가 강하다", confirmed)["checked"] == 0
    assert verify_narrative_numbers("", confirmed)["ok"]
    # 셈 단위 정수 - "297개" 창작 실측 사례: 확정치(296) 밖이면 적발
    conf2 = {"regime": {"latest_advancers": 296, "latest_decliners": 51}}
    good = verify_narrative_numbers("상승 296개, 하락 51종목", conf2)
    assert good["ok"] and good["checked_counts"] == 2
    bad2 = verify_narrative_numbers("상승 297개, 하락 132종목", conf2)
    assert not bad2["ok"] and bad2["unmatched_counts"] == [297, 132]
    # 단위 없는 정수(가격·날짜)는 검사하지 않는다 - 오탐 방지
    assert verify_narrative_numbers("종가 1718000원, 7월 30일", conf2)["checked_counts"] == 0
    print("  서술 수치 재대조 가드    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{BUNDLE_VERSION} 자체 점검 (네트워크 없음)")
    _check_surge_plus27()
    _check_drop()
    _check_insufficient_bars()
    _check_api_unavailable()
    _check_bundle_contract()
    _check_narrative_numbers()
    print("Evidence Bundle 6개 영역 통과.")
