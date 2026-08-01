#!/usr/bin/env python3
"""배치 수집기 스케줄러 - Docker Container 의 entrypoint.

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "다른 수집기들도 띄우자 / 계속 부족한 부분 개선"

▶ 왜 상주 서비스가 아니라 스케줄러인가
  공시·재무·CA·거시·기업개황·Calendar 는 하루 1~수십 회 실행이면 충분한 배치형이다.
  各各 컨테이너로 "떠 있게" 만들면 컨테이너 7개가 종일 잠만 잔다. 하나의 스케줄러가
  같은 Image 안의 수집기를 subprocess 로 돌린다.

▶ 지킨 것
  - **순차 실행.** 동시에 돌리지 않는다 - DART 계열이 같은 키·같은 Rate Limit 을
    공유하므로 병렬이면 서로를 429 로 민다.
  - **재시작 후 재실행은 안전하다.** 상태를 메모리에만 두므로 컨테이너 재시작이
    일일 Job 을 다시 돌릴 수 있는데, 모든 수집기가 멱등 적재라 중복이 생기지 않는다
    (그래서 상태 파일을 만들지 않았다 - 지금 볼륨 하나 늘리는 것보다 싸다).
  - **종료 코드 2 는 SKIP 이다.** market_breadth 가 휴장·수집 불가 국면에서 2 를
    돌려준다("수집하지 않았다"). 실패(1)와 섞으면 휴장일마다 거짓 경보가 쌓인다.
  - **연속 실패를 드러낸다.** Job 별 연속 실패 횟수를 세고 3회부터 요약에 ⚠ 를
    붙인다. 실패해도 스케줄러는 계속 돈다 - 한 Source 장애가 나머지를 멈추지 않는다.

▶ 여기 없는 것
  - watchlist_builder: LS t1444 에 호출 건수 제한(IGW00201)이 있고 결과 파일을
    호스트가 커밋 관리한다 - 주 1회 호스트에서 수동 실행.
  - 뉴스·실시간 시세: 전용 상주 컨테이너(news-watcher, ls-realtime)가 맡는다.

실행
  python collectors/collector_scheduler.py            # 상주 실행
  python collectors/collector_scheduler.py --check    # 자체 점검 (실행 없음)
  python collectors/collector_scheduler.py --once 이름  # 한 Job 즉시 1회 (수동 점검용)
"""
from __future__ import annotations

import subprocess
import sys
import threading
import signal
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SCHEDULER_VERSION = "research-collector-scheduler-v1"
KST = timezone(timedelta(hours=9))

CHECK_EVERY_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 30 * 60
FAILURE_ALERT_THRESHOLD = 3
EXIT_SKIP = 2  # 수집기 규약: "의도된 미수집" (market_breadth 실측)


@dataclass(frozen=True)
class Job:
    """스케줄 한 줄. every 와 daily_at 중 하나만 갖는다."""

    name: str
    argv: tuple[str, ...]
    every_minutes: int | None = None
    window: tuple[time, time] | None = None  # 주기형의 활동 창 (KST)
    daily_at: time | None = None             # 일일형의 실행 시각 (KST)

    def __post_init__(self):
        if (self.every_minutes is None) == (self.daily_at is None):
            raise ValueError(f"{self.name}: every_minutes 와 daily_at 중 하나만 지정한다")
        if self.every_minutes is not None and self.window is None:
            raise ValueError(f"{self.name}: 주기형은 활동 창이 필수다 - 무제한 폴링을 막는다")


# 시각은 전부 KST. DART 정기공시가 주로 장 마감 후에 몰리므로 일일 Job 은 저녁에 둔다.
JOBS: tuple[Job, ...] = (
    # 공시는 증분 폴링. 기본 범위 = 최근 3일(주말·야간 공시 누락 방지), 멱등이라
    # 겹쳐도 안전. --max-pages 30: 3일 창이 25페이지까지 나온 실측 + 여유
    Job("disclosure", ("collectors/opendart_collector.py", "--collect", "--max-pages", "30"),
        every_minutes=10, window=(time(7, 0), time(19, 0))),
    # 시장 Breadth - 세션 판정은 수집기 자신이 한다 (휴장이면 exit 2 = SKIP)
    Job("breadth", ("collectors/market_breadth_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 30), time(16, 10))),
    # KOSPI200 파생 스냅샷 - 파생 세션(주식 ±15분)은 수집기가 판정, 밖이면 SKIP.
    # 창 상한 17:00: 수능일 파생 마감 16:45 까지 덮는다. 호출 4회/실행이라 가볍다.
    Job("derivatives", ("collectors/derivatives_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 40), time(17, 0))),
    # 관측 Calendar 갱신 - 오늘 세션을 역산에 반영해 선언 Calendar 검증 폭을 늘린다
    Job("calendar-observed", ("collectors/calendar_collector.py", "--collect"),
        daily_at=time(16, 20)),
    Job("macro", ("collectors/macro_collector.py", "--collect"),
        daily_at=time(7, 30)),
    # 시세 평면 심박·품질 감사 - 개장 전, 밤 배치가 다 끝난 뒤 (FAIL 이면 exit 1
    # 로 스케줄러 로그에 남는다 - 개장 전에 눈에 띄는 것이 목적)
    Job("data-steward", ("collectors/market_data_steward.py", "--audit"),
        daily_at=time(7, 10)),
    # Raw -> 검증된 Parquet Archive (전일분 - 기본값이 데이터 있는 최근 거래일).
    # 06:50: 분봉 백필 등 밤 작업이 끝난 뒤, Steward(07:10)가 결과를 보기 전.
    Job("market-archive", ("collectors/market_archive_exporter.py", "--export"),
        daily_at=time(6, 50)),
    # --limit 명시: CLI 기본값(재무 20, CA 40)은 프로브용이라 스케줄이 그대로 쓰면
    # 발행사 1,049곳 중 꼬리만 돌게 된다 (2026-07-31 점검에서 발견).
    # 재무는 corp_code 콤마 배치 조회라 1,200 이어도 호출 수십 회다.
    Job("financial", ("collectors/opendart_financial.py", "--collect", "--limit", "1200"),
        daily_at=time(18, 10)),
    Job("corporate-action", ("collectors/corporate_action_collector.py", "--collect", "--limit", "400"),
        daily_at=time(18, 30)),
    # 공시 원문 Archive - 당일 공시 원본 ZIP 을 Private Storage 로 (2시간 유예가
    # 있어 저녁 실행이 당일분 대부분을 잡고, 미준비분은 다음 날 자연 재시도)
    Job("document-archive", ("collectors/opendart_document_collector.py", "--collect", "--limit", "600"),
        daily_at=time(20, 0)),
    # 개황이 빈 issuer 보강 (전량 보강은 완료 - 이후는 신규 필러 몫).
    # 300: 공시 백필 하루치가 신규 143 corp 를 만든 실측(2026-07-31) + 여유.
    # 2건/초 제한이라 300개 = 2.5분이면 끝난다.
    Job("company-profile", ("collectors/opendart_company_collector.py", "--collect", "--limit", "300"),
        daily_at=time(19, 0)),
    # 지정학 리스크 (GPR 일별 지수 + GDELT 테마 보도량·톤).
    # 07:20 - Steward(07:10) 뒤, 개장 전. 밤사이 미국·중동 사건이 반영된
    # 상태로 장을 연다. 일 단위 계열이고 진행 중인 날은 제외하므로 장중
    # 재폴링은 이득이 없다(15분 해상도가 필요하면 timelinevolraw - 백로그).
    Job("geopolitical", ("collectors/geopolitical_collector.py", "--collect",
                         "--days", "120"),
        daily_at=time(7, 20)),
    # Bluesky 미국 표적 계정 (기관 미디어 4곳 + 매크로 논객 2인, ~166건/일 실측).
    # 60분 주기면 계정당 피드 50건 버퍼가 최고 볼륨(Reuters ~57건/일)도 20시간
    # 이상 덮는다 - 놓칠 수 없는 구조. 창 06:00~23:50: 미 장중(KST 밤)은 다음날
    # 아침 첫 폴링이 버퍼로 회수하므로 새벽 상주가 필요 없다.
    Job("bluesky-watch", ("collectors/bluesky_watch_collector.py", "--collect"),
        every_minutes=60, window=(time(6, 0), time(23, 50))),
)


@dataclass
class JobState:
    last_started: datetime | None = None
    last_finished_date: date | None = None  # 일일형 중복 방지
    consecutive_failures: int = 0
    runs: int = 0
    skips: int = 0


def is_due(job: Job, state: JobState, now: datetime) -> bool:
    if job.daily_at is not None:
        if now.timetz().replace(tzinfo=None) < job.daily_at:
            return False
        return state.last_finished_date != now.date()
    lo, hi = job.window
    t = now.timetz().replace(tzinfo=None)
    if not (lo <= t <= hi):
        return False
    if state.last_started is None:
        return True
    return (now - state.last_started) >= timedelta(minutes=job.every_minutes)


def run_job(job: Job, *, timeout: float = JOB_TIMEOUT_SECONDS) -> tuple[int, str]:
    """subprocess 로 한 번 실행. (종료 코드, 출력 꼬리)."""
    proc = subprocess.run(
        [sys.executable, *job.argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    if proc.returncode not in (0, EXIT_SKIP) and proc.stderr:
        tail += "\nstderr: " + (proc.stderr or "").strip()[-400:]
    return proc.returncode, tail


def main() -> int:
    stop = threading.Event()

    def _handle(signum, _frame):
        print(f"신호 {signum} 수신 - 진행 중인 Job 을 마치고 종료한다", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    states: dict[str, JobState] = {j.name: JobState() for j in JOBS}
    print(f"{SCHEDULER_VERSION}: Job {len(JOBS)}개", flush=True)
    for j in JOBS:
        when = (f"{j.every_minutes}분마다 {j.window[0]:%H:%M}~{j.window[1]:%H:%M}"
                if j.every_minutes else f"매일 {j.daily_at:%H:%M}")
        print(f"  {j.name:<18} {when}  <- {' '.join(j.argv)}", flush=True)

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
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: ⚠ TIMEOUT "
                      f"({JOB_TIMEOUT_SECONDS / 60:.0f}분, 연속 {st.consecutive_failures})",
                      flush=True)
                continue
            st.runs += 1
            st.last_finished_date = datetime.now(KST).date()
            if code == 0:
                st.consecutive_failures = 0
                last = tail.splitlines()[-1] if tail else ""
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: OK  {last}", flush=True)
            elif code == EXIT_SKIP:
                st.consecutive_failures = 0
                st.skips += 1
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: SKIP (수집기 판단)", flush=True)
            else:
                st.consecutive_failures += 1
                mark = " ⚠" if st.consecutive_failures >= FAILURE_ALERT_THRESHOLD else ""
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: 실패 exit={code} "
                      f"(연속 {st.consecutive_failures}){mark}\n{tail}", flush=True)
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
    assert len(names) == len(set(names)), "Job 이름이 겹친다"
    for j in JOBS:
        assert Path(__file__).resolve().parent.parent.joinpath(j.argv[0]).exists(), \
            f"{j.name}: {j.argv[0]} 이 없다"
        # 실행 플래그가 없으면 수집기 규약상 자체점검만 돌게 된다.
        # --collect(수집) / --audit(Steward 감사) / --export(Archive) 가 실행 동사다.
        assert {"--collect", "--audit", "--export"} & set(j.argv), \
            f"{j.name}: 실행 플래그(--collect/--audit/--export)가 없다 - 자체점검만 돈다"
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
    kst = lambda h, m=0: datetime(2026, 7, 31, h, m, tzinfo=KST)  # noqa: E731
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
    # 재시작(상태 소실) 후에는 다시 due 다 - 멱등 적재라 안전하다는 전제를 명시
    st3 = JobState()
    assert is_due(daily, st3, kst(23, 0))
    print("  due 판정                 OK")


def _check_exit_codes():
    assert EXIT_SKIP == 2, "market_breadth 의 '수집하지 않았다' 규약과 다르다"
    print("  종료 코드 규약           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--check" in sys.argv:
        print(f"{SCHEDULER_VERSION} 자체 점검 (실행 없음)")
        _check_job_table()
        _check_due_logic()
        _check_exit_codes()
        print("스케줄러 3개 영역 통과. 상주 실행은 인자 없이")
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

    raise SystemExit(main())
