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



def corwin_schultz(highs, lows, *, window: int = WINDOW,
                   min_days: int = 3) -> dict | None:
    """Corwin-Schultz (2012) 고가-저가 기반 유효 스프레드 추정.

    ▶ 왜 Roll 만으로 부족한가
      Roll 은 종가 자기공분산이 **음수일 때만** 정의된다 - 추세장에서 양수가
      나오면 통째로 미확인이다. 실측에서 RES-03 이 스프레드를 못 말하는 날이
      잦았던 이유다. Corwin-Schultz 는 일중 고가·저가만 쓰므로 추세와 무관하게
      나온다. 서로 다른 가정의 두 추정치가 있으면 한쪽이 죽어도 판정이 산다.

    beta  = 이틀치 (ln(H/L))^2 합
    gamma = 이틀 통합 고가/저가의 (ln(H/L))^2
    alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma/(3-2*sqrt(2)))
    S     = 2*(exp(alpha)-1) / (1+exp(alpha))

    **음수 추정은 0 으로 절사하지 않고 그대로 센다.** 절사하면 추정 잡음이
    한쪽으로만 쌓여 스프레드가 실제보다 넓어 보인다 - 원논문도 음수 관측을
    기간 평균에 그대로 넣는 쪽을 권한다.
    """
    pairs = [(h, l) for h, l in zip(highs or [], lows or [])
             if h is not None and l is not None and h > 0 and l > 0 and h >= l]
    if len(pairs) < min_days + 1:
        return None
    pairs = pairs[-(window + 1):]
    k = 3.0 - 2.0 * math.sqrt(2.0)
    vals = []
    for i in range(1, len(pairs)):
        (h1, l1), (h2, l2) = pairs[i - 1], pairs[i]
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2
        try:
            alpha = ((math.sqrt(2.0 * beta) - math.sqrt(beta)) / k
                     - math.sqrt(gamma / k))
            s = 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))
        except (ValueError, OverflowError):
            continue
        vals.append(s)
    if not vals:
        return None
    return {"spread_pct": round(sum(vals) / len(vals) * 100.0, 4),
            "days_used": len(vals),
            "negative_days": sum(1 for v in vals if v < 0)}


def kyle_lambda(closes, volumes, *, window: int = WINDOW,
                min_days: int = 5) -> dict | None:
    """Kyle 람다 대용 - 거래량 1단위당 가격 충격.

    원 모형은 **부호 있는** 주문흐름이 필요한데 우리는 일봉만 있으므로
    수익률 부호를 매수·매도 압력의 대용으로 쓴다(Amihud 와 같은 계열의 타협).
    proxy 임을 지우지 않는다 - 원 람다로 읽히면 안 된다.

    회귀 대신 |r| / sqrt(V) 의 중앙값을 쓴다. 소표본 회귀는 하루의 이상치가
    기울기를 통째로 끌고, 우리는 60봉 안팎만 본다.
    """
    pairs = [(c, v) for c, v in zip(closes or [], volumes or [])
             if c is not None and v is not None and c > 0 and v > 0]
    if len(pairs) < min_days + 1:
        return None
    pairs = pairs[-(window + 1):]
    impacts = []
    for i in range(1, len(pairs)):
        c0, _ = pairs[i - 1]
        c1, v1 = pairs[i]
        r = abs(c1 / c0 - 1.0)
        impacts.append(r / math.sqrt(v1))
    if not impacts:
        return None
    impacts.sort()
    m = len(impacts)
    med = (impacts[m // 2] if m % 2 else (impacts[m // 2 - 1] + impacts[m // 2]) / 2)
    return {"lambda_proxy": round(med * 1e6, 4), "days_used": m,
            "scale": "x1e6, |수익률|/sqrt(거래량)", "is_proxy": True}


def _num(v):
    """숫자로 못 읽으면 None. **0 으로 대체하지 않는다.**"""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


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
    # 없는 필드는 None 으로 남긴다 - 0 으로 채우면 "못 구했다" 가 사라진다
    highs = [_num(b.get("high")) for b in rows]
    lows = [_num(b.get("low")) for b in rows]
    volumes = [_num(b.get("volume")) for b in rows]

    return {
        "bars_used": len(rows),
        "last_bar_date": str(rows[-1]["bucket_time"])[:10] if rows else None,
        "amihud": (amihud_illiquidity(closes, notionals, window=window)
                   if has_notional else None),
        "roll": roll_spread(closes, window=window),
        # ▶ Roll 은 종가 자기공분산이 음수일 때만 정의된다 - 추세장에서 통째로
        #   미확인이 된다. 가정이 다른 두 번째 추정치를 함께 둔다.
        "corwin_schultz": corwin_schultz(highs, lows, window=window),
        "kyle_lambda": kyle_lambda(closes, volumes, window=window),
        "method_keys": ("amihud_illiquidity", "roll_effective_spread",
                        "corwin_schultz_spread", "kyle_lambda_proxy"),
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


def _check_second_estimator_survives_roll_failure():
    """**Roll 이 죽는 날 스프레드 판정이 살아남는가.**

    Roll 은 종가 자기공분산이 음수일 때만 정의된다 - 추세·진동장에서 양수가
    나오면 통째로 미확인이고, 그런 날 RES-03 은 스프레드를 아예 못 말했다.
    가정이 다른 두 번째 추정치를 둔 이유가 이것이므로, 그 상황을 픽스처로
    고정한다. Roll 이 살아 있는 픽스처만 쓰면 이 검사는 아무것도 안 지킨다.
    """
    bars = []
    for i in range(40):
        c = 100.0 + 5 * math.sin(i / 4.0)
        bars.append({"bucket_time": f"2026-06-{i + 1:02d}", "close": c,
                     "high": c * 1.012, "low": c * 0.988,
                     "volume": 1e5 + i * 500, "notional": c * 1e5})
    bars.reverse()                                  # API 는 최신순으로 준다
    r = compute_liquidity(bars)
    assert r["roll"]["model_holds"] is False, "픽스처가 Roll 을 죽이지 못했다"
    assert r["roll"]["spread"] is None
    cs = r["corwin_schultz"]
    assert cs and cs["spread_pct"] > 0, cs
    assert cs["days_used"] >= 20, cs
    # 음수 추정을 절사하지 않았다는 사실이 드러나야 한다 - 절사하면 잡음이
    # 한쪽으로만 쌓여 스프레드가 실제보다 넓어 보인다
    assert "negative_days" in cs, cs
    kl = r["kyle_lambda"]
    assert kl and kl["lambda_proxy"] > 0 and kl["is_proxy"] is True, kl


def _check_missing_fields_are_none_not_zero():
    """고가·저가·거래량이 없으면 **미확인**이다. 0 으로 채우지 않는다."""
    bars = [{"bucket_time": f"2026-06-{i + 1:02d}", "close": 100.0 + i}
            for i in range(30)]
    r = compute_liquidity(bars)
    assert r["corwin_schultz"] is None, r["corwin_schultz"]
    assert r["kyle_lambda"] is None, r["kyle_lambda"]
    assert r["amihud"] is None, "notional 없이 Amihud 가 나왔다"


def _check_registry_contract():
    """레지스트리가 이 파일을 가리키고, 그 함수가 실재하는가."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from methods import METHODS

    m = {x.key: x for x in METHODS}
    # ▶ **compute_liquidity 가 실제로 내는 method_keys 를 순회한다.** 예전엔
    #   키 2개를 하드코딩해서 지표를 새로 붙여도 검사가 그냥 통과했다 -
    #   호출하는데 레지스트리엔 없는 상태(F-Score 결함의 거울상)를 못 잡았다.
    #   목록을 코드에서 가져오면 드리프트가 불가능하다.
    declared = compute_liquidity([])["method_keys"]
    assert set(declared) >= {"amihud_illiquidity", "roll_effective_spread"}, declared
    for key in declared:
        assert key in m, f"{key} 가 레지스트리에 없다"
        assert m[key].status == "ADOPTED", key
        path, _, name = (m[key].module or "").partition(":")
        assert path == "evidence/liquidity.py", (key, path)
        assert name and name in globals(), \
            f"{key}: 레지스트리가 가리키는 함수 {name!r} 가 이 파일에 없다"
    print("  레지스트리 계약 일치     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_amihud_math()
    _check_roll_model_limit()
    _check_compute_from_bars()
    _check_second_estimator_survives_roll_failure()
    print("  Roll 실패시 2차 추정      OK")
    _check_missing_fields_are_none_not_zero()
    print("  결측=미확인(0 아님)      OK")
    _check_registry_contract()
    print("유동성 지표 6개 영역 통과.")
