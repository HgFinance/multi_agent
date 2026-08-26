#!/usr/bin/env python3
"""시세 전용 배치 스케줄러 - Docker Container entrypoint.

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "다른 수집기들도 띄우자 / 계속 부족한 부분 개선"

가격·시장상태·파생시장 관측과 그 직접 파생물만 실행한다. 뉴스·공시·재무·거시·
소셜 수집기는 폐기했으며, 그 정보는 에이전트가 필요할 때 MCP로 조회한다.

▶ 지킨 것
  - **순차 실행.** LS 호출 한도와 장중 부하를 넘지 않는다.
  - **재시작 후 재실행은 안전하다.** 상태를 메모리에만 두므로 컨테이너 재시작이
    일일 Job 을 다시 돌릴 수 있는데, 모든 수집기가 멱등 적재라 중복이 생기지 않는다
    (그래서 상태 파일을 만들지 않았다 - 지금 볼륨 하나 늘리는 것보다 싸다).
  - **종료 코드 2 는 SKIP 이다.** market_breadth 가 휴장·수집 불가 국면에서 2 를
    돌려준다("수집하지 않았다"). 실패(1)와 섞으면 휴장일마다 거짓 경보가 쌓인다.
  - **연속 실패를 드러낸다.** Job 별 연속 실패 횟수를 세고 3회부터 요약에 ⚠ 를
    붙인다. 실패해도 스케줄러는 계속 돈다 - 한 Source 장애가 나머지를 멈추지 않는다.

실시간 호가·체결은 별도 ``ls-realtime`` 서비스가 맡는다.

실행
  python collectors/collector_scheduler.py            # 자체 점검 (실행 없음)
  python collectors/collector_scheduler.py --check    # 같음 (명시형)
  python collectors/collector_scheduler.py --serve    # **상주 실행** (compose 가 쓴다)
  python collectors/collector_scheduler.py --once 이름  # 한 Job 즉시 1회 (수동 점검용)

  인자 없는 실행이 상주가 아닌 이유: 이 저장소는 "python <파일> = 자체 점검"이
  관례다. 이 파일만 예외라서, 전 모듈을 훑는 점검 루프가 돌 때마다 상주
  스케줄러가 조용히 떴다(2026-08-02까지 4회). 관례를 어기는 쪽이 플래그를
  달게 했다.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SCHEDULER_VERSION = "market-data-scheduler-v2"
KST = timezone(timedelta(hours=9))

CHECK_EVERY_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 30 * 60
FAILURE_ALERT_THRESHOLD = 3
EXIT_SKIP = 2  # 수집기 규약: "의도된 미수집" (market_breadth 실측)
TIMEOUT_EXIT = -1  # 타임아웃은 종료 코드를 못 받는다 - DB 기록용 표식


@dataclass(frozen=True)
class Job:
    """스케줄 한 줄. every 와 daily_at 중 하나만 갖는다."""

    name: str
    argv: tuple[str, ...]
    every_minutes: int | None = None
    window: tuple[time, time] | None = None  # 주기형의 활동 창 (KST)
    daily_at: time | None = None             # 일일형의 실행 시각 (KST)
    # ▶ Job 별 제한 시간. 기본 30분은 짧은 Job 을 지키는 값이라, 유니버스
    #   전량처럼 본래 몇 시간 걸리는 Job 에 그대로 걸면 **정상 동작이
    #   타임아웃으로 기록된다.** 예산은 Job 이 하는 일의 크기에서 나온다.
    timeout_seconds: float | None = None

    def __post_init__(self):
        if (self.every_minutes is None) == (self.daily_at is None):
            raise ValueError(f"{self.name}: every_minutes 와 daily_at 중 하나만 지정한다")
        if self.every_minutes is not None and self.window is None:
            raise ValueError(f"{self.name}: 주기형은 활동 창이 필수다 - 무제한 폴링을 막는다")

    @property
    def timeout(self) -> float:
        return self.timeout_seconds or JOB_TIMEOUT_SECONDS


# 시각은 전부 KST.
JOBS: tuple[Job, ...] = (
    # 검증된 전일 raw 호가·체결을 Parquet로 고정한다.
    Job("market-archive", ("collectors/market_archive_exporter.py", "--export"),
        daily_at=time(6, 50)),
    # 거래정지·관리·정리매매 등 브로커 시장상태. 뉴스/공시가 아니라 당일
    # 거래가능 유니버스를 fail-closed 하기 위한 market reference다.
    Job("universe-restrictions",
        ("collectors/universe_restriction_collector.py", "--collect"),
        daily_at=time(7, 5)),
    # 시세 평면 심박·품질 감사 - 개장 전.
    Job("data-steward", ("collectors/market_data_steward.py", "--audit"),
        daily_at=time(7, 10)),
    # Archive export 검증이 끝난 후에만 정책 기간을 넘은 raw 청크를
    # 내린다. 이 Job은 수집을 늘리지 않고, 검증 커버리지가 비면 무조건 보류한다.
    Job("retention", ("collectors/retention_enforcer.py", "--enforce"),
        daily_at=time(7, 20)),
    # 시장 Breadth - 세션 판정은 수집기 자신이 한다 (휴장이면 exit 2 = SKIP)
    Job("breadth", ("collectors/market_breadth_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 30), time(16, 10))),
    # KOSPI200 파생 스냅샷 - 파생 세션(주식 ±15분)은 수집기가 판정, 밖이면 SKIP.
    # 창 상한 17:00: 수능일 파생 마감 16:45 까지 덮는다. 호출 4회/실행이라 가볍다.
    Job("derivatives", ("collectors/derivatives_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 40), time(17, 0))),
    # VKOSPI 종가 - 정규장 마감(15:45) 뒤. 휴장이면 수집기가 SKIP(exit 2) 한다
    # (t1511 은 휴장에도 직전 종가를 주므로 그대로 넣으면 없는 관측이 생긴다).
    Job("vkospi", ("collectors/volatility_index_collector.py", "--collect"),
        daily_at=time(16, 5)),
    # 스타일·안전자산 지수 6계열 - VKOSPI 와 같은 t1511 경로, 같은 휴장 규칙.
    # 1분 뒤에 둬서 두 수집기가 같은 초에 LS 를 두드리지 않게 한다.
    Job("style-index", ("collectors/style_index_collector.py", "--collect"),
        daily_at=time(16, 6)),
    # VKOSPI/style이 오늘 거래일을 fail-closed 판정하기 전에 갱신한다.
    Job("calendar-observed", ("collectors/calendar_collector.py", "--collect"),
        daily_at=time(16, 0)),
    # 일별 시장 라벨 스냅샷. 가격·breadth에서 계산한 레짐만 기록한다.
    Job("label-snapshot", ("collectors/label_snapshot_collector.py", "--collect"),
        daily_at=time(16, 30)),
    # 전종목 확정 일봉. 장중 LS 호출과 경합하지 않도록 21:00에 실행하며 최근
    # 3일을 겹쳐 받아 장애·휴일 구간을 멱등하게 복구한다. 긴 작업이라 마지막.
    Job("chart-daily-universe",
        ("collectors/chart_backfill_collector.py",
         "--daily", "--universe", "--recent-days", "3"),
        daily_at=time(21, 0), timeout_seconds=3 * 60 * 60),
)

# Production collection is deliberately limited to exchange-derived market
# observations and deterministic derivatives of those observations. News,
# filings, financial statements, macro series, corporate actions, social
# feeds, and capability crawling are obtained on demand through MCP and must
# never be reintroduced into this always-on scheduler by accident.
MARKET_DATA_JOB_NAMES = frozenset(
    {
        "breadth",
        "derivatives",
        "universe-restrictions",
        "chart-daily-universe",
        "label-snapshot",
        "vkospi",
        "style-index",
        "calendar-observed",
        "data-steward",
        "market-archive",
        "retention",
    }
)
FORBIDDEN_ALWAYS_ON_COLLECTOR_PATHS = frozenset(
    {
        "collectors/opendart_collector.py",
        "collectors/opendart_financial.py",
        "collectors/opendart_cashflow.py",
        "collectors/opendart_document_collector.py",
        "collectors/opendart_company_collector.py",
        "collectors/corporate_action_collector.py",
        "collectors/macro_collector.py",
        "collectors/geopolitical_collector.py",
        "collectors/bluesky_watch_collector.py",
        "collectors/research_data_steward.py",
        "collectors/capability_audit.py",
    }
)


# ▶ **실패한 일일 Job 은 그날 안에 다시 띄운다** (2026-08-11 실측)
#   예전에는 성패와 무관하게 `last_finished_date` 를 오늘로 찍었다. 그래서 일일
#   Job 은 실패해도 '오늘 완료' 가 되어 그날 다시 뜨지 않았다 - 한 번 미끄러지면
#   그날 데이터에 **구멍이 영구히** 남는다. 실측: TimescaleDB 재시작 중
#   `the database system is starting up` 으로 21:00 chart-daily-universe 가 죽었고,
#   유니버스 일봉이 3,924종목에서 350종목(관심종목 전용 Job 몫)으로 남았다.
#   로그에는 재시도처럼 네 번 찍혔지만 그건 재시도가 아니라 **스케줄러가 네 번
#   재시작한 것**이었다 - 재시작에 기대는 복구는 복구가 아니다.
#
#   무한 재시도는 외부 API 를 두드리므로 간격을 늘려 가며 센다. 시도를 다 쓰면
#   내일로 미루되 **'완료' 로 찍지는 않는다** - 대장에 미완으로 남아야 이력
#   복원(seed_states_from_history)이 이 Job 을 건너뛰지 않는다.
RETRY_BACKOFF_MINUTES = (10, 30, 60, 120)


@dataclass
class JobState:
    last_started: datetime | None = None
    last_finished_date: date | None = None  # 일일형 중복 방지 (OK/SKIP 만)
    consecutive_failures: int = 0
    runs: int = 0
    skips: int = 0
    retry_after: datetime | None = None     # 실패 후 이 시각까지는 다시 안 뜬다
    attempts_date: date | None = None       # 재시도 계수는 날마다 초기화한다
    attempts_today: int = 0


def retry_at(job: Job, attempts_today: int, now: datetime) -> datetime:
    """실패한 일일 Job 을 언제 다시 띄울지."""
    if attempts_today <= len(RETRY_BACKOFF_MINUTES):
        return now + timedelta(minutes=RETRY_BACKOFF_MINUTES[attempts_today - 1])
    return datetime.combine(now.date() + timedelta(days=1), job.daily_at, tzinfo=KST)


def schedule_retry(job: Job, state: JobState, now: datetime) -> str:
    """실패한 Job 의 다음 시도 시각을 정한다. 로그에 붙일 문구를 돌려준다.

    주기형은 손대지 않는다 - 원래 주기가 곧 재시도다.
    """
    if job.daily_at is None:
        return ""
    if state.attempts_date != now.date():
        state.attempts_date, state.attempts_today = now.date(), 0
    state.attempts_today += 1
    state.retry_after = retry_at(job, state.attempts_today, now)
    # 소진 여부는 **시도 횟수로** 판정한다 - 날짜로 보면 자정을 넘긴 backoff 를
    # 소진으로 착각한다(늦은 Job 의 마지막 backoff 는 다음 날로 넘어간다).
    if state.attempts_today > len(RETRY_BACKOFF_MINUTES):
        return " - 오늘 시도 소진, 내일 다시(완료로 찍지 않는다)"
    return f" - {state.retry_after:%m-%d %H:%M} 재시도"


def is_due(job: Job, state: JobState, now: datetime) -> bool:
    if job.daily_at is not None:
        if now.timetz().replace(tzinfo=None) < job.daily_at:
            return False
        if state.last_finished_date == now.date():
            return False
        return state.retry_after is None or now >= state.retry_after
    lo, hi = job.window
    t = now.timetz().replace(tzinfo=None)
    if not (lo <= t <= hi):
        return False
    if state.last_started is None:
        return True
    return (now - state.last_started) >= timedelta(minutes=job.every_minutes)


def run_job(job: Job, *, timeout: float | None = None) -> tuple[int, str]:
    """subprocess 로 한 번 실행. (종료 코드, 출력 꼬리)."""
    proc = subprocess.run(
        [sys.executable, *job.argv],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout if timeout is not None else job.timeout,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    if proc.returncode not in (0, EXIT_SKIP) and proc.stderr:
        tail += "\nstderr: " + (proc.stderr or "").strip()[-400:]
    return proc.returncode, tail


def classify(exit_code: int) -> str:
    """종료 코드 -> 상태. SKIP 은 실패가 아니다(휴장 등 의도된 미수집)."""
    if exit_code == 0:
        return "OK"
    if exit_code == EXIT_SKIP:
        return "SKIP"
    if exit_code == TIMEOUT_EXIT:
        return "TIMEOUT"
    return "FAILED"


def record_run(job_name: str, argv: tuple[str, ...], *, started, ended,
               exit_code: int, tail: str, consecutive_failures: int,
               conn=None) -> bool:
    """research.collector_runs 기록.

    **기록 실패가 수집을 죽이면 안 된다** - 조용한 실패를 막으려고 만든 장치가
    새로운 실패 원인이 되면 본말전도다. 실패는 stderr 로만 알린다.
    """
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.collector_runs
                  (job_name, argv, started_at, ended_at, duration_ms, exit_code,
                   status, consecutive_failures, output_tail, scheduler_version)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (job_name, " ".join(argv), started, ended,
                 int((ended - started).total_seconds() * 1000), exit_code,
                 classify(exit_code), consecutive_failures,
                 (tail or "")[:4000], SCHEDULER_VERSION))
        conn.commit()
        if own:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ collector_runs 기록 실패({job_name}): {type(e).__name__}: "
              f"{str(e)[:120]}", file=sys.stderr, flush=True)
        return False


def seed_states_from_history(states: dict[str, JobState], *, conn=None,
                             today: date | None = None) -> int:
    """재시작 뒤 **오늘 이미 끝낸 일일 Job 을 다시 돌리지 않게** 원장에서 복원한다.

    ▶ 왜 필요한가 (2026-08-11)
      JobState 는 메모리에만 있었다. 컨테이너를 한 번 올리면 그 시각 이전의
      daily_at Job 이 **전부 다시 돈다** - is_due 가 `last_finished_date !=
      오늘` 로 판정하는데, 갓 만든 상태는 None 이라 언제나 참이다.
      저녁에 재시작하면 그날 일일 Job 열몇 개가 한꺼번에 재실행되고,
      DART·GDELT·LS 를 다시 두드린다(GDELT 는 실제로 429 를 준다).
      멱등이라 데이터는 안 깨지지만, **재시작이 비싸지면 고치는 것을 미루게
      된다.** 배포를 겁내게 만드는 것 자체가 결함이다.

      복원 실패는 수집을 죽이지 않는다 - 못 읽었으면 예전처럼 다 도는 것뿐이라
      안전한 쪽으로 떨어진다. 다만 조용히 넘기지 않고 stderr 로 알린다.

    OK/SKIP 만 '끝냈다'로 친다. 실패한 Job 은 재시작이 곧 재시도여야 한다.
    """
    today = today or datetime.now(KST).date()
    # 주기형은 복원하지 않는다 - is_due 가 last_started 로 판정하고, 재시작
    # 직후 한 번 더 도는 것이 오히려 맞다(10분 폴링은 겹쳐도 싸다).
    daily = [j.name for j in JOBS if j.daily_at is not None and j.name in states]
    if not daily:
        return 0
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                select job_name, max((ended_at at time zone 'Asia/Seoul')::date)
                  from research.collector_runs
                 where job_name = any(%s)
                   and status in ('OK','SKIP')
                   and ended_at >= now() - interval '48 hours'
                 group by job_name
                """,
                (daily,))
            rows = cur.fetchall()
        if own:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 실행 이력 복원 실패 - 오늘 일일 Job 이 다시 돈다: "
              f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr, flush=True)
        return 0
    seeded = 0
    for name, last in rows:
        if last == today and name in states:
            states[name].last_finished_date = last
            seeded += 1
    return seeded


def main() -> int:
    stop = threading.Event()

    def _handle(signum, _frame):
        print(f"신호 {signum} 수신 - 진행 중인 Job 을 마치고 종료한다", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    states: dict[str, JobState] = {j.name: JobState() for j in JOBS}
    seeded = seed_states_from_history(states)
    print(f"{SCHEDULER_VERSION}: Job {len(JOBS)}개"
          f" (오늘 이미 끝난 일일 Job {seeded}개는 건너뛴다)", flush=True)
    for j in JOBS:
        when = (f"{j.every_minutes}분마다 {j.window[0]:%H:%M}~{j.window[1]:%H:%M}"
                if j.every_minutes else f"매일 {j.daily_at:%H:%M}")
        done = " [오늘 완료]" if states[j.name].last_finished_date else ""
        print(f"  {j.name:<21} {when}{done}  <- {' '.join(j.argv)}", flush=True)

    while not stop.is_set():
        now = datetime.now(KST)
        for job in JOBS:
            if stop.is_set():
                break
            st = states[job.name]
            if not is_due(job, st, now):
                continue
            st.last_started = datetime.now(KST)
            try:
                code, tail = run_job(job)
            except subprocess.TimeoutExpired:
                st.consecutive_failures += 1
                nxt = schedule_retry(job, st, datetime.now(KST))
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: ⚠ TIMEOUT "
                      f"({job.timeout / 60:.0f}분, 연속 {st.consecutive_failures})"
                      f"{nxt}", flush=True)
                # 타임아웃도 기록한다 - 로그에만 남기면 재시작과 함께 사라진다
                record_run(job.name, job.argv, started=st.last_started,
                           ended=datetime.now(KST), exit_code=TIMEOUT_EXIT,
                           tail=f"{job.timeout / 60:.0f}분 초과",
                           consecutive_failures=st.consecutive_failures)
                continue
            st.runs += 1
            # ▶ '끝냈다' 는 **OK/SKIP 일 때만** 찍는다. 실패한 일일 Job 을 완료로
            #   찍으면 그날 다시 뜨지 않아 구멍이 남는다(위 RETRY_BACKOFF_MINUTES).
            # 상태 갱신 뒤 기록해야 consecutive_failures 가 맞다 - 아래 분기에서
            # 0 으로 초기화되거나 증가한 값을 그대로 남긴다.
            if code == 0:
                st.consecutive_failures = 0
                st.last_finished_date = datetime.now(KST).date()
                st.retry_after = None
                last = tail.splitlines()[-1] if tail else ""
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: OK  {last}", flush=True)
            elif code == EXIT_SKIP:
                st.consecutive_failures = 0
                st.last_finished_date = datetime.now(KST).date()
                st.retry_after = None
                st.skips += 1
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: SKIP (수집기 판단)", flush=True)
            else:
                st.consecutive_failures += 1
                mark = " ⚠" if st.consecutive_failures >= FAILURE_ALERT_THRESHOLD else ""
                nxt = schedule_retry(job, st, datetime.now(KST))
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: 실패 exit={code} "
                      f"(연속 {st.consecutive_failures}){mark}{nxt}\n{tail}", flush=True)
            record_run(job.name, job.argv, started=st.last_started,
                       ended=datetime.now(KST), exit_code=code, tail=tail,
                       consecutive_failures=st.consecutive_failures)
        stop.wait(CHECK_EVERY_SECONDS)

    print("종료: " + ", ".join(
        f"{n}={s.runs}회(skip {s.skips})" for n, s in states.items() if s.runs
    ), flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 실행 없이
# ---------------------------------------------------------------------------

def _check_job_table():
    names = [j.name for j in JOBS]
    assert set(names) == MARKET_DATA_JOB_NAMES, (
        "always-on scheduler must contain only the reviewed market-data jobs: "
        f"unexpected={sorted(set(names) - MARKET_DATA_JOB_NAMES)}, "
        f"missing={sorted(MARKET_DATA_JOB_NAMES - set(names))}"
    )
    assert len(names) == len(set(names)), "Job 이름이 겹친다"
    for j in JOBS:
        assert j.argv[0] not in FORBIDDEN_ALWAYS_ON_COLLECTOR_PATHS, (
            f"{j.name}: non-market collector cannot run continuously: {j.argv[0]}"
        )
        assert Path(__file__).resolve().parent.parent.joinpath(j.argv[0]).exists(), \
            f"{j.name}: {j.argv[0]} 이 없다"
        # 실행 플래그가 없으면 수집기 규약상 자체점검만 돌게 된다.
        # --collect(수집) / --audit(Steward 감사) / --export(Archive) /
        # --score(Packet 사후 채점) 가 실행 동사다.
        assert {"--collect", "--audit", "--export", "--score",
                "--daily", "--minute", "--enforce"} & set(j.argv), \
            f"{j.name}: 실행 플래그가 없다 - 자체점검만 돈다"
    # ▶ **아카이브가 보존 집행보다 앞서야 한다** (2026-08-12)
    #   순서가 뒤집히면 그날 것이 아직 verified 가 아니라, 집행기가 전부
    #   보류로 넘긴다. 막히긴 하지만 매일 헛돌고 디스크는 안 준다.
    order = {j.name: i for i, j in enumerate(JOBS)}
    if "retention" in order and "market-archive" in order:
        assert order["market-archive"] < order["retention"], \
            "보존 집행이 아카이브보다 먼저다 - 아카이브 안 된 날을 지우려 한다"
    try:
        Job("bad", ("x.py", "--collect"))
        raise AssertionError("every/daily 둘 다 없는 Job 이 통과했다")
    except ValueError:
        pass
    try:
        Job("bad2", ("x.py", "--collect"), every_minutes=10)
        raise AssertionError("활동 창 없는 주기형이 통과했다")
    except ValueError:
        pass
    print("  Job 목록 무결성          OK")


def _check_due_logic():
    kst = lambda h, m=0: datetime(2026, 7, 31, h, m, tzinfo=KST)
    periodic = Job("p", ("x.py", "--collect"), every_minutes=10,
                   window=(time(9, 0), time(15, 0)))
    st = JobState()
    assert not is_due(periodic, st, kst(8, 59)), "창 밖에서 돌았다"
    assert is_due(periodic, st, kst(9, 0))
    st.last_started = kst(9, 0)
    assert not is_due(periodic, st, kst(9, 5)), "주기 전에 다시 돌았다"
    assert is_due(periodic, st, kst(9, 10))
    assert not is_due(periodic, st, kst(15, 1)), "창이 닫혔는데 돌았다"

    daily = Job("d", ("x.py", "--collect"), daily_at=time(18, 0))
    st2 = JobState()
    assert not is_due(daily, st2, kst(17, 59))
    assert is_due(daily, st2, kst(18, 0))
    st2.last_finished_date = date(2026, 7, 31)
    assert not is_due(daily, st2, kst(23, 0)), "같은 날 두 번 돌았다"
    # 빈 상태는 다시 due 다. 이건 is_due 의 결함이 아니라 **상태를 안 준
    # 쪽의 문제**라, 재시작 경로는 seed_states_from_history 로 메운다
    # (아래 _check_seed_skips_finished_daily_jobs).
    st3 = JobState()
    assert is_due(daily, st3, kst(23, 0))
    print("  due 판정                 OK")


def _check_failed_daily_job_retries_same_day():
    """**실패한 일일 Job 이 그날 안에 다시 뜨는가.**

    2026-08-11 에 이게 없어서 유니버스 일봉이 하루 통째로 비었다. TimescaleDB
    재시작 중 21:00 Job 이 죽었는데 성패와 무관하게 '오늘 완료' 로 찍혀 그날 다시
    뜨지 않았다. 로그의 네 번은 재시도가 아니라 스케줄러 재시작이었다.
    """
    kst = lambda h, m=0: datetime(2026, 8, 11, h, m, tzinfo=KST)
    daily = Job("d", ("x.py", "--collect"), daily_at=time(9, 0))
    st = JobState()
    assert is_due(daily, st, kst(9, 0))

    schedule_retry(daily, st, kst(9, 0))
    assert st.last_finished_date is None, "실패를 '완료'로 찍었다 - 구멍이 영구히 남는다"
    assert not is_due(daily, st, kst(9, 5)), "backoff 없이 즉시 다시 떴다 - API 를 두드린다"
    assert is_due(daily, st, kst(9, 10)), "10분 뒤 재시도가 안 뜬다"

    # 간격은 넓어진다 - 실패가 이어져도 10분마다 두드리지 않는다
    schedule_retry(daily, st, kst(9, 10))
    assert not is_due(daily, st, kst(9, 30))
    assert is_due(daily, st, kst(9, 40))

    for h, m in ((9, 40), (10, 40)):
        schedule_retry(daily, st, kst(h, m))
    tail = schedule_retry(daily, st, kst(12, 40))       # 5회째 - 소진
    assert "소진" in tail, tail
    assert st.last_finished_date is None, "시도 소진을 '완료'로 위장했다"
    assert not is_due(daily, st, kst(23, 59)), "소진 후에도 계속 떴다"
    assert st.retry_after.date() == date(2026, 8, 12)

    # 계수는 날마다 초기화된다 - 어제 소진했다고 오늘까지 막히면 안 된다
    schedule_retry(daily, st, datetime(2026, 8, 12, 9, 0, tzinfo=KST))
    assert st.attempts_today == 1, "재시도 계수가 날을 넘겨 누적됐다"

    # 주기형은 손대지 않는다 - 원래 주기가 곧 재시도다
    periodic = Job("p", ("x.py",), every_minutes=10, window=(time(9, 0), time(15, 0)))
    stp = JobState()
    assert schedule_retry(periodic, stp, kst(9, 0)) == "" and stp.retry_after is None

    # 실행 경로가 실제로 그렇게 돼 있는가 - 위 판정을 통과해도 main 이 예전처럼
    # 무조건 완료로 찍으면 아무것도 고쳐지지 않는다
    import inspect

    src = inspect.getsource(main)
    assert src.count("st.last_finished_date = datetime.now(KST).date()") == 2, \
        "완료 표시가 OK/SKIP 두 분기 밖에서도 일어난다"
    assert src.count("schedule_retry(job, st") == 2, \
        "실패·타임아웃 두 경로가 모두 재시도를 잡지는 않는다"
    print("  일일 Job 실패 재시도     OK")


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, _sql, _params=None): pass
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCursor(self._rows)


def _check_seed_skips_finished_daily_jobs():
    """재시작이 **그날 이미 끝낸 일일 Job 을 다시 돌리지 않는가.**

    이걸 안 하면 저녁 배포 한 번에 DART·GDELT·LS 를 하루치 더 두드린다.
    멱등이라 데이터는 안 깨지지만, 재시작이 비싸지면 고치는 것을 미루게 된다.
    """
    today = date(2026, 8, 11)
    yesterday = date(2026, 8, 10)
    states = {j.name: JobState() for j in JOBS}
    # ▶ **Job 이름을 박지 않는다** (2026-08-12)
    #   예전엔 `macro` 를 표본으로 썼는데, 다른 세션이 그 Job 을 내리자 점검이
    #   `KeyError` 로 죽었다. 편제가 바뀔 때마다 점검이 깨지면 사람이 점검을
    #   고치는 대신 지운다. 조건(21:30 이전에 도는 일일 Job)에 맞는 것을 고른다.
    at_2130 = datetime(2026, 8, 11, 21, 30, tzinfo=KST)
    daily = [j for j in JOBS
             if j.daily_at is not None and j.daily_at <= at_2130.timetz().replace(tzinfo=None)]
    assert len(daily) >= 2, "일일 Job 이 둘은 있어야 이 점검이 성립한다"
    done_job, stale_job = daily[0].name, daily[1].name

    n = seed_states_from_history(
        states,
        conn=_FakeConn([(done_job, today),        # 오늘 끝냄 -> 건너뛴다
                        (stale_job, yesterday),   # 어제 것 -> 오늘은 돈다
                        ("no-such-job", today)]), # 사라진 Job -> 무시
        today=today)
    assert n == 1, f"복원 수가 틀리다: {n}"
    assert states[done_job].last_finished_date == today
    assert states[stale_job].last_finished_date is None

    by_name = {j.name: j for j in JOBS}
    assert not is_due(by_name[done_job], states[done_job], at_2130), \
        "오늘 끝낸 Job 이 재시작 뒤 또 돈다"
    assert is_due(by_name[stale_job], states[stale_job], at_2130), \
        "어제가 마지막인 Job 은 오늘 돌아야 한다"

    # 주기형은 복원 대상이 아니다 - 재시작 직후 한 번 더 도는 것이 맞다
    assert states["breadth"].last_finished_date is None

    # 원장을 못 읽어도 수집은 계속된다 (예전처럼 다 도는 쪽 = 안전한 실패)
    class _Boom:
        def cursor(self): raise RuntimeError("DB 없음")
    assert seed_states_from_history({j.name: JobState() for j in JOBS},
                                    conn=_Boom(), today=today) == 0
    print("  재시작 중복 실행 차단    OK")


def _check_job_timeout_budget():
    """유니버스 전량처럼 오래 걸리는 Job 에 기본 30분을 걸면 **정상이 타임아웃으로 기록된다.**

    초당 1회 제한 × 3,900종목 = 65분 이상이다. 예산은 Job 이 하는 일에서 나온다.
    """
    by_name = {j.name: j for j in JOBS}
    assert by_name["breadth"].timeout == JOB_TIMEOUT_SECONDS, "기본값이 안 붙는다"
    uni = by_name["chart-daily-universe"]
    assert "--universe" in uni.argv, "전량 Job 인데 --universe 가 없다"
    assert uni.timeout >= 65 * 60, \
        f"초당 1회 × 3,900종목을 못 담는 예산이다: {uni.timeout / 60:.0f}분"

    # 순차 실행이라 긴 Job 은 다른 일일 Job 뒤에 와야 한다 - 그날치 관측
    # (VKOSPI·라벨 스냅샷)이 밀리면 사후 소급이 안 된다.
    later_than = [j.name for j in JOBS
                  if j.daily_at is not None and j is not uni
                  and j.daily_at > uni.daily_at]
    assert not later_than, f"전량 Job 뒤에 일일 Job 이 남아 밀린다: {later_than}"
    print("  Job 제한 시간 예산       OK")


def _check_exit_codes():
    assert EXIT_SKIP == 2, "market_breadth 의 '수집하지 않았다' 규약과 다르다"
    print("  종료 코드 규약           OK")


def _check_status_classification():
    """SKIP 을 실패로 세면 휴장마다 경보가 울려 아무도 안 보게 된다."""
    assert classify(0) == "OK"
    assert classify(EXIT_SKIP) == "SKIP"
    assert classify(1) == "FAILED"
    assert classify(TIMEOUT_EXIT) == "TIMEOUT"
    assert classify(255) == "FAILED"
    print("  실행 상태 분류           OK")


def _check_record_failure_is_contained():
    """기록 실패가 수집을 죽이면 안 된다 - 조용한 실패를 막는 장치가 새로운
    실패 원인이 되면 본말전도다."""
    class _Boom:
        def cursor(self):
            raise RuntimeError("DB down")

    now = datetime.now(KST)
    ok = record_run("t", ("x.py", "--collect"), started=now, ended=now,
                    exit_code=0, tail="", consecutive_failures=0, conn=_Boom())
    assert ok is False, "기록 실패를 성공으로 보고했다"
    print("  기록 실패 격리           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--check" in sys.argv:
        print(f"{SCHEDULER_VERSION} 자체 점검 (실행 없음)")
        # 총계는 손으로 적지 말고 센다 - 적으면 검사를 늘려도 숫자가 안 는다.
        checks = (_check_job_table,
                  _check_due_logic,
                  _check_exit_codes,
                  _check_status_classification,
                  _check_record_failure_is_contained,
                  _check_seed_skips_finished_daily_jobs,
                  _check_job_timeout_budget,
                  _check_failed_daily_job_retries_same_day)
        for c in checks:
            c()
        print(f"스케줄러 {len(checks)}개 영역 통과. 상주 실행은 --serve")
        raise SystemExit(0)

    if "--once" in sys.argv:
        name = sys.argv[sys.argv.index("--once") + 1]
        matches = [j for j in JOBS if j.name == name]
        if not matches:
            print(f"모르는 Job 이다: {name} (가능: {', '.join(j.name for j in JOBS)})")
            raise SystemExit(1)
        code, tail = run_job(matches[0])
        print(tail)
        print(f"exit={code}")
        raise SystemExit(0 if code in (0, EXIT_SKIP) else code)

    # ▶ 상주 기동은 **명시적으로만** (2026-08-02)
    #   예전에는 인자가 없으면 곧장 상주 루프였다. 그런데 이 저장소의 관례는
    #   "python <파일>" = 자체 점검이라, 전 모듈을 훑는 점검 루프가 이 파일을
    #   만날 때마다 스케줄러가 조용히 떴다(네 번 겪었다 - 그때마다 사람이
    #   알아채고 정리해야 했다). 관례를 지키는 쪽이 아니라 **어기는 쪽이
    #   비용을 내야** 한다: 상주는 --serve 로만, 인자 없으면 점검이다.
    if "--serve" in sys.argv:
        raise SystemExit(main())

    print(f"{SCHEDULER_VERSION}: 인자가 없으면 자체 점검만 한다 "
          f"(상주 기동은 --serve, 단발 실행은 --once <job>)")
    print(f"{SCHEDULER_VERSION} 자체 점검 (실행 없음)")
    _check_job_table()
    _check_due_logic()
    _check_exit_codes()
    _check_status_classification()
    _check_record_failure_is_contained()
    print("스케줄러 5개 영역 통과. 상주 실행은 --serve")
    raise SystemExit(0)
