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

MODULE_VERSION = "quant-config-binding-v1"

# 허용 범위. 벗어나면 자르지 않고 거부한다.
# 실행면이 실제로 읽는 expected_edge 키. **여기 없는 키는 무시가 아니라 거부다.**
# 기획안이 논문 용어(formation_window_days 등)로 쓰면 조용히 기본값으로 떨어지고,
# 그러면 등록한 가설과 실행한 실험이 달라진다.
# These are preregistration/context fields, not tunable execution parameters.
# They must remain in the hypothesis contract, but rejecting them as unknown
# makes a valid hypothesis impossible to execute. `universe` is accepted as the
# legacy contract spelling and is mapped to the execution key below.
NON_EXECUTION_KEYS = frozenset({"observation_refs", "universe"})

EDGE_KEYS = frozenset({
    "type",            # edge_type - 어느 시그널 템플릿인가
    "universe_key",    # 유니버스
    "horizon_days",    # 보유/형성 창 -> lookback_days
    "top_n",           # 상위 N 종목
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
})

# 실수로 읽는 키. 나머지는 정수다 - `_take` 가 정수만 받으므로 갈라야 한다.
FLOAT_KEYS = frozenset({"vol_target_annual", "max_drawdown_stop", "max_exposure"})

LIMITS = {
    "lookback_days": (2, 250),      # 1일은 신호가 아니고, 250일 초과는 표본 부족
    "top_n": (5, 100),              # 5 미만은 분산이 안 되고, 100 초과는 지수다
    "holding_horizon": (1, 120),
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
}

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


def bind(hyp: dict, base_config: dict) -> Binding:
    """가설 -> config. **없는 값을 지어내지 않고, 범위 밖은 거부한다.**"""
    cfg = dict(base_config)
    b = Binding(config=cfg)
    edge = hyp.get("expected_edge") or {}

    def _take(name: str, value, target: str) -> None:
        if value is None:
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

    horizon = edge.get("horizon_days") or hyp.get("holding_horizon")
    _take("holding_horizon", horizon, "lookback_days")
    _take("top_n", edge.get("top_n") or hyp.get("top_n"), "top_n")
    for _rk in ("vol_target_annual", "max_drawdown_stop", "max_exposure",
                "vol_lookback_days"):
        _take_risk(_rk)

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

    unknown = sorted(k for k in edge if k not in EDGE_KEYS and k not in NON_EXECUTION_KEYS)
    if unknown:
        b.rejected.append(
            f"실행면이 읽지 않는 파라미터가 있다: {unknown} - 무시하고 돌리면 "
            f"등록한 가설과 실행한 실험이 달라진다. 사용 가능: {sorted(EDGE_KEYS)}")

    if b.rejected:
        return b

    if horizon is not None:
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


def _check_runner_and_binder_agree_on_risk_keys():
    """**두 곳이 같은 손잡이 이름을 써야 한다.** 다르면 조용히 무시된다."""
    import sys as _s  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415
    _s.path.insert(0, str(_P(__file__).resolve().parent))
    from backtest_runner import RISK_KEYS  # noqa: PLC0415

    missing = sorted(set(RISK_KEYS) - EDGE_KEYS)
    assert not missing, (f"러너는 읽는데 바인더가 거부하는 손잡이: {missing} - "
                         f"에이전트가 쓰려고 하면 파라미터 거부로 죽는다")
    print("  러너<->바인더 손잡이 일치 OK")


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
    _check_runner_and_binder_agree_on_risk_keys()
    print("config 바인딩 10개 영역 통과.")
