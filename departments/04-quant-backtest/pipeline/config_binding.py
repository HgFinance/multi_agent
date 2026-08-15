"""가설 -> 백테스트 config 바인딩 - 가설이 실제로 실험에 반영되게 한다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 2026-08-04 실측 - 새 가설을 등록해도 "중복 실험" 이 났다

▶ 무엇이 문제였나
  전략 카탈로그가 **edge type -> 고정 config** 로만 매핑했다. 즉
  `mean_reversion` 이면 가설이 무엇이든 REV_CONFIG 하나로 돌았다.
  input_hash 가 (데이터셋 + config + 코드 + seed) 이므로 **가설이 달라도
  같은 실험**이 되고, 중복 가드가 두 번째부터 전부 막았다.

  이건 단순 불편이 아니다. 세 가지가 동시에 무력해진다:
    · QNT-01 이 매번 다른 가설을 내도 실행부는 같은 것만 돌린다
    · **사전등록 지문에 holding_horizon 을 넣어도 실제 백테스트가 그 값을
      안 쓰면, 고정한 것과 실행한 것이 다르다** - 관문이 형식만 남는다
    · trial_pressure 가 세는 "변형 시도" 가 실제 변형이 아니다

▶ 무엇을 바인딩하고 무엇을 안 하는가
  한다   : 가설이 명시한 파라미터(지평·유니버스 크기·리밸런싱)를 config 로
  안 한다: 가설에 없는 값을 지어내기. 없으면 카탈로그 기본값을 쓰고
           **무엇이 기본값이었는지 남긴다** - 어디까지가 가설이고 어디부터가
           관례인지 구분되지 않으면 결과를 해석할 수 없다.

▶ 범위를 벗어난 값은 거부한다
  LLM 이 낸 가설이므로 holding_horizon=9999 같은 값이 올 수 있다. 잘라서
  쓰지 않고 거부한다 - 조용히 자르면 사전등록 지문과 실행 config 가 달라져
  같은 문제가 다시 생긴다.

자체 점검: python departments/04-quant-backtest/pipeline/config_binding.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from strategy_templates import (          # noqa: E402  (같은 디렉터리 모듈)
    EDGE_VOCAB,
    NOT_IMPLEMENTED,
    template_for_edge,
)


def _rebalance_policies() -> tuple[str, ...]:
    """실행면이 아는 리밸런스 정책. **러너에서 가져온다 - 여기 적지 않는다.**

    두 곳에 적으면 어긋난다: 오늘 바인더가 만든 `EVERY_TRADING_DAY` 를 러너가
    몰라 `ValueError: 알 수 없는 rebalance 정책` 으로 실험이 죽었다.
    """
    try:
        from backtest_runner import REBALANCE_POLICIES as _P  # noqa: PLC0415

        return tuple(_P)
    except ImportError:      # 러너를 못 읽으면 유도만 쓴다(직접 지정을 거부)
        return ()


REBALANCE_POLICIES = _rebalance_policies()

MODULE_VERSION = "quant-config-binding-v1"

# 허용 범위. 벗어나면 자르지 않고 거부한다.
# 실행면이 실제로 읽는 expected_edge 키. **여기 없는 키는 무시가 아니라 거부다.**
# 기획안이 논문 용어(formation_window_days 등)로 쓰면 조용히 기본값으로 떨어지고,
# 그러면 등록한 가설과 실행한 실험이 달라진다.
# These are preregistration/context fields, not tunable execution parameters.
# They must remain in the hypothesis contract, but rejecting them as unknown
# makes a valid hypothesis impossible to execute. `universe` is accepted as the
# legacy contract spelling and is mapped to the execution key below.
# ▶ walk_forward_window_days 는 왜 여기인가 (2026-08-14, 카드 t_e9534028)
#   창 분할은 **실행 손잡이가 아니라 사전등록 정책**이다. `walk_forward.
#   make_windows(days, warmup_days, embargo_days)` 에는 가설별 창 길이 인자가
#   아예 없고, 가설마다 창 길이가 달라지면 같은 계열의 다중검정 분모가 비교
#   불가능해진다(fam_* 누적 시도 수가 뜻을 잃는다). 그렇다고 거부하면 이 값을
#   적은 정상 가설이 영영 실행 불가다 - 그래서 **받되, 안 읽는다고 돌려준다**
#   (아래 `Binding.ignored`). 조용히 버리면 등록자는 반영된 줄 안다.
NON_EXECUTION_KEYS = frozenset({"observation_refs", "universe",
                                "walk_forward_window_days"})

EDGE_KEYS = frozenset({
    "type",            # edge_type - 어느 시그널 템플릿인가
    "universe_key",    # 유니버스
    # ── 형성창 / 보유창 분리 (2026-08-14 개방, 카드 t_e9534028) ────────────
    # ▶ 왜 나눴나 - 손잡이 **하나가 두 창을 같이 밀고 있었다.** `horizon_days`
    #   가 형성창(lookback_days)과 리밸런스 주기를 동시에 정해서, "형성
    #   3~12개월 · 보유 1개월" 같은 정상 사양을 표현할 자리가 아예 없었다.
    #
    #   실측 피해(2026-08-13 밤 기각 2건은 같은 뿌리다):
    #     · RAMOM(33c33d0c) - JT 관성의 형성 3~12개월을 20일로 사상해 쟀다
    #     · REV(8041de9d)   - 형성 20일이 반전 반감기(약 1주)보다 길었다
    #   둘 다 가설이 틀린 게 아니라 **가설대로 못 돌린 것**이다. 그 성적을
    #   가설의 증거로 쓰면 안 된다.
    #
    # ▶ 러너는 원래부터 두 창을 따로 읽는다 - `lookback_days`(형성)와
    #   `rebalance`(보유 주기)는 backtest_runner 에서 이미 별개 손잡이다.
    #   못 나눈 것은 바인더뿐이었다.
    #
    # ▶ **안 주면 예전과 완전히 같다.** `signal_window_days` 가 없으면
    #   `horizon_days` 가 형성창을 겸한다 - 기존 가설의 config 가 흔들리면
    #   input_hash 가 바뀌어 사전등록이 무너진다. 넓히되 과거를 안 건드린다.
    "signal_window_days",   # 형성창(신호를 재는 창) -> lookback_days
    "horizon_days",         # 보유·리밸런스 주기(형성창이 따로 없으면 겸용)
    # ── 리밸런스 주기 직접 지정 (2026-08-14 개방) ──────────────────────────
    # ▶ 왜 열었나 (첫 수식형 알파 `332fdec9` 실측)
    #   `rebalance` 는 `_rebalance_for(horizon)` 로 **자동 유도**만 됐다.
    #   horizon<=3 이면 무조건 EVERY_TRADING_DAY 다. 그래서 짧은 신호를 낸
    #   기획안은 매일 리밸런스가 강제됐고, 3개월 표본에서 회전 33배 ·
    #   수수료가 자본의 5.67%p 로 나왔다. IC 는 +0.012 로 **양수인데**
    #   순수익은 음수였다 - 비용이 신호를 먹은 것이지 신호가 없던 게 아니다.
    #
    #   그때 회전을 줄일 손잡이가 없었다. horizon 을 늘리면 신호 자체가
    #   바뀌어 다른 실험이 된다 - "같은 신호를 덜 자주 거래한다" 를 표현할
    #   칸이 필요했다. 신호 지평과 거래 빈도는 원래 다른 개념이고, 러너는
    #   처음부터 `rebalance` 를 따로 읽는다(REBALANCE_POLICIES).
    #
    # ▶ **안 주면 예전과 완전히 같다.** 없으면 `_rebalance_for(horizon)` 가
    #   그대로 돈다 - 기존 가설의 config 가 흔들리면 input_hash 가 바뀌어
    #   사전등록이 무너진다.
    "rebalance",            # REBALANCE_POLICIES 중 하나. 미지정 = horizon 유도
    "top_n",           # 상위 N 종목
    # ── 구성 방식 (2026-08-14 개방) ────────────────────────────────────────
    # ▶ 왜 열었나 - 위 top_n 주석이 인용한 KCI 예측("알파는 숏다리 →
    #   롱온리는 빼기+광폭 보유")을 우리 데이터로 확인했다. signal_composite
    #   분위 곡선(체결가능 1,689종목, 비용 전): 롱온리 초과(Q10-평균)
    #   **+0.06%p/월** 인데 숏다리 몫(평균-Q1)은 **+1.00%p/월** 이다.
    #   상위 분위가 평균과 구별되지 않으니 top_n 을 어떻게 흔들어도 안 된다.
    #
    #   그때 top_n 상한만 300 으로 열고 **"빼기" 는 구현하지 않았다.** 그래서
    #   어휘에 "상위 N개를 산다" 밖에 없었고, 병목 센서스 실측으로
    #   `BASELINE_NOT_BEATEN` 이 계열 14개에서 되풀이됐다. 수단이 없으면
    #   가설을 더 만들어도 같은 벽이다.
    "portfolio_construction",   # TOP_N(기본) | EXCLUDE_BOTTOM
    "exclude_bottom_pct",       # EXCLUDE_BOTTOM 일 때 하위 몇 %를 빼는가
    # ── 위험관리 (2026-08-12 개방) ──────────────────────────────────────────
    # ▶ 왜 열었나
    #   momentum 이 초과 +157.51%p · IR 1.26 · DSR 0.976 을 내고도 낙폭
    #   -50.52% 로 관문(-35%)을 못 넘었다. 그때 실행면에 낙폭을 줄일 손잡이가
    #   **하나도 없었다** - 완전투자 동일가중 말고는 표현할 수가 없었다.
    #   그래서 리서치는 계속 새 엣지를 설계했고, 새 엣지도 같은 자리에서 죽었다.
    #
    #   **값은 우리가 정하지 않는다.** 무엇을 얼마로 걸지는 기획안이 정하고
    #   실험이 검증한다. 여기서는 범위만 지킨다.
    "vol_target_annual",    # 목표 연변동성 (0.15 = 15%)
    "max_drawdown_stop",    # 고점 대비 이 낙폭이면 전량 현금 (-0.25 = -25%)
    "max_exposure",         # 익스포저 상한 (1.0 = 완전투자)
    "vol_lookback_days",    # 변동성 추정 창
    # ── 시장 추세 필터 (2026-08-14 개방) ──────────────────────────────────
    # ▶ 왜 열었나 - 원장 실측이 양쪽으로 막혀 있었다. 손잡이를 끄면 momentum
    #   이 IR 1.255·초과 +157.5%p 인데 낙폭 -50.5% 로 강건성에 걸리고, 변동성
    #   타게팅을 켜면 낙폭은 잡히는데 IR 이 -0.45 로 무너진다. 관문은 정당하다
    #   (업계 OOS 승률 임계 60%·Millennium 낙폭 -5% 자본 반감과 대조하면
    #   우리 -25% 는 오히려 관대하다) - 그러니 **수단이 모자란 것**이다.
    #   문헌: 이동평균 등 타이밍 요소는 장기 하락장 노출 축소에 효과적이고,
    #   변동성 타게팅과 달리 강세장 참여를 덜 희생한다.
    # ── 미시구조 (2026-08-14 개방) ─────────────────────────────────────
    #   호가·체결 일별 집계를 쓰는 신호의 평균 창. 일별 미시구조 값은
    #   잡음이 커서 며칠을 볼지가 곧 사양이다 - 값은 기획안이 정한다.
    "micro_window_days",
    # ── 알파 수식(AST) (2026-08-14 개방) ──────────────────────────────
    # ▶ 왜 - 완성된 신호 14종 중 택1 이라 "호가 압력이 크면서 스프레드가
    #   좁은 종목" 같은 결합 가설은 **낼 칸이 없었다**(제안 42건 중 가격
    #   밖 근거 2건). 문헌(AlphaAgent KDD'25)이 AST 를 쓰는 이유는 창의성과
    #   규제를 동시에 주기 때문이다 - 독창성 유사도·복잡도 제약이 트리
    #   위에서 계산되고, 코드 생성과 달리 지문이 성립해 input_hash 가 산다.
    "signal_expr",
    "trend_filter_days",      # 시장 추세 판정 창(거래일). 미지정 = 꺼짐
    "trend_filter_exposure",  # 추세 이탈 시 익스포저(기본 0.0 = 전량 현금)
    # ── 체결 가능 유니버스 (2026-08-14 개방) ────────────────────────────────
    # ▶ 왜 열었나 (실측)
    #   비유동성 프리미엄 신호가 뽑은 종목의 **일평균 거래대금 중앙값이 0원**,
    #   60일 창의 무거래일이 평균 57.2일이었다. 그 위에서 나온 IR 0.17 은
    #   백테스트에서만 존재하는 수익이다 - 1억을 넣으면 종목당 5백만원인데
    #   그 종목의 하루 거래대금이 사실상 0 이라 체결이 성립하지 않는다.
    #   문헌(McLean-Pontiff)도 같은 것을 경고한다: 공개 후 잔존 알파는
    #   차익거래가 어려운(=우리도 못 사는) 저유동성 구간에 몰린다.
    #
    #   **기본값은 꺼짐(0)이다.** 어느 유니버스로 재는지는 사전등록 대상이라
    #   여기서 임의로 켜면 과거 실험과 비교가 불가능해진다.
    "min_adv_krw",          # 창 평균 거래대금 하한(원). 예: 1e8 = 1억
    "min_trading_days",     # 창 안 최소 거래일수. 예: 40 (60일 중)
    "liquidity_window",     # 유동성 판정 창(거래일). 기본 60
})

# 실수로 읽는 키. 나머지는 정수다 - `_take` 가 정수만 받으므로 갈라야 한다.
FLOAT_KEYS = frozenset({"vol_target_annual", "max_drawdown_stop", "max_exposure",
                        "min_adv_krw", "exclude_bottom_pct",
                        "trend_filter_exposure"})

LIMITS = {
    "lookback_days": (2, 250),      # 1일은 신호가 아니고, 250일 초과는 표본 부족
    # ▶ top_n 상한 개방 (5,100)→(5,300) (2026-08-13). IR 구조 진단 실측:
    #   top-20/3,924 초집중이 TC 0.114(신호 89% 소실)·TE 연 34.6% 의 주범이고
    #   N=200 동일가중은 TC 0.316(2.8배)다. KCI(KRX 32년, 알파는 숏다리 →
    #   롱온리는 빼기+광폭 보유)·DeMiguel(1/N)도 같은 방향이다. 300 초과는
    #   3,924 종목의 8%를 넘어 지수 복제에 가까워지므로 여전히 막는다.
    #   **값은 우리가 정하지 않는다** - 얼마로 걸지는 기획안이 정한다.
    "top_n": (5, 300),              # 5 미만은 분산이 안 된다
    # 하위 배제 비율. 90% 를 넘게 빼면 그건 "빼기" 가 아니라 상위 소수 집중이라
    # top_n 구성과 같아진다 - 그럴 거면 TOP_N 으로 선언해야 실험이 정직하다.
    "exclude_bottom_pct": (1.0, 90.0),

    "holding_horizon": (1, 120),
    # 체결 가능 유니버스 (2026-08-14). 0 = 필터 없음(현행). 상한은 유니버스를
    # 통째로 비우지 않게 막는다 - 일평균 100억은 대형주 수십 종목만 남는다.
    "min_adv_krw": (0.0, 1e10),
    "min_trading_days": (0, 250),
    "liquidity_window": (5, 250),
    # ▶ **레버리지를 열지 않는다.** 상한이 1.0 이다.
    #   개발원칙 9: "위험한 기능은 실패 시 거래 확대가 아니라 Entry 차단
    #   방향으로 동작한다." 익스포저 상한을 1.0 넘게 허용하면 변동성이 낮게
    #   추정된 구간에서 자동으로 레버리지가 걸린다 - 그건 관문이 보는 낙폭을
    #   줄이는 게 아니라 늘리는 길이다.
    "max_exposure": (0.1, 1.0),
    # 2% 미만 목표변동성은 사실상 현금이고, 100% 초과는 타게팅이 무의미하다
    "vol_target_annual": (0.02, 1.0),
    # 음수만 받는다. -0.02 보다 얕으면 노이즈에 계속 끊기고, -0.9 보다 깊으면
    # 정지가 아니라 장식이다.
    "max_drawdown_stop": (-0.90, -0.02),
    "vol_lookback_days": (20, 250),
    # 시장 추세 창. 20일 미만은 노이즈에 계속 끊기고, 250일 초과는 반응이
    # 너무 늦어 필터 구실을 못 한다(문헌의 200일선이 이 범위 한가운데다).
    # 미시구조 평균 창. 1일은 잡음이고, 60일 넘게 평균하면 일별 신호가
    # 아니라 저주파 지표가 된다(그건 다른 가설이다).
    "micro_window_days": (1, 60),
    "trend_filter_days": (20, 250),
    # 추세 이탈 시 익스포저. 1.0 이면 필터를 켠 것이 아니므로 상한을 0.9 로
    # 둔다 - "켰는데 아무것도 안 하는" 설정을 실험으로 받지 않는다.
    "trend_filter_exposure": (0.0, 0.9),
}

# 구성 방식 통제 어휘. **정본은 `backtest_runner.CONSTRUCTIONS` 이고**, 여기 사본은
# 러너를 import 하지 않고(pandas·데이터 경로에 의존한다) 값을 검사하기 위한 것이다.
# 둘이 갈라지면 조용히 어긋나므로 `_check_runner_and_binder_agree_on_knobs` 가 대조한다.
CONSTRUCTION_VOCAB = ("TOP_N", "EXCLUDE_BOTTOM")

# 어휘로 선언하는 손잡이(수가 아니다). 이름 -> 통제 어휘.
CHOICE_KEYS = {"portfolio_construction": CONSTRUCTION_VOCAB}

REBALANCE_BY_HORIZON = {
    # 지평보다 자주 갈아타면 그 지평을 검증하는 것이 아니다
    1: "EVERY_TRADING_DAY",
    5: "EVERY_5_TRADING_DAYS",
    20: "MONTH_FIRST_TRADING_DAY",
}


@dataclass
class Binding:
    config: dict
    from_hypothesis: list = field(default_factory=list)   # 가설이 정한 것
    from_default: list = field(default_factory=list)      # 관례로 채운 것
    # ▶ **받았지만 실행면이 안 읽는 것.** 거부도 아니고 반영도 아닌 제3의 답이
    #   필요하다 - 이 자리가 없으면 `NON_EXECUTION_KEYS` 는 조용한 무시가 되고,
    #   등록자는 자기가 적은 값이 실험에 들어간 줄 안다(그게 이 모듈이 처음부터
    #   막으려던 것이다).
    ignored: list = field(default_factory=list)
    rejected: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def as_dict(self) -> dict:
        return {"config": dict(self.config),
                "from_hypothesis": list(self.from_hypothesis),
                # ▶ 어디까지가 가설이고 어디부터가 관례인지 남긴다.
                #   구분되지 않으면 결과를 해석할 수 없다.
                "from_default": list(self.from_default),
                "ignored": list(self.ignored),
                "rejected": list(self.rejected)}


def _rebalance_for(horizon: int) -> str:
    """지평에 맞는 리밸런싱. **가장 가까운 것**으로 내린다."""
    if horizon in REBALANCE_BY_HORIZON:
        return REBALANCE_BY_HORIZON[horizon]
    if horizon <= 3:
        return "EVERY_TRADING_DAY"
    if horizon <= 10:
        return "EVERY_5_TRADING_DAYS"
    return "MONTH_FIRST_TRADING_DAY"


def unknown_edge_keys(edge: dict | None) -> list[str]:
    """`bind` 가 거부할 `expected_edge` 키. **이 판정의 정본은 여기 하나다.**

    ▶ 왜 함수로 빼는가 (2026-08-14 실측)
      같은 판정을 세 곳이 각자 적고 있었다. 실행면(`bind`)은
      `EDGE_KEYS | NON_EXECUTION_KEYS` 를 받는데, 발주 관문
      (`factory_autopilot._blocked_reasons`)과 배분자(`allocator`)는
      `EDGE_KEYS` 만 봤다. 그래서 **실행면이 받아 줄 가설을 관문이 막았다.**

      실측 피해: `observation_refs`/`universe` 만 갖고 있어 실행면은 통과할
      가설 13건이 관문에 막혔고, 그중 4건(049d07c1·c2d4c707·7c1c1116·
      774f3b75)은 **실험을 한 번도 못 돌고 폐기**됐다. 관문이 실행면보다
      엄격하면 그 차이만큼은 검증이 아니라 소실이다.

      표를 넓힐 때 세 곳을 같이 고치라는 규칙은 지켜지지 않는다(edge_type
      표 셋이 하나씩 순서대로 터진 전례가 있다). 판정을 여기 하나로 두면
      넓히는 순간 세 곳이 같이 넓어진다 - 규칙이 아니라 구조로 막는다.
    """
    return sorted(k for k in (edge or {})
                  if k not in EDGE_KEYS and k not in NON_EXECUTION_KEYS)


def rejection_reasons(hyp: dict) -> list[str]:
    """실행면이 이 가설을 거부할 이유. **관문이 발주 전에 묻는 창구.**

    ▶ 왜 필요한가 (2026-08-14 실측)
      관문은 이름(`unknown_edge_keys`)만 봤고 **값의 범위·부호는 안 봤다.**
      그래서 `a266a02d` 가 `max_drawdown_stop=0.35`(양수 - 낙폭 정지는 음수여야
      한다)로 등록된 뒤, 발주 -> 실행 -> 거부를 **3회** 반복했다. 매번 워커가
      집어 가서 같은 자리에서 죽었고, 그 사이 돌 수 있는 주문이 밀렸다.

      값 검사는 이름 검사보다 늦게 알 이유가 없다. 등록된 값은 이미 다 있다.

    ▶ 판정을 다시 적지 않는다
      범위표(`LIMITS`)를 관문에서 다시 훑으면 `EDGE_KEYS` 때와 똑같이 갈라진다.
      대신 **실행면을 그대로 부른다** - `bind` 의 거부 목록은 base config 와
      무관하게 `expected_edge` 값만으로 정해지므로(범위 위반과 미지 키는 전부
      `b.rejected` 조기 반환 앞에서 결정된다) 빈 base 로 물어도 답이 같다.

    못 재면 빈 목록을 돌려준다 - 판정을 못 한다고 막으면 정상 가설이 굶는다.
    """
    try:
        return list(bind(hyp, {}).rejected)
    except Exception:  # noqa: BLE001
        return []


def bind(hyp: dict, base_config: dict) -> Binding:
    """가설 -> config. **없는 값을 지어내지 않고, 범위 밖은 거부한다.**"""
    cfg = dict(base_config)
    b = Binding(config=cfg)
    edge = hyp.get("expected_edge") or {}

    def _take(name: str, value, target: str, *, write: bool = True) -> None:
        """`write=False` 면 **검사만 하고 config 에 안 쓴다.**

        형성창이 따로 있을 때의 `horizon_days` 가 그렇다 - 값을 lookback_days
        에 넣으면 안 되지만, 검사를 건너뛰면 `horizon_days="닷새"` 가 아래
        `_rebalance_for(int(horizon))` 까지 살아서 **거부가 아니라 예외**로
        죽는다. 거부 사유를 만들지 못하면 관문도 못 묻는다.
        """
        if value is None:
            if write:
                b.from_default.append(f"{target}={cfg.get(target)}")
            return
        try:
            v = int(value)
        except (TypeError, ValueError):
            b.rejected.append(f"{name}={value!r} 를 정수로 읽을 수 없다")
            return
        # ▶ 한도는 **값이 들어갈 자리**(target)의 것을 본다. 입력 이름의 것을
        #   보면 엉뚱한 한도로 막힌다 - horizon_days=126 은 lookback_days(2~250)
        #   로 들어가는데 holding_horizon(1~120) 한도에 걸려 거부됐다
        #   (2026-08-10 실측). 6개월 형성 모멘텀은 정상 사양이다.
        lo, hi = LIMITS.get(target, LIMITS.get(name, (None, None)))
        if lo is not None and not (lo <= v <= hi):
            # ▶ 자르지 않는다. 조용히 자르면 사전등록 지문과 실행 config 가
            #   달라져 같은 문제가 다시 생긴다.
            b.rejected.append(
                f"{name}={v} 가 허용 범위 [{lo}, {hi}] 밖이다 - 자르지 않고 "
                f"거부한다(자르면 등록한 것과 실행한 것이 달라진다)")
            return
        if not write:
            return
        cfg[target] = v
        b.from_hypothesis.append(f"{target}={v}")

    def _take_risk(name: str) -> None:
        """위험관리 손잡이. **없으면 안 넣는다** - 기본은 꺼짐이고, 꺼짐은
        `None` 이 아니라 **키가 없는 상태**여야 러너가 예전 경로로 간다."""
        value = edge.get(name)
        if value is None:
            return
        try:
            v = float(value) if name in FLOAT_KEYS else int(value)
        except (TypeError, ValueError):
            b.rejected.append(f"{name}={value!r} 를 수로 읽을 수 없다")
            return
        lo, hi = LIMITS.get(name, (None, None))
        if lo is not None and not (lo <= v <= hi):
            # 자르지 않는다 - 자르면 등록한 것과 실행한 것이 달라진다
            b.rejected.append(
                f"{name}={v} 가 허용 범위 [{lo}, {hi}] 밖이다 - 자르지 않고 거부한다"
                + (" (레버리지는 열려 있지 않다)" if name == "max_exposure" else ""))
            return
        cfg[name] = v
        b.from_hypothesis.append(f"{name}={v}")

    def _take_choice(name: str) -> None:
        """어휘로 선언하는 손잡이. **어휘 밖은 자르지 않고 거부한다.**

        수 손잡이와 계약이 같다 - 없으면 키를 안 넣고, 그러면 러너가 예전 경로
        (`config.get(...) or "TOP_N"`)로 간다. 다만 정규화는 여기서 끝낸다:
        러너가 `.upper()` 로 받아 주더라도 config 에는 대문자 정본만 남겨야
        `exclude_bottom` 과 `EXCLUDE_BOTTOM` 이 서로 다른 input_hash 가 되지 않는다.
        """
        value = edge.get(name)
        if value is None:
            return
        v = str(value).strip().upper()
        vocab = CHOICE_KEYS[name]
        if v not in vocab:
            b.rejected.append(
                f"{name}={value!r} 가 실행면 통제 어휘 밖이다 - 사용 가능: "
                f"{list(vocab)}. 비슷한 것으로 대신 돌리지 않는다")
            return
        cfg[name] = v
        b.from_hypothesis.append(f"{name}={v}")

    # ▶ 형성창과 보유창은 다른 것이다 (2026-08-14, 카드 t_e9534028)
    #   `signal_window_days` 를 주면 **그것이 형성창**이고 `horizon_days` 는
    #   보유·리밸런스 전용이 된다. 안 주면 예전 그대로 `horizon_days` 가 둘 다
    #   겸한다 - 기존 실험의 config(=input_hash)를 흔들지 않기 위해서다.
    # Invalid false-y values (for example 0) must reach validation instead of
    # silently falling through to a different field/default.
    horizon = (edge.get("horizon_days")
               if edge.get("horizon_days") is not None
               else hyp.get("holding_horizon"))
    signal_window = edge.get("signal_window_days")
    if signal_window is not None:
        _take("signal_window_days", signal_window, "lookback_days")
        # 보유창은 형성창을 덮지 않지만 **검사는 똑같이 받는다**(위 _take 참고).
        # 한도는 예전과 같은 자리(lookback_days) 기준으로 본다 - 여기서 자리를
        # holding_horizon(1~120)으로 바꾸면 6개월 보유 같은 정상 사양이 갑자기
        # 거부된다(2026-08-10 실측과 같은 사고).
        before = len(b.rejected)
        _take("horizon_days", horizon, "lookback_days", write=False)
        # Formation and forecast horizon are separate axes.  Keeping only the
        # strategy-name suffix is not enough: downstream signal IC reads the
        # structured config and otherwise falls back to lookback_days.
        if horizon is not None and len(b.rejected) == before:
            cfg["horizon_days"] = int(horizon)
            b.from_hypothesis.append(f"horizon_days={int(horizon)}")
    else:
        _take("holding_horizon", horizon, "lookback_days")
    _take("top_n", edge.get("top_n") or hyp.get("top_n"), "top_n")
    _take_choice("portfolio_construction")
    # ▶ **알파 수식은 접수 시점에 검증한다** (2026-08-14)
    #   트리라 수치·어휘 경로로 못 싣는다. `parse` 를 통과해야만 config 에
    #   얹는다 - 잘못된 수식이 실험을 만들어 놓고 중간에 죽으면 원장에
    #   반쪽짜리 흔적이 남는다. 막을 거면 **만들기 전에** 막는다.
    _expr = edge.get("signal_expr")
    if _expr is not None:
        try:
            from alpha_ast import parse as _parse_expr  # noqa: PLC0415

            cfg["signal_expr"] = _parse_expr(_expr)
            b.from_hypothesis.append("signal_expr=<AST>")
        except Exception as _e:  # noqa: BLE001 - 사유를 그대로 싣는다
            b.rejected.append(f"알파 수식이 성립하지 않는다: {_e}")


    for _rk in ("vol_target_annual", "max_drawdown_stop", "max_exposure",
                "vol_lookback_days",
                # 시장 추세 필터 (2026-08-14). **러너의 RISK_KEYS 에 있는데 여기
                # 목록에는 없었다** - 어휘·범위는 다 등재돼 있어 `unknown_edge_keys`
                # 가 통과시키는데 config 에는 안 얹혀, 필터를 건 가설이 조용히
                # 필터 없이 돌았다(구성 방식과 같은 결함, 2026-08-14 실측).
                "trend_filter_days", "trend_filter_exposure",
                # 미시구조 평균 창. 위와 같은 이유로 목록에 같이 넣는다.
                "micro_window_days",
                # 하위 배제 비율. 위와 같은 이유로 빠져 있었다.
                "exclude_bottom_pct",
                # 체결 가능 유니버스 (2026-08-14). 위험 손잡이와 같은 계약:
                # 없으면 키를 안 넣고, 그러면 실행면이 예전 경로로 간다.
                "min_adv_krw", "min_trading_days", "liquidity_window"):
        _take_risk(_rk)

    # ▶ **구성 방식과 비율은 짝이다.** 한쪽만 오면 등록한 것과 도는 것이 갈린다.
    #   · pct 만 주면 러너는 `portfolio_construction` 이 없어 TOP_N 으로 가고
    #     pct 를 통째로 무시한다 - 지금 이 카드를 만든 바로 그 모양이다.
    #   · EXCLUDE_BOTTOM 만 주면 러너 안의 기본값 10% 로 조용히 떨어진다.
    #     그 10 은 가설이 정한 값이 아니므로, 사전등록 지문에 남는 "하위 배제"
    #     실험이 실제로는 아무도 선언한 적 없는 비율로 돌게 된다.
    #   자르지도 채우지도 않고 거부한다 - 이 모듈의 다른 손잡이와 같은 계약이다.
    _pc, _pct = cfg.get("portfolio_construction"), cfg.get("exclude_bottom_pct")
    if _pct is not None and _pc != "EXCLUDE_BOTTOM":
        b.rejected.append(
            f"exclude_bottom_pct={_pct} 를 줬는데 portfolio_construction="
            f"{_pc!r} 이다 - 실행면은 EXCLUDE_BOTTOM 일 때만 이 값을 읽으므로 "
            f"지금 그대로 돌리면 비율이 조용히 버려진다. 구성 방식을 함께 선언한다")
    if _pc == "EXCLUDE_BOTTOM" and _pct is None:
        b.rejected.append(
            "portfolio_construction=EXCLUDE_BOTTOM 인데 exclude_bottom_pct 가 "
            f"없다 - 하위 몇 %를 빼는지는 가설이 정한다(허용 {LIMITS['exclude_bottom_pct']}). "
            "여기서 기본값을 채우면 아무도 선언하지 않은 비율이 사전등록에 박힌다")

    # ▶ **읽지 않은 파라미터를 조용히 버리지 않는다** (2026-08-10 실측)
    #   기획안이 `formation_window_days=42` 를 적었는데 바인더는 `horizon_days`
    #   만 봐서 전부 무시했고, 실험이 기본값 5일로 돌았다. 그러면 **등록한 가설과
    #   실제로 돈 실험이 다르고**, 그 성적은 이 가설의 증거가 아니다. 이번에는
    #   input_hash 가 같아 중복 가드에 걸려 드러났을 뿐, 기본값이 달랐다면
    #   조용히 엉뚱한 실험이 근거로 남았다.
    # Preserve observation references for preregistration, while binding the
    # legacy universe spelling to the actual runner parameter.
    if edge.get("universe_key") is None and edge.get("universe") is not None:
        value = edge.get("universe")
        if isinstance(value, str) and value.strip():
            cfg["universe_key"] = value.strip()
            b.from_hypothesis.append(f"universe_key={value.strip()}")

    # ▶ **받았다고 반영된 것이 아니다 - 그 차이를 등록자에게 돌려준다.**
    #   `NON_EXECUTION_KEYS` 는 거부하지 않는 대신 조용해지기 쉽다. 특히
    #   `walk_forward_window_days=252` 는 사전등록에는 남지만 실험은 기본 창으로
    #   돈다 - 그 사실이 리서치에 안 돌아가면, 등록한 가설과 실행한 실험이
    #   다르다는 이 모듈의 원래 문제가 다른 이름으로 되살아난다.
    #   `universe` 는 제외한다 - 그건 무시가 아니라 universe_key 로 **사상**된다.
    for _nk in sorted(k for k in edge
                      if k in NON_EXECUTION_KEYS and k != "universe"):
        b.ignored.append(
            f"{_nk}={edge[_nk]!r} 는 실행면이 읽지 않는다 - 사전등록에는 남지만 "
            f"실험은 기본값으로 돈다")

    unknown = unknown_edge_keys(edge)
    if unknown:
        b.rejected.append(
            f"실행면이 읽지 않는 파라미터가 있다: {unknown} - 무시하고 돌리면 "
            f"등록한 가설과 실행한 실험이 달라진다. 사용 가능: {sorted(EDGE_KEYS)}")

    if b.rejected:
        return b

    # ▶ **가설이 직접 정했으면 그것이 이긴다** (2026-08-14). 유도는 기본값일
    #   뿐이다 - 짧은 신호를 덜 자주 거래하는 사양은 유도로는 표현이 안 된다.
    #   모르는 정책은 자르지 않고 거부한다: 러너가 ValueError 로 죽으면
    #   가설이 RUNNING 에 갇히고, 조용히 바꾸면 등록한 것과 다른 실험이 돈다.
    _rb = edge.get("rebalance")
    if _rb is not None:
        _rb = str(_rb).strip().upper()
        if _rb not in REBALANCE_POLICIES:
            b.rejected.append(
                f"rebalance={_rb!r} 는 실행면에 없는 정책이다 - "
                f"사용 가능: {list(REBALANCE_POLICIES)}")
            return b
        cfg["rebalance"] = _rb
        b.from_hypothesis.append(f"rebalance={_rb} (가설이 직접 지정)")
    elif horizon is not None:
        cfg["rebalance"] = _rebalance_for(int(horizon))
        b.from_hypothesis.append(f"rebalance={cfg['rebalance']}")
    else:
        b.from_default.append(f"rebalance={cfg.get('rebalance')}")

    # ▶ **전략 이름에 가설을 새긴다.** 같은 카탈로그 전략이라도 파라미터가
    #   다르면 다른 실험이어야 하고, input_hash 가 config 를 포함하므로
    #   이 이름까지 바뀌면 사람이 로그에서 구분할 수 있다.
    #
    # ▶ 2026-08-10: 접두를 **가설의 edge_type 에서** 가져온다. 이전에는 base_config
    #   의 전략 이름에서 잘라 썼는데, 그러면 가설이 momentum 이어도 base 가
    #   REV_CONFIG 면 평균회귀 시그널이 돌았다 - **가설과 실제 도는 시그널이
    #   달라지는 것**이고, 그 결과는 그 가설의 증거가 아니라 다른 전략의 성적이다.
    raw_type = str(edge.get("type") or "").strip().lower()
    prefix = str(base_config.get("strategy", "")).split("-")[0]
    if raw_type:
        tpl = template_for_edge(raw_type)
        if tpl is None:
            why = NOT_IMPLEMENTED.get(raw_type)
            b.rejected.append(
                f"edge_type={raw_type!r} 를 실행면 통제 어휘로 사상할 수 없다 - "
                + (f"미구현: {why}" if why
                   else f"어휘에 없다(사용 가능: {sorted(EDGE_VOCAB)})")
                + ". 비슷한 템플릿으로 대신 돌리지 않는다")
            return b
        prefix = tpl.template_id
    cfg["strategy"] = f"{prefix}-{cfg.get('lookback_days')}-{cfg.get('top_n')}"
    # ▶ 하위 배제는 **top_n 을 안 쓴다.** 이름에 `-20` 만 남으면 로그에서
    #   "상위 20종목" 실험으로 읽히는데 실제로는 70종목을 든다 - 이 카드가
    #   지적한 "등록 != 실행" 이 이름 층에서 되풀이되는 것이다. `resolve()` 는
    #   첫 마디만 보므로 꼬리를 붙여도 러너가 못 찾는 일은 없다.
    if cfg.get("portfolio_construction") == "EXCLUDE_BOTTOM":
        cfg["strategy"] = (f"{prefix}-{cfg.get('lookback_days')}"
                           f"-XB{cfg['exclude_bottom_pct']:g}")
    # ▶ 창이 갈린 뒤로는 **형성창만으로 이름을 지으면 안 된다.** 형성 60일에
    #   보유 5일과 보유 20일은 다른 실험인데(리밸런스가 다르다) 이름이 같아지면
    #   사람이 로그에서 구분할 수 없다 - 이 모듈이 이름에 파라미터를 새기는
    #   이유가 그것이다. `signal_window_days` 를 준 가설에만 붙여 기존 실험의
    #   이름(=input_hash)은 건드리지 않는다. `resolve()` 는 첫 토큰만 보므로
    #   러너 해석에는 영향이 없다.
    if signal_window is not None and horizon is not None:
        cfg["strategy"] += f"-H{int(horizon)}"
    return b


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_BASE = {"strategy": "REV-5-SMOKE", "lookback_days": 5, "top_n": 20,
         "rebalance": "EVERY_5_TRADING_DAYS", "initial_capital": 1e8}


def _check_hypothesis_changes_config():
    """**가설이 다르면 config 가 달라야 한다.** 안 그러면 전부 중복 실험이다."""
    a = bind({"expected_edge": {"type": "mean_reversion", "horizon_days": 5}},
             _BASE)
    c = bind({"expected_edge": {"type": "mean_reversion", "horizon_days": 20}},
             _BASE)
    assert a.ok and c.ok
    assert a.config["lookback_days"] == 5 and c.config["lookback_days"] == 20
    # 전략 이름까지 달라져 로그에서 구분된다
    assert a.config["strategy"] != c.config["strategy"], (a.config, c.config)
    # 리밸런싱도 지평을 따라간다 - 지평보다 자주 갈아타면 그 지평을 검증하는
    # 것이 아니다
    assert a.config["rebalance"] == "EVERY_5_TRADING_DAYS"
    assert c.config["rebalance"] == "MONTH_FIRST_TRADING_DAY"


def _check_out_of_range_is_rejected_not_clamped():
    """**자르지 않는다** - 자르면 등록한 것과 실행한 것이 달라진다."""
    b = bind({"expected_edge": {"horizon_days": 9999}}, _BASE)
    assert not b.ok and "허용 범위" in b.rejected[0], b
    assert "자르지 않고" in b.rejected[0], b
    # 값이 반영되지 않았는지도 확인 - 거부인데 config 가 바뀌면 최악이다
    assert b.config["lookback_days"] == _BASE["lookback_days"]

    b2 = bind({"expected_edge": {"horizon_days": 5}, "top_n": 3}, _BASE)
    assert not b2.ok and "top_n" in b2.rejected[0], b2
    # ▶ 광폭 보유 개방(2026-08-13): 200 은 실행 가능해야 하고(진단 실측 TC
    #   2.8배), 400 은 여전히 거부돼야 한다 - 개방이 무제한이 되면 지수를
    #   '전략'으로 등록하는 길이 열린다.
    b3 = bind({"expected_edge": {"horizon_days": 5, "top_n": 200}}, _BASE)
    assert b3.ok, b3.rejected
    assert b3.config["top_n"] == 200, b3.config
    b4 = bind({"expected_edge": {"horizon_days": 5, "top_n": 400}}, _BASE)
    assert not b4.ok and "top_n" in b4.rejected[0], b4


def _check_missing_uses_default_and_says_so():
    """**어디까지가 가설이고 어디부터가 관례인지** 남는다."""
    b = bind({"expected_edge": {"type": "momentum"}}, _BASE)
    assert b.ok, b.rejected
    d = b.as_dict()
    assert any("lookback_days" in x for x in d["from_default"]), d
    assert any("top_n" in x for x in d["from_default"]), d
    assert not d["from_hypothesis"], d


def _check_non_numeric_is_rejected():
    b = bind({"expected_edge": {"horizon_days": "닷새"}}, _BASE)
    assert not b.ok and "정수로 읽을 수 없다" in b.rejected[0], b


def _check_unmapped_edge_type_is_rejected():
    """**실행면에 없는 유형은 거부한다.** 비슷한 템플릿으로 대신 돌리면 그 결과는
    가설의 증거가 아니라 다른 전략의 성적이다."""
    b = bind({"expected_edge": {"type": "volatility_risk_premium",
                                "horizon_days": 20}}, _BASE)
    assert not b.ok, b
    assert "사상할 수 없다" in b.rejected[0] and "미구현" in b.rejected[0], b
    # 어휘에 아예 없는 것도 거부하되 사용 가능 목록을 알려준다
    b2 = bind({"expected_edge": {"type": "정체불명", "horizon_days": 20}}, _BASE)
    assert not b2.ok and "사용 가능" in b2.rejected[0], b2


def _check_prefix_comes_from_hypothesis_not_base():
    """**가설이 시그널을 정한다.** 이전에는 base_config 가 정해서, momentum 가설에
    평균회귀 시그널이 도는 경로가 열려 있었다(실측 결함)."""
    b = bind({"expected_edge": {"type": "momentum", "horizon_days": 20}}, _BASE)
    assert b.ok, b.rejected
    assert b.config["strategy"].startswith("MOM-"), b.config
    b2 = bind({"expected_edge": {"type": "liquidity_shock_reversal",
                                 "horizon_days": 5}}, _BASE)
    assert b2.ok and b2.config["strategy"].startswith("LIQREV-"), b2.config


def _check_bound_config_is_runnable():
    """**바인딩 결과를 러너가 받아야 한다.** 실측 버그: `REV-5-20` 이 카탈로그에
    없어 가설이 반영되는 순간 ValueError 로 실행이 거부됐다."""
    from strategy_templates import resolve

    for et in ("momentum", "mean_reversion", "low_volatility", "breakout"):
        b = bind({"expected_edge": {"type": et, "horizon_days": 20}}, _BASE)
        assert b.ok, (et, b.rejected)
        assert resolve(b.config["strategy"]) is not None,             f"{et}: 바인딩이 만든 {b.config['strategy']!r} 를 러너가 못 찾는다"


def _check_base_config_not_mutated():
    """원본 카탈로그를 건드리면 다음 가설이 오염된다."""
    before = dict(_BASE)
    bind({"expected_edge": {"horizon_days": 20, "top_n": 50}}, _BASE)
    assert _BASE == before, _BASE


def _check_risk_knobs_bind_and_bound():
    """**위험관리 손잡이가 실제로 config 에 닿는다.** (2026-08-12)

    손잡이를 러너에 만들어도 바인더가 `실행면이 읽지 않는 파라미터` 로 거부하면
    에이전트는 영영 못 쓴다 - 오늘 하루 이 모양을 열두 번 봤다.
    """
    base = {"strategy": "MOM", "lookback_days": 20, "top_n": 20}
    b = bind({"expected_edge": {"type": "momentum", "horizon_days": 126,
                                "max_drawdown_stop": -0.25,
                                "vol_target_annual": 0.15,
                                "vol_lookback_days": 60}}, base)
    assert not b.rejected, b.rejected
    assert b.config["max_drawdown_stop"] == -0.25, b.config
    assert b.config["vol_target_annual"] == 0.15, b.config
    assert b.config["vol_lookback_days"] == 60, b.config

    # ▶ **안 준 손잡이는 키 자체가 없어야 한다.** `None` 을 넣어 두면 러너가
    #   "켜졌는데 값이 없다" 로 읽을 수 있다.
    b2 = bind({"expected_edge": {"type": "momentum"}}, base)
    for k in ("vol_target_annual", "max_drawdown_stop", "max_exposure"):
        assert k not in b2.config, f"{k} 를 안 줬는데 config 에 들어갔다"

    # ▶ **레버리지는 안 열린다.** 개발원칙 9.
    b3 = bind({"expected_edge": {"type": "momentum", "max_exposure": 2.0}}, base)
    assert b3.rejected, "max_exposure=2.0 이 통과했다 - 레버리지가 열렸다"
    assert any("레버리지" in r for r in b3.rejected), b3.rejected

    # 낙폭 정지는 음수만. 양수는 뜻이 없다
    b4 = bind({"expected_edge": {"type": "momentum", "max_drawdown_stop": 0.25}},
              base)
    assert b4.rejected, "양수 낙폭 정지가 통과했다"

    # 자르지 않는다 - 범위 밖은 거부
    b5 = bind({"expected_edge": {"type": "momentum", "vol_target_annual": 5.0}},
              base)
    assert b5.rejected and "5.0" in b5.rejected[0], b5.rejected
    print("  위험관리 손잡이 바인딩   OK")


# ▶ **"이름이 등재됐는가" 가 아니라 "값이 얹히는가" 를 본다** (2026-08-14 실측)
#   `portfolio_construction`·`exclude_bottom_pct` 는 EDGE_KEYS·FLOAT_KEYS·LIMITS
#   에 다 등재돼 있어 `unknown_edge_keys()` 를 통과했는데, `bind()` 본문의
#   `_take`/`_take_risk` 호출 목록에 없어 config 에 안 얹혔다. 러너는
#   `config.get("portfolio_construction") or "TOP_N"` 이라 조용히 TOP_N 으로
#   떨어졌고, 거부도 경고도 `dropped` 도 남지 않았다 - **하위 배제 실험이라는
#   이름의 TOP_N top-20 실험**이 원장에 박히는 경로였다. 같은 결함이
#   `trend_filter_days`/`trend_filter_exposure` 에도 동시에 있었다.
#
#   아래 표가 그 재발을 구조로 막는다: EDGE_KEYS 를 넓히면서 여기 탐침을 안
#   적으면 자체 점검이 먼저 터진다. 규칙이 아니라 구조로 막는 쪽을 택한다.
#   형식: 키 -> (넣어 볼 값, config 에 얹혀야 할 이름, 같이 줘야 할 짝)
#   얹히는 이름이 None = 실행 손잡이가 아니라 식별·계약 필드(러너가 안 읽는다).
_LANDING_PROBE = {
    "type":                   ("momentum", "strategy", {}),
    # 유니버스는 데이터셋 선택으로 정해진다 - 러너 config 를 거치지 않는다.
    # (`bind` 는 구 철자 `universe` 만 `universe_key` 로 사상한다)
    "universe_key":           ("krx_all", None, {}),
    "horizon_days":           (20, "lookback_days", {}),
    # 형성창. `horizon_days` 와 **같은 자리(lookback_days)를 두고 다툰다** - 주면
    # 이쪽이 형성창을 가져가고 `horizon_days` 는 보유·리밸런스 전용이 된다.
    # 그래서 이 탐침은 단독으로 준다(짝을 주면 무엇이 얹혔는지 안 갈린다).
    "signal_window_days":     (60, "lookback_days", {}),
    # 리밸런스는 horizon 유도를 **이긴다** - 짝으로 horizon 을 같이 줘서
    # (유도라면 EVERY_TRADING_DAY 가 됐을 값) 직접 지정이 실제로 얹히는지 본다
    "rebalance":              ("MONTH_FIRST_TRADING_DAY", "rebalance",
                               {"horizon_days": 2}),
    "top_n":                  (50, "top_n", {}),
    "portfolio_construction": ("EXCLUDE_BOTTOM", "portfolio_construction",
                               {"exclude_bottom_pct": 30.0}),
    "exclude_bottom_pct":     (30.0, "exclude_bottom_pct",
                               {"portfolio_construction": "EXCLUDE_BOTTOM"}),
    "vol_target_annual":      (0.15, "vol_target_annual", {}),
    "max_drawdown_stop":      (-0.25, "max_drawdown_stop", {}),
    "max_exposure":           (0.8, "max_exposure", {}),
    "vol_lookback_days":      (60, "vol_lookback_days", {}),
    "trend_filter_days":      (200, "trend_filter_days", {}),
    "trend_filter_exposure":  (0.0, "trend_filter_exposure", {}),
    # 미시구조 평균 창(2026-08-14). 호가·체결 일별 집계를 쓰는 신호가 며칠을
    # 평균할지 - 값은 기획안이 정하고 여기서는 얹히는지만 본다.
    "micro_window_days":      (5, "micro_window_days", {}),
    # 알파 수식(AST). 값이 트리라 탐침도 트리를 준다 - `parse` 정규형이
    # config 에 그대로 얹히는지 본다.
    "signal_expr":            ({"op": "ts_return", "field": "close", "n": 21},
                               "signal_expr", {}),
    "min_adv_krw":            (1e8, "min_adv_krw", {}),
    "min_trading_days":       (40, "min_trading_days", {}),
    "liquidity_window":       (60, "liquidity_window", {}),
}


def _check_every_edge_key_actually_lands_in_config():
    """**등재된 손잡이는 전부 config 에 얹혀야 한다.**

    이름 검사(`unknown_edge_keys`)와 값 검사(`LIMITS`)를 다 통과하고도 `bind`
    본문이 안 부르면 조용히 사라진다 - 거부보다 나쁘다. 거부는 보이지만 이건
    결과가 나오는데 다른 실험이기 때문이다.
    """
    missing_probe = sorted(EDGE_KEYS - set(_LANDING_PROBE))
    assert not missing_probe, (
        f"EDGE_KEYS 에 넣고 탐침을 안 적은 키: {missing_probe} - 이 키가 실제로 "
        f"config 에 얹히는지 아무도 안 보고 있다는 뜻이다")
    stale = sorted(set(_LANDING_PROBE) - EDGE_KEYS)
    assert not stale, f"EDGE_KEYS 에서 빠진 키의 탐침이 남아 있다: {stale}"

    base = {"strategy": "REV-5-SMOKE", "lookback_days": 5, "top_n": 20}
    for key, (value, target, mates) in _LANDING_PROBE.items():
        if target is None:
            continue
        edge = {"type": "momentum", **mates, key: value}
        b = bind({"expected_edge": edge}, base)
        assert not b.rejected, f"{key}={value!r} 정상값인데 거부됐다: {b.rejected}"
        assert target in b.config, (
            f"{key}={value!r} 를 줬는데 config['{target}'] 이 없다 - "
            f"`bind()` 가 이 키를 읽지 않는다. 등재만 하고 얹지 않으면 "
            f"실행면은 기본값으로 가고, 판정문은 그것을 이 가설의 결과로 적는다")
        if key == "type":
            assert b.config["strategy"].startswith("MOM-"), b.config
            continue
        got = b.config[target]
        want = value.upper() if isinstance(value, str) else value
        assert got == want, (
            f"{key}={value!r} 를 줬는데 config['{target}']={got!r} 이다 - "
            f"값이 도중에 바뀌었다")
        # 가설이 정한 값이라고 남아야 한다 - 관례와 구분되지 않으면 해석이 안 된다
        assert any(x.startswith(f"{target}=") for x in b.from_hypothesis), (
            f"{key} 가 config 에는 얹혔는데 from_hypothesis 에 안 남았다 - "
            f"어디까지가 가설인지 사후에 구분할 수 없다")
    print(f"  등재 키가 실제로 얹힌다({len(_LANDING_PROBE)}개) OK")


def _check_bound_config_constructs_what_was_registered():
    """**사전등록한 구성과 실제로 도는 종목 집합이 같아야 한다.** (2026-08-14)

    이 카드를 만든 실측: `expected_edge` 에 EXCLUDE_BOTTOM/30% 를 등록해도
    `bind` 결과 config 로 `_construct` 를 돌리면 **top-20** 이 나왔다(등록은
    70종목). 거부 목록은 비어 있었다 - 즉 사람도 관문도 알 방법이 없었다.

    이름 대조(`_check_runner_and_binder_agree_on_knobs`)는 이걸 못 잡는다.
    실행면을 실제로 돌려 봐야 잡힌다.
    """
    import sys as _s  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415
    _s.path.insert(0, str(_P(__file__).resolve().parent))
    try:
        from backtest_runner import _construct  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001  (데이터 경로 없는 환경)
        print(f"  등록==실행 구성 대조     건너뜀({type(exc).__name__})")
        return

    ranked = [f"S{i:03d}" for i in range(100)]
    edge = {"type": "low_max", "universe_key": "krx_all", "horizon_days": 20,
            "portfolio_construction": "EXCLUDE_BOTTOM",
            "exclude_bottom_pct": 30.0}
    b = bind({"expected_edge": edge}, dict(_BASE))
    assert not b.rejected, b.rejected

    executed = _construct(ranked, b.config)
    registered = _construct(ranked, dict(_BASE, **{
        "portfolio_construction": edge["portfolio_construction"],
        "exclude_bottom_pct": edge["exclude_bottom_pct"]}))
    assert executed == registered, (
        f"등록한 구성({len(registered)}종목)과 바인딩이 실제로 돌리는 것"
        f"({len(executed)}종목)이 다르다 - 같은 실험이 아니다")
    assert len(executed) == 70, len(executed)

    # TOP_N 은 예전 그대로여야 한다 - 안 켠 실험의 input_hash 가 흔들리면
    # 사전등록이 무너진다
    b0 = bind({"expected_edge": {"type": "momentum", "horizon_days": 20}}, _BASE)
    assert "portfolio_construction" not in b0.config, b0.config
    assert _construct(ranked, b0.config) == ranked[:_BASE["top_n"]]
    print("  등록==실행 구성 대조     OK")


def _check_construction_vocabulary_is_enforced():
    """**어휘 밖은 거부한다** - 자르거나 비슷한 것으로 대신 돌리지 않는다."""
    ok = {"type": "momentum", "horizon_days": 20,
          "portfolio_construction": "exclude_bottom", "exclude_bottom_pct": 30.0}
    b = bind({"expected_edge": ok}, _BASE)
    assert b.ok, b.rejected
    # 정규화까지 여기서 끝낸다 - 대소문자가 다른 두 config 는 다른 input_hash 다
    assert b.config["portfolio_construction"] == "EXCLUDE_BOTTOM", b.config
    # 이름에도 새겨진다 - `-20`(top_n) 으로 남으면 로그가 거짓말을 한다
    assert "XB30" in b.config["strategy"], b.config

    bad = bind({"expected_edge": dict(ok, portfolio_construction="LONG_SHORT")},
               _BASE)
    assert not bad.ok and "통제 어휘 밖" in bad.rejected[0], bad
    assert "EXCLUDE_BOTTOM" in bad.rejected[0], bad.rejected

    # 범위 밖 비율도 자르지 않고 거부
    for q in (0.5, 95.0):
        r = bind({"expected_edge": dict(ok, exclude_bottom_pct=q)}, _BASE)
        assert not r.ok and "허용 범위" in r.rejected[0], (q, r.rejected)

    # **짝이 아니면 거부한다** - 한쪽만 오면 등록과 실행이 갈린다
    only_pct = bind({"expected_edge": {"type": "momentum", "horizon_days": 20,
                                       "exclude_bottom_pct": 30.0}}, _BASE)
    assert not only_pct.ok and "조용히 버려진다" in only_pct.rejected[0], only_pct
    only_mode = bind({"expected_edge": {
        "type": "momentum", "horizon_days": 20,
        "portfolio_construction": "EXCLUDE_BOTTOM"}}, _BASE)
    assert not only_mode.ok and "exclude_bottom_pct 가 없다" in only_mode.rejected[0], \
        only_mode

    # TOP_N 을 명시하는 것은 정상이다 - 기본값과 같아도 선언은 선언이다
    t = bind({"expected_edge": {"type": "momentum", "horizon_days": 20,
                                "portfolio_construction": "TOP_N"}}, _BASE)
    assert t.ok and t.config["portfolio_construction"] == "TOP_N", t

    # 관문도 같은 답을 내야 한다 - 갈라지면 실행면이 받을 가설을 관문이 막는다
    assert rejection_reasons({"expected_edge": dict(ok, portfolio_construction="X")})
    assert rejection_reasons({"expected_edge": ok}) == []
    print("  구성 방식 어휘 강제       OK")


def _check_runner_and_binder_agree_on_knobs():
    """**두 곳이 같은 손잡이 이름·어휘를 써야 한다.** 다르면 조용히 무시된다."""
    import sys as _s  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415
    _s.path.insert(0, str(_P(__file__).resolve().parent))
    from backtest_runner import CONSTRUCTIONS, RISK_KEYS  # noqa: PLC0415

    missing = sorted(set(RISK_KEYS) - EDGE_KEYS)
    assert not missing, (f"러너는 읽는데 바인더가 거부하는 손잡이: {missing} - "
                         f"에이전트가 쓰려고 하면 파라미터 거부로 죽는다")
    # ▶ 이름이 같아도 **`bind` 가 안 부르면 소용없다** - 그게 이 카드의 결함이다.
    #   RISK_KEYS 는 전원 탐침 표에 있어야 하고, 탐침 표는 실제로 얹히는지 본다.
    unprobed = sorted(set(RISK_KEYS) - set(_LANDING_PROBE))
    assert not unprobed, (
        f"러너가 읽는 손잡이인데 얹힘 탐침이 없다: {unprobed}")
    assert tuple(CONSTRUCTIONS) == CONSTRUCTION_VOCAB, (
        f"구성 방식 어휘가 갈라졌다 - 러너 {CONSTRUCTIONS} vs "
        f"바인더 {CONSTRUCTION_VOCAB}. 바인더가 좁으면 정상 가설이 막히고, "
        f"넓으면 러너가 ValueError 로 죽는다")
    print("  러너<->바인더 손잡이 일치 OK")


def _check_preregistration_keys_are_not_unknown():
    """**사전등록용 키는 거부 대상이 아니다** (2026-08-14 실측).

    `observation_refs`/`universe` 는 실행 손잡이가 아니라 계약 필드라
    `bind` 가 받아 준다. 이걸 거부로 세면 정상 가설이 실행 불가가 된다 -
    관문이 실제로 그렇게 세고 있었고, 실험을 한 번도 못 돌고 폐기된 가설이
    4건이었다(049d07c1·c2d4c707·7c1c1116·774f3b75).
    """
    for k in NON_EXECUTION_KEYS:
        assert unknown_edge_keys({"type": "momentum", k: "x"}) == [], (
            f"사전등록 키 {k!r} 가 거부로 세어졌다 - 관문이 이걸 보면 "
            f"실행면이 받아 줄 가설을 막는다")

    # 모르는 이름은 여전히 거부한다 - 넓힌 것이 아니라 맞춘 것이다
    assert unknown_edge_keys({"formation_window_days": 42}) == \
        ["formation_window_days"], "모르는 이름까지 통과시키면 안 된다"
    print("  사전등록 키 != 미지 파라미터 OK")


def _check_gate_sees_range_violations_too():
    """**관문이 이름만 보면 값으로 죽는다** (2026-08-14 실측).

    `a266a02d` 가 `max_drawdown_stop=0.35`(양수) 로 등록됐다. 이름은 정상이라
    관문을 통과했고, 실행면이 범위 [-0.9, -0.02] 로 거부해 **3회** 발주-실행-
    거부를 반복했다. 관문이 물어야 할 것은 "읽을 수 있는 이름인가" 가 아니라
    "실행면이 이걸 돌리는가" 다.

    함께 고정하는 것: **관문의 답이 실행면의 답과 같아야 한다.** 다르면 또
    갈라진다 - 그래서 `rejection_reasons` 는 `bind` 를 그대로 부른다.
    """
    hyp = {"expected_edge": {"type": "momentum", "max_drawdown_stop": 0.35}}
    why = rejection_reasons(hyp)
    assert why and "0.35" in why[0], f"부호가 뒤집힌 값을 관문이 통과시킨다: {why}"

    # 실행면이 실제로 거부하는 것과 같은 답이어야 한다
    assert why == bind(hyp, {"strategy": "X"}).rejected, \
        "관문과 실행면의 답이 다르다 - 판정이 또 갈라졌다"

    # 미지 이름도 같은 창구로 잡힌다(관문이 두 번 묻지 않게)
    # (`signal_window_days` 는 2026-08-14 에 형성창으로 열렸다 - 이제 미지
    #  파라미터의 예시가 아니다. 여전히 어휘 밖인 이름으로 바꾼다.)
    assert rejection_reasons({"expected_edge": {"type": "momentum",
                                                "formation_window_days": 20}}), \
        "미지 파라미터를 이 창구가 못 잡으면 관문이 검사를 둘로 나눠야 한다"

    # 정상 가설은 통과한다 - 못 재면 막는 쪽으로 기울면 정상이 굶는다
    assert rejection_reasons({"expected_edge": {
        "type": "momentum", "horizon_days": 20, "top_n": 200,
        "max_drawdown_stop": -0.25, "observation_refs": ["x"]}}) == []
    print("  관문이 값 범위도 본다      OK")


def _check_gate_asks_the_execution_surface():
    """**관문이 판정을 직접 적으면 안 된다** (2026-08-14 실측).

    발주 관문과 배분자가 `k not in EDGE_KEYS` 를 각자 적고 있었다.
    `NON_EXECUTION_KEYS` 를 실행면에만 더하자 **관문이 실행면보다 엄격해졌고**,
    그 차이가 곧바로 소실로 나타났다(위 4건). 표를 넓힐 때 세 곳을 같이
    고치라는 규칙은 지켜지지 않으므로, 손으로 센 흔적이 남아 있는지를 본다.

    파일이 없으면(컨테이너로 pipeline 만 복사된 경우) 검사를 건너뛴다 -
    없는 파일을 실패로 치면 실행면 자체 점검이 배포에서 못 돈다.
    """
    from pathlib import Path as _P  # noqa: PLC0415

    here = _P(__file__).resolve().parent
    # parents[1] = departments/ (pipeline -> 04-quant-backtest -> departments)
    gates = [here / "allocator.py",
             # 버리는 쪽도 같은 판정이다 - 여기서 갈리면 **조용히** 유니버스가
             # 바뀐다(막히는 것보다 나쁘다: 결과가 나오는데 다른 실험이다).
             here / "walk_forward.py",
             here.parents[1] / "01-research" / "factory" / "factory_autopilot.py"]
    checked = 0
    for g in gates:
        if not g.exists():
            continue
        checked += 1
        src = g.read_text(encoding="utf-8")
        # 주석에서 이 규칙을 설명하는 것은 괜찮다 - 코드로 세는 것만 막는다
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "not in EDGE_KEYS" not in code, (
            f"{g.name} 이 미지 파라미터를 직접 세고 있다 - "
            f"`unknown_edge_keys()` 에 물어야 관문과 실행면이 갈라지지 않는다")
    print(f"  관문이 실행면에 묻는다({checked}곳) OK")


def _check_formation_and_holding_windows_are_separate():
    """**손잡이 하나가 두 창을 같이 밀지 않는다** (2026-08-14, 카드 t_e9534028).

    실측 발단: `667f0a45`[mean_reversion krx_all] 가 signal_window_days=60 ·
    walk_forward_window_days=252 를 들고 "실행면이 안 읽는 파라미터" 로 매 주기
    발주 보류였다(재시도로 안 풀린다 - 가설이 실행 불가로 태어났다).
    같은 뿌리로 RAMOM(33c33d0c)·REV(8041de9d)가 형성창을 못 적어 기각됐다.
    """
    # 원장 실측 그대로의 expected_edge
    edge = {"type": "mean_reversion", "top_n": 20, "horizon_days": 20,
            "universe_key": "krx_all", "signal_window_days": 60,
            "walk_forward_window_days": 252}
    assert unknown_edge_keys(edge) == [], unknown_edge_keys(edge)
    b = bind({"expected_edge": edge}, _BASE)
    assert b.ok, b.rejected
    assert rejection_reasons({"expected_edge": edge}) == [], "관문이 아직 막는다"

    # 형성창은 signal_window_days 가, 보유 주기는 horizon_days 가 정한다
    assert b.config["lookback_days"] == 60, b.config
    assert b.config["horizon_days"] == 20, b.config
    assert b.config["rebalance"] == "MONTH_FIRST_TRADING_DAY", b.config

    # 한쪽을 밀어도 다른 쪽이 안 따라온다 - 이게 "분리" 의 정의다
    h5 = bind({"expected_edge": dict(edge, horizon_days=5)}, _BASE)
    assert h5.config["lookback_days"] == 60, "보유창이 형성창을 끌고 갔다"
    assert h5.config["horizon_days"] == 5, h5.config
    assert h5.config["rebalance"] == "EVERY_5_TRADING_DAYS", h5.config
    assert h5.config["strategy"] != b.config["strategy"], \
        "보유 주기만 다른 두 실험이 로그에서 같은 이름을 받는다"
    s20 = bind({"expected_edge": dict(edge, signal_window_days=20)}, _BASE)
    assert s20.config["lookback_days"] == 20, s20.config
    assert s20.config["horizon_days"] == 20, s20.config
    assert s20.config["rebalance"] == b.config["rebalance"], \
        "형성창이 보유 주기를 끌고 갔다"

    # **안 주면 예전과 완전히 같다** - 기존 실험의 input_hash 가 흔들리면
    # 사전등록이 무너진다. 넓힌 것이지 바꾼 것이 아니다.
    old = bind({"expected_edge": {"type": "mean_reversion", "horizon_days": 20}},
               _BASE)
    assert old.config["lookback_days"] == 20 and old.config["strategy"] == "REV-20-20", \
        old.config

    # 형성창도 자르지 않고 거부한다
    bad = bind({"expected_edge": {"type": "momentum", "signal_window_days": 9999}},
               _BASE)
    assert not bad.ok and "자르지 않고" in bad.rejected[0], bad.rejected
    assert bad.config["lookback_days"] == _BASE["lookback_days"], bad.config
    # 보유창이 비수치면 예외가 아니라 **거부 사유**로 나와야 한다
    bad2 = bind({"expected_edge": {"type": "momentum", "signal_window_days": 60,
                                   "horizon_days": "닷새"}}, _BASE)
    assert not bad2.ok and "정수로 읽을 수 없다" in bad2.rejected[0], bad2.rejected

    zero = bind({"expected_edge": {"type": "momentum", "signal_window_days": 60,
                                    "horizon_days": 0}}, _BASE)
    assert not zero.ok, "0일 지평이 기본값으로 조용히 바뀌었다"

    # 창 분할 사양은 받되 **조용히 무시하지 않는다**
    assert any("walk_forward_window_days" in x for x in b.ignored), b.ignored
    assert any("walk_forward_window_days" in x for x in b.as_dict()["ignored"])
    print("  형성창<->보유창 분리       OK")


def _check_rebalance_can_be_set_apart_from_horizon():
    """**같은 신호를 덜 자주 거래하는 사양이 표현돼야 한다** (2026-08-14 실측).

    첫 수식형 알파가 horizon 2일이라 `_rebalance_for` 가 매일 리밸런스를
    강제했고, 3개월 표본에서 회전 33배 · 수수료가 자본의 5.67%p 였다. IC 는
    +0.012 로 양수인데 순수익은 음수 - **비용이 신호를 먹었다.** 그때 회전을
    줄일 손잡이가 없었다(horizon 을 늘리면 신호 자체가 바뀌어 다른 실험이 된다).
    """
    # ① 직접 지정이 **유도를 이긴다** - horizon 2 면 유도는 EVERY_TRADING_DAY 다
    b = bind({"expected_edge": {"type": "momentum", "horizon_days": 2,
                                "rebalance": "EVERY_5_TRADING_DAYS"}}, _BASE)
    assert not b.rejected, b.rejected
    assert b.config["rebalance"] == "EVERY_5_TRADING_DAYS", b.config
    assert any("직접 지정" in x for x in b.from_hypothesis), b.from_hypothesis
    # 형성창은 건드리지 않는다 - 리밸런스만 바꾼 것이 전부여야 한다
    assert b.config["lookback_days"] == 2, b.config

    # ② **안 주면 예전 그대로다** - 기존 가설의 input_hash 가 흔들리면
    #    사전등록이 무너진다
    old = bind({"expected_edge": {"type": "momentum", "horizon_days": 2}}, _BASE)
    assert old.config["rebalance"] == _rebalance_for(2), old.config

    # ③ 모르는 정책은 **자르지 않고 거부한다.** 러너가 ValueError 로 죽으면
    #    가설이 RUNNING 에 갇히고, 조용히 바꾸면 등록한 것과 다른 실험이 돈다
    bad = bind({"expected_edge": {"type": "momentum", "horizon_days": 2,
                                  "rebalance": "EVERY_HOUR"}}, _BASE)
    assert bad.rejected and any("없는 정책" in x for x in bad.rejected), bad.rejected

    # ④ 어휘 정본은 **러너**다 - 여기 따로 적으면 어긋난다(오늘 실측: 바인더가
    #    만든 EVERY_TRADING_DAY 를 러너가 몰라 실험이 죽었다)
    from backtest_runner import REBALANCE_POLICIES as _RP
    assert set(REBALANCE_POLICIES) == set(_RP), (REBALANCE_POLICIES, _RP)
    assert "EVERY_5_TRADING_DAYS" in _RP and "EVERY_TRADING_DAY" in _RP, _RP
    print("  리밸런스 직접 지정        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_hypothesis_changes_config();          print("  가설 -> config 반영      OK")
    _check_out_of_range_is_rejected_not_clamped(); print("  범위 밖 = 거부(자름X)   OK")
    _check_unmapped_edge_type_is_rejected();     print("  미사상 유형 거부         OK")
    _check_prefix_comes_from_hypothesis_not_base(); print("  가설이 시그널 결정      OK")
    _check_bound_config_is_runnable();           print("  바인딩 -> 러너 해석 가능  OK")
    _check_missing_uses_default_and_says_so();   print("  기본값 출처 표시        OK")
    _check_non_numeric_is_rejected();            print("  비수치 거부             OK")
    _check_base_config_not_mutated();            print("  원본 불변               OK")
    _check_risk_knobs_bind_and_bound()
    _check_every_edge_key_actually_lands_in_config()
    _check_construction_vocabulary_is_enforced()
    _check_bound_config_constructs_what_was_registered()
    _check_runner_and_binder_agree_on_knobs()
    _check_preregistration_keys_are_not_unknown()
    _check_gate_sees_range_violations_too()
    _check_gate_asks_the_execution_surface()
    _check_formation_and_holding_windows_are_separate()
    _check_rebalance_can_be_set_apart_from_horizon()
    print("config 바인딩 18개 영역 통과.")
