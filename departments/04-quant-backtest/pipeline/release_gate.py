"""릴리스 관문 (QNT-07) - 좋은 전략에 갈 데를 만든다. **승격은 하지 않는다.**

담당: 재일 (퀀트·백테스트본부 QNT)
근거: CLAUDE.md "quant-backtest-department 는 Production 승격을 직접 하지
      않는다. CEO·Risk·QA 승인이 필요하다"

▶ 왜 필요한가
  파이프라인이 ROBUST 까지 판정해도 그 뒤가 비어 있었다. 좋은 전략을 찾아도
  **갈 데가 없다** - SUPPORTED 로 표시만 되고 아무 일도 안 일어난다.
  QNT-07 은 P1 인데 코드가 0줄이었다.

▶ 이 모듈이 하는 것과 **절대 하지 않는 것**
  한다   : 릴리스 기준을 결정론으로 검사하고 Release Candidate 를 만든다
  안 한다: Production 승격. 그건 CEO·Risk·QA 의 권한이다.

  퀀트가 자기 후보를 스스로 승인하면 견제가 사라진다. 계약(quant_v2)도
  Decision.SUBMIT_TO_QA 를 "승격이 아니다. 제출이다" 로 못 박는다 -
  이 모듈은 그 문장을 코드로 만든 것이다.

▶ 기준은 결정론이다
  "이 정도면 괜찮지 않나" 를 LLM 이나 사람 감으로 정하면 매번 달라지고,
  달라지는 기준은 기준이 아니다. 임계는 상수로 두고 근거를 값 옆에 적는다.

  그리고 **하나라도 미달이면 제출하지 않는다.** 종합 점수로 뭉개면 치명적
  결함이 좋은 지표에 가려진다 - 예컨대 MDD -60% 를 Sharpe 2.0 이 덮는다.

자체 점검: python departments/04-quant-backtest/pipeline/release_gate.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

GATE_VERSION = "quant-release-gate-v1"

# ── 릴리스 기준 ──────────────────────────────────────────────────────────────
# 근거를 값 옆에 남긴다. 바꾸면 이 주석도 같이 고친다.
CRITERIA = {
    # 벤치마크를 못 이기면 존재 이유가 없다. 0 이 아니라 여유를 둔다 -
    # 비용 추정 오차만으로 뒤집히는 초과는 초과가 아니다.
    "min_excess_return_pct": 10.0,
    # 초과수익의 안정성. 0.5 는 "초과가 있긴 하나 운과 구분이 어렵다" 선이다.
    "min_information_ratio": 0.5,
    # 한 번의 낙폭으로 운용이 끊기면 장기 성과는 의미가 없다.
    "max_drawdown_pct": -35.0,
    # 회전율이 높을수록 비용 가정에 결과가 좌우된다. 우리 비용 모델은
    # 계층 근사라 고회전 전략의 추정 오차가 크다.
    "max_turnover": 200.0,
    # 창별 재검증을 통과해야 한다. FRAGILE 은 애초에 여기 오지 않는다.
    "required_fragility": "ROBUST",
    # ▶ Deflated Sharpe. "이 성적이 우연이 아닐 확률" 이다.
    #   0.95 는 관행적 유의수준 - 20번 중 1번은 우연이어도 통과한다는 뜻이라
    #   느슨한 편이지만, 표본이 짧은 우리 단계에서 더 조이면 아무것도 못 낸다.
    "min_deflated_sharpe": 0.95,
    # ▶ 부트스트랩 구간이 0을 걸치면 알파라고 말할 수 없다.
    "require_ci_excludes_zero": True,
    # ▶ 백테스트 1등이 OOS 에서 중앙값 아래로 떨어지는 비율. 절반을 넘으면
    #   "백테스트 1등" 이 미래를 예측하지 못한다는 뜻이다.
    "max_pbo": 0.5,
}


@dataclass
class ReleaseDecision:
    """관문 판정. **SUBMIT_TO_QA 는 승격이 아니라 제출이다.**"""
    decision: str                       # SUBMIT_TO_QA / HOLD
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    next_owner: str = ""
    # ▶ **못 잰 것과 못 넘은 것을 가른다.** 둘 다 차단이지만 뜻이 다르다 -
    #   "낙폭이 기준을 넘었다" 는 설계를 고치라는 말이고, "PBO 를 안 쟀다" 는
    #   재라는 말이다. 섞으면 환류가 "과적합됐다" 는 없는 사실을 만든다
    #   (2026-08-12: momentum 이 PBO 미측정으로 OVERFIT_PBO 교훈을 받았다).
    unmeasured: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "passed": list(self.passed),
            "failed": list(self.failed),
            "reasons": list(self.reasons),
            "unmeasured": list(self.unmeasured),
            "next_owner": self.next_owner,
            # ▶ 오해를 막는 문장을 판정에 같이 싣는다. 이 dict 만 보고
            #   "통과했으니 올려도 된다" 로 읽히면 안 된다.
            "not_a_promotion": "SUBMIT_TO_QA 는 제출이다. Production 승격은 "
                               "CEO·Risk·QA 승인 뒤에만 일어난다",
        }


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ── 저장된 지표 이름 -> 관문 어휘 ──────────────────────────────────────────
# ▶ 왜 여기 있나 (2026-08-12 실측 - 이걸로 관문이 눈을 감고 있었다)
#   관문은 `max_drawdown_pct`(퍼센트) 와 `turnover` 를 찾는데 러너가 적는 이름은
#   `max_drawdown`(비율) 과 `turnover_total` 이다. **셋 다 안 맞았다.**
#   결과: MDD 와 회전율이 영구 "미확인" 이라 어떤 실험도 통과할 수 없었다.
#   보정한다고 넣어 둔 `mdd_pct` 도 존재하지 않는 이름이라 None 을 덮어썼다.
#
#   기준을 소유한 쪽이 어휘도 소유한다. 읽는 곳마다 각자 이름을 맞추면
#   한 곳을 고쳐도 다른 곳이 계속 눈을 감는다(실제로 두 곳이 그랬다).
#
#   (저장이름, 배수) - 배수는 단위 변환이다. 비율 -0.5052 를 그대로 -35.0 과
#   비교하면 **항상 통과**한다. 이름만 고치고 단위를 두면 조항이 조용히 꺼진다.
STORED_ALIASES: dict[str, tuple[str, float]] = {
    "max_drawdown_pct": ("max_drawdown", 100.0),   # 비율 -> 퍼센트
    "turnover": ("turnover_total", 1.0),
}

# 전기간 성적이 담기는 split. 창별 행과 섞으면 정렬 없는 dict 축약이
# **아무 창이나** 집어 간다 - 실제로 그랬다.
FULL_PERIOD_SPLIT = "TEST"
# 창을 가리키지 않는 차원 값. `SUMMARY` 는 전체 요약이라는 뜻이다.
_NOT_A_WINDOW = ("", "SUMMARY")
# 계열 전체를 보는 통계. 전기간 split 이 아니라 WALK_FORWARD 에 적재된다 -
# "이 창의 성적" 이 아니라 "이 계열에서 고르는 행위가 신뢰되는가" 이기 때문이다.
_FAMILY_STATS = frozenset({"pbo", "pbo_n_variants", "pbo_n_splits",
                           "pbo_n_windows"})


def _window_of(dims) -> str:
    """행의 창 이름. 없으면 빈 문자열(=전체 구간 행)."""
    if isinstance(dims, dict):
        return str(dims.get("window") or "")
    if isinstance(dims, str) and dims.strip().startswith("{"):
        try:
            import json  # noqa: PLC0415

            return str((json.loads(dims) or {}).get("window") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def metrics_from_rows(rows) -> dict:
    """`(metric, value, split[, dimensions])` 행들 -> 관문이 읽는 지표 dict.

    **창별 행만 뺀다. split 으로 가르지 않는다.**

    ▶ 왜 split 이 기준이 아닌가 (2026-08-12, 이 함수의 첫 판이 틀렸다)
      전기간 성적은 `split=TEST` 인데, **PBO 는 `split=WALK_FORWARD` 에
      적재된다** - 창별 성적이 아니라 계열 전체를 보는 통계라서 그렇다.
      `TEST` 만 읽게 짰더니 PBO 가 통째로 안 보였다. 관문은 미확인을 통과로
      치지 않으므로(옳다) 어떤 실험도 영원히 못 넘을 뻔했다.

      가르는 기준은 **창을 가리키는 행인가**이다. 창별 행은 `dimensions.window`
      를 갖고, 전체 구간·계열 통계는 안 갖는다. 환류 적재부가 이미 같은
      기준을 쓰고 있었다(`coalesce(dimensions->>'window','') in ('','SUMMARY')`).
    """
    full: dict[str, float] = {}
    for r in rows or ():
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            continue
        name, value = r[0], _num(r[1])
        if value is None:
            continue
        dims = r[3] if len(r) > 3 else None
        if _window_of(dims) not in _NOT_A_WINDOW:
            continue                     # 창별 행 - 전기간 기준과 비교하지 않는다
        # 차원이 안 실려 온 옛 호출은 split 으로 보수적으로 가른다
        if len(r) <= 3:
            split = str(r[2]) if len(r) > 2 and r[2] is not None else FULL_PERIOD_SPLIT
            if split != FULL_PERIOD_SPLIT and str(name) not in _FAMILY_STATS:
                continue
        full[str(name)] = value

    out = dict(full)
    for want, (stored, scale) in STORED_ALIASES.items():
        # 정본 이름이 이미 있으면 건드리지 않는다 - 별칭은 보조다
        if want not in out and stored in full:
            out[want] = full[stored] * scale
    return out


SQL_GATE_METRICS = """select metric, value, split, dimensions
                        from quant.experiment_metrics
                       where experiment_id = %s"""


def evaluate(metrics: dict, *, fragility: str,
             trial_pressure: dict | None = None) -> ReleaseDecision:
    """지표 -> 릴리스 판정. **순수 함수, 결정론.**

    미확인 지표는 통과로 치지 않는다 - 없는 것과 좋은 것은 다르다.
    """
    passed: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    unmeasured: list[str] = []

    def _check(name: str, value, ok: bool, msg: str) -> None:
        if value is None:
            failed.append(name)
            unmeasured.append(name)
            # ▶ **미확인을 통과로 위장하지 않는다.** 벤치마크가 없으면 초과를
            #   못 재는 것이지 초과가 있는 게 아니다.
            reasons.append(f"{name}: 미확인 - 판정할 수 없다")
        elif ok:
            passed.append(name)
        else:
            failed.append(name)
            reasons.append(msg)

    ex = _num(metrics.get("excess_return_pct"))
    _check("excess_return", ex,
           ex is not None and ex >= CRITERIA["min_excess_return_pct"],
           f"초과수익 {ex}%p < 기준 {CRITERIA['min_excess_return_pct']}%p - "
           f"벤치마크를 못 이기면 존재 이유가 없다")

    ir = _num(metrics.get("information_ratio"))
    _check("information_ratio", ir,
           ir is not None and ir >= CRITERIA["min_information_ratio"],
           f"정보비율 {ir} < 기준 {CRITERIA['min_information_ratio']} - "
           f"초과가 운과 구분되지 않는다")

    mdd = _num(metrics.get("max_drawdown_pct"))
    _check("max_drawdown", mdd,
           mdd is not None and mdd >= CRITERIA["max_drawdown_pct"],
           f"MDD {mdd}% < 허용 {CRITERIA['max_drawdown_pct']}% - "
           f"한 번의 낙폭으로 운용이 끊기면 장기 성과는 의미가 없다")

    to = _num(metrics.get("turnover"))
    _check("turnover", to,
           to is not None and to <= CRITERIA["max_turnover"],
           f"회전율 {to}x > 허용 {CRITERIA['max_turnover']}x - "
           f"비용 가정 오차가 결과를 좌우한다")

    # ── 과적합 통계 (계약 quant_v2 가 요구하는 필드들) ──────────────────────
    dsr = _num(metrics.get("deflated_sharpe"))
    _check("deflated_sharpe", dsr,
           dsr is not None and dsr >= CRITERIA["min_deflated_sharpe"],
           f"DSR {dsr} < 기준 {CRITERIA['min_deflated_sharpe']} - "
           f"시도 횟수를 감안하면 이 성적은 우연과 구분되지 않는다")

    lo = _num(metrics.get("bootstrap_ci_low"))
    if CRITERIA["require_ci_excludes_zero"]:
        _check("bootstrap_ci", lo,
               lo is not None and lo > 0.0,
               f"Sharpe 신뢰구간 하한 {lo} <= 0 - 구간이 0을 걸치면 "
               f"알파가 있다고 말할 수 없다")

    pbo_v = _num(metrics.get("pbo"))
    _check("pbo", pbo_v,
           pbo_v is not None and pbo_v <= CRITERIA["max_pbo"],
           f"PBO {pbo_v} > 허용 {CRITERIA['max_pbo']} - 백테스트 1등이 "
           f"OOS 에서 중앙값 아래로 떨어진다")

    frag = (fragility or "").strip().upper()
    _check("fragility", frag or None, frag == CRITERIA["required_fragility"],
           f"강건성 {frag or '미확인'} != {CRITERIA['required_fragility']}")

    # 다중검정 압력. 예산 초과면 성적이 좋아도 제출하지 않는다 -
    # 20번 돌려 고른 최고치는 실력과 운을 못 가린다.
    tp = trial_pressure or {}
    if tp.get("over_budget"):
        failed.append("trial_pressure")
        reasons.append(
            f"시도 {tp.get('trials_used')}회 > 예산 {tp.get('trial_budget')}회 - "
            f"많이 돌려 고른 최고치는 실력과 운을 못 가린다")
    elif tp:
        passed.append("trial_pressure")

    # ▶ **하나라도 미달이면 제출하지 않는다.** 종합 점수로 뭉개면 치명적
    #   결함이 좋은 지표에 가린다(MDD -60% 를 Sharpe 2.0 이 덮는 식).
    ok = not failed
    return ReleaseDecision(
        decision="SUBMIT_TO_QA" if ok else "HOLD",
        passed=sorted(passed), failed=sorted(failed), reasons=reasons,
        unmeasured=sorted(unmeasured),
        next_owner="AI QA/감사본부 + Risk (독립 검증) -> CEO (승격 승인)"
        if ok else "퀀트본부 (기준 미달 - 재설계 또는 폐기)")


def _print(d: ReleaseDecision) -> None:
    print(f"{GATE_VERSION}: {d.decision}")
    print(f"  통과 {len(d.passed)} / 미달 {len(d.failed)}")
    for r in d.reasons:
        print(f"  · {r}")
    print(f"  다음: {d.next_owner}")
    if d.decision == "SUBMIT_TO_QA":
        print("  ⚠ 제출이지 승격이 아니다 - Production 은 CEO·Risk·QA 승인 뒤")


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_GOOD = {"excess_return_pct": 156.37, "information_ratio": 1.2495,
         "max_drawdown_pct": -30.0, "turnover": 114.87,
         "deflated_sharpe": 0.985, "bootstrap_ci_low": 0.42,
         "bootstrap_ci_high": 1.91, "pbo": 0.2}


def _check_overfit_stats_block():
    """**과적합 통계가 실제로 막는가.** 계약에 필드만 있고 안 보면 무의미하다.

    contracts/quant_v2 는 deflated_sharpe·bootstrap_ci 를 필드로 갖고 있었는데
    채우는 코드도, 보는 코드도 없었다. 계산과 판정을 같이 붙여야 값을 한다.
    """
    # DSR 이 낮으면 나머지가 좋아도 막힌다 - 많이 돌려 얻은 성적이다
    d = evaluate(dict(_GOOD, deflated_sharpe=0.60), fragility="ROBUST")
    assert d.decision == "HOLD" and "deflated_sharpe" in d.failed, d
    assert any("우연과 구분" in r for r in d.reasons), d.reasons

    # 신뢰구간이 0을 걸치면 알파라고 말할 수 없다
    d2 = evaluate(dict(_GOOD, bootstrap_ci_low=-0.1), fragility="ROBUST")
    assert d2.decision == "HOLD" and "bootstrap_ci" in d2.failed, d2

    # PBO 가 높으면 백테스트 1등이 미래를 예측하지 못한다
    d3 = evaluate(dict(_GOOD, pbo=0.8), fragility="ROBUST")
    assert d3.decision == "HOLD" and "pbo" in d3.failed, d3

    # ▶ 셋 다 **미확인이면 통과가 아니다** - 검증을 안 돌린 것과 통과한 것은
    #   다르다. 계약이 NOT_RUN 을 별도 값으로 두는 이유와 같다.
    d4 = evaluate({k: v for k, v in _GOOD.items()
                   if k not in ("deflated_sharpe", "bootstrap_ci_low", "pbo")},
                  fragility="ROBUST")
    assert d4.decision == "HOLD", d4
    for k in ("deflated_sharpe", "bootstrap_ci", "pbo"):
        assert k in d4.failed, (k, d4.failed)


def _check_all_pass_submits():
    d = evaluate(_GOOD, fragility="ROBUST",
                 trial_pressure={"over_budget": False})
    assert d.decision == "SUBMIT_TO_QA", d
    assert not d.failed, d.failed
    assert "QA" in d.next_owner and "CEO" in d.next_owner
    # 승격이 아니라는 사실이 판정에 남는가
    assert "승격은" in d.as_dict()["not_a_promotion"]


def _check_one_failure_blocks():
    """**하나라도 미달이면 막는다.** 종합 점수로 뭉개면 치명적 결함이 가린다."""
    bad_mdd = dict(_GOOD, max_drawdown_pct=-60.0)
    d = evaluate(bad_mdd, fragility="ROBUST")
    assert d.decision == "HOLD" and "max_drawdown" in d.failed, d
    # 나머지는 여전히 통과로 남는다 - 무엇이 문제인지 보여야 고친다
    assert "excess_return" in d.passed and "information_ratio" in d.passed


def _check_missing_is_not_pass():
    """미확인을 통과로 위장하지 않는다 - 없는 것과 좋은 것은 다르다."""
    d = evaluate({"max_drawdown_pct": -10.0, "turnover": 10.0},
                 fragility="ROBUST")
    assert d.decision == "HOLD", d
    assert "excess_return" in d.failed and "information_ratio" in d.failed
    assert any("미확인" in r for r in d.reasons), d.reasons


def _check_fragile_never_submits():
    for f in ("FRAGILE", "", "UNKNOWN"):
        d = evaluate(_GOOD, fragility=f)
        assert d.decision == "HOLD", (f, d)
        assert "fragility" in d.failed


def _check_trial_pressure_blocks():
    """많이 돌려 고른 최고치는 성적이 좋아도 제출하지 않는다."""
    d = evaluate(_GOOD, fragility="ROBUST",
                 trial_pressure={"trials_used": 20, "trial_budget": 5,
                                 "over_budget": True})
    assert d.decision == "HOLD" and "trial_pressure" in d.failed, d
    assert any("실력과 운" in r for r in d.reasons), d.reasons


def _check_negative_excess_blocks():
    """절대수익이 양수여도 시장을 못 이기면 막힌다 - 실측 mean_reversion."""
    d = evaluate({"excess_return_pct": -43.25, "information_ratio": -0.4423,
                  "max_drawdown_pct": -20.0, "turnover": 50.0},
                 fragility="ROBUST")
    assert d.decision == "HOLD", d
    assert "excess_return" in d.failed and "information_ratio" in d.failed


def _check_gate_cannot_promote():
    """**이 모듈에 승격 경로가 없다는 것**을 코드로 확인한다.

    판정 값이 SUBMIT_TO_QA / HOLD 둘뿐이어야 한다. PROMOTE 같은 값이 생기면
    퀀트가 자기 후보를 스스로 올리는 길이 열린 것이다.
    """
    seen = set()
    for f in ("ROBUST", "FRAGILE"):
        for m in (_GOOD, {}):
            seen.add(evaluate(m, fragility=f).decision)
    assert seen <= {"SUBMIT_TO_QA", "HOLD"}, seen
    # ▶ **문자열 검색이 아니라 AST 로 본다.** 금지어 목록으로 소스를 훑으면
    #   목록 자신에 걸려 늘 실패한다 - 오늘 아침 evidence/method_performance.py
    #   에서 똑같이 겪었다. 검사가 자기 문장을 잡으면 사람이 곧 꺼버린다.
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"promote", "activate", "deploy_to_production"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 자체점검 함수는 제외한다 - 이름에 promote 가 들어가는 것이
            # 당연하다(이 검사 자신이 그렇다). 자기 이름에 걸리는 검사는
            # 아무것도 못 지키고 사람만 지치게 한다.
            if node.name.startswith(("_check_", "test_")):
                continue
            assert not any(b in node.name.lower() for b in banned),                 f"승격 함수가 생겼다: {node.name}"
    # ▶ SQL 리터럴까지 훑지 않는다 - **검사에 쓴 문장 자체가 잡힌다.**
    #   실제로 두 번 걸렸다(금지어 목록, 그 다음 SQL 패턴). 이 모듈은
    #   DB 를 아예 안 쓰므로 import 로 확인하는 쪽이 정확하다.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = ([a.name for a in node.names]
                    if isinstance(node, ast.Import) else [node.module or ""])
            for m in mods:
                assert "psycopg2" not in (m or ""),                     "DB 연결이 생겼다 - 관문은 판정만 하고 상태를 쓰지 않는다"


def _check_stored_names_reach_the_gate():
    """**2026-08-12 사고 원문을 픽스처로 넣는다.**

    실험 75a6d09e(momentum) 가 실제로 남긴 행이다. 초과 +157.51%p, IR 1.26,
    DSR 0.976 - 수치 조항을 다 통과하는데 MDD 와 회전율이 "미확인" 이라
    탈락했다. 이름이 안 맞아서였다. 그 행을 그대로 넣어 다시 나면 실패한다.
    """
    rows = [
        ("excess_return_pct", 157.51, "TEST"),
        ("information_ratio", 1.2552, "TEST"),
        ("deflated_sharpe", 0.9762, "TEST"),
        ("bootstrap_ci_low", -0.0029, "TEST"),
        ("max_drawdown", -0.5052, "TEST"),        # 비율. 관문은 퍼센트를 본다
        ("turnover_total", 114.8667, "TEST"),
        # 창별 행 - 섞이면 안 된다. 아래 -0.0709 가 전기간 낙폭으로 읽히면
        # -50.5% 짜리 전략이 -7% 로 통과한다.
        ("max_drawdown", -0.2544, "WALK_FORWARD"),
        ("max_drawdown", -0.0709, "WALK_FORWARD"),
    ]
    m = metrics_from_rows(rows)

    mdd = m.get("max_drawdown_pct")
    assert mdd is not None and abs(mdd - (-50.52)) < 1e-6, (
        f"전기간 낙폭이 퍼센트로 안 온다: {mdd}")
    assert m.get("turnover") == 114.8667, m.get("turnover")

    # ▶ 미확인이 아니라 **판정**이 나와야 한다.
    d = evaluate(m, fragility="ROBUST")
    assert "max_drawdown: 미확인 - 판정할 수 없다" not in d.reasons, d.reasons
    assert "turnover: 미확인 - 판정할 수 없다" not in d.reasons, d.reasons
    assert "max_drawdown" in d.failed, "MDD -50.52% 가 -35% 기준을 통과했다"
    assert "turnover" in d.passed, d.failed

    # ▶ **단위를 안 고치면 조항이 꺼진다** - 배수를 1.0 으로 되돌리면 통과한다.
    #   이름만 맞추고 끝냈으면 -0.5052 >= -35.0 으로 늘 통과했을 것이다.
    assert -0.5052 >= CRITERIA["max_drawdown_pct"], "단위 함정이 사라졌다"


def _check_full_period_only():
    """창별 행밖에 없으면 **미확인**이다. 창 성적으로 전기간을 대신하지 않는다."""
    m = metrics_from_rows([
        ("max_drawdown", -0.07, "WALK_FORWARD", {"window": "2025H1"})])
    assert "max_drawdown_pct" not in m, m
    d = evaluate(m, fragility="ROBUST")
    assert "max_drawdown" in d.failed
    assert any("미확인" in r for r in d.reasons), d.reasons


def _check_family_stats_are_not_split_filtered():
    """**PBO 는 WALK_FORWARD 에 적재된다.** split 으로 가르면 통째로 사라진다.

    (2026-08-12: 이 함수의 첫 판이 `TEST` 만 읽어 PBO 를 못 봤다. 관문은
    미확인을 통과로 안 치므로 어떤 실험도 영원히 못 넘을 뻔했다.)
    """
    m = metrics_from_rows([
        ("excess_return_pct", 157.51, "TEST", {}),
        ("pbo", 0.30, "WALK_FORWARD", {}),                 # 계열 통계 - 창이 없다
        ("pbo_n_windows", 5.0, "WALK_FORWARD", {"stat": "pbo"}),
        ("max_drawdown", -0.20, "WALK_FORWARD", {"window": "2025H1"}),  # 창별
    ])
    assert m.get("pbo") == 0.30, f"PBO 가 split 필터에 걸려 사라졌다: {m}"
    assert m.get("pbo_n_windows") == 5.0, m
    # 창별 낙폭은 여전히 안 들어온다 - 그건 전기간 기준과 비교할 값이 아니다
    assert "max_drawdown_pct" not in m, m
    assert "max_drawdown" not in m, m
    d = evaluate(m, fragility="ROBUST")
    assert "pbo" in d.passed, (d.failed, d.reasons)
    assert "pbo" not in d.unmeasured, d.unmeasured

    # dimensions 가 문자열(JSON)로 와도 같아야 한다 - 드라이버마다 다르다
    m2 = metrics_from_rows([("pbo", 0.30, "WALK_FORWARD", '{"stat": "pbo"}'),
                            ("sharpe", 1.0, "WALK_FORWARD", '{"window": "2025H1"}')])
    assert m2.get("pbo") == 0.30, m2
    assert "sharpe" not in m2, m2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{GATE_VERSION} 자체 점검 (DB 없음)")
    _check_all_pass_submits();       print("  전부 통과 -> 제출        OK")
    _check_overfit_stats_block();    print("  과적합 통계 차단        OK")
    _check_one_failure_blocks();     print("  하나 미달 -> 차단        OK")
    _check_missing_is_not_pass();    print("  미확인 != 통과          OK")
    _check_fragile_never_submits();  print("  FRAGILE 차단            OK")
    _check_trial_pressure_blocks();  print("  다중검정 차단           OK")
    _check_negative_excess_blocks(); print("  음수 초과 차단          OK")
    _check_gate_cannot_promote();    print("  승격 경로 부재          OK")
    _check_stored_names_reach_the_gate()
    print("  저장이름->관문 어휘     OK")
    _check_full_period_only();       print("  창 성적으로 전기간 대체 안함  OK")
    _check_family_stats_are_not_split_filtered()
    print("  계열 통계(PBO) 도달       OK")
    print("릴리스 관문 11개 영역 통과.")
