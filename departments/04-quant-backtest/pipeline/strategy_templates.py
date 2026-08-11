"""전략 시그널 - 실행면의 전략 층.

담당: 재일 (퀀트·백테스트본부 QNT)

▶ 무엇이 문제였나
  실행면에 실제로 구현된 시그널이 **모멘텀 하나뿐**이었다. `MOM-20-SMOKE` 와
  `REV-5-SMOKE` 는 같은 `market.momentum` 을 순위 방향만 바꿔 썼다. 그래서
  리서치가 "유동성 충격 반전" 같은 다른 유형을 기획해 와도 실행할 방법이 없었다.
  전략 공장이 파라미터 손잡이만 돌리는 기계가 되는 지점이다.

▶ 두 가지를 함께 연다
  ① 검증된 기성 템플릿 - 흔한 유형은 여기서 바로 꺼내 쓴다(아래 TEMPLATES).
  ② 실험별 커스텀 시그널 - 템플릿에 없는 유형은 코드로 받는다(strategy_spec.py).
  **전략 코드가 실험마다 다른 것은 정상이다.** 막아야 하는 것은 코드가 다른 것이
  아니라 *결과를 본 뒤* 코드가 바뀌는 것이다 - 그건 사전등록에 코드 해시를 넣어
  막는다(strategy_spec.spec_hash).

▶ 누수는 검사가 아니라 **구조**로 막는다
  시그널 함수에 Market 을 통째로 주지 않고 `PITView` 를 준다. PITView 는 기준일
  `until` **이하** 데이터만 노출하며 미래를 꺼낼 수 있는 접근자가 아예 없다.
  사후 누수 검사는 통과시킬 방법이 있지만, 꺼낼 수 없는 데이터는 쓸 수 없다.
  (그럼에도 자체 점검은 미래 데이터를 붙여 시그널 불변성을 다시 확인한다 - 이중 방어)

▶ 시그널은 점수만 낸다
  종목당 float 하나. 상위/하위 선택과 비중·체결·비용은 러너가 한다. 시그널이
  주문이나 비중을 만들면 전략마다 실행 규칙이 달라져 비교가 불가능해진다.

자체 점검: python departments/04-quant-backtest/pipeline/strategy_templates.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from typing import Callable, Protocol

MODULE_VERSION = "quant-strategy-templates-v1"

# 표본이 이보다 적으면 그 종목 점수를 내지 않는다. **0 으로 채우지 않는다** -
# 0 은 "중립"이 아니라 "모른다"이고, 0 을 넣으면 데이터 없는 종목이 순위 중앙에
# 들어와 조용히 선택된다.
_EPS = 1e-12


class MarketLike(Protocol):
    dates: list
    closes: dict
    symbols: list
    notionals: dict


# ---------------------------------------------------------------------------
# PITView - 시그널이 볼 수 있는 전부. 미래를 꺼낼 접근자가 없다.
# ---------------------------------------------------------------------------

class PITView:
    """기준일 `until` 이하만 노출하는 시장 뷰.

    시그널 함수는 Market 이 아니라 이것을 받는다. `until` 초과 날짜를 꺼내는
    메서드가 존재하지 않으므로, 커스텀 코드가 무엇을 하든 미래를 볼 수 없다.
    """

    __slots__ = ("_m", "_until", "_idx")

    def __init__(self, market: MarketLike, until: date):
        self._m = market
        self._until = until
        # 여기서 한 번 잘라 두고, 이후 어떤 경로로도 이 목록 밖을 보지 않는다.
        self._idx = [d for d in market.dates if d <= until]

    # ── 조회면 ──────────────────────────────────────────────────────────────
    @property
    def until(self) -> date:
        return self._until

    @property
    def symbols(self) -> list[str]:
        return list(self._m.symbols)

    def n_days(self) -> int:
        """기준일까지 확보된 거래일 수."""
        return len(self._idx)

    def dates(self, n: int | None = None) -> list[date]:
        return list(self._idx if n is None else self._idx[-n:])

    def closes(self, symbol: str, n: int) -> list[float]:
        """최근 n 거래일 종가(오름차순). **구멍이 있으면 짧게 돌려준다** -
        없는 값을 앞뒤로 채우면 그 자체가 지어낸 데이터다."""
        out = []
        for d in self._idx[-n:]:
            v = self._m.closes.get((d, symbol))
            if v is not None:
                out.append(float(v))
        return out

    def notionals(self, symbol: str, n: int) -> list[float]:
        out = []
        for d in self._idx[-n:]:
            v = self._m.notionals.get((d, symbol))
            if v is not None:
                out.append(float(v))
        return out

    # ── 파생 재료 (직접 구현하면 실수하기 쉬운 것들) ─────────────────────────
    def total_return(self, symbol: str, lookback: int) -> float | None:
        """lookback 거래일 전 대비 수익률. 표본이 모자라면 None."""
        if len(self._idx) < lookback + 1:
            return None
        d_now, d_then = self._idx[-1], self._idx[-1 - lookback]
        a = self._m.closes.get((d_then, symbol))
        b = self._m.closes.get((d_now, symbol))
        if a is None or b is None or a <= 0:
            return None
        return float(b) / float(a) - 1.0

    def daily_returns(self, symbol: str, n: int) -> list[float]:
        px = self.closes(symbol, n + 1)
        return [px[i] / px[i - 1] - 1.0
                for i in range(1, len(px)) if px[i - 1] > 0]

    def volatility(self, symbol: str, n: int) -> float | None:
        """실현 변동성(일별 수익률 표준편차).

        **요청한 창을 다 채우지 못하면 None 이다.** 3일치로 계산한 값을
        "20일 변동성"이라 부르면 그건 다른 지표이고, 종목마다 창 길이가 달라져
        순위 비교 자체가 성립하지 않는다.
        """
        r = self.daily_returns(symbol, n)
        if len(r) < n or len(r) < 2:
            return None
        mu = sum(r) / len(r)
        var = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
        return var ** 0.5

    def adv(self, symbol: str, n: int) -> float | None:
        """평균 거래대금. **창을 다 채우지 못하면 None**(0 도, 부분평균도 아니다)."""
        v = self.notionals(symbol, n)
        return sum(v) / len(v) if len(v) >= n and v else None

    def max_close(self, symbol: str, n: int) -> float | None:
        px = self.closes(symbol, n)
        return max(px) if px else None

    def sma(self, symbol: str, n: int) -> float | None:
        px = self.closes(symbol, n)
        return sum(px) / len(px) if len(px) >= n else None


# ---------------------------------------------------------------------------
# 템플릿 정의
# ---------------------------------------------------------------------------

SignalFn = Callable[[PITView, dict], dict]


@dataclass(frozen=True)
class Template:
    """검증된 기성 시그널 하나.

    edge_type 이 **Gate 0 의 통제 어휘**다 - 리서치 기획안의 edge_type 은 이
    집합에 사상되어야 접수된다(trial_family 가 이 어휘로 Family 를 가른다).
    """

    template_id: str          # 전략 ID 접두 (MOM, REV, ...)
    edge_type: str            # 통제 어휘 키
    rank: str                 # TOP | BOTTOM - 점수 상위/하위 중 무엇을 사는가
    signal: SignalFn
    min_history: Callable[[dict], int]
    claimed_edge: str         # 이 템플릿이 주장하는 엣지(실험이 검증한다)
    note: str


def _p(params: dict, key: str, default: int) -> int:
    v = params.get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── 시그널들 ────────────────────────────────────────────────────────────────
# 전부 PITView 만 받는다. 표본이 모자란 종목은 **결과에서 뺀다**(0 으로 안 채운다).

def _sig_return(v: PITView, params: dict) -> dict:
    lb = _p(params, "lookback_days", 20)
    out = {}
    for s in v.symbols:
        r = v.total_return(s, lb)
        if r is not None:
            out[s] = r
    return out


def _sig_volatility(v: PITView, params: dict) -> dict:
    lb = _p(params, "lookback_days", 20)
    out = {}
    for s in v.symbols:
        sd = v.volatility(s, lb)
        if sd is not None:
            out[s] = sd
    return out


def _sig_risk_adjusted_return(v: PITView, params: dict) -> dict:
    """수익률 / 변동성. 변동성이 0 이면 제외한다 - 나눗셈을 방어값으로
    가리면 '변동 없는 종목'이 무한대 점수로 1등이 된다."""
    lb = _p(params, "lookback_days", 20)
    out = {}
    for s in v.symbols:
        r, sd = v.total_return(s, lb), v.volatility(s, lb)
        if r is not None and sd is not None and sd > _EPS:
            out[s] = r / sd
    return out


def _sig_liquidity_shock_reversal(v: PITView, params: dict) -> dict:
    """거래대금이 평소보다 급증하면서 하락한 종목.

    유동성 충격(강제 매도)이 가격을 밀어냈다면 되돌아온다는 가설. 점수는
    `충격배수 x (-수익률)` 이고, 거래대금이 없으면 계산하지 않는다.
    """
    lb = _p(params, "lookback_days", 5)
    base = _p(params, "adv_window", 60)
    out = {}
    for s in v.symbols:
        r = v.total_return(s, lb)
        recent, normal = v.adv(s, lb), v.adv(s, base)
        if r is None or recent is None or normal is None or normal <= _EPS:
            continue
        surge = recent / normal
        if surge <= 1.0 or r >= 0:      # 급증도 아니고 하락도 아니면 대상 아님
            continue
        out[s] = surge * (-r)
    return out


def _sig_breakout(v: PITView, params: dict) -> dict:
    """N일 최고가 대비 현재 위치. 1.0 에 가까울수록 신고가 근처."""
    lb = _p(params, "lookback_days", 60)
    out = {}
    for s in v.symbols:
        px = v.closes(s, lb)
        hi = v.max_close(s, lb)
        if not px or hi is None or hi <= _EPS or len(px) < lb:
            continue
        out[s] = px[-1] / hi
    return out


def _sig_trend_distance(v: PITView, params: dict) -> dict:
    """이동평균 대비 이격도. 종점 두 개만 보는 모멘텀과 달리 경로를 본다."""
    lb = _p(params, "lookback_days", 20)
    out = {}
    for s in v.symbols:
        px = v.closes(s, lb)
        ma = v.sma(s, lb)
        if not px or ma is None or ma <= _EPS:
            continue
        out[s] = px[-1] / ma - 1.0
    return out


def _sig_illiquidity(v: PITView, params: dict) -> dict:
    """평균 거래대금. 낮은 쪽을 사면 비유동성 프리미엄 가설이 된다."""
    lb = _p(params, "adv_window", 60)
    out = {}
    for s in v.symbols:
        a = v.adv(s, lb)
        if a is not None and a > _EPS:
            out[s] = a
    return out


TEMPLATES: dict[str, Template] = {
    t.template_id: t for t in (
        Template("MOM", "momentum", "TOP", _sig_return,
                 lambda p: _p(p, "lookback_days", 20) + 1,
                 "과거 상승이 이어진다(수익률 상위 매수)",
                 "N일 수익률 상위 균등"),
        Template("REV", "mean_reversion", "BOTTOM", _sig_return,
                 lambda p: _p(p, "lookback_days", 5) + 1,
                 "단기 과매도가 되돌아온다(수익률 하위 매수)",
                 "N일 수익률 하위 균등"),
        Template("LOWVOL", "low_volatility", "BOTTOM", _sig_volatility,
                 lambda p: _p(p, "lookback_days", 20) + 1,
                 "저변동 종목이 위험 대비 초과수익을 낸다",
                 "실현 변동성 하위 균등"),
        Template("RAMOM", "risk_adjusted_momentum", "TOP",
                 _sig_risk_adjusted_return,
                 lambda p: _p(p, "lookback_days", 20) + 1,
                 "변동성으로 정규화한 추세가 원시 추세보다 안정적이다",
                 "수익률/변동성 상위 균등"),
        Template("LIQREV", "liquidity_shock_reversal", "TOP",
                 _sig_liquidity_shock_reversal,
                 lambda p: max(_p(p, "adv_window", 60),
                               _p(p, "lookback_days", 5) + 1),
                 "유동성 충격에 밀린 가격은 되돌아온다",
                 "거래대금 급증 + 하락 종목 매수"),
        Template("BRK", "breakout", "TOP", _sig_breakout,
                 lambda p: _p(p, "lookback_days", 60),
                 "신고가 돌파가 추세 지속의 신호다",
                 "N일 최고가 근접 상위 균등"),
        Template("TREND", "trend_following", "TOP", _sig_trend_distance,
                 lambda p: _p(p, "lookback_days", 20),
                 "이동평균 위 종목이 추세를 유지한다",
                 "이동평균 이격도 상위 균등"),
        Template("ILLIQ", "illiquidity_premium", "BOTTOM", _sig_illiquidity,
                 lambda p: _p(p, "adv_window", 60),
                 "비유동성에 프리미엄이 붙는다",
                 "평균 거래대금 하위 균등"),
    )
}

# Gate 0 통제 어휘. 리서치 기획안의 edge_type 은 여기 있어야 접수된다.
EDGE_VOCAB: frozenset[str] = frozenset(t.edge_type for t in TEMPLATES.values())

# ▶ 요청은 있으나 **실행면에 없는** 유형. 어휘에 넣지 않는다 - 넣으면 Gate 0 이
#   접수해 놓고 실행 단계에서 죽는다(접수는 실행 가능성의 약속이어야 한다).
#   리서치가 이 유형을 기획해 오면 UNMAPPED_VOCAB 으로 반려되고, 여기 사유가 뜬다.
NOT_IMPLEMENTED: dict[str, str] = {
    "volatility_risk_premium": "옵션 IV·RV 시계열이 Market 에 없다(파생 데이터 면 필요)",
    "cross_asset_carry": "금리·FX 캐리 시계열이 Market 에 없다",
    "earnings_drift": "공시 이벤트 시각이 Market 격자에 붙어 있지 않다",
    "pairs_trading": "러너가 종목 단위 균등비중만 지원한다(롱숏·페어 미지원)",
}


def template_for_edge(edge_type: str) -> Template | None:
    key = str(edge_type or "").strip().lower()
    for t in TEMPLATES.values():
        if t.edge_type == key:
            return t
    return None


def resolve(strategy_id: str) -> Template | None:
    """전략 ID -> 템플릿. `REV-5-20` 처럼 파라미터가 새겨진 ID 도 받는다.

    ▶ 이 함수가 없어서 실제 버그가 있었다(2026-08-10 실측): config_binding 이
      가설 파라미터를 ID 에 새겨 `REV-5-20` 을 만드는데 러너 카탈로그에는 그
      문자열이 없어 **가설이 반영되는 순간 실행이 거부**됐다. 즉 파라미터가
      기본값과 같을 때만 실험이 돌았다.
    """
    sid = str(strategy_id or "").strip().upper()
    if not sid:
        return None
    return TEMPLATES.get(sid.split("-")[0])


def signal_scores(market: MarketLike, until: date, *, template: Template,
                  params: dict) -> dict:
    """템플릿 시그널 실행. **Market 이 아니라 PITView 를 넘긴다.**"""
    return template.signal(PITView(market, until), params)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _mk(n_days: int = 90, symbols=("A", "B", "C", "D")):
    """결정적 가짜 시장. 종목마다 다른 추세·변동·거래대금을 준다."""
    from datetime import timedelta

    class _M:
        pass

    m = _M()
    d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(n_days)]
    m.symbols = list(symbols)
    m.closes, m.notionals, m.opens = {}, {}, {}
    # ▶ **모든 값이 절대 인덱스 i 로만 정해진다.** n_days 에 의존하면 "같은 규칙에
    #   미래를 덧붙인 시장"이 아니라 과거까지 다른 시장이 되고, PIT 점검이
    #   무의미해진다(이전 판이 그랬고 자체 점검이 잡아냈다).
    drift = (0.0015, 0.0005, -0.0010, -0.0030)
    for si, s in enumerate(symbols):
        px = 100.0
        for i, d in enumerate(m.dates):
            wob = 0.004 if (i + si) % 7 == 0 else -0.0008
            px *= 1.0 + drift[si % len(drift)] + wob
            m.closes[(d, s)] = px
            m.opens[(d, s)] = px
            # 55~59 구간에만 거래대금 급증 - 위치가 고정이라 append-only 다
            m.notionals[(d, s)] = 1e9 * (si + 1) * (3.0 if 55 <= i < 60 else 1.0)
    return m


def _check_pit_view_cannot_see_future():
    """**미래 데이터를 붙여도 같은 기준일의 시그널이 변하지 않는다.**

    구조적으로는 PITView 가 잘라 주므로 불가능하지만, 그 구조가 깨졌는지
    감지하는 이중 방어다.
    """
    short = _mk(60)
    long = _mk(90)                       # 같은 규칙 + 미래 30일 추가
    until = short.dates[-1]
    for t in TEMPLATES.values():
        a = signal_scores(short, until, template=t, params={})
        b = signal_scores(long, until, template=t, params={})
        assert a == b, f"{t.template_id}: 미래 데이터가 과거 시그널을 바꿨다"


def _check_deterministic():
    m = _mk()
    until = m.dates[-1]
    for t in TEMPLATES.values():
        assert (signal_scores(m, until, template=t, params={})
                == signal_scores(m, until, template=t, params={})), t.template_id


def _check_insufficient_history_is_empty_not_zero():
    """**표본이 모자라면 뺀다. 0 으로 채우지 않는다** - 0 은 순위 중앙에 앉아
    조용히 선택된다."""
    m = _mk(3)
    until = m.dates[-1]
    for t in TEMPLATES.values():
        out = signal_scores(m, until, template=t,
                            params={"lookback_days": 30, "adv_window": 60})
        assert out == {}, f"{t.template_id}: 데이터 부족인데 점수를 냈다 {out}"


def _check_missing_notional_does_not_fabricate():
    """거래대금이 없으면 유동성 계열은 점수를 내지 않는다."""
    m = _mk()
    m.notionals = {}
    until = m.dates[-1]
    for tid in ("LIQREV", "ILLIQ"):
        assert signal_scores(m, until, template=TEMPLATES[tid],
                             params={}) == {}, tid


def _check_rank_directions_are_opposite():
    """TOP/BOTTOM 이 실제로 반대 종목을 고른다 - 방향이 같으면 두 전략이 사실상
    하나이고, 그건 예전 편제의 문제였다."""
    m = _mk()
    until = m.dates[-1]
    sc = signal_scores(m, until, template=TEMPLATES["MOM"], params={})
    assert len(sc) >= 2
    top = sorted(sc, key=sc.get, reverse=True)[0]
    bottom = sorted(sc, key=sc.get)[0]
    assert top != bottom


def _check_edge_vocab_is_unique_and_covers_templates():
    kinds = [t.edge_type for t in TEMPLATES.values()]
    assert len(kinds) == len(set(kinds)), f"edge_type 중복: {kinds}"
    assert EDGE_VOCAB == set(kinds)
    # 미구현 유형은 어휘에 없어야 한다 - 있으면 접수 후 실행 단계에서 죽는다
    assert not (EDGE_VOCAB & set(NOT_IMPLEMENTED)), \
        "미구현 유형이 통제 어휘에 섞였다"


def _check_resolve_accepts_parameterised_ids():
    """`REV-5-20` 같은 동적 ID 를 받는다 - 실측 버그의 재발 방지."""
    assert resolve("REV-5-SMOKE").template_id == "REV"
    assert resolve("REV-5-20").template_id == "REV"
    assert resolve("MOM-25-22").template_id == "MOM"
    assert resolve("LIQREV-5-20").template_id == "LIQREV"
    assert resolve("NOPE-1-1") is None and resolve("") is None


def _check_matches_legacy_momentum():
    """기존 `Market.momentum` 과 값이 같아야 한다 - 다르면 과거 실험이 재현
    불가가 된다(이 변경은 실행면 확장이지 결과 변경이 아니다)."""
    m = _mk()
    until = m.dates[-1]
    idx = [d for d in m.dates if d <= until]
    lb = 20
    legacy = {}
    d_now, d_then = idx[-1], idx[-1 - lb]
    for s in m.symbols:
        a, b = m.closes.get((d_then, s)), m.closes.get((d_now, s))
        if a and b and a > 0:
            legacy[s] = b / a - 1.0
    new = signal_scores(m, until, template=TEMPLATES["MOM"],
                        params={"lookback_days": lb})
    assert new.keys() == legacy.keys()
    for s in legacy:
        assert abs(new[s] - legacy[s]) < 1e-12, (s, new[s], legacy[s])


def _check_min_history_is_honest():
    """min_history 가 실제 요구와 맞는가 - 그보다 하루 짧으면 비어야 한다."""
    for t in TEMPLATES.values():
        need = t.min_history({})
        m = _mk(max(need - 1, 2))
        out = signal_scores(m, m.dates[-1], template=t, params={})
        assert out == {}, f"{t.template_id}: {need-1}일인데 점수를 냈다"


def _check_zero_volatility_is_excluded_not_defaulted():
    """변동성 0 을 방어값으로 가리면 무한대 점수가 1등이 된다."""
    from datetime import timedelta
    class _M: pass
    m = _M(); d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(40)]
    m.symbols = ["FLAT", "MOVE"]
    m.closes, m.notionals, m.opens = {}, {}, {}
    for i, d in enumerate(m.dates):
        m.closes[(d, "FLAT")] = 100.0                      # 변동 0
        m.closes[(d, "MOVE")] = 100.0 + (i % 3)
        m.notionals[(d, "FLAT")] = m.notionals[(d, "MOVE")] = 1e9
    out = signal_scores(m, m.dates[-1], template=TEMPLATES["RAMOM"], params={})
    assert "FLAT" not in out, out


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음) - 템플릿 {len(TEMPLATES)}종")
    _check_pit_view_cannot_see_future();      print("  PIT: 미래 불가시           OK")
    _check_deterministic();                   print("  결정론                     OK")
    _check_insufficient_history_is_empty_not_zero()
    print("  표본 부족 -> 제외(0 아님)   OK")
    _check_missing_notional_does_not_fabricate()
    print("  거래대금 없음 -> 미산출     OK")
    _check_rank_directions_are_opposite();    print("  TOP/BOTTOM 반대 방향       OK")
    _check_edge_vocab_is_unique_and_covers_templates()
    print("  통제 어휘 유일·미구현 분리  OK")
    _check_resolve_accepts_parameterised_ids()
    print("  동적 ID 해석(실측 버그)     OK")
    _check_matches_legacy_momentum();         print("  기존 모멘텀과 값 동일       OK")
    _check_min_history_is_honest();           print("  min_history 정직            OK")
    _check_zero_volatility_is_excluded_not_defaulted()
    print("  변동성 0 제외(무한대 방지)  OK")
    print(f"전략 템플릿 10개 영역 통과. 어휘: {sorted(EDGE_VOCAB)}")
