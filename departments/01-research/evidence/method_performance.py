"""방법 단위 성과를 판정으로 되돌린다 - 학습 계층 2단.

담당: 재일 (리서치본부 RES)
근거: HEDGE_FUND_MASTER_PLAN.md, P0-9 방법론 단위 성과 귀속

▶ 무엇이 끊겨 있었나
  선순환의 앞쪽은 다 있었다:
    packet_claims(method_key)  -> 어떤 기법이 낸 주장인가
    packet_outcome_scorer      -> 지평이 지나면 시세로 대조
    research.method_calibration -> 방법별 발동률·Brier 집계 (뷰)

  그런데 **그 숫자를 아무도 되읽지 않는다.** 뷰까지 계산해놓고 다음 실행이
  모른다 - 적중률 30% 짜리 기법과 70% 짜리 기법을 리포트가 똑같은 어조로
  인용한다. 계산은 다 해놓고 마지막 한 칸이 비어 자가 발전이 안 되던 자리다.

▶ 이 모듈이 하는 것과 하지 않는 것
  한다   : 성과 숫자 -> 사람이 읽는 판정(신뢰 등급)과 강등 후보 목록
  안 한다: methods.py 의 status 를 자동으로 고치는 것

  왜 자동 강등을 안 하는가. methods.py 는 **소스 코드**이고 status 는 사람이
  승인한 선언이다. 기계가 소스를 고치기 시작하면 (a) 어느 실행이 무엇을 바꿨는지
  추적이 어렵고 (b) 나쁜 표본 한 번이 지표를 영구히 끄고 (c) 리뷰 없이 파이프라인
  거동이 바뀐다. 대신 **강등 후보를 드러내고 그 사실을 서술에 싣는다** -
  리포트가 스스로 "이 신호는 최근 성적이 나쁘다" 고 말하면 그것만으로 질이 오르고,
  실제 강등은 사람이 근거를 보고 한다.

▶ 소표본은 판정이 아니다
  n 이 작을 때 나온 적중률은 잡음이다. 임계 미만이면 **등급을 매기지 않고**
  INSUFFICIENT 로 둔다 - 0 이나 '나쁨' 으로 채우면 새로 도입한 지표가 표본이
  없다는 이유만으로 전부 나쁜 기법이 된다. 마이그레이션 주석도 같은 것을
  경고한다: "표본이 쌓이기 전까지 이 뷰의 숫자로 가중치를 바꾸지 않는다 -
  소표본 과적합은 되먹임이 아니라 잡음 증폭이다."

자체 점검: python departments/01-research/evidence/method_performance.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable, Optional

MODULE_VERSION = "research-method-performance-v1"

# ── 판정 상수 ────────────────────────────────────────────────────────────────
# 등급을 매기려면 최소 이만큼의 채점된 표본이 필요하다. 20 은 임의가 아니라
# 발동률 0.3 근방에서 표준오차가 ~0.10 이 되는 지점이다 - 그보다 적으면
# 0.3 과 0.5 를 구분할 수 없고, 구분 못 하는 숫자로 등급을 매기면 잡음이다.
MIN_SCORED = 20

# 발동률 기준. 주장은 "이 조건이 성립할 것" 이므로 높을수록 잘 맞춘 것이다.
STRONG_RATE = 0.60
WEAK_RATE = 0.35

# Brier 는 낮을수록 좋다. 0.25 는 "항상 0.5 라고 답한" 것과 같은 점수 -
# 그보다 나쁘면 확률을 말할 자격이 없다는 뜻이다.
BRIER_USELESS = 0.25

GRADE_STRONG = "STRONG"
GRADE_MIXED = "MIXED"
GRADE_WEAK = "WEAK"
GRADE_INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True)
class MethodPerf:
    """한 (방법, 지평) 의 성과 판정."""
    method_key: str
    horizon_days: int
    scored: int
    trigger_rate: Optional[float]
    brier_score: Optional[float]
    grade: str
    reason: str

    @property
    def is_demotion_candidate(self) -> bool:
        """강등 후보인가. **강등 그 자체는 아니다** - 사람이 판단한다."""
        return self.grade == GRADE_WEAK

    def as_dict(self) -> dict:
        return {
            "method_key": self.method_key,
            "horizon_days": self.horizon_days,
            "scored": self.scored,
            # ▶ 계산 못 한 것과 0 을 구분한다. None 을 0 으로 채우면 표본이
            #   없는 기법이 "한 번도 안 맞은 기법" 으로 읽힌다.
            "trigger_rate_pct": (None if self.trigger_rate is None
                                 else round(self.trigger_rate * 100.0, 1)),
            "brier_score": self.brier_score,
            "grade": self.grade,
            "reason": self.reason,
        }


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def grade_one(row: dict) -> MethodPerf:
    """method_calibration 한 행 -> 판정. 순수 함수."""
    key = str(row.get("method_key") or "").strip()
    horizon = int(row.get("horizon_days") or 0)
    scored = int(row.get("scored") or 0)
    rate = _num(row.get("trigger_rate"))
    brier = _num(row.get("brier_score"))

    if scored < MIN_SCORED:
        return MethodPerf(key, horizon, scored, rate, brier, GRADE_INSUFFICIENT,
                          f"채점 표본 {scored}건 - 등급 판정에 {MIN_SCORED}건 필요")
    if rate is None:
        # 표본은 있는데 발동률이 없다 = 채점이 안 끝났거나 계약 위반이다.
        # 통과로 위장하지 않는다.
        return MethodPerf(key, horizon, scored, rate, brier, GRADE_INSUFFICIENT,
                          "채점 표본은 있으나 발동률이 계산되지 않았다")

    if rate >= STRONG_RATE:
        grade = GRADE_STRONG
        reason = f"{horizon}일 지평 발동률 {rate * 100:.0f}% (n={scored})"
    elif rate < WEAK_RATE:
        grade = GRADE_WEAK
        reason = (f"{horizon}일 지평 발동률 {rate * 100:.0f}% - "
                  f"기준 {WEAK_RATE * 100:.0f}% 미만 (n={scored})")
    else:
        grade = GRADE_MIXED
        reason = f"{horizon}일 지평 발동률 {rate * 100:.0f}% (n={scored})"

    # 확률을 말하는데 Brier 가 무작위보다 나쁘면 등급을 한 칸 내린다.
    # 방향은 맞혀도 확신의 크기가 틀리면 그 확률을 근거로 쓸 수 없다.
    if brier is not None and brier > BRIER_USELESS:
        if grade == GRADE_STRONG:
            grade = GRADE_MIXED
        elif grade == GRADE_MIXED:
            grade = GRADE_WEAK
        reason += f"; Brier {brier:.3f} 로 무작위({BRIER_USELESS}) 보다 나쁨"

    return MethodPerf(key, horizon, scored, rate, brier, grade, reason)


def grade_all(rows: Iterable[dict]) -> list[MethodPerf]:
    """행 목록 -> 판정 목록. 표본 많은 것부터."""
    out = [grade_one(r) for r in rows if (r or {}).get("method_key")]
    out.sort(key=lambda p: (-p.scored, p.method_key, p.horizon_days))
    return out


def demotion_candidates(perfs: Iterable[MethodPerf]) -> list[MethodPerf]:
    """강등 후보. **자동 강등이 아니라 사람에게 내미는 목록이다.**"""
    return [p for p in perfs if p.is_demotion_candidate]


def performance_note(perfs: Iterable[MethodPerf], used_keys: Iterable[str]) -> dict:
    """이번 실행이 **실제로 쓴** 방법들의 성적만 추린다.

    쓰지도 않은 기법의 성적을 서술에 실으면 근거와 무관한 소음이 된다.
    """
    want = {k for k in used_keys if k}
    hit = [p for p in perfs if p.method_key in want]
    weak = [p for p in hit if p.grade == GRADE_WEAK]
    strong = [p for p in hit if p.grade == GRADE_STRONG]
    unknown = sorted(want - {p.method_key for p in hit})
    return {
        "graded": [p.as_dict() for p in hit],
        # 서술이 어조를 낮춰야 하는 근거. 리포트가 스스로 성적을 말하게 한다.
        "weak_methods": [p.method_key for p in weak],
        "strong_methods": [p.method_key for p in strong],
        # ▶ 성과가 **없는** 것과 나쁜 것은 다르다. 이름을 남겨야 사람이 안다.
        "unscored_methods": unknown,
        "caution": (f"성적이 낮은 방법 {len(weak)}종을 근거로 썼다: "
                    + ", ".join(p.method_key for p in weak)) if weak else None,
    }


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _row(key="momentum_20d", horizon=20, scored=50, rate=0.7, brier=None):
    return {"method_key": key, "horizon_days": horizon, "scored": scored,
            "trigger_rate": rate, "brier_score": brier}


def _check_small_sample_is_not_bad():
    """표본이 적은 것과 성적이 나쁜 것은 **다르다.**

    새 지표는 표본이 없다. 그것을 WEAK 로 매기면 도입하자마자 전부 나쁜
    기법이 되고, 그러면 아무도 새 지표를 안 넣는다.
    """
    p = grade_one(_row(scored=3, rate=0.0))
    assert p.grade == GRADE_INSUFFICIENT, p
    assert p.is_demotion_candidate is False, "소표본이 강등 후보가 되면 안 된다"
    assert "3건" in p.reason, p.reason
    # 경계: 임계 직전과 직후
    assert grade_one(_row(scored=MIN_SCORED - 1, rate=0.9)).grade == GRADE_INSUFFICIENT
    assert grade_one(_row(scored=MIN_SCORED, rate=0.9)).grade == GRADE_STRONG


def _check_missing_rate_is_not_zero():
    """발동률이 None 이면 '0%' 가 아니라 판정 불가다."""
    p = grade_one(_row(rate=None))
    assert p.grade == GRADE_INSUFFICIENT, p
    assert p.as_dict()["trigger_rate_pct"] is None, "None 이 0 으로 채워졌다"


def _check_grades():
    assert grade_one(_row(rate=0.75)).grade == GRADE_STRONG
    assert grade_one(_row(rate=0.45)).grade == GRADE_MIXED
    assert grade_one(_row(rate=0.20)).grade == GRADE_WEAK
    assert grade_one(_row(rate=0.20)).is_demotion_candidate is True


def _check_bad_brier_downgrades():
    """방향은 맞혀도 확신의 크기가 틀리면 그 확률은 근거가 못 된다."""
    good = grade_one(_row(rate=0.75, brier=0.10))
    assert good.grade == GRADE_STRONG, good
    bad = grade_one(_row(rate=0.75, brier=0.40))
    assert bad.grade == GRADE_MIXED, bad
    assert "Brier" in bad.reason, bad.reason
    # MIXED 에서 한 칸 더 내려가면 WEAK 이고 강등 후보가 된다
    worse = grade_one(_row(rate=0.45, brier=0.40))
    assert worse.grade == GRADE_WEAK and worse.is_demotion_candidate


def _check_note_only_covers_used_methods():
    """쓰지도 않은 기법의 성적은 소음이다."""
    perfs = grade_all([_row("momentum_20d", rate=0.20),
                       _row("adx_trend", rate=0.80),
                       _row("안쓴기법", rate=0.10)])
    note = performance_note(perfs, used_keys=["momentum_20d", "adx_trend", "신규기법"])
    keys = {g["method_key"] for g in note["graded"]}
    assert keys == {"momentum_20d", "adx_trend"}, keys
    assert note["weak_methods"] == ["momentum_20d"], note
    assert note["strong_methods"] == ["adx_trend"], note
    # 성적이 **없는** 것은 나쁜 것이 아니다 - 이름을 남긴다
    assert note["unscored_methods"] == ["신규기법"], note
    assert "momentum_20d" in (note["caution"] or "")


def _check_no_weak_means_no_caution():
    perfs = grade_all([_row("adx_trend", rate=0.80)])
    note = performance_note(perfs, used_keys=["adx_trend"])
    assert note["caution"] is None, note


def _check_sorted_by_sample():
    perfs = grade_all([_row("a", scored=5), _row("b", scored=99), _row("c", scored=40)])
    assert [p.method_key for p in perfs] == ["b", "c", "a"], perfs


def _check_does_not_touch_registry():
    """이 모듈은 **methods.py 를 고치지 않는다.** 자동 강등은 하지 않는다."""
    # ▶ **문자열 검색이 아니라 AST 로 본다.** 처음엔 금지어 목록으로 훑었는데
    #   목록 안의 "methods.py" 자체에 걸려 늘 실패했다 - 검사가 자기 문장을
    #   잡는 전형적인 오탐이고, 그런 검사는 사람이 곧 꺼버린다.
    import ast

    tree = ast.parse(__import__("pathlib").Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") != "methods", "레지스트리를 import 한다"
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "methods", "레지스트리를 import 한다"
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("write_text", "write_bytes"),                 "파일을 쓴다 - 이 모듈은 판정만 낸다"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", "파일을 연다 - 이 모듈은 판정만 낸다"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_small_sample_is_not_bad();      print("  소표본 != 나쁨          OK")
    _check_missing_rate_is_not_zero();     print("  결측 != 0%              OK")
    _check_grades();                       print("  등급 경계               OK")
    _check_bad_brier_downgrades();         print("  Brier 강등              OK")
    _check_note_only_covers_used_methods();print("  쓴 기법만 서술          OK")
    _check_no_weak_means_no_caution();     print("  경고 남발 안 함         OK")
    _check_sorted_by_sample();             print("  표본순 정렬             OK")
    _check_does_not_touch_registry();      print("  레지스트리 불변         OK")
    print("방법 성과 판정 8개 영역 통과.")
