#!/usr/bin/env python3
"""뉴스 감시 2계층 계획 - 한도 안에서 전종목을 훑는다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 "2,595 종목으로 뉴스 공시 수집 확대" ->
      실측 후 "2계층으로 가자".

▶ 왜 전종목을 같은 빈도로 못 도는가 (실측 2026-08-03)
  NAVER 일 한도 22,500(90% = 20,250)에서
    2,595종목을 25분 간격  -> 149,472회/일  (한도의 6.6배)
    2,595종목을 한도 안으로 -> 간격 **185분**
  185분이면 속보성이 사라진다. 뉴스에서 3시간 지연은 사실상 못 쓰는 것이다.

▶ 그래서 중요도로 나눈다
  Tier1 핵심 바스켓(코스피200+코스닥150) - 짧은 간격 유지. 속보는 여기서 잡는다.
  Tier2 나머지 전종목            - 남는 한도로 **순회**. 하루 몇 번이라도 훑는다.

  Tier2 를 순회로 도는 이유: 전부를 매 sweep 마다 부르면 한도가 즉시 터진다.
  한 sweep 에 slice 만 부르고 다음 sweep 은 그다음 slice 를 부른다 - 그러면
  한 바퀴 도는 데 걸리는 시간이 곧 Tier2 의 실효 간격이 된다.

▶ 놓치는 것을 숨기지 않는다
  Tier2 의 실효 간격(cycle_hours)을 계획에 실어 보고한다. "전종목 수집 중"
  이라고만 하면 하위 종목이 하루 한 번 훑인다는 사실이 가려진다.
  그리고 **LS 뉴스가 실시간 푸시로 같은 종목을 이미 덮는다**(실측 1,142종목,
  전종목 구독 후 확대 중) - NAVER Tier2 는 보완이지 유일한 경로가 아니다.

실행: python collectors/news_watch_tiers.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

MODULE_VERSION = "research-news-tiers-v1"

# 한도는 naver_news_collector 가 유일한 출처다. 여기 숫자를 따로 두었더니
# 실제 25,000 인데 22,500 으로 계획해 Tier2 를 매일 5,700회씩 놀렸다
# (실측 2026-08-03). 상수를 두 곳에 두면 언젠가 반드시 갈라진다.
from naver_news_collector import DAILY_QUOTA  # noqa: E402
QUOTA_SOFT_RATIO = 0.9        # 한도의 90% 를 상한으로 - 밤에 조용히 죽는 것보다 낫다
SECONDS_PER_DAY = 86_400


class TierPlanError(ValueError):
    """계획이 한도를 넘거나 입력이 모순이다. 조용히 줄이지 않는다."""


def plan_tiers(core: list[str], full: list[str], *,
               core_interval_seconds: float,
               quota: int = DAILY_QUOTA,
               soft_ratio: float = QUOTA_SOFT_RATIO) -> dict:
    """(핵심, 전종목) -> 2계층 폴링 계획.

    core 는 full 의 부분집합이 아닐 수도 있다(감시 바스켓에만 있고 거래정지로
    전종목에서 빠진 종목). 그때 core 를 버리지 않는다 - 감시 대상으로 지정된
    이유가 따로 있고, 그 판단을 여기서 뒤집지 않는다.

    Tier2 = full - core. Tier1 이 쓰고 남은 한도를 Tier2 가 순회로 나눠 쓴다.
    남는 한도가 0 이하면 **예외** - 조용히 Tier2 를 0 으로 만들면 "전종목
    수집 중" 이라는 거짓이 된다.
    """
    if core_interval_seconds <= 0:
        raise TierPlanError("core_interval_seconds 는 양수여야 한다")
    core_set = list(dict.fromkeys(core or []))          # 순서 유지 + 중복 제거
    rest = [s for s in dict.fromkeys(full or []) if s not in set(core_set)]

    ceiling = int(quota * soft_ratio)
    sweeps_per_day = SECONDS_PER_DAY / core_interval_seconds
    tier1_calls = int(round(len(core_set) * sweeps_per_day))
    remaining = ceiling - tier1_calls

    if remaining <= 0:
        raise TierPlanError(
            f"Tier1({len(core_set)}종목 × {sweeps_per_day:.0f}회/일 = "
            f"{tier1_calls:,})만으로 한도 {ceiling:,} 를 다 쓴다 - 간격을 늘리거나 "
            f"핵심 바스켓을 줄여야 Tier2 가 성립한다")

    # 한 sweep 에서 Tier2 를 몇 개나 부를 수 있나
    per_sweep = int(remaining // sweeps_per_day) if sweeps_per_day else 0
    if rest and per_sweep < 1:
        raise TierPlanError(
            f"남는 한도 {remaining:,}회/일로는 sweep 당 1종목도 못 부른다 - "
            f"Tier1 간격을 늘려야 한다")

    cycle_sweeps = (len(rest) + per_sweep - 1) // per_sweep if per_sweep else 0
    cycle_hours = cycle_sweeps * core_interval_seconds / 3600.0 if cycle_sweeps else 0.0
    tier2_calls = int(round(len(rest) * (SECONDS_PER_DAY / (cycle_hours * 3600))
                            )) if cycle_hours else 0

    return {
        "tier1": core_set,
        "tier2": rest,
        "core_interval_seconds": core_interval_seconds,
        "tier2_per_sweep": per_sweep,
        "tier2_cycle_sweeps": cycle_sweeps,
        "tier2_cycle_hours": round(cycle_hours, 2),
        "projected_daily_calls": tier1_calls + tier2_calls,
        "quota_ceiling": ceiling,
        "headroom": ceiling - (tier1_calls + tier2_calls),
        "note": (f"Tier1 {len(core_set)}종목 {core_interval_seconds/60:.0f}분 간격, "
                 f"Tier2 {len(rest)}종목 sweep 당 {per_sweep}개씩 순회 "
                 f"(한 바퀴 {cycle_hours:.1f}시간)"),
    }


def sweep_symbols(plan: dict, sweep_index: int) -> list[str]:
    """이번 sweep 에서 부를 종목 = Tier1 전부 + Tier2 의 이번 slice.

    slice 는 sweep_index 로 결정론적으로 정해진다 - 무작위로 고르면 어떤
    종목이 언제 훑였는지 재현할 수 없고, 운 나쁘면 며칠씩 안 뽑힌다.
    """
    t1 = list(plan.get("tier1") or [])
    t2 = list(plan.get("tier2") or [])
    per = int(plan.get("tier2_per_sweep") or 0)
    if not t2 or per <= 0:
        return t1
    cycle = plan.get("tier2_cycle_sweeps") or 1
    start = (int(sweep_index) % cycle) * per
    return t1 + t2[start:start + per]


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _syms(n: int, prefix: str = "S") -> list[str]:
    return [f"{prefix}{i:06d}" for i in range(n)]


def _check_realistic_plan():
    """실측 규모(핵심 350 / 전종목 2,595)에서 계획이 성립하는가."""
    core, full = _syms(350, "C"), _syms(350, "C") + _syms(2245, "R")
    p = plan_tiers(core, full, core_interval_seconds=1800)   # 30분
    assert len(p["tier1"]) == 350 and len(p["tier2"]) == 2245
    assert p["projected_daily_calls"] <= p["quota_ceiling"], p
    assert p["headroom"] >= 0
    assert p["tier2_per_sweep"] >= 1
    # 한 바퀴가 하루를 크게 넘지 않아야 쓸모가 있다
    assert p["tier2_cycle_hours"] <= 48, p["tier2_cycle_hours"]
    print(f"  실측 규모 계획           OK (Tier2 한바퀴 {p['tier2_cycle_hours']}h)")


def _check_quota_never_exceeded():
    """어떤 간격에서도 예상 호출이 상한을 넘지 않는다 - 넘으면 예외지 절삭이 아니다."""
    core, full = _syms(350, "C"), _syms(350, "C") + _syms(2245, "R")
    for interval in (1500, 1800, 2400, 3600):
        p = plan_tiers(core, full, core_interval_seconds=interval)
        assert p["projected_daily_calls"] <= p["quota_ceiling"], (interval, p)
    # Tier1 만으로 한도를 다 쓰면 조용히 Tier2 를 0 으로 만들지 않고 예외
    try:
        plan_tiers(_syms(2000, "C"), _syms(2600, "R"), core_interval_seconds=600)
        raise AssertionError("한도를 넘겼는데 계획이 나왔다")
    except TierPlanError as e:
        assert "한도" in str(e)
    print("  한도 초과 = 예외         OK")


def _check_rotation_covers_all():
    """순회가 **모든 Tier2 종목을 빠짐없이** 훑는가 - 한 바퀴 돌면 전부 나와야 한다."""
    core, full = _syms(10, "C"), _syms(10, "C") + _syms(95, "R")
    p = plan_tiers(core, full, core_interval_seconds=1800)
    seen = set()
    for i in range(p["tier2_cycle_sweeps"]):
        s = sweep_symbols(p, i)
        assert set(p["tier1"]) <= set(s), "Tier1 은 매 sweep 마다 들어간다"
        seen |= set(s) - set(p["tier1"])
    assert seen == set(p["tier2"]), f"순회에서 빠진 종목 {len(set(p['tier2']) - seen)}개"
    # 결정론 - 같은 index 면 같은 결과
    assert sweep_symbols(p, 3) == sweep_symbols(p, 3)
    # 한 바퀴 뒤에는 처음으로 돌아온다
    assert sweep_symbols(p, 0) == sweep_symbols(p, p["tier2_cycle_sweeps"])
    print("  순회 전수 커버·결정론    OK")


def _check_core_not_dropped():
    """전종목에 없는 핵심 종목(거래정지 등)을 버리지 않는다."""
    core = ["HALTED01", "C000001"]
    full = ["C000001", "R000001"]
    p = plan_tiers(core, full, core_interval_seconds=1800)
    assert "HALTED01" in p["tier1"], "감시 지정 종목을 전종목 목록이 없다고 버렸다"
    assert "C000001" not in p["tier2"], "핵심이 Tier2 에 중복되면 한도를 두 번 쓴다"
    assert p["tier2"] == ["R000001"]
    print("  핵심 보존·중복 방지      OK")


def _check_empty_and_edges():
    # Tier2 가 없으면 순회 없이 Tier1 만
    p = plan_tiers(_syms(10, "C"), _syms(10, "C"), core_interval_seconds=1800)
    assert p["tier2"] == [] and sweep_symbols(p, 5) == p["tier1"]
    assert p["tier2_cycle_hours"] == 0.0
    # 잘못된 간격은 예외
    for bad in (0, -1):
        try:
            plan_tiers(["A"], ["A"], core_interval_seconds=bad)
            raise AssertionError("음수 간격이 통과했다")
        except TierPlanError:
            pass
    print("  경계 조건                OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_realistic_plan()
    _check_quota_never_exceeded()
    _check_rotation_covers_all()
    _check_core_not_dropped()
    _check_empty_and_edges()
    print("뉴스 2계층 5개 영역 통과.")
