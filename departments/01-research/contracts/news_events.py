#!/usr/bin/env python3
"""Sprint J3: 뉴스 Stream 계약 - Provider 가 WebSocket 이든 REST 폴링이든 같은 모양.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(뉴스 P0), 3.3(수집 금지),
      4.2(PIT), 5.2(research.documents), 8.2(DQ)

▶ 왜 이 계약이 필요한가 (2026-07-31)
  뉴스 Source 마다 전송 방식이 다르다.
    - Alpaca: 진짜 WebSocket (wss://stream.data.alpaca.markets/v1beta1/news)
    - NAVER 검색 API: REST GET 만. display<=100, start<=1000, 일 25,000회
    - BIGKinds: REST(JSON/XML) 만. API 이용은 유료 회원
  **한국 P0 Source 중 WebSocket 을 주는 곳이 없다.** 그렇다고 호출부가 Source 마다
  다른 모양으로 붙으면, 나중에 Source 를 바꿀 때 하류가 전부 깨진다.

  그래서 전송 방식을 Adapter 안으로 감추고 **호출부는 항상 push(on_record) 로만**
  받는다. 폴링 Source 는 Adapter 가 Cursor 를 들고 새 기사만 밀어 올린다.

▶ 폴링을 Stream 으로 바꿀 때 반드시 지켜야 하는 것
  1. **중복을 하류로 내보내지 않는다.** 같은 기사를 두 번 밀면 Story Cluster 와
     Agent 근거 개수가 부풀려진다. Cursor + 최근 ID 창으로 막는다.
  2. **빈 응답과 실패를 구분한다.** 폴링이 실패했는데 0건으로 넘기면 "뉴스가
     없었다" 가 되어 조용히 통과한다(개발 원칙 9).
  3. **observed_at 은 우리가 본 시각이다.** 폴링 주기가 길면 published_at 과
     벌어지는데, 그 격차를 숨기지 않고 Stats 에 남긴다(4.2 PIT).
  4. **본문을 계약에 담지 않는다.** Source 별 allowed_uses 가 다르고, 담을 수
     있게 해 두면 언젠가 저장된다(3.3).

자체 점검: python departments/01-research/contracts/news_events.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol, runtime_checkable

SCHEMA_VERSION = 1
CONTRACT_ID = "research-news-events-v1"

# Cursor 가 기억하는 최근 기사 ID 개수. 같은 published_at 을 가진 기사가 여러 건일 때
# Cursor 시각만으로는 경계를 못 가르므로 ID 창을 함께 쓴다. 폴링 1회 최대 건수보다
# 넉넉해야 한다(NAVER display 최대 100).
DEDUP_WINDOW = 2000

# published_at 이 관측 시각보다 이만큼 미래면 공급자 시계가 어긋난 것이다.
MAX_FUTURE_SKEW = timedelta(minutes=10)

# 이만큼 오래된 기사는 Stream 이 밀어 올리지 않는다. 폴링 Source 가 정렬을 바꾸거나
# 과거 페이지를 다시 주면 오래된 기사가 '새 기사' 로 흘러들어간다.
MAX_BACKFILL_AGE = timedelta(days=3)


class NewsStreamError(RuntimeError):
    """뉴스 Stream 실패. 빈 결과로 바꾸지 않는다."""


class Transport(StrEnum):
    """전송 방식. 호출부는 몰라도 되지만 DQ·운영에는 기록한다."""

    WEBSOCKET = "WEBSOCKET"
    POLLING = "POLLING"


class NewsQuality(StrEnum):
    DUPLICATE = "DUPLICATE"                 # 이미 본 기사
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"   # published_at 이 미래
    TOO_OLD = "TOO_OLD"                     # 창 밖의 과거 기사
    TITLE_EMPTY = "TITLE_EMPTY"
    URL_MISSING = "URL_MISSING"
    REVISED = "REVISED"                     # 같은 ID 인데 제목이 바뀜


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_title(raw: str) -> str:
    """공급자 제목을 정규화한다.

    NAVER 는 검색어를 <b> 태그로 감싸 돌려주고 &quot; 같은 Entity 도 섞인다.
    태그를 남겨두면 같은 기사가 검색어에 따라 다른 문자열이 되어 중복 판정이 깨진다.
    """
    import html

    s = _TAG.sub("", raw or "")
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def build_external_id(provider: str, provider_id: str) -> str:
    """Provider 안에서 유일한 ID. research.documents 의 external_id 다.

    Provider 가 안정적인 기사 ID 를 주면 그것을 쓴다. 안 주는 곳(NAVER 는 기사
    ID 가 없다)은 호출자가 URL 같은 안정 키를 넘긴다 - **제목을 키로 쓰지 않는다.**
    제목은 정정으로 바뀌므로 키가 되면 같은 기사가 둘로 늘어난다.
    """
    pid = (provider_id or "").strip()
    if not pid:
        raise NewsStreamError(f"{provider}: provider_id 가 비었다")
    if len(pid) > 120:
        pid = hashlib.sha256(pid.encode()).hexdigest()[:40]
    return f"{provider}:{pid}"


@dataclass(frozen=True)
class NewsRecord:
    """정규 뉴스 항목. research.documents 한 행으로 간다.

    **본문(content)과 요약(summary) 필드가 없다.** Source 별 사용권이 달라서
    계약에 자리를 만들면 권한 없는 Source 의 본문까지 흘러든다(가이드 3.3).
    있었는지 여부만 had_content/had_summary 로 남긴다.
    """

    external_id: str
    title: str
    canonical_url: str | None
    published_at: datetime
    observed_at: datetime
    language: str
    provider: str
    publisher: str = ""
    author: str = ""
    symbols: tuple[str, ...] = ()
    document_type: str = "NEWS"
    status: str = "ACTIVE"
    had_summary: bool = False
    had_content: bool = False
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise NewsStreamError(f"{self.external_id}: title 이 비었다")
        for name in ("published_at", "observed_at"):
            v = getattr(self, name)
            if v.tzinfo is None:
                raise NewsStreamError(f"{self.external_id}: {name} 에 Timezone 이 없다")

    @property
    def ingest_lag(self) -> timedelta:
        return self.observed_at - self.published_at


@dataclass
class StreamCursor:
    """폴링 Source 가 중복 없이 이어받기 위한 상태.

    시각만으로는 부족하다 - 같은 초에 발행된 기사가 여러 건이면 경계에서 하나가
    빠지거나 겹친다. 그래서 시각과 최근 ID 창을 함께 본다.
    """

    last_published_at: datetime | None = None
    _seen: deque = field(default_factory=lambda: deque(maxlen=DEDUP_WINDOW))
    _seen_set: set = field(default_factory=set)

    @classmethod
    def sized(cls, window: int) -> "StreamCursor":
        """dedup 창 크기를 지정한 Cursor.

        기본 창(DEDUP_WINDOW=2,000)은 단일 질의용 가정이다. Watch sweep 는
        종목수 × display 가 한 번에 흐르므로 **창이 한 sweep 보다 작으면 직전
        sweep 기사가 창에서 밀려나 매번 재방출된다** - 실측 2026-07-31: 바스켓
        350종목 sweep 2 가 3,585건을 다시 밀어냈고 신규는 314건뿐이었다
        (멱등 upsert 라 데이터는 안 깨지지만 하루 19만 회의 헛 UPDATE 가 된다).
        """
        if window < 1:
            raise ValueError(f"dedup 창은 1 이상이어야 한다: {window}")
        c = cls()
        c._seen = deque(maxlen=window)
        return c

    def has_seen(self, external_id: str) -> bool:
        return external_id in self._seen_set

    def remember(self, record: NewsRecord) -> None:
        if len(self._seen) == self._seen.maxlen and self._seen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(record.external_id)
        self._seen_set.add(record.external_id)
        if self.last_published_at is None or record.published_at > self.last_published_at:
            self.last_published_at = record.published_at

    @property
    def size(self) -> int:
        return len(self._seen_set)


@dataclass
class StreamStats:
    """한 번의 Stream 실행 결과. 축소를 숨기지 않는다(가이드 8.2)."""

    source_id: str
    transport: Transport
    seen: int = 0            # Provider 가 준 항목 수
    emitted: int = 0         # 하류로 밀어 올린 수
    duplicates: int = 0
    rejected: int = 0        # 계약 위반으로 버린 수
    polls: int = 0
    errors: int = 0
    flags: dict = field(default_factory=dict)
    max_lag: timedelta = timedelta(0)

    def flag(self, f: NewsQuality) -> None:
        self.flags[str(f)] = self.flags.get(str(f), 0) + 1

    def summary(self) -> str:
        lag = f"{self.max_lag.total_seconds():.0f}s"
        base = (
            f"[{self.source_id}/{self.transport}] 수신 {self.seen} 전달 {self.emitted} "
            f"중복 {self.duplicates} 거부 {self.rejected} 최대지연 {lag}"
        )
        if self.polls:
            base += f" 폴링 {self.polls}"
        if self.errors:
            base += f" 오류 {self.errors}"
        if self.flags:
            base += f" {self.flags}"
        return base


@runtime_checkable
class NewsStream(Protocol):
    """뉴스 Source 하나를 흐름으로 본다.

    호출부는 run() 만 안다. WebSocket 인지 폴링인지 구분하지 않으며, 구분해야 하는
    코드가 생기면 그건 이 계약이 새고 있다는 뜻이다.
    """

    source_id: str
    transport: Transport

    def run(
        self,
        *,
        on_record,
        max_seconds: float | None = None,
        max_records: int | None = None,
    ) -> StreamStats:
        ...


def admit(
    record: NewsRecord,
    cursor: StreamCursor,
    stats: StreamStats,
    *,
    now: datetime | None = None,
) -> bool:
    """이 기사를 하류로 밀어도 되는지. 여기가 WebSocket 과 폴링의 유일한 공통 관문이다.

    False 면 stats 에 이유가 남는다 - 조용히 사라지는 항목이 없어야 한다.
    """
    now = now or datetime.now(timezone.utc)

    if cursor.has_seen(record.external_id):
        stats.duplicates += 1
        stats.flag(NewsQuality.DUPLICATE)
        return False

    if record.published_at > now + MAX_FUTURE_SKEW:
        stats.rejected += 1
        stats.flag(NewsQuality.FUTURE_TIMESTAMP)
        return False

    if now - record.published_at > MAX_BACKFILL_AGE:
        stats.rejected += 1
        stats.flag(NewsQuality.TOO_OLD)
        return False

    if record.canonical_url is None:
        # 버리지는 않는다. URL 이 없어도 제목·시각은 근거가 된다.
        stats.flag(NewsQuality.URL_MISSING)

    cursor.remember(record)
    stats.emitted += 1
    lag = record.ingest_lag
    if lag > stats.max_lag:
        stats.max_lag = lag
    return True


# ---------------------------------------------------------------------------
# 폴링을 Stream 으로 바꾸는 Adapter
# ---------------------------------------------------------------------------

class PollingNewsStream:
    """REST 폴링을 push 로 바꾼다. WebSocket Adapter 와 같은 인터페이스다.

    fetch_page(cursor) -> list[NewsRecord] 를 주면 나머지는 이 클래스가 한다.
    Provider 별로 다른 것은 fetch_page 안에만 있어야 한다.
    """

    def __init__(
        self,
        *,
        source_id: str,
        fetch_page,
        interval_seconds: float,
        cursor: StreamCursor | None = None,
        sleep=None,
        clock=None,
        now=None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 는 0 보다 커야 한다")
        self.source_id = source_id
        self.transport = Transport.POLLING
        self._fetch_page = fetch_page
        self._interval = interval_seconds
        self.cursor = cursor or StreamCursor()
        # 테스트에서 실제로 자거나 벽시계를 보지 않게 주입 가능하게 둔다.
        # now 는 admit 의 나이·미래 판정 기준이라 clock(단조시계)과 별개다.
        self._sleep = sleep
        self._clock = clock
        self._now = now

    def run(
        self,
        *,
        on_record,
        max_seconds: float | None = None,
        max_records: int | None = None,
    ) -> StreamStats:
        import time as _time

        sleep = self._sleep or _time.sleep
        clock = self._clock or _time.monotonic

        stats = StreamStats(self.source_id, Transport.POLLING)
        started = clock()

        while True:
            if max_records is not None and stats.emitted >= max_records:
                return stats
            if max_seconds is not None and clock() - started >= max_seconds:
                return stats

            stats.polls += 1
            # **실패를 0건으로 바꾸지 않는다.** 예외를 그대로 올린다 - 폴링이 계속
            # 실패하는데 "뉴스가 없었다" 로 보이는 것이 이 도메인에서 제일 위험하다.
            page = self._fetch_page(self.cursor)
            stats.seen += len(page)

            now = self._now() if self._now else None
            for rec in page:
                if admit(rec, self.cursor, stats, now=now):
                    on_record(rec)
                    if max_records is not None and stats.emitted >= max_records:
                        return stats

            if max_seconds is not None and clock() - started + self._interval > max_seconds:
                return stats
            sleep(self._interval)


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

def _dt(h, m=0, s=0, day=30) -> datetime:
    return datetime(2026, 7, day, h, m, s, tzinfo=timezone.utc)


def _rec(i: int, *, pub=None, obs=None, title=None, url="https://x.invalid/a") -> NewsRecord:
    return NewsRecord(
        external_id=f"t:{i}",
        title=title or f"기사 {i}",
        canonical_url=url,
        published_at=pub or _dt(10, 0, i),
        observed_at=obs or _dt(10, 1, 0),
        language="ko",
        provider="t",
    )


def _check_record_contract():
    r = _rec(1)
    assert r.ingest_lag == timedelta(seconds=60 - 1)
    # 본문·요약 자리가 계약에 없어야 한다
    fields = set(NewsRecord.__dataclass_fields__)
    assert "content" not in fields and "summary" not in fields
    assert {"had_content", "had_summary"} <= fields

    # _rec 헬퍼는 title=None 을 기본값으로 바꾸므로 여기서는 직접 만든다
    for bad in ("", "   ", "\n\t"):
        try:
            NewsRecord(external_id="t:2", title=bad, canonical_url=None,
                       published_at=_dt(10, 0), observed_at=_dt(10, 1),
                       language="ko", provider="t")
            raise AssertionError(f"title={bad!r} 가 통과했다")
        except NewsStreamError:
            pass

    # Timezone 없는 시각은 PIT 에서 위험하다
    try:
        NewsRecord(external_id="t:3", title="x", canonical_url=None,
                   published_at=datetime(2026, 7, 30, 10, 0), observed_at=_dt(10, 1),
                   language="ko", provider="t")
        raise AssertionError("naive published_at 이 통과했다")
    except NewsStreamError as e:
        assert "Timezone" in str(e)
    print("  Record 계약              OK")


def _check_title_cleaning():
    # NAVER 는 검색어를 <b> 로 감싸 준다. 그대로 두면 검색어마다 다른 문자열이 된다.
    assert clean_title("<b>삼성전자</b>, 2분기 실적 발표") == "삼성전자, 2분기 실적 발표"
    assert clean_title("A &quot;B&quot; C") == 'A "B" C'
    assert clean_title("  줄  간격   정리  ") == "줄 간격 정리"
    assert clean_title("&amp;lt;") == "&lt;"
    # 같은 기사가 검색어에 따라 다르게 와도 같은 문자열이 돼야 한다
    a = clean_title("<b>삼성</b>전자 실적")
    b = clean_title("삼성<b>전자</b> 실적")
    assert a == b == "삼성전자 실적"
    print("  제목 정규화              OK")


def _check_external_id():
    assert build_external_id("naver", "https://n.news/1") == "naver:https://n.news/1"
    # 긴 키는 해시로 접는다(external_id 가 무한정 길면 Index 가 망가진다)
    long_id = build_external_id("naver", "https://n.news/" + "x" * 200)
    assert len(long_id) == len("naver:") + 40
    # 같은 입력은 항상 같은 결과여야 한다
    assert long_id == build_external_id("naver", "https://n.news/" + "x" * 200)
    for bad in ("", "   ", None):
        try:
            build_external_id("naver", bad)
            raise AssertionError(f"{bad!r} 가 통과했다")
        except NewsStreamError:
            pass
    print("  external_id              OK")


def _check_admit():
    cur, st = StreamCursor(), StreamStats("t", Transport.POLLING)
    now = _dt(10, 1)

    assert admit(_rec(1), cur, st, now=now) is True
    assert st.emitted == 1
    # 같은 기사를 다시 주면 하류로 안 간다
    assert admit(_rec(1), cur, st, now=now) is False
    assert st.duplicates == 1 and st.flags["DUPLICATE"] == 1

    # 미래 시각은 공급자 시계가 어긋난 것이다
    assert admit(_rec(2, pub=_dt(10, 30)), cur, st, now=now) is False
    assert st.flags["FUTURE_TIMESTAMP"] == 1
    # 10분 이내 미래는 허용(시계 오차 여유)
    assert admit(_rec(3, pub=_dt(10, 5)), cur, st, now=now) is True

    # 창 밖의 과거는 '새 기사' 가 아니다
    assert admit(_rec(4, pub=_dt(10, 0, 0, day=20)), cur, st, now=now) is False
    assert st.flags["TOO_OLD"] == 1

    # URL 이 없어도 버리지는 않는다. 흔적만 남긴다.
    assert admit(_rec(5, url=None), cur, st, now=now) is True
    assert st.flags["URL_MISSING"] == 1

    # 최대 지연이 기록돼야 한다
    assert st.max_lag > timedelta(0)
    print("  admit 관문               OK")


def _check_cursor_window():
    cur = StreamCursor()
    st = StreamStats("t", Transport.POLLING)
    now = _dt(23, 59)
    # 창 크기를 넘겨도 set 과 deque 가 어긋나면 안 된다(메모리 누수 / 오판정)
    for i in range(DEDUP_WINDOW + 500):
        r = NewsRecord(external_id=f"t:{i}", title=f"x{i}", canonical_url=None,
                       published_at=_dt(10, 0), observed_at=_dt(10, 1),
                       language="ko", provider="t")
        admit(r, cur, st, now=now)
    assert cur.size == DEDUP_WINDOW, cur.size
    assert len(cur._seen) == len(cur._seen_set) == DEDUP_WINDOW
    # 가장 오래된 것은 잊어야 한다(창 밖이므로 다시 받아들여진다)
    assert not cur.has_seen("t:0")
    assert cur.has_seen(f"t:{DEDUP_WINDOW + 499}")

    # sized(): 큰 sweep 용 창. 기본 창이면 밀려났을 ID 가 창 안에 남아야 한다
    big = StreamCursor.sized(DEDUP_WINDOW + 1000)
    st2 = StreamStats("t", Transport.POLLING)
    for i in range(DEDUP_WINDOW + 500):
        r = NewsRecord(external_id=f"b:{i}", title=f"y{i}", canonical_url=None,
                       published_at=_dt(10, 0), observed_at=_dt(10, 1),
                       language="ko", provider="t")
        admit(r, big, st2, now=now)
    assert big.size == DEDUP_WINDOW + 500
    assert big.has_seen("b:0"), "sized 창인데 첫 sweep 가 밀려났다 - 재방출 회귀"
    try:
        StreamCursor.sized(0)
        raise AssertionError("창 0 이 통과했다")
    except ValueError:
        pass
    print("  Cursor 창 관리           OK")


def _check_polling_stream():
    """폴링이 WebSocket 과 같은 모양으로 동작하는지."""
    pages = [
        [_rec(1), _rec(2)],
        [_rec(2), _rec(3)],   # 겹치는 페이지 - 중복이 하류로 가면 안 된다
        [_rec(3), _rec(4)],
    ]
    calls = {"n": 0}

    def fetch(cursor):
        i = calls["n"]
        calls["n"] += 1
        return pages[i] if i < len(pages) else []

    slept = []
    ticks = {"t": 0.0}

    def clock():
        return ticks["t"]

    def sleep(s):
        slept.append(s)
        ticks["t"] += s

    got = []
    # now 를 고정한다 - 안 그러면 MAX_BACKFILL_AGE 때문에 며칠 뒤 이 테스트가 깨진다
    s = PollingNewsStream(source_id="t", fetch_page=fetch, interval_seconds=1.0,
                          sleep=sleep, clock=clock, now=lambda: _dt(10, 1))
    stats = s.run(on_record=got.append, max_records=4)
    assert [r.external_id for r in got] == ["t:1", "t:2", "t:3", "t:4"], got
    assert stats.emitted == 4 and stats.duplicates == 2
    assert stats.seen == 6 and stats.polls == 3
    assert stats.transport is Transport.POLLING
    assert isinstance(s, NewsStream), "Protocol 을 만족하지 않는다"
    print("  폴링 -> Stream           OK")


def _check_polling_failure_not_silent():
    """폴링 실패를 0건으로 바꾸지 않는지."""
    def boom(cursor):
        raise NewsStreamError("upstream 500")

    s = PollingNewsStream(source_id="t", fetch_page=boom, interval_seconds=1.0,
                          sleep=lambda _: None, clock=lambda: 0.0)
    try:
        s.run(on_record=lambda r: None, max_records=1)
        raise AssertionError("폴링 실패가 조용히 통과했다")
    except NewsStreamError as e:
        assert "upstream" in str(e)

    try:
        PollingNewsStream(source_id="t", fetch_page=boom, interval_seconds=0)
        raise AssertionError("interval 0 이 통과했다")
    except ValueError:
        pass
    print("  폴링 실패 전파           OK")


def _check_stats_summary():
    st = StreamStats("naver", Transport.POLLING, seen=10, emitted=7,
                     duplicates=2, rejected=1, polls=3)
    st.flag(NewsQuality.DUPLICATE)
    st.max_lag = timedelta(seconds=125)
    out = st.summary()
    for token in ("naver", "POLLING", "수신 10", "전달 7", "중복 2", "거부 1", "125s", "폴링 3"):
        assert token in out, f"{token} 이 요약에 없다: {out}"
    print("  Stats 요약               OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{CONTRACT_ID} 자체 점검 (외부 호출 없음)")
    _check_record_contract()
    _check_title_cleaning()
    _check_external_id()
    _check_admit()
    _check_cursor_window()
    _check_polling_stream()
    _check_polling_failure_not_silent()
    _check_stats_summary()
    print("뉴스 Stream 계약 8개 영역 통과")
