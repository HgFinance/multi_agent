"""실험 작업 큐 - 주문이 사라지지 않게 한다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-04 "실험도구를 무조건적으로 보장해야 함"

▶ 왜 큐인가
  백테스트는 수 분이 걸린다. HTTP 요청 안에서 돌리면 타임아웃으로 죽고,
  죽은 요청은 **"실패했는지 아직 도는지" 를 알 수 없게** 만든다.
  제출과 실행을 분리하면 제출은 즉시 끝나고 상태는 언제든 조회된다.

▶ 워커 사망과 정상 실행을 구분한다
  status='LEASED' 만으로는 둘을 못 가린다. 워커가 죽어도 상태는 LEASED 로
  남아 "돌고 있다" 로 읽힌다. leased_at 을 함께 보고 오래됐으면 회수한다.

  **회수는 자동이되 무한 재시도는 아니다.** max_attempts 를 넘으면 FAILED 로
  두고 사유를 남긴다 - 영원히 되살아나는 작업은 워커를 계속 죽인다.

▶ 실패를 성공으로도, 침묵으로도 위장하지 않는다
  FAILED 에는 사유가 반드시 있다(DB 제약이 강제한다). 사유 없는 실패는
  다시 시도할지 폐기할지 판단할 수 없고 결국 아무도 안 본다.

자체 점검: python departments/04-quant-backtest/pipeline/job_queue.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MODULE_VERSION = "quant-job-queue-v1"

# 워커가 집어간 뒤 이 시간을 넘기면 죽은 것으로 보고 회수한다.
# 백테스트가 실측 3~5분이므로 30분이면 정상 실행을 뺏지 않는다.
LEASE_TIMEOUT_MIN = 30

# 한 번에 집어가는 작업 수. 1 로 둔다 - 워커가 죽으면 잡고 있던 것이
# 전부 회수 대기가 되므로 많이 쥘수록 지연이 커진다.
BATCH = 1


@dataclass(frozen=True)
class Job:
    job_id: str
    hypothesis_id: str
    status: str
    attempts: int
    max_attempts: int
    leased_at: datetime | None = None
    failure_reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts


def is_lease_expired(job: Job, *, now: datetime,
                     timeout_min: int = LEASE_TIMEOUT_MIN) -> bool:
    """워커가 죽었는가. **leased_at 이 없으면 판정하지 않는다.**

    시각을 모르는데 만료로 단정하면 정상 실행을 뺏는다.
    """
    if job.status != "LEASED" or job.leased_at is None:
        return False
    return (now - job.leased_at) > timedelta(minutes=timeout_min)


def reclaim_decision(job: Job, *, now: datetime) -> dict:
    """만료된 lease 를 어떻게 할 것인가. **순수 함수.**

    무한 재시도를 막는다 - 영원히 되살아나는 작업은 워커를 계속 죽인다.
    """
    if not is_lease_expired(job, now=now):
        return {"action": "KEEP", "reason": "lease 가 아직 살아 있다"}
    if job.exhausted:
        return {
            "action": "FAIL",
            "reason": f"워커 무응답으로 회수 - 시도 {job.attempts}/"
                      f"{job.max_attempts} 소진. 실행이 반복해 죽는다는 뜻이므로 "
                      f"자동 재시도를 멈추고 사람이 본다",
        }
    return {"action": "REQUEUE",
            "reason": f"워커 무응답 {LEASE_TIMEOUT_MIN}분 초과 - 재대기 "
                      f"(시도 {job.attempts}/{job.max_attempts})"}


def enqueue(conn, hypothesis_id: str, *, requested_by: str) -> dict:
    """주문 접수. **같은 가설의 활성 주문은 하나뿐이다.**

    중복 제출을 막지 않으면 같은 백테스트가 동시에 돌아 결과가 둘이 되고,
    어느 것이 진짜인지 모르게 된다(DB 부분 유니크 인덱스가 강제한다).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into quant.experiment_jobs (hypothesis_id, requested_by)
            values (%s::uuid, %s)
            on conflict (hypothesis_id) where status in ('QUEUED', 'LEASED')
            do nothing
            returning job_id, status
            """, (hypothesis_id, requested_by))
        row = cur.fetchone()
        if row:
            conn.commit()
            return {"accepted": True, "job_id": str(row[0]), "status": row[1]}
        # ▶ 조용히 성공으로 넘기지 않는다 - 호출부가 새 주문이 들어간 줄 안다
        cur.execute(
            """select job_id, status from quant.experiment_jobs
               where hypothesis_id = %s::uuid and status in ('QUEUED','LEASED')
               limit 1""", (hypothesis_id,))
        cur_row = cur.fetchone()
    conn.rollback()
    return {"accepted": False,
            "reason": "이 가설에 이미 대기·실행 중인 주문이 있다",
            "job_id": str(cur_row[0]) if cur_row else None,
            "status": cur_row[1] if cur_row else None}


def lease(conn, *, worker: str, batch: int = BATCH) -> list[dict]:
    """작업을 집어간다. **동시에 두 워커가 같은 것을 못 집는다.**

    for update skip locked 가 없으면 두 워커가 같은 백테스트를 돌려
    같은 input_hash 로 중복 등록이 나거나, 더 나쁘게는 결과가 엇갈린다.
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(
            """
            with picked as (
              select job_id from quant.experiment_jobs
               where status = 'QUEUED'
               order by created_at
               limit %s
               for update skip locked
            )
            update quant.experiment_jobs j
               set status='LEASED', leased_at=now(), leased_by=%s,
                   attempts=j.attempts+1, updated_at=now()
              from picked p
             where j.job_id = p.job_id
             returning j.job_id, j.hypothesis_id, j.attempts
            """, (batch, worker))
        for jid, hid, att in cur.fetchall():
            out.append({"job_id": str(jid), "hypothesis_id": str(hid),
                        "attempts": att})
    conn.commit()
    return out


def finish(conn, job_id: str, *, ok: bool, experiment_id: str | None = None,
           reason: str | None = None) -> None:
    """작업 종료. **실패는 사유가 있어야 한다**(DB 제약이 강제한다)."""
    if not ok and not reason:
        raise ValueError("실패에는 사유가 필요하다 - 사유 없는 실패는 "
                         "다시 시도할지 폐기할지 판단할 수 없다")
    with conn.cursor() as cur:
        cur.execute(
            """update quant.experiment_jobs
                  set status=%s, experiment_id=%s::uuid, failure_reason=%s,
                      updated_at=now(), leased_at=null, leased_by=null
                where job_id=%s::uuid""",
            ("DONE" if ok else "FAILED", experiment_id,
             None if ok else reason[:500], job_id))
    conn.commit()


def reclaim(conn, *, now: datetime | None = None) -> dict:
    """만료된 lease 회수. 워커가 죽어도 주문이 영원히 묶이지 않게 한다."""
    n = now or datetime.now(timezone.utc)
    requeued = failed = 0
    with conn.cursor() as cur:
        cur.execute(
            """select job_id, hypothesis_id, status, attempts, max_attempts,
                      leased_at, failure_reason
               from quant.experiment_jobs where status='LEASED'
               for update skip locked""")
        rows = cur.fetchall()
        for jid, hid, st, att, mx, la, fr in rows:
            job = Job(str(jid), str(hid), st, att, mx, la, fr)
            d = reclaim_decision(job, now=n)
            if d["action"] == "REQUEUE":
                cur.execute(
                    """update quant.experiment_jobs
                          set status='QUEUED', leased_at=null, leased_by=null,
                              updated_at=now()
                        where job_id=%s""", (jid,))
                requeued += 1
            elif d["action"] == "FAIL":
                cur.execute(
                    """update quant.experiment_jobs
                          set status='FAILED', failure_reason=%s,
                              leased_at=null, leased_by=null, updated_at=now()
                        where job_id=%s""", (d["reason"][:500], jid))
                failed += 1
    conn.commit()
    return {"checked": len(rows), "requeued": requeued, "failed": failed}


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


def _job(**kw) -> Job:
    d = {"job_id": "j1", "hypothesis_id": "h1", "status": "LEASED",
         "attempts": 1, "max_attempts": 2,
         "leased_at": _NOW - timedelta(minutes=5)}
    d.update(kw)
    return Job(**d)


def _check_fresh_lease_is_kept():
    """정상 실행을 뺏지 않는다 - 백테스트는 몇 분 걸린다."""
    d = reclaim_decision(_job(), now=_NOW)
    assert d["action"] == "KEEP", d


def _check_expired_lease_requeues():
    d = reclaim_decision(_job(leased_at=_NOW - timedelta(minutes=45)), now=_NOW)
    assert d["action"] == "REQUEUE" and "무응답" in d["reason"], d


def _check_exhausted_fails_not_loops():
    """**무한 재시도를 막는다** - 영원히 되살아나는 작업은 워커를 계속 죽인다."""
    d = reclaim_decision(
        _job(leased_at=_NOW - timedelta(hours=3), attempts=2), now=_NOW)
    assert d["action"] == "FAIL" and "소진" in d["reason"], d
    assert "사람이 본다" in d["reason"], d


def _check_missing_lease_time_is_not_expired():
    """시각을 모르는데 만료로 단정하면 정상 실행을 뺏는다."""
    assert is_lease_expired(_job(leased_at=None), now=_NOW) is False
    d = reclaim_decision(_job(leased_at=None), now=_NOW)
    assert d["action"] == "KEEP", d


def _check_only_leased_expires():
    for st in ("QUEUED", "DONE", "FAILED", "CANCELLED"):
        assert not is_lease_expired(
            _job(status=st, leased_at=_NOW - timedelta(days=9)), now=_NOW), st


def _check_finish_requires_reason():
    """실패에 사유가 없으면 다시 시도할지 폐기할지 판단할 수 없다."""
    class _C:
        def cursor(self):
            raise AssertionError("사유 검사 전에 DB 를 건드리면 안 된다")

    try:
        finish(_C(), "j1", ok=False)
    except ValueError as e:
        assert "사유" in str(e), e
    else:
        raise AssertionError("사유 없는 실패가 통과했다")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_fresh_lease_is_kept();            print("  정상 실행 유지          OK")
    _check_expired_lease_requeues();         print("  만료 -> 재대기          OK")
    _check_exhausted_fails_not_loops();      print("  소진 -> 실패(무한X)     OK")
    _check_missing_lease_time_is_not_expired(); print("  시각 미상 != 만료      OK")
    _check_only_leased_expires();            print("  LEASED 만 만료          OK")
    _check_finish_requires_reason();         print("  실패엔 사유 필수        OK")
    print("작업 큐 6개 영역 통과.")
