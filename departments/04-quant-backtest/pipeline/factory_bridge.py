"""공장 다리 - 리서치 기획안 접수(Gate 0)와 실험 결과 환류.

담당: 재일 (퀀트·백테스트본부 QNT)
계약: departments/01-research/contracts/factory_contracts.py
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 7.1절 0단계·10단계, 7.6.2절

▶ 이 모듈이 루프를 닫는다
  지금까지 리서치 -> 퀀트는 `GET /seeds` 호출처가 0개라 끊겨 있었고, 퀀트 -> 리서치는
  코드가 아예 없었다. 그래서 실험 결과가 다음 가설에 영향을 주지 못했다 -
  **공장이 아니라 일방통행이었다.**

  여기서 두 방향을 모두 연결한다:
    접수  research.experiment_proposals -> Gate 0 -> quant.hypotheses
    환류  실험 판정 -> research.experiment_outcomes -> 다음 Gate 0 가 조회

▶ **환류 적재와 상태 전이는 한 트랜잭션이다**
  "적재가 종결의 전제 조건" 을 주석으로 적어 두면 언젠가 지켜지지 않는다. 그래서
  `finalize()` 가 둘을 함께 커밋한다 - 환류가 실패하면 상태도 안 바뀐다. 조용히
  종결되고 교훈만 사라지는 경로를 구조로 없앤다.

▶ Gate 0 는 결정론이다
  어휘 사상·예산·기각 이력 대조 셋 다 코드가 판정한다. 리서치의 발행 게이트가 같은
  검사를 이미 하지만 두 번 하는 것이 낭비가 아니다 - 앞의 것은 기획 비용을 아끼고
  뒤의 것은 **다른 경로로 들어온 가설까지** 막는다(퀀트 자체 생성, 수동 등록).

자체 점검: python departments/04-quant-backtest/pipeline/factory_bridge.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

for _research_contracts in (
    Path("/app/repo/departments/01-research/contracts"),
    Path(__file__).resolve().parents[2] / "01-research" / "contracts",
):
    if _research_contracts.is_dir():
        sys.path.insert(0, str(_research_contracts))

from data_resolution import SOURCE_TABLES
from strategy_templates import EDGE_VOCAB, NOT_IMPLEMENTED
from trial_family import UNIVERSE_VOCAB, family_id, hypothesis_view, pressure

MODULE_VERSION = "quant-factory-bridge-v1"

# 기각으로 보는 판정. 이것들의 교훈에는 대응이 있어야 재도전할 수 있다.
REJECTING = frozenset({"REJECT", "GATE_HOLD", "KILLED", "DEMOTED"})

# 예산을 세지 않는 판정 - 실험이 시작조차 못 한 것은 시도가 아니다.
NOT_A_TRIAL = frozenset({"BLOCKED"})

# ── 전략 구조 어휘 ─────────────────────────────────────────────────────────
# ▶ **어휘가 없으면 개념이 조용히 사라진다** (2026-08-11 회고)
#   기획안 초안은 롱숏이었다(하위 분위 롱 / 상위 분위 숏). 실행면이 읽는 키가
#   horizon_days·top_n 뿐이라고 알려 주자, 기획자는 숏 다리를 **표현할 자리가
#   없어서 개념째 지웠다.** 그런데 원장에는 그냥 "momentum 가설" 로 남았다 -
#   "우리 엔진이 숏을 못 해 롱온리로 깎인 모멘텀" 이 아니라.
#   그리고 그것이 결과를 정했을 수 있다: 상승장 벤치마크 +69.55% 를 롱온리
#   선별로 이기는 것은 사실상 불가능하다. **깎인 채로 실험하고 "안 된다" 고
#   기록한 셈이다.** 어휘 밖 구조는 깎지 말고 반려한다.
STRUCTURE_VOCAB = frozenset({"long_only"})
STRUCTURE_NOT_IMPLEMENTED = {
    "long_short": "실행면이 숏 다리를 지원하지 않는다 - 롱온리로 깎아 돌리면 "
                  "그 성적은 이 가설의 증거가 아니다",
    "market_neutral": "베타 중립화 미구현",
    "pairs": "페어 구성·헤지비 미구현",
}

# walk-forward 창이 최소 몇 개는 나와야 강건성을 판정할 수 있다. 이보다 적으면
# 실험을 돌려도 INCONCLUSIVE 로 끝난다 - 그건 접수에서 막아야 할 낭비다.
MIN_WF_WINDOWS = 4
# Keep these rulers aligned with walk_forward._short_sample_windows.  Gate 0
# uses the same arithmetic before spending a trial on a sample that cannot
# produce enough out-of-sample windows.
SHORT_SAMPLE_MAX_DAYS = 120
SHORT_MIN_TEST_DAYS = 10
SHORT_TARGET_WINDOWS = 6

INTRADAY_LANE = "INTRADAY_EVENT"
INTRADAY_EDGE_KEYS = frozenset({
    "intraday_signal_expr", "horizon_seconds", "sample_interval_seconds",
    "feature_lookback_seconds", "order_latency_ms", "max_quote_age_seconds",
    "fee_bps_per_side", "maker_fee_bps_per_side", "execution", "threshold",
    "evaluation_days", "instrument_count", "position_mode",
})


@dataclass
class Gate0Result:
    """접수 판정. **거부 사유는 기계 판독 가능한 코드**여야 리서치가 대응한다."""

    ok: bool = True
    codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    trial_family_id: str = ""
    trial_number: int = 1
    trials_used: int = 0
    # 막지는 않지만 사람이 봐야 하는 것. 경고를 거부로 올리면 게이트가
    # "의심스러우면 차단"이 되고, 그건 신규 가설을 영영 못 사게 만든다.
    warnings: list[str] = field(default_factory=list)

    def reject(self, code: str, why: str) -> None:
        self.ok = False
        self.codes.append(code)
        self.reasons.append(why)

    def warn(self, why: str) -> None:
        self.warnings.append(why)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "codes": list(self.codes),
                "reasons": list(self.reasons),
                "trial_family_id": self.trial_family_id,
                "trial_number": self.trial_number,
                "trials_used": self.trials_used,
                "warnings": list(self.warnings)}


def _hyp_view(proposal: dict) -> dict:
    """기획안 -> trial_family 가 읽는 모양. Family 키는 튜닝값을 안 본다.

    ▶ 기본값을 **여기서 정하지 않는다**(2026-08-11). 예전엔 label/baseline 기본값이
      이 함수에 박혀 있었고 실행면은 그 값을 몰라서, 같은 기획안이 접수와 실행에서
      서로 다른 Family 를 받았다 - 세는 장부와 각인하는 장부가 갈렸다.
    """
    semantic_plan = proposal.get("semantic_plan") or {}
    semantic_fingerprint = None
    if semantic_plan:
        try:
            from alpha_semantics import fingerprint as semantic_fp
            semantic_fingerprint = semantic_fp(semantic_plan)
        except (ImportError, ValueError):
            pass
    return hypothesis_view(edge_type=proposal.get("edge_type"),
                           universe_key=proposal.get("universe_key"),
                           label=proposal.get("label"),
                           baseline=proposal.get("baseline"),
                           research_lane=proposal.get("research_lane"),
                           semantic_fingerprint=semantic_fingerprint)


def _horizon_of(proposal: dict) -> int:
    """기획안이 요구하는 실행 워밍업 중 **가장 긴 것**(거래일).

    ▶ 왜 첫 키가 아니라 최댓값인가 (2026-08-14, 카드 t_e9534028)
      형성창과 보유창이 갈린 뒤로 기획안은 두 값을 같이 적는다. 아래 ①-d 는
      "이 창이 표본에 들어가는가" 를 묻는 검사인데, 형성 250일·보유 5일짜리
      기획안을 **보유창으로만 재면** 창이 넉넉하다고 통과시킨 뒤 walk-forward
      에서 창 0개로 죽는다 - 이 검사가 막으라고 있는 바로 그 사고다.
      긴 쪽으로 재면 통과하던 것이 막힐 수는 있어도 그 반대는 없다(개발원칙 9).
    """
    sp = proposal.get("suggested_params") or {}
    longest = 0
    for k in ("signal_window_days", "horizon_days", "holding_horizon",
              "lookback_days", "micro_window_days", "liquidity_window",
              "trend_filter_days", "vol_lookback_days"):
        v = sp.get(k)
        if v is None:
            continue
        try:
            longest = max(longest, int(v))
        except (TypeError, ValueError):
            return 0            # 비수치는 바인딩이 사유를 만든다
    req = proposal.get("data_requirements") or {}
    if isinstance(req, dict) and req.get("min_history_days") is not None:
        try:
            longest = max(longest, int(req["min_history_days"]))
        except (TypeError, ValueError):
            return 0
    expr = sp.get("signal_expr")
    if expr is not None:
        try:
            from alpha_ast import min_history as _ast_history  # noqa: PLC0415
            from alpha_ast import parse as _parse_ast

            longest = max(longest, int(_ast_history(_parse_ast(expr))))
        except Exception:  # invalid formula receives the specific Gate 0 reason below
            pass
    return longest


def _embargo_of(proposal: dict) -> int:
    """Forecast/holding horizon that the walk-forward evaluator embargoes."""
    sp = proposal.get("suggested_params") or {}
    for key in ("horizon_days", "holding_horizon", "lookback_days"):
        try:
            value = int(sp.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 1:
            return value
    return 0


def gate0(proposal: dict, *, trials_used: int = 0,
          past_outcomes: list[dict] | None = None,
          available_days: int = 0) -> Gate0Result:
    """접수 검사. **순수 함수** - DB 없이 자체 점검이 돈다.

    ▶ **시도 계수와 교훈 조회의 출처를 나눈다.**
      trials_used   = quant.experiments 의 실행 기록(호출부가 센다)
      past_outcomes = research.experiment_outcomes 의 종결 기록(교훈 대조용)

      한 곳에서 세면 안 된다. 실행됐지만 아직 종결되지 않은 실험이 있으면 환류
      기록만 세는 쪽이 적게 세고, 그러면 **예산을 넘겨서 접수된다.** DSR 감가도
      실행 횟수를 보므로 계수 기준은 실행 기록이다.

    past_outcomes: 같은 Family 의 [{decision, lesson_codes:[...]}] 목록.
    """
    r = Gate0Result()
    past = list(past_outcomes or [])
    lane = str(proposal.get("research_lane") or "DAILY_CROSS_SECTIONAL").upper()
    intraday = lane == INTRADAY_LANE
    if lane not in {"DAILY_CROSS_SECTIONAL", INTRADAY_LANE}:
        r.reject("UNMAPPED_RESEARCH_LANE", f"unsupported research_lane={lane!r}")

    # ① 통제 어휘 사상. 실행면에 없는 유형을 접수하면 실행 단계에서 죽는다 -
    #    접수는 실행 가능성의 약속이어야 한다.
    edge = str(proposal.get("edge_type") or "").strip().lower()
    if edge not in EDGE_VOCAB:
        why = NOT_IMPLEMENTED.get(edge)
        r.reject("UNMAPPED_VOCAB",
                 f"edge_type={edge!r} 을 실행면 어휘로 사상할 수 없다 - "
                 + (f"미구현: {why}" if why else f"사용 가능: {sorted(EDGE_VOCAB)}"))
    universe = str(proposal.get("universe_key") or "").strip().lower()
    if universe not in UNIVERSE_VOCAB:
        r.reject("UNMAPPED_VOCAB",
                 f"universe_key={universe!r} 가 통제 어휘 밖이다 - "
                 f"사용 가능: {sorted(UNIVERSE_VOCAB)}. 자유 서술은 같은 컨셉을 "
                 f"여러 Family 로 흩어 다중검정 가드를 무력화한다")

    # ①-a SUGGESTED_PARAMS 어휘. 막지는 않되 **접수 시점에 말해 준다.**
    #     실행면이 안 읽는 키는 `expected_edge` 에 얹히지 않고 버려지는데(아래
    #     expected_edge_for), 그 사실을 여기서 안 알리면 리서치는 자기가 적은
    #     파라미터가 반영된 줄 안다. `type` 을 여기 적어 관문 승인 어휘를
    #     덮으려 한 실측도 있었다(3bb50969) - 그건 이제 무시되지만 조용히
    #     무시하면 같은 실수를 반복한다.
    try:
        _, _dropped_params = expected_edge_for(proposal)
    except (ImportError, TypeError, ValueError) as exc:
        _dropped_params = []
        r.reject("INTRADAY_CONTRACT_INVALID" if intraday else "EXECUTION_BINDING_REJECTED",
                 str(exc))
    if _dropped_params:
        # ▶ 쓸 수 있는 키를 **손으로 적지 않는다** (2026-08-14, 카드 t_e9534028).
        #   여기 "horizon_days·top_n" 이 박혀 있었는데 그 사이 실행면은 위험
        #   손잡이·유동성·구성방식·형성창까지 열렸다. 안내문이 실행면보다 좁으면
        #   리서치는 쓸 수 있는 손잡이를 안 쓴다 - 표를 넓혀도 안 쓰이면 안 넓힌
        #   것과 같다. `EDGE_KEYS` 를 그대로 읽어 자동으로 따라오게 한다.
        if intraday:
            _usable = sorted(INTRADAY_EDGE_KEYS)
        else:
            from config_binding import EDGE_KEYS as _EDGE_KEYS   # noqa: PLC0415
            _usable = sorted(set(_EDGE_KEYS) - {"type", "universe_key"})
        r.warnings.append(
            f"SUGGESTED_PARAMS 의 {_dropped_params} 는 실행면이 읽지 않아 "
            f"등록에서 빠진다 - 쓸 수 있는 키: {_usable}. "
            f"EDGE_TYPE·UNIVERSE_KEY 는 전용 필드로만 정해진다")

    # ①-a-2 정체성 키를 파라미터 칸에 적으면 **반려한다** (2026-08-13)
    #   가설-실행 정합성 감사 실측: MAX 가설이 suggested_params 에
    #   {"type": "low_max"} 까지 명시했는데 그 키는 조용히 버려지고
    #   edge_type=low_volatility 로 실행됐다 - 에이전트의 가설은 검증된 적
    #   없이 LOWVOL 성적표를 받았다. 힌트가 **실행 가능한 어휘**인데 전용
    #   필드와 다르면, 조용한 무시는 곧 다른 가설을 검증하는 것이므로 멈춘다.
    #   어휘 밖 힌트는 노이즈일 수 있어 기존 ①-a 경고로만 알린다.
    sp = proposal.get("suggested_params") or {}
    for f, vocab, approved, dest in (("type", EDGE_VOCAB, edge, "EDGE_TYPE"),
                                     ("universe_key", UNIVERSE_VOCAB, universe,
                                      "UNIVERSE_KEY")):
        hint = str(sp.get(f) or "").strip().lower()
        if hint and hint in vocab and hint != approved:
            r.reject("IDENTITY_IN_PARAMS",
                     f"SUGGESTED_PARAMS 의 {f}={hint!r} 는 실행 가능한 어휘인데 "
                     f"전용 필드는 {approved!r} 다 - 지금 접수하면 {approved!r} 로 "
                     f"실행돼 네 가설({hint!r})은 검증되지 않는다. "
                     f"{dest} 필드로 옮겨라")

    # ①-a-3 top_n 미지정은 특별히 짚는다 - 실행 관례(기본 20)가 조용히 채우는데,
    #   IR 구조 진단(2026-08-13 실측)에서 top-20 초집중이 TC 0.114(신호 89%
    #   소실)·TE 연 34.6% 의 주범이었다. 관례가 성적을 결정하는데 그 관례는
    #   누구의 가설도 아니다. 막지 않고 정하라고 말한다.
    if not intraday and "top_n" not in sp:
        r.warn("top_n 미지정 - 실행 관례(기본 20)로 돈다. 집중도가 성적을 크게 "
               "바꾼다(실측 TC: top20 0.114 vs top200 0.316) - 가설이 직접 정하라")

    # ①-b 원천 어휘. **접수는 실행 가능성의 약속**인데 여기서 안 보면 가설이
    #     등록된 뒤 실행 단계에서 죽는다(2026-08-10 실측: 기획자가 DATA_TABLES 에
    #     `derived_returns`·`cost_scenarios` 같은 **파생 산출물**을 적었다.
    #     그건 우리가 계산할 것이지 리서치가 요구할 원천이 아니다).
    req = proposal.get("data_requirements") or {}
    tables = req.get("tables") if isinstance(req, dict) else None
    for t in (tables or []):
        # 설명이 붙어 오는 경우가 흔하다("market_bars 일봉 OHLCV"). 첫 토큰만 본다.
        name = str(t).strip().split()[0] if str(t).strip() else ""
        if name not in SOURCE_TABLES:
            r.reject("UNMAPPED_SOURCE",
                     f"data_requirements.tables 의 {name!r} 가 원천 어휘 밖이다 - "
                     f"사용 가능: {sorted(SOURCE_TABLES)}. 파생 지표(수익률·베타·"
                     f"유동성 분위·비용 시나리오)는 실행면이 원천에서 계산한다")

    # ①-c 전략 구조. 어휘 밖이면 **깎지 말고 반려한다.**
    structure = str(proposal.get("strategy_structure") or "long_only").strip().lower()
    if structure not in STRUCTURE_VOCAB:
        why = STRUCTURE_NOT_IMPLEMENTED.get(structure)
        r.reject("UNMAPPED_STRUCTURE",
                 f"strategy_structure={structure!r} 를 실행면이 표현할 수 없다 - "
                 + (why if why else f"사용 가능: {sorted(STRUCTURE_VOCAB)}")
                 + ". 표현 못 하는 구조를 빼고 돌리면 등록한 가설과 다른 실험이 된다")

    # ①-c-2 반증 실행가능성 고지 (2026-08-13 실측)
    #   에이전트는 beta 통제·섹터 통제·MAX 스프레드 같은 진짜 반증을 설계하는데
    #   실행면의 반증 모듈은 6종(비용·창 부호·베이스라인 등)만 돌린다. 그 사실을
    #   접수 때 안 알리면 에이전트는 자기 반증이 전부 걸린 줄 알고, 판정은
    #   "반증 통과" 로 오독된다. **막지 않고 알린다** - 못 도는 반증을 썼다고
    #   기획이 나쁜 게 아니고, 실행면이 자라야 할 방향의 수요 신호다.
    try:
        from falsification import classify as _fals_classify  # noqa: PLC0415

        _tests = [str(t) for t in (proposal.get("falsification_tests") or [])]
        _dead = [t[:60] for t in _tests if not _fals_classify(t)[1]]
        if _dead:
            r.warn(f"반증 {len(_tests)}개 중 {len(_dead)}개는 실행면이 못 돌린다"
                   f"(미실행으로 남고 통과로 세지 않는다): {_dead}")
    except Exception:  # noqa: BLE001 - 고지 실패가 접수를 막으면 안 된다
        pass

    # ①-d-0 파라미터 범위 (2026-08-13 실측 prop_86e535d7)
    #   max_drawdown_stop=+0.35(부호 반대)가 접수를 통과했다 - 범위 검사가
    #   실행(바인딩)에만 있어서, 가설이 **실행 불가로 태어나** 발주-사망-회수를
    #   반복할 뻔했다(667f0a45 와 같은 사고 유형). 자르거나 부호를 대신 고치지
    #   않는다 - 그러면 등록한 가설과 실행한 실험이 달라진다. 반려하고 알린다.
    try:
        from config_binding import LIMITS      # noqa: PLC0415 (실행면이 정본)

        for k, v in (() if intraday else
                     (proposal.get("suggested_params") or {}).items()):
            # 형성창·보유창 둘 다 lookback_days 자리의 한도를 받는다
            # (2026-08-14: signal_window_days 개방, 카드 t_e9534028)
            target = ("lookback_days"
                      if k in ("horizon_days", "signal_window_days") else k)
            lo, hi = LIMITS.get(target, (None, None))
            if lo is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue                # 비수치는 바인딩이 사유를 만든다
            if not (lo <= fv <= hi):
                r.reject("PARAM_OUT_OF_RANGE",
                         f"suggested_params.{k}={v} 는 실행 범위({lo}~{hi}) 밖이다"
                         + (". max_drawdown_stop 은 음수다(-0.35 = 고점 대비 "
                            "-35% 에서 전량 현금)"
                            if k == "max_drawdown_stop" and fv > 0 else "")
                         + ". 자르지 않고 반려한다 - 값을 고쳐 다시 내라")
    except ImportError:                 # 검사 불능 != 접수 차단. 다만 조용히
        r.warn("config_binding 을 읽지 못해 파라미터 범위를 접수에서 못 봤다")

    # ①-d-1 숫자 범위만으로는 실행 가능성을 보장하지 못한다. 선택형 어휘와
    # 손잡이 조합까지 실행면 자체에 묻는다. 2026-08-15 실측에서
    # rebalance=EVERY_2_TRADING_DAYS가 Gate 0를 통과해 가설로 승격된 뒤 발주
    # 관문에서 처음 거부됐다(지금은 실제 실행 정책으로 구현). 같은 bind()를
    # 접수 시점에 부르면 다른 미구현 주기도 "등록됐지만
    # 영원히 실행되지 않는 가설"을 만들지 않는다.
    try:
        from config_binding import rejection_reasons as _binding_rejections

        if not intraday:
            _edge, _ = expected_edge_for(proposal)
            for _why in _binding_rejections({"expected_edge": _edge}):
                r.reject("EXECUTION_BINDING_REJECTED", _why)
    except ImportError:
        r.warn("config_binding 을 읽지 못해 실행면 어휘·조합 검사를 못 했다")

    # ①-c2 **알파 수식(AST)은 접수에서 검증한다** (2026-08-14)
    #   수식은 실행면이 신호로 삼는 것이라, 성립하지 않으면 그 기획안은
    #   실험을 만들어 놓고 중간에 죽는다. 접수는 실행 가능성의 약속이므로
    #   여기서 판정한다 - 연산자·필드·창·복잡도까지 `alpha_ast.parse` 가 본다.
    _expr = (proposal.get("suggested_params") or {}).get("signal_expr")
    if _expr is not None and not intraday:
        try:
            from alpha_ast import check_alignment as _ca  # noqa: PLC0415
            from alpha_ast import needs_micro as _nm
            from alpha_ast import fields_of as _fo
            from alpha_ast import parse as _pe

            _parsed = _pe(_expr)
            # 무엇을 읽는 수식인지 접수 기록에 남긴다 - 나중에 "이 알파가
            # 어떤 데이터로 나왔나" 를 묻는 자리가 여기다.
            r.warn(f"알파 수식 접수: 필드 {sorted(_fo(_parsed))}"
                   f"{' · 미시구조 필요' if _nm(_parsed) else ''}")
            if "order_flow_imbalance" in _fo(_parsed):
                # The persisted name predates the AST.  Its implementation is
                # signed trade quantity / total quantity; it is not quote-event
                # OFI and does not measure order-sign persistence.  Disclose the
                # semantic loss before a literature claim is attached to it.
                r.warn("order_flow_imbalance 는 호가 신규·취소 기반 OFI가 아니라 "
                       "체결 방향×수량 / 총체결량인 legacy 필드다. Cont식 호가 OFI나 "
                       "주문분할 지속성 가설의 직접 검정으로 해석하지 않는다")
            if _nm(_parsed):
                # ▶ **표본이 짧아진다는 사실을 접수에서 말한다** (2026-08-14)
                #   미시구조는 일봉(2016~)보다 훨씬 짧다. 실행면이 표본을 그
                #   커버리지로 자르므로(clip_to_micro_coverage), 형성·보유 창을
                #   길게 잡으면 창이 몇 개 안 나온다. 첫 수식형 알파가 이 사실을
                #   모른 채 10년 표본으로 돌아 초과 -102%p 를 냈다.
                r.warn("미시구조 수식은 표본이 그 데이터 커버리지로 잘린다"
                       "(일봉 2016~ 이 아니라 호가·체결이 있는 기간만). "
                       "형성·보유 창을 짧게 잡아야 창이 나온다")

            # ①-c3 **가설과 수식이 같은 이야기인가** (2026-08-14)
            #   수식은 실행면이 그대로 쓰지만, 결과를 해석하는 것은 논리다.
            #   둘이 다른 이야기면 실험이 무엇을 검증했는지 아무도 모른다 -
            #   성적이 좋아도 그 논리의 증거가 아니고, 나빠도 그 논리의 반증이
            #   아니다. 원장에는 "매수 압력 가설 REJECT" 로 남는데 실제로는
            #   종가만 봤을 수 있다.
            #
            #   **결정론으로만 본다.** LLM 자기검증은 문헌상 성능을 떨어뜨리고
            #   (self-critique 성능 붕괴), 자기선호 편향이 -38%~+90% 다 -
            #   리서치가 자기 수식을 채점하면 통과율만 오른다. 그래서 기계가
            #   확실히 아는 것만 본다: 읽는 필드가 논리에 등장하는가.
            _align = _ca(_parsed, " ".join(str(proposal.get(k) or "") for k in
                                           ("economic_rationale", "counterparty",
                                            "hypothesis", "title")))
            if not _align["ok"]:
                r.reject("HYPOTHESIS_FACTOR_MISMATCH",
                         f"{_align['note']}. 실험은 수식대로 돌지만 판정은 "
                         f"논리 이름으로 원장에 남는다 - 둘이 다르면 그 결과는 "
                         f"무엇의 증거도 아니다. 수식을 논리에 맞추거나, "
                         f"논리를 수식이 실제로 재는 것으로 다시 써라")
            elif _align["unmentioned"]:
                r.warn(f"수식 정합 경고: {_align['note']}")
        except ImportError:
            r.warn("alpha_ast 를 읽지 못해 수식을 접수에서 못 봤다")
        except Exception as _e:  # noqa: BLE001 - 사유를 그대로 싣는다
            r.reject("SIGNAL_EXPR_INVALID",
                     f"알파 수식이 성립하지 않는다: {_e}. 실행면이 신호로 삼는 "
                     f"것이라 이대로 접수하면 실험이 중간에 죽는다 - 고쳐서 다시 내라")

    # ①-d 설계 실현 가능성. **돌리기 전에 알 수 있는 산수는 접수에서 한다.**
    #   126일 형성 창이 634거래일 표본에 안 들어간다는 것은 백테스트를 다 돌린 뒤
    #   walk-forward 에서야 창 0개로 드러났다(2026-08-11 실측). 실험 한 번을
    #   통째로 버린 셈이다 - 어휘·예산은 접수에서 보면서 이건 안 봤다.
    if intraday:
        try:
            from alpha_semantics import (check_observables, fingerprint,
                                         lane_of, validate)
            from intraday_alpha_ast import (conditional_fields_of, fields_of,
                                            operators_of, parse)

            plan = validate(proposal.get("semantic_plan") or {})
            if lane_of(plan) != INTRADAY_LANE:
                raise ValueError("semantic plan is not intraday")
            iexpr = parse(sp.get("intraday_signal_expr"))
            # Bind every controlled runtime knob at Gate 0 so a bad fee/latency
            # range cannot consume a trial and fail only inside the worker.
            edge_for_runtime, _ = expected_edge_for(proposal)
            from intraday_experiment_runner import config_from_edge
            config_from_edge(edge_for_runtime)
            alignment = check_observables(
                plan, fields_of(iexpr), operators=operators_of(iexpr),
                conditional_fields=conditional_fields_of(iexpr))
            if not alignment["ok"]:
                r.reject("SEMANTIC_FORMULA_MISMATCH", "; ".join(alignment["missing"]))
            if int(sp.get("horizon_seconds", plan["horizon_seconds"])) != plan["horizon_seconds"]:
                r.reject("SEMANTIC_HORIZON_MISMATCH",
                         "semantic_plan and suggested_params use different horizons")
            if str(sp.get("execution") or plan["execution"]).upper() != plan["execution"]:
                r.reject("SEMANTIC_EXECUTION_MISMATCH",
                         "semantic_plan and suggested_params use different execution models")
            r.warn(f"intraday AST accepted: semantic_family={fingerprint(plan)} "
                   f"fields={sorted(fields_of(iexpr))}")
        except (ImportError, TypeError, ValueError) as exc:
            r.reject("INTRADAY_CONTRACT_INVALID", str(exc))

    horizon = 0 if intraday else _horizon_of(proposal)
    avail = int(available_days or 0)
    if horizon and avail:
        usable = max(0, avail - horizon)
        if avail < SHORT_SAMPLE_MAX_DAYS:
            test_span = SHORT_MIN_TEST_DAYS + _embargo_of(proposal)
            windows = min(SHORT_TARGET_WINDOWS, usable // max(test_span, 1))
        else:
            windows = usable // max(horizon, 1)
        if windows < MIN_WF_WINDOWS:
            r.reject("UNDERPOWERED_DESIGN",
                     f"신호·위험관리 워밍업 {horizon}일을 표본 {avail}거래일에 태우면 "
                     f"walk-forward 창이 {max(windows, 0)}개다(최소 {MIN_WF_WINDOWS}) - "
                     f"돌려도 강건성을 못 재고 INCONCLUSIVE 로 끝난다. "
                     f"창을 줄이거나 더 긴 이력을 요구하라")

    # ② 시도 압력. 어휘가 안 잡히면 Family 도 못 만든다.
    fam = family_id(_hyp_view(proposal)) if r.ok else ""
    budget = int(proposal.get("trial_budget") or 5)
    used = max(0, int(trials_used))
    p = pressure(fam, [{"trial_family_id": fam} for _ in range(used)], budget=budget)
    r.trial_family_id = p["trial_family_id"]
    r.trial_number = int(p["trial_number"])
    r.trials_used = int(p["trials_used"])
    if p.get("over_budget"):
        r.reject("OVER_BUDGET",
                 f"이 계열에서 이미 {r.trials_used}회 시도했다(예산 {budget}) - "
                 f"증액은 CEO 결정이 필요하다")

    # ③ 기각 이력 대응. **회사가 이미 산 실험을 다시 사지 않는다.**
    #
    # ▶ 전제: "이미 샀어야" 이 규칙이 성립한다 (2026-08-11 재설계)
    #   환류의 목적은 **같은 실험을 두 번 사지 않는 것**이다. 그런데 이 검사가
    #   `trials_used` 를 보지 않아서, **이 계열에서 한 번도 실행된 적이 없는데도**
    #   교훈이 있다는 이유로 막았다(실측: `기존 실행 0, 환류 1` 인데
    #   DUPLICATE_UNADDRESSED). 그건 중복 방지가 아니라 **신규 차단**이고,
    #   아직 아무도 해보지 않은 가설이 영영 실험되지 못하는 교착이 된다.
    #   가설은 실험해서 경험을 쌓아야 하고, 막는 것은 그 경험이 생긴 뒤다.
    needed: set[str] = set()
    for o in past:
        if str(o.get("decision")) in REJECTING:
            needed.update(str(c) for c in (o.get("lesson_codes") or []))
    prior = proposal.get("prior_check") or {}
    answered = set((prior.get("lessons_addressed") or {}).keys())
    missing = sorted(needed - answered)
    exact_ast_reuse = any(o.get("match_scope") == "AST_EXACT" for o in past)
    if missing and (r.trials_used > 0 or exact_ast_reuse):
        # ▶ **"대응이 없다" 가 사실이 아닐 수 있다** (2026-08-13 실측)
        #   `prop_5682` 는 교훈 5개를 이름으로 짚어 대응을 적었다 - 다만 카드가
        #   `ECONOMIC_RATIONALE 에 적어라` 라고 시켜서 거기 적었고, 계약은
        #   `LESSONS_ADDRESSED:` 만 읽는다. 그런데 반려문은 "대응이 없다" 였다.
        #   **거짓 사유는 고칠 수 없는 사유다** - 기획자는 자기가 적은 것을
        #   보면서 무엇이 문제인지 알 수 없다. 카드 문구는 고쳤고, 여기서는
        #   본문에 답이 있는 경우를 가려내 **어디로 옮기라고** 말해 준다.
        body = " ".join(str(proposal.get(k) or "") for k in
                        ("economic_rationale", "counterparty",
                         "competing_explanation"))
        in_body = sorted(c for c in missing if c in body)
        if in_body:
            r.reject("LESSONS_IN_WRONG_FIELD",
                     f"이 계열에서 이미 {r.trials_used}회 실행하고 기각됐다. "
                     f"대응을 **본문에는 적었는데**({in_body}) 계약이 읽는 칸은 "
                     f"`LESSONS_ADDRESSED:` 다 - 같은 내용을 "
                     f"`교훈코드=무엇을 다르게 하는가` 형식으로 그 칸에 옮겨라. "
                     f"본문은 그대로 두면 된다"
                     + (f". 본문에도 없는 것: {sorted(set(missing) - set(in_body))}"
                        if set(missing) - set(in_body) else ""))
        else:
            scope = ("동일 AST가 다른 이름·계열을 포함해 이미 실행되고 부정 종결됐다"
                     if exact_ast_reuse and r.trials_used == 0 else
                     f"이 계열에서 이미 {r.trials_used}회 실행하고 기각됐다")
            r.reject("AST_DUPLICATE_UNADDRESSED" if exact_ast_reuse
                     else "DUPLICATE_UNADDRESSED",
                     f"{scope} - 그 교훈에 대응이 없다: {missing}")
    elif missing:
        # 실행 0인데 기각 교훈이 있다 = 원장이 어긋났다는 신호다(다른 계열의
        # 결과가 섞였거나 정리가 덜 됐다). **막지 않고 알린다** - 여기서 막으면
        # 데이터 불일치가 곧 신규 실험 금지가 된다.
        r.warn(
            f"이 계열의 실행 기록은 0인데 기각 교훈이 있다: {missing}. "
            "원장 정합성을 확인하되, 아직 실행된 적이 없으므로 접수는 막지 않는다."
        )
    return r


def expected_edge_for(proposal: dict) -> tuple[dict, list]:
    """기획안 -> `expected_edge`. 반환: (edge, 버려진 파라미터 키).

    ▶ **관문이 승인한 값이 이겨야 한다** (2026-08-12 실측)
      예전에는 이렇게 만들었다:

        edge = {"type": …edge_type, "universe_key": …, **suggested_params}

      전개가 **뒤에** 있어서 `suggested_params` 안의 `type` 이 Gate 0 이 검증하고
      통과시킨 `edge_type` 을 덮었다. 실측(`3bb50969`):

        기획안 edge_type        = mean_reversion        ← 관문 통과
        기획안 suggested_params = {"type": "short_term_reversal", …}
        가설  expected_edge     = {"type": "short_term_reversal", …}  ← 이게 남았다

      **관문이 승인한 값과 원장에 남는 값이 달랐다.** 어휘 검사가 장식이 된다.
      그래서 정체성 키(type·universe_key)를 **맨 뒤에** 둬서 덮이지 않게 한다.

    ▶ 실행면이 안 읽는 키는 얹지 않는다
      `config_binding.EDGE_KEYS` 밖의 키가 `expected_edge` 에 들어가면 실행
      단계에서 "등록한 가설과 실행한 실험이 달라진다"며 **거부된다**. 실측:
      `667f0a45` 가 `signal_window_days`·`walk_forward_window_days` 로 죽었고,
      스톨 회수로 다시 발주해도 같은 자리에서 또 죽었다 - 가설이 만들어질 때
      이미 실행 불가로 태어난 것이다. 접수는 실행 가능성의 약속이어야 한다.

      **후속(2026-08-14, 카드 t_e9534028)**: 그 두 키의 처분이 갈렸다.
      `signal_window_days` 는 형성창으로 **실행면에 열렸고**(EDGE_KEYS),
      `walk_forward_window_days` 는 창 분할 = 사전등록 정책이라 실행 손잡이가
      되지 않았다(NON_EXECUTION_KEYS). 그래서 여기서 떨어지는 것은 후자뿐이다.
    """
    lane = str(proposal.get("research_lane") or "DAILY_CROSS_SECTIONAL").upper()
    params = proposal.get("suggested_params") or {}
    if lane == INTRADAY_LANE:
        from alpha_semantics import fingerprint as semantic_fp
        from alpha_semantics import validate as validate_semantics
        from intraday_alpha_ast import parse as parse_intraday

        plan = validate_semantics(proposal.get("semantic_plan") or {})
        kept = {key: value for key, value in params.items()
                if key in INTRADAY_EDGE_KEYS}
        if "intraday_signal_expr" in kept:
            kept["intraday_signal_expr"] = parse_intraday(kept["intraday_signal_expr"])
        dropped = sorted(key for key in params if key not in INTRADAY_EDGE_KEYS)
        return ({**kept,
                 "type": proposal.get("edge_type"),
                 "universe_key": proposal.get("universe_key"),
                 "research_lane": lane,
                 "semantic_plan": plan,
                 "semantic_fingerprint": semantic_fp(plan)}, dropped)

    from config_binding import EDGE_KEYS      # noqa: PLC0415 (실행면이 정본)

    tunable = set(EDGE_KEYS) - {"type", "universe_key"}
    kept = {k: v for k, v in params.items() if k in tunable}
    dropped = sorted(k for k in params if k not in tunable)
    # 정체성 키를 마지막에 - suggested_params 가 덮을 수 없다.
    edge = {**kept,
            "type": proposal.get("edge_type"),
            "universe_key": proposal.get("universe_key")}
    return edge, dropped


def mapping_loss_of(proposal: dict) -> dict:
    """가설→실행 번역에서 무엇이 떨어지는지 **접수 시점에 계산해 각인한다.**

    ▶ 왜 (2026-08-13 가설-실행 정합성 감사)
      실험이 돈 가설 41개가 서로 다른 config 19개로 접혔다. SMA20 매도 4건이
      전부 REV-5(반대 방향)로, MAX 가설이 LOWVOL 로 실행됐는데 **원장 어디에도
      번역에서 무엇이 사라졌는지 남지 않았다.** 판정은 남은 것만 보고 "이 가설은
      기각"이라 적었다 - 검증된 적 없는 가설에 성적표가 붙은 것이다. 경고는
      접수 응답과 함께 흘러가지만 각인은 판정·회고 때도 남아 있다.

    반환(전부 기계 계산, 빈 dict = 무손실):
      dropped_keys   - 실행면이 안 읽어 버려진 파라미터 키
      defaulted_keys - 가설이 안 정해 실행 관례가 채울 손잡이
      identity_hints - 파라미터 칸에 적힌 정체성 값 중 전용 필드와 다른 것
    """
    lane = str(proposal.get("research_lane") or "DAILY_CROSS_SECTIONAL").upper()
    if lane == INTRADAY_LANE:
        params = proposal.get("suggested_params") or {}
        _edge, dropped = expected_edge_for(proposal)
        loss = {}
        if dropped:
            loss["dropped_keys"] = dropped
        missing = sorted(INTRADAY_EDGE_KEYS - set(params))
        if missing:
            loss["defaulted_keys"] = missing
        return loss

    from config_binding import EDGE_KEYS      # noqa: PLC0415 (실행면이 정본)

    tunable = set(EDGE_KEYS) - {"type", "universe_key"}
    params = proposal.get("suggested_params") or {}
    edge, dropped = expected_edge_for(proposal)
    defaulted = sorted(k for k in tunable if edge.get(k) is None)
    hints = {}
    for f, approved in (("type", proposal.get("edge_type")),
                        ("universe_key", proposal.get("universe_key"))):
        hv = str(params.get(f) or "").strip().lower()
        if hv and hv != str(approved or "").strip().lower():
            hints[f] = hv
    loss: dict = {}
    if dropped:
        loss["dropped_keys"] = dropped
    if defaulted:
        loss["defaulted_keys"] = defaulted
    if hints:
        loss["identity_hints"] = hints
    return loss


def to_hypothesis_row(proposal: dict, gate: Gate0Result, *,
                      created_by: str = "factory-bridge") -> dict:
    """기획안 -> quant.hypotheses INSERT 페이로드.

    사전등록 시점의 근거를 **복사해 둔다** - 기획안이 나중에 수정돼도 이 실험이
    무엇을 등록했는지는 변하면 안 된다(사전등록의 의미가 그것이다).
    """
    edge, _dropped = expected_edge_for(proposal)
    return {
        "title": f"[{proposal.get('edge_type')}] {proposal.get('universe_key')}",
        "rationale": proposal.get("economic_rationale") or "",
        "expected_edge": edge,
        "falsification_criteria": list(proposal.get("falsification_tests") or []),
        "required_data_products": proposal.get("data_requirements") or {},
        "status": "PROPOSED",
        "created_by": created_by,
        # ── 계보 (2026-08-10 신설 컬럼) ──
        "proposal_id": proposal.get("proposal_id"),
        "lead_ids": list(proposal.get("lead_ids") or []),
        "research_packet_ids": list(proposal.get("research_packet_ids") or []),
        "claim_ids": list(proposal.get("claim_ids") or []),
        "economic_rationale": proposal.get("economic_rationale"),
        "counterparty": proposal.get("counterparty"),
        "competing_explanation": proposal.get("competing_explanation"),
        "competing_explanation_codes": list(
            proposal.get("competing_explanation_codes") or []),
        "skeptic_sign": proposal.get("skeptic_sign"),
        "source_reported_effect": proposal.get("source_reported_effect") or {},
        "trial_family_id": gate.trial_family_id,
        "trial_number": gate.trial_number,
        # 번역 손실 각인 - 판정·회고가 "무엇이 검증됐고 무엇이 관례였나"를 본다
        "mapping_loss": mapping_loss_of(proposal),
    }


def outcome_id_for(experiment_id: str, decision: str) -> str:
    """같은 실험의 같은 판정은 한 번만 적재된다(재실행 멱등)."""
    blob = f"{experiment_id}|{decision}"
    return "out_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_outcome(*, experiment_id: str, hypothesis_id: str, trial_family_id: str,
                  trial_number: int, decision: str,
                  failed_criteria=(), lesson_codes=(), oos_summary=None,
                  regime_concerns=(), notes: str = "",
                  proposal_id: str = "") -> dict:
    """환류 행. **미측정 지표는 키를 넣지 않는다** - 0 으로 채우면 관문이 통과로 읽는다."""
    if decision in REJECTING and not (failed_criteria or lesson_codes):
        raise ValueError(
            f"{decision} 인데 사유가 없다 - 사유 없는 기각은 다음 기획안이 "
            f"대조할 것이 없다(환류가 성립하지 않는다)")
    clean = {k: v for k, v in (oos_summary or {}).items() if v is not None}
    return {
        "outcome_id": outcome_id_for(experiment_id, decision),
        "experiment_id": experiment_id, "hypothesis_id": hypothesis_id,
        "trial_family_id": trial_family_id, "trial_number": int(trial_number),
        "decision": decision, "proposal_id": proposal_id,
        "decided_at": datetime.now(timezone.utc),
        "failed_criteria": list(failed_criteria), "oos_summary": clean,
        "regime_concerns": list(regime_concerns), "lesson_codes": list(lesson_codes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# DB 경로 (자체 점검은 conn 주입으로 DB 없이 돈다)
# ---------------------------------------------------------------------------

_SQL_PUBLISHED = """
    select p.proposal_id, p.edge_type, p.universe_key, p.label, p.baseline,
           p.economic_rationale, p.counterparty, p.competing_explanation,
           p.competing_explanation_codes, p.skeptic_sign, p.lead_ids,
           p.falsification_tests, p.data_requirements, p.suggested_params,
           p.trial_budget, p.prior_check, p.source_reported_effect,
           p.research_packet_ids, p.claim_ids, p.research_lane, p.semantic_plan
      from research.experiment_proposals p
     where p.status = 'PUBLISHED'
       and not exists (select 1 from quant.hypotheses h
                        where h.proposal_id = p.proposal_id)
     order by p.as_known_at
     limit %s
"""
# ▶ 이미 승격된 기획안을 창에서 빼는 not exists 가 생명선이다 (2026-08-13
#   실측, 카드 t_7cd9bd5f). 승격돼도 status 는 영원히 PUBLISHED 로 남는데
#   창은 "가장 오래된 limit 건" 이라, 오래된 승격분 18건 + 영구 게이트거부
#   2건이 창을 다 채운 시점부터 새 기획안은 fetch 자체가 안 됐다(PUBLISHED
#   23건 중 21~23위 3건이 어떤 주기에도 승격·거부 판정을 받지 못했다).
#   리서치가 아무리 발행해도 공장에 신규 가설 공급이 0 이 되는 조용한 정지다.
#   게이트거부분은 quant.hypotheses 에 없어 창에 남는다 - 종결 상태 각인
#   (PROMOTED/GATE_REJECTED 등)은 원장 상태 어휘라 별도 합의 사항(카드의
#   B안). 거부가 20건 쌓이면 같은 병이 재발하므로 그때는 B안이 필요하다.

_SQL_FAMILY_OUTCOMES = """
    select decision, lesson_codes
      from research.experiment_outcomes
     where trial_family_id = %s
     order by decided_at desc
"""

_SQL_EXACT_AST_OUTCOMES = """
    select o.decision, o.lesson_codes
      from research.experiment_outcomes o
     where o.experiment_id = any(
           select e.experiment_id::text
             from quant.experiments e
            where e.config->'signal_expr' = %s::jsonb
               or e.config->'intraday_signal_expr' = %s::jsonb)
     order by o.decided_at desc
"""

# ▶ **한 실험에는 판정 하나** (2026-08-14 실측). outcome_id 는
#   (experiment_id, decision) 해시라 멱등이 **판정별로만** 작동했다 - 같은
#   실험에 REJECT 와 GATE_HOLD 가 나란히 앉는 것을 막지 못한다(e820053a 실측:
#   고아 소탕기와 정규 판정 사슬이 경합). 판정이 둘이면 "이 실험은 어떻게
#   끝났나" 에 답이 두 개이고, 교훈 집계·파레토·시도 계수가 전부 이중으로 센다.
#   먼저 온 판정이 정본이다 - 나중 것은 조용히 버리지 않고 호출부가 알도록
#   0행 반환으로 드러낸다(아래 finalize 가 그 사실을 로그로 남긴다).
_SQL_INSERT_OUTCOME = """
    insert into research.experiment_outcomes
      (outcome_id, experiment_id, hypothesis_id, trial_family_id, trial_number,
       decision, decided_at, proposal_id, failed_criteria, oos_summary,
       regime_concerns, lesson_codes, notes, root_cause, corrective_action)
    select %(outcome_id)s, %(experiment_id)s, %(hypothesis_id)s,
           %(trial_family_id)s, %(trial_number)s, %(decision)s, %(decided_at)s,
           coalesce(nullif(%(proposal_id)s, ''),
                    (select h.proposal_id
                       from quant.hypotheses h
                      where h.hypothesis_id::text = %(hypothesis_id)s), ''),
           %(failed_criteria)s, %(oos_summary)s,
           %(regime_concerns)s, %(lesson_codes)s, %(notes)s,
           %(root_cause)s, %(corrective_action)s
     where not exists (
             select 1 from research.experiment_outcomes o
              where o.experiment_id = %(experiment_id)s)
    on conflict (outcome_id) do nothing
"""


def fetch_published_proposals(conn, limit: int = 20) -> list[dict]:
    cols = ("proposal_id edge_type universe_key label baseline economic_rationale "
            "counterparty competing_explanation competing_explanation_codes "
            "skeptic_sign lead_ids falsification_tests data_requirements "
            "suggested_params trial_budget prior_check source_reported_effect "
            "research_packet_ids claim_ids research_lane semantic_plan").split()
    with conn.cursor() as cur:
        cur.execute(_SQL_PUBLISHED, (limit,))
        return [dict(zip(cols, row)) for row in cur.fetchall()]


_SQL_FAMILY_TRIALS = "select count(*) from quant.experiments where trial_family_id = %s"


def count_family_trials(conn, trial_family_id: str) -> int:
    """이 Family 에서 **실제로 실행된** 실험 수. 예산과 DSR 이 같은 값을 본다."""
    if not trial_family_id:
        return 0
    with conn.cursor() as cur:
        cur.execute(_SQL_FAMILY_TRIALS, (trial_family_id,))
        return int(cur.fetchone()[0])


def _checkable_regime_evidence(values) -> list:
    """Keep only regime observations with an auditable label and span/count."""
    out = []
    for value in values or ():
        if isinstance(value, dict):
            label = value.get("regime_label") or value.get("label") or value.get("regime")
            window = value.get("window") or value.get("window_label") or value.get("period")
            count = value.get("count") or value.get("window_count") or value.get("observations")
            if label and (window or count is not None):
                out.append(value)
            continue
        text = str(value)
        if re.search(r"(?:regime|국면)\s*[:=]\s*[^,; ]+", text, re.I) and \
           re.search(r"(?:window|기간|count|개수)\s*[:=]\s*[^,; ]+", text, re.I):
            out.append(text)
    return out


def _check_signal_expr_is_gated_at_intake():
    """**수식은 접수에서 판정한다** (2026-08-14).

    실행면이 신호로 삼는 것이라, 성립하지 않는 수식을 접수하면 그 기획안은
    실험을 만들어 놓고 중간에 죽는다 - 원장에 반쪽짜리 흔적이 남는다.
    접수는 실행 가능성의 약속이어야 한다.
    """
    # 논리는 수식이 실제로 읽는 것을 말해야 한다(아래 ①-c3 정합 검사) - 예전
    # 이 픽스처는 "x"/"y" 였고, 그래서 새 검사가 붙었는데도 이 점검은 통과했다.
    base = {"edge_type": "momentum", "universe_key": "krx_all",
            "economic_rationale": "호가 매수 압력이 크고 스프레드가 좁은 종목이 "
                                  "이후 초과수익을 낸다",
            "counterparty": "유동성을 급히 요구하는 청산 매매",
            "falsification_tests": ["IC t<2 면 기각"], "trial_budget": 5}

    ok_expr = {"op": "sub", "args": [
        {"op": "rank", "arg": {"op": "ts_mean",
                               "field": "order_flow_imbalance", "n": 3}},
        {"op": "rank", "arg": {"op": "ts_mean", "field": "spread_bps", "n": 10}}]}
    g = gate0(dict(base, suggested_params={"horizon_days": 2, "top_n": 200,
                                           "signal_expr": ok_expr}))
    assert g.ok, g.as_dict()          # codes 만 보면 다른 코드로 막힌 걸 놓친다
    assert "SIGNAL_EXPR_INVALID" not in g.codes, g.as_dict()
    # 무엇을 읽는지 접수 기록에 남는다
    assert any("알파 수식 접수" in str(w) for w in (g.warnings or [])), g.as_dict()
    assert any("미시구조" in str(w) for w in (g.warnings or [])), g.as_dict()
    assert any("호가 신규·취소 기반 OFI가 아니라" in str(w)
               for w in (g.warnings or [])), g.as_dict()

    # 모르는 연산자·필드·범위 밖 창은 **접수에서** 막힌다
    for bad in ({"op": "magic", "field": "close", "n": 5},
                {"op": "ts_mean", "field": "pe_ratio", "n": 5},
                {"op": "ts_mean", "field": "close", "n": 9999}):
        gb = gate0(dict(base, suggested_params={"horizon_days": 2,
                                                "signal_expr": bad}))
        assert not gb.ok and "SIGNAL_EXPR_INVALID" in gb.codes, (bad, gb.as_dict())

    # 수식이 없는 기획안은 예전 그대로 - 새 검사가 기존 접수를 막지 않는다
    g2 = gate0(dict(base, suggested_params={"horizon_days": 20, "top_n": 20}))
    assert "SIGNAL_EXPR_INVALID" not in g2.codes, g2.as_dict()

    # 접수된 수식이 가설까지 그대로 간다 - 중간에 떨어지면 실행면이 못 본다
    edge, dropped = expected_edge_for(
        dict(base, suggested_params={"horizon_days": 2, "signal_expr": ok_expr}))
    assert "signal_expr" in edge, (edge, dropped)


def _check_hypothesis_and_factor_tell_the_same_story():
    """**논리와 수식이 다른 이야기면 접수에서 되돌린다** (2026-08-14).

    수식은 실행면이 그대로 쓰지만, 결과를 해석하는 것은 논리다. 둘이 다르면
    원장에는 "매수 압력 가설 REJECT" 로 남는데 실제로 잰 것은 종가일 수 있다 -
    그 판정은 무엇의 증거도 아니고, 다음 기획안이 그 교훈을 읽고 엉뚱한 곳을
    고친다.

    **결정론으로만 본다.** LLM 자기검증은 문헌상 성능을 떨어뜨리고 자기선호
    편향이 -38%~+90% 라, 리서치가 자기 수식을 채점하면 통과율만 오른다.
    """
    ofi = {"op": "ts_mean", "field": "order_flow_imbalance", "n": 3}
    base = {"edge_type": "momentum", "universe_key": "krx_all",
            "falsification_tests": ["IC t<2 면 기각"], "trial_budget": 5,
            "suggested_params": {"horizon_days": 2, "signal_expr": ofi}}

    def _g(rationale, counterparty="유동성 수요자"):
        return gate0(dict(base, economic_rationale=rationale,
                          counterparty=counterparty))

    # ① 같은 이야기면 통과한다
    assert _g("호가 매수 압력이 이후 수익을 예측한다").ok

    # ② 다른 이야기는 **막는다** - 이것이 이 검사의 존재 이유다
    bad = _g("저PBR 종목이 장기적으로 초과수익을 낸다", "가치를 무시하는 투자자")
    assert not bad.ok and "HYPOTHESIS_FACTOR_MISMATCH" in bad.codes, bad.as_dict()
    # 사유에 무엇이 어긋났는지 남는다 - 고칠 수 없는 반려는 소음이다
    assert any("order_flow_imbalance" in str(x) for x in (bad.reasons or [])), \
        bad.as_dict()

    # ③ 빈 논리로 수식만 던지는 것도 막는다
    assert not _g("", "").ok

    # ④ **표현 차이로 죽이지 않는다** - 좁은 어휘는 멀쩡한 가설을 막는다.
    #    영어 논리, 그리고 필드 일부만 언급한 경우는 통과(경고)여야 한다.
    assert _g("order flow imbalance predicts short-term returns").ok
    two = {"op": "sub", "args": [
        {"op": "rank", "arg": ofi},
        {"op": "rank", "arg": {"op": "ts_mean", "field": "spread_bps", "n": 10}}]}
    part = gate0(dict(base, economic_rationale="매수 압력이 크면 이후 오른다",
                      counterparty="청산 매매",
                      suggested_params={"horizon_days": 2, "signal_expr": two}))
    assert part.ok, part.as_dict()
    assert any("정합 경고" in str(w) for w in (part.warnings or [])), part.as_dict()

    # ⑤ 수식 없는 기존 기획안은 이 검사를 통과할 필요가 없다 - 새 검사가
    #    옛 접수 경로를 막으면 공장이 선다
    assert gate0({"edge_type": "momentum", "universe_key": "krx_all",
                  "economic_rationale": "장기 추세는 이어진다",
                  "counterparty": "과소반응 투자자",
                  "falsification_tests": ["IC t<2"], "trial_budget": 5,
                  "suggested_params": {"horizon_days": 20, "top_n": 20}}).ok


def _check_no_trade_does_not_teach_performance_lessons():
    """**한 주도 안 샀으면 이기고 지고가 없다** (2026-08-14 실측).

    유니버스가 비어 거래 0 인 실험의 초과가 -82.86%p 로 찍혔고, 그대로면
    환류에 BASELINE_NOT_BEATEN 이 실린다 - 다음 기획안은 그것을 읽고
    "기준선을 이기도록" 사양을 고친다. 고칠 것은 사양이 아니라 유니버스다.
    """
    empty = lessons_from(oos_summary={
        "turnover_total": 0.0, "total_return": 0.0,
        "excess_return_pct": -82.86, "max_drawdown": 0.0})
    assert "UNDERPOWERED_DATA" in empty, empty
    assert "BASELINE_NOT_BEATEN" not in empty, \
        f"거래 0 인데 기준선을 못 이겼다고 가르친다: {empty}"
    assert "BEAR_FRAGILE" not in empty, empty

    # 진짜로 돌아서 진 실험은 그대로 배운다 - 놓아주기가 아니다.
    real = lessons_from(oos_summary={
        "turnover_total": 4.2, "total_return": -0.12,
        "excess_return_pct": -18.4, "max_drawdown": -0.42})
    assert "BASELINE_NOT_BEATEN" in real, real
    assert "BEAR_FRAGILE" in real, real
    assert "UNDERPOWERED_DATA" not in real, real

    # 회전율을 못 잰 옛 실험은 예전과 똑같이 판단한다(무더기 무효화 방지)
    legacy = lessons_from(oos_summary={"excess_return_pct": -18.4})
    assert "BASELINE_NOT_BEATEN" in legacy, legacy
    assert "UNDERPOWERED_DATA" not in legacy, legacy


def lessons_from(*, failed_criteria=(), regime_concerns=(),
                 fragility: str = "", oos_summary=None) -> list[str]:
    """판정 재료 -> 통제 어휘 교훈. **결정론 기본값이다.**

    LLM 워커(QNT-04)가 이 위에 서술을 얹을 수 있지만, 얹지 않아도 루프는 돈다 -
    에이전트가 없으면 교훈이 안 남는 구조면 **환류가 에이전트 가용성에 묶인다.**
    """
    out: list[str] = []
    f = {str(x).lower() for x in failed_criteria}
    if any("pbo" in x for x in f):
        out.append("OVERFIT_PBO")
    if any("deflated_sharpe" in x for x in f):
        out.append("OVERFIT_DSR")
    if any("excess_return" in x or "information_ratio" in x for x in f):
        out.append("BASELINE_NOT_BEATEN")
    if any("turnover" in x for x in f):
        out.append("COST_SENSITIVE")
    if any("drawdown" in x for x in f):
        out.append("BEAR_FRAGILE")
    # ▶ **관문을 못 거쳐도 지표가 말하는 것은 남긴다** (2026-08-10 실측)
    #   walk-forward 창이 0개라 종결했더니 교훈이 UNDERPOWERED_DATA 하나였다.
    #   그런데 그 실험은 기준선에 58.83%p 뒤졌다 - 표본이 모자란 것과 별개로
    #   **이미 아는 사실**이고, 안 남기면 다음 기획안이 같은 사양을 또 낸다.
    #   failed_criteria 는 릴리스 관문이 만드는데, 관문에 도달 못 하면 비어 있다.
    oos = {k: v for k, v in (oos_summary or {}).items() if v is not None}
    # ▶ **거래가 0이면 성과 교훈을 만들지 않는다** (2026-08-14 실측)
    #   min_adv_krw 단위 사고로 유니버스가 비었을 때 지표가 전부 0 이 되고
    #   초과만 -82.86%p 로 찍혔다. 그대로 두면 "기준선을 못 이겼다
    #   (BASELINE_NOT_BEATEN)" 가 환류에 실리는데, 그건 **없는 사실**이다 -
    #   한 주도 안 샀으니 이기고 지고가 없다. 다음 기획안이 이 교훈을 읽고
    #   엉뚱한 방향으로 사양을 고친다. 표본이 사양을 못 받쳤다고만 남긴다.
    _turnover = oos.get("turnover_total")
    _no_trade = False
    if _turnover is not None:
        try:
            _no_trade = float(_turnover) == 0.0
        except (TypeError, ValueError):
            _no_trade = False
    if _no_trade:
        out.append("UNDERPOWERED_DATA")
    else:
        if oos.get("excess_return_pct", 0) < 0 or oos.get("information_ratio", 0) < 0:
            out.append("BASELINE_NOT_BEATEN")
        if (oos.get("max_drawdown_pct") or 0) < -30 or (oos.get("max_drawdown") or 0) < -0.30:
            out.append("BEAR_FRAGILE")
        # Intraday feedback must distinguish a bad economic signal from an
        # implementation-cost failure. Optimising execution cannot rescue a
        # negative gross markout; conversely, discarding a positive gross
        # mechanism because spread/fees flipped net P&L loses useful structure.
        try:
            gross = float(oos["mean_mid_markout_bps"])
            net = float(oos["mean_net_bps_per_opportunity"])
        except (KeyError, TypeError, ValueError):
            gross = net = None
        if net is not None and net <= 0:
            out.append("COST_SENSITIVE" if gross > 0 else
                       "BASELINE_NOT_BEATEN")

    if str(fragility).upper() == "INSUFFICIENT":
        # 창이 0개라 강건성을 재지 못했다. "전략이 나쁘다" 가 아니라 **표본이
        # 사양을 못 받친다** 는 뜻이고, 리서치가 다음 기획에서 창을 줄이거나
        # 더 긴 이력을 요구해야 할 신호다.
        out.append("UNDERPOWERED_DATA")
    all_regimes = list(regime_concerns or ())
    checked_regimes = _checkable_regime_evidence(all_regimes)
    joined = " ".join(str(c) for c in all_regimes)
    if "하락장" in joined:
        out.append("BEAR_FRAGILE")
    checked_joined = " ".join(str(c) for c in checked_regimes)
    if "가로질러" in checked_joined or "국면이 1개" in checked_joined or any(
            isinstance(c, dict) and (c.get("count") == 1 or c.get("window_count") == 1)
            for c in checked_regimes):
        out.append("SINGLE_REGIME_ONLY")
    seen, uniq = set(), []          # 순서 보존 중복 제거 - 대조가 지저분해진다
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def fetch_family_outcomes(conn, trial_family_id: str) -> list[dict]:
    if not trial_family_id:
        return []
    with conn.cursor() as cur:
        cur.execute(_SQL_FAMILY_OUTCOMES, (trial_family_id,))
        return [{"decision": d, "lesson_codes": list(lc or [])}
                for d, lc in cur.fetchall()]


def fetch_exact_ast_outcomes(conn, signal_expr) -> list[dict]:
    """Negative/positive history follows executable formula identity, not its name."""
    if signal_expr is None:
        return []
    try:
        from alpha_ast import parse as _parse  # noqa: PLC0415
        normalized = _parse(signal_expr)
    except Exception:  # gate0 owns the human-readable invalid-AST rejection
        return []
    with conn.cursor() as cur:
        encoded = json.dumps(normalized, sort_keys=True)
        cur.execute(_SQL_EXACT_AST_OUTCOMES, (encoded, encoded))
        return [{"decision": d, "lesson_codes": list(lc or []),
                 "match_scope": "AST_EXACT"}
                for d, lc in cur.fetchall()]


def _merge_outcome_evidence(*groups) -> list[dict]:
    """Union lessons without double-counting an outcome found through both indexes."""
    merged: dict[tuple, dict] = {}
    for group in groups:
        for row in group or ():
            key = (str(row.get("decision") or ""),
                   tuple(sorted(str(x) for x in (row.get("lesson_codes") or ()))))
            prior = merged.get(key)
            if prior is None or row.get("match_scope") == "AST_EXACT":
                merged[key] = dict(row)
    return list(merged.values())


_SQL_ALREADY_PROMOTED = (
    "select proposal_id from quant.hypotheses "
    " where proposal_id = any(%s) and proposal_id is not null")

_SQL_MICRO_AVAILABLE_DAYS = """
select count(distinct p.partition_key)
  from quant.dataset_manifests m
  join quant.dataset_partitions p on p.dataset_id = m.dataset_id
 where m.name = %s and m.version = %s
"""


def _uses_microstructure(proposal: dict) -> bool:
    req = proposal.get("data_requirements") or {}
    if isinstance(req, dict) and "microstructure_features" in set(req.get("tables") or []):
        return True
    expr = (proposal.get("suggested_params") or {}).get("signal_expr")
    if expr is None:
        return False
    try:
        from alpha_ast import needs_micro, parse  # noqa: PLC0415

        return bool(needs_micro(parse(expr)))
    except Exception:
        return False


def _micro_dataset_for_proposal(proposal: dict) -> tuple[str, str] | None:
    """Return the exact immutable dataset version required by this AST."""
    if not _uses_microstructure(proposal):
        return None
    expr = (proposal.get("suggested_params") or {}).get("signal_expr")
    try:
        from backtest_runner import micro_dataset_for  # noqa: PLC0415

        return micro_dataset_for({"signal_expr": expr} if expr is not None else {})
    except Exception:
        # Invalid expressions receive a specific Gate 0 rejection.  For a
        # legacy table-only request the execution surface has always used v3.
        return "krx-microstructure-daily", "v3"


def _available_days_for_proposal(conn, proposal: dict) -> int:
    """Read the limiting sample from the ledger instead of trusting prose."""
    if not _uses_microstructure(proposal):
        return 0
    dataset = _micro_dataset_for_proposal(proposal)
    if dataset is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_MICRO_AVAILABLE_DAYS, dataset)
            row = cur.fetchone()
        return int((row or (0,))[0] or 0)
    except Exception:  # inability to measure is reported elsewhere; do not invent a count
        return 0

_SQL_INSERT_HYPOTHESIS = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id,
   proposal_id, lead_ids, research_packet_ids, claim_ids,
   economic_rationale, counterparty, competing_explanation,
   competing_explanation_codes, skeptic_sign, source_reported_effect,
   mapping_loss)
values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
returning hypothesis_id
"""


@dataclass(frozen=True)
class Promotion:
    """기획안 하나의 Gate 0 결과. 거부도 결과다 - 조용히 사라지지 않는다."""

    proposal_id: str
    accepted: bool
    hypothesis_id: str = ""
    gate: dict = field(default_factory=dict)

    @property
    def why(self) -> str:
        g = self.gate
        return "; ".join(g.get("reasons") or []) or ",".join(g.get("codes") or [])


def promote_published(conn, *, limit: int = 20, created_by: str = "factory-bridge",
                      dry_run: bool = False) -> list[Promotion]:
    """PUBLISHED 기획안을 Gate 0 에 태워 **가설로 승격한다.**

    ▶ 왜 이 함수가 필요했나 (2026-08-12)
      `gate0` 을 부르는 곳이 E2E 하네스(factory_e2e.py)뿐이었다. 즉 **기획안이
      가설이 되는 운영 경로가 없었다.** 리서치 에이전트가 기획안을 내고 접수까지
      통과해도 `quant.hypotheses` 는 그대로였고, 그래서 공장 자동 조종의 퀀트
      브리핑은 언제나 "실험 대기 가설 0건"이었다. 테스트에서만 도는 루프였다.

    ▶ 왜 결정론이 여기서 도는가
      Gate 0 은 판단이 아니라 **검사**다(어휘 대조·다중검정 예산·계열 압력).
      에이전트에게 맡기면 같은 기획안이 부를 때마다 다른 판정을 받는다.
      마스터플랜의 "결정론이 사실을 모으고 에이전트는 판단만" 이 그 뜻이다.

    ▶ 멱등
      이미 승격된 proposal_id 는 건너뛴다. 두 번 태우면 같은 기획안이 계열
      예산을 두 번 먹고, 다중검정 방어가 스스로 무너진다.
    """
    proposals = fetch_published_proposals(conn, limit=limit)
    if not proposals:
        return []
    ids = [p["proposal_id"] for p in proposals]
    with conn.cursor() as cur:
        cur.execute(_SQL_ALREADY_PROMOTED, (ids,))
        done = {r[0] for r in cur.fetchall()}

    out: list[Promotion] = []
    for p in proposals:
        if p["proposal_id"] in done:
            continue
        # 계열 예산·과거 교훈은 **원장에서 읽는다.** 인자로 받으면 호출부마다
        # 다른 수를 넘겨 같은 기획안이 다른 판정을 받는다.
        probe = gate0(p, trials_used=0, past_outcomes=[])
        fam = probe.trial_family_id
        family_history = fetch_family_outcomes(conn, fam)
        exact_history = fetch_exact_ast_outcomes(
            conn, ((p.get("suggested_params") or {}).get("signal_expr") or
                   (p.get("suggested_params") or {}).get("intraday_signal_expr")))
        available_days = _available_days_for_proposal(conn, p)
        gate = gate0(p, trials_used=count_family_trials(conn, fam),
                      past_outcomes=_merge_outcome_evidence(
                          family_history, exact_history),
                      available_days=available_days)
        if not gate.ok or dry_run:
            out.append(Promotion(p["proposal_id"], gate.ok, gate=gate.as_dict()))
            continue
        row = to_hypothesis_row(p, gate, created_by=created_by)
        with conn.cursor() as cur:
            cur.execute(_SQL_INSERT_HYPOTHESIS, (
                row["title"], row["rationale"], json.dumps(row["expected_edge"]),
                json.dumps(row["falsification_criteria"]),
                json.dumps(row["required_data_products"]), row["created_by"],
                row["proposal_id"], row["lead_ids"], row["research_packet_ids"],
                row["claim_ids"], row["economic_rationale"], row["counterparty"],
                row["competing_explanation"], row["competing_explanation_codes"],
                row["skeptic_sign"], json.dumps(row["source_reported_effect"]),
                json.dumps(row["mapping_loss"])))
            hid = str(cur.fetchone()[0])
        conn.commit()
        out.append(Promotion(p["proposal_id"], True, hypothesis_id=hid,
                             gate=gate.as_dict()))
    return out


class _PromoConn:
    """승격 검증용 가짜 연결. 실행된 SQL 을 순서대로 들고 있는다."""

    def __init__(self, published, already=(), family_trials=0, outcomes=(),
                 micro_available_days=61):
        self.published, self.already = published, list(already)
        self.family_trials, self.outcomes = family_trials, list(outcomes)
        self.micro_available_days = int(micro_available_days)
        self.micro_dataset_queries: list[tuple] = []
        self.inserted: list[tuple] = []
        self.commits = 0

    def cursor(self):
        return _PromoCursor(self)

    def commit(self):
        self.commits += 1


class _PromoCursor:
    def __init__(self, conn):
        self.c, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "from research.experiment_proposals" in s:
            # SQL 문면을 따라간다 - not exists 를 지우면 가짜도 예전(승격분이
            # 창을 차지하는) 동작으로 돌아가 아래 굶주림 자체 점검이 죽는다.
            already = set(self.c.already) if "not exists" in s else set()
            rows = [tuple(p.get(k, ({ } if k == "semantic_plan" else
                                    "DAILY_CROSS_SECTIONAL" if k == "research_lane" else None))
                          for k in (
                "proposal_id edge_type universe_key label baseline "
                "economic_rationale counterparty competing_explanation "
                "competing_explanation_codes skeptic_sign lead_ids "
                "falsification_tests data_requirements suggested_params "
                "trial_budget prior_check source_reported_effect "
                "research_packet_ids claim_ids research_lane semantic_plan").split())
                for p in self.c.published
                if p["proposal_id"] not in already]
            if params:
                rows = rows[: int(params[0])]
            self._rows = rows
        elif "select proposal_id from quant.hypotheses" in s:
            self._rows = [(x,) for x in self.c.already]
        elif "count(*) from quant.experiments" in s:
            self._rows = [(self.c.family_trials,)]
        elif "count(distinct p.partition_key)" in s:
            self.c.micro_dataset_queries.append(tuple(params or ()))
            self._rows = [(self.c.micro_available_days,)]
        elif "insert into quant.hypotheses" in s:
            self.c.inserted.append(params)
            self._rows = [("hyp-" + str(len(self.c.inserted)),)]
        else:
            self._rows = list(self.c.outcomes)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _check_fetch_window_excludes_promoted():
    """승격분이 창을 차지하면 새 기획안이 영영 못 들어온다 (t_7cd9bd5f).

    status 는 승격 후에도 PUBLISHED 로 남으므로, 창(가장 오래된 limit 건)에서
    승격분을 빼는 것은 SQL 의 not exists 몫이다. 이게 빠지면 오래된 승격분이
    창을 다 채운 시점부터 promote_published 는 매 주기 0건으로 끝나고,
    리서치 발행 -> 퀀트 승격 공급이 조용히 0 이 된다(실측: PUBLISHED 23건 중
    미소진 3건이 21~23위라 어떤 주기에도 판정을 받지 못했다).
    """
    s = " ".join(_SQL_PUBLISHED.lower().split())
    assert "not exists" in s and "quant.hypotheses" in s, \
        "_SQL_PUBLISHED 가 이미 승격된 기획안을 창에서 빼지 않는다"
    # 창 2칸에 승격분 2 + 신규 1(가장 최신). 예전 SQL 은 승격분 2건만 fetch 해
    # 신규가 창 밖에서 굶었다. 지금은 승격분이 창에서 빠져 신규가 판정받는다.
    old = [_prop(proposal_id=f"prop_old_{i}") for i in range(2)]
    new = _prop(proposal_id="prop_new")
    conn = _PromoConn(old + [new], already=["prop_old_0", "prop_old_1"])
    got = promote_published(conn, limit=2)
    assert [g.proposal_id for g in got] == ["prop_new"], got
    assert got[0].accepted and len(conn.inserted) == 1, got


def _check_promotion_is_idempotent_and_records_rejection():
    """기획안 -> 가설 승격이 **한 번만** 일어나고, 거부가 조용히 사라지지 않는가.

    ▶ 이 경로가 없어서 공장이 안 돌았다 (2026-08-12)
      gate0 을 부르는 곳이 E2E 하네스뿐이라 기획안이 가설이 되지 못했다.
      접수는 통과하는데 quant.hypotheses 는 늘 비어 있었고, 퀀트 브리핑은
      언제나 "실험 대기 가설 0건"이었다.

    ▶ 두 번 태우면 다중검정 방어가 무너진다
      같은 기획안이 계열 예산을 두 번 먹는다. 그래서 멱등이 성능이 아니라
      **정합성** 문제다.
    """
    good = _prop(proposal_id="prop_ok")
    conn = _PromoConn([good])
    got = promote_published(conn, limit=5)
    assert len(got) == 1 and got[0].accepted, got
    assert len(conn.inserted) == 1 and conn.commits == 1
    # 계보가 실제로 실렸는가 - 참조가 아니라 복사여야 사전등록이 성립한다
    assert "prop_ok" in conn.inserted[0], conn.inserted[0]

    # 이미 승격된 것은 다시 안 태운다
    again = _PromoConn([good], already=["prop_ok"])
    assert promote_published(again, limit=5) == []
    assert again.inserted == [] and again.commits == 0

    # 거부는 **결과로 남는다** - ok=False 가 목록에 들어오고 INSERT 는 없다
    bad = _prop(proposal_id="prop_bad", edge_type="없는_엣지_유형")
    rej = _PromoConn([bad])
    res = promote_published(rej, limit=5)
    assert len(res) == 1 and not res[0].accepted, res
    assert rej.inserted == [], "거부된 기획안이 가설이 됐다"
    assert res[0].why, "거부 사유가 비었다 - 리서치가 대응할 수 없다"

    # dry_run 은 판정만 하고 쓰지 않는다
    dry = _PromoConn([good])
    assert promote_published(dry, limit=5, dry_run=True)[0].accepted
    assert dry.inserted == [] and dry.commits == 0
    print("  기획안->가설 승격 멱등   OK")


# ── FRACAS 사상표 ────────────────────────────────────────────────────────────
# ▶ **REJECT 는 시정조치 없이 닫히지 않는다** (2026-08-13, MIL-STD-2155 폐루프)
#   교훈 코드는 NTSB 권고와 같은 지위였다 - 읽는 쪽이 무시해도 아무 일도 안
#   일어난다(실측: 권고의 35%가 10년 방치). 행동을 바꾸는 것은 시정조치의
#   **지정**이고, 그 조치가 재발을 실제로 막았는지 검증(verification_state)될
#   때까지 루프는 열려 있다. 여기 사상은 결정론 기본값이다 - 에이전트가 더
#   나은 조치를 지정할 수 있지만, 없어도 루프는 돈다(환류가 에이전트 가용성에
#   묶이면 안 된다는 lessons_from 과 같은 원칙).
FRACAS = {
    # 교훈 코드: (근본원인 분류, 기본 시정조치)
    "BASELINE_NOT_BEATEN": (
        "가설_논리",
        "브리핑 세칙: 같은 테마 재도전은 IR 개선 경로(비용·회전율·국면 방어 중 "
        "무엇으로 벤치마크를 일관되게 이길지)를 LESSONS_ADDRESSED 에 명시"),
    "SINGLE_REGIME_ONLY": (
        "표본_설계",
        "접수 세칙: 표본이 국면 1개면 판정 불가 - 장기 데이터셋(v3+)으로만 발주"),
    "BEAR_FRAGILE": (
        "가설_논리",
        "기획 세칙: 하락 국면 방어 장치(낙폭 정지·노출 축소)를 사전 고정하고 "
        "그 효과를 반증 기준에 포함"),
    "OVERFIT_DSR": (
        "표본_설계",
        "배분 세칙: 이 계열의 남은 시도는 탐색층(부분샘플·IC)에서 거른 1개 "
        "변형에만 사용 - DSR 감가는 시도를 되돌릴 수 없다"),
    "COST_SENSITIVE": (
        "가설_논리",
        "기획 세칙: 회전율 상한(200x)을 설계 제약으로 명시 - 보유기간을 늘리거나 "
        "리밸런스를 줄인 변형만 접수"),
    "UNDERPOWERED": (
        "표본_설계",
        "접수 세칙: 창 산수(walk-forward >= 4창)를 만족하는 형성창만 접수"),
    "UNDERPOWERED_DATA": (
        "표본_설계",
        "접수 세칙: 더 긴 이력 데이터셋으로만 재발주"),
    "DATA_ARTIFACT": (
        "데이터_결함",
        "데이터 세칙: 해당 원천의 결함(미조정 분할 등)을 빌드에서 다루기 전까지 "
        "그 구간 발주 금지"),
}
FRACAS_DEFAULT = ("순수_무알파",
                  "환류 세칙: 이 계열 재도전은 새 메커니즘 근거(신규 리드) 필수")


def fracas_of(lesson_codes) -> tuple[str, str]:
    """교훈 코드 -> (근본원인, 시정조치). 첫 번째로 사상되는 코드가 대표다.

    코드가 없거나 전부 모르는 코드면 기본값 - **비워 두지 않는다.** 빈 칸은
    NTSB 권고의 운명(방치)을 그대로 밟는다.
    """
    for c in (lesson_codes or []):
        if str(c) in FRACAS:
            return FRACAS[str(c)]
    return FRACAS_DEFAULT


def finalize(conn, *, hypothesis_id: str, new_status: str, outcome: dict) -> str:
    """**환류 적재와 상태 전이를 한 트랜잭션으로 묶는다.**

    "적재가 종결의 전제 조건" 을 주석으로만 적어 두면 언젠가 지켜지지 않는다.
    여기서 환류 INSERT 가 실패하면 상태 UPDATE 도 롤백된다 - 조용히 종결되고
    교훈만 사라지는 경로가 구조적으로 없어진다.

    FRACAS: 기각 계열 판정에는 근본원인·시정조치가 자동 지정된다(위 사상표).
    호출부가 명시하면 그것이 이긴다 - 결정론 기본값은 바닥이지 천장이 아니다.
    """
    payload = dict(outcome)
    payload["oos_summary"] = json.dumps(payload.get("oos_summary") or {})
    if str(payload.get("decision", "")).upper() in REJECTING:
        rc, ca = fracas_of(payload.get("lesson_codes"))
        payload.setdefault("root_cause", rc)
        payload.setdefault("corrective_action", ca)
    else:
        payload.setdefault("root_cause", None)
        payload.setdefault("corrective_action", None)
    # 컨텍스트 매니저를 쓰지 않는다 - 호출부(오케스트레이터)의 가짜 커서와 관례가 같다
    cur = conn.cursor()
    cur.execute(_SQL_INSERT_OUTCOME, payload)
    # 0행이면 이 실험에 이미 판정이 있다 - 먼저 온 것이 정본이므로 덮지 않되,
    # **조용히 넘어가지도 않는다**(두 경로가 경합했다는 사실 자체가 신호다).
    if getattr(cur, "rowcount", 1) == 0:
        print(f"  판정 중복 회피: {payload.get('experiment_id')} 는 이미 판정이 "
              f"있어 {payload.get('decision')} 를 적재하지 않았다(먼저 온 판정이 "
              f"정본). 두 경로가 같은 실험을 판정하려 한 것이므로 배선을 본다.",
              flush=True)
    cur.execute(
        "update quant.hypotheses set status = %s, status_changed_at = now() "
        "where hypothesis_id = %s", (new_status, hypothesis_id))
    conn.commit()
    return payload["outcome_id"]


# ---------------------------------------------------------------------------
# 자체 점검 (DB 없음)
# ---------------------------------------------------------------------------

def _prop(**kw):
    base = dict(proposal_id="prop_1", edge_type="mean_reversion",
                universe_key="krx_all", label="forward_return",
                baseline="equal_weight_buy_and_hold",
                economic_rationale="강제 청산자가 반대편에서 판다",
                counterparty="레버리지 청산 물량",
                competing_explanation="베타 노출일 수 있다",
                competing_explanation_codes=["BETA_EXPOSURE"],
                skeptic_sign="worker_run_42", lead_ids=["lead_a"],
                falsification_tests=["하락장 초과수익 < 0 이면 기각"],
                data_requirements={"tables": ["market_bars"], "min_history_days": 750},
                suggested_params={"lookback_days": 20}, trial_budget=5,
                prior_check={}, source_reported_effect={},
                research_packet_ids=[], claim_ids=[])
    base.update(kw)
    return base


def _check_clean_proposal_is_accepted():
    g = gate0(_prop())
    assert g.ok, g.as_dict()
    assert g.trial_family_id and g.trial_number == 1


def _check_unmapped_vocabulary_is_rejected():
    """**접수는 실행 가능성의 약속이다** - 실행면에 없는 유형을 받으면 나중에 죽는다."""
    g = gate0(_prop(edge_type="volatility_risk_premium"))
    assert not g.ok and "UNMAPPED_VOCAB" in g.codes
    assert "미구현" in g.reasons[0], g.reasons
    g2 = gate0(_prop(universe_key="내가 만든 유니버스"))
    assert not g2.ok and "UNMAPPED_VOCAB" in g2.codes
    assert "다중검정 가드" in g2.reasons[0]


def _check_budget_is_enforced():
    g = gate0(_prop(trial_budget=5,
                    prior_check={"lessons_addressed": {"OVERFIT_PBO": "파라미터 고정"}}),
              trials_used=5,
              past_outcomes=[{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}])
    assert not g.ok and "OVER_BUDGET" in g.codes, g.as_dict()
    assert g.trials_used == 5


def _check_trial_count_comes_from_executions_not_outcomes():
    """**실행됐지만 아직 종결 안 된 실험을 안 세면 예산을 넘겨 접수된다.**

    환류 기록이 1건인데 실행이 5건이면 계수 기준은 5여야 한다.
    """
    addressed = {"lessons_addressed": {"OVERFIT_PBO": "파라미터 고정"}}
    past = [{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}]
    over = gate0(_prop(trial_budget=5, prior_check=addressed),
                 trials_used=5, past_outcomes=past)
    assert over.trials_used == 5 and not over.ok, over.as_dict()
    # 반대로 실행이 0이면 환류가 있어도 예산은 남아 있다
    fresh = gate0(_prop(trial_budget=5, prior_check=addressed),
                  trials_used=0, past_outcomes=past)
    assert fresh.ok and fresh.trial_number == 1, fresh.as_dict()


def _check_unaddressed_lessons_block():
    """**회사가 이미 산 실험을 다시 사지 않는다.** 단, 산 적이 있어야 한다."""
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE", "COST_SENSITIVE"]}]
    g = gate0(_prop(), trials_used=1, past_outcomes=past)
    assert not g.ok and "DUPLICATE_UNADDRESSED" in g.codes
    assert "BEAR_FRAGILE" in g.reasons[-1] and "COST_SENSITIVE" in g.reasons[-1]


def _check_never_run_family_is_not_blocked_by_lessons():
    """**실행 0인 계열은 교훈이 있어도 막지 않는다** (2026-08-11 재설계).

    환류의 목적은 같은 실험을 두 번 사지 않는 것이다. 한 번도 실행되지 않은
    계열을 막으면 그건 중복 방지가 아니라 신규 차단이고, 아직 아무도 해보지
    않은 가설이 영영 실험되지 못하는 교착이 된다. 실측에서 실제로 그렇게 막혔다.
    """
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE"]}]
    g = gate0(_prop(), trials_used=0, past_outcomes=past)
    assert g.ok, g.as_dict()
    assert "DUPLICATE_UNADDRESSED" not in g.codes, g.as_dict()
    # 조용히 통과시키지는 않는다 - 원장이 어긋났다는 신호이므로 경고로 남긴다.
    assert g.warnings and "실행 기록은 0" in g.warnings[-1], g.as_dict()


def _check_exact_ast_history_cannot_be_renamed_away():
    """A new edge label must not erase the negative memory of the same formula."""
    ast_history = [{"decision": "GATE_HOLD",
                    "lesson_codes": ["UNDERPOWERED_DATA"],
                    "match_scope": "AST_EXACT"}]
    blocked = gate0(_prop(edge_type="momentum"), trials_used=0,
                    past_outcomes=ast_history)
    assert not blocked.ok, blocked.as_dict()
    assert "AST_DUPLICATE_UNADDRESSED" in blocked.codes, blocked.as_dict()
    addressed = gate0(_prop(
        edge_type="momentum",
        prior_check={"lessons_addressed": {
            "UNDERPOWERED_DATA": "longer PIT microstructure history acquired"}}),
        trials_used=0, past_outcomes=ast_history)
    assert addressed.ok, addressed.as_dict()


def _check_answer_in_wrong_field_says_where_to_move_it():
    """**거짓 사유는 고칠 수 없는 사유다.** (2026-08-13 실측)

    `prop_5682` 는 교훈 5개를 이름으로 짚어 대응을 적었다. 다만 카드가
    `ECONOMIC_RATIONALE 에 적어라` 라고 시켜서 거기 적었고, 계약은
    `LESSONS_ADDRESSED:` 만 읽는다. 반려문은 **"그 교훈에 대응이 없다"** 였다 -
    사실이 아니다. 기획자는 자기가 적은 것을 보면서 무엇이 문제인지 알 수
    없었고, 그래서 다음 기획안에서 같은 자리에 또 적었다.
    """
    past = [{"decision": "REJECT",
             "lesson_codes": ["BEAR_FRAGILE", "OVERFIT_DSR"]}]
    body = ("기존 시도는 하락 국면에서 손실이 확대됐다. BEAR_FRAGILE 에는 "
            "고점 대비 -25% 손실 정지를 사전 고정하고, OVERFIT_DSR 에는 "
            "사전 지정 파라미터 한 조합만 쓴다.")
    g = gate0(_prop(economic_rationale=body), trials_used=1, past_outcomes=past)
    assert not g.ok, "본문에만 있으면 통과하면 안 된다(계약이 무력해진다)"
    assert "LESSONS_IN_WRONG_FIELD" in g.codes, g.as_dict()
    why = " ".join(g.reasons)
    assert "LESSONS_ADDRESSED" in why, why          # 어디로 옮길지 말해 준다
    assert "대응이 없다" not in why, "사실이 아닌 사유가 아직 나간다"

    # 본문에도 없으면 예전 사유가 그대로 맞다 - 없는 답을 있다고 하지 않는다
    g2 = gate0(_prop(economic_rationale="그냥 될 것 같다"),
               trials_used=1, past_outcomes=past)
    assert "DUPLICATE_UNADDRESSED" in g2.codes, g2.as_dict()

    # 일부만 본문에 있으면 나머지를 이름으로 짚어 준다
    g3 = gate0(_prop(economic_rationale="BEAR_FRAGILE 에는 손실 정지를 건다"),
               trials_used=1, past_outcomes=past)
    assert "LESSONS_IN_WRONG_FIELD" in g3.codes
    assert "OVERFIT_DSR" in " ".join(g3.reasons), g3.as_dict()


def _check_unrunnable_falsification_is_disclosed():
    """**못 돌리는 반증은 접수 때 말해 준다.** (2026-08-13 실측)

    에이전트는 beta 통제·스프레드 검정 같은 진짜 반증을 설계하는데 실행면은
    6종만 돌린다. 침묵하면 판정이 "반증 통과" 로 오독된다. 막지는 않는다 -
    못 도는 반증은 실행면이 자라야 할 방향의 수요 신호다.
    """
    # 픽스처 주의(첫 작성 때 내가 틀렸다): "하락장 초과수익 < 0" 은 어느
    # 패턴에도 안 걸리는 미분류다 - 미분류도 "못 돌린다" 로 세는 것이 맞다.
    g = gate0(_prop(falsification_tests=[
        "거래비용 차감 후 초과수익이 소멸하면 기각",   # 실행 가능(cost_stress)
        "베타 중립화 후 효과 소멸 여부",               # 실행 불가
    ]))
    assert g.ok, "고지가 접수를 막았다 - 경고여야 한다"
    joined = " ".join(g.warnings)
    assert "못 돌린다" in joined and "베타" in joined, g.warnings
    # 전부 실행 가능하면 이 경고는 없어야 한다(늑대 없는 경보 금지)
    g2 = gate0(_prop(falsification_tests=[
        "거래비용 차감 후 초과수익이 소멸하면 기각",
        "walk-forward 창의 절반 이상에서 부호가 반전되면 기각",
    ]))
    assert "못 돌린다" not in " ".join(g2.warnings), g2.warnings


def _check_reject_never_closes_without_corrective_action():
    """**REJECT 는 시정조치 없이 닫히지 않는다.** (FRACAS, 2026-08-13)

    교훈 코드만 남기는 것은 NTSB 권고와 같다 - 35%가 10년 방치됐다.
    사상표에 없는 코드도 기본값을 받는다(빈 칸 금지). 채택(PROMOTED)에는
    시정조치를 지어내지 않는다.
    """
    rc, ca = fracas_of(["BASELINE_NOT_BEATEN", "BEAR_FRAGILE"])
    assert rc == "가설_논리" and "LESSONS_ADDRESSED" in ca
    rc2, ca2 = fracas_of(["듣도못한코드"])
    assert rc2 == "순수_무알파" and ca2, "모르는 코드가 빈 칸을 만들었다"
    assert fracas_of([]) == FRACAS_DEFAULT
    assert fracas_of(None) == FRACAS_DEFAULT
    # 어휘 정합: lessons_from 이 낼 수 있는 코드는 전부 사상표에 있어야 한다
    known = {"BASELINE_NOT_BEATEN", "SINGLE_REGIME_ONLY", "BEAR_FRAGILE",
             "OVERFIT_DSR", "COST_SENSITIVE", "UNDERPOWERED",
             "UNDERPOWERED_DATA", "DATA_ARTIFACT"}
    assert known <= set(FRACAS), f"사상표 구멍: {known - set(FRACAS)}"


def _check_addressed_lessons_pass():
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE"]}]
    g = gate0(_prop(prior_check={"lessons_addressed": {"BEAR_FRAGILE": "표본 확대"}}),
              trials_used=1, past_outcomes=past)
    assert g.ok, g.as_dict()
    assert g.trial_number == 2, g.as_dict()   # 두 번째 시도로 계수된다


def _check_success_history_does_not_block():
    past = [{"decision": "PROMOTED", "lesson_codes": []}]
    g = gate0(_prop(), past_outcomes=past)
    assert g.ok, g.as_dict()


def _check_lineage_is_copied_not_referenced():
    """사전등록 시점의 근거를 복사한다 - 기획안이 수정돼도 등록 내용은 안 변한다."""
    p = _prop()
    row = to_hypothesis_row(p, gate0(p))
    for k in ("proposal_id", "economic_rationale", "counterparty",
              "competing_explanation", "skeptic_sign"):
        assert row[k] == p[k if k != "proposal_id" else "proposal_id"], k
    assert row["competing_explanation_codes"] == ["BETA_EXPOSURE"]
    assert row["trial_family_id"] and row["trial_number"] == 1
    # ▶ 튜닝 파라미터는 **실행면이 읽는 것만** expected_edge 로 들어간다.
    #   예전 이 검사는 `lookback_days == 20` 을 기대했는데, 그 키는
    #   config_binding.EDGE_KEYS 밖이라 실행 단계에서 거부된다 - 검사가
    #   실행 불가로 태어나는 가설을 정상으로 규정하고 있었다(2026-08-12).
    assert "lookback_days" not in row["expected_edge"], row["expected_edge"]
    assert row["expected_edge"]["type"] == _prop()["edge_type"]


def _check_gate_approved_edge_type_wins():
    """**관문이 승인한 edge_type 을 suggested_params 가 덮을 수 없다.**

    실측 3bb50969: 기획안 edge_type=mean_reversion 이 관문을 통과했는데
    suggested_params 의 type=short_term_reversal 이 원장에 남았다. 승인한 값과
    기록된 값이 다르면 어휘 검사가 장식이 된다.
    """
    p = dict(_prop())
    p["suggested_params"] = {"type": "short_term_reversal",
                             "universe_key": "몰래바꾸기",
                             "horizon_days": 20, "top_n": 20,
                             "signal_window_days": 60,
                             "walk_forward_window_days": 252}
    edge, dropped = expected_edge_for(p)
    assert edge["type"] == p["edge_type"], edge
    assert edge["universe_key"] == p["universe_key"], edge
    # 실행면이 읽는 키는 살아남는다. **`signal_window_days` 는 2026-08-14 에
    # 형성창으로 열렸다**(카드 t_e9534028) - 이제 "안 읽는 키" 의 예시가 아니다.
    assert edge["horizon_days"] == 20 and edge["top_n"] == 20, edge
    assert edge["signal_window_days"] == 60, edge
    # 안 읽는 키는 빠지고, 무엇이 빠졌는지 알려 준다. 창 분할 사양
    # (`walk_forward_window_days`)이 그 자리다 - 실행 손잡이가 아니라
    # 사전등록 정책이라 접수에서 떨어뜨리고 알린다.
    assert "walk_forward_window_days" not in edge, edge
    assert dropped == ["type", "universe_key", "walk_forward_window_days"], dropped
    # 접수 단계에서 경고로 뜬다 - 세 주기 뒤 실행면에서 처음 알면 늦다
    g = gate0(p)
    assert any("SUGGESTED_PARAMS" in w for w in g.warnings), g.warnings


def _check_identity_hint_in_vocab_is_rejected():
    """**실행 가능한 어휘를 파라미터 칸에 적으면 반려한다** - 각인의 짝.

    실측(가설-실행 정합성 감사): MAX 가설이 suggested_params 에
    {"type": "low_max"} 까지 명시했는데 조용히 버려지고 LOWVOL 로 실행됐다.
    low_max 가 어휘에 없던 때는 어쩔 수 없었지만 이제 있다 - 있는데도 조용히
    다른 것을 돌리면 그건 무시가 아니라 **다른 가설의 검증**이다.
    """
    p = _prop(edge_type="low_volatility")
    p["suggested_params"] = {"type": "low_max", "top_n": 20}
    g = gate0(p)
    assert not g.ok and "IDENTITY_IN_PARAMS" in g.codes, g.as_dict()
    assert "EDGE_TYPE 필드로 옮겨라" in "; ".join(g.reasons), g.reasons
    # 어휘 밖 힌트는 반려하지 않는다(노이즈일 수 있다) - 기존 ①-a 경고가 잡는다
    p2 = _prop()
    p2["suggested_params"] = {"type": "sma_cross_sell", "top_n": 20}
    g2 = gate0(p2)
    assert g2.ok and "IDENTITY_IN_PARAMS" not in g2.codes, g2.as_dict()
    assert any("SUGGESTED_PARAMS" in w for w in g2.warnings), g2.warnings


def _check_mapping_loss_is_stamped():
    """번역 손실이 **원장에 각인**되는가 - 경고는 흘러가고 각인은 남는다."""
    p = _prop()  # lookback_days 는 버려지고, top_n 등은 관례로 채워진다
    loss = mapping_loss_of(p)
    assert loss["dropped_keys"] == ["lookback_days"], loss
    assert "top_n" in loss["defaulted_keys"], loss
    assert "identity_hints" not in loss, loss
    row = to_hypothesis_row(p, gate0(p))
    assert row["mapping_loss"] == loss, row["mapping_loss"]
    # INSERT 가 각인 컬럼을 실제로 나른다 - 계산만 하고 안 실으면 각인이 아니다
    assert "mapping_loss" in _SQL_INSERT_HYPOTHESIS, _SQL_INSERT_HYPOTHESIS
    # top_n 관례가 성적을 결정한 실측(TC 0.114) - 접수 경고로도 짚는다
    g = gate0(p)
    assert any("top_n 미지정" in w for w in g.warnings), g.warnings
    # top_n 을 정하면 그 경고는 사라진다 - 경고가 상수면 아무도 안 읽는다
    p3 = _prop()
    p3["suggested_params"] = {"top_n": 200}
    g3 = gate0(p3)
    assert not any("top_n 미지정" in w for w in g3.warnings), g3.warnings


def _check_out_of_range_param_rejected_at_intake():
    """**실행 불가로 태어나는 가설을 접수가 막는다** (2026-08-13 실측).

    prop_86e535d7 이 max_drawdown_stop=+0.35(부호 반대)로 접수를 통과했다 -
    바인딩에서야 죽을 값이다. 접수는 실행 가능성의 약속이다. 단 horizon_days
    는 lookback_days 자리(2~250)의 한도를 본다 - 입력 이름의 한도를 보면
    6개월 모멘텀(126일) 같은 정상 사양이 막힌다(2026-08-10 교훈 재확인).
    """
    p = _prop()
    p["suggested_params"] = {"top_n": 20, "max_drawdown_stop": 0.35}
    g = gate0(p)
    assert not g.ok and "PARAM_OUT_OF_RANGE" in g.codes, g.as_dict()
    assert "음수다" in "; ".join(g.reasons), g.reasons
    # 정상 사양은 통과한다 - 특히 horizon_days=126 (자리 기준 한도)
    p2 = _prop()
    p2["suggested_params"] = {"horizon_days": 126, "top_n": 200,
                              "max_drawdown_stop": -0.35}
    g2 = gate0(p2)
    assert g2.ok and "PARAM_OUT_OF_RANGE" not in g2.codes, g2.as_dict()

    # 선택형 값도 실행면과 같은 어휘로 접수에서 막힌다. 숫자 범위만 보면
    # 미구현 주기의 가설이 승격된 뒤 영구 PROPOSED로 남는다.
    p3 = _prop()
    p3["suggested_params"] = {
        "horizon_days": 2, "top_n": 100,
        "rebalance": "EVERY_3_TRADING_DAYS"}
    g3 = gate0(p3)
    assert "EXECUTION_BINDING_REJECTED" in g3.codes, g3.as_dict()
    assert any("EVERY_3_TRADING_DAYS" in x for x in g3.reasons), g3.reasons


def _check_micro_coverage_is_used_before_promotion():
    """A 61-day sample is availability, not a 61-day warm-up allowance."""
    p = _prop()
    p["suggested_params"] = {"horizon_days": 2, "top_n": 20}
    p["data_requirements"] = {
        "tables": ["market_bars", "microstructure_features"],
        "min_history_days": 61,
    }
    conn = _PromoConn([p], micro_available_days=61)
    assert _available_days_for_proposal(conn, p) == 61
    assert conn.micro_dataset_queries[-1] == ("krx-microstructure-daily", "v3")
    blocked = gate0(p, available_days=61)
    assert "UNDERPOWERED_DESIGN" in blocked.codes, blocked.as_dict()

    p["data_requirements"]["min_history_days"] = 3
    viable = gate0(p, available_days=61)
    assert "UNDERPOWERED_DESIGN" not in viable.codes, viable.as_dict()

    p5 = _prop()
    p5["suggested_params"] = {"signal_expr": {
        "op": "div",
        "args": [
            {"op": "ts_last", "field": "ofi_close", "n": 1},
            {"op": "ts_mean", "field": "book_depth_notional_l10", "n": 3},
        ],
    }}
    assert _micro_dataset_for_proposal(p5) == (
        "krx-microstructure-daily", "v5")


def _check_outcome_requires_reason():
    """사유 없는 기각은 환류가 성립하지 않는다."""
    for d in ("REJECT", "KILLED", "GATE_HOLD", "DEMOTED"):
        try:
            build_outcome(experiment_id="e1", hypothesis_id="h1",
                          trial_family_id="fam", trial_number=1, decision=d)
        except ValueError as exc:
            assert "사유가 없다" in str(exc)
        else:
            raise AssertionError(f"{d} 무사유가 통과했다")
    # 성공 종결은 사유 없이도 적재된다 - 무엇이 통했는지도 배운다
    ok = build_outcome(experiment_id="e1", hypothesis_id="h1",
                       trial_family_id="fam", trial_number=1, decision="PROMOTED")
    assert ok["decision"] == "PROMOTED"


def _check_unmeasured_metrics_are_dropped_not_zeroed():
    """**미측정을 0 으로 채우면 관문이 통과로 읽는다.**"""
    o = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="fam",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"],
                      oos_summary={"pbo": 0.8, "deflated_sharpe": None,
                                   "information_ratio": None})
    assert o["oos_summary"] == {"pbo": 0.8}, o["oos_summary"]
    assert "deflated_sharpe" not in o["oos_summary"]


def _check_outcome_id_is_idempotent():
    a = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    b = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    assert a["outcome_id"] == b["outcome_id"]
    c = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REVISE")
    assert c["outcome_id"] != a["outcome_id"]
    # ▶ **한 실험에 판정 하나** (2026-08-14 실측 e820053a: REJECT·GATE_HOLD 공존).
    #   outcome_id 멱등은 판정별로만 작동하므로 SQL 이 실험 단위로 막아야 한다 -
    #   판정이 둘이면 교훈 집계·파레토·시도 계수가 이중으로 센다.
    sql = " ".join(_SQL_INSERT_OUTCOME.split())
    assert "not exists" in sql and "o.experiment_id = %(experiment_id)s" in sql, sql
    assert "insert into research.experiment_outcomes" in sql and " select " in sql, \
        "values 절이면 실험 단위 가드가 안 걸린다"
    assert "select h.proposal_id" in sql and "nullif(%(proposal_id)s, '')" in sql, \
        "환류가 가설의 proposal 계보를 자동으로 이어받지 않는다"


def _check_finalize_is_atomic():
    """**환류 실패 시 상태 전이도 안 된다** - 조용히 종결되는 경로를 없앤다."""

    class _Cur:
        def __init__(self, boom_on): self.boom_on, self.ran = boom_on, []
        def execute(self, sql, params=None):
            self.ran.append(sql.strip()[:20])
            if self.boom_on and self.boom_on in sql:
                raise RuntimeError("insert 실패")

    class _Conn:
        def __init__(self, boom_on=None):
            self.cur = _Cur(boom_on); self.committed = False
        def cursor(self): return self.cur
        def commit(self): self.committed = True

    o = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    ok = _Conn()
    finalize(ok, hypothesis_id="h1", new_status="REJECTED", outcome=o)
    assert ok.committed and len(ok.cur.ran) == 2

    boom = _Conn(boom_on="insert into research.experiment_outcomes")
    try:
        finalize(boom, hypothesis_id="h1", new_status="REJECTED", outcome=o)
    except RuntimeError:
        pass
    else:
        raise AssertionError("환류 실패가 통과했다")
    assert not boom.committed, "환류가 실패했는데 커밋됐다"
    assert len(boom.cur.ran) == 1, "환류 실패 후 상태 UPDATE 가 실행됐다"


def _check_lessons_mapping_is_deterministic_baseline():
    """**에이전트가 없어도 교훈이 남는다** - 환류가 에이전트 가용성에 묶이면 안 된다."""
    assert lessons_from(failed_criteria=["pbo", "min_deflated_sharpe"]) == [
        "OVERFIT_PBO", "OVERFIT_DSR"]
    b = lessons_from(failed_criteria=["max_turnover"], fragility="FRAGILE")
    assert "COST_SENSITIVE" in b and "SINGLE_REGIME_ONLY" not in b, b
    assert "SINGLE_REGIME_ONLY" in lessons_from(regime_concerns=[
        {"regime_label": "TEST", "window": "2024-01/2024-06", "count": 1}])
    assert "SINGLE_REGIME_ONLY" not in lessons_from(regime_concerns=["국면이 1개"])
    assert lessons_from(regime_concerns=["하락장 평균 수익률 -32.1%"]) == ["BEAR_FRAGILE"]
    # 같은 교훈이 두 경로로 나와도 한 번만 (대조가 지저분해진다)
    assert lessons_from(failed_criteria=["max_drawdown_pct"],
                        regime_concerns=["하락장 손실"]) == ["BEAR_FRAGILE"]
    assert lessons_from() == []


def _check_gate0_is_deterministic():
    p = _prop()
    kw = dict(trials_used=1,
              past_outcomes=[{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}])
    assert gate0(p, **kw).as_dict() == gate0(p, **kw).as_dict()


_SQL_REGISTER = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id, proposal_id, lead_ids,
   economic_rationale, counterparty, competing_explanation,
   competing_explanation_codes, skeptic_sign, source_reported_effect)
values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
        %s,%s,%s,%s,%s,%s,%s,%s)
-- 부분 유니크 인덱스(proposal_id is not null)를 쓰므로 같은 술어를 적어야
-- Postgres 가 그 인덱스를 추론한다. 빠뜨리면 제약이 있어도 못 찾는다.
on conflict (proposal_id) where proposal_id is not null do nothing
returning hypothesis_id
"""


def intake_published(conn, *, limit: int = 20, available_days: int = 0) -> list[dict]:
    """PUBLISHED 기획안을 Gate 0 에 태우고 통과분을 가설로 등록한다.

    ▶ 계수 출처를 섞지 않는다: 시도 횟수는 `quant.experiments`(실행 기록)에서,
      기각 교훈은 `research.experiment_outcomes`(대조 대상)에서 온다. 한 곳에서
      세면 미종결 실험을 놓쳐 예산을 넘긴 채 접수한다.
    """
    import json as _json

    out: list[dict] = []
    for prop in fetch_published_proposals(conn, limit=limit):
        fam = family_id(_hyp_view(prop))
        # ▶ 표본 일수를 접수에 넘긴다. 없으면 설계 검사가 조용히 생략되므로
        #   못 잰 경우 0 을 넘겨 검사를 건너뛰되, 잰 경우엔 반드시 쓴다.
        gate = gate0(prop,
                     trials_used=count_family_trials(conn, fam),
                     past_outcomes=fetch_family_outcomes(conn, fam),
                     available_days=available_days)
        rec = {"proposal_id": prop.get("proposal_id"), "gate": gate,
               "hypothesis_id": None}
        if gate.ok:
            row = to_hypothesis_row(prop, gate)
            cur = conn.cursor()
            cur.execute(_SQL_REGISTER, (
                row["title"], row["rationale"], _json.dumps(row["expected_edge"]),
                _json.dumps(row["falsification_criteria"]),
                _json.dumps(row["required_data_products"]), row["created_by"],
                row["proposal_id"], row["lead_ids"], row["economic_rationale"],
                row["counterparty"], row["competing_explanation"],
                row["competing_explanation_codes"], row["skeptic_sign"],
                _json.dumps(row["source_reported_effect"])))
            got = cur.fetchone()
            rec["hypothesis_id"] = str(got[0]) if got else None
        out.append(rec)
    conn.commit()
    return out


def _cli_intake() -> int:
    import psycopg2

    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    # 표본 일수는 시장 DB 에만 있다. 못 붙으면 0 -> 설계 검사는 건너뛰지만
    # 그 사실이 로그에 남는다(조용히 통과시키지 않는다).
    days = 0
    try:
        m = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
        c = m.cursor()
        c.execute("select count(distinct bucket_time::date) from market.market_bars"
                  " where interval_code = '1D'")
        days = int(c.fetchone()[0] or 0)
        m.close()
        print(f"  표본 {days}거래일 기준으로 설계 실현가능성을 검사한다")
    except Exception as e:
        print(f"  ! 표본 일수를 못 쟀다({type(e).__name__}) - 설계 검사 생략")
    try:
        for rec in intake_published(conn, available_days=days):
            g = rec["gate"]
            mark = "접수" if g.ok else "반려"
            print(f"  [{mark}] {rec['proposal_id']}  family={g.trial_family_id}"
                  f" trial#{g.trial_number}")
            for r in (g.reasons or []):
                print(f"      X {r}")
            if rec["hypothesis_id"]:
                print(f"      -> 가설 {rec['hypothesis_id']}")
            elif g.ok:
                print("      (이미 등록된 기획안 - 중복 접수 안 함)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--intake" in sys.argv:
        raise SystemExit(_cli_intake())

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_clean_proposal_is_accepted();        print("  정상 기획안 접수         OK")
    _check_unmapped_vocabulary_is_rejected();   print("  어휘 미사상 거부         OK")
    _check_budget_is_enforced();                print("  시도 예산 강제           OK")
    _check_trial_count_comes_from_executions_not_outcomes()
    print("  계수=실행기록(종결 아님)  OK")
    _check_unaddressed_lessons_block();         print("  기각 교훈 미대응 차단    OK")
    _check_never_run_family_is_not_blocked_by_lessons()
    _check_exact_ast_history_cannot_be_renamed_away()
    print("  동일 AST 이름 우회 차단    OK")
    _check_answer_in_wrong_field_says_where_to_move_it()
    _check_reject_never_closes_without_corrective_action()
    _check_unrunnable_falsification_is_disclosed(); print("  실행 0 계열은 안 막음    OK")
    _check_addressed_lessons_pass();            print("  대응하면 재도전 허용     OK")
    _check_success_history_does_not_block();    print("  성공 이력은 안 막음      OK")
    _check_lineage_is_copied_not_referenced();  print("  계보 복사(참조 아님)     OK")
    _check_gate_approved_edge_type_wins();      print("  관문 승인값이 이긴다     OK")
    _check_identity_hint_in_vocab_is_rejected(); print("  정체성 힌트 반려         OK")
    _check_mapping_loss_is_stamped();           print("  번역 손실 각인           OK")
    _check_out_of_range_param_rejected_at_intake()
    print("  범위 밖 파라미터 접수 반려 OK")
    _check_micro_coverage_is_used_before_promotion()
    print("  미시표본·웜업 분리 판정   OK")
    _check_outcome_requires_reason();           print("  사유 없는 기각 거부      OK")
    _check_unmeasured_metrics_are_dropped_not_zeroed(); print("  미측정 != 0        OK")
    _check_outcome_id_is_idempotent();          print("  환류 멱등                OK")
    _check_finalize_is_atomic();                print("  환류+전이 원자성         OK")
    _check_lessons_mapping_is_deterministic_baseline()
    print("  교훈 사상(에이전트 무관)  OK")
    _check_signal_expr_is_gated_at_intake()
    print("  수식은 접수에서 판정      OK")
    _check_hypothesis_and_factor_tell_the_same_story()
    print("  논리<->수식 정합(결정론)  OK")
    _check_no_trade_does_not_teach_performance_lessons()
    print("  거래 0 -> 성과 교훈 없음  OK")
    _check_gate0_is_deterministic();            print("  Gate 0 결정론            OK")
    _check_promotion_is_idempotent_and_records_rejection()
    _check_fetch_window_excludes_promoted()
    print("  승격분은 창에서 빠진다   OK")
    print("공장 다리 26개 영역 통과. 루프가 닫혔다.")
