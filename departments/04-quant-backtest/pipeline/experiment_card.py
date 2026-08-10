"""ExperimentCard 생산자 - 실험의 최종 산출물을 실제로 만든다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: contracts/quant_v2.ExperimentCardV1

▶ 계약은 있는데 만드는 코드가 없었다
  ExperimentCardV1 은 실험 결과를 QA 로 넘기는 유일한 산출물 계약인데
  **생산자가 0개였다.** DSR·PBO·시도압력·릴리스판정을 다 계산해놓고
  experiment_metrics 행과 메모리 안 report 로만 흩어져 있었다 - 계약이
  묶어주기로 한 것을 아무도 안 묶었다.

▶ 카드를 못 만드는 것도 결과다
  계약이 trial_family_id 를 min_length=1 로 요구한다. Family 를 못 정한
  실험은 **카드가 안 나온다** - 이건 버그가 아니라 fail-closed 다.
  어느 컨셉의 몇 번째 시도인지 모르는 결과를 QA 에 넘기지 않는다.
  못 만든 이유를 blockers 로 돌려준다(조용히 None 을 내지 않는다).

▶ 안 돌린 것을 통과로 세지 않는다
  Validation 은 PASS/FAIL/NOT_RUN 셋이다. purged_walk_forward 는 창별
  검증이 실제로 돈 실험만 PASS 다. CPCV 는 우리가 안 돌리므로 NOT_RUN 이며
  **영원히 NOT_RUN 으로 두지 않고 그 사실이 카드에 남는다.**

▶ decision 은 릴리스 관문이 정한다
  카드가 스스로 SUBMIT_TO_QA 를 못 붙인다. 관문 판정을 그대로 옮긴다 -
  계약의 _decision_is_earned 가 근거 없는 제출을 거부한다.

자체 점검: python departments/04-quant-backtest/pipeline/experiment_card.py
실행:      python departments/04-quant-backtest/pipeline/experiment_card.py --build [--experiment <id>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MODULE_VERSION = "quant-experiment-card-v1"

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "contracts"))
sys.path.insert(0, str(_HERE.parent.parent / "01-research" / "collectors"))

# 창별 검증이 이 정도는 돌아야 purged_walk_forward 를 PASS 로 본다.
# walk_forward.MIN_JUDGE_TEST_DAYS 로 걸러진 뒤의 창 수 기준이다.
MIN_WF_WINDOWS = 3


def validation_block(metrics: dict, *, n_windows: int) -> dict:
    """검증표. **안 돌린 것은 NOT_RUN 이다**(PASS 아님).

    metrics 는 experiment_metrics 에서 읽은 {metric: value}.
    """
    wf = "PASS" if n_windows >= MIN_WF_WINDOWS else "NOT_RUN"
    return {
        "purged_walk_forward": wf,
        # ▶ CPCV 는 우리가 안 돌린다. NOT_RUN 으로 남겨 **안 돌렸다는 사실이
        #   카드에 보이게** 한다 - PASS 로 채우면 검증표가 장식이 된다.
        "cpcv": "NOT_RUN",
        "deflated_sharpe": metrics.get("deflated_sharpe"),
        "probability_of_backtest_overfitting": metrics.get("pbo"),
        "bootstrap_ci_low": metrics.get("bootstrap_ci_low"),
        "bootstrap_ci_high": metrics.get("bootstrap_ci_high"),
    }


def blockers(row: dict, val: dict) -> list:
    """카드를 못 만드는 이유. **빈 리스트여야 카드가 나온다.**"""
    out = []
    if not (row.get("trial_family_id") or "").strip():
        out.append(
            "trial_family_id 없음 - 어느 컨셉의 몇 번째 시도인지 모르는 결과를 "
            "QA 에 넘기지 않는다(유니버스를 통제 어휘로 못 사상한 실험이다)")
    if not row.get("trial_number"):
        out.append("trial_number 없음 - 시도 순번 없이는 다중검정을 못 읽는다")
    if not (row.get("hypothesis_fingerprint") or "").strip():
        out.append(
            "hypothesis_fingerprint 없음 - 사전등록 지문이 없으면 결과를 보고 "
            "설정을 바꿨는지 확인할 수 없다")
    if not (row.get("dataset_hash") or "").strip():
        out.append("dataset_hash 없음 - 어느 데이터로 돌렸는지 특정 불가")
    lo, hi = val.get("bootstrap_ci_low"), val.get("bootstrap_ci_high")
    if (lo is None) != (hi is None):
        # 계약이 거부하기 전에 여기서 말한다 - 어디가 문제인지 알려야 고친다
        out.append("bootstrap CI 한쪽만 있다 - 구간이 아니다")
    return out


def build_payload(row: dict, metrics: dict, *, n_windows: int,
                  decision: str, failures=(), regimes=None,
                  capacity=None) -> dict:
    """카드 dict. 계약 검증 전 단계 - **순수 함수**(DB 없음)."""
    import reproducibility as R

    val = validation_block(metrics, n_windows=n_windows)
    repro = R.snapshot(
        dataset_name=row.get("dataset_name") or "",
        dataset_version=row.get("dataset_version") or "",
        manifest_id=row.get("dataset_manifest_id") or "",
        dataset_hash=row.get("dataset_hash") or "",
        seed=int(row.get("seed") or 0),
        source_versions=row.get("source_versions"))
    gaps = repro.pop("reproducibility_gaps", None)
    if gaps:
        # 의존성을 못 읽었으면 실패 목록에 남긴다 - 카드가 "다시 만들 수 있다"
        # 고 말하면 안 된다
        failures = tuple(failures) + tuple(
            f"의존성 버전 미상: {g}" for g in gaps)
    return {
        "experiment_id": str(row["experiment_id"]),
        "hypothesis_id": str(row["hypothesis_id"]),
        "hypothesis_version": int(row.get("hypothesis_version") or 1),
        "hypothesis_fingerprint": row.get("hypothesis_fingerprint") or "",
        "trial_family_id": row.get("trial_family_id") or "",
        "trial_number": int(row.get("trial_number") or 0),
        "cost_model_version": row.get("cost_model_version") or "",
        "validation": val,
        "oos_metrics": {k: float(v) for k, v in (metrics or {}).items()
                        if isinstance(v, (int, float))
                        and not isinstance(v, bool)},
        "regime_breakdown": dict(regimes or {}),
        "capacity": dict(capacity or {}),
        "failures": tuple(failures),
        "decision": decision,
        **repro,
    }


def build(row: dict, metrics: dict, *, n_windows: int, decision: str,
          failures=(), regimes=None, capacity=None) -> dict:
    """카드를 만든다. **못 만들면 이유를 돌려준다**(조용한 None 금지)."""
    val = validation_block(metrics, n_windows=n_windows)
    bl = blockers(row, val)
    if bl:
        return {"card": None, "blockers": bl}

    payload = build_payload(row, metrics, n_windows=n_windows,
                            decision=decision, failures=failures,
                            regimes=regimes, capacity=capacity)
    try:
        from quant_v2 import ExperimentCardV1

        card = ExperimentCardV1.model_validate(payload)
        return {"card": card.model_dump(mode="json"), "blockers": []}
    except Exception as e:  # noqa: BLE001
        # 계약 위반을 삼키지 않는다 - 카드가 안 나온 이유가 보여야 고친다
        return {"card": None,
                "blockers": [f"계약 검증 실패: {type(e).__name__}: "
                             f"{str(e)[:300]}"],
                "payload": payload}


def load_regimes(conn, experiment_id: str) -> dict:
    """국면별 지표. dimensions={"regime": …} 로 적재돼 있다."""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """select dimensions->>'regime', metric, value
                 from quant.experiment_metrics
                where experiment_id = %s
                  and dimensions->>'regime' is not null""",
            (experiment_id,))
        for r, m, v in cur.fetchall():
            out.setdefault(r, {})[m] = float(v)
    return out


def load_row(conn, experiment_id: str) -> tuple[dict, dict, int]:
    """DB -> (실험 행, 지표, 창 수)."""
    row: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.experiment_id, e.hypothesis_id, e.seed, e.cost_model_version,
                   e.trial_family_id, e.trial_number,
                   d.dataset_id, d.name, d.version, d.content_hash,
                   d.source_versions, h.material_fingerprint
              from quant.experiments e
              join quant.dataset_manifests d on d.dataset_id = e.dataset_id
              join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
             where e.experiment_id = %s
            """, (experiment_id,))
        r = cur.fetchone()
        if not r:
            return {}, {}, 0
        row = {
            "experiment_id": str(r[0]), "hypothesis_id": str(r[1]),
            "seed": r[2], "cost_model_version": r[3],
            "trial_family_id": r[4], "trial_number": r[5],
            "dataset_manifest_id": str(r[6]), "dataset_name": r[7],
            "dataset_version": r[8], "dataset_hash": r[9],
            "source_versions": r[10], "hypothesis_fingerprint": r[11],
        }
        # ▶ **국면 행을 제외한다.** 예전 필터는 window 키만 봐서
        #   dimensions={"regime": "BULL"} 행이 통과했고, BULL 과 BEAR 가
        #   같은 metric 이름(total_return 등)으로 서로 덮어써 oos_metrics 에
        #   **어느 국면 값인지 모르는 숫자**가 남았다(실측으로 걸렸다).
        #   국면별 값은 regime_breakdown 이 따로 싣는다.
        cur.execute(
            """select metric, value from quant.experiment_metrics
               where experiment_id = %s
                 and coalesce(dimensions->>'window', '') in ('', 'SUMMARY')
                 and dimensions->>'regime' is null""",
            (experiment_id,))
        metrics = {m: float(v) for m, v in cur.fetchall()}
        cur.execute(
            """select count(distinct dimensions->>'window')
                 from quant.experiment_metrics
                where experiment_id = %s
                  and dimensions->>'window' is not null
                  and dimensions->>'window' <> 'SUMMARY'""",
            (experiment_id,))
        n_windows = int((cur.fetchone() or [0])[0])
    return row, metrics, n_windows


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_ROW = {
    "experiment_id": "e-1", "hypothesis_id": "h-1", "hypothesis_version": 1,
    "hypothesis_fingerprint": "sha256:" + "a" * 20,
    "dataset_manifest_id": "ds-1", "dataset_name": "krx", "dataset_version": "v2",
    "dataset_hash": "sha256:" + "b" * 20, "seed": 20260804,
    "cost_model_version": "v1", "trial_family_id": "fam_x", "trial_number": 3,
}
_M = {"deflated_sharpe": 0.99, "pbo": 0.2,
      "bootstrap_ci_low": 0.3, "bootstrap_ci_high": 1.9}


def _check_card_is_actually_produced():
    """**계약이 있는데 생산자가 0개**이던 상태를 끝낸다."""
    r = build(_ROW, _M, n_windows=5, decision="REVISE")
    assert r["card"] is not None, r
    c = r["card"]
    assert c["trial_family_id"] == "fam_x" and c["trial_number"] == 3, c
    assert c["code_hash"].startswith("sha256:"), c
    assert c["dependency_lock_hash"].startswith("sha256:"), c
    assert c["lineage"]["dataset"] == "krx/v2", c


def _check_submit_needs_regime_breakdown():
    """**제출은 구간 분해가 있어야 열린다**(계약 7.3절 P0).

    검증 통계(DSR·PBO·CI)를 다 채워도 regime_breakdown 이 비면 SUBMIT_TO_QA
    가 거부된다 - 특정 구간에서만 되는 전략을 걸러내지 않고 넘기지 않는다.
    카드 생산(A)과 제출 자격(E)은 다른 문제다.
    """
    r = build(_ROW, _M, n_windows=5, decision="SUBMIT_TO_QA")
    assert r["card"] is None, r
    assert any("regime_breakdown" in b for b in r["blockers"]), r
    # 같은 재료로 REVISE 는 나온다 - 막힌 것은 제출이지 카드가 아니다
    assert build(_ROW, _M, n_windows=5, decision="REVISE")["card"] is not None


def _check_regime_breakdown_opens_submit():
    """**구간 분해가 채워지면 제출이 열린다** - 계약이 요구하던 그 필드다."""
    reg = {"BULL": {"n_windows": 3.0, "total_return": 0.42,
                    "excess_return": 0.18, "worst_return": 0.05,
                    "win_ratio": 1.0, "benchmark_return": 0.24,
                    "is_single_sample": 0.0},
           "BEAR": {"n_windows": 2.0, "total_return": -0.03,
                    "excess_return": 0.12, "worst_return": -0.08,
                    "win_ratio": 0.5, "benchmark_return": -0.15,
                    "is_single_sample": 0.0}}
    r = build(_ROW, _M, n_windows=5, decision="SUBMIT_TO_QA", regimes=reg)
    assert r["card"] is not None, r
    assert set(r["card"]["regime_breakdown"]) == {"BULL", "BEAR"}, r["card"]


def _check_no_family_means_no_card():
    """**Family 없는 결과는 카드가 안 나온다** - 버그가 아니라 fail-closed.

    어느 컨셉의 몇 번째 시도인지 모르는 결과를 QA 에 넘기지 않는다.
    """
    r = build(dict(_ROW, trial_family_id=""), _M, n_windows=5,
              decision="SUBMIT_TO_QA")
    assert r["card"] is None, r
    assert any("trial_family_id" in b for b in r["blockers"]), r
    # 조용히 None 을 내지 않는다 - 이유가 붙는다
    assert any("QA 에 넘기지 않는다" in b for b in r["blockers"]), r


def _check_not_run_is_not_pass():
    """**안 돌린 것을 통과로 세면 검증표가 장식이 된다.**"""
    v = validation_block(_M, n_windows=1)
    assert v["purged_walk_forward"] == "NOT_RUN", v
    # 우리가 안 돌리는 CPCV 는 항상 NOT_RUN 이고 그 사실이 카드에 남는다
    assert validation_block(_M, n_windows=9)["cpcv"] == "NOT_RUN"
    assert validation_block(_M, n_windows=9)["purged_walk_forward"] == "PASS"


def _check_unearned_submit_is_rejected():
    """창별 검증을 안 돌리고 제출할 수 없다 - 계약이 막는다."""
    r = build(_ROW, _M, n_windows=1, decision="SUBMIT_TO_QA")
    assert r["card"] is None, r
    assert any("계약 검증 실패" in b for b in r["blockers"]), r
    # 창이 부족하면 REVISE 카드도 NOT_RUN 을 달고 나온다(통과로 위장 안 함)
    ok = build(_ROW, _M, n_windows=1, decision="REVISE")
    assert ok["card"]["validation"]["purged_walk_forward"] == "NOT_RUN", ok


def _check_missing_fingerprint_blocks():
    """사전등록 지문이 없으면 설정을 바꿨는지 확인할 수 없다."""
    r = build(dict(_ROW, hypothesis_fingerprint=""), _M, n_windows=5,
              decision="REVISE")
    assert r["card"] is None and any("fingerprint" in b for b in r["blockers"])


def _check_half_ci_is_caught_here():
    """계약이 거부하기 전에 어디가 문제인지 말한다."""
    r = build(_ROW, {"bootstrap_ci_low": 0.3}, n_windows=5, decision="REVISE")
    assert r["card"] is None, r
    assert any("한쪽만" in b for b in r["blockers"]), r


def _check_dependency_gap_becomes_failure():
    """**의존성을 못 읽었으면 카드가 '다시 만들 수 있다' 고 말하면 안 된다.**"""
    import reproducibility as R

    orig = R.RUNTIME_PACKAGES
    try:
        R.RUNTIME_PACKAGES = ("definitely-not-installed-xyz",)
        p = build_payload(_ROW, _M, n_windows=5, decision="REVISE")
        assert any("의존성 버전 미상" in f for f in p["failures"]), p["failures"]
        # 결손 표시는 failures 로 가고 카드 필드로는 새지 않는다
        assert "reproducibility_gaps" not in p, p
    finally:
        R.RUNTIME_PACKAGES = orig


def _check_regime_rows_do_not_leak_into_oos():
    """**국면 값이 oos_metrics 로 새면 안 된다** (2026-08-04 실측).

    BULL 과 BEAR 가 같은 metric 이름(total_return 등)을 쓰므로, 필터가
    국면 행을 걸러내지 않으면 서로 덮어써 **어느 국면 값인지 모르는 숫자**가
    총계 자리에 남는다. 실제로 BULL 의 +49% 가 oos_metrics.total_return 으로
    올라왔었다.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split("# ── 자체 점검")[0]
    assert "dimensions->>'regime' is null" in body,         "load_row 의 지표 조회가 국면 행을 제외해야 한다"
    # 국면 값은 regime_breakdown 으로만 간다
    reg = {"BULL": {"total_return": 0.5, "n_windows": 3.0}}
    p2 = build_payload(_ROW, {"sharpe": 0.6}, n_windows=5, decision="REVISE",
                       regimes=reg)
    assert "total_return" not in p2["oos_metrics"], p2["oos_metrics"]
    assert p2["regime_breakdown"]["BULL"]["total_return"] == 0.5, p2


def _check_bool_metrics_do_not_leak():
    """oos_metrics 는 수치 전용 - bool 은 int 서브클래스라 조용히 섞인다."""
    p = build_payload(_ROW, dict(_M, is_final=True), n_windows=5,
                      decision="REVISE")
    assert "is_final" not in p["oos_metrics"], p["oos_metrics"]


def _main_build(argv) -> int:
    import psycopg2
    from source_registry import load_project_env

    eid = None
    if "--experiment" in argv:
        eid = argv[argv.index("--experiment") + 1]
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                            connect_timeout=25)
    try:
        if eid is None:
            with conn.cursor() as cur:
                cur.execute(
                    """select experiment_id from quant.experiments
                       where trial_family_id is not null
                       order by created_at desc limit 1""")
                r = cur.fetchone()
                if not r:
                    print("Family 배정된 실험이 없다"); return 1
                eid = str(r[0])
        row, metrics, nw = load_row(conn, eid)
        if not row:
            print(f"실험 {eid} 없음"); return 1
        regimes = load_regimes(conn, eid)
        res = build(row, metrics, n_windows=nw, decision="REVISE",
                    regimes=regimes)
        if res["card"]:
            print(json.dumps(res["card"], ensure_ascii=False, indent=2)[:2200])
        else:
            print(f"카드 생성 불가 ({eid[:8]}…):")
            for b in res["blockers"]:
                print(f"  - {b}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--build" in sys.argv:
        raise SystemExit(_main_build(sys.argv))

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_card_is_actually_produced();     print("  카드 실제 생성          OK")
    _check_submit_needs_regime_breakdown(); print("  제출엔 구간분해 필요     OK")
    _check_regime_breakdown_opens_submit(); print("  구간분해 -> 제출 열림    OK")
    _check_no_family_means_no_card();       print("  Family 없음 -> 카드 없음 OK")
    _check_not_run_is_not_pass();           print("  NOT_RUN != PASS         OK")
    _check_unearned_submit_is_rejected();   print("  근거 없는 제출 거부      OK")
    _check_missing_fingerprint_blocks();    print("  지문 없음 차단          OK")
    _check_half_ci_is_caught_here();        print("  반쪽 CI 조기 차단       OK")
    _check_dependency_gap_becomes_failure(); print("  의존성 결손 -> 실패     OK")
    _check_regime_rows_do_not_leak_into_oos()
    print("  국면->총계 누출 차단     OK")
    _check_bool_metrics_do_not_leak();      print("  bool 누출 차단          OK")
    print("ExperimentCard 11개 영역 통과. 실행은 --build [--experiment <id>]")
