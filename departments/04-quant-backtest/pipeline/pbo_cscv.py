"""PBO - 백테스트 1등이 미래에도 1등인가 (CSCV).

담당: 재일 (퀀트·백테스트본부 QNT)
근거: contracts/quant_v2.probability_of_backtest_overfitting
      Bailey, Borwein, López de Prado, Zhu (2016) CSCV

▶ 무엇을 재는가
  한 Family 안의 변형들을 놓고, **기간을 반으로 갈라** 한쪽(IS)에서 1등이던
  변형이 다른 쪽(OOS)에서 중앙값 아래로 떨어지는 비율을 센다.
  높으면 "백테스트 1등" 이 미래를 예측하지 못한다는 뜻이다.

  trial_pressure 는 "몇 번 시도했나" 를 세고, DSR 은 그 횟수만큼 Sharpe 를
  깎는다. PBO 는 다른 것을 본다 - **고르는 행위 자체가 작동하는가.**
  20번 시도해도 1등이 계속 1등이면 실력이고, 매번 다른 것이 1등이면
  고르는 것이 무의미하다. 셋은 서로를 대체하지 않는다.

▶ 왜 overfit_stats.pbo() 가 호출처 0개였나
  is_ranks(분할별 IS 1등의 OOS 순위)를 만들 코드가 없었다. 재료인 창별
  수익률은 experiment_metrics 에 dimensions={"window": label} 로 이미
  쌓이고 있었는데 아무도 꺼내 쓰지 않았다.

▶ 같은 변형을 여러 번 돌린 것은 하나로 센다
  실측(2026-08-04): fam_2e407aad 의 변형 3개 중 둘이 수익률 벡터가 완전히
  같았다 - 같은 설정의 중복 실행이다. 그대로 세면 전략 수만 부풀어 PBO 가
  낙관적으로 나온다(같은 것끼리는 순위가 안 바뀌므로).

▶ 못 하면 None 이다
  변형 4개 미만, 창 4개 미만이면 계산하지 않는다. 0 을 내면 "과적합 없음"
  으로 읽히는데 그건 **안 재봤다**는 뜻과 정반대다. 릴리스 관문이 None 을
  fail-closed 로 막으므로, 표본이 얇으면 통과하지 못한다 - 의도한 것이다.

자체 점검: python departments/04-quant-backtest/pipeline/pbo_cscv.py
"""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

MODULE_VERSION = "quant-pbo-cscv-v1"

# 최소 요건. 미달이면 None 이다 - 0 은 "과적합 없음" 으로 읽힌다.
# ▶ 4 인 이유: PBO 는 IS 1등이 OOS 에서 **중앙값 아래**로 떨어지는 비율이다.
#   변형이 2개면 "중앙값 아래" = "2등" 이라 동전 던지기와 구분이 안 되고,
#   3개면 중앙값이 곧 2등 자신이다. 4개부터 위/아래가 각각 2개씩 생겨
#   순위가 의미를 갖는다. 미달이면 None 이고, 릴리스 관문이 fail-closed 로
#   막는다 - 표본이 얇은데 PBO 0.0 을 "과적합 없음" 으로 읽는 쪽이 훨씬 나쁘다.
#   (overfit_stats 의 MIN_RETURNS=60, method_performance 의 MIN_SCORED=20 과
#    같은 규율이다.)
MIN_VARIANTS = 4
MIN_WINDOWS = 4       # 양쪽에 최소 2창씩 - 1창짜리 순위는 잡음이다

# ▶ **창이 적으면 PBO 가 위로 편향된다**(2026-08-04 실측 S=5, PBO=0.8).
#   IS 가 S//2 창뿐이라(5창이면 2창) 그 2창의 1등은 상당 부분 잡음이고,
#   잡음으로 뽑은 1등이 OOS 에서 못 버티는 것은 당연하다. Bailey 원문은
#   S=16 을 8/8 로 가른다. 즉 지금 값은 "과적합이 이만큼 있다" 보다
#   **"이 표본으로는 고르는 행위를 신뢰할 수 없다"** 로 읽어야 한다.
#   방향은 맞고(관문을 막는 쪽) 크기는 과장돼 있다 - n_windows 를 함께
#   적재하는 이유다. 창을 늘리기 전에는 낮은 PBO 를 기대하지 않는다.
RELIABLE_WINDOWS = 8

# 분할 수 상한. 창이 많으면 C(S, S/2) 가 폭발한다(S=20 이면 184,756).
MAX_SPLITS = 2000


def dedupe_variants(perf: dict) -> tuple[dict, list]:
    """수익률 벡터가 같은 변형을 하나로 접는다.

    같은 설정을 여러 번 돌린 것을 각각 세면 전략 수만 부풀고, 같은 것끼리는
    순위가 안 바뀌므로 **PBO 가 낙관적으로 나온다.**
    """
    seen: dict[tuple, str] = {}
    kept, dropped = {}, []
    for vid in sorted(perf):                 # 결정론 - 어느 것을 남길지 고정
        sig = tuple(round(perf[vid][w], 10) for w in sorted(perf[vid]))
        if sig in seen:
            dropped.append({"variant": vid, "duplicate_of": seen[sig]})
            continue
        seen[sig] = vid
        kept[vid] = perf[vid]
    return kept, dropped


def _rank_of(vid: str, scores: dict) -> int:
    """OOS 순위(1 = 최고). 동점은 **불리하게** 센다 - 동점을 1등으로 세면
    구분 못 하는 것을 실력으로 읽는다."""
    better = sum(1 for k, v in scores.items()
                 if v > scores[vid] or (v == scores[vid] and k != vid))
    return better + 1


def is_ranks_cscv(perf: dict, *, max_splits: int = MAX_SPLITS) -> dict:
    """분할마다 IS 1등을 뽑아 그 OOS 순위를 모은다.

    perf: {variant_id: {window_label: return}}
    창 수가 홀수여도 돈다(IS = S//2 창, OOS = 나머지) - 데이터를 조용히
    버리지 않는다. 표준 CSCV 는 짝수를 쓰지만 버리는 쪽이 더 나쁘다.
    """
    if len(perf) < MIN_VARIANTS:
        return {"is_ranks": [], "reason":
                f"변형이 {len(perf)}개 - 최소 {MIN_VARIANTS}개 필요"
                f"(4개 미만이면 '중앙값 아래' 에 해상도가 없다)"}

    # 모든 변형이 공통으로 가진 창만 쓴다 - 창이 다르면 비교가 아니다
    common = set.intersection(*(set(v) for v in perf.values()))
    windows = sorted(common)
    if len(windows) < MIN_WINDOWS:
        return {"is_ranks": [], "reason":
                f"공통 창이 {len(windows)}개 - 최소 {MIN_WINDOWS}개 필요"
                f"(양쪽에 2창씩 있어야 순위가 잡음이 아니다)"}

    half = len(windows) // 2
    splits = list(combinations(range(len(windows)), half))
    truncated = 0
    if len(splits) > max_splits:
        truncated = len(splits) - max_splits
        splits = splits[:max_splits]        # 조용히 자르지 않는다 - 아래에 남긴다

    ranks = []
    for idx in splits:
        is_w = [windows[i] for i in idx]
        oos_w = [w for w in windows if w not in set(is_w)]
        is_score = {v: sum(perf[v][w] for w in is_w) / len(is_w) for v in perf}
        oos_score = {v: sum(perf[v][w] for w in oos_w) / len(oos_w) for v in perf}
        # IS 1등. 동점이면 이름순 - 결정론이어야 재현된다.
        best = max(sorted(is_score), key=lambda v: is_score[v])
        ranks.append(_rank_of(best, oos_score))

    out = {"is_ranks": ranks, "n_splits": len(splits),
           "windows": len(windows), "variants": len(perf)}
    if truncated:
        # 조용한 절단 금지 - 무엇을 안 봤는지 남긴다
        out["truncated_splits"] = truncated
    return out


def compute(perf: dict, *, max_splits: int = MAX_SPLITS) -> dict:
    """창별 수익률 -> PBO. **못 하면 None 이고 이유가 붙는다.**"""
    kept, dropped = dedupe_variants(perf)
    r = is_ranks_cscv(kept, max_splits=max_splits)
    if not r["is_ranks"]:
        return {"probability_of_backtest_overfitting": None,
                "reason": r["reason"],
                "duplicate_variants": dropped}

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from overfit_stats import pbo as pbo_stat

    res = pbo_stat(r["is_ranks"], len(kept))
    out = {"probability_of_backtest_overfitting": res.get("pbo"),
           "n_splits": r["n_splits"], "n_windows": r["windows"],
           "n_variants": len(kept)}
    if r["windows"] < RELIABLE_WINDOWS:
        # 값을 숨기지도, 그냥 믿게 두지도 않는다 - 위로 편향돼 있다고 말한다
        out["caveat"] = (
            f"창 {r['windows']}개 (신뢰 기준 {RELIABLE_WINDOWS}개) - IS 가 "
            f"{r['windows'] // 2}창뿐이라 1등 선정에 잡음이 크고 PBO 가 위로 "
            f"편향된다. '과적합이 이만큼' 이 아니라 '이 표본으로는 고르는 "
            f"행위를 신뢰할 수 없다' 로 읽는다")
    if dropped:
        # 같은 설정을 여러 번 돌린 것을 접었다는 사실을 남긴다
        out["duplicate_variants"] = dropped
    if "truncated_splits" in r:
        out["truncated_splits"] = r["truncated_splits"]
    if res.get("pbo") is None:
        out["reason"] = res.get("reason", "계산 불가")
    return out


def load_family_performance(conn, family_id: str, *,
                            metric: str = "total_return") -> dict:
    """DB 에서 Family 의 창별 성적을 꺼낸다.

    창별 원값은 experiment_metrics 에 dimensions={"window": label} 로 이미
    쌓여 있다 - SUMMARY 행은 창이 아니므로 뺀다.
    """
    perf: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.experiment_id, m.dimensions->>'window', m.value
              from quant.experiments e
              join quant.experiment_metrics m
                on m.experiment_id = e.experiment_id
             where e.trial_family_id = %s
               and m.metric = %s
               and m.dimensions->>'window' is not null
               and m.dimensions->>'window' <> 'SUMMARY'
            """, (family_id, metric))
        for eid, w, v in cur.fetchall():
            perf.setdefault(str(eid), {})[w] = float(v)
    return perf


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_W = ["2024H2", "2025H1", "2025H2", "2026H1"]


def _perf(**kw) -> dict:
    return {k: dict(zip(_W, v)) for k, v in kw.items()}


def _check_persistent_winner_is_low_pbo():
    """**계속 1등이면 PBO 가 낮다** - 고르는 행위가 작동한다는 뜻이다."""
    p = _perf(a=[0.10, 0.11, 0.09, 0.12],      # 모든 창에서 최고
              b=[0.01, 0.02, 0.00, 0.01],
              c=[-0.05, -0.04, -0.06, -0.03],
              d=[-0.09, -0.08, -0.10, -0.07])
    r = compute(p)
    assert r["probability_of_backtest_overfitting"] == 0.0, r


def _check_alternating_winner_is_high_pbo():
    """**매번 다른 것이 1등이면 PBO 가 높다** - 고르는 것이 무의미하다."""
    p = _perf(a=[0.30, -0.20, 0.30, -0.20],    # 앞창 강세
              b=[-0.20, 0.30, -0.20, 0.30],    # 뒷창 강세 (정반대)
              c=[0.25, -0.18, 0.26, -0.19],
              d=[-0.18, 0.27, -0.17, 0.26])
    r = compute(p)
    assert r["probability_of_backtest_overfitting"] > 0.4, r


def _check_duplicate_variants_are_folded():
    """**같은 설정 중복 실행을 각각 세면 PBO 가 낙관적으로 나온다**
    (2026-08-04 실측: fam_2e407aad 변형 3개 중 둘이 동일 벡터였다)."""
    same = [0.10, 0.11, 0.09, 0.12]
    p = _perf(a=same, a_dup=list(same), b=[0.01, 0.02, 0.00, 0.01],
              c=[-0.05, -0.04, -0.06, -0.03], d=[-0.09, -0.08, -0.10, -0.07])
    r = compute(p)
    assert r["n_variants"] == 4, r
    assert r["duplicate_variants"], r
    assert r["duplicate_variants"][0]["duplicate_of"] == "a", r


def _check_insufficient_returns_none_not_zero():
    """**0 을 내지 않는다** - 0 은 "과적합 없음" 인데 "안 재봤다" 와 정반대다."""
    # 변형 2개는 "중앙값 아래" 에 해상도가 없다 - 0.0 을 내면 안 된다
    two = compute(_perf(a=[0.1, 0.2, 0.3, 0.4], b=[0.0, 0.1, 0.2, 0.3]))
    assert two["probability_of_backtest_overfitting"] is None, two
    assert "변형이 2개" in two["reason"], two

    short = compute({k: {"w1": v, "w2": v + 0.1, "w3": v}
                     for k, v in zip("abcd", (0.1, 0.0, -0.1, -0.2))})
    assert short["probability_of_backtest_overfitting"] is None, short
    assert "공통 창" in short["reason"], short


def _check_only_common_windows_are_compared():
    """창이 다르면 비교가 아니다 - 겹치는 창만 쓴다."""
    p = {"a": dict(zip(_W, [0.1, 0.2, 0.3, 0.4])),
         "b": dict(zip(_W, [0.0, 0.1, 0.2, 0.3]), extra=9.9),
         "c": dict(zip(_W, [-0.1, 0.0, 0.1, 0.2])),
         "d": dict(zip(_W, [-0.2, -0.1, 0.0, 0.1]))}
    r = compute(p)
    assert r["n_windows"] == 4, r          # extra 는 a 에 없으므로 제외


def _check_deterministic():
    """같은 입력이면 같은 답이다 - 재현 안 되는 통계는 근거가 아니다."""
    p = _perf(a=[0.3, -0.2, 0.3, -0.2], b=[-0.2, 0.3, -0.2, 0.3],
              c=[0.05, 0.05, 0.05, 0.05], d=[-0.01, 0.02, -0.03, 0.04])
    assert compute(p) == compute(p)


def _check_small_sample_carries_caveat():
    """**작은 표본을 그냥 믿게 두지 않는다** (2026-08-04 실측 S=5, PBO=0.8).

    창이 적으면 IS 가 S//2 창뿐이라 1등 선정이 잡음이고 PBO 가 위로 편향된다.
    값을 숨기지도 않고 무조건 믿게 두지도 않는다.
    """
    p = _perf(a=[0.3, -0.2, 0.3, -0.2], b=[-0.2, 0.3, -0.2, 0.3],
              c=[0.25, -0.18, 0.26, -0.19], d=[-0.18, 0.27, -0.17, 0.26])
    r = compute(p)
    assert r["probability_of_backtest_overfitting"] is not None, r
    assert "편향" in r.get("caveat", ""), r


def _check_split_cap_is_reported():
    """**조용히 자르지 않는다** - 무엇을 안 봤는지 남는다."""
    p = _perf(a=[0.3, -0.2, 0.3, -0.2], b=[-0.2, 0.3, -0.2, 0.3],
              c=[0.25, -0.18, 0.26, -0.19], d=[-0.18, 0.27, -0.17, 0.26])
    r = compute(p, max_splits=2)
    assert r.get("truncated_splits") == 4, r   # C(4,2)=6, 2 만 봄


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_persistent_winner_is_low_pbo();     print("  지속 1등 -> 낮은 PBO    OK")
    _check_alternating_winner_is_high_pbo();   print("  교대 1등 -> 높은 PBO    OK")
    _check_duplicate_variants_are_folded();    print("  중복 변형 접기          OK")
    _check_insufficient_returns_none_not_zero(); print("  부족 -> None(0 아님)   OK")
    _check_only_common_windows_are_compared(); print("  공통 창만 비교          OK")
    _check_deterministic();                    print("  결정론                  OK")
    _check_small_sample_carries_caveat();      print("  작은 표본 경고          OK")
    _check_split_cap_is_reported();            print("  절단 보고               OK")
    print("PBO(CSCV) 8개 영역 통과.")
