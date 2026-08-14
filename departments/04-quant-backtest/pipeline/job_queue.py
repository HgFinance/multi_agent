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

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MODULE_VERSION = "quant-job-queue-v1"

# 워커가 집어간 뒤 이 시간을 넘기면 죽은 것으로 보고 회수한다.
# 백테스트가 실측 3~5분이므로 30분이면 정상 실행을 뺏지 않는다.
LEASE_TIMEOUT_MIN = 30

# 한 번에 집어가는 작업 수. 기본 1 - 워커가 죽으면 잡고 있던 것이 전부 회수
# 대기가 되므로 많이 쥘수록 지연이 커진다.
#
# ▶ **상수로 박지 않는다** (2026-08-12, 재일 지시)
#   병렬도는 상황에 따라 다르다. 백테스트가 v2 에서 2.5초, v3 에서 110초였다
#   (33배 데이터에 44배 시간). 큐에 32건이 밀려 있으면 직렬로 한 시간이고,
#   4병렬이면 15분이다. 그 판단은 **큐를 보는 쪽**이 해야지 코드에 박을 값이
#   아니다 - 박아 두면 상황이 바뀌어도 안 바뀌고, 그게 오늘 하루 종일 고친
#   유형이다.
#
#   그래서 환경변수로 연다. 퀀트 에이전트가 큐 적체를 보고 판단해서 올린다
#   (스킬 "병목이면 병렬도를 올린다" 참고).
#
# ▶ 올려도 안전한 이유
#   `lease` 가 `for update skip locked` 를 쓰므로 여러 워커·여러 배치가 같은
#   작업을 집지 않는다. 집는 순간 `attempts` 가 오르므로 무한 재시도도 막힌다.
#   그리고 `register_and_run` 이 `input_hash` 로 중복 실험을 거부하므로,
#   설령 같은 가설이 두 번 돌아도 원장에 두 번 남지 않는다.
BATCH = max(1, int(os.getenv("QUANT_EXPERIMENT_BATCH", "1")))


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


# 같은 가설이 **같은 사유로** 이만큼 죽었으면 다시 넣지 않는다.
# 병목 인구조사의 CHURN_ATTEMPTS 와 같은 값이다 - 큐가 발행을 멈추는 바로 그
# 지점에서 인구조사가 그것을 병목으로 집어 올려야 신호가 끊기지 않는다.
REPEAT_BLOCK = int(os.getenv("FACTORY_REPEAT_BLOCK", "3"))


@dataclass(frozen=True)
class Blocked:
    """되풀이 실패로 막힌 주문. **연령을 같이 들고 다닌다.**

    DLQ 운영 원칙이 네 가지인데(진단 맥락 보존 · 종착지로 두고 자동 재투입
    금지 · 연령과 부피 감시 · 결함을 고친 뒤 의도적 재생), 우리는 셋째를
    안 하고 있었다. "며칠째 막혀 있나" 를 세지 않으면 8일짜리가 조용히 생긴다.
    """

    reason: str
    count: int
    days: int = 0

    def __iter__(self):          # 옛 호출부가 (사유, 횟수) 로 풀던 것을 지킨다
        return iter((self.reason, self.count))


def repeat_block(cur, hypothesis_id: str) -> Blocked | None:
    """같은 사유로 되풀이해 죽은 주문. 있으면 (사유, 횟수), 없으면 None.

    ▶ 왜 사유를 안 가리나 (2026-08-12 실측)
      가설 `3bb50969` 하나에 **4시간 동안 job 25개**가 발행됐다. 전부 같은
      사유(`'short_term_reversal' 는 어휘에 없다`)로 죽었다. `on conflict` 는
      QUEUED/LEASED 만 덮으므로 FAILED 가 되는 순간 같은 주문이 다시 들어간다.

      "구조적 실패만 막자"고 사유를 분류하려 했지만 그건 틀린 길이다 -
      `NOT_RUNNABLE` 안에 어휘 부재(영구)와 시장 DB 연결 없음(일시)이 같이
      있다. 대신 **같은 사유가 N번 되풀이됐는가**만 본다. 일시적 실패는
      사유가 바뀌거나 언젠가 성공하므로 여기 안 걸린다. 사유를 몰라도 맞다.

      막는 것으로 끝내지 않는다 - 반환값이 사유를 그대로 실어 보내므로
      부르는 쪽이 "무엇이 없어서 못 도는지"를 안다.
    """
    cur.execute(
        """select coalesce(failure_reason, ''), count(*),
                  extract(day from now() - min(updated_at))::int
             from quant.experiment_jobs
            where hypothesis_id = %s::uuid and status = 'FAILED'
              and failure_reason is not null and failure_reason <> ''
            group by 1 order by 2 desc limit 1""", (hypothesis_id,))
    row = cur.fetchone()
    if row and int(row[1]) >= REPEAT_BLOCK:
        return Blocked(str(row[0]), int(row[1]), int(row[2] or 0))
    return None


def enqueue(conn, hypothesis_id: str, *, requested_by: str,
            replay: str = "") -> dict:
    """주문 접수. **같은 가설의 활성 주문은 하나뿐이다.**

    중복 제출을 막지 않으면 같은 백테스트가 동시에 돌아 결과가 둘이 되고,
    어느 것이 진짜인지 모르게 된다(DB 부분 유니크 인덱스가 강제한다).

    되풀이해 죽은 주문도 다시 받지 않는다(`repeat_block`). 유니크 인덱스는
    QUEUED/LEASED 만 덮어서 FAILED 뒤에는 아무것도 막지 않았다.

    ▶ `replay` - **결함을 고친 뒤 의도적으로 재생하는 길** (2026-08-12)
      차단만 있고 푸는 길이 없으면, 실행면을 고쳐도 막힌 가설은 영원히 막혀
      있다. 그렇다고 자동으로 풀면 안 된다 - DLQ 운영 원칙이 "자동 재투입
      금지, 결함을 고친 뒤 통제된 배치로 재생" 이다. 자동 재개는 고쳤는지
      확인하지 않고 같은 자리에서 또 죽는다.

      그래서 **명시적 재생만** 연다. 부르는 쪽이 무엇을 고쳤는지 문자열로
      적어야 하고, 그것이 `requested_by` 에 남아 감사된다. 사유 없는 재생은
      거부한다 - 사유 없이 풀면 그냥 자동 재투입과 같다.
    """
    with conn.cursor() as cur:
        blocked = repeat_block(cur, hypothesis_id)
        if blocked and not replay:
            conn.rollback()
            aged = f" {blocked.days}일째" if blocked.days else ""
            return {"accepted": False, "blocked": True,
                    "failure_reason": blocked.reason, "attempts": blocked.count,
                    "days": blocked.days,
                    "reason": (f"같은 사유로 {blocked.count}회 죽은 주문이다"
                               f"{aged} - 같은 것을 다시 넣어도 같은 자리에서 "
                               f"죽는다. **실행면을 고친 뒤 무엇을 고쳤는지 "
                               f"적고 재생(replay)해라**: "
                               f"{blocked.reason[:150]}")}
        if replay and not str(replay).strip():
            conn.rollback()
            return {"accepted": False,
                    "reason": "재생에는 무엇을 고쳤는지가 필요하다 - 사유 없는 "
                              "재생은 자동 재투입과 같아서 같은 자리에서 또 죽는다"}
        if replay:
            requested_by = f"{requested_by} replay:{str(replay)[:120]}"
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
               -- ▶ **놓아준 주문은 잠시 쉰다** (2026-08-14 실측)
               --   놓아주기만 넣었더니 같은 주문을 15초마다 집었다 놓기를
               --   반복했다(로그 120회). 기다리는 조건(가설 회수)은 분 단위로
               --   바뀌므로 초 단위 재시도는 순수한 낭비다. 신규 주문은
               --   `failure_reason` 이 비어 있어 **즉시** 집히므로 발주가
               --   늦어지지 않는다 - 쉬는 것은 놓아준/회수된 주문뿐이다.
               and (failure_reason is null
                    or updated_at < now() - interval '2 minutes')
               -- ▶ **가장 오래 안 건드린 것부터** (2026-08-14)
               --   `created_at` 만 보면 방금 놓아준(release) 주문이 계속 맨
               --   앞을 차지한다. BATCH 기본이 1 이라 그 한 자리를 물고 있는
               --   동안 **돌 수 있는 다른 주문이 굶는다.** updated_at 을 앞에
               --   두면 놓아준 것은 뒤로 가고, 신규 주문은 updated_at =
               --   created_at 이므로 선착순이 그대로 유지된다.
               order by updated_at, created_at
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


def release(conn, job_id: str, *, reason: str) -> None:
    """**지금은 못 도는 주문을 놓아준다 - 시도 횟수는 태우지 않는다.**

    ▶ 왜 필요한가 (2026-08-14 실측)
      `orchestrate` 가 `BAD_TRANSITION`(가설이 아직 RUNNING)을 돌려줄 때
      `finish(ok=False)` 로 종결하고 있었다. 그런데 그건 주문의 잘못이 아니라
      **타이밍**이다 - 앞 시도가 남긴 RUNNING 을 회수(10분 정체 기준)가 곧
      PROPOSED 로 되돌리면 그대로 돌 주문이다.

      실측: 24시간 실패 13건 중 7건이 BAD_TRANSITION 이었고, 전부
      `attempts=2/2` 였다. 두 번의 시도가 모두 회수보다 먼저 일어나 **재시도
      예산만 태우고 영구 FAILED 로 굳은 것이다.** 그 뒤 회수가 가설을
      PROPOSED 로 되돌려 재발주되고, 같은 자리에서 또 죽는 고리가 돈다.

      그래서 실패가 아니라 놓아주기로 돌린다. `lease` 가 올린 시도 횟수를
      되돌려 예산을 보존하고, 사유는 남겨 무엇을 기다리는지 보이게 한다
      (사유 없는 되돌리기는 조용한 재시도와 같다).
    """
    if not reason:
        raise ValueError("놓아주기에도 사유가 필요하다 - 무엇을 기다리는지 "
                         "안 남기면 조용한 무한 재시도와 구별되지 않는다")
    with conn.cursor() as cur:
        cur.execute(
            """update quant.experiment_jobs
                  set status='QUEUED', failure_reason=%s, updated_at=now(),
                      leased_at=null, leased_by=null,
                      attempts=greatest(attempts - 1, 0)
                where job_id=%s::uuid and status='LEASED'""",
            (reason[:500], job_id))
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


def _check_release_keeps_the_attempt_budget():
    """**놓아주기는 시도 예산을 태우지 않는다** (2026-08-14 실측).

    타이밍(가설이 아직 RUNNING)으로 못 돈 주문을 `finish(ok=False)` 로
    종결하고 있었다 - 24시간 실패 7건이 전부 attempts=2/2 로 굳었다.
    놓아줄 때 `lease` 가 올린 시도를 되돌리지 않으면 실패로 태우는 것과
    똑같아진다. SQL 이 실제로 예산을 되돌리는지 여기서 고정한다.
    """
    seen = {}

    class _Cur:
        def execute(self, sql, params=None):
            seen["sql"], seen["params"] = " ".join(sql.split()), params
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _C:
        def cursor(self):
            return _Cur()
        def commit(self):
            seen["committed"] = True

    release(_C(), "j1", reason="verdict=BAD_TRANSITION; 가설이 아직 RUNNING")
    assert "status='QUEUED'" in seen["sql"], seen["sql"]
    assert "attempts=greatest(attempts - 1, 0)" in seen["sql"], seen["sql"]
    assert "status='LEASED'" in seen["sql"], "집지 않은 주문을 되돌리면 안 된다"
    assert seen["params"][0].startswith("verdict=BAD_TRANSITION"), seen["params"]
    assert seen.get("committed"), "놓아주고 커밋하지 않으면 다음 순회가 못 본다"

    # 사유 없는 놓아주기는 조용한 무한 재시도와 구별되지 않는다.
    class _Boom:
        def cursor(self):
            raise AssertionError("사유 검사 전에 DB 를 건드리면 안 된다")

    try:
        release(_Boom(), "j1", reason="")
    except ValueError as e:
        assert "사유" in str(e), e
    else:
        raise AssertionError("사유 없는 놓아주기가 통과했다")


def _check_lease_does_not_starve_behind_a_released_job():
    """**놓아준 주문이 큐 앞자리를 물고 있으면 안 된다** (2026-08-14).

    BATCH 기본이 1 이다. `order by created_at` 만이면 방금 놓아준(=가장 오래된)
    주문이 매 순회 그 한 자리를 다시 차지해, 돌 수 있는 다른 주문이 굶는다.
    """
    seen = {}

    class _Cur:
        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())
        def fetchall(self):
            return []
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _C:
        def cursor(self):
            return _Cur()
        def commit(self):
            pass

    lease(_C(), worker="w:1")
    assert "order by updated_at, created_at" in seen["sql"], seen["sql"]
    # 놓아준 주문은 쉬게 하되, **신규 주문은 즉시** 집어야 발주가 안 늦는다.
    assert "failure_reason is null" in seen["sql"], seen["sql"]
    assert "interval '2 minutes'" in seen["sql"], seen["sql"]


def _check_repeat_block_stops_reissue():
    """**같은 사유로 3번 죽었으면 4번째 주문을 안 받는다.** (2026-08-12 실측)

    가설 3bb50969 하나에 4시간 동안 job 25개가 발행됐다. 전부 같은 사유였다.
    유니크 인덱스는 QUEUED/LEASED 만 덮어 FAILED 뒤를 안 막았다.
    """
    class _Cur:
        def __init__(self, rows):
            self.rows, self.inserted, self.by = rows, False, None

        def execute(self, sql, *a):
            if "insert" in sql.lower():
                self.inserted = True
                self.by = a[0][1] if a and len(a[0]) > 1 else None
            self._sql = sql

        def fetchone(self):
            if "group by" in self._sql:
                return self.rows
            return ("job-1", "QUEUED") if self.inserted else None

        def __enter__(self):  return self
        def __exit__(self, *a):  return False

    class _Conn:
        def __init__(self, cur):  self._c = cur
        def cursor(self):  return self._c
        def commit(self):  pass
        def rollback(self):  pass

    HID = "3bb50969-0000-0000-0000-000000000000"
    cur = _Cur(("'short_term_reversal' 는 어휘에 없다", 25, 8))
    r = enqueue(_Conn(cur), HID, requested_by="t")
    assert r["accepted"] is False and r.get("blocked") is True, r
    assert not cur.inserted, "막았다면서 insert 를 했다"
    # 사유를 실어 보내야 부르는 쪽이 무엇을 고쳐야 하는지 안다
    assert "short_term_reversal" in r["reason"], r["reason"]
    # 연령을 세지 않으면 8일짜리가 조용히 생긴다
    assert r["days"] == 8 and "8일째" in r["reason"], r

    # 2회는 아직 막지 않는다 - 일시적 실패를 영구 차단으로 몰면 안 된다
    cur2 = _Cur(("일시적인 무언가", REPEAT_BLOCK - 1, 0))
    assert repeat_block(cur2, "x") is None, "임계 미만인데 막았다"
    # 사유가 없는 실패는 세지 않는다(무엇이 되풀이됐는지 모른다)
    cur3 = _Cur(None)
    assert repeat_block(cur3, "x") is None


def _check_replay_is_deliberate_not_automatic():
    """**고친 뒤 재생하는 길은 있되, 자동 재투입은 없다.** (2026-08-12)

    차단만 있고 푸는 길이 없으면 실행면을 고쳐도 막힌 가설이 영원히 막혀
    있다. 그렇다고 자동으로 풀면 고쳤는지 확인하지 않고 같은 자리에서 또
    죽는다 - DLQ 운영 원칙이 "자동 재투입 금지, 결함을 고친 뒤 의도적 재생".
    그래서 **무엇을 고쳤는지 적어야만** 열린다.
    """
    class _Cur:
        def __init__(self):
            self.inserted, self.by = False, None

        def execute(self, sql, *a):
            if "insert" in sql.lower():
                self.inserted = True
                self.by = a[0][1]
            self._sql = sql

        def fetchone(self):
            if "group by" in self._sql:
                return ("'x' 는 어휘에 없다", 25, 8)
            return ("job-2", "QUEUED") if self.inserted else None

        def __enter__(self):  return self
        def __exit__(self, *a):  return False

    class _Conn:
        def __init__(self, cur):  self._c = cur
        def cursor(self):  return self._c
        def commit(self):  pass
        def rollback(self):  pass

    HID = "3bb50969-0000-0000-0000-000000000000"
    cur = _Cur()
    r = enqueue(_Conn(cur), HID, requested_by="quant",
                replay="short_term_reversal 템플릿을 추가했다")
    assert r["accepted"] is True, r
    # 무엇을 고쳐서 풀었는지가 원장에 남아야 감사된다
    assert "replay:" in cur.by and "템플릿을 추가" in cur.by, cur.by

    # 빈 사유로는 못 푼다 - 그건 그냥 자동 재투입이다
    cur2 = _Cur()
    r2 = enqueue(_Conn(cur2), HID, requested_by="quant", replay="   ")
    assert r2["accepted"] is False and not cur2.inserted, r2
    assert "무엇을 고쳤는지" in r2["reason"], r2["reason"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_repeat_block_stops_reissue();     print("  되풀이 실패 재발행 차단  OK")
    _check_replay_is_deliberate_not_automatic(); print("  고친 뒤 의도적 재생만   OK")
    _check_fresh_lease_is_kept();            print("  정상 실행 유지          OK")
    _check_expired_lease_requeues();         print("  만료 -> 재대기          OK")
    _check_exhausted_fails_not_loops();      print("  소진 -> 실패(무한X)     OK")
    _check_missing_lease_time_is_not_expired(); print("  시각 미상 != 만료      OK")
    _check_only_leased_expires();            print("  LEASED 만 만료          OK")
    _check_finish_requires_reason();         print("  실패엔 사유 필수        OK")
    _check_release_keeps_the_attempt_budget(); print("  놓아주기=예산 보존      OK")
    _check_lease_does_not_starve_behind_a_released_job()
    print("  놓아준 주문에 안 굶음   OK")
    print("작업 큐 10개 영역 통과.")
