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


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{BUNDLE_VERSION} 자체 점검 (네트워크 없음)")
    _check_surge_plus27()
    _check_drop()
    _check_insufficient_bars()
    _check_api_unavailable()
    _check_bundle_contract()
    print("Evidence Bundle 5개 영역 통과.")
