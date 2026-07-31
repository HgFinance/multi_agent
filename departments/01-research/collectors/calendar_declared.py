#!/usr/bin/env python3
"""거래 Calendar 선언적 생성 - 당일·미래 거래일 (관측 검증 필수).

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "캘린더는 알아서 수집해오셈, API로 안 해도 괜찮을듯"
      calendar_collector.py (관측 역산 - 이 파일의 검증 기준)

▶ 구조: 선언 + 관측 검증, 불일치는 적재 거부
  공표된 휴장일 목록(아래 HOLIDAYS_2026)과 주말 규칙으로 1년 치를 만들고,
  **관측 Calendar(t8410 역산)와 겹치는 전 구간이 하루라도 다르면 적재를 거부**한다
  (fail-closed). 관측이 선언을 계속 검증하므로 시간이 갈수록 선언의 신뢰가
  실측으로 바뀐다. 실제로 이 검증 체계가 설계 단계에서 성과를 냈다 -
  "2026-07-17 비거래일 원인 미상"이 **제헌절 공휴일 재지정**(2026-04-28 국무회의,
  18년 만) 때문임을 선언 목록을 만들면서 확인했다.

▶ 선언 근거 (2026-07-31 조사)
  - 제헌절 재지정: 국무회의 2026-04-28 의결, 관공서의 공휴일에 관한 규정 개정
  - 2026 증시 휴장일: 언론 보도(dpi1004.com/10769 등) + 아래 관측 교차검증
  - 1~7월 관측(141 거래일)과 평일 비거래일 10건 전부 일치 확인:
    1/1, 2/16~18(설), 3/2(삼일절 대체), 5/1, 5/5, 5/25(석탄일 대체),
    6/3(지방선거), 7/17(제헌절)
  - **관측이 부정한 것도 근거다**: 6/6 현충일(토)은 대체공휴일이 없다 -
    6/8(월) 정상 거래 관측. 추석 셋째 날 9/26(토)도 설·추석 대체는 일요일
    겹침만이라 9/28(월)은 거래일이다.

▶ 2027년은 만들지 않는다. 설·추석(음력)과 대체공휴일, 임시공휴일은 규칙만으로
  확정할 수 없다 - 매년 공표를 보고 목록을 갱신한다(DECLARED_THROUGH 가 강제).

자체 점검: python collectors/calendar_declared.py
실제 적재:  python collectors/calendar_declared.py --collect
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from calendar_collector import (  # noqa: E402
    KST,
    MARKET_KRX,
    SESSION_REGULAR,
    CalendarUnavailable,
    SessionRow,
)

DECLARED_VERSION = "research-calendar-declared-v1"

DECLARED_FROM = date(2026, 1, 1)
DECLARED_THROUGH = date(2026, 12, 31)

# 평일인 휴장일만 적는다. 주말과 겹치는 공휴일(8/15 광복절 토 등)은 어차피
# 주말 규칙이 거르므로 여기 넣지 않는다 - 넣으면 "평일 휴장 수" 검산이 흐려진다.
HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "신정",
    date(2026, 2, 16): "설날 연휴",
    date(2026, 2, 17): "설날",
    date(2026, 2, 18): "설날 연휴",
    date(2026, 3, 2): "삼일절 대체공휴일 (3/1 일요일)",
    date(2026, 5, 1): "근로자의 날 (증시 휴장)",
    date(2026, 5, 5): "어린이날",
    date(2026, 5, 25): "부처님오신날 대체공휴일 (5/24 일요일)",
    date(2026, 6, 3): "제9회 전국동시지방선거",
    date(2026, 7, 17): "제헌절 (2026-04-28 국무회의 재지정, 18년 만)",
    date(2026, 8, 17): "광복절 대체공휴일 (8/15 토요일)",
    date(2026, 9, 24): "추석 연휴",
    date(2026, 9, 25): "추석",
    date(2026, 10, 5): "개천절 대체공휴일 (10/3 토요일)",
    date(2026, 10, 9): "한글날",
    date(2026, 12, 25): "성탄절",
    date(2026, 12, 31): "연말 휴장 (KRX 규정 - 공휴일 아님)",
}

REGULAR_OPEN = time(9, 0)
REGULAR_CLOSE = time(15, 30)

# 시각이 다른 특이 세션. (개장, 폐장, 사유)
SPECIAL_SESSIONS: dict[date, tuple[time, time, str]] = {
    # 연초 개장식 - 1시간 지연 개장, 폐장은 그대로 (KRX 연례 공지)
    date(2026, 1, 2): (time(10, 0), REGULAR_CLOSE, "연초 개장일 - 개장식 1시간 지연"),
    # 수능일 - 개장·폐장 모두 1시간 연기 (2027학년도 수능 2026-11-19)
    date(2026, 11, 19): (time(10, 0), time(16, 30), "수능일 - 개장·폐장 1시간 연기"),
}

MIN_OVERLAP_TRADING_DAYS = 60  # 이보다 짧은 관측과의 일치는 검증이라 부르기 어렵다


@dataclass(frozen=True)
class DeclaredDraft:
    """reference.upsert_calendar 가 받는 Duck Type (calendar_collector.CalendarDraft 와 동형)."""

    market: str
    effective_from: date
    effective_to: date
    sessions: tuple[SessionRow, ...]
    content_hash: str

    @property
    def trading_days(self) -> int:
        return sum(1 for s in self.sessions if s.is_trading_day)

    def summary(self) -> str:
        return (
            f"{self.market} {self.effective_from}~{self.effective_to} "
            f"전체 {len(self.sessions)}일 중 거래일 {self.trading_days}일 "
            f"(선언 휴장 {len(HOLIDAYS_2026)}건, hash {self.content_hash[:12]})"
        )


def build_declared_draft(
    start: date = DECLARED_FROM, end: date = DECLARED_THROUGH
) -> DeclaredDraft:
    """선언 목록으로 Calendar 를 만든다. 아는 범위 밖은 거부한다 - 추정하지 않는다."""
    if start > end:
        raise CalendarUnavailable("start 가 end 보다 늦다")
    if start < DECLARED_FROM or end > DECLARED_THROUGH:
        raise CalendarUnavailable(
            f"선언 목록은 {DECLARED_FROM}~{DECLARED_THROUGH} 뿐이다 - "
            f"{start}~{end} 는 만들 수 없다. 다음 해 공표를 보고 목록부터 갱신할 것"
        )
    for d in HOLIDAYS_2026:
        if d.isoweekday() >= 6:
            raise CalendarUnavailable(
                f"휴장 목록에 주말이 들어 있다: {d} - 평일만 적는 규칙이 깨졌다"
            )

    sessions: list[SessionRow] = []
    cur = start
    while cur <= end:
        weekend = cur.isoweekday() >= 6
        holiday = HOLIDAYS_2026.get(cur)
        trading = not weekend and holiday is None

        meta: dict = {"method": "declared_public_notice"}
        if holiday:
            meta["reason"] = holiday
        if trading and cur in SPECIAL_SESSIONS:
            opens, closes, note = SPECIAL_SESSIONS[cur]
            meta["note"] = note
        else:
            opens, closes = REGULAR_OPEN, REGULAR_CLOSE

        sessions.append(SessionRow(
            trade_date=cur,
            session_type=SESSION_REGULAR,
            is_trading_day=trading,
            opens_at=datetime.combine(cur, opens, tzinfo=KST) if trading else None,
            closes_at=datetime.combine(cur, closes, tzinfo=KST) if trading else None,
            metadata=meta,
        ))
        cur += timedelta(days=1)

    material = "|".join([
        MARKET_KRX, "declared_public_notice", start.isoformat(), end.isoformat(),
        ",".join(sorted(d.isoformat() for d in HOLIDAYS_2026)),
        ",".join(f"{d.isoformat()}:{o:%H%M}-{c:%H%M}" for d, (o, c, _n) in sorted(SPECIAL_SESSIONS.items())),
    ])
    return DeclaredDraft(
        market=MARKET_KRX,
        effective_from=start,
        effective_to=end,
        sessions=tuple(sessions),
        content_hash=hashlib.sha256(material.encode()).hexdigest(),
    )


def verify_against_observed(
    draft: DeclaredDraft,
    observed: dict[date, bool],
    *,
    min_overlap_trading_days: int = MIN_OVERLAP_TRADING_DAYS,
) -> tuple[int, int]:
    """관측 Calendar 와 겹치는 전 구간을 비교한다. 하나라도 다르면 예외다.

    반환 (겹친 날 수, 겹친 거래일 수). 검증 없이 적재하는 경로는 없다 -
    관측이 아예 없으면(새 환경) 그것도 실패다. 선언을 맹신하지 않는다.
    """
    if not observed:
        raise CalendarUnavailable("관측 Calendar 가 없다 - 선언을 검증 없이 적재하지 않는다")

    mismatches: list[str] = []
    overlap = overlap_trading = 0
    for s in draft.sessions:
        obs = observed.get(s.trade_date)
        if obs is None:
            continue
        overlap += 1
        if obs:
            overlap_trading += 1
        if obs != s.is_trading_day:
            mismatches.append(
                f"{s.trade_date}: 선언={'거래' if s.is_trading_day else '휴장'} "
                f"관측={'거래' if obs else '휴장'}"
            )
    if overlap_trading < min_overlap_trading_days:
        raise CalendarUnavailable(
            f"관측과 겹치는 거래일이 {overlap_trading}일뿐이다 "
            f"(최소 {min_overlap_trading_days}) - 검증이라 부를 수 없다"
        )
    if mismatches:
        raise CalendarUnavailable(
            "선언과 관측이 다르다 - 적재하지 않는다: " + "; ".join(mismatches[:5])
            + (f" 외 {len(mismatches) - 5}건" if len(mismatches) > 5 else "")
        )
    return overlap, overlap_trading


# ---------------------------------------------------------------------------
# 실제 적재
# ---------------------------------------------------------------------------

def _load_observed(ref) -> dict[date, bool]:
    """가장 최신의 **관측 역산** Version 세션들. 선언 Version 은 검증 기준이 될 수 없다."""
    with ref._conn.cursor() as cur:
        cur.execute(
            """
            select s.trade_date, s.is_trading_day
            from reference.market_sessions s
            where s.calendar_version_id = (
                select s2.calendar_version_id
                from reference.market_sessions s2
                join reference.market_calendar_versions v using (calendar_version_id)
                where v.market = %s
                  and s2.metadata->>'method' = 'derived_from_daily_bars'
                group by s2.calendar_version_id, v.version
                order by v.version desc
                limit 1
            )
            """,
            (MARKET_KRX,),
        )
        return {d: bool(t) for d, t in cur.fetchall()}


def _collect() -> int:
    from reference_repository import SupabaseReferenceRepository

    draft = build_declared_draft()
    print(f"  {draft.summary()}")

    ref = SupabaseReferenceRepository()
    try:
        observed = _load_observed(ref)
        overlap, overlap_trading = verify_against_observed(draft, observed)
        print(f"  관측 교차검증: 겹침 {overlap}일 (거래일 {overlap_trading}일) 전부 일치")

        version_id, rows, created = ref.upsert_calendar(draft)
        if created:
            print(f"  적재: 새 Version {version_id} ({rows}행)")
        else:
            print(f"  적재 생략: 같은 내용의 Version 이 이미 있다 ({version_id})")

        # 멱등 재시도 - 같은 content_hash 는 새 Version 을 만들지 않아야 한다
        vid2, _, created2 = ref.upsert_calendar(draft)
        if created2 or vid2 != version_id:
            raise CalendarUnavailable("재적재가 새 Version 을 만들었다 - 멱등이 깨졌다")
        print("  멱등 재시도: 통과")

        today = datetime.now(KST).date()
        session = ref.market_session(today)
        print(f"  오늘({today}) 세션 조회: {session}")
        if session is None:
            raise CalendarUnavailable("적재 후에도 오늘 세션이 없다 - 조회 경로 확인")
    finally:
        ref.close()
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없이
# ---------------------------------------------------------------------------

def _check_holiday_table():
    assert all(DECLARED_FROM <= d <= DECLARED_THROUGH for d in HOLIDAYS_2026)
    assert all(d.isoweekday() < 6 for d in HOLIDAYS_2026), "휴장 목록에 주말이 있다"
    # 대체공휴일은 전부 월요일이어야 한다 (주말 원일의 다음 평일)
    for d, reason in HOLIDAYS_2026.items():
        if "대체공휴일" in reason:
            assert d.isoweekday() == 1, f"{d} {reason} 가 월요일이 아니다"
    print("  휴장 목록 무결성         OK")


def _check_generation():
    draft = build_declared_draft()
    by_date = {s.trade_date: s for s in draft.sessions}
    assert len(draft.sessions) == 365

    def trading(y, m, d):
        return by_date[date(y, m, d)].is_trading_day

    # 관측으로 이미 확인된 상반기
    assert not trading(2026, 1, 1) and trading(2026, 1, 2)
    assert not any(trading(2026, 2, x) for x in (16, 17, 18)) and trading(2026, 2, 19)
    assert not trading(2026, 6, 3), "지방선거일이 거래일로 나왔다"
    assert trading(2026, 6, 8), "현충일(토)에 없는 대체공휴일을 만들었다"  # 관측 확인
    assert not trading(2026, 7, 17), "재지정된 제헌절이 거래일로 나왔다"
    # 선언뿐인 하반기
    assert not trading(2026, 8, 17) and trading(2026, 8, 18)
    assert not trading(2026, 9, 24) and not trading(2026, 9, 25)
    assert trading(2026, 9, 28), "추석 토요일(9/26)에 없는 대체공휴일을 만들었다"
    assert not trading(2026, 10, 5) and not trading(2026, 10, 9)
    assert not trading(2026, 12, 25) and not trading(2026, 12, 31)
    assert trading(2026, 12, 30), "마지막 거래일(12/30)이 휴장으로 나왔다"

    # 특이 세션 시각
    jan2 = by_date[date(2026, 1, 2)]
    assert jan2.opens_at.hour == 10 and jan2.closes_at.hour == 15
    csat = by_date[date(2026, 11, 19)]
    assert csat.opens_at.hour == 10 and csat.closes_at.hour == 16
    normal = by_date[date(2026, 7, 31)]
    assert normal.opens_at.hour == 9 and normal.closes_at.minute == 30

    # 총 거래일 검산: 평일 수 - 평일 휴장 수 (전부 평일임은 위에서 강제)
    weekdays = sum(1 for s in draft.sessions if s.trade_date.isoweekday() < 6)
    assert draft.trading_days == weekdays - len(HOLIDAYS_2026), (
        draft.trading_days, weekdays, len(HOLIDAYS_2026)
    )
    print(f"  생성 ({draft.trading_days}거래일/365일)  OK")


def _check_range_guard():
    for bad in ((date(2025, 12, 1), DECLARED_THROUGH),
                (DECLARED_FROM, date(2027, 1, 31))):
        try:
            build_declared_draft(*bad)
            raise AssertionError(f"{bad} 가 통과했다 - 모르는 해를 추정으로 만들었다")
        except CalendarUnavailable:
            pass
    print("  범위 가드                OK")


def _check_verification():
    draft = build_declared_draft()
    observed = {
        s.trade_date: s.is_trading_day
        for s in draft.sessions if s.trade_date <= date(2026, 7, 30)
    }
    overlap, overlap_trading = verify_against_observed(draft, observed)
    assert overlap == 211 and overlap_trading == 141, (overlap, overlap_trading)

    # 하루라도 다르면 거부 - 7/17 을 관측이 '거래일'이라고 했다고 치자
    planted = dict(observed)
    planted[date(2026, 7, 17)] = True
    try:
        verify_against_observed(draft, planted)
        raise AssertionError("불일치가 통과했다")
    except CalendarUnavailable as e:
        assert "2026-07-17" in str(e)

    # 관측이 없거나 너무 짧으면 검증이 아니다
    try:
        verify_against_observed(draft, {})
        raise AssertionError("빈 관측이 통과했다")
    except CalendarUnavailable:
        pass
    short = {d: t for d, t in observed.items() if d < date(2026, 2, 1)}
    try:
        verify_against_observed(draft, short)
        raise AssertionError("한 달짜리 겹침이 검증으로 통과했다")
    except CalendarUnavailable:
        pass
    print("  관측 교차검증            OK")


def _check_hash_stability():
    a = build_declared_draft()
    b = build_declared_draft()
    assert a.content_hash == b.content_hash, "같은 선언이 다른 hash 를 냈다"
    print("  hash 안정성              OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        print(f"{DECLARED_VERSION} 적재")
        raise SystemExit(_collect())

    print(f"{DECLARED_VERSION} 자체 점검 (DB 없이)")
    _check_holiday_table()
    _check_generation()
    _check_range_guard()
    _check_verification()
    _check_hash_stability()
    print("선언 Calendar 5개 영역 통과. 적재는 --collect")
