#!/usr/bin/env python3
"""뉴스 Stream 을 DB 로 흘려보내는 Sink - 기사가 들어오는 즉시 적재한다.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(뉴스 P0), 4.2(PIT), 8.2(DQ)
      departments/01-research/contracts/news_events.py (NewsStream 계약)

▶ 왜 필요한가
  지금까지 수집기는 Stream 을 다 돌린 **뒤에** 모아서 한 번에 적재했다. 그러면
  Stream 이 도는 동안 DB 에 아무것도 없고, 프로세스가 죽으면 그때까지 받은 것을
  통째로 잃는다. 실시간으로 쓰려면 밀려온 순간 넣어야 한다.

▶ 그렇다고 한 건씩 넣지 않는다
  기사 하나마다 왕복하면 초기 백필(수백 건)에서 DB 왕복이 그만큼 늘어난다.
  **크기 또는 시간 중 먼저 오는 쪽으로 Flush** 해서 지연을 상한으로 묶는다.
  max_batch=1 로 두면 진짜 건건이 넣는 것도 된다.

▶ 절대 하지 않는 것
  1. **버퍼에 남은 것을 버리지 않는다.** close()/컨텍스트 종료에서 반드시 Flush 한다.
     안 그러면 마지막 몇 건이 조용히 사라진다.
  2. **적재 실패를 삼키지 않는다.** 예외를 올린다 - 실패했는데 Stream 이 계속
     돌면 "수집은 됐는데 DB 에 없는" 상태가 된다(개발 원칙 9).
  3. **Provider 별 종목 매핑을 여기 넣지 않는다.** NAVER 는 KRX 코드로, Alpaca 는
     미국 심볼로 온다. link_resolver 를 주입받아 Sink 는 매핑을 모른다.

자체 점검(DB 없이): python departments/01-research/collectors/news_pipeline.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
from news_events import NewsRecord, NewsStreamError, StreamStats

PIPELINE_VERSION = "research-news-pipeline-v1"

# 기본 Flush 정책. 뉴스 유입은 분당 몇 건 수준이라 이 정도면 지연이 5초 안쪽이다.
DEFAULT_MAX_BATCH = 20
DEFAULT_MAX_DELAY_SECONDS = 5.0


class NewsSinkError(NewsStreamError):
    """적재 실패. 빈 결과로 바꾸지 않는다."""


@dataclass
class SinkStats:
    """Sink 가 무엇을 했는지. 축소를 숨기지 않는다(가이드 8.2)."""

    received: int = 0
    documents_new: int = 0
    documents_updated: int = 0
    links_new: int = 0
    links_attempted: int = 0
    issuers_linked: int = 0
    revisions: int = 0
    flushes: int = 0
    title_dups: int = 0           # 제목 창이 걸러낸 재전송·재게재
    unresolved_symbols: set = field(default_factory=set)

    def summary(self) -> str:
        base = (
            f"수신 {self.received} 신규 {self.documents_new} 갱신 {self.documents_updated} "
            f"연결 {self.links_new}/{self.links_attempted} Flush {self.flushes}"
        )
        if self.issuers_linked:
            base += f" issuer {self.issuers_linked}"
        if self.revisions:
            base += f" 정정 {self.revisions}"
        if self.title_dups:
            base += f" 제목중복 {self.title_dups}"
        if self.unresolved_symbols:
            n = len(self.unresolved_symbols)
            sample = ", ".join(sorted(self.unresolved_symbols)[:6])
            base += f" 미해결심볼 {n}({sample})"
        return base


class NewsSink:
    """Stream 에서 밀려온 Record 를 DB 로 넣는다.

    `on_record` 로 그대로 넘길 수 있게 callable 이다. WebSocket 이든 폴링이든
    호출부는 이 Sink 를 똑같이 쓴다.

        with NewsSink(ref, source_id=src, link_resolver=r) as sink:
            stream.run(on_record=sink, max_seconds=600)
        print(sink.stats.summary())
    """

    def __init__(
        self,
        ref,
        *,
        source_id,
        link_resolver=None,
        issuer_resolver=None,
        max_batch: int = DEFAULT_MAX_BATCH,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        clock=None,
        on_flush=None,
        title_dedup_window: int = 0,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch 는 1 이상이어야 한다")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds 는 0 보다 커야 한다")

        self._ref = ref
        self._source_id = source_id
        # record -> [(instrument_id, relation_type, confidence)]
        self._link_resolver = link_resolver
        # record -> issuer_id | None
        self._issuer_resolver = issuer_resolver
        self._max_batch = max_batch
        self._max_delay = max_delay_seconds
        self._clock = clock
        self._on_flush = on_flush

        self._buf: list[NewsRecord] = []
        self._buf_started: float | None = None
        self._closed = False
        self.stats = SinkStats()
        # 제목 기반 중복 창 (재일님 지적 2026-07-31: LS 재전송·NAVER 재게재가
        # 다른 external_id 로 들어와 소스 내 중복 270행이 쌓였다 - DB 소급 제거
        # 후 재발을 여기서 막는다). 키 = (제목, 게시 날짜). 0 이면 비활성.
        from collections import deque
        self._title_window = title_dedup_window
        self._seen_titles: deque = deque(maxlen=max(title_dedup_window, 1))
        self._seen_title_set: set = set()

    # -- callable: on_record 로 그대로 넘길 수 있다 ---------------------------

    def __call__(self, record: NewsRecord) -> None:
        self.add(record)

    def seed_titles(self, keys) -> int:
        """재기동 예열: DB 에 이미 있는 (제목, 게시일) 로 창을 채운다.

        실측 2026-07-31: 컨테이너 재배포(12:09 UTC) 직후 창이 비어 재방출분
        42행이 그 시간대에 전부 재적재됐다. 기동 시 최근 기사로 예열하면
        재기동이 중복 창의 구멍이 되지 않는다. 오래된 것부터 넣어야 deque 가
        최신 키를 밀어내지 않는다.
        """
        if not self._title_window:
            return 0
        seeded = 0
        for key in keys:
            key = (key[0], key[1])
            if key in self._seen_title_set:
                continue
            if len(self._seen_titles) == self._seen_titles.maxlen:
                self._seen_title_set.discard(self._seen_titles[0])
            self._seen_titles.append(key)
            self._seen_title_set.add(key)
            seeded += 1
        return seeded

    def add(self, record: NewsRecord) -> None:
        if self._closed:
            raise NewsSinkError("닫힌 Sink 에 기사를 넣으려 했다")

        if self._title_window:
            key = (record.title, record.published_at.date())
            if key in self._seen_title_set:
                self.stats.title_dups += 1
                return
            if len(self._seen_titles) == self._seen_titles.maxlen:
                self._seen_title_set.discard(self._seen_titles[0])
            self._seen_titles.append(key)
            self._seen_title_set.add(key)

        now = self._now()
        self._buf.append(record)
        self.stats.received += 1
        if self._buf_started is None:
            self._buf_started = now

        if len(self._buf) >= self._max_batch or (now - self._buf_started) >= self._max_delay:
            self.flush()

    # -- 시간 경과만으로도 Flush 되게 --------------------------------------

    def tick(self) -> None:
        """기사가 안 들어와도 시간이 지나면 Flush 한다.

        add() 안에서만 시간을 보면 **마지막 기사 뒤에 아무것도 안 오는 동안**
        버퍼가 계속 남는다. Stream 이 한가할 때 이걸 불러준다.
        """
        if self._buf and self._buf_started is not None:
            if (self._now() - self._buf_started) >= self._max_delay:
                self.flush()

    def flush(self) -> None:
        """버퍼를 DB 로 밀어 넣는다. 실패하면 예외이며 버퍼를 비우지 않는다."""
        if not self._buf:
            return

        batch = self._buf
        # 같은 external_id 가 한 배치에 두 번 있으면 upsert 가 같은 행을 두 번
        # 건드려 실패한다(DART 재무에서 실제로 겪었다). 뒤에 온 것을 남긴다.
        uniq: dict[str, NewsRecord] = {}
        for r in batch:
            uniq[r.external_id] = r
        records = list(uniq.values())

        issuer_by_ext = {}
        if self._issuer_resolver is not None:
            for r in records:
                iid = self._issuer_resolver(r)
                if iid is not None:
                    issuer_by_ext[r.external_id] = iid

        new, updated, id_by_ext = self._ref.upsert_news_documents(
            records, source_id=self._source_id, issuer_by_external_id=issuer_by_ext
        )

        links = []
        if self._link_resolver is not None:
            for r in records:
                did = id_by_ext.get(r.external_id)
                if did is None:
                    # upsert 가 id 를 안 돌려준 건 계약 위반이다. 조용히 넘기지 않는다.
                    raise NewsSinkError(
                        f"document_id 를 못 받았다: {r.external_id} - upsert 반환 확인"
                    )
                resolved = self._link_resolver(r)
                for instrument_id, relation, confidence in resolved:
                    links.append((did, instrument_id, relation, confidence))
                if not resolved and r.symbols:
                    self.stats.unresolved_symbols.update(r.symbols)

        added = 0
        if links:
            deduped = {(d, i, rt): (d, i, rt, c) for d, i, rt, c in links}
            added = self._ref.link_documents_to_instruments(list(deduped.values()))
            self.stats.links_attempted += len(deduped)

        # 여기까지 왔으면 성공이다. 이제 비운다 - 예외가 나면 버퍼가 남아 재시도된다.
        self._buf = []
        self._buf_started = None

        self.stats.documents_new += new
        self.stats.documents_updated += updated
        self.stats.links_new += added
        self.stats.issuers_linked += len(issuer_by_ext)
        self.stats.revisions += len(getattr(self._ref, "last_revisions", ()) or ())
        self.stats.flushes += 1

        if self._on_flush is not None:
            self._on_flush(records, new, updated, added)

    @property
    def pending(self) -> int:
        """아직 DB 로 나가지 못한 버퍼 건수. 종료 경로가 유실을 셀 때 쓴다."""
        return len(self._buf)

    def rebind(self, ref) -> None:
        """DB 재접속 후 Repository 를 갈아끼운다.

        상주 서비스에서 접속이 죽으면 flush 가 실패하며 버퍼는 남는다(위 flush
        참고). 새 접속으로 갈아끼우면 **버퍼가 그대로 다음 flush 에서 재시도**된다
        - Sink 를 새로 만들면 버퍼와 통계를 잃으므로 그렇게 하지 않는다.
        """
        if self._closed:
            raise NewsSinkError("닫힌 Sink 는 rebind 할 수 없다")
        self._ref = ref

    def close(self) -> None:
        """남은 버퍼를 반드시 밀어 넣고 닫는다."""
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True

    def __enter__(self) -> NewsSink:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 예외로 빠져나가도 받은 것은 남긴다. 단 Flush 가 또 실패하면 그건 올린다.
        self.close()
        return False

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        import time as _time

        return _time.monotonic()


# ---------------------------------------------------------------------------
# Stream <-> Sink 연결
# ---------------------------------------------------------------------------

def seed_title_window_from_db(sink: NewsSink, conn, source_id) -> int:
    """상주 서비스 기동 시 제목 창을 DB 최근 기사로 예열한다.

    게시일은 Asia/Seoul 기준으로 뽑는다 - add() 의 키가
    record.published_at.date() 이고 두 한국 소스(NAVER·LS) 레코드가 KST
    tz 를 달고 오기 때문에, UTC date 로 뽑으면 자정 전후 기사가 어긋난다.
    """
    if not sink._title_window:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            select title, (published_at at time zone 'Asia/Seoul')::date
            from research.documents
            where source_id = %s and document_type = 'NEWS'
            order by observed_at desc
            limit %s
            """,
            (source_id, sink._title_window),
        )
        rows = cur.fetchall()
    return sink.seed_titles(reversed(rows))


def run_pipeline(
    stream,
    sink: NewsSink,
    *,
    max_seconds: float | None = None,
    max_records: int | None = None,
) -> tuple[StreamStats, SinkStats]:
    """Stream 을 돌리면서 들어오는 즉시 Sink 로 넘긴다.

    Stream 이 WebSocket 이든 폴링이든 이 함수는 같다 - NewsStream 계약 덕이다.
    """
    with sink:
        stream_stats = stream.run(
            on_record=sink, max_seconds=max_seconds, max_records=max_records
        )
    return stream_stats, sink.stats


# ---------------------------------------------------------------------------
# Provider 별 link_resolver
# ---------------------------------------------------------------------------

def title_has_standalone(name: str, title: str, other_names) -> bool:
    """제목에 종목명이 **독립적으로** 등장하는가.

    `name in title` 만 보면 '두산에너빌리티 수주' 제목에서 '두산'(000150)이
    DEDICATED 가 된다 - 짧은 이름이 더 긴 다른 종목명의 부분 문자열인 오탐이다
    (재일님 지적 2026-07-31: 종목코드와 내용이 안 맞는 연결). 등장 위치마다,
    그 위치를 덮는 더 긴 이름이 감시 목록에 있으면 그 등장은 무효로 친다.
    '두산에너빌리티와 두산 동반 상승' 처럼 둘 다 나오면 둘 다 유효다.
    """
    longer = [n for n in other_names if len(n) > len(name) and name in n]
    start = 0
    while True:
        i = title.find(name, start)
        if i < 0:
            return False
        covered = False
        for ln in longer:
            off = ln.find(name)
            while off >= 0 and not covered:
                j = i - off
                if j >= 0 and title.startswith(ln, j):
                    covered = True
                off = ln.find(name, off + 1)
            if covered:
                break
        if not covered:
            return True
        start = i + 1


# 제목에 종목명이 없는 연결의 신뢰도. NAVER 는 본문까지 검색하므로 질의에 걸렸어도
# 제목에 이름이 없으면 '본문 어딘가에서 언급' 추정일 뿐이다 - 0.5 는 과신이었다
# (재일님 지적 2026-07-31). 본문은 저장하지 않으므로(3.3) 더 확인할 수 없고,
# 확인 못 하는 것을 높게 치지 않는다.
BODY_MATCH_CONFIDENCE = "0.3"

# ── 종목명 별칭 체계 (재일님 지적 2026-07-31: 'LG CNS' 기사가 LG씨엔에스가
#    아니라 'LG' 오탐으로 물림 - Master 한글 표기와 기사 표기의 불일치) ──
#
# 3단 구조로 관리한다:
#  1. 규칙 자동 파생(아래 derive_roman_aliases) - 한글 음차를 로마자로 되돌린다.
#     'LG씨엔에스' -> 'LG CNS'/'LGCNS', '케이티앤지' -> 'KT&G'. 결정론이고
#     종목이 늘어도 손댈 게 없다.
#  2. 수동 확정 목록(NAME_ALIASES) - 규칙으로 못 만드는 통용 표기만. 속어·약칭
#     추정은 넣지 않는다(오탐 제조기가 된다).
#  3. (승격 예정) reference DB - 별칭이 자산이 되면 instrument_symbols 의
#     NAME_ALIAS 유형 또는 issuers.metadata 로 옮기고 수집기는 읽기만 한다.

# 한글 음차 -> 로마자. **2음절 이상 연속으로 매핑될 때만** 변환한다 -
# '이수페타시스' 의 '이' 한 글자를 E 로 바꾸는 오탐을 막는다. 긴 것부터 매칭.
_ROMAN_SYLLABLES: tuple[tuple[str, str], ...] = (
    ("더블유", "W"), ("에이치", "H"), ("에프", "F"), ("에스", "S"), ("에이", "A"),
    ("엠", "M"), ("엔", "N"), ("엘", "L"), ("알", "R"), ("아이", "I"),
    ("제이", "J"), ("제트", "Z"), ("케이", "K"), ("큐", "Q"), ("티", "T"),
    ("피", "P"), ("브이", "V"), ("와이", "Y"), ("엑스", "X"), ("유", "U"),
    ("비", "B"), ("씨", "C"), ("디", "D"), ("지", "G"), ("오", "O"), ("이", "E"),
    ("앤", "&"),
)

NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "현대차": ("현대자동차",),
    "POSCO홀딩스": ("포스코홀딩스",),
    "LG에너지솔루션": ("LG엔솔",),   # 언론 통용 축약 - 공식에 준해 확정
}


def derive_roman_aliases(name: str) -> set[str]:
    """한글 음차 구간(2음절 이상 연속 매핑)을 로마자로 되돌린 별칭들.

    'LG씨엔에스' -> {'LGCNS', 'LG CNS'} (붙임/띄어쓰기 둘 다 - 기사 표기가 갈린다).
    매핑이 안 되는 글자가 섞이면 그 구간은 건드리지 않는다.
    """
    out: set[str] = set()
    i = 0
    prefix = name
    # 가장 이른 위치의 연속 매핑 구간 하나를 찾아 변환한다(종목명에 구간이 둘
    # 이상인 경우는 실측에서 없다 - 나오면 그때 확장한다)
    while i < len(name):
        j, letters = i, []
        while j < len(name):
            for syl, rom in _ROMAN_SYLLABLES:
                if name.startswith(syl, j):
                    letters.append(rom)
                    j += len(syl)
                    break
            else:
                break
        if len(letters) >= 2:
            head, tail = name[:i], name[j:]
            roman = "".join(letters)
            out.add(head + roman + tail)
            if head or tail:
                out.add((head + " " + roman + " " + tail).strip())
            break
        i += 1
    out.discard(name)
    return out


def names_for(display_name: str) -> tuple[str, ...]:
    """판정에 쓸 이름 집합: Master 표기 + 자동 파생 + 수동 확정."""
    return (display_name,
            *sorted(derive_roman_aliases(display_name)),
            *NAME_ALIASES.get(display_name, ()))


def expand_aliases(names) -> set[str]:
    """이름 모집단에 별칭을 전부 합친다 - '더 긴 이름' 가드가 별칭도 알아야
    제목의 'LG CNS' 가 'LG' 의 DEDICATED 오탐을 덮는다."""
    out = set(names)
    for n in list(out):
        out.update(derive_roman_aliases(n))
        out.update(NAME_ALIASES.get(n, ()))
    return out


def krx_symbol_resolver(instrument_by_symbol: dict, *, dedicated_names: dict | None = None,
                        known_names=None):
    """record.symbols 가 KRX 종목코드일 때(NAVER).

    dedicated_names 는 {symbol: 종목명}. 제목에 이름이 **독립 등장**하면
    DEDICATED(0.9), 아니면 MENTIONS(0.3 - 본문 매칭 추정)다.
    known_names 는 '더 긴 이름' 검사의 모집단이다 - 감시 목록만 주면 감시 밖
    회사(두산로보틱스 등)가 제목에 있을 때 못 거르므로 **전 상장사 이름**을
    주는 쪽이 맞다.
    """
    names = dedicated_names or {}
    all_names = expand_aliases(known_names if known_names else names.values())

    def resolve(record: NewsRecord):
        out = []
        for sym in record.symbols:
            iid = instrument_by_symbol.get(sym)
            if iid is None:
                continue
            name = names.get(sym)
            if name:
                own = set(names_for(name))
                if any(title_has_standalone(n, record.title, all_names - own)
                       for n in own):
                    out.append((iid, "DEDICATED", "0.9"))
                    continue
            out.append((iid, "MENTIONS", BODY_MATCH_CONFIDENCE))
        return out

    return resolve


def foreign_symbol_resolver(us_to_krx: dict, instrument_by_symbol: dict, patterns: dict):
    """record.symbols 가 미국 심볼일 때(Alpaca).

    us_to_krx 로 KRX 코드를 찾고, patterns(미국심볼 -> 헤드라인 정규식)로 전용
    여부를 가른다. 매핑에 없는 미국 심볼은 **만들지 않고 버린다** - 호출부가
    stats.unresolved_symbols 로 확인한다.
    """
    import re as _re

    def resolve(record: NewsRecord):
        out = []
        seen = set()
        title_lc = record.title.lower()
        for sym in record.symbols:
            krx = us_to_krx.get(sym)
            if krx is None:
                continue
            iid = instrument_by_symbol.get(krx)
            if iid is None or iid in seen:
                continue
            seen.add(iid)
            pat = patterns.get(sym)
            if pat and _re.search(pat, title_lc):
                out.append((iid, "DEDICATED", "0.95"))
            else:
                out.append((iid, "MENTIONS", "0.5"))
        return out

    return resolve


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없이
# ---------------------------------------------------------------------------

class _FakeRef:
    """upsert/link 계약만 흉내낸다."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.docs: dict[str, str] = {}
        self.links: set = set()
        self.calls = 0
        self.last_revisions: tuple = ()
        self._fail_on = fail_on

    def upsert_news_documents(self, records, *, source_id, issuer_by_external_id=None):
        self.calls += 1
        if self._fail_on is not None and self.calls == self._fail_on:
            raise NewsSinkError("DB 장애")
        new = 0
        ids = {}
        for r in records:
            if r.external_id not in self.docs:
                self.docs[r.external_id] = f"doc-{len(self.docs)}"
                new += 1
            ids[r.external_id] = self.docs[r.external_id]
        return new, len(records) - new, ids

    def link_documents_to_instruments(self, links):
        before = len(self.links)
        for d, i, rt, _c in links:
            self.links.add((d, i, rt))
        return len(self.links) - before


def _rec(i: int, *, symbols=(), title=None) -> NewsRecord:
    return NewsRecord(
        external_id=f"t:{i}",
        title=title or f"기사 {i}",
        canonical_url=f"https://x.invalid/{i}",
        published_at=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 31, 1, 1, tzinfo=timezone.utc),
        language="ko",
        provider="t",
        symbols=tuple(symbols),
    )


def _check_batch_flush():
    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=3, clock=lambda: 0.0)
    for i in range(2):
        sink.add(_rec(i))
    assert ref.calls == 0, "배치가 안 찼는데 Flush 했다"
    sink.add(_rec(2))
    assert ref.calls == 1 and sink.stats.flushes == 1
    assert sink.stats.documents_new == 3
    print("  크기 Flush               OK")


def _check_time_flush():
    t = {"v": 0.0}
    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=100, max_delay_seconds=5.0,
                    clock=lambda: t["v"])
    sink.add(_rec(0))
    assert ref.calls == 0
    # 기사가 안 들어와도 시간이 지나면 tick() 으로 나가야 한다
    t["v"] = 6.0
    sink.tick()
    assert ref.calls == 1, "시간 경과 Flush 가 안 됐다"
    assert sink.stats.documents_new == 1

    # 다음 기사부터 타이머가 다시 시작한다
    sink.add(_rec(1))
    sink.tick()
    assert ref.calls == 1, "직전 타이머가 남아 바로 Flush 됐다"
    t["v"] = 12.0
    sink.tick()
    assert ref.calls == 2
    print("  시간 Flush               OK")


def _check_immediate_mode():
    """max_batch=1 이면 진짜 건건이 들어간다."""
    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=1, clock=lambda: 0.0)
    for i in range(3):
        sink.add(_rec(i))
        assert ref.calls == i + 1, f"{i}번째가 즉시 안 들어갔다"
    assert len(ref.docs) == 3
    print("  즉시 모드                OK")


def _check_tail_not_lost():
    """버퍼에 남은 것을 잃지 않는지 - 이게 제일 흔한 사고다."""
    ref = _FakeRef()
    with NewsSink(ref, source_id="s", max_batch=100, clock=lambda: 0.0) as sink:
        sink.add(_rec(0))
        sink.add(_rec(1))
        assert ref.calls == 0
    assert ref.calls == 1 and len(ref.docs) == 2, "close 에서 남은 것을 잃었다"

    # 예외로 빠져나가도 받은 것은 남긴다
    ref2 = _FakeRef()
    try:
        with NewsSink(ref2, source_id="s", max_batch=100, clock=lambda: 0.0) as s2:
            s2.add(_rec(0))
            raise RuntimeError("바깥에서 터짐")
    except RuntimeError:
        pass
    assert len(ref2.docs) == 1, "예외 경로에서 버퍼를 잃었다"

    # 닫힌 Sink 는 조용히 받아먹지 않는다
    sink2 = NewsSink(_FakeRef(), source_id="s")
    sink2.close()
    try:
        sink2.add(_rec(0))
        raise AssertionError("닫힌 Sink 가 기사를 받았다")
    except NewsSinkError:
        pass
    print("  꼬리 보존                OK")


def _check_failure_not_swallowed():
    """적재 실패를 삼키면 '수집은 됐는데 DB 에 없는' 상태가 된다."""
    ref = _FakeRef(fail_on=1)
    sink = NewsSink(ref, source_id="s", max_batch=2, clock=lambda: 0.0)
    sink.add(_rec(0))
    try:
        sink.add(_rec(1))
        raise AssertionError("적재 실패가 조용히 지나갔다")
    except NewsSinkError:
        pass
    # 실패했으면 버퍼를 비우지 않아야 재시도가 된다
    assert len(sink._buf) == 2, "실패했는데 버퍼를 비웠다 - 그 기사는 영영 사라진다"
    sink.flush()  # 두 번째 호출은 성공한다
    assert len(ref.docs) == 2 and len(sink._buf) == 0
    print("  실패 전파/재시도         OK")


def _check_rebind():
    """DB 재접속 시 버퍼·통계를 잃지 않고 새 접속으로 이어가는지 (상주 서비스용)."""
    bad = _FakeRef(fail_on=1)
    sink = NewsSink(bad, source_id="s", max_batch=2, clock=lambda: 0.0)
    sink.add(_rec(0))
    try:
        sink.add(_rec(1))
        raise AssertionError("장애가 조용히 지나갔다")
    except NewsSinkError:
        pass
    assert len(sink._buf) == 2

    good = _FakeRef()
    sink.rebind(good)
    sink.flush()
    assert len(good.docs) == 2 and len(sink._buf) == 0, "rebind 후 재시도가 안 됐다"
    assert sink.stats.received == 2 and sink.stats.documents_new == 2

    # 닫힌 Sink 는 rebind 도 거부한다
    sink.close()
    try:
        sink.rebind(_FakeRef())
        raise AssertionError("닫힌 Sink 가 rebind 됐다")
    except NewsSinkError:
        pass
    print("  재접속 rebind            OK")


def _check_batch_dedup():
    """같은 기사가 한 배치에 두 번 오면 upsert 가 같은 행을 두 번 건드려 실패한다."""
    seen = {}

    class _StrictRef(_FakeRef):
        def upsert_news_documents(self, records, **kw):
            ids = [r.external_id for r in records]
            assert len(ids) == len(set(ids)), f"배치 안에 중복이 있다: {ids}"
            return super().upsert_news_documents(records, **kw)

    ref = _StrictRef()
    sink = NewsSink(ref, source_id="s", max_batch=3, clock=lambda: 0.0)
    sink.add(_rec(0))
    sink.add(_rec(0))
    sink.add(_rec(1))
    assert len(ref.docs) == 2
    print("  배치 내 중복 제거        OK")


def _check_title_dedup_window():
    """같은 (제목, 게시일) 재전송을 적재 전에 거르는지 - DB 중복 270행 재발 방지."""
    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=1, clock=lambda: 0.0,
                    title_dedup_window=100)
    sink.add(_rec(1, title="아이텍, 자기 전환사채 만기전 취득 결정"))
    # 같은 제목·같은 날, external_id 만 다른 재전송(LS 실측 x14 사례)
    dup = NewsRecord(external_id="t:999", title="아이텍, 자기 전환사채 만기전 취득 결정",
                     canonical_url=None,
                     published_at=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
                     observed_at=datetime(2026, 7, 31, 1, 2, tzinfo=timezone.utc),
                     language="ko", provider="t")
    sink.add(dup)
    assert len(ref.docs) == 1, "재전송이 적재됐다"
    assert sink.stats.title_dups == 1 and "제목중복 1" in sink.stats.summary()
    # 창 0 이면 기존 동작 그대로
    s2 = NewsSink(_FakeRef(), source_id="s", max_batch=1, clock=lambda: 0.0)
    s2.add(_rec(1)); s2.add(dup)
    assert s2.stats.title_dups == 0
    print("  제목 중복 창             OK")


def _check_title_window_seed():
    """재기동 예열 - 창이 비어 있는 첫 몇 분이 중복 구멍이 되지 않는지."""
    from datetime import date

    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=1, clock=lambda: 0.0,
                    title_dedup_window=3)
    n = sink.seed_titles([("기사A", date(2026, 7, 31)), ("기사B", date(2026, 7, 31))])
    assert n == 2
    # 예열된 키의 재방출은 적재 전에 걸린다 (재배포 직후 42행 재유입 사례)
    dup = NewsRecord(external_id="t:1", title="기사A", canonical_url=None,
                     published_at=datetime(2026, 7, 31, 1, 0,
                                           tzinfo=timezone(timedelta(hours=9))),
                     observed_at=datetime(2026, 7, 31, 1, 2, tzinfo=timezone.utc),
                     language="ko", provider="t")
    sink.add(dup)
    assert len(ref.docs) == 0 and sink.stats.title_dups == 1, "예열이 안 먹었다"
    # 창 크기 초과 예열은 오래된 키부터 밀려난다 + 중복 키는 다시 세지 않는다
    n2 = sink.seed_titles([("기사B", date(2026, 7, 31)), ("기사C", date(2026, 7, 31)),
                           ("기사D", date(2026, 7, 31))])
    assert n2 == 2 and len(sink._seen_title_set) == 3
    # 창 0(비활성)이면 예열도 no-op
    assert NewsSink(_FakeRef(), source_id="s", max_batch=1,
                    clock=lambda: 0.0).seed_titles([("x", date(2026, 1, 1))]) == 0
    print("  제목 창 예열             OK")


def _check_title_standalone():
    names = {"두산", "두산에너빌리티", "LG", "LG전자"}
    # 긴 이름의 부분 문자열은 독립 등장이 아니다
    assert not title_has_standalone("두산", "두산에너빌리티 대규모 수주", names - {"두산"})
    assert title_has_standalone("두산에너빌리티", "두산에너빌리티 대규모 수주",
                                names - {"두산에너빌리티"})
    # 둘 다 나오면 둘 다 유효
    assert title_has_standalone("두산", "두산에너빌리티와 두산 동반 상승", names - {"두산"})
    # 독립 등장
    assert title_has_standalone("두산", "두산, 3분기 흑자 전환", names - {"두산"})
    assert not title_has_standalone("LG", "LG전자 실적 발표", names - {"LG"})
    assert title_has_standalone("LG", "LG그룹주 강세", names - {"LG"})  # 'LG그룹' 은 목록에 없다
    assert not title_has_standalone("두산", "반도체 업황 개선", names - {"두산"})
    print("  제목 독립 등장 판정      OK")


def _check_aliases():
    # 음차 자동 파생
    assert derive_roman_aliases("LG씨엔에스") == {"LGCNS", "LG CNS"}, \
        derive_roman_aliases("LG씨엔에스")
    assert "KT&G" in derive_roman_aliases("케이티앤지")
    assert derive_roman_aliases("이수페타시스") == set(), "한 음절 '이' 를 E 로 바꿨다"
    assert derive_roman_aliases("삼성전자") == set()

    # LG CNS 실측 회귀 (2026-07-31): 'LG CNS 상반기 매출' 제목에서
    #  - LG(003550) 는 DEDICATED 가 아니어야 하고 (별칭 'LG CNS' 가 덮는다)
    #  - LG씨엔에스(064400) 는 별칭으로 DEDICATED 여야 한다
    inst = {"003550": "iid-lg", "064400": "iid-lgcns"}
    dedicated = {"003550": "LG", "064400": "LG씨엔에스"}
    resolver = krx_symbol_resolver(inst, dedicated_names=dedicated,
                                   known_names={"LG", "LG씨엔에스", "LG전자"})
    rec = _rec(0, symbols=("003550", "064400"),
               title="LG CNS 상반기 매출 2.8조…AI·클라우드 성장")
    got = {iid: (rel, conf) for iid, rel, conf in resolver(rec)}
    assert got["iid-lg"][0] == "MENTIONS", f"LG 오탐이 살아있다: {got}"
    assert got["iid-lgcns"] == ("DEDICATED", "0.9"), f"별칭 전용 판정 실패: {got}"
    print("  별칭 파생/판정           OK")


def _check_link_resolvers():
    ref = _FakeRef()
    inst = {"005930": "iid-samsung", "000660": "iid-hynix"}
    resolver = krx_symbol_resolver(inst, dedicated_names={"005930": "삼성전자"})

    sink = NewsSink(ref, source_id="s", max_batch=1, clock=lambda: 0.0,
                    link_resolver=resolver)
    sink.add(_rec(0, symbols=("005930",), title="삼성전자, 2분기 최대 실적"))
    sink.add(_rec(1, symbols=("005930", "000660"), title="반도체 업황 개선"))
    # 매핑에 없는 종목은 만들지 않고 센다
    sink.add(_rec(2, symbols=("999999",), title="모르는 종목"))

    assert ("doc-0", "iid-samsung", "DEDICATED") in ref.links
    assert ("doc-1", "iid-samsung", "MENTIONS") in ref.links
    assert ("doc-1", "iid-hynix", "MENTIONS") in ref.links
    assert sink.stats.unresolved_symbols == {"999999"}, sink.stats.unresolved_symbols

    # 해외 심볼 -> KRX
    ref2 = _FakeRef()
    fr = foreign_symbol_resolver(
        {"SSNLF": "005930", "SSNGY": "005930"}, inst, {"SSNLF": r"samsung", "SSNGY": r"samsung"}
    )
    s2 = NewsSink(ref2, source_id="s", max_batch=1, clock=lambda: 0.0, link_resolver=fr)
    # 같은 기업의 미국 심볼이 둘 다 붙어도 instrument 는 하나다
    s2.add(_rec(0, symbols=("SSNLF", "SSNGY"), title="Samsung Q3 beat"))
    assert len([lk for lk in ref2.links if lk[0] == "doc-0"]) == 1
    assert ("doc-0", "iid-samsung", "DEDICATED") in ref2.links
    print("  종목 연결 Resolver       OK")


def _check_pipeline_with_stream():
    """폴링 Stream 을 그대로 물려도 동작하는지 - WebSocket 도 같은 계약이다."""
    from news_events import PollingNewsStream

    pages = [[_rec(1), _rec(2)], [_rec(2), _rec(3)]]
    n = {"i": 0}

    def fetch(_cursor):
        i = n["i"]
        n["i"] += 1
        return pages[i] if i < len(pages) else []

    t = {"v": 0.0}
    stream = PollingNewsStream(
        source_id="t", fetch_page=fetch, interval_seconds=1.0,
        sleep=lambda s: t.__setitem__("v", t["v"] + s), clock=lambda: t["v"],
        now=lambda: datetime(2026, 7, 31, 1, 1, tzinfo=timezone.utc),
    )
    ref = _FakeRef()
    sink = NewsSink(ref, source_id="s", max_batch=1, clock=lambda: 0.0)

    st, sk = run_pipeline(stream, sink, max_records=3)
    # Stream 이 중복을 걸러 3건만 내려보내고, Sink 가 건건이 넣는다
    assert st.emitted == 3 and sk.received == 3, (st.summary(), sk.summary())
    assert len(ref.docs) == 3
    assert sk.documents_new == 3 and sk.flushes == 3
    print("  Stream 연결              OK")


def _check_config_guards():
    ref = _FakeRef()
    for kw in ({"max_batch": 0}, {"max_batch": -1}, {"max_delay_seconds": 0}):
        try:
            NewsSink(ref, source_id="s", **kw)
            raise AssertionError(f"{kw} 가 통과했다")
        except ValueError:
            pass
    # 요약에 축소가 드러나야 한다
    s = SinkStats(received=10, documents_new=7, documents_updated=3,
                  links_new=5, links_attempted=8, flushes=2, revisions=1)
    s.unresolved_symbols.add("ZZZZ")
    out = s.summary()
    for token in ("수신 10", "신규 7", "갱신 3", "연결 5/8", "Flush 2", "정정 1", "ZZZZ"):
        assert token in out, f"{token} 이 요약에 없다: {out}"
    print("  설정 가드/요약           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{PIPELINE_VERSION} 자체 점검 (DB 없이)")
    _check_batch_flush()
    _check_time_flush()
    _check_immediate_mode()
    _check_tail_not_lost()
    _check_failure_not_swallowed()
    _check_rebind()
    _check_batch_dedup()
    _check_title_dedup_window()
    _check_title_window_seed()
    _check_title_standalone()
    _check_aliases()
    _check_link_resolvers()
    _check_pipeline_with_stream()
    _check_config_guards()
    print("Sink 14개 영역 통과")
