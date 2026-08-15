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


# ── 거래대금 단위 ───────────────────────────────────────────────────────────
#
# ▶ **데이터셋 `notional` 은 원이 아니라 백만원이다** (2026-08-14 실측)
#   krx-basket-daily/v3 2026-08 파티션 첫 행: close 1,525 × volume 150,555 =
#   2.296억원인데 저장된 `notional` 은 **227**이다. 세 행을 검산해도 비율이
#   모두 ≈1e6 이었다. 비용 모델도 같은 단위를 쓴다 -
#   `backtest_runner.LIQUIDITY_TIERS` 의 최저 계층 상한이 3,000 인데, 이것이
#   30억원이라야 "얇은 종목일수록 비싸다" 는 계층 설계가 성립한다.
#
#   그런데 `min_adv_krw` 만 이름대로 **원 단위**로 받아 그대로 비교하고 있었다.
#   1억(=1e8)을 백만원 단위 값(중앙값 269)과 겨루니 **전 종목이 탈락**했고,
#   유니버스가 0 이 됐다 - 분위 곡선 측정에서 "체결가능 ADV 1억: 표본 부족(0)"
#   으로 드러났다. 실험 config 에는 이미 `min_adv_krw=100000000.0` 이 실려
#   돌고 있었으므로, 그 실험들은 빈 유니버스를 본 셈이다.
#
#   자체점검이 이것을 못 잡은 이유도 같다 - **픽스처가 원 단위(5e8)였다.**
#   실물과 다른 단위로 검사하면 검사는 통과하고 현장만 죽는다. 아래 검사의
#   픽스처는 실제 데이터와 같은 백만원 단위로 맞췄다.
NOTIONAL_UNIT_KRW = 1_000_000


# ── 미조정 액면분할·병합 방어선 ─────────────────────────────────────────────
# 정수배 판정 기준. ×2 미만은 진짜 급등과 구별할 수 없으므로 건드리지 않고,
# 배수에서 벗어난 정도가 이 허용치를 넘으면 조정 이벤트로 보지 않는다.
_ADJ_MIN_MULT = 2.0
_ADJ_TOL = 0.002


def _adjustment_break(px: list[float]) -> int:
    """미조정 분할·병합으로 보이는 **마지막 점프 위치**. 없으면 0.

    가격비가 정확히 정수배(오차 0.2% 이내)로 뛰거나 떨어진 지점을 찾는다.
    ×10.0000 같은 값이 우연히 나올 확률은 사실상 없고(실측 21건 전부 정수),
    진짜 급등은 배수에서 벗어나므로 여기 안 걸린다.
    """
    for i in range(len(px) - 1, 0, -1):
        a, b = px[i - 1], px[i]
        if a <= 0 or b <= 0:
            continue
        r = b / a
        mult = r if r >= 1.0 else 1.0 / r
        if mult < _ADJ_MIN_MULT:
            continue
        near = round(mult)
        if near >= 2 and abs(mult - near) <= _ADJ_TOL * near:
            return i        # 이 지점 **이후**만 쓴다(점프 전 가격은 다른 단위다)
    return 0


# ---------------------------------------------------------------------------
# PITView - 시그널이 볼 수 있는 전부. 미래를 꺼낼 접근자가 없다.
# ---------------------------------------------------------------------------

class PITView:
    """기준일 `until` 이하만 노출하는 시장 뷰.

    시그널 함수는 Market 이 아니라 이것을 받는다. `until` 초과 날짜를 꺼내는
    메서드가 존재하지 않으므로, 커스텀 코드가 무엇을 하든 미래를 볼 수 없다.
    """

    __slots__ = ("_m", "_until", "_idx", "_min_adv", "_min_days", "_tradable")

    def __init__(self, market: MarketLike, until: date, *,
                 min_adv_krw: float = 0.0, min_trading_days: int = 0,
                 liquidity_window: int = 60):
        self._m = market
        self._until = until
        # 여기서 한 번 잘라 두고, 이후 어떤 경로로도 이 목록 밖을 보지 않는다.
        self._idx = [d for d in market.dates if d <= until]
        self._min_adv = float(min_adv_krw or 0.0)
        self._min_days = int(min_trading_days or 0)
        self._tradable: list[str] | None = None
        if self._min_adv > 0 or self._min_days > 0:
            self._tradable = self._compute_tradable(int(liquidity_window or 60))

    def _compute_tradable(self, win: int) -> list[str]:
        """**살 수 있는 종목만 남긴다** (2026-08-14 실측).

        ▶ 왜 필요했나
          비유동성 프리미엄 신호가 실제로 뽑은 종목을 재보니 일평균 거래대금
          중앙값이 **0원**, 60일 창에서 무거래일이 평균 57.2일이었다. 그 위에서
          나온 IR 0.17·DSR 0.997 은 **백테스트에서만 존재하는 수익**이다 -
          1억을 배분하면 종목당 5백만원인데 그 종목의 하루 거래대금이 사실상
          0 이라 체결 자체가 성립하지 않는다.
          문헌(McLean-Pontiff)이 같은 것을 경고한다: 공개 후 살아남는 수익은
          유동성이 낮아 차익거래가 어려운 구간에 몰린다 - 그 구간은 우리도
          거래할 수 없다.

        ▶ PIT 안전
          기준일까지의 거래대금만 본다. 미래 유동성으로 과거 유니버스를
          고르면 그 자체가 생존 편향이다.

        ▶ 기본값은 꺼짐(0)
          켜고 끄는 판단은 가설이 한다 - 여기서 임의로 켜면 과거 실험과
          비교가 불가능해지고, 어떤 유니버스로 재려는지는 사전등록 대상이다.
        """
        out = []
        for s in self._m.symbols:
            vals = [v for v in (self._m.notionals.get((d, s))
                                for d in self._idx[-win:]) if v is not None]
            traded = [float(v) for v in vals if float(v) > 0]
            if self._min_days and len(traded) < self._min_days:
                continue
            if self._min_adv:
                # 평균은 **관측된 창 전체**로 나눈다 - 거래일만으로 나누면
                # 한 달에 하루 거래되는 종목이 유동성 상위로 올라온다.
                denom = len(vals) or 1
                # 데이터는 백만원 단위, 파라미터는 원 단위다(NOTIONAL_UNIT_KRW).
                # 환산 없이 비교하면 1e6 배 차이로 전 종목이 탈락한다.
                if sum(traded) / denom < self._min_adv / NOTIONAL_UNIT_KRW:
                    continue
            out.append(s)
        return out

    # ── 조회면 ──────────────────────────────────────────────────────────────
    @property
    def until(self) -> date:
        return self._until

    @property
    def symbols(self) -> list[str]:
        """시그널이 보는 종목. 유동성 필터가 켜져 있으면 **체결 가능분만**."""
        return list(self._m.symbols if self._tradable is None else self._tradable)

    @property
    def universe_size(self) -> int:
        """필터 적용 후 남은 종목 수 - 필터가 너무 세면 여기서 드러난다."""
        return len(self.symbols)

    def n_days(self) -> int:
        """기준일까지 확보된 거래일 수."""
        return len(self._idx)

    def dates(self, n: int | None = None) -> list[date]:
        return list(self._idx if n is None else self._idx[-n:])

    def _untraded(self, d: date, symbol: str) -> bool:
        """그날 그 종목이 **거래되지 않았다**고 단정할 수 있는가.

        거래대금이 **있는데 0** 일 때만 참이다. 거래대금 열 자체가 없는
        데이터셋(옛 파티션·합성 시장)에서는 알 수 없고, **모르는 것을 무거래로
        치면 유니버스가 통째로 비어** 전 종목 미산출이 된다 - 그건 측정이
        아니라 사고다(`zero-fill` 사고와 같은 모양).

        실측(2026-08-14, krx-basket-daily-v3 2024-01~2026-08 234만행):
        `volume == 0` 인 봉의 **100.00%** 가 `notional == 0` 이다. 거래대금
        하나로 무거래를 정확히 가릴 수 있다는 뜻이라 별도 열이 필요없다.
        """
        nv = self._m.notionals.get((d, symbol))
        return nv is not None and float(nv) <= 0.0

    def closes(self, symbol: str, n: int) -> list[float]:
        """최근 n 거래일 종가(오름차순). **구멍이 있으면 짧게 돌려준다** -
        없는 값을 앞뒤로 채우면 그 자체가 지어낸 데이터다.

        ▶ **거래정지일 종가는 관측이 아니다** (2026-08-14 실측)
          거래정지일은 거래량·거래대금이 0 인데 종가는 전일이 이월된다. 그
          이월값을 관측으로 세면 수익률이 0 으로 **만들어지고**, 그 0 이
          표준편차를 깎아 정지가 잦은 종목이 '가장 안 흔들린 종목'이 된다.

          실측이 그대로 나왔다 - 저변동성 신호가 뽑은 20종목의 창 내 무거래율이
          **100.00%**(유니버스 5.46%, **18.3배**)였고, 그 20종목의 변동성은
          전부 정확히 **0.00000** 이었다. 실제로 거래된 날만으로 다시 재면
          같은 종목들이 일간 0.05~0.13 - 저변동성이 아니라 **최고 변동성**
          구간이다. 즉 이 신호는 저변동성이 아니라 **정지 종목**을 고르고
          있었다.

          그래서 무거래일은 여기서 빠진다. 창을 못 채운 종목은 각 지표가
          이미 None 을 내므로(변동성·수익률·ADV 공통 원칙), '못 잰 것'이
          '0 이었던 것'으로 둔갑하는 경로가 닫힌다. **미관측과 무변동을
          가르는 것이지, 값을 고치는 것이 아니다.**

        ▶ **미조정 액면분할·병합 앞에서 시계열을 끊는다** (2026-08-14 실측)
          일간 종가가 정확히 정수배로 뛴 곳이 21건(19종목) 있었다 - ×10.0000,
          ×5.0000, ×30.0000 같은 값은 급등이 아니라 **조정계수가 적용되지
          않은 것**이고, 수익률로 읽으면 +900%·+2900% 다. 모멘텀류 신호는
          그런 종목을 그 달의 1등으로 뽑는다.
          우리 원장에는 상장주식수 시계열도 corporate_actions 표도 없어
          CRSP 정석(facpr = s(t)/s(t′)−1)을 쓸 수 없다. 그래서 같은 표준이
          제시하는 차선을 쓴다 - **조정 이벤트가 미상인 갭이 끼면 그 구간
          값을 쓰지 않는다.** 자르는 것이지 고치는 것이 아니다: 창을 못 채운
          종목은 어차피 각 신호가 제외하므로(변동성·수익률 계산이 None),
          가짜 수익률이 순위에 들어오는 경로가 닫힌다.
        """
        out = []
        for d in self._idx[-n:]:
            if self._untraded(d, symbol):
                continue            # 이월 종가 - 관측이 아니다
            v = self._m.closes.get((d, symbol))
            if v is not None:
                out.append(float(v))
        cut = _adjustment_break(out)
        return out[cut:] if cut else out

    def notionals(self, symbol: str, n: int) -> list[float]:
        out = []
        for d in self._idx[-n:]:
            v = self._m.notionals.get((d, symbol))
            if v is not None:
                out.append(float(v))
        return out

    def micro(self, symbol: str, feature: str, n: int = 1) -> list[float]:
        """기준일까지의 미시구조 일별 피처. **없으면 빈 리스트**다.

        미시구조 커버리지(2026-05-18~)는 일봉(2016~)보다 훨씬 짧다. 없는
        구간을 0 으로 채우면 "호가가 없었다" 와 "불균형이 0이었다" 가 같아지고
        그 순간 신호가 오염된다 - 없으면 없는 채로 둔다.

        `quality_status` 는 문자열이라 여기서 걸러진다(수치 피처만 준다).
        상태를 보고 거를지는 신호가 `quality` 로 따로 묻는다.
        """
        out = []
        for d in self._idx[-n:]:
            m = getattr(self._m, "micro", {}).get((d, symbol))
            if not m:
                continue
            v = m.get(feature)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
        return out

    def micro_quality(self, symbol: str) -> str | None:
        """기준일의 미시구조 품질 상태(PASS/WARN). 없으면 None."""
        for d in reversed(self._idx):
            m = getattr(self._m, "micro", {}).get((d, symbol))
            if m:
                q = m.get("quality_status")
                return str(q) if q is not None else None
        return None

    # ── 파생 재료 (직접 구현하면 실수하기 쉬운 것들) ─────────────────────────
    def total_return(self, symbol: str, lookback: int) -> float | None:
        """lookback 거래일 전 대비 수익률. 표본이 모자라면 None.

        미조정 분할·병합이 창 안에 있으면 **None**이다(위 closes 주석 참고) -
        그 구간의 가격비는 수익이 아니라 단위 변경이라 비교가 성립하지 않는다.
        """
        if len(self._idx) < lookback + 1:
            return None
        d_now, d_then = self._idx[-1], self._idx[-1 - lookback]
        a = self._m.closes.get((d_then, symbol))
        b = self._m.closes.get((d_now, symbol))
        if a is None or b is None or a <= 0:
            return None
        # 창 안에 조정 점프가 있으면 계산하지 않는다. closes 가 잘라 준 시계열
        # 길이가 요청한 창을 못 채우는 것으로 드러난다(같은 판정을 두 번 쓰지
        # 않으려고 여기서도 그 함수를 그대로 쓴다).
        if len(self.closes(symbol, lookback + 1)) < lookback + 1:
            return None
        return float(b) / float(a) - 1.0

    def daily_returns(self, symbol: str, n: int) -> list[float]:
        px = self.closes(symbol, n + 1)
        return [px[i] / px[i - 1] - 1.0
                for i in range(1, len(px)) if px[i - 1] > 0]

    def returns_by_date(self, symbol: str, n: int) -> dict:
        """최근 n개 **수익률 날짜**의 일별 수익률을 날짜 키로 돌려준다.

        `daily_returns` 는 구멍을 건너뛰어 시계열을 압축하므로 종목 간 날짜
        정렬이 필요한 계산(시장수익률 회귀 등)에 쓸 수 없다. 여기서는 연속
        이틀 종가가 모두 있는 날짜에만 수익률을 내고, 없는 날짜는 **키 자체가
        없다**(0 으로 채우지 않는다 - 없는 값은 없는 값이다).
        """
        idx = self._idx
        out: dict = {}
        if len(idx) < 2:
            return out
        for i in range(max(1, len(idx) - n), len(idx)):
            if self._untraded(idx[i], symbol) or self._untraded(idx[i - 1], symbol):
                continue            # 이월 종가로 만든 0 은 수익률이 아니다
            a = self._m.closes.get((idx[i - 1], symbol))
            b = self._m.closes.get((idx[i], symbol))
            if a is not None and b is not None and float(a) > 0:
                out[idx[i]] = float(b) / float(a) - 1.0
        return out

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
    # ▶ **신호가 자기 데이터 요구를 선언한다** (2026-08-14)
    #   미시구조(호가·체결 일별 집계)는 일봉과 다른 데이터셋이라 따로 실어야
    #   한다. 가설이 `micro_dataset=...` 같은 키를 적게 하면 빠뜨린 가설이
    #   조용히 미시구조 없이 돌고, 그 성적은 그 가설의 증거가 아니다 -
    #   오늘 `trend_filter_days` 가 정확히 그렇게 무시됐다.
    #   신호를 고르는 순간 요구가 따라오게 두면 빠뜨릴 자리가 없다.
    needs_micro: bool = False


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


def _sig_max_daily_return(v: PITView, params: dict) -> dict:
    """MAX - 창 안의 **최대 일일수익률** (Bali-Cakici-Whitelaw 2011 의 신호).

    ▶ 실행면 확장 1호 (2026-08-13, 에이전트 수요 실측)
      리서치 에이전트가 복권선호(lottery-preference) 가설을 내며 파라미터에
      `"type": "low_max"` 를 직접 적었는데 실행면이 이 신호를 몰라 실현변동성
      (LOWVOL)으로 깎여 실행됐다 - **MAX 와 실현변동성은 다른 신호라 그
      판정은 MAX 가설의 판정이 아니었다.** 재료는 처음부터 있었다:
      `daily_returns` 에서 max 한 줄이다. 어휘는 TEMPLATES 에서 파생되므로
      이 등록으로 Gate 0 이 열린다.

      창을 다 못 채우면 제외한다 - 3일치의 max 를 "한 달 MAX" 라 부르면
      다른 지표가 된다(_sig_volatility 와 같은 원칙).
    """
    lb = _p(params, "lookback_days", 21)
    out = {}
    for s in v.symbols:
        r = v.daily_returns(s, lb)
        if len(r) >= lb:
            out[s] = max(r)
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


def _ols_r2(x_rows: list, y: list) -> float | None:
    """최소제곱 R². 정규방정식을 부분 피벗 소거로 푼다(외부 라이브러리 없음).

    특이(공선·상수 y)면 **None** 이다 - 0 이나 방어값으로 가리면 '못 잰 종목'이
    점수를 받고 순위에 앉는다. 못 재면 못 잰 것이다.
    """
    n, k = len(y), len(x_rows[0])
    if n <= k:
        return None
    ata = [[sum(x_rows[r][i] * x_rows[r][j] for r in range(n))
            for j in range(k)] for i in range(k)]
    aty = [sum(x_rows[r][i] * y[r] for r in range(n)) for i in range(k)]
    a = [ata[i][:] + [aty[i]] for i in range(k)]
    scale = max(abs(a[i][j]) for i in range(k) for j in range(k))
    if scale <= _EPS:
        return None
    tol = 1e-12 * scale
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) <= tol:
            return None                  # 공선 - 이 관측으로는 못 잰다
        a[c], a[p] = a[p], a[c]
        for r in range(k):
            if r != c:
                f = a[r][c] / a[c][c]
                for j in range(c, k + 1):
                    a[r][j] -= f * a[c][j]
    beta = [a[i][k] / a[i][i] for i in range(k)]
    mu = sum(y) / n
    sst = sum((v - mu) ** 2 for v in y)
    if sst <= _EPS:
        return None                      # 움직이지 않은 종목은 지연을 잴 수 없다
    sse = sum((y[r] - sum(beta[j] * x_rows[r][j] for j in range(k))) ** 2
              for r in range(n))
    return 1.0 - sse / sst


def _sig_price_delay(v: PITView, params: dict) -> dict:
    """Hou-Moskowitz(2005) 가격지연 D1. 높을수록 시장 정보 반영이 느리다.

    ▶ 실행면 확장 2호 (2026-08-14, 카드 t_2334a259)
      리서치의 가격지연 가설(667f0a45)이 EDGE_KEYS 로는 mean_reversion +
      horizon_days=60 으로밖에 번역되지 않았고, 독립 회의론자가 그 번역을
      기각했다(t_946e5acc): 그 실험의 통과는 60일 낙폭주 반전의 증거이지
      가격지연의 증거가 아니다 - **번역이 가설을 파괴한다.** 지연은 수익률
      수준이 아니라 **시차 시장수익률 회귀**로만 잰다.

    ▶ 측정
      각 종목의 일별 수익률을 동시(t) + 시차(t-1..t-L) 시장수익률에 회귀:
        D1 = 1 - R²(동시항만) / R²(동시+시차)
      시차항이 설명을 보태는 만큼 D1 이 1 에 가깝다(정보 반영 지연).
      시장수익률은 유니버스 동일가중 평균으로 근사한다 - PITView 에 지수
      시계열이 없고, 유니버스가 곧 실험의 시장 정의다(krx_all 이면 전 종목
      평균). 해당 날짜에 수익률 있는 종목이 2개 미만이면 그 날짜는 시장을
      정의하지 않는다. 소형 유니버스에선 자기 자신이 시장 평균에 섞이는
      비중이 커진다 - 이는 지연을 **낮추는**(보수적) 방향의 편향이다.

    ▶ 정직성 (다른 템플릿과 같은 원칙)
      형성창 lookback_days(기본 60) 관측을 **다 채우지 못한 종목은 뺀다** -
      부분 창의 D1 은 다른 지표다. 회귀가 특이하거나 R² 가 0 이면 뺀다.
      점수는 [0, 1] 안에 있다(제약모형 R² ≤ 비제약모형 R², 내포 관계).
    """
    lb = _p(params, "lookback_days", 60)
    lags = max(1, _p(params, "market_lags", 5))
    if v.n_days() < lb + lags + 1:
        return {}
    rdates = v.dates(lb + lags)          # 수익률이 정의될 수 있는 날짜들
    rets = {s: v.returns_by_date(s, lb + lags) for s in v.symbols}
    mkt: dict = {}
    for d in rdates:
        vals = [r[d] for r in rets.values() if d in r]
        if len(vals) >= 2:
            mkt[d] = sum(vals) / len(vals)
    out = {}
    for s in v.symbols:
        rs = rets[s]
        rows_u, rows_r, ys = [], [], []
        for i in range(lags, lb + lags):
            d = rdates[i]
            if d not in rs:
                continue
            m_lagged = []
            for k in range(lags + 1):
                dk = rdates[i - k]
                if dk not in mkt:
                    break
                m_lagged.append(mkt[dk])
            if len(m_lagged) < lags + 1:
                continue
            ys.append(rs[d])
            rows_u.append([1.0] + m_lagged)
            rows_r.append([1.0, m_lagged[0]])
        if len(ys) < lb:                 # 창을 다 채우지 못하면 다른 지표다
            continue
        r2u = _ols_r2(rows_u, ys)
        if r2u is None or r2u <= _EPS:
            continue                     # 시장이 아무것도 설명 못하면 D1 미정의
        r2r = _ols_r2(rows_r, ys)
        if r2r is None:
            continue
        out[s] = 1.0 - max(0.0, min(r2r, r2u)) / r2u
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


def _zrank(scores: dict) -> dict:
    """점수 -> [-1, 1] 균등 랭크. 동률은 심볼 순(결정론).

    단위가 다른 신호를 더하려면 공통 척도가 필요하다. 랭크를 쓰는 이유는
    이상치에 둔감하고, 신호마다 다른 분포 모양이 결합 가중을 조용히 바꾸는
    것을 막기 때문이다.
    """
    if len(scores) < 2:
        return {s: 0.0 for s in scores}
    order = sorted(scores, key=lambda s: (scores[s], s))
    n = len(order)
    return {s: 2.0 * i / (n - 1) - 1.0 for i, s in enumerate(order)}


# 결합에서 제외할 template_id. 자기 자신을 재귀로 부르지 않는다.
_COMPOSITE_IDS = frozenset({"COMBO"})


def _sig_composite(v: PITView, params: dict) -> dict:
    """등재 신호 전부를 **선호 방향으로 통일해** z-랭크 평균 = 완제품(mega-alpha v1).

    ▶ 왜 이 템플릿이 필요한가 (2026-08-14, 2층 관문 결재)
      IR = TC × IC_Adj × √BR (Grinold-Kahn 완전형)에서 IR 을 키우는 항은
      **결합의 √BR** 이다. 실측으로 부품(단일 신호)의 천장이 확인됐다 -
      illiq × top200 이 DSR 0.997 · CI · MDD 를 다 통과하고도 IR 0.17.
      개별 가설에 IR 0.5 를 요구하는 것은 결합 후에만 도달 가능한 배수를
      부품에 소급 요구하는 범주 오류였고(TWO_TIER_GATE_PROPOSAL.md),
      이 템플릿이 그 요구를 받을 **완제품**이다.

    ▶ 동일가중이다 - 하이퍼파라미터가 없다
      IC 비례 가중이 이론상 낫지만 그 가중은 결과를 보고 정하는 값이라
      사전등록을 무력화한다. 동일가중은 정할 것이 없어 코드 해시만으로
      완전히 고정된다(DeMiguel 이 1/N 을 최난 벤치마크로 만든 그 성질).
      가중 최적화는 별도 사전등록 실험의 몫이다.

    ▶ 표본 부족이면 침묵하고, 조건 미충족이면 그 부품만 뺀다
      표본이 어떤 부품의 준비 기간에 못 미치면 결합은 통째로 침묵한다 -
      일부만으로 만든 값을 "전 신호 평균" 이라 부르면 min_history 로 한 약속이
      거짓이 된다. 반대로 표본은 충분한데 조건부 신호(유동성 충격 반전)가
      그날 해당 종목을 못 찾은 것은 정보가 없는 것이므로 그 부품만 제외한다.

    ▶ 중복 신호는 결합해도 BR 을 못 늘린다
      유효 BR = N/(1+(N−1)ρ). 실측 상관 low_vol×low_max = 0.914,
      momentum×mean_reversion = −1.0 이라 우리 서가 9종은 실질 2.5개다.
      그래서 이 템플릿의 성적이 부품을 못 넘으면 그것은 결합의 실패가
      아니라 **서가가 좁다는 증거**다 - 판정문에 그렇게 적어야 한다.
    """
    # ▶ **표본 부족과 조건 미충족을 가른다** (2026-08-14, 자체점검이 잡음)
    #   ① 표본이 부품의 준비 기간에 못 미치면 → 결합 무효(빈 dict). 그때
    #      점수를 내면 "전 신호 평균" 이라 선언한 것이 일부 신호의 평균이 되고,
    #      min_history 로 한 약속이 거짓이 된다.
    #   ② 표본은 충분한데 조건부 신호(유동성 충격 반전 등)가 그날 해당 종목을
    #      못 찾은 것은 **정보가 없는 것**이지 오류가 아니다 - 그 부품만 빼고
    #      결합한다. 이 둘을 안 가르면 조건부 신호 하나가 결합 전체를 상시
    #      침묵시킨다.
    have = v.n_days()
    parts, out = [], {}
    for t in TEMPLATES.values():
        if t.template_id in _COMPOSITE_IDS:
            continue
        if have < t.min_history(params):
            return {}                      # ① 표본 부족 - 결합 자체가 무효
        try:
            raw = t.signal(v, params)
        except Exception:  # noqa: BLE001 - 계산 실패는 조용히 넘기지 않는다
            return {}
        if len(raw) < 2:
            continue                       # ② 그날 정보 없음 - 이 부품만 제외
        # BOTTOM 랭크 신호는 "낮을수록 산다" 이므로 부호를 뒤집어 방향을 통일한다.
        # 이걸 안 하면 저변동과 모멘텀이 서로를 상쇄해 결합이 잡음이 된다.
        pref = {s: (-x if t.rank == "BOTTOM" else x) for s, x in raw.items()}
        parts.append(_zrank(pref))
    if not parts:
        return {}
    # 모든 구성 신호가 점수를 낸 종목만 결합한다. 일부만 있는 종목을 평균에
    # 넣으면 신호 개수가 종목마다 달라져 비교가 무의미해진다(0 으로 채우는
    # 것과 같은 사고).
    common = set(parts[0])
    for p in parts[1:]:
        common &= set(p)
    for s in common:
        out[s] = sum(p[s] for p in parts) / len(parts)
    return out


def _composite_warmup(p: dict) -> int:
    """구성 신호 중 가장 긴 준비 기간. 하나라도 못 채우면 결합이 성립하지 않는다."""
    need = [t.min_history(p) for t in TEMPLATES.values()
            if t.template_id not in _COMPOSITE_IDS]
    return max(need) if need else 1


def _sig_order_flow(v, params: dict) -> dict[str, float]:
    """**호가 주문흐름 불균형(OFI)**. 매수 압력이 단기 수익을 예측한다.

    ▶ 근거
      Cont-Kukanov-Stoikov(2014): 최우선 호가의 주문흐름 불균형이 단기 가격
      변화와 선형에 가까운 관계를 갖는다. 거래량이나 체결 불균형보다 **호가
      갱신에서 오는 압력**이 설명력이 크다는 것이 그 논문의 요지다.

    ▶ 우리 구현
      `krx-microstructure-daily` 가 하루 단위로 이미 집계한
      `order_flow_imbalance` 를 쓴다. 일별 값은 잡음이 크므로 `micro_window_days`
      만큼 평균할 수 있게 열어 둔다(기본 5일) - 며칠을 볼지는 가설이 정한다.

    ▶ 없으면 안 만든다
      미시구조 커버리지(2026-05-18~)는 일봉보다 훨씬 짧다. 값이 없는 종목은
      **점수를 내지 않는다** - 0 으로 채우면 "호가가 없었다" 가 "압력이
      중립이었다" 로 둔갑해 그 종목이 순위 중앙에 조용히 들어온다.
    """
    win = _p(params, "micro_window_days", 5)
    out: dict[str, float] = {}
    for s in v.symbols:
        vals = v.micro(s, "order_flow_imbalance", win)
        if not vals:
            continue
        out[s] = sum(vals) / len(vals)
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
        # 실행면 확장 1호 (2026-08-13): 에이전트가 요청한 신호를 원형 그대로.
        # BOTTOM = 저MAX 매수(복권형 회피 가설). 역방향이면 IC 검정이 부호와
        # 함께 말해 준다 - 고MAX 쪽 재도전은 새 사전등록으로.
        Template("LOWMAX", "low_max", "BOTTOM", _sig_max_daily_return,
                 lambda p: _p(p, "lookback_days", 21) + 1,
                 "복권형 수익(높은 MAX)을 좇는 수요가 그 종목을 과대평가한다"
                 " - 저MAX 가 이후 수익에서 앞선다",
                 "창 내 최대 일일수익률 하위 균등"),
        # 실행면 확장 2호 (2026-08-14, 카드 t_2334a259): 시차 시장수익률 회귀.
        # TOP = 고지연 매수(Hou-Moskowitz: 지연 상위 분위가 프리미엄을 받는다).
        # 형성창은 EDGE_KEYS 의 horizon_days -> lookback_days 재사용, 시차 수
        # (market_lags=5)는 템플릿 상수다 - 튜닝 손잡이가 아니라 측정 정의의
        # 일부이고, 사전등록 코드 해시에 함께 각인된다.
        Template("PDELAY", "price_delay", "TOP", _sig_price_delay,
                 lambda p: (_p(p, "lookback_days", 60)
                            + max(1, _p(p, "market_lags", 5)) + 1),
                 "시장 정보가 가격에 늦게 반영되는(고지연) 종목은 그 마찰의 "
                 "대가로 프리미엄을 받는다",
                 "시차 시장회귀 D1 상위 균등"),
        # 완제품 층 (2026-08-14, 2층 관문 결재). 위 신호들이 부품이고 이것이
        # 그 부품들을 합친 하나의 북이다 - IR 0.5 관문은 이 층이 받는다.
        Template("COMBO", "signal_composite", "TOP", _sig_composite,
                 _composite_warmup,
                 "약한 신호도 서로 독립이면 결합이 IR 을 √BR 배 키운다"
                 " - 부품이 아니라 결합된 북이 알파다(Grinold-Kahn)",
                 "전 신호 방향통일 z-랭크 평균 상위 균등"),
        # ▶ **첫 미시구조 신호** (2026-08-14). 지금까지 어휘 13종이 전부
        #   가격·거래량 파생이었고, 호가·체결은 원장에 쌓여만 있었다
        #   (원본 3일치 4,600만 행 · 일별 집계 61거래일 × 2,489종목).
        #   커버리지가 짧아 백테스트 관문은 어렵지만 횡단면 IC 는 잰다.
        Template("OFI", "order_flow_imbalance", "TOP", _sig_order_flow,
                 lambda p: _p(p, "micro_window_days", 5),
                 "최우선 호가의 주문흐름 불균형이 단기 가격 변화를 예측한다"
                 " - 체결량이 아니라 호가 갱신 압력이 설명력을 갖는다"
                 "(Cont-Kukanov-Stoikov 2014)",
                 "OFI N일 평균 상위 균등",
                 needs_micro=True),
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


def pit_view_for(market: MarketLike, until: date, params: dict) -> PITView:
    """config/params 의 유동성 손잡이를 반영한 PITView. **한 곳에서만 만든다.**

    시그널 실행(백테스트)과 IC 측정이 서로 다른 유니버스를 보면 두 숫자가
    같은 대상을 말하지 않는다 - 그래서 생성을 여기로 모은다.
    손잡이가 없으면 예전과 완전히 동일하게 동작한다(기본값 0 = 필터 없음).
    """
    return PITView(
        market, until,
        min_adv_krw=float(params.get("min_adv_krw") or 0.0),
        min_trading_days=int(params.get("min_trading_days") or 0),
        liquidity_window=int(params.get("liquidity_window") or 60))


def signal_scores(market: MarketLike, until: date, *, template: Template,
                  params: dict) -> dict:
    """템플릿 시그널 실행. **Market 이 아니라 PITView 를 넘긴다.**"""
    return template.signal(pit_view_for(market, until, params), params)


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


def _check_widening_the_vocab_widens_the_theme_table():
    """**어휘를 넓히면 테마 표도 같이 넓어져야 한다** (카드 t_fa233e6b 실측).

    `signal_composite`(2026-08-14 완제품 층)와 `order_flow_imbalance`(같은 날
    첫 미시구조 신호)를 여기 어휘에 넣으면서 `trial_family.THEMES` 에는 안
    넣었다. **한쪽만 넓힌 것**이고, 결과로 이웃 모듈의 자체 점검이 계속
    빨간불이었다 - 항상 터지는 검사는 신호가 아니라 소음이 되고, 그 옆에서
    새 결함이 그냥 지나간다.

    검사는 `trial_family` 쪽에도 있다(`_check_theme_is_a_lens_not_identity`).
    없어서 못 잡은 게 아니라 **어휘를 넓히는 사람이 그 파일을 열지 않아서**
    못 봤다. 어휘를 소유한 이 모듈에서도 같이 터뜨려야 손이 닿는 자리에서
    막힌다 - `config_binding` 이 `EDGE_KEYS` 옆에 착지 탐침 표를 두고 같이
    검사하는 것과 같은 형태다.

    테마는 조회 층이지 정체성이 아니므로(계열 해시에 안 들어간다) 여기서
    강제해도 family_id 는 흔들리지 않는다.
    """
    from trial_family import THEMES, theme_of   # noqa: PLC0415 (테마는 조회층)

    missing = sorted(e for e in EDGE_VOCAB if theme_of(e) == "미분류")
    assert not missing, (
        f"어휘에 넣고 테마를 안 적은 edge_type: {missing} - `trial_family.THEMES` "
        f"에 함께 넣어라. 테마가 없으면 그 계열이 파레토·테마별 π₀ 집계에서 "
        f"'미분류' 한 칸에 무관한 유형들과 뭉뚱그려져 **자기 사전확률을 못 "
        f"갖는다**(TreeBH·GBH·Bühlmann 이 전부 이 층을 연료로 쓴다)")
    # 반대 방향: 어휘에서 사라진 유형의 테마가 남아 있으면 이름 변경을 놓친 것이다.
    # 미구현 유형은 예외 - 실행면에 열기 전에 테마를 미리 적어 둘 수 있다.
    stale = sorted(set(THEMES) - set(EDGE_VOCAB) - set(NOT_IMPLEMENTED))
    assert not stale, (
        f"어휘에 없는 edge_type 의 테마가 남아 있다: {stale} - 이름을 바꿨다면 "
        f"표도 같이 바꿔라(옛 이름이 남으면 집계가 조용히 빈 칸을 만든다)")


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


def _check_price_delay_pit_and_bounds():
    """PDELAY: 미래 불가시 + 점수는 [0,1] + min_history 경계에서 실제 산출.

    기본 min_history 66일이라 공용 PIT 점검(_mk(60))에서는 빈 결과끼리 비교돼
    통과가 공허하다 - 여기서 점수가 **실제로 나오는** 길이로 다시 확인한다.
    """
    short, long = _mk(70), _mk(90)
    until = short.dates[-1]
    t = TEMPLATES["PDELAY"]
    a = signal_scores(short, until, template=t, params={})
    b = signal_scores(long, until, template=t, params={})
    assert a, "70일 시장인데 PDELAY 점수가 없다"
    assert a == b, "미래 데이터가 과거 지연 점수를 바꿨다"
    assert all(0.0 <= x <= 1.0 for x in a.values()), a
    # 경계: 정확히 min_history 일이면 산출, 하루 모자라면 빈 결과
    need = t.min_history({})
    assert signal_scores(_mk(need), _mk(need).dates[-1], template=t, params={})
    assert signal_scores(_mk(need - 1), _mk(need - 1).dates[-1],
                         template=t, params={}) == {}


def _check_price_delay_ranks_lagged_response_not_return_level():
    """**지연 측정이 수익률 수준 측정과 다름을 실물로 증명한다.**

    회의론자 판정(t_946e5acc)의 핵심: mean_reversion 번역은 '많이 빠진 종목'을
    고를 뿐 '늦게 반응하는 종목'을 고르지 못한다. 여기서는 시장 신호를 3일
    늦게 따라가는 종목(LAG3)을 심어 두고, PDELAY 가 그 종목을 동시 반응
    종목들보다 위에 올리는지 본다 - 수익률 수준 시그널로는 불가능한 순위다.
    """
    from datetime import timedelta

    class _M:
        pass

    m = _M()
    d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(80)]
    m.symbols = ["S1", "S2", "S3", "S4", "LAG3"]
    m.closes, m.notionals = {}, {}

    def drv(i: int) -> float:            # 결정적 유사난수 시장 신호
        return ((i * i * 31 + i * 17) % 97 - 48) / 5000.0

    px = {s: 100.0 for s in m.symbols}
    for i, d in enumerate(m.dates):
        for si, s in enumerate(m.symbols):
            r = drv(i - 3) if s == "LAG3" else drv(i) + si * 1e-4
            px[s] *= 1.0 + r
            m.closes[(d, s)] = px[s]
            m.notionals[(d, s)] = 1e9
    sc = signal_scores(m, m.dates[-1], template=TEMPLATES["PDELAY"], params={})
    assert "LAG3" in sc and all(s in sc for s in ("S1", "S2", "S3", "S4")), sc
    sync_max = max(sc[s] for s in ("S1", "S2", "S3", "S4"))
    assert sc["LAG3"] > sync_max, (
        f"지연 종목이 동시 반응 종목보다 낮다: LAG3={sc['LAG3']:.4f} "
        f"vs sync_max={sync_max:.4f}")


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


def _check_unadjusted_split_does_not_become_a_return():
    """**미조정 분할·병합이 수익률로 둔갑하지 않는다** (2026-08-14 실측 21건).

    ×10.0000 같은 정수배 점프는 급등이 아니라 조정계수 미적용이다. 그대로
    두면 모멘텀류가 그 종목을 그 달 1등으로 뽑고, 그 성적은 전략의 증거가
    아니라 데이터 결함의 증거가 된다.
    """
    from datetime import timedelta

    class _M: pass
    m = _M(); d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(60)]
    m.symbols = ["SPLIT10", "REAL", "SURGE"]
    m.closes, m.notionals, m.opens = {}, {}, {}
    for i, d in enumerate(m.dates):
        # ▶ 점프 **직전 대비** 정확히 정수배여야 실측과 같은 모양이다
        #   (실측 원문: 224 -> 2240 = ×10.0000). 기저가 매일 움직이는 채로
        #   전체에 배수를 곱하면 ×10.04 가 되어 실제 사고와 다른 픽스처가 된다.
        m.closes[(d, "SPLIT10")] = (200.0 + i) if i < 40 else (239.0 * 10.0 + (i - 40))
        m.closes[(d, "REAL")] = 1000.0 + i * 3.0          # 완만한 상승
        # SURGE: 40일째에 ×2.37 (진짜 급등 - 배수에서 벗어난다)
        m.closes[(d, "SURGE")] = (500.0 + i) if i < 40 else (539.0 * 2.37 + (i - 40))
        for s in m.symbols:
            m.notionals[(d, s)] = 1e9

    v = PITView(m, m.dates[-1])
    # 조정 점프가 창 안에 있으면 그 종목의 장기 수익률은 계산되지 않는다
    assert v.total_return("SPLIT10", 30) is None, v.total_return("SPLIT10", 30)
    # 진짜 급등과 정상 종목은 그대로 산출된다(과잉 차단 금지)
    assert v.total_return("SURGE", 30) is not None
    assert v.total_return("REAL", 30) is not None
    # 점프 이후 구간만으로 짧게 보는 것은 가능하다 - 자르는 것이지 지우는 게 아니다
    assert v.total_return("SPLIT10", 10) is not None
    # 모멘텀 신호가 조정 종목을 1등으로 뽑지 않는다
    sc = signal_scores(m, m.dates[-1], template=TEMPLATES["MOM"],
                       params={"lookback_days": 30})
    assert "SPLIT10" not in sc, sc
    assert _adjustment_break([100.0, 1000.0]) == 1
    assert _adjustment_break([100.0, 237.0]) == 0, "진짜 급등을 조정으로 오인했다"
    print("  미조정 분할 방어선        OK")


def _check_halted_days_are_not_zero_returns():
    """**거래정지일을 수익률 0 으로 세면 정지 종목이 저변동성 1등이 된다**
    (2026-08-14 실측).

    원장 census 가 센 것: 일봉 697만개 중 거래량 0 이 16.7만개(2.4%). 종가는
    이월되므로 수익률 0 으로 계산되고 변동성이 깎인다.

    실행면에서 재보니 census 보다 나빴다 - 저변동성 신호가 뽑은 20종목의 창 내
    무거래율이 **100.00%**(유니버스 5.46%, 18.3배), 변동성은 전부 정확히
    0.00000. 거래된 날만으로 다시 재면 0.05~0.13 로 **최고 변동성** 구간이다.

    여기서 고정하는 것은 세 가지다:
      · 창 내내 거래가 없던 종목은 변동성 **점수를 받지 못한다**(0 이 아니다)
      · 이월 종가에서 수익률 0.0 을 만들어 내지 않는다
      · 매일 거래된 종목들 사이의 판정은 하나도 바뀌지 않는다(과잉 차단 금지)
    """
    from datetime import timedelta

    class _M: pass
    m = _M(); d0 = date(2026, 1, 5)
    m.dates = [d0 + timedelta(days=i) for i in range(40)]
    m.symbols = ["CALM", "HALTED", "WILD"]
    m.closes, m.notionals, m.opens = {}, {}, {}
    for i, d in enumerate(m.dates):
        m.closes[(d, "CALM")] = 1000.0 * (1.0 + 0.002 * (1 if i % 2 else -1))
        m.closes[(d, "WILD")] = 1000.0 * (1.0 + 0.05 * (1 if i % 2 else -1))
        m.notionals[(d, "CALM")] = m.notionals[(d, "WILD")] = 1e9
        if i < 5:                      # 5일만 거래되고 이후 정지
            m.closes[(d, "HALTED")] = 1000.0 * (1.0 + 0.08 * (1 if i % 2 else -1))
            m.notionals[(d, "HALTED")] = 1e9
        else:
            m.closes[(d, "HALTED")] = m.closes[(m.dates[4], "HALTED")]  # 이월
            m.notionals[(d, "HALTED")] = 0.0                            # 무거래

    v = PITView(m, m.dates[-1])
    sc = _sig_volatility(v, {"lookback_days": 20})
    assert "HALTED" not in sc, (
        f"정지 종목이 변동성 점수를 받았다: {sc.get('HALTED')} - 무거래일을 "
        f"수익률 0 으로 세었다")
    assert sc and min(sc, key=sc.get) != "HALTED", sc
    r = v.daily_returns("HALTED", 20)
    assert not any(x == 0.0 for x in r), (
        f"이월 종가에서 수익률 0.0 을 만들어 냈다: {r}")
    assert v.total_return("HALTED", 20) is None, v.total_return("HALTED", 20)
    assert v.returns_by_date("HALTED", 20) == {}, v.returns_by_date("HALTED", 20)

    # 과잉 차단 금지 - 매일 거래된 종목은 그대로 산출되고 순서도 그대로다
    assert "CALM" in sc and "WILD" in sc, sc
    assert sc["CALM"] < sc["WILD"], sc
    # 거래대금 열이 아예 없는 시장에서는 아무것도 안 바뀐다(모르면 안 거른다)
    m.notionals = {}
    v2 = PITView(m, m.dates[-1])
    assert "HALTED" in _sig_volatility(v2, {"lookback_days": 20}), \
        "거래대금을 모르는데 무거래로 단정했다 - 유니버스가 비는 경로다"
    print("  거래정지 != 무변동         OK")


def _check_liquidity_filter_removes_untradable_names():
    """**살 수 없는 종목은 신호가 보지 못한다** (2026-08-14 실측).

    비유동성 프리미엄 신호가 뽑은 종목의 일평균 거래대금 중앙값이 0원,
    60일 창 무거래일이 평균 57.2일이었다 - 그 위에서 나온 IR 은 백테스트에서만
    존재한다. 필터는 기본 꺼짐이지만, 켰을 때 실제로 거르는지 여기서 고정한다.
    """
    from datetime import timedelta

    class _M: pass
    m = _M(); d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(80)]
    m.symbols = ["LIQUID", "GHOST", "THIN"]
    m.closes, m.notionals, m.opens = {}, {}, {}
    for i, d in enumerate(m.dates):
        for s in m.symbols:
            m.closes[(d, s)] = 1000.0 + i
        # ▶ 값은 **백만원 단위**다 - 실물 데이터와 같은 단위여야 한다.
        #   원 단위(5e8)로 두었더니 필터가 1e6 배 어긋난 것을 못 잡았다.
        m.notionals[(d, "LIQUID")] = 500.0          # 매일 5억원
        # GHOST: 60일 중 2일만 거래(실측 유령 종목 구도)
        m.notionals[(d, "GHOST")] = 3.0 if i % 30 == 0 else 0.0   # 3백만원
        m.notionals[(d, "THIN")] = 20.0             # 매일 2천만원

    # 필터 꺼짐(기본) - 예전과 동일하게 전 종목이 보인다
    assert PITView(m, m.dates[-1]).universe_size == 3

    # 거래일수 기준: 60일 창에서 40일 이상 거래된 종목만
    v_days = PITView(m, m.dates[-1], min_trading_days=40, liquidity_window=60)
    assert "GHOST" not in v_days.symbols, v_days.symbols
    assert {"LIQUID", "THIN"} <= set(v_days.symbols), v_days.symbols

    # 거래대금 기준: 일평균 1억 이상
    v_adv = PITView(m, m.dates[-1], min_adv_krw=1e8, liquidity_window=60)
    assert v_adv.symbols == ["LIQUID"], v_adv.symbols

    # ▶ 평균은 **창 전체**로 나눈다 - 거래일만으로 나누면 한 달에 하루
    #   3백만원 거래되는 GHOST 가 "일평균 300만원" 으로 살아남는다.
    v_mix = PITView(m, m.dates[-1], min_adv_krw=1e6, liquidity_window=60)
    assert "GHOST" not in v_mix.symbols, \
        f"창 전체 평균이 아니라 거래일 평균을 썼다: {v_mix.symbols}"

    # 시그널이 필터를 실제로 통과받는가(신호는 v.symbols 만 본다)
    sig = signal_scores(m, m.dates[-1], template=TEMPLATES["ILLIQ"],
                        params={"adv_window": 60, "min_adv_krw": 1e8})
    assert set(sig) <= {"LIQUID"}, sig

    # ▶ **비용 모델과 같은 단위인가** (2026-08-14)
    #   필터와 비용이 다른 단위를 쓰면 한쪽은 유령 종목을 거르고 다른 쪽은
    #   그 종목에 대형주 슬리피지를 매긴다. 최저 유동성 계층 상한(3,000)이
    #   30억원이라는 해석을 여기서 못박는다 - 둘 중 하나만 바뀌면 깨진다.
    from backtest_runner import LIQUIDITY_TIERS  # noqa: PLC0415

    assert 3_000_000_000 / NOTIONAL_UNIT_KRW == LIQUIDITY_TIERS[0][0], \
        "유동성 필터와 비용 계층의 거래대금 단위가 어긋났다"

    # 필터를 켠 유니버스가 통째로 비면 그건 필터가 아니라 사고다. 실측:
    # 단위 환산 없이 1억을 비교해 전 종목이 사라졌고 실험은 빈 유니버스를 봤다.
    assert PITView(m, m.dates[-1], min_adv_krw=1e8,
                   liquidity_window=60).universe_size > 0, \
        "체결가능 유니버스가 0 이다 - 단위가 어긋났을 때 나오는 증상이다"
    print("  체결 가능 유니버스 필터   OK")


def _check_composite_unifies_direction_and_is_deterministic():
    """**결합은 방향을 통일하고, 부품 하나가 죽어도 성립한다.** (2026-08-14)

    BOTTOM 신호(저변동·저MAX·비유동)를 부호 반전 없이 더하면 TOP 신호와
    서로를 상쇄해 결합이 잡음이 된다 - 완제품 층 전체가 무의미해지는 사고라
    여기서 고정한다. 재귀(자기 자신 포함)와 부분 종목 평균도 함께 막는다.
    """
    from datetime import timedelta

    class _M: pass
    m = _M(); d0 = date(2025, 1, 1)
    m.dates = [d0 + timedelta(days=i) for i in range(160)]
    m.symbols = ["CALM", "WILD", "MID"]
    m.closes, m.notionals, m.opens = {}, {}, {}
    for i, d in enumerate(m.dates):
        # CALM: 완만한 상승(저변동·저MAX) / WILD: 큰 진폭 / MID: 중간
        m.closes[(d, "CALM")] = 100.0 + i * 0.10
        m.closes[(d, "WILD")] = 100.0 + (i % 7) * 6.0
        m.closes[(d, "MID")] = 100.0 + i * 0.10 + (i % 5) * 1.0
        m.notionals[(d, "CALM")] = 1e8
        m.notionals[(d, "WILD")] = 9e9
        m.notionals[(d, "MID")] = 1e9

    v = PITView(m, m.dates[-1])
    out = _sig_composite(v, {})
    assert out, "결합이 아무 점수도 내지 않았다"
    # 저변동·비유동인 CALM 이 고변동·고유동인 WILD 보다 높아야 방향 통일이 된 것
    assert out["CALM"] > out["WILD"], out
    # 결정론: 같은 입력 두 번이면 같은 값
    assert out == _sig_composite(PITView(m, m.dates[-1]), {}), "결합이 비결정적이다"
    # 재귀 금지: COMBO 가 자기 자신을 부품으로 세면 무한 재귀다
    assert "COMBO" in _COMPOSITE_IDS and TEMPLATES["COMBO"].edge_type == "signal_composite"
    # 완제품도 어휘에 있어야 접수·발주가 된다
    assert "signal_composite" in EDGE_VOCAB, sorted(EDGE_VOCAB)
    # 준비 기간은 부품 중 최댓값 - 짧게 잡으면 일부 신호가 빠진 채 결합된다
    assert _composite_warmup({}) >= max(
        t.min_history({}) for t in TEMPLATES.values() if t.template_id != "COMBO")
    print("  결합: 방향통일·결정론      OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음) - 템플릿 {len(TEMPLATES)}종")
    _check_unadjusted_split_does_not_become_a_return()
    _check_halted_days_are_not_zero_returns()
    _check_liquidity_filter_removes_untradable_names()
    _check_composite_unifies_direction_and_is_deterministic()
    _check_pit_view_cannot_see_future();      print("  PIT: 미래 불가시           OK")
    _check_deterministic();                   print("  결정론                     OK")
    _check_insufficient_history_is_empty_not_zero()
    print("  표본 부족 -> 제외(0 아님)   OK")
    _check_missing_notional_does_not_fabricate()
    print("  거래대금 없음 -> 미산출     OK")
    _check_rank_directions_are_opposite();    print("  TOP/BOTTOM 반대 방향       OK")
    _check_edge_vocab_is_unique_and_covers_templates()
    print("  통제 어휘 유일·미구현 분리  OK")
    _check_widening_the_vocab_widens_the_theme_table()
    print("  어휘 넓히면 테마도 넓힌다   OK")
    _check_resolve_accepts_parameterised_ids()
    print("  동적 ID 해석(실측 버그)     OK")
    _check_matches_legacy_momentum();         print("  기존 모멘텀과 값 동일       OK")
    _check_min_history_is_honest();           print("  min_history 정직            OK")
    _check_zero_volatility_is_excluded_not_defaulted()
    print("  변동성 0 제외(무한대 방지)  OK")
    _check_price_delay_pit_and_bounds()
    print("  PDELAY: PIT·경계·[0,1]      OK")
    _check_price_delay_ranks_lagged_response_not_return_level()
    print("  PDELAY: 지연≠수익률수준     OK")
    print(f"전략 템플릿 12개 영역 통과. 어휘: {sorted(EDGE_VOCAB)}")
