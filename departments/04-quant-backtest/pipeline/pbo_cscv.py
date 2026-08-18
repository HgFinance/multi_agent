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
import json
import math
import random
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stock_universe import governed_stock_evidence_sql  # noqa: E402

MODULE_VERSION = "quant-pbo-cscv-v5"

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
RECOMMENDED_WINDOWS = 16
RELIABLE_VARIANTS = 20

# 분할 수 상한. 창이 많으면 C(S, S/2) 가 폭발한다(S=20 이면 184,756).
MAX_SPLITS = 2000

_GOVERNED_PBO_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="manifest", hypothesis_alias="h")


def dedupe_variants(perf: dict, *,
                    candidate_identities: dict | None = None) -> tuple[dict, list]:
    """Fold only candidates proven to have the same predeclared identity.

    Equal realised returns do not prove that two formula/configuration trials
    are the same candidate. Collapsing them would discard legitimate trials
    and can understate selection pressure. Without a complete identity map,
    every labelled candidate is therefore retained.
    """
    if candidate_identities is None:
        return dict(perf), []
    if set(candidate_identities) != set(perf):
        raise ValueError("candidate identity map must cover every variant")

    seen: dict[object, object] = {}
    kept, dropped = {}, []
    for variant_id in sorted(perf, key=str):
        identity = candidate_identities[variant_id]
        if identity is None:
            raise ValueError("candidate identities must be non-null")
        try:
            is_duplicate = identity in seen
        except TypeError as exc:
            raise ValueError("candidate identities must be hashable") from exc
        if is_duplicate:
            dropped.append({"variant": variant_id,
                            "duplicate_of": seen[identity]})
            continue
        seen[identity] = variant_id
        kept[variant_id] = perf[variant_id]
    return kept, dropped


def _validate_synchronous_matrix(perf: dict) -> tuple[list, str | None]:
    """Validate the complete raw candidate/window matrix before filtering."""
    if not isinstance(perf, dict) or not perf:
        return [], "CSCV requires a non-empty performance matrix"
    try:
        rows = list(perf.values())
        if any(not isinstance(row, dict) for row in rows):
            return [], "CSCV performance rows must be window mappings"
        window_sets = [set(row) for row in rows]
        if any(windows != window_sets[0] for windows in window_sets[1:]):
            return [], "CSCV requires a synchronous window matrix"
        windows = sorted(window_sets[0])
        for row in rows:
            if any(not math.isfinite(float(row[window]))
                   for window in windows):
                return [], "CSCV requires finite performance in every cell"
    except (TypeError, ValueError, OverflowError) as exc:
        return [], f"invalid performance matrix: {exc}"
    return windows, None


def _legacy_rank_of(vid: str, scores: dict) -> int:
    """OOS 순위(1 = 최고). 동점은 **불리하게** 센다 - 동점을 1등으로 세면
    구분 못 하는 것을 실력으로 읽는다."""
    better = sum(1 for k, v in scores.items()
                 if v > scores[vid] or (v == scores[vid] and k != vid))
    return better + 1


def _legacy_is_ranks_cscv(perf: dict, *, max_splits: int = MAX_SPLITS) -> dict:
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
        ranks.append(_legacy_rank_of(best, oos_score))

    out = {"is_ranks": ranks, "n_splits": len(splits),
           "windows": len(windows), "variants": len(perf)}
    if truncated:
        # 조용한 절단 금지 - 무엇을 안 봤는지 남긴다
        out["truncated_splits"] = truncated
    return out


def _legacy_compute(perf: dict, *, max_splits: int = MAX_SPLITS) -> dict:
    """창별 수익률 -> PBO. **못 하면 None 이고 이유가 붙는다.**"""
    kept, dropped = dedupe_variants(perf)
    r = _legacy_is_ranks_cscv(kept, max_splits=max_splits)
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


def _ascending_midrank(variant_id: str, scores: dict[str, float]) -> float:
    """Return an OOS midrank with 1=worst and N=best, as CSCV requires."""
    target = scores[variant_id]
    lower = sum(value < target for value in scores.values())
    tied = sum(value == target for value in scores.values())
    return 1.0 + lower + (tied - 1.0) / 2.0


def _unrank_combination(n: int, k: int, rank: int) -> tuple[int, ...]:
    """Map a lexicographic rank to one k-combination without enumeration."""
    total = math.comb(n, k)
    if rank < 0 or rank >= total:
        raise ValueError("combination rank out of range")
    selected = []
    start = 0
    remaining = k
    for _position in range(k):
        for value in range(start, n):
            suffix_count = math.comb(n - value - 1, remaining - 1)
            if rank < suffix_count:
                selected.append(value)
                start = value + 1
                remaining -= 1
                break
            rank -= suffix_count
    return tuple(selected)


def _cscv_splits(n_windows: int, *, max_splits: int,
                 seed: int) -> tuple[list[tuple[int, ...]], dict]:
    """Enumerate CSCV exactly or uniformly sample complementary split pairs."""
    half = n_windows // 2
    total_splits = math.comb(n_windows, half)
    if max_splits < 2:
        raise ValueError("max_splits must be at least two")
    if type(seed) is not int:
        raise ValueError("seed must be an explicit integer")
    if total_splits <= max_splits:
        splits = list(combinations(range(n_windows), half))
        return splits, {
            "sampling_mode": "exact",
            "total_splits": total_splits,
            "sampled_splits": total_splits,
            "sampling_fraction": 1.0,
            "sampling_seed": None,
            "complement_pairs_preserved": True,
        }

    # Every S/2 split has exactly one complement.  The member containing block
    # zero is a one-to-one canonical representation of that pair.  Sampling
    # ranks from this canonical universe is uniform without replacement and
    # avoids constructing C(S,S/2) objects in memory.
    pair_universe = math.comb(n_windows - 1, half - 1)
    pair_count = min(pair_universe, max_splits // 2)
    rng = random.Random(seed)
    sampled_pair_ranks = sorted(rng.sample(range(pair_universe), pair_count))
    all_indices = set(range(n_windows))
    splits = []
    for pair_rank in sampled_pair_ranks:
        tail = _unrank_combination(n_windows - 1, half - 1, pair_rank)
        canonical = (0,) + tuple(index + 1 for index in tail)
        complement = tuple(sorted(all_indices - set(canonical)))
        splits.extend((canonical, complement))
    return splits, {
        "sampling_mode": "uniform_complement_pairs_without_replacement",
        "total_splits": total_splits,
        "sampled_splits": len(splits),
        "sampled_pairs": pair_count,
        "total_pairs": pair_universe,
        "sampling_fraction": len(splits) / total_splits,
        "sampling_seed": seed,
        "complement_pairs_preserved": True,
        "truncated_splits": total_splits - len(splits),
    }


def is_ranks_cscv(perf: dict, *, max_splits: int = MAX_SPLITS,
                  seed: int = 20260817) -> dict:
    """Return CSCV OOS ranks using exact or unbiased split allocation.

    ``perf`` must be a synchronous matrix represented as
    ``{terminal_endpoint: {ordered_window: performance}}``.  Missing windows
    fail closed.  For large split universes, complementary split pairs are
    sampled uniformly without replacement rather than taking a lexicographic
    prefix.
    """
    windows, validation_error = _validate_synchronous_matrix(perf)
    if validation_error:
        return {"is_ranks": [], "reason": validation_error}
    if len(perf) < MIN_VARIANTS:
        return {"is_ranks": [],
                "reason": f"need at least {MIN_VARIANTS} distinct variants"}
    if len(windows) < MIN_WINDOWS:
        return {"is_ranks": [],
                "reason": f"need at least {MIN_WINDOWS} common windows"}
    if len(windows) % 2:
        return {"is_ranks": [],
                "reason": "CSCV requires an even number of equal blocks"}
    try:
        splits, sampling = _cscv_splits(
            len(windows), max_splits=max_splits, seed=seed)
    except (TypeError, ValueError, OverflowError) as exc:
        return {"is_ranks": [], "reason": str(exc)}

    ranks = []
    for split in splits:
        in_sample_indices = set(split)
        in_sample_windows = [windows[index] for index in split]
        out_sample_windows = [window for index, window in enumerate(windows)
                              if index not in in_sample_indices]
        in_sample_scores = {
            variant: math.fsum(float(perf[variant][window])
                               for window in in_sample_windows) /
            len(in_sample_windows)
            for variant in perf
        }
        out_sample_scores = {
            variant: math.fsum(float(perf[variant][window])
                               for window in out_sample_windows) /
            len(out_sample_windows)
            for variant in perf
        }
        # The predeclared identifier is the deterministic IS tie breaker.  OOS
        # information is never consulted to choose among IS ties.
        best = max(sorted(in_sample_scores),
                   key=lambda variant: in_sample_scores[variant])
        ranks.append(_ascending_midrank(best, out_sample_scores))

    return {
        "is_ranks": ranks,
        "n_splits": len(ranks),
        "windows": len(windows),
        "variants": len(perf),
        **sampling,
    }


def _sampling_standard_error(ranks: list[float], n_strategies: int,
                             sampling: dict) -> float | None:
    """Monte-Carlo SE across sampled complement-pair PBO contributions."""
    if sampling.get("sampling_mode") == "exact":
        return 0.0
    pair_count = sampling.get("sampled_pairs", 0)
    pair_universe = sampling.get("total_pairs", 0)
    if pair_count < 2 or len(ranks) != pair_count * 2:
        return None
    outcomes = []
    for index in range(0, len(ranks), 2):
        indicators = []
        for rank in ranks[index:index + 2]:
            omega = rank / (n_strategies + 1.0)
            indicators.append(float(math.log(omega / (1.0 - omega)) <= 0.0))
        outcomes.append(math.fsum(indicators) / 2.0)
    mean = math.fsum(outcomes) / pair_count
    sample_variance = math.fsum((value - mean) ** 2
                                for value in outcomes) / (pair_count - 1)
    finite_population = ((pair_universe - pair_count) /
                         (pair_universe - 1)
                         if pair_universe > 1 else 0.0)
    return math.sqrt(sample_variance / pair_count * finite_population)


def compute(perf: dict, *, max_splits: int = MAX_SPLITS,
            seed: int = 20260817,
            candidate_identities: dict | None = None) -> dict:
    """Compute PBO with transparent sampling and adequacy diagnostics."""
    _windows, validation_error = _validate_synchronous_matrix(perf)
    if validation_error:
        return {
            "probability_of_backtest_overfitting": None,
            "reason": validation_error,
            "duplicate_variants": [],
            "adequacy_status": "insufficient",
        }
    try:
        kept, dropped = dedupe_variants(
            perf, candidate_identities=candidate_identities)
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "probability_of_backtest_overfitting": None,
            "reason": f"invalid performance matrix: {exc}",
            "duplicate_variants": [],
            "adequacy_status": "insufficient",
        }
    result = is_ranks_cscv(kept, max_splits=max_splits, seed=seed)
    if not result["is_ranks"]:
        return {
            "probability_of_backtest_overfitting": None,
            "reason": result["reason"],
            "duplicate_variants": dropped,
            "adequacy_status": "insufficient",
        }

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from overfit_stats import pbo as pbo_stat

    statistic = pbo_stat(result["is_ranks"], len(kept))
    pbo_value = statistic.get("pbo")
    monte_carlo_se = _sampling_standard_error(
        result["is_ranks"], len(kept), result)
    warnings = []
    if result["windows"] < RECOMMENDED_WINDOWS:
        warnings.append(
            f"{result['windows']} CSCV blocks; the paper regards S=16 as a "
            "reasonable general setting")
    if len(kept) < RELIABLE_VARIANTS:
        warnings.append(
            f"{len(kept)} terminal endpoints; PBO<0.1 needs N well above 10 "
            f"(operational target: {RELIABLE_VARIANTS})")
    if result["sampling_mode"] != "exact":
        warnings.append(
            "split universe was uniformly sampled; inspect monte_carlo_se")

    adequate = (result["windows"] >= RECOMMENDED_WINDOWS and
                len(kept) >= RELIABLE_VARIANTS)
    output = {
        "probability_of_backtest_overfitting": pbo_value,
        "n_splits": result["n_splits"],
        "total_splits": result["total_splits"],
        "n_windows": result["windows"],
        "n_variants": len(kept),
        "sampling_mode": result["sampling_mode"],
        "sampling_fraction": result["sampling_fraction"],
        "sampling_seed": result["sampling_seed"],
        "complement_pairs_preserved": result["complement_pairs_preserved"],
        "monte_carlo_se": monte_carlo_se,
        "adequacy_status": ("adequate_diagnostic" if adequate
                            else "diagnostic_only"),
        "sufficiency_warnings": warnings,
        "rank_convention": statistic.get("rank_convention"),
    }
    if warnings:
        output["caveat"] = "; ".join(warnings)
    if dropped:
        output["duplicate_variants"] = dropped
    for key in ("sampled_pairs", "total_pairs", "truncated_splits"):
        if key in result:
            output[key] = result[key]
    if pbo_value is None:
        output["reason"] = statistic.get("reason", "PBO calculation failed")
    return output


def load_family_performance(conn, family_id: str, *,
                            metric: str = "total_return",
                            reference_experiment_id: str | None = None,
                            evaluation_scope: str | None = None) -> dict:
    """DB 에서 Family 의 창별 성적을 꺼낸다.

    창별 원값은 experiment_metrics 에 dimensions={"window": label} 로 이미
    쌓여 있다 - SUMMARY 행은 창이 아니므로 뺀다.
    """
    perf: dict = {}
    # A family name alone is not a statistical comparison cohort. Every
    # caller must anchor PBO to one immutable experiment identity so variants
    # from another dataset, stock universe, window plan, or cost model cannot
    # leak into the calculation.
    if reference_experiment_id is None:
        return {}
    with conn.cursor() as cur:
        if reference_experiment_id is not None:
            cur.execute(
                f"""
                select e.experiment_id::text, metric.dimensions, metric.value,
                       metric.cost_model_version,
                       ({_GOVERNED_PBO_EVIDENCE}) is true
                  from quant.experiments e
                  join quant.hypotheses h
                    on h.hypothesis_id = e.hypothesis_id
                  join quant.dataset_manifests manifest
                    on manifest.dataset_id = e.dataset_id
                  join quant.experiment_metrics metric
                    on metric.experiment_id = e.experiment_id
                 where e.trial_family_id = %s
                   and metric.metric = %s
                   and metric.dimensions->>'window' is not null
                   and metric.dimensions->>'window' <> 'SUMMARY'
                """, (family_id, metric))
            rows = cur.fetchall()

            def dimensions(value) -> dict:
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return {}
                return value if isinstance(value, dict) else {}

            # The SQL predicate is authoritative, while the returned boolean is
            # retained as a second fail-closed boundary for test doubles,
            # replicas, and future query refactors.  Legacy four-column rows are
            # not silently treated as governed evidence.
            parsed = [
                (str(row[0]), dimensions(row[1]), float(row[2]), str(row[3]))
                for row in rows
                if isinstance(row, (list, tuple)) and len(row) == 5
                and row[4] is True
            ]
            reference = [row for row in parsed
                         if row[0] == str(reference_experiment_id)
                         and not row[1].get("screening_candidate")
                         and row[1].get("evaluation_identity_complete") is True
                         and (evaluation_scope is None or
                              row[1].get("evaluation_scope") == evaluation_scope)]
            signatures = {(
                row[1].get("evaluation_scope"),
                row[1].get("evaluation_fingerprint"),
                row[1].get("session_boundary_fingerprint"),
                row[1].get("source_content_fingerprint"),
                row[1].get("instrument_ids_fingerprint"),
                row[1].get("cost_model_version"),
                row[3],
            ) for row in reference}
            if len(signatures) != 1:
                return {}
            signature = next(iter(signatures))
            if (not all(signature)
                    or signature[5] != signature[6]):
                return {}
            reference_windows = {
                row[1].get("window"): (
                    row[1].get("start_session"), row[1].get("end_session"))
                for row in reference}
            if (len(reference_windows) < MIN_WINDOWS
                    or any(not window or not start or not end
                           for window, (start, end) in reference_windows.items())):
                return {}

            invalid: set[str] = set()
            for eid, dim, value, cost in parsed:
                row_signature = (
                    dim.get("evaluation_scope"),
                    dim.get("evaluation_fingerprint"),
                    dim.get("session_boundary_fingerprint"),
                    dim.get("source_content_fingerprint"),
                    dim.get("instrument_ids_fingerprint"),
                    dim.get("cost_model_version"),
                    cost,
                )
                window = dim.get("window")
                if (row_signature != signature
                        or window not in reference_windows
                        or (dim.get("start_session"), dim.get("end_session"))
                        != reference_windows[window]):
                    continue
                candidate = dim.get("screening_candidate")
                variant = (f"{eid}:SCREEN:{candidate}" if candidate else eid)
                if window in perf.setdefault(variant, {}):
                    invalid.add(variant)
                    continue
                perf[variant][window] = value
            expected_windows = set(reference_windows)
            return {
                variant: windows for variant, windows in perf.items()
                if variant not in invalid and set(windows) == expected_windows}


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


def _check_duplicate_variants_need_identity_evidence():
    """Equal outcomes alone never prove candidate identity."""
    same = [0.10, 0.11, 0.09, 0.12]
    p = _perf(a=same, a_dup=list(same), b=[0.01, 0.02, 0.00, 0.01],
              c=[-0.05, -0.04, -0.06, -0.03], d=[-0.09, -0.08, -0.10, -0.07])
    retained = compute(p)
    r = compute(p, candidate_identities={
        "a": "formula-a", "a_dup": "formula-a", "b": "formula-b",
        "c": "formula-c", "d": "formula-d",
    })
    assert retained["n_variants"] == 5, retained
    assert r["n_variants"] == 4, r
    assert r["duplicate_variants"], r
    assert r["duplicate_variants"][0]["duplicate_of"] == "a", r


def _check_insufficient_returns_none_not_zero():
    """**0 을 내지 않는다** - 0 은 "과적합 없음" 인데 "안 재봤다" 와 정반대다."""
    # 변형 2개는 "중앙값 아래" 에 해상도가 없다 - 0.0 을 내면 안 된다
    two = compute(_perf(a=[0.1, 0.2, 0.3, 0.4], b=[0.0, 0.1, 0.2, 0.3]))
    assert two["probability_of_backtest_overfitting"] is None, two
    assert "at least 4" in two["reason"], two

    short = compute({k: {"w1": v, "w2": v + 0.1, "w3": v}
                     for k, v in zip("abcd", (0.1, 0.0, -0.1, -0.2))})
    assert short["probability_of_backtest_overfitting"] is None, short
    assert "at least 4" in short["reason"], short


def _check_only_common_windows_are_compared():
    """창이 다르면 교집합으로 숨기지 않고 동기화 실패로 막는다."""
    p = {"a": dict(zip(_W, [0.1, 0.2, 0.3, 0.4])),
         "b": dict(zip(_W, [0.0, 0.1, 0.2, 0.3]), extra=9.9),
         "c": dict(zip(_W, [-0.1, 0.0, 0.1, 0.2])),
         "d": dict(zip(_W, [-0.2, -0.1, 0.0, 0.1]))}
    r = compute(p)
    assert r["probability_of_backtest_overfitting"] is None, r
    assert "synchronous" in r["reason"], r


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
    assert r.get("sufficiency_warnings"), r


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
    _check_duplicate_variants_need_identity_evidence(); print("  식별자 기반 중복 접기    OK")
    _check_insufficient_returns_none_not_zero(); print("  부족 -> None(0 아님)   OK")
    _check_only_common_windows_are_compared(); print("  공통 창만 비교          OK")
    _check_deterministic();                    print("  결정론                  OK")
    _check_small_sample_carries_caveat();      print("  작은 표본 경고          OK")
    _check_split_cap_is_reported();            print("  절단 보고               OK")
    print("PBO(CSCV) 8개 영역 통과.")
