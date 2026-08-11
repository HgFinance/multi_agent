#!/usr/bin/env python3
"""장 마감 후 일일 마감 보고 · 금요일 주간 포트폴리오 보고 -> Discord.

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 12.4(NAV Close 절차), 19.11~19.12
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 8.2, 8.4
      departments/05-accounting-portfolio/scripts.py (마감 파이프라인 - 여기가 부른다)

**수치를 만들지 않는다. 순서만 안다.** 원장·평가·보고는 전부 기존 결정론 모듈이 하고,
부서장(Hermes)은 `scripts.supervise` / `scripts.narrate_weekly` 에서 **서술만** 한다.
이 파일이 하는 일은 셋뿐이다 - 언제 돌릴지, 무엇을 읽어 넘길지, 어디로 보낼지.

**거래소 캘린더를 만들지 않는다.** 휴장일 판정은 재일님 시세 파트 소관이다(CLAUDE.local.md).
대신 주말만 요일로 거르고, 그 외에는 **스냅샷이 있는지**로 판단한다 - 스냅샷이 없으면
보고를 지어내지 않고 "보고 불가 + 사유"를 보낸다. 휴장과 시세 장애를 우리가 구분해서
말하지 않는 이유가 그것이다. 우리가 아는 사실은 "오늘 평가된 스냅샷이 없다" 뿐이고,
침묵하면 그 둘과 "파이프라인이 죽었다"가 전부 같은 모양이 된다.

**Discord 발송은 파이프라인 안이 아니라 여기서 한다.** `run_accounting_close` 는 API·
테스트도 부르는 경로라, 그 안에서 채널로 쏘면 아무도 요청하지 않은 발송이 따라붙는다.
보내는 주체는 스케줄러다.

설정은 `accounting_ops.yaml` 의 `close_schedule` 이 소유한다(튜닝 대상 숫자는 YAML).

실행:
  python departments/05-accounting-portfolio/close_scheduler.py --serve        # 상주
  python departments/05-accounting-portfolio/close_scheduler.py --once daily   # 지금 1회
  python departments/05-accounting-portfolio/close_scheduler.py --once weekly
  python departments/05-accounting-portfolio/close_scheduler.py --dry-run daily  # 발송 안 함
자체 점검:
  python departments/05-accounting-portfolio/close_scheduler.py   (네트워크·DB 없음)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, time as clock, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

_BASE = Path(__file__).resolve().parent
for _sub in ("", "ledger", "portfolio", "reporting", "reconciliation", "close",
             "treasury", "fees"):
    _p = str(_BASE / _sub) if _sub else str(_BASE)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import discord_reporter  # noqa: E402
import fees  # noqa: E402
import nav_close  # noqa: E402
import mark_provider  # noqa: E402
import scripts as close_pipeline  # noqa: E402
from daily_report import ReportError, build_daily_report  # noqa: E402
from repository import LedgerRepository  # noqa: E402

KST = timezone(timedelta(hours=9))

# 요일 이름 -> weekday(). YAML 이 숫자 대신 이름을 쓰게 해서 오프바이원을 막는다.
_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}

_DEFAULTS = {
    # KRX 정규장은 15:30 마감이다. 종가 봉이 확정돼 market-api 에 실릴 여유를 둔다.
    "daily_at": "15:40",
    "weekly_on": "FRI",
    "weekly_at": "16:00",
    "timezone_offset_hours": 9,
    "weekdays_only": True,
    # 보고를 못 만들었을 때 알릴지. 끄면 휴장과 장애가 같은 침묵이 된다.
    "notify_on_no_data": True,
    # 종가 평가. 비우면 장중 체결가가 되므로 마감 보고에는 1D 가 기본이다.
    "mark_interval": "1D",
    "weekly_holdings_top_n": 10,
}


def _log(message: str) -> None:
    print(f"[close-scheduler] {message}", flush=True)


def settings() -> dict[str, Any]:
    """`accounting_ops.yaml` 의 close_schedule. 없는 키는 _DEFAULTS 로 채운다.

    YAML 로더는 `employee_workers.load_ops` 가 소유한다 - 같은 파일을 두 곳에서
    각자 읽으면 경로나 기본값이 언젠가 갈린다.
    """
    from employee_workers import load_ops

    return {**_DEFAULTS, **((load_ops().get("close_schedule") or {}))}


def _hhmm(value: str) -> clock:
    hour, _, minute = str(value).partition(":")
    return clock(int(hour), int(minute or 0))


def _zone(cfg: dict) -> timezone:
    return timezone(timedelta(hours=int(cfg.get("timezone_offset_hours", 9))))


# ── 무엇을 읽어 넘길 것인가 (전부 결정론) ─────────────────────────────────
def _snapshots_between(repo: LedgerRepository, fund_id: UUID, book_id: UUID,
                       start: date, end: date, zone: timezone) -> list:
    """[start, end] 회계일 범위의 스냅샷. 시각 오름차순.

    회계일 판정은 **현지 시각 기준**이다. UTC 로 자르면 KST 09시 이전 스냅샷이
    전날로 밀려 기초 NAV 가 남의 날 값이 된다.
    """
    return [s for s in repo.load_snapshots(fund_id, book_id)
            if start <= s.as_of.astimezone(zone).date() <= end]


def _close_marks(repo: LedgerRepository, fund_id: UUID, book_id: UUID,
                 as_of: datetime, interval: str | None):
    """마감 평가용 Mark. 못 받은 종목은 로그에 남고 dict 에서 빠진다(NAV 는 fail-closed)."""
    with repo.cursor() as cur:
        cur.execute("select instrument_id from accounting.positions "
                    " where fund_id = %s and book_id = %s and quantity <> 0",
                    (fund_id, book_id))
        held = [row[0] for row in cur.fetchall()]
    if not held:
        return {}
    marks = mark_provider.fetch_marks(repo.symbols_for(held, as_of=as_of), as_of,
                                      interval=interval or None)
    for instrument_id, why in marks.missing:
        _log(f"Mark 없음 instrument={instrument_id}: {why}")
    return marks.prices


def _holdings(snapshot, top_n: int) -> list[dict]:
    """비중 큰 순 보유 종목. 비중 계산과 정렬은 여기(결정론)서 끝낸다."""
    nav = snapshot.nav
    rows = [{
        "instrument_id": str(p.instrument_id),
        "quantity": str(p.quantity),
        "mark_price": str(p.mark_price),
        "market_value": str(p.market_value),
        "unrealized_pnl": str(p.unrealized_pnl),
        "weight": (str(p.market_value / nav) if nav > 0 else None),
        "_sort": p.market_value,
    } for p in snapshot.positions]
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    for row in rows:
        row.pop("_sort")
    return rows[:top_n]


def _label_symbols(repo: LedgerRepository, rows: list[dict], as_of: datetime) -> None:
    """종목코드를 붙인다. 못 찾으면 넣지 않는다 - 짐작한 코드를 보고서에 싣지 않는다."""
    if not rows:
        return
    symbols = repo.symbols_for([UUID(r["instrument_id"]) for r in rows], as_of=as_of)
    for row in rows:
        symbol = symbols.get(UUID(row["instrument_id"]))
        if symbol:
            row["symbol"] = symbol


# ── 일일 마감 ──────────────────────────────────────────────────────────────
def run_daily(repo: LedgerRepository, *, now: datetime, cfg: dict,
              send=None, chat=None) -> dict:
    """장 마감 후 일일 보고 1회. 돌려주는 값은 발송 결과다."""
    zone = _zone(cfg)
    send = send or discord_reporter.send_close
    accounting_date = now.astimezone(zone).date()

    chosen = repo.default_book()
    if chosen is None:
        return _no_data(cfg, "일일 마감 보고 불가",
                        "Canonical 장부를 고르지 못했습니다 "
                        "(ACTIVE 장부가 0개 또는 2개 이상, ACCOUNTING_DEFAULT_BOOK_ID 미설정)")
    fund_id, book_id = chosen

    todays = _snapshots_between(repo, fund_id, book_id, accounting_date, accounting_date, zone)
    if not todays:
        return _no_data(cfg, f"일일 마감 보고 불가 · {accounting_date}",
                        "오늘 회계일의 평가 스냅샷이 없습니다. 휴장이거나 시세·평가가 "
                        "막혀 NAV 가 보류된 상태입니다(원장 분개는 별개로 진행됩니다).")

    ledger = repo.load(fund_id, book_id)
    out = close_pipeline.run_accounting_close(
        ledger=ledger,
        as_of=now,
        accounting_date=accounting_date,
        marks=_close_marks(repo, fund_id, book_id, now, cfg.get("mark_interval")),
        opening_snapshot=todays[0],
        # 성과보수 고수위. **승인된 공식 NAV 최대값**이고 파이프라인은 DB를 모른다 -
        # 저장소를 아는 여기가 읽어서 넘긴다(scripts.accrue_fees 주석 참고).
        high_water_mark=fees.high_water_mark(repo, fund_id),
        chat=chat,
    )
    # 마감 수치를 `accounting.nav_runs`에 Preliminary로 남기고 독립 검증을 함께
    # 돌린다. **확정이 아니다** - 공식 승격은 외부 승인이 있어야 하고
    # (`nav_close.approve_official`), 그 승인 행은 우리가 만들지 않는다.
    # 실패해도 보고는 나간다 - 확정 절차가 막혔다고 마감 보고까지 끊으면 아무도
    # 그 사실을 모른다. 검증 blocker는 로그로 남아 다음 조사 대상이 된다.
    try:
        closing = todays[-1]
        nav_run_id = nav_close.record_run(
            repo, closing, valuation_date=accounting_date, trace_id=fund_id)
        check = nav_close.independent_check(
            closing,
            report=out.get("report"),
            trial_balance_sum=sum(repo.load(fund_id, book_id).trial_balance().values(),
                                  Decimal(0)),
        )
        _log(f"NAV Run {nav_run_id} check={'PASS' if check.passed else 'BLOCKED'} "
             f"{'/'.join(check.blockers)}")
    except Exception as exc:  # noqa: BLE001
        _log(f"NAV Run 기록 불가 {accounting_date}: {type(exc).__name__}: {exc}")

    result = send(out)
    _log(f"일일 마감 {accounting_date} nav_status={out.get('nav_status')} "
         f"discord={result.get('ok')}")
    return result


# ── 주간 포트폴리오 ────────────────────────────────────────────────────────
def run_weekly(repo: LedgerRepository, *, now: datetime, cfg: dict,
               send=None, chat=None) -> dict:
    """이번 주(월~오늘) 포트폴리오 보고 1회."""
    zone = _zone(cfg)
    send = send or discord_reporter.send_weekly
    today = now.astimezone(zone).date()
    week_start = today - timedelta(days=today.weekday())

    chosen = repo.default_book()
    if chosen is None:
        return _no_data(cfg, "주간 포트폴리오 보고 불가",
                        "Canonical 장부를 고르지 못했습니다")
    fund_id, book_id = chosen

    week = _snapshots_between(repo, fund_id, book_id, week_start, today, zone)
    if len(week) < 2:
        return _no_data(cfg, f"주간 포트폴리오 보고 불가 · {week_start}~{today}",
                        f"구간 스냅샷이 {len(week)}건입니다. 기초·기말 최소 2건이 필요합니다.")

    try:
        report = build_daily_report(snapshots=week, ledger=repo.load(fund_id, book_id),
                                    accounting_date=today, period_start=week_start)
    except ReportError as exc:
        return _no_data(cfg, f"주간 포트폴리오 보고 불가 · {week_start}~{today}",
                        str(exc))

    rows = _holdings(week[-1], int(cfg.get("weekly_holdings_top_n", 10)))
    _label_symbols(repo, rows, week[-1].as_of)
    payload = report.to_dict()
    note = close_pipeline.narrate_weekly(payload, rows, chat=chat)

    result = send(payload, rows, narrative=note["narrative"])
    _log(f"주간 보고 {week_start}~{today} 스냅샷 {len(week)}건 "
         f"llm={note.get('llm_called')} discord={result.get('ok')}")
    return result


def _no_data(cfg: dict, title: str, detail: str) -> dict:
    """보고를 못 만든 경우. 설정이 허용하면 그 사실을 채널에 알린다."""
    _log(f"{title}: {detail}")
    if not cfg.get("notify_on_no_data", True):
        return {"ok": False, "reason": "notify_on_no_data=false - 통지 생략"}
    return discord_reporter.send_notice(title, detail)


# ── 언제 돌릴 것인가 ───────────────────────────────────────────────────────
def due(now: datetime, cfg: dict, last: dict[str, date]) -> list[str]:
    """지금 실행할 작업 목록. 하루에 한 번씩만 나온다(`last` 가 그 기억이다).

    시각을 지나쳐서 깨어나도 그날 안이면 실행한다 - 프로세스가 15:40 에 자고 있었다고
    그날 마감 보고가 통째로 없어지면 안 된다. 날짜가 바뀌면 다시 대상이 된다.
    """
    local = now.astimezone(_zone(cfg))
    today = local.date()
    jobs: list[str] = []
    if cfg.get("weekdays_only", True) and local.weekday() >= 5:
        return jobs
    if local.time() >= _hhmm(cfg["daily_at"]) and last.get("daily") != today:
        jobs.append("daily")
    if (local.weekday() == _WEEKDAYS[str(cfg["weekly_on"]).upper()]
            and local.time() >= _hhmm(cfg["weekly_at"]) and last.get("weekly") != today):
        jobs.append("weekly")
    return jobs


def serve() -> None:
    repo = LedgerRepository.from_env(required=True)
    cfg = settings()
    tick = max(float(os.environ.get("CLOSE_SCHEDULER_TICK_SECONDS", "60")), 5.0)
    last: dict[str, date] = {}
    _log(f"start daily={cfg['daily_at']} weekly={cfg['weekly_on']} {cfg['weekly_at']} "
         f"(UTC{cfg['timezone_offset_hours']:+d}) tick={tick}s")

    runners = {"daily": run_daily, "weekly": run_weekly}
    while True:
        try:
            now = datetime.now(timezone.utc)
            for job in due(now, cfg, last):
                # 실행 전에 찍는다. 실패했다고 같은 주기에 계속 재시도하면 채널이 잠긴다.
                last[job] = now.astimezone(_zone(cfg)).date()
                try:
                    runners[job](repo, now=now, cfg=cfg)
                except Exception as exc:  # noqa: BLE001
                    # 한 작업의 실패가 스케줄러를 죽이지 않는다. 침묵도 아니다.
                    _log(f"{job} 실패: {type(exc).__name__}: {exc}")
                    discord_reporter.send_notice(
                        f"{job} 보고 실패", f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"cycle failed: {type(exc).__name__}: {exc}")
        time.sleep(tick)


# ── 자체 점검 (네트워크·DB 없음) ───────────────────────────────────────────
def _self_check() -> None:
    cfg = {**_DEFAULTS, "daily_at": "15:40", "weekly_at": "16:00", "weekly_on": "FRI"}

    def at(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=KST).astimezone(timezone.utc)

    # 1. 마감 시각 전에는 안 돈다. 지나면 그날 한 번만 돈다.
    assert due(at(2026, 8, 10, 15, 29), cfg, {}) == []          # 월 15:29
    assert due(at(2026, 8, 10, 15, 40), cfg, {}) == ["daily"]   # 월 15:40
    assert due(at(2026, 8, 10, 23, 59), cfg, {}) == ["daily"], "늦게 깨어나면 그날을 건너뛴다"
    assert due(at(2026, 8, 10, 16, 0), cfg, {"daily": date(2026, 8, 10)}) == []

    # 2. 금요일에는 주간 보고가 붙는다. 순서는 일일 -> 주간이다.
    assert due(at(2026, 8, 14, 16, 0), cfg, {}) == ["daily", "weekly"]   # 금
    assert due(at(2026, 8, 14, 15, 45), cfg, {}) == ["daily"], "주간 시각 전인데 돌았다"
    assert due(at(2026, 8, 13, 16, 0), cfg, {}) == ["daily"], "목요일에 주간이 돌았다"

    # 3. 주말은 아예 돌지 않는다. 거래소 캘린더 없이 거를 수 있는 유일한 축이다.
    assert due(at(2026, 8, 15, 16, 0), cfg, {}) == []   # 토
    assert due(at(2026, 8, 16, 16, 0), cfg, {}) == []   # 일

    # 4. 날짜가 바뀌면 다시 대상이 된다
    assert due(at(2026, 8, 11, 15, 40), cfg, {"daily": date(2026, 8, 10)}) == ["daily"]

    # 5. 스냅샷이 없으면 **보고를 지어내지 않고 사유를 보낸다**(침묵 금지)
    notices: list[tuple[str, str]] = []
    real_notice = discord_reporter.send_notice
    discord_reporter.send_notice = lambda t, d, **k: (notices.append((t, d)), {"ok": True})[1]
    try:
        class _EmptyRepo:
            def default_book(self):
                return (UUID(int=1), UUID(int=2))

            def load_snapshots(self, fund_id, book_id):
                return []

        sent: list = []
        result = run_daily(_EmptyRepo(), now=at(2026, 8, 10, 15, 40), cfg=cfg,
                           send=lambda out: sent.append(out) or {"ok": True})
        assert result["ok"] is True and not sent, "스냅샷이 없는데 마감 보고를 보냈다"
        assert notices and "스냅샷이 없습니다" in notices[0][1], notices

        # 6. 장부를 못 고르면 남의 장부로 보고하지 않는다
        notices.clear()

        class _NoBook(_EmptyRepo):
            def default_book(self):
                return None

        run_daily(_NoBook(), now=at(2026, 8, 10, 15, 40), cfg=cfg,
                  send=lambda out: sent.append(out) or {"ok": True})
        assert not sent, "장부를 못 골랐는데 보고를 보냈다"
        assert "장부를 고르지 못했" in notices[0][1], notices

        # 7. notify_on_no_data=false 면 통지도 안 나간다(설정대로)
        notices.clear()
        quiet = {**cfg, "notify_on_no_data": False}
        assert run_daily(_NoBook(), now=at(2026, 8, 10, 15, 40), cfg=quiet,
                         send=lambda out: sent.append(out))["ok"] is False
        assert not notices, "notify_on_no_data=false 인데 통지가 나갔다"
    finally:
        discord_reporter.send_notice = real_notice

    # 8. 회계일은 **현지 시각** 기준이다. UTC 로 자르면 KST 오전이 전날로 밀린다
    class _Snap:
        def __init__(self, when):
            self.as_of = when

    morning = datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)   # KST 09:30 같은 날
    class _OneRepo:
        def load_snapshots(self, fund_id, book_id):
            return [_Snap(morning)]

    got = _snapshots_between(_OneRepo(), UUID(int=1), UUID(int=2),
                             date(2026, 8, 10), date(2026, 8, 10), KST)
    assert len(got) == 1, "KST 오전 스냅샷이 전날로 밀렸다"

    # 9. 보유 종목은 비중 순이고 상위 N 개만 간다. 비중은 NAV 대비다
    class _Pos:
        def __init__(self, iid, qty, mark, value):
            self.instrument_id, self.quantity = iid, qty
            self.mark_price, self.market_value = mark, value
            self.unrealized_pnl = Decimal(0)

    class _S:
        nav = Decimal("1000")
        positions = (_Pos(UUID(int=7), Decimal(1), Decimal(100), Decimal(100)),
                     _Pos(UUID(int=8), Decimal(3), Decimal(300), Decimal(900)))

    rows = _holdings(_S(), 1)
    assert len(rows) == 1 and rows[0]["instrument_id"] == str(UUID(int=8)), rows
    assert rows[0]["weight"] == "0.9", rows[0]["weight"]
    assert all(isinstance(v, (str, type(None))) for v in rows[0].values()), rows[0]

    print("ok - Close Scheduler 9개 영역 점검 통과 (네트워크·DB 없음)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--serve" in sys.argv:
        serve()
    elif "--once" in sys.argv or "--dry-run" in sys.argv:
        flag = "--dry-run" if "--dry-run" in sys.argv else "--once"
        job = sys.argv[sys.argv.index(flag) + 1] if len(sys.argv) > sys.argv.index(flag) + 1 else "daily"
        try:
            from dotenv import load_dotenv
            load_dotenv(Path.cwd() / ".env")
        except ModuleNotFoundError:
            pass
        repository = LedgerRepository.from_env(required=True)
        dry = flag == "--dry-run"
        # dry-run 은 발송만 막는다. 마감 계산과 Hermes 서술은 그대로 돌아야
        # "보낼 뻔한 내용"을 볼 수 있다.
        sender = (lambda *a, **k: {"ok": True, "status": "dry-run"}) if dry else None
        runner = run_weekly if job == "weekly" else run_daily
        outcome = runner(repository, now=datetime.now(timezone.utc),
                         cfg=settings(), send=sender)
        print(outcome)
        raise SystemExit(0 if outcome.get("ok") else 1)
    else:
        _self_check()
