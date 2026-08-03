#!/usr/bin/env python3
"""QNT-00 실험 오케스트레이터 - 가설을 실험 체인에 태우는 결정론 상태 머신.

소유: 재일 (퀀트/백테스트본부, QNT-00 supervisor 의 결정론 부분)
근거: quant.hypotheses (strategy_hypothesis_agent 가 PROPOSED 등록),
      pipeline/{pit_dataset,backtest_runner,walk_forward}.py (실험 체인),
      QNT-00 페르소나 계약("실패를 포함한 모든 실험을 Registry 에 기록,
      Production 승격은 직접 하지 않는다")

▶ 설계 - 오케스트레이션은 판단이 아니라 게이트다
  1. 실험 가능성 게이트(결정론):
     - required_data_products 가 quant.dataset_manifests 에 실재하는가
     - expected_edge.type 이 **구현된 전략 카탈로그**에 있는가
     둘 중 하나라도 없으면 NOT_RUNNABLE - 가설은 PROPOSED 로 남고(가설이
     틀린 게 아니라 실험 수단이 없는 것), 부족분이 백로그로 보고된다.
     없는 전략을 비슷한 구현으로 대충 돌리는 것이 이 게이트가 막는 거짓이다.
  2. 실행 가능하면: PROPOSED→TESTING 전이 후 백테스트+강건성 검증을 돌리고,
     강건성 판정(FRAGILE 여부)으로 TESTING→REJECTED/SUPPORTED 를 전이한다.
     전이는 실험 증거(experiment_id) 없이는 일어나지 않는다.
  3. 승격은 없다 - SUPPORTED 조차 Candidate 제출 자격일 뿐, Production
     결정은 CEO·Risk·QA 게이트 몫(권한 분리).

사용
  python pipeline/experiment_orchestrator.py            # 자체 점검 (DB 없음)
  python pipeline/experiment_orchestrator.py --run      # 최신 PROPOSED 가설 처리
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ORCH_VERSION = "quant-experiment-orchestrator-v1"

# 구현된 전략 카탈로그 - 여기 없는 edge type 은 실험 불가가 사실이다.
# 새 전략을 구현하면 한 줄 추가한다 (구현 없이 추가하는 것이 금지 사항).
STRATEGY_CATALOG: dict[str, dict] = {
    "momentum": {
        "strategy_code": "MOM-20-SMOKE",
        "impl": "pipeline/backtest_runner.py + walk_forward.py",
        "note": "20일 모멘텀 상위 N 균등, 월초 리밸런스",
    },
    "mean_reversion": {
        "strategy_code": "REV-5-SMOKE",
        "impl": "pipeline/backtest_runner.py (STRATEGIES) + walk_forward 조각",
        "note": "5일 낙폭 하위 N 균등, 5거래일 리밸런스 (2026-08-01 구현 - "
                "QNT-01 첫 가설의 백로그를 이행)",
    },
}
DATASET_NAME, DATASET_VERSION = "krx-basket-daily", "v1"


@dataclass
class OrchestratorReport:
    hypothesis_id: str
    title: str
    verdict: str                    # RUNNABLE / NOT_RUNNABLE / NO_HYPOTHESIS
    missing: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    experiment_refs: dict = field(default_factory=dict)
    backlog: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 게이트 (순수 함수 - 자체점검 대상)
# ---------------------------------------------------------------------------

def feasibility(hypothesis: dict, existing_datasets: set,
                catalog: dict | None = None) -> tuple[bool, list, list]:
    """(실행 가능?, 부족 목록, 백로그 제안). 판단이 아니라 존재 확인이다."""
    catalog = STRATEGY_CATALOG if catalog is None else catalog
    missing: list = []
    backlog: list = []

    needed = hypothesis.get("required_data_products") or []
    for d in needed:
        if d not in existing_datasets:
            missing.append(f"dataset:{d}")
            backlog.append(f"Dataset '{d}' 구축 (pit_dataset.py --build)")
    if not needed:
        missing.append("dataset:(미지정)")
        backlog.append("가설에 required_data_products 가 없다 - QNT-01 스펙 보강")

    edge = ((hypothesis.get("expected_edge") or {}).get("type") or "").strip().lower()
    if not edge:
        missing.append("edge_type:(미지정)")
    elif edge not in catalog:
        missing.append(f"strategy_impl:{edge}")
        backlog.append(f"'{edge}' 전략 구현 (STRATEGY_CATALOG 등재 조건)")
    return (not missing), missing, backlog


def robustness_to_status(fragility_verdict: str) -> str:
    """강건성 판정 -> 가설 상태. SUPPORTED 도 승격이 아니라 후보 자격일 뿐."""
    v = (fragility_verdict or "").strip().upper()
    if v == "FRAGILE":
        return "REJECTED"
    if v == "ROBUST":
        return "SUPPORTED"
    raise ValueError(f"알 수 없는 강건성 판정: {fragility_verdict!r} - "
                     f"모르는 값을 상태 전이로 옮기지 않는다")


# ---------------------------------------------------------------------------
# 오케스트레이션 본체
# ---------------------------------------------------------------------------

def orchestrate(hypothesis_id: str | None = None, *, conn=None,
                run_chain=None) -> OrchestratorReport:
    """가설 하나를 게이트에 태운다. conn/run_chain 주입은 자체점검용."""
    own_conn = conn is None
    if own_conn:
        import psycopg2

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "01-research" / "collectors"))
        from source_registry import load_project_env

        conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                connect_timeout=20)
    try:
        cur = conn.cursor()
        if hypothesis_id:
            cur.execute("""
                select hypothesis_id, title, expected_edge, required_data_products,
                       status from quant.hypotheses where hypothesis_id = %s
            """, (hypothesis_id,))
        else:
            cur.execute("""
                select hypothesis_id, title, expected_edge, required_data_products,
                       status from quant.hypotheses
                where status = 'PROPOSED' order by created_at desc limit 1
            """)
        row = cur.fetchone()
        if row is None:
            return OrchestratorReport(hypothesis_id="-", title="-",
                                      verdict="NO_HYPOTHESIS")
        hid, title, edge, data_products, status = row
        hyp = {"expected_edge": edge if isinstance(edge, dict) else json.loads(edge or "{}"),
               "required_data_products": (data_products if isinstance(data_products, list)
                                          else json.loads(data_products or "[]"))}

        cur.execute("select distinct name || '/' || version from quant.dataset_manifests")
        datasets = {r[0] for r in cur.fetchall()}

        ok, missing, backlog = feasibility(hyp, datasets)
        report = OrchestratorReport(hypothesis_id=str(hid), title=title,
                                    verdict="RUNNABLE" if ok else "NOT_RUNNABLE",
                                    missing=missing, backlog=backlog)
        if not ok:
            return report          # PROPOSED 유지 - 수단 부족은 가설의 죄가 아니다

        # 실행 가능 - TESTING 전이 후 체인 실행 (전이는 증거와 함께만 전진)
        cur.execute("update quant.hypotheses set status='TESTING' "
                    "where hypothesis_id=%s and status='PROPOSED'", (hid,))
        conn.commit()
        report.transitions.append("PROPOSED->TESTING")

        chain = run_chain or _default_chain
        result = chain(hyp, str(hid))   # {"experiment_id", "fragility": FRAGILE|ROBUST}
        report.experiment_refs = {k: result[k] for k in ("experiment_id", "fragility")
                                  if k in result}
        new_status = robustness_to_status(result["fragility"])
        cur.execute("update quant.hypotheses set status=%s "
                    "where hypothesis_id=%s and status='TESTING'", (new_status, hid))
        conn.commit()
        report.transitions.append(f"TESTING->{new_status}")
        return report
    finally:
        if own_conn:
            conn.close()


def _default_chain(hyp: dict, hypothesis_id: str | None = None) -> dict:
    """실전 체인: 백테스트(가설 바인딩) + walk-forward 강건성 -> 판정.

    edge type -> 전략 config 매핑은 카탈로그가 정하고, 강건성 지표는
    walk_forward 의 조각(make_windows/run_window/fragility_summary)을
    같은 config 로 재사용한다 - 검증 규칙을 두 벌 만들지 않는다.
    """
    import psycopg2
    from backtest_runner import (
        DEFAULT_CONFIG,
        REV_CONFIG,
        Market,
        load_dataset,
        register_and_run,
    )
    from source_registry import load_project_env
    from walk_forward import (
        WARMUP_TRADING_DAYS,
        fragility_summary,
        make_windows,
        run_window,
        slice_market,
    )

    edge = ((hyp.get("expected_edge") or {}).get("type") or "").lower()
    config = {"momentum": DEFAULT_CONFIG, "mean_reversion": REV_CONFIG}[edge]

    bt = register_and_run(DATASET_NAME, DATASET_VERSION,
                          config=config, hypothesis_id=hypothesis_id)
    if bt.get("duplicate"):
        # 같은 (가설, 데이터, 코드) 실험이 이미 있다 - 다시 돌리지 않고 기존
        # 실험의 강건성 판정을 찾아 쓴다. 여기 없으면 판정 불가로 끊는다.
        raise RuntimeError(f"중복 실험({bt.get('experiment_id')}) - 기존 판정을 "
                           f"수동 확인할 것 (자동 재판정은 결과 조작 여지가 있다)")

    # 강건성: 같은 config 로 창별 재실행 (walk_forward 조각 재사용)
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        _, _, _, rows = load_dataset(conn, DATASET_NAME, DATASET_VERSION)
        market = Market.from_rows(rows)
        windows = make_windows(market.dates, WARMUP_TRADING_DAYS)
        wm = [(w.label, run_window(slice_market(market, w), w, dict(config)))
              for w in windows]
        summary, flags, verdict = fragility_summary(wm)
        with conn.cursor() as cur:
            for label, metrics in wm:
                for k in ("total_return", "sharpe_rf0", "max_drawdown"):
                    if isinstance(metrics.get(k), (int, float)):
                        cur.execute("""
                            insert into quant.experiment_metrics
                              (experiment_id, split, metric, value,
                               dimensions, cost_model_version)
                            values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                            on conflict do nothing
                        """, (bt["experiment_id"], k, metrics[k],
                              json.dumps({"window": label, "chain": ORCH_VERSION}),
                              "krx-cost-v1"))
        conn.commit()
    finally:
        conn.close()

    return {"experiment_id": bt["experiment_id"], "fragility": verdict,
            "fragility_flags": flags, "windows": len(wm),
            "backtest_metrics": bt.get("metrics")}


def _print_report(r: OrchestratorReport) -> None:
    print(f"{ORCH_VERSION}: {r.verdict}")
    print(f"  가설: {r.title[:60]} ({r.hypothesis_id[:8]}…)")
    if r.missing:
        print(f"  부족: {', '.join(r.missing)}")
    for b in r.backlog:
        print(f"  백로그: {b}")
    for t in r.transitions:
        print(f"  전이: {t}")
    if r.experiment_refs:
        print(f"  실험: {r.experiment_refs}")


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음 (가짜 커서·체인 주입)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, hypothesis_row, datasets):
        self._row = hypothesis_row
        self._datasets = datasets
        self.updates: list = []

    def execute(self, sql, params=()):
        self._last = (sql, params)
        if "update quant.hypotheses" in sql:
            self.updates.append(params)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [(d,) for d in self._datasets]


class _FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


def _check_feasibility_gate():
    ds = {"krx-basket-daily/v1"}
    ok, missing, _ = feasibility(
        {"expected_edge": {"type": "momentum"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert ok and not missing
    # mean_reversion 은 2026-08-01 REV-5 구현으로 RUNNABLE 이 됐다 (백로그 이행)
    ok_rev, _, _ = feasibility(
        {"expected_edge": {"type": "mean_reversion"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert ok_rev, "REV-5 구현 후에도 mean_reversion 이 막혀 있다"
    # 미구현 전략 -> NOT_RUNNABLE (카탈로그에 없는 가상 전략으로 검증)
    ok2, missing2, backlog2 = feasibility(
        {"expected_edge": {"type": "pairs_trading"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert not ok2 and "strategy_impl:pairs_trading" in missing2
    assert any("pairs_trading" in b for b in backlog2)
    # 없는 데이터셋 -> NOT_RUNNABLE
    ok3, missing3, _ = feasibility(
        {"expected_edge": {"type": "momentum"},
         "required_data_products": ["us-daily/v1"]}, ds)
    assert not ok3 and "dataset:us-daily/v1" in missing3
    # 스펙 자체가 비면 둘 다 잡힌다
    ok4, missing4, _ = feasibility({}, ds)
    assert not ok4 and "dataset:(미지정)" in missing4 and "edge_type:(미지정)" in missing4
    print("  실험 가능성 게이트       OK")


def _check_status_mapping():
    assert robustness_to_status("FRAGILE") == "REJECTED"
    assert robustness_to_status("ROBUST") == "SUPPORTED"
    for bad in ("", "MAYBE", None):
        try:
            robustness_to_status(bad)
            raise AssertionError(f"{bad!r} 가 상태로 옮겨졌다")
        except ValueError:
            pass
    print("  강건성->상태 매핑        OK")


def _check_orchestrate_paths():
    row = ("h-1", "미구현 엣지 가설", {"type": "pairs_trading", "horizon_days": 5},
           ["krx-basket-daily/v1"], "PROPOSED")
    cur = _FakeCursor(row, ["krx-basket-daily/v1"])
    r = orchestrate("h-1", conn=_FakeConn(cur))
    assert r.verdict == "NOT_RUNNABLE" and not cur.updates, \
        "NOT_RUNNABLE 인데 상태를 건드렸다"

    row2 = ("h-2", "모멘텀 가설", {"type": "momentum"},
            ["krx-basket-daily/v1"], "PROPOSED")
    cur2 = _FakeCursor(row2, ["krx-basket-daily/v1"])
    r2 = orchestrate("h-2", conn=_FakeConn(cur2),
                     run_chain=lambda h, hid: {"experiment_id": "e-1",
                                               "fragility": "FRAGILE"})
    assert r2.verdict == "RUNNABLE"
    assert r2.transitions == ["PROPOSED->TESTING", "TESTING->REJECTED"]
    assert len(cur2.updates) == 2 and cur2.updates[1][0] == "REJECTED"

    cur3 = _FakeCursor(row2, ["krx-basket-daily/v1"])
    r3 = orchestrate("h-2", conn=_FakeConn(cur3),
                     run_chain=lambda h, hid: {"experiment_id": "e-2",
                                               "fragility": "ROBUST"})
    assert r3.transitions[-1] == "TESTING->SUPPORTED"

    cur4 = _FakeCursor(None, [])
    assert orchestrate("none", conn=_FakeConn(cur4)).verdict == "NO_HYPOTHESIS"
    print("  오케스트레이션 경로       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        hid = a[a.index("--hypothesis") + 1] if "--hypothesis" in a else None
        _print_report(orchestrate(hid))
        raise SystemExit(0)

    print(f"{ORCH_VERSION} 자체 점검 (DB 없음)")
    _check_feasibility_gate()
    _check_status_mapping()
    _check_orchestrate_paths()
    print("오케스트레이터 3개 영역 통과. 실행은 --run [--hypothesis <id>]")
