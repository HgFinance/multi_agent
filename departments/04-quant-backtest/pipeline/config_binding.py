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
LIMITS = {
    "lookback_days": (2, 250),      # 1일은 신호가 아니고, 250일 초과는 표본 부족
    "top_n": (5, 100),              # 5 미만은 분산이 안 되고, 100 초과는 지수다
    "holding_horizon": (1, 120),
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
        lo, hi = LIMITS.get(name, (None, None))
        if lo is not None and not (lo <= v <= hi):
            # ▶ 자르지 않는다. 조용히 자르면 사전등록 지문과 실행 config 가
            #   달라져 같은 문제가 다시 생긴다.
            b.rejected.append(
                f"{name}={v} 가 허용 범위 [{lo}, {hi}] 밖이다 - 자르지 않고 "
                f"거부한다(자르면 등록한 것과 실행한 것이 달라진다)")
            return
        cfg[target] = v
        b.from_hypothesis.append(f"{target}={v}")

    horizon = edge.get("horizon_days") or hyp.get("holding_horizon")
    _take("holding_horizon", horizon, "lookback_days")
    _take("top_n", edge.get("top_n") or hyp.get("top_n"), "top_n")

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
    print("config 바인딩 8개 영역 통과.")
