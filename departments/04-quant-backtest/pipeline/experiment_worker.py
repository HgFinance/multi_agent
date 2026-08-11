"""실험 워커 - 큐를 물고 오케스트레이터를 돌린다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-04 "실험도구를 무조건적으로 보장해야 함"

▶ 워커가 지켜야 할 것
  ① **집어간 것은 반드시 끝낸다.** 성공이든 실패든 status 를 종결로 옮긴다.
     예외가 나도 finally 에서 FAILED 를 쓴다 - 안 그러면 lease 만료까지
     30분을 기다려야 하고, 그 사이 그 가설은 아무도 못 건드린다.
  ② **실패 사유를 남긴다.** 사유 없는 실패는 다시 시도할지 폐기할지
     판단할 수 없다.
  ③ **죽은 lease 를 회수한다.** 매 순회 시작에 reclaim 을 돈다 - 워커가
     죽어도 주문이 영원히 묶이지 않는다.

▶ 워커를 여럿 띄워도 안전하다
  lease 가 `for update skip locked` 를 쓰므로 같은 작업을 둘이 못 집는다.
  집는 순간 attempts 가 오르므로 무한 재시도도 막힌다.

실행: python departments/04-quant-backtest/pipeline/experiment_worker.py --serve
자체 점검: python departments/04-quant-backtest/pipeline/experiment_worker.py
"""

from __future__ import annotations

import os
import socket
import sys
import time
import traceback
from pathlib import Path

WORKER_VERSION = "quant-experiment-worker-v1"

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "contracts"))
sys.path.insert(0, str(_HERE.parent.parent / "01-research" / "collectors"))

# 큐가 비었을 때 쉬는 시간. 짧으면 DB 를 계속 두드리고, 길면 주문이 오래 대기한다.
IDLE_SLEEP_SEC = 15


def worker_name() -> str:
    """어느 워커가 집었는지 남긴다. 여럿 띄웠을 때 추적이 안 되면 회수를 못 한다."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _conn():
    import psycopg2
    from source_registry import load_project_env

    return psycopg2.connect(load_project_env()["DATABASE_URL"],
                            connect_timeout=20)


def run_one(conn, job: dict) -> dict:
    """작업 하나. **예외가 나도 반드시 종결한다.**"""
    from experiment_orchestrator import orchestrate
    from job_queue import finish

    jid, hid = job["job_id"], job["hypothesis_id"]
    try:
        rep = orchestrate(hid, conn=conn)
        exp = (rep.experiment_refs or {}).get("experiment_id")
        if rep.verdict in ("RUNNABLE",) and exp:
            finish(conn, jid, ok=True, experiment_id=str(exp))
            return {"job_id": jid, "result": "DONE", "experiment_id": str(exp)}
        # ▶ 실행은 됐는데 실험이 안 나온 것도 실패다. "돌긴 돌았다" 를
        #   성공으로 치면 결과 없는 주문이 DONE 으로 쌓인다.
        reason = (f"verdict={rep.verdict}; "
                  f"backlog={'; '.join(rep.backlog)[:200] or '-'}")
        finish(conn, jid, ok=False, reason=reason)
        return {"job_id": jid, "result": "FAILED", "reason": reason}
    except Exception as e:  # noqa: BLE001
        # 집어간 것은 반드시 끝낸다 - 안 그러면 lease 만료까지 그 가설이 묶인다
        reason = f"{type(e).__name__}: {e}"[:400]
        try:
            finish(conn, jid, ok=False, reason=reason)
        except Exception:  # noqa: BLE001
            conn.rollback()
        print(f"⚠ 작업 실패 {jid}: {reason}", file=sys.stderr)
        traceback.print_exc()
        return {"job_id": jid, "result": "FAILED", "reason": reason}


def tick(conn, *, worker: str) -> dict:
    """한 순회. 회수 -> 집기 -> 실행."""
    from job_queue import lease, reclaim

    rec = reclaim(conn)
    jobs = lease(conn, worker=worker)
    results = [run_one(conn, j) for j in jobs]
    return {"reclaimed": rec, "picked": len(jobs), "results": results}


def serve() -> None:
    worker = worker_name()
    print(f"{WORKER_VERSION} 시작 - {worker}", flush=True)
    conn = _conn()
    try:
        while True:
            try:
                r = tick(conn, worker=worker)
                if r["picked"]:
                    for x in r["results"]:
                        print(f"  {x['result']:6} {x['job_id'][:8]} "
                              f"{x.get('reason', '')[:70]}", flush=True)
                else:
                    if r["reclaimed"]["requeued"] or r["reclaimed"]["failed"]:
                        print(f"  회수 {r['reclaimed']}", flush=True)
                    time.sleep(IDLE_SLEEP_SEC)
            except Exception as e:  # noqa: BLE001
                # 순회가 죽어도 워커는 산다 - 죽으면 큐가 멈춘다
                print(f"⚠ 순회 실패(계속 돈다): {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    conn = _conn()
                time.sleep(IDLE_SLEEP_SEC)
    finally:
        conn.close()


# ── 자체 점검 ────────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self):
        self.rolled = 0

    def rollback(self):
        self.rolled += 1


def _check_worker_name_is_traceable():
    """여럿 띄웠을 때 누가 집었는지 모르면 회수를 못 한다."""
    n = worker_name()
    assert ":" in n and n.split(":")[-1].isdigit(), n


def _check_failure_always_finishes(monkey=None):
    """**집어간 것은 반드시 끝낸다.** 안 그러면 lease 만료까지 가설이 묶인다."""
    import experiment_worker as W

    calls = []

    def fake_finish(conn, jid, *, ok, experiment_id=None, reason=None):
        calls.append({"job_id": jid, "ok": ok, "reason": reason})

    def boom(hid, conn=None):
        raise RuntimeError("백테스트 폭발")

    import types
    mod = types.ModuleType("job_queue")
    mod.finish = fake_finish
    orch = types.ModuleType("experiment_orchestrator")
    orch.orchestrate = boom
    sys.modules["job_queue"], sys.modules["experiment_orchestrator"] = mod, orch
    try:
        r = W.run_one(_FakeConn(), {"job_id": "j1", "hypothesis_id": "h1"})
        assert r["result"] == "FAILED", r
        assert calls and calls[0]["ok"] is False, calls
        # 사유가 반드시 남는다
        assert "백테스트 폭발" in calls[0]["reason"], calls
    finally:
        sys.modules.pop("job_queue", None)
        sys.modules.pop("experiment_orchestrator", None)


def _check_ran_but_no_experiment_is_failure():
    """**"돌긴 돌았다" 를 성공으로 치지 않는다** - 결과 없는 DONE 이 쌓인다."""
    import types

    import experiment_worker as W

    calls = []
    mod = types.ModuleType("job_queue")
    mod.finish = lambda conn, jid, *, ok, experiment_id=None, reason=None: \
        calls.append({"ok": ok, "reason": reason})
    orch = types.ModuleType("experiment_orchestrator")

    class _Rep:
        verdict = "NOT_RUNNABLE"
        experiment_refs: dict = {}
        backlog = ["데이터셋 없음"]

    orch.orchestrate = lambda hid, conn=None: _Rep()
    sys.modules["job_queue"], sys.modules["experiment_orchestrator"] = mod, orch
    try:
        r = W.run_one(_FakeConn(), {"job_id": "j2", "hypothesis_id": "h2"})
        assert r["result"] == "FAILED", r
        assert calls[0]["ok"] is False and "NOT_RUNNABLE" in calls[0]["reason"]
    finally:
        sys.modules.pop("job_queue", None)
        sys.modules.pop("experiment_orchestrator", None)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--serve" in sys.argv:
        serve()
        raise SystemExit(0)

    print(f"{WORKER_VERSION} 자체 점검 (DB 없음)")
    _check_worker_name_is_traceable();      print("  워커 식별 가능          OK")
    _check_failure_always_finishes();       print("  예외에도 반드시 종결     OK")
    _check_ran_but_no_experiment_is_failure(); print("  결과 없음 = 실패        OK")
    print("실험 워커 3개 영역 통과. 상주 실행은 --serve")
