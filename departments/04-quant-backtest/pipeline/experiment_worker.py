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
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKER_VERSION = "quant-experiment-worker-v1"

# 가설이 RUNNING 에 갇혀 있다고 보는 시간. lease 만료(30분)보다 짧으면 아직
# 큐에서 도는 작업을 뺏게 되므로 같은 값을 쓴다.
HYPOTHESIS_STALL_MIN = 30

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


# ── 가설 스톨 회수 ───────────────────────────────────────────────────────────
#
# ▶ 왜 필요한가 (2026-08-12 실측)
#   작업(job)은 `reclaim` 이 회수하지만 **가설 상태는 아무도 안 되돌린다.**
#   orchestrate 는 PROPOSED -> PREREGISTERED -> RUNNING 으로 옮긴 다음 백테스트를
#   돌리는데, 그 뒤에서 터지면 job 은 FAILED 로 종결되고 가설은 `RUNNING` 에
#   남는다. 그런데 발주는 `where h.status = 'PROPOSED'`(factory_autopilot.py:576)
#   라 다시 집히지 않고, 브리핑 목록(:124)에도 RUNNING 은 없어 **사람 눈에도
#   안 보인다.**
#
#   실측: `667f0a45`(mean_reversion)가 2026-08-11 18:22 부터 8시간 45분,
#   `4b253d12`(low_volatility)가 KeyError 로 죽은 채 갇혀 있었다. 그동안 공장은
#   못 도는 두 건만 15분마다 재시도했다 - **돌 수 있는 것만 안 돌았다.**
#
# ▶ 조용히 되돌리지 않는다
#   되돌리기 전 마지막 실패 사유를 같이 싣는다. 사유는 `experiment_jobs.
#   failure_reason` 에 남아 있으므로 이력은 지워지지 않는다 - 여기서는 그것을
#   읽어 로그로 드러낸다. 실패가 사라지면 같은 실험을 새 것으로 착각한다.

_STALL_SQL = """
    select h.hypothesis_id::text, h.status, h.status_changed_at,
           (select count(*) from quant.experiment_jobs j
             where j.hypothesis_id = h.hypothesis_id
               and j.status in ('QUEUED', 'LEASED')) as open_jobs,
           (select j.failure_reason from quant.experiment_jobs j
             where j.hypothesis_id = h.hypothesis_id and j.status = 'FAILED'
             order by j.updated_at desc limit 1) as last_failure
      from quant.hypotheses h
     where h.status = 'RUNNING'
"""


def stalled_hypotheses(rows: list[dict], *, now: datetime,
                       stall_min: int = HYPOTHESIS_STALL_MIN) -> list[dict]:
    """(순수 함수) 되돌릴 가설. 판단이 아니라 상태·시간 확인이다."""
    out: list[dict] = []
    for r in rows:
        if r.get("status") != "RUNNING":
            continue
        # 아직 큐에 살아 있는 작업이 있으면 도는 중이다 - 뺏지 않는다.
        if r.get("open_jobs"):
            continue
        changed = r.get("status_changed_at")
        if changed is None:
            continue
        if now - changed < timedelta(minutes=stall_min):
            continue
        out.append({
            "hypothesis_id": r["hypothesis_id"],
            "stalled_min": int((now - changed).total_seconds() // 60),
            "last_failure": r.get("last_failure") or "(사유 기록 없음)",
        })
    return out


def reclaim_hypotheses(conn, *, now: datetime | None = None) -> dict:
    """RUNNING 에 갇힌 가설을 PROPOSED 로 되돌린다. 발주가 다시 집게 하려는 것."""
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(_STALL_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    victims = stalled_hypotheses(rows, now=now)
    if not victims:
        return {"checked": len(rows), "requeued": 0, "items": []}

    with conn.cursor() as cur:
        for v in victims:
            # PROPOSED 로 되돌린다 - 오케스트레이터 계약상 "가설이 틀린 게 아니라
            # 실험 수단이 없는 것" 이면 PROPOSED 로 남는 것이 정상 상태다.
            cur.execute(
                "update quant.hypotheses set status='PROPOSED', "
                "status_changed_at=now() where hypothesis_id=%s::uuid "
                "and status='RUNNING'", (v["hypothesis_id"],))
    conn.commit()
    return {"checked": len(rows), "requeued": len(victims), "items": victims}


def tick(conn, *, worker: str) -> dict:
    """한 순회. 가설 회수 -> 작업 회수 -> 집기 -> 실행."""
    from job_queue import lease, reclaim

    hyp = reclaim_hypotheses(conn)
    rec = reclaim(conn)
    jobs = lease(conn, worker=worker)
    results = [run_one(conn, j) for j in jobs]
    return {"hypotheses": hyp, "reclaimed": rec,
            "picked": len(jobs), "results": results}


def serve() -> None:
    worker = worker_name()
    print(f"{WORKER_VERSION} 시작 - {worker}", flush=True)
    conn = _conn()
    try:
        while True:
            try:
                r = tick(conn, worker=worker)
                # 되돌린 것은 항상 드러낸다 - 조용히 되돌리면 같은 실험을
                # 새 것으로 착각한다.
                for v in r["hypotheses"]["items"]:
                    print(f"  회수 가설 {v['hypothesis_id'][:8]} "
                          f"RUNNING {v['stalled_min']}분 -> PROPOSED "
                          f"| 마지막 실패: {v['last_failure'][:90]}", flush=True)
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


def _hyp(**kw) -> dict:
    d = {"hypothesis_id": "h1", "status": "RUNNING", "open_jobs": 0,
         "status_changed_at": datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc),
         "last_failure": "KeyError: 'low_volatility'"}
    d.update(kw)
    return d


def _check_running_job_is_not_stolen():
    """큐에 살아 있는 작업이 있으면 도는 중이다 - 뺏으면 같은 실험이 둘이 된다."""
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    assert stalled_hypotheses([_hyp(open_jobs=1)], now=now) == []


def _check_fresh_running_is_kept():
    """방금 RUNNING 이 된 것을 되돌리면 정상 실험을 죽인다."""
    now = datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)   # 10분 경과
    assert stalled_hypotheses([_hyp()], now=now) == []


def _check_stalled_is_requeued_with_reason():
    """**되돌리되 사유를 싣는다.** 조용히 되돌리면 실패가 사라진다."""
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)    # 5시간 경과
    got = stalled_hypotheses([_hyp()], now=now)
    assert len(got) == 1, got
    assert got[0]["stalled_min"] == 300, got
    assert "KeyError" in got[0]["last_failure"], got
    # 사유가 없어도 되돌리기는 한다 - 다만 없다는 것을 적는다
    got2 = stalled_hypotheses([_hyp(last_failure=None)], now=now)
    assert got2[0]["last_failure"] == "(사유 기록 없음)", got2


def _check_only_running_is_touched():
    """PROPOSED·SUPPORTED 를 건드리면 종결된 실험이 되살아난다."""
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    for s in ("PROPOSED", "TESTING", "SUPPORTED", "REJECTED", "INCONCLUSIVE"):
        assert stalled_hypotheses([_hyp(status=s)], now=now) == [], s


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
    _check_running_job_is_not_stolen();     print("  도는 작업은 안 뺏는다    OK")
    _check_fresh_running_is_kept();         print("  갓 RUNNING 은 유지       OK")
    _check_stalled_is_requeued_with_reason(); print("  스톨 회수 + 사유 표기   OK")
    _check_only_running_is_touched();       print("  종결 상태는 불가침       OK")
    print("실험 워커 7개 영역 통과. 상주 실행은 --serve")
