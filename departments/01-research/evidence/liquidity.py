#!/usr/bin/env python3
"""유동성 지표 - Amihud 비유동성과 Roll 유효 스프레드.

소유: 재일 (리서치본부)
근거: evidence/methods.py 의 `amihud_illiquidity`(Amihud 2002),
      `roll_effective_spread`(Roll 1984). 두 방법이 **ADOPTED 로 등재돼
      있었는데 이 파일이 없었다**(2026-08-02 계획 대조에서 적발).
      레지스트리가 실제 역량보다 부풀어 있었던 것이고, 그건 레지스트리가
      막으려던 바로 그 상태다. 강등 대신 구현으로 맞춘다 - 필요한 입력
      (일별 종가·거래대금)이 이미 market-api /bars 에 있다.

▶ 왜 RES-03(미시구조)인가
  둘 다 "같은 거래대금이 가격을 얼마나 미는가"와 "왕복 거래에 얼마가 드는가"
  를 본다. 체결·호가 단면(스프레드 bp, 심도 불균형)이 **오늘 하루**를 본다면
  이 둘은 **최근 20일의 구조적 비용**을 본다 - 축이 다르다.

▶ 지키는 것
  - 모형이 성립하지 않으면 None 이다. Roll 은 자기공분산이 0 이상이면 실수
    스프레드가 안 나온다 - 그때 0 이나 임의값으로 채우지 않는다(Roll 1984 의
    한계 그대로).
  - 거래대금 0(거래정지·휴장)인 날은 Amihud 분모가 될 수 없다 - 그 날을
    빼고 남은 날로 계산하되 **몇 날을 썼는지 함께 낸다.**
  - 결측이 섞인 창은 부분 평균으로 위장하지 않는다.

실행: python evidence/liquidity.py     # 자체 점검(네트워크·DB 없음)
"""
from __future__ import annotations

import math
import sys

MODULE_VERSION = "research-liquidity-v1"

WINDOW = 20            # 기본 관측 창(거래일) - 미시구조 지표의 관례
MIN_DAYS_AMIHUD = 10   # 이보다 적으면 평균이라 부르지 않는다
MIN_DAYS_ROLL = 12     # 자기공분산에 표본이 더 필요하다(차분 2회 소모)
AMIHUD_SCALE = 1e6     # 원 단위 거래대금이라 값이 매우 작다 - 백만 배로 읽는다


def amihud_illiquidity(closes, notionals, *, window: int = WINDOW,
                       min_days: int = MIN_DAYS_AMIHUD) -> dict:
    """Amihud(2002) ILLIQ = mean(|일수익률| / 거래대금).

    closes·notionals 는 **오래된 것부터** 정렬된 같은 길이의 시퀀스다.
    값이 클수록 같은 거래대금이 가격을 더 민다 = 충격비용이 크다.

    거래대금 0 인 날은 분모가 될 수 없어 제외한다(정지·거래 없음). 그 결과
    쓸 수 있는 날이 min_days 미만이면 None - '유동성이 나쁘다'와 '측정 못
    했다'는 다른 말이다.
    """
    out = {"illiq": None, "days_used": 0, "days_skipped": 0,
           "scale": f"x{AMIHUD_SCALE:.0e} (원 단위 거래대금)",
           "window_requested": window}
    if not closes or not notionals or len(closes) != len(notionals):
        return out

    c = list(closes)[-(window + 1):]
    n = list(notionals)[-(window + 1):]
    ratios, skipped = [], 0
    for i in range(1, len(c)):
        prev, cur, dv = c[i - 1], c[i], n[i]
        if prev in (None, 0) or cur is None or dv is None or float(dv) <= 0:
            skipped += 1
            continue
        ret = abs(float(cur) / float(prev) - 1.0)
        ratios.append(ret / float(dv))

    out["days_skipped"] = skipped
    out["days_used"] = len(ratios)
    if len(ratios) < min_days:
        return out
    out["illiq"] = round(sum(ratios) / len(ratios) * AMIHUD_SCALE, 6)
    return out


def roll_spread(closes, *, window: int = WINDOW,
                min_days: int = MIN_DAYS_ROLL) -> dict:
    """Roll(1984) 유효 스프레드 = 2*sqrt(-Cov(Δp_t, Δp_{t-1})).

    가격 **변화의 자기공분산이 음수**일 때만 정의된다 - 매수/매도 호가를
    오가는 튐(bid-ask bounce)이 음의 자기상관을 만든다는 것이 모형의 전제다.
    자기공분산이 0 이상이면 그 전제가 깨진 것이므로 값을 내지 않고
    model_holds=False 로 사실을 남긴다. 0 으로 채우면 '스프레드가 없다'는
    정반대 뜻이 된다.

    반환의 spread 는 **가격 단위**이고, spread_bp 는 평균가 대비 bp 다.
    """
    out = {"spread": None, "spread_bp": None, "autocov": None,
           "model_holds": None, "days_used": 0, "window_requested": window}
    if not closes:
        return out

    c = [float(x) for x in list(closes)[-(window + 1):] if x is not None]
    if len(c) < min_days:
        out["days_used"] = len(c)
        return out

    d = [c[i] - c[i - 1] for i in range(1, len(c))]       # 가격 변화
    if len(d) < 2:
        out["days_used"] = len(c)
        return out

    m = sum(d) / len(d)
    pairs = [(d[i] - m) * (d[i - 1] - m) for i in range(1, len(d))]
    cov = sum(pairs) / len(pairs)

    out["days_used"] = len(c)
    out["autocov"] = round(cov, 6)
    out["model_holds"] = cov < 0
    if cov >= 0:
        return out

    spread = 2.0 * math.sqrt(-cov)
    avg_price = sum(c) / len(c)
    out["spread"] = round(spread, 4)
    if avg_price > 0:
        out["spread_bp"] = round(spread / avg_price * 10000.0, 2)
    return out


def compute_liquidity(bars, *, window: int = WINDOW) -> dict:
    """market-api /bars 행 목록 -> 두 지표. 정렬·중복은 여기서 정리한다.

    bars 는 최신순(desc)으로 오므로 오름차순으로 뒤집고, 같은 bucket_time
    중복은 나중 것(재수집 우선)을 쓴다 - technical_analyst 와 같은 관례.
    거래대금(notional)이 없으면 Amihud 는 미확인이고 Roll 만 나온다.
    """
    dedup: dict[str, dict] = {}
    for b in sorted((b for b in (bars or []) if b.get("close") is not None),
                    key=lambda b: str(b.get("bucket_time"))):
        dedup[str(b["bucket_time"])] = b
    rows = [dedup[k] for k in sorted(dedup)]

    closes = [float(b["close"]) for b in rows]
    notionals = [b.get("notional") for b in rows]
    has_notional = any(v is not None for v in notionals)

    return {
        "bars_used": len(rows),
        "last_bar_date": str(rows[-1]["bucket_time"])[:10] if rows else None,
        "amihud": (amihud_illiquidity(closes, notionals, window=window)
                   if has_notional else None),
        "roll": roll_spread(closes, window=window),
        "method_keys": ("amihud_illiquidity", "roll_effective_spread"),
    }


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_amihud_math():
    # 수작업 대조: 종가 100->101->100, 거래대금 1e6 씩
    #   |1.01-1|/1e6 = 1e-8, |100/101-1|/1e6 = 9.90099e-9
    #   평균 = 9.950495e-9, x1e6 = 0.00995
    c = [100.0, 101.0, 100.0]
    n = [1e6, 1e6, 1e6]
    r = amihud_illiquidity(c, n, min_days=2)
    assert r["days_used"] == 2, r
    assert abs(r["illiq"] - 0.00995) < 1e-5, r

    # 거래대금 0 인 날은 분모가 될 수 없다 - 빼되 뺐다는 사실을 남긴다
    r2 = amihud_illiquidity([100.0, 101.0, 100.0], [1e6, 0, 1e6], min_days=1)
    assert r2["days_used"] == 1 and r2["days_skipped"] == 1, r2

    # 표본이 모자라면 None - '유동성 나쁨'과 '측정 못 함'은 다르다
    assert amihud_illiquidity(c, n, min_days=5)["illiq"] is None
    assert amihud_illiquidity([], [])["illiq"] is None
    assert amihud_illiquidity([1.0], [1.0, 2.0])["illiq"] is None, "길이 불일치"
    print("  Amihud 수작업 대조       OK")


def _check_roll_model_limit():
    """자기공분산이 0 이상이면 값을 내지 않는다 - 모형의 한계를 지킨다."""
    # 단조 상승은 가격변화가 일정해 공분산이 0 근처/양수 -> 성립 안 함
    up = [100.0 + i for i in range(20)]
    r = roll_spread(up)
    assert r["model_holds"] is False and r["spread"] is None, r
    assert r["autocov"] is not None, "공분산 자체는 보고해야 판단할 수 있다"

    # 톱니(bid-ask bounce)는 음의 자기상관 -> 성립
    saw = [100.0 + (1.0 if i % 2 else 0.0) for i in range(20)]
    r2 = roll_spread(saw)
    assert r2["model_holds"] is True and r2["spread"] is not None, r2
    assert r2["spread"] > 0 and r2["spread_bp"] > 0
    # 진폭 A 인 톱니는 가격변화가 +A, -A 를 번갈아 자기공분산이 -A^2 이므로
    # Roll 스프레드는 2*sqrt(A^2) = 2A 다. A=1 이면 2.0 근방.
    # (호가가 S 만큼 벌어진 진짜 bid-ask bounce 는 자기공분산 -S^2/4 라
    #  2*sqrt(S^2/4) = S 로 되돌아온다 - 여기 픽스처는 ±1 전폭 진동이라 2A.)
    assert abs(r2["spread"] - 2.0) < 0.1, r2
    assert abs(r2["autocov"] + 1.0) < 0.05, r2

    # 표본 부족이면 None (0 으로 채우지 않는다)
    assert roll_spread([100.0, 101.0])["spread"] is None
    assert roll_spread([])["spread"] is None
    print("  Roll 모형 성립 조건      OK")


def _check_compute_from_bars():
    bars = [{"bucket_time": f"2026-07-{d:02d}", "close": 100.0 + (d % 2),
             "notional": 1e9} for d in range(1, 22)]
    bars.reverse()                       # API 는 최신순으로 준다
    out = compute_liquidity(bars)
    assert out["bars_used"] == 21 and out["last_bar_date"] == "2026-07-21", out
    assert out["amihud"]["illiq"] is not None
    assert out["roll"]["model_holds"] is True

    # 거래대금이 없으면 Amihud 는 미확인, Roll 은 그대로 나온다
    no_dv = [{k: v for k, v in b.items() if k != "notional"} for b in bars]
    out2 = compute_liquidity(no_dv)
    assert out2["amihud"] is None and out2["roll"]["spread"] is not None

    # 같은 bucket_time 중복은 나중 것이 이긴다
    dup = [{"bucket_time": "2026-07-01", "close": 1.0, "notional": 1.0},
           {"bucket_time": "2026-07-01", "close": 2.0, "notional": 1.0}]
    assert compute_liquidity(dup)["bars_used"] == 1
    print("  bars -> 지표 조립        OK")


def _check_registry_contract():
    """레지스트리가 이 파일을 가리키고, 그 함수가 실재하는가."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from methods import METHODS

    m = {x.key: x for x in METHODS}
    for key, func in (("amihud_illiquidity", amihud_illiquidity),
                      ("roll_effective_spread", roll_spread)):
        assert key in m, f"{key} 가 레지스트리에 없다"
        assert m[key].status == "ADOPTED", key
        path, _, name = (m[key].module or "").partition(":")
        assert path == "evidence/liquidity.py", (key, path)
        assert name and name in globals(), \
            f"{key}: 레지스트리가 가리키는 함수 {name!r} 가 이 파일에 없다"
        assert callable(func)
    print("  레지스트리 계약 일치     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_amihud_math()
    _check_roll_model_limit()
    _check_compute_from_bars()
    _check_registry_contract()
    print("유동성 지표 4개 영역 통과.")
