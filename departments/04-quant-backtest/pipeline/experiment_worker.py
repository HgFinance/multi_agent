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


# 가설이 여기 오면 이 주문은 다시 돌 이유가 없다. 회수(`RUNNING` 만 되돌림)도
# 발주(`PROPOSED` 만 집음)도 건드리지 않는 상태들이다. **재실험이 필요하면
# 리서치가 새 가설을 내는 것이 계약이다** - 워커가 종결을 되돌리지 않는다.
TERMINAL_HYPOTHESIS_STATUSES = frozenset({
    "REJECTED", "SUPPORTED", "INCONCLUSIVE", "ARCHIVED", "PROMOTED",
})


def hypothesis_is_terminal(conn, hypothesis_id: str) -> bool:
    """가설이 종결 상태인가. 못 읽으면 **종결로 보지 않는다**(지어내지 않는다).

    판단을 못 했다고 주문을 닫으면 멀쩡한 것을 죽인다. 반대로 놓아주기는
    반복될 뿐이라 회복 가능하므로, 불확실할 때는 놓아주기 쪽으로 기운다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("select status from quant.hypotheses "
                        "where hypothesis_id = %s::uuid", (hypothesis_id,))
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - 못 물으면 종결로 몰지 않는다
        return False
    return bool(row) and str(row[0] or "").upper() in TERMINAL_HYPOTHESIS_STATUSES


def run_one(conn, job: dict) -> dict:
    """작업 하나. **예외가 나도 반드시 종결한다.**"""
    from experiment_orchestrator import orchestrate
    from job_queue import finish, release

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
        # ▶ **타이밍은 실패가 아니다** (2026-08-14 실측)
        #   가설이 아직 RUNNING 이라 전이가 거부된 것은 이 주문의 흠이 아니다 -
        #   회수가 10분 정체 기준으로 PROPOSED 로 되돌리면 그대로 돌 주문이다.
        #   실측 24시간 실패 13건 중 7건이 이것이었고 전부 attempts=2/2 로
        #   재시도 예산을 태운 채 영구 FAILED 였다. 놓아주고 다음 순회에 맡긴다.
        if rep.verdict == "BAD_TRANSITION":
            # ▶ **종결된 가설의 주문은 놓아주지 않는다** (2026-08-14 실측)
            #   놓아주기는 "회수가 곧 PROPOSED 로 풀어 줄 것" 이라는 전제 위에
            #   선다. 그런데 회수는 `status='RUNNING'` 만 되돌리고 발주는
            #   `PROPOSED` 만 집으므로, 가설이 INCONCLUSIVE·REJECTED 로 **종결**
            #   되면 그 전제가 깨진다 - 주문은 QUEUED 로 남아 집힐 때마다 같은
            #   BAD_TRANSITION 을 내고 영원히 놓아주기를 반복한다.
            #   실측: `805a81b5`·`17bd0213` 두 건이 INCONCLUSIVE 인 채 큐에
            #   앉아 그 고리를 돌고 있었다("INCONCLUSIVE -> PREREGISTERED").
            #   종결된 가설의 주문은 **할 일이 없다.** 사유를 남기고 닫는다.
            if hypothesis_is_terminal(conn, hid):
                done_reason = (f"{reason} | 가설이 이미 종결 상태다 - 회수도 "
                               f"발주도 이 주문을 되살리지 않으므로 닫는다")
                finish(conn, jid, ok=False, reason=done_reason)
                return {"job_id": jid, "result": "FAILED",
                        "reason": done_reason}
            release(conn, jid, reason=reason)
            return {"job_id": jid, "result": "RELEASED", "reason": reason}
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
               and j.status = 'LEASED') as leased_jobs,
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
        # **집혀 있는(LEASED) 작업이 있으면 도는 중이다 - 뺏지 않는다.**
        #
        # ▶ 큐에 있는(QUEUED) 것까지 세면 안 된다 (2026-08-14 실측)
        #   원래는 `QUEUED, LEASED` 를 같이 셌다. 그런데 같은 날 들어온
        #   놓아주기(`job_queue.release`)가 BAD_TRANSITION 주문을 FAILED 가
        #   아니라 **QUEUED 로 되돌린다.** 그 순간 두 장치가 서로를 기다렸다:
        #     · 놓아주기는 "회수가 곧 가설을 PROPOSED 로 돌려줄 것" 이라 기다리고
        #     · 회수는 "큐에 작업이 살아 있으니 도는 중" 이라 안 건드린다
        #   실측 교착: 주문 3건이 각각 36·16·14회 놓아주기를 반복했고, 그
        #   가설들은 RUNNING 에 92·89·76분 갇혀 있었다. 둘 다 "기다리는 중"
        #   이라 로그는 조용했다.
        #
        #   **QUEUED 는 도는 중이 아니라 기다리는 중이다.** 그리고 가설이
        #   RUNNING 인 한 그 대기는 영원하다 - 집혀도 같은 BAD_TRANSITION 이다.
        #   집힌 것만 세면 30분 lease 만료가 이미 경계를 지켜 준다.
        if r.get("leased_jobs"):
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


_SQL_CANCEL_ZOMBIES = """
    update quant.experiments e
       set status = 'CANCELLED', ended_at = now()
     where e.hypothesis_id = %s::uuid
       and e.status = 'RUNNING'
       and e.ended_at is null
       and e.started_at < now() - interval '10 minutes'
       and not exists (select 1 from quant.backtest_runs r
                        where r.experiment_id = e.experiment_id)
       and not exists (select 1 from research.experiment_outcomes o
                        where o.experiment_id = e.experiment_id::text)
"""


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
            # ▶ 버려진 실험 행을 **같은 트랜잭션에서 닫는다** (2026-08-13 실측)
            #   가설만 되돌리면 재발주가 새 실험 행을 만들고 옛 RUNNING 행은
            #   아무도 안 닫는다 - 좀비 11건이 쌓였고(같은 가설에 3건까지),
            #   count_family_trials 가 이 행들을 시도로 세서 계열 예산을 정보 0
            #   으로 소모했다. 좀비 술어(backtest_runner.zombie_experiment 와
            #   동일: run 0·판정 0·종료 없음·10분 경과)만 닫는다 - **판정이 붙은
            #   행은 불가침**이고, FAILED 가 아니라 CANCELLED 다(실행이 실패한
            #   게 아니라 실행되기 전에 버려진 것이다).
            cur.execute(_SQL_CANCEL_ZOMBIES, (v["hypothesis_id"],))
            v["cancelled_experiments"] = cur.rowcount
    conn.commit()
    return {"checked": len(rows), "requeued": len(victims), "items": victims}


# ── 고아 실험 소탕 ───────────────────────────────────────────────────────────
#
# ▶ 왜 필요한가 (2026-08-14, 병목 카드 t_0c6f76a9)
#   실행부는 COMPLETED 를 자기 트랜잭션으로 커밋하고 판정·환류는 그 뒤에
#   붙는다. 그 사이에서 죽으면 실험은 COMPLETED 인 채 환류 0건으로 남고,
#   중복 가드가 재실행마저 막아 **어떤 경로도 다시 판정하지 않는다** -
#   21건이 그렇게 쌓였고 TESTING 정체 7건의 원인이었다. 회수(reclaim)가
#   가설을 되살리듯, 여기는 판정을 완주시킨다(orphan_finalizer 참고).

# 소탕 주기. 매 순회(15초)마다 anti-join 을 두드리는 것은 과하다 - 고아는
# 사고 잔여물이라 시간 단위로 충분하다. 워커 시작 직후 한 번은 바로 돈다.
ORPHAN_SWEEP_SEC = 900
_last_orphan_sweep: float | None = None


def sweep_orphans(conn) -> dict | None:
    """판정 없는 COMPLETED 실험을 완주시킨다. 실패해도 순회는 산다."""
    global _last_orphan_sweep
    now = time.monotonic()
    if _last_orphan_sweep is not None and \
            now - _last_orphan_sweep < ORPHAN_SWEEP_SEC:
        return None
    _last_orphan_sweep = now
    try:
        from orphan_finalizer import finalize_orphans

        return finalize_orphans(conn)
    except Exception as e:  # noqa: BLE001 - 소탕 실패가 큐를 멈추면 안 된다
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 정상 종료 시 집은 주문 반납 ──────────────────────────────────────────────
#
# ▶ 왜 필요한가 (2026-08-14 실측)
#   워커를 재기동하면 그때 집혀 있던 주문이 lease 만료(30분)까지 묶여 있다가
#   `reclaim_decision` 에서 **영구 FAILED** 로 닫힌다. 사유는 "워커 무응답으로
#   회수 - 시도 2/2 소진. 실행이 반복해 죽는다는 뜻" 인데, 그 해석은 **워커가
#   그 주문 때문에 죽었을 때** 맞는 말이다. 배포하려고 사람이 재기동한 것은
#   그 주문의 흠이 아니다.
#
#   실측: 오늘 두 건이 그렇게 죽었다 - 04:48 `04839e7e`, 05:26 `7df0ddd6`.
#   둘 다 재기동 시각과 정확히 겹친다. 하루에 배포를 여러 번 하는 동안
#   **배포마다 한 건씩** 영구 실패가 쌓이는 구조였다.
#
# ▶ 크래시와 구분한다
#   여기서 반납하는 것은 SIGTERM/SIGINT(정상 종료)뿐이다. 진짜로 죽은 워커는
#   신호를 못 받으므로 기존 lease 만료 경로가 그대로 처리한다 - 무한 재시도를
#   막는 그 보호는 남는다.
_HELD: dict = {"jobs": []}


def release_held_jobs(job_ids, *, connect, release_fn, reason: str) -> int:
    """집고 있던 주문을 반납한다. **도는 트랜잭션을 건드리지 않는다.**

    백테스트 도중에 신호가 오면 순회용 연결은 트랜잭션 한복판일 수 있다.
    거기에 끼어들어 쓰면 그 트랜잭션이 무엇을 남길지 알 수 없으므로, 반납은
    **새 연결**로 한다(`connect`). 이미 끝난 주문은 `release` 가
    `status='LEASED'` 조건으로 걸러 내므로 그냥 넘어간다.
    """
    ids = [j for j in (job_ids or []) if j]
    if not ids:
        return 0
    conn = connect()
    try:
        for jid in ids:
            release_fn(conn, jid, reason=reason)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 닫기 실패가 반납을 무르게 하지 않는다
            pass
    return len(ids)


def _on_shutdown(signum, _frame):  # pragma: no cover - 신호 경로
    from job_queue import release  # noqa: PLC0415

    held = list(_HELD.get("jobs") or [])
    try:
        n = release_held_jobs(
            held, connect=_conn, release_fn=release,
            reason=f"워커 정상 종료(signal {signum})로 반납 - 배포·재기동은 "
                   f"이 주문의 흠이 아니다. 시도 예산을 태우지 않는다")
        print(f"{WORKER_VERSION} 종료 - 집고 있던 주문 {n}건 반납", flush=True)
    except Exception as e:  # noqa: BLE001 - 반납 실패해도 종료는 한다
        print(f"⚠ 종료 반납 실패(다음 lease 만료가 처리한다): "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def tick(conn, *, worker: str) -> dict:
    """한 순회. 가설 회수 -> 작업 회수 -> 고아 소탕 -> 집기 -> 실행."""
    from job_queue import lease, reclaim

    hyp = reclaim_hypotheses(conn)
    rec = reclaim(conn)
    orph = sweep_orphans(conn)
    jobs = lease(conn, worker=worker)
    # 집는 순간부터 반납 대상이다 - 실행 중에 신호가 와도 놓아줄 수 있어야 한다
    _HELD["jobs"] = [str(j["job_id"]) for j in jobs]
    try:
        results = [run_one(conn, j) for j in jobs]
    finally:
        _HELD["jobs"] = []
    return {"hypotheses": hyp, "reclaimed": rec, "orphans": orph,
            "picked": len(jobs), "results": results}


def connection_is_usable(conn) -> bool:
    """**쓰기 전에 살아 있는지 묻는다** (2026-08-14 실측).

    `DATABASE_URL` 은 Supabase transaction pooler(6543)다. 백테스트가 도는 수
    분 동안 이 연결은 idle 이라 풀러가 끊어 버리고, 다음 순회가
    `InterfaceError: connection already closed` 로 통째로 실패했다 - 그 순회가
    집어 둔 작업은 30분 리스 만료까지 묶인다.

    끊긴 것은 사고가 아니라 **풀러의 정상 동작**이다. 그러니 예외로 받아
    순회를 죽이지 말고, 순회 시작에 한 번 물어 조용히 다시 잡는다.
    `select 1` 한 번이면 되고, 15초 주기에서 이 비용은 무시할 만하다.
    """
    if conn is None or getattr(conn, "closed", 1):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("select 1")
        conn.rollback()
        return True
    except Exception:  # noqa: BLE001 - 못 물으면 죽은 것으로 본다
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - 이미 죽은 연결
            pass
        return False


def serve() -> None:
    import signal  # noqa: PLC0415

    worker = worker_name()
    print(f"{WORKER_VERSION} 시작 - {worker}", flush=True)
    # ▶ 배포 재기동이 주문을 태우지 않게 한다. docker 는 SIGTERM 을 먼저 보내고
    #   유예(기본 10초) 뒤 SIGKILL 한다 - 반납은 새 연결 한 번이라 그 안에 끝난다.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_shutdown)
        except (ValueError, OSError):  # noqa: PERF203 - 메인 스레드가 아니면 건너뛴다
            pass
    conn = _conn()
    try:
        while True:
            try:
                if not connection_is_usable(conn):
                    conn = _conn()
                r = tick(conn, worker=worker)
                # 되돌린 것은 항상 드러낸다 - 조용히 되돌리면 같은 실험을
                # 새 것으로 착각한다.
                for v in r["hypotheses"]["items"]:
                    print(f"  회수 가설 {v['hypothesis_id'][:8]} "
                          f"RUNNING {v['stalled_min']}분 -> PROPOSED "
                          f"| 좀비 실험 {v.get('cancelled_experiments', 0)}건 닫음 "
                          f"| 마지막 실패: {v['last_failure'][:90]}", flush=True)
                o = r.get("orphans") or {}
                if o.get("finalized") or o.get("failed") or o.get("error"):
                    # 완주한 것은 항상 드러낸다 - 조용한 환류는 검증 불가다
                    print(f"  고아 완주: 검사 {o.get('checked', 0)}건 "
                          f"완주 {len(o.get('finalized') or [])}건 "
                          f"실패 {len(o.get('failed') or [])}건 "
                          f"{o.get('error') or ''}".rstrip(), flush=True)
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
    mod.release = _never_release
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


def _never_release(conn, job_id, *, reason):
    """놓아주기가 일어나면 안 되는 경로에 꽂는 스텁.

    놓아주기는 시도 예산을 되돌리므로, 정말 못 도는 주문에까지 쓰면 그 주문이
    큐에서 영원히 돈다. 어느 경로가 종결이고 어느 경로가 대기인지 검사가
    구분하게 만든다.
    """
    raise AssertionError(f"이 경로는 놓아주기가 아니라 종결이어야 한다: {reason}")


def _check_terminal_hypothesis_job_is_closed_not_parked():
    """**종결된 가설의 주문은 닫는다** (2026-08-14 실측).

    놓아주기는 "회수가 곧 PROPOSED 로 풀어 준다" 는 전제 위에 선다. 가설이
    INCONCLUSIVE·REJECTED 로 종결되면 회수(RUNNING 만)도 발주(PROPOSED 만)도
    그 주문을 건드리지 않으므로 전제가 깨지고, 집힐 때마다 같은
    BAD_TRANSITION 을 내며 영원히 돈다(실측 2건: 805a81b5·17bd0213).
    """
    import types

    import experiment_worker as W

    class _StatusConn:
        def __init__(self, status):
            self._status = status

        def cursor(self):
            status = self._status

            class _C:
                def __enter__(self_):
                    return self_
                def __exit__(self_, *a):
                    return False
                def execute(self_, *a, **k):
                    return None
                def fetchone(self_):
                    return (status,)
            return _C()

    calls = []
    mod = types.ModuleType("job_queue")
    mod.finish = lambda conn, jid, *, ok, experiment_id=None, reason=None: \
        calls.append(("finish", ok, reason))
    mod.release = lambda conn, jid, *, reason: calls.append(("release", reason))

    class _Rep:
        experiment_refs = None
        backlog = ["INCONCLUSIVE -> PREREGISTERED 는 계약 순서를 건너뛴다"]
        verdict = "BAD_TRANSITION"

    orch = types.ModuleType("experiment_orchestrator")
    orch.orchestrate = lambda hid, conn=None: _Rep()
    sys.modules["job_queue"], sys.modules["experiment_orchestrator"] = mod, orch
    try:
        # 종결 상태 -> 닫는다
        for st in ("INCONCLUSIVE", "REJECTED", "ARCHIVED"):
            calls.clear()
            r = W.run_one(_StatusConn(st), {"job_id": "j1", "hypothesis_id": "h1"})
            assert r["result"] == "FAILED", (st, r)
            assert [c[0] for c in calls] == ["finish"], (st, calls)
            assert "종결" in (calls[0][2] or ""), (st, calls)

        # 아직 도는 상태 -> 놓아준다(예산 보존)
        calls.clear()
        r2 = W.run_one(_StatusConn("RUNNING"), {"job_id": "j2",
                                                "hypothesis_id": "h1"})
        assert r2["result"] == "RELEASED", r2
        assert [c[0] for c in calls] == ["release"], calls
    finally:
        sys.modules.pop("job_queue", None)
        sys.modules.pop("experiment_orchestrator", None)

    # 상태를 못 물으면 **종결로 몰지 않는다** - 판단 못 한 것으로 주문을 죽이면
    # 멀쩡한 것을 잃는다. 놓아주기는 반복될 뿐이라 회복 가능하다.
    class _Boom:
        def cursor(self):
            raise RuntimeError("연결이 끊겼다")
    assert W.hypothesis_is_terminal(_Boom(), "h1") is False

    class _Missing:
        def cursor(self):
            class _C:
                def __enter__(self_):
                    return self_
                def __exit__(self_, *a):
                    return False
                def execute(self_, *a, **k):
                    return None
                def fetchone(self_):
                    return None
            return _C()
    assert W.hypothesis_is_terminal(_Missing(), "h1") is False


def _check_dead_connection_is_reconnected_not_fatal():
    """**끊긴 연결이 순회를 죽이지 않는다** (2026-08-14 실측).

    풀러가 idle 연결을 끊는 것은 정상 동작인데, 그것을 예외로 받아 순회가
    통째로 실패했다(`InterfaceError: connection already closed`). 그 순회가
    집어 둔 작업은 30분 리스 만료까지 묶인다 - 큐가 그만큼 멈춘다.
    """
    assert not connection_is_usable(None)

    class _Closed:
        closed = 1
    assert not connection_is_usable(_Closed())

    closed_calls = []

    class _Broken:
        closed = 0
        def cursor(self):
            raise RuntimeError("서버가 연결을 끊었다")
        def close(self):
            closed_calls.append(True)
    assert not connection_is_usable(_Broken())
    assert closed_calls, "죽은 연결을 닫지 않으면 소켓이 샌다"

    class _Live:
        closed = 0
        rolled = []
        def cursor(self):
            class _C:
                def __enter__(self_):
                    return self_
                def __exit__(self_, *a):
                    return False
                def execute(self_, *a, **k):
                    return None
            return _C()
        def rollback(self):
            _Live.rolled.append(True)
    assert connection_is_usable(_Live())
    assert _Live.rolled, "확인 쿼리의 트랜잭션을 안 닫으면 idle in transaction 이 쌓인다"

    # 순회가 시작 전에 실제로 묻는지 - 물어보지 않으면 이 함수는 장식이다.
    import ast
    import pathlib
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    body = ast.get_source_segment(src, next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "serve")) or ""
    assert "connection_is_usable(conn)" in body, "순회가 연결 생존을 안 묻는다"


def _check_bad_transition_is_released_not_failed():
    """**타이밍을 영구 실패로 굳히지 않는다** (2026-08-14 실측).

    가설이 아직 RUNNING 이라 전이가 거부된 것은 회수가 곧 풀어줄 상태다.
    그런데 `finish(ok=False)` 로 종결하고 있었고, 24시간 실패 13건 중 7건이
    이것이었다 - 전부 attempts=2/2 로 재시도 예산을 태운 채 굳었다.

    함께 고정하는 것: **놓아주기가 모든 실패를 삼키면 안 된다.** 못 도는
    주문(NOT_RUNNABLE 등)은 여전히 종결돼야 큐에서 영원히 돌지 않는다.
    """
    import types

    import experiment_worker as W

    calls = []
    mod = types.ModuleType("job_queue")
    mod.finish = lambda conn, jid, *, ok, experiment_id=None, reason=None: \
        calls.append(("finish", ok, reason))
    mod.release = lambda conn, jid, *, reason: calls.append(("release", reason))

    class _Rep:
        experiment_refs = None
        backlog = ["RUNNING -> PREREGISTERED 는 계약 순서를 건너뛴다"]
        verdict = "BAD_TRANSITION"

    orch = types.ModuleType("experiment_orchestrator")
    orch.orchestrate = lambda hid, conn=None: _Rep()
    sys.modules["job_queue"], sys.modules["experiment_orchestrator"] = mod, orch
    try:
        r = W.run_one(_FakeConn(), {"job_id": "j1", "hypothesis_id": "h1"})
        assert r["result"] == "RELEASED", r
        assert [c[0] for c in calls] == ["release"], calls
        assert "BAD_TRANSITION" in calls[0][1], calls

        calls.clear()
        _Rep.verdict = "NOT_RUNNABLE"
        r2 = W.run_one(_FakeConn(), {"job_id": "j2", "hypothesis_id": "h1"})
        assert r2["result"] == "FAILED", r2
        assert [c[0] for c in calls] == ["finish"], \
            f"못 도는 주문까지 놓아주면 큐에서 영원히 돈다: {calls}"
        assert calls[0][1] is False, calls
    finally:
        _Rep.verdict = "BAD_TRANSITION"
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
    mod.release = _never_release
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
    d = {"hypothesis_id": "h1", "status": "RUNNING", "leased_jobs": 0,
         "status_changed_at": datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc),
         "last_failure": "KeyError: 'low_volatility'"}
    d.update(kw)
    return d


def _check_running_job_is_not_stolen():
    """집혀 있는 작업이 있으면 도는 중이다 - 뺏으면 같은 실험이 둘이 된다."""
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    assert stalled_hypotheses([_hyp(leased_jobs=1)], now=now) == []


def _check_shutdown_returns_the_job_instead_of_burning_it():
    """**배포 재기동은 그 주문의 흠이 아니다** (2026-08-14 실측).

    재기동 때 집혀 있던 주문은 lease 만료까지 묶였다가 `reclaim_decision` 이
    "시도 2/2 소진 - 실행이 반복해 죽는다" 로 **영구 FAILED** 처리했다.
    오늘 두 건이 그렇게 죽었고(04:48 `04839e7e`, 05:26 `7df0ddd6`) 둘 다
    재기동 시각과 겹친다 - 배포할 때마다 한 건씩 태우는 구조였다.

    여기서 고정하는 것:
      ① 집고 있던 주문을 **전부** 반납한다.
      ② 반납은 **새 연결**로 한다 - 백테스트 도중이면 순회용 연결은
         트랜잭션 한복판이라 거기 끼어들면 무엇이 남을지 알 수 없다.
      ③ 반납 뒤 연결을 닫는다 - 종료 경로가 연결을 흘리면 풀이 마른다.
    """
    calls, opened, closed = [], [], []

    class _C:
        def close(self):
            closed.append(1)

    def _connect():
        c = _C()
        opened.append(c)
        return c

    def _release(conn, jid, *, reason):
        assert isinstance(conn, _C), "순회용 연결로 반납하면 안 된다 - 새 연결이어야 한다"
        calls.append((jid, reason))

    n = release_held_jobs(["j1", "j2"], connect=_connect,
                          release_fn=_release, reason="정상 종료 반납")
    assert n == 2 and [c[0] for c in calls] == ["j1", "j2"], (n, calls)
    assert len(opened) == 1, ("연결은 한 번만 연다", opened)
    assert len(closed) == 1, ("반납 뒤 연결을 안 닫았다", closed)

    # 집은 것이 없으면 연결도 열지 않는다 - 매 종료마다 풀을 건드릴 이유가 없다
    opened.clear()
    assert release_held_jobs([], connect=_connect, release_fn=_release,
                             reason="x") == 0
    assert not opened, "반납할 것이 없는데 연결을 열었다"

    # 반납이 실패해도 연결은 닫는다(다음 lease 만료가 처리하게 두고 빠진다)
    def _boom(conn, jid, *, reason):
        raise RuntimeError("반납 실패")

    closed.clear()
    try:
        release_held_jobs(["j9"], connect=_connect, release_fn=_boom, reason="x")
    except RuntimeError:
        pass
    assert len(closed) == 1, "반납이 터지면 연결이 샌다"


def _check_parked_job_does_not_block_reclaim():
    """**기다리는 주문이 회수를 막으면 교착이다** (2026-08-14 실측).

    놓아주기(`job_queue.release`)는 BAD_TRANSITION 주문을 FAILED 가 아니라
    QUEUED 로 되돌린다 - "회수가 곧 가설을 PROPOSED 로 풀어 줄 것" 이라는
    전제다. 그런데 회수가 `QUEUED` 도 "도는 중" 으로 세면 **그 전제를 자기가
    깬다.** 실측으로 주문 3건이 36·16·14회 놓아주기를 반복했고 가설은
    RUNNING 에 92·89·76분 갇혔다.

    여기서 고정하는 것: **QUEUED 는 회수를 막지 않는다.** 집힌 것만 막는다.
    """
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    got = stalled_hypotheses([_hyp(leased_jobs=0)], now=now)
    assert len(got) == 1, ("놓아준 주문이 큐에 있다고 회수를 건너뛰면 "
                           "가설이 RUNNING 에서 못 나온다", got)

    # 집혀 있으면 여전히 안 뺏는다 - 교착을 푼다고 도는 실험을 죽이면 안 된다
    assert stalled_hypotheses([_hyp(leased_jobs=1)], now=now) == []


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


class _ReclaimConn:
    """회수 검증용 가짜 연결 - 실행된 SQL 을 순서대로 들고 있는다."""

    def __init__(self, rows):
        self.rows, self.executed, self.commits = rows, [], 0

    def commit(self):
        self.commits += 1

    def cursor(self):
        conn = self

        class _Cur:
            description = [(k,) for k in
                           ("hypothesis_id", "status", "leased_jobs",
                            "status_changed_at", "last_failure")]
            rowcount = 3

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                conn.executed.append(" ".join(sql.split()))

            def fetchall(self):
                keys = [d[0] for d in self.description]
                return [tuple(r[k] for k in keys) for r in conn.rows]

        return _Cur()


def _check_reclaim_closes_abandoned_experiments():
    """**가설을 되돌릴 때 버려진 실험 행도 닫는다** (2026-08-13 실측).

    가설만 PROPOSED 로 되돌리면 재발주가 새 실험 행을 만들고 옛 RUNNING 행은
    영원히 산 척한다 - 좀비 11건 실측(같은 가설에 3건까지). 닫되 **좀비 술어**
    (run 0·판정 0·종료 없음·10분 경과)로만 닫고, 판정 붙은 행은 불가침이다.
    """
    now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    conn = _ReclaimConn([_hyp()])
    r = reclaim_hypotheses(conn, now=now)
    assert r["requeued"] == 1, r
    # 닫은 개수가 조용히 사라지지 않는다
    assert r["items"][0]["cancelled_experiments"] == 3, r["items"]
    joined = " || ".join(conn.executed)
    assert "set status='PROPOSED'" in joined, joined
    # 좀비 술어의 네 조항이 전부 SQL 에 있어야 한다 - 하나라도 빠지면
    # 정상 실험이나 판정 붙은 행을 닫는 사고가 된다
    z = next(s for s in conn.executed if "CANCELLED" in s)
    for guard in ("e.status = 'RUNNING'", "e.ended_at is null",
                  "interval '10 minutes'",
                  "not exists (select 1 from quant.backtest_runs",
                  "not exists (select 1 from research.experiment_outcomes"):
        assert guard in z, (guard, z)
    assert conn.commits == 1


def _check_orphan_sweep_is_throttled_and_isolated():
    """소탕은 주기로 돌고, 죽어도 순회를 못 죽인다 - 오류는 삼키되 드러낸다."""
    import experiment_worker as W

    W._last_orphan_sweep = None
    conn = _FakeConn()                 # cursor() 가 없어 소탕 내부에서 터진다
    r = W.sweep_orphans(conn)
    assert r is not None and "error" in r, r
    assert conn.rolled >= 1, conn.rolled       # 실패가 트랜잭션을 안 남긴다
    assert W.sweep_orphans(conn) is None       # 방금 돌았다 - 주기 전엔 안 돈다


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
    _check_reclaim_closes_abandoned_experiments()
    print("  회수가 좀비 실험도 닫음  OK")
    _check_orphan_sweep_is_throttled_and_isolated()
    print("  고아 소탕 격리+주기      OK")
    _check_bad_transition_is_released_not_failed()
    print("  타이밍은 놓아주기        OK")
    _check_parked_job_does_not_block_reclaim()
    print("  기다리는 주문 != 도는 중  OK")
    _check_dead_connection_is_reconnected_not_fatal()
    print("  끊긴 연결은 다시 잡는다   OK")
    _check_shutdown_returns_the_job_instead_of_burning_it()
    print("  정상 종료는 주문을 반납   OK")
    _check_terminal_hypothesis_job_is_closed_not_parked()
    print("  종결 가설 주문은 닫는다   OK")
    print("실험 워커 14개 영역 통과. 상주 실행은 --serve")
