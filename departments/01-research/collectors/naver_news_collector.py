#!/usr/bin/env python3
"""P0 국내 뉴스 수집 - NAVER 검색 API.

소유: 재일 (리서치본부)
근거: https://developers.naver.com/docs/serviceapi/search/news/news.md
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(뉴스 P0), 3.3(수집 금지),
      4.2(PIT), 5.2(research.documents), 8.2(DQ)
      departments/01-research/contracts/news_events.py (Stream 계약)

▶ 왜 폴링인데 Stream 인가
  NAVER 는 **WebSocket 을 주지 않는다.** REST GET 뿐이다(BIGKinds 도 마찬가지고,
  2026-07-31 뉴스 API 5종 조사에서 한국어를 커버하면서 무료 WS 를 주는 곳은 없다는
  것이 구조적 결론이었다). 그렇다고 호출부가 Source 마다 다른 모양으로 붙으면 나중에
  Source 를 바꿀 때 하류가 전부 깨진다. 그래서 news_events.PollingNewsStream 으로
  감싸 **Alpaca WebSocket 과 똑같은 push 인터페이스** 로 노출한다.

▶ 실측으로 확인한 것 (2026-07-31)
  - item 필드는 title, originallink, link, description, pubDate **다섯 개뿐이다.**
    기사 ID 가 없다. 그래서 external_id 를 URL 로 만든다 - 제목은 정정되면 바뀌므로
    키가 될 수 없다.
  - title 과 description 에 검색어가 <b> 태그로 감싸여 온다. 그대로 두면 같은 기사가
    검색어에 따라 다른 문자열이 되어 중복 판정이 깨진다(clean_title 로 제거).
  - pubDate 는 RFC 822 + KST 오프셋. "Fri, 31 Jul 2026 02:04:00 +0900"
  - display 최대 100, start 최대 1000. start=1001 은 HTTP 400
    "Invalid start value". 즉 **한 쿼리로 최대 1,000건까지만 거슬러 갈 수 있다.**
  - total 은 수백만이 나오지만 위 한계 때문에 의미가 없다. 전수 수집이 아니라
    **최신 구간 감시** 용도로만 쓴다.

▶ 라이선스 (가이드 3.3)
  Registry 의 allowed_uses 는 SEARCH_ONLY, SNIPPET_STORE 다. description 은 기사
  본문의 일부이므로 **저장하지 않는다** - research.documents 에 넣을 자리도 없고,
  넣을 수 있게 해 두면 언젠가 들어간다. 제목·URL·시각만 적재한다.

자체 점검(호출 없음): python departments/01-research/collectors/naver_news_collector.py
단발 조회:            python departments/01-research/collectors/naver_news_collector.py --probe 삼성전자
Watchlist 수집:       python departments/01-research/collectors/naver_news_collector.py --collect [--top N | --symbols 005930,000660]
Stream(폴링):         python departments/01-research/collectors/naver_news_collector.py --stream [--seconds N]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
from news_events import (  # noqa: E402
    DEDUP_WINDOW,
    NewsRecord,
    NewsStreamError,
    PollingNewsStream,
    StreamCursor,
    Transport,
    build_external_id,
    clean_title,
)
from source_registry import SourceRegistry, UseScope, load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-naver-news-v1"
SOURCE_ID = "naver_apihub"
PROVIDER = "naver"

ENDPOINT = "https://openapi.naver.com/v1/search/news.json"

# 실측으로 확인한 한계. 넘기면 HTTP 400 이다.
DISPLAY_MAX = 100
START_MAX = 1000

# 개발자센터 기준 일 25,000회. Registry 에 넣지 않고 여기 두는 이유 - 이건 Vendor
# 문서에서 확인한 값이지만 계정 등급마다 다를 수 있어 보수적으로 쓴다.
DAILY_QUOTA = 25_000

LANGUAGE = "ko"
# sort=date 여야 최신순이다. sim(정확도순)은 Stream 이 될 수 없다 - 새 기사가
# 앞에 온다는 보장이 없어 Cursor 가 성립하지 않는다.
SORT_BY_DATE = "date"


class NaverNewsError(NewsStreamError):
    """NAVER 수집 실패. 빈 결과로 바꾸지 않는다."""


@dataclass(frozen=True)
class NaverCredentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> NaverCredentials:
        e = env or load_project_env()
        SourceRegistry(env=e).require(SOURCE_ID)
        return cls(e["NAVER_CLIENT_ID"], e["NAVER_CLIENT_SECRET"])

    def headers(self) -> dict[str, str]:
        return {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------

def _parse_pub_date(raw: str) -> datetime:
    """RFC 822 + KST 오프셋을 UTC aware 로. 실측: 'Fri, 31 Jul 2026 02:04:00 +0900'"""
    s = (raw or "").strip()
    if not s:
        raise NaverNewsError("pubDate 가 비었다")
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError) as e:
        raise NaverNewsError(f"pubDate 를 읽지 못했다: {raw!r} ({e})") from None
    if dt.tzinfo is None:
        # RFC 822 는 오프셋이 필수지만 없는 응답을 KST 로 가정하지 않는다.
        raise NaverNewsError(f"pubDate 에 Timezone 이 없다: {raw!r}")
    return dt.astimezone(timezone.utc)


def _stable_url(item: dict) -> str:
    """external_id 의 근거가 될 URL.

    originallink(언론사 원문)가 우선이다. link 는 네이버 뉴스 미러라 같은 기사가
    미러 유무에 따라 두 건으로 갈린다. originallink 가 비면 link 로 떨어진다.
    """
    for key in ("originallink", "link"):
        v = str(item.get(key) or "").strip()
        if v:
            return v
    raise NaverNewsError(f"URL 이 없다: keys={sorted(item)}")


def parse_item(item: dict, *, observed_at: datetime) -> NewsRecord:
    """검색 결과 한 건을 정규 Record 로. 본문·요약은 담지 않는다."""
    title = clean_title(str(item.get("title") or ""))
    if not title:
        raise NaverNewsError(f"title 이 비었다: {item.get('link')!r}")

    url = _stable_url(item)
    return NewsRecord(
        external_id=build_external_id(PROVIDER, url),
        title=title,
        canonical_url=url,
        published_at=_parse_pub_date(str(item.get("pubDate") or "")),
        observed_at=observed_at,
        language=LANGUAGE,
        provider=PROVIDER,
        # 발행 언론사는 응답에 이름으로 오지 않는다. URL 호스트가 유일한 단서인데
        # 그것을 언론사명으로 단정하지 않는다(추정 금지). 호스트만 남긴다.
        publisher=urllib.parse.urlparse(url).netloc,
        had_summary=bool(str(item.get("description") or "").strip()),
        had_content=False,  # 본문은 애초에 오지 않는다
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

# ▶ 실측 2026-07-31: 20종목을 연속으로 던지자 HTTP 429
#   {"errorMessage":"Rate limit exceeded...","errorCode":"012"} 가 왔다.
#   일일 25,000회와 **별개로 초당 버스트 제한** 이 있다. 문서에 수치가 없어서
#   보수적으로 잡는다 - 추측값을 낙관적으로 잡으면 장중에 수집이 끊긴다.
DEFAULT_RATE_LIMIT_PER_SEC = 4.0
RATE_LIMIT_ERROR_CODE = "012"
# 429 는 재시도할 가치가 있지만 무한정은 아니다. 이 횟수를 넘으면 실패로 올린다.
MAX_RATE_LIMIT_RETRIES = 3


class RateLimiter:
    """초당 호출 제한. ls_client 와 같은 방식이다."""

    def __init__(self, per_sec: float) -> None:
        if per_sec <= 0:
            raise ValueError("per_sec 는 0 보다 커야 한다")
        self._interval = 1.0 / per_sec
        self._last = 0.0

    def wait(self) -> None:
        import time as _time

        gap = _time.monotonic() - self._last
        if gap < self._interval:
            _time.sleep(self._interval - gap)
        self._last = _time.monotonic()


class NaverNewsClient:
    """NAVER 검색 API Client. 초당 제한과 일일 한도를 둘 다 지킨다."""

    def __init__(
        self,
        creds: NaverCredentials | None = None,
        *,
        timeout: int = 15,
        rate_limit_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC,
        sleep=None,
    ) -> None:
        self._creds = creds or NaverCredentials.from_env()
        self._timeout = timeout
        self._limiter = RateLimiter(rate_limit_per_sec)
        self._sleep = sleep
        self.calls = 0
        self.rate_limited = 0

    def search(
        self, query: str, *, display: int = DISPLAY_MAX, start: int = 1, sort: str = SORT_BY_DATE
    ) -> dict:
        if not query.strip():
            raise NaverNewsError("query 가 비었다")
        if not 1 <= display <= DISPLAY_MAX:
            raise NaverNewsError(f"display 는 1~{DISPLAY_MAX} 다: {display}")
        if not 1 <= start <= START_MAX:
            raise NaverNewsError(f"start 는 1~{START_MAX} 다: {start}")
        if self.calls >= DAILY_QUOTA:
            raise NaverNewsError(f"일일 한도 {DAILY_QUOTA} 회에 도달했다")

        params = urllib.parse.urlencode(
            {"query": query, "display": display, "start": start, "sort": sort}
        )
        url = f"{ENDPOINT}?{params}"

        import time as _time

        sleep = self._sleep or _time.sleep
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            self._limiter.wait()
            req = urllib.request.Request(url, headers=self._creds.headers())
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode("utf-8", "replace")
                if e.code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                    # 지수 백오프. 재시도를 조용히 하지 않고 세어서 드러낸다.
                    self.rate_limited += 1
                    sleep(2.0 * (attempt + 1))
                    continue
                raise NaverNewsError(f"search HTTP {e.code}: {body}") from None
            except urllib.error.URLError as e:
                raise NaverNewsError(f"search 연결 실패: {e.reason}") from None
        raise NaverNewsError(
            f"429 가 {MAX_RATE_LIMIT_RETRIES}회 재시도 후에도 계속된다 - "
            f"rate_limit_per_sec 를 낮추거나 Watchlist 를 줄일 것"
        )

    def fetch(
        self, query: str, *, display: int = DISPLAY_MAX, start: int = 1
    ) -> list[NewsRecord]:
        d = self.search(query, display=display, start=start)
        items = d.get("items")
        if items is None:
            raise NaverNewsError(f"items 가 응답에 없다: keys={sorted(d)}")
        observed_at = datetime.now(timezone.utc)
        return [parse_item(it, observed_at=observed_at) for it in items]


# ---------------------------------------------------------------------------
# Watchlist - 어떤 종목을 감시할 것인가
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchItem:
    """감시 대상 하나. 검색어와 종목을 함께 들고 있어야 연결이 정확해진다."""

    instrument_id: object
    symbol: str
    name: str

    @property
    def query(self) -> str:
        return self.name


# ▶ Alpaca 와 결정적으로 다른 점
#   Alpaca 는 기사에 심볼이 붙어 오지만 KRX 종목이 없어서 매핑을 따로 만들어야 했다.
#   NAVER 는 **우리가 종목명으로 질의** 하므로 어떤 종목의 기사인지가 처음부터
#   확실하다. 대신 동명이의(예: 한화 - 그룹/종목)를 우리가 걸러야 한다.
#
#   질의어가 종목명 그대로면 오탐이 섞인다. 판정은 news_pipeline 의
#   title_has_standalone 을 쓴다 - '두산에너빌리티' 제목에서 '두산' 이 DEDICATED
#   가 되는 부분 문자열 오탐과, 본문 매칭 추정(제목 미포함)의 과신(0.5)을
#   2026-07-31 재일님 지적으로 고쳤다.
DEDICATED_CONFIDENCE = "0.9"


def relation_for(record: NewsRecord, item: WatchItem,
                 all_names=()) -> tuple[str, str]:
    """(relation_type, confidence). 제목에 종목명(별칭 포함)이 **독립 등장**하면 전용."""
    from news_pipeline import (
        BODY_MATCH_CONFIDENCE, expand_aliases, names_for, title_has_standalone,
    )

    own = set(names_for(item.name))
    universe = expand_aliases(set(all_names) | {item.name}) - own
    if any(title_has_standalone(n, record.title, universe) for n in own):
        return "DEDICATED", DEDICATED_CONFIDENCE
    return "MENTIONS", BODY_MATCH_CONFIDENCE


def _watchlist_for(ref, symbols: tuple[str, ...]) -> list[WatchItem]:
    """명시한 종목코드로 Watchlist 를 만든다. 못 찾은 코드는 조용히 빼지 않는다."""
    with ref._conn.cursor() as cur:
        cur.execute(
            """
            select i.instrument_id, s.symbol, i.display_name
            from reference.instruments i
            join reference.instrument_symbols s using (instrument_id)
            where i.market = 'KRX' and i.instrument_type = 'STOCK' and s.symbol = any(%s)
            """,
            (list(symbols),),
        )
        found = [WatchItem(iid, sym, name) for iid, sym, name in cur.fetchall()]
    missing = set(symbols) - {w.symbol for w in found}
    if missing:
        raise NaverNewsError(f"Instrument Master 에 없는 종목코드다: {sorted(missing)}")
    return found


def load_watchlist(
    ref, *, top: int = 40, symbols: tuple[str, ...] = ()
) -> list[WatchItem]:
    """감시 대상을 Reference 에서 가져온다.

    ▶ 코스피200·코스닥150 구성종목을 쓸 수 없다. KRX 서비스 이용 승인이 없어
      구성종목 API 가 401 이고(source_registry NOT_AUTHORIZED_OBSERVED), LS 에는
      해당 TR 이 없다. **구성종목을 추정해서 만들지 않는다.**

      대신 **공시 건수** 를 활동성 대리지표로 쓴다. 시가총액이 없는 상태에서
      이름 순으로 자르면 'BGF, BNK금융지주, CJ대한통운...' 처럼 아무 근거 없는
      목록이 된다. 공시를 많이 낸 기업이 뉴스도 많다는 것이 훨씬 나은 가정이다.
      승인이 떨어지면 이 함수만 갈아끼우면 된다 - 호출부는 WatchItem 만 안다.

    ⚠ **이 대리지표는 증권사로 쏠린다** (실측 2026-07-31). ELS·DLS 발행 공시를
      대량으로 내기 때문에 상위 15개 중 10개가 증권사였다. 시가총액 Source 가
      생기기 전까지는 symbols 로 명시 지정하는 쪽이 낫다.
    """
    if symbols:
        return _watchlist_for(ref, symbols)

    with ref._conn.cursor() as cur:
        cur.execute(
            """
            select i.instrument_id, s.symbol, i.display_name, count(d.document_id) as docs
            from reference.instruments i
            join reference.instrument_symbols s using (instrument_id)
            left join research.documents d on d.issuer_id = i.issuer_id
            where i.market = 'KRX' and i.instrument_type = 'STOCK'
              and i.status = 'ACTIVE' and i.venue = 'KOSPI'
              and i.issuer_id is not null
            group by i.instrument_id, s.symbol, i.display_name
            order by docs desc, i.display_name
            limit %s
            """,
            (top,),
        )
        return [WatchItem(iid, sym, name) for iid, sym, name, _ in cur.fetchall()]


def make_watch_stream(
    client: NaverNewsClient,
    items: list[WatchItem],
    *,
    display: int = 30,
    interval_seconds: float = 60.0,
    cursor: StreamCursor | None = None,
    sweep_items=None,
    sleep=None,
    clock=None,
    now=None,
) -> PollingNewsStream:
    """Watchlist 전체를 한 번 도는 것을 '한 번의 폴링' 으로 본다.

    호출부는 이게 폴링인지 WebSocket 인지 모른다 - news_events.NewsStream 계약이다.
    sleep/clock/now 는 테스트 주입용이다(실제로 자거나 벽시계를 보지 않게).

    sweep_items(sweep_index) -> list[WatchItem] 을 주면 **sweep 마다 대상이 바뀐다**
    (2계층 순회). 안 주면 매번 items 전체다 - 기존 동작 그대로다.
    items 는 그때도 필요하다: dedup 창 크기와 빈 감시 판정의 기준이다.
    """
    if not items:
        raise NaverNewsError("Watchlist 가 비었다 - 빈 감시를 정상으로 보지 않는다")

    def fetch_page(_cursor: StreamCursor) -> list[NewsRecord]:
        # ▶ 질의어 -> 종목 연결을 잃지 않는다 (이게 NAVER 의 최대 강점이다)
        #   Alpaca 는 기사에 심볼이 붙어 오지만 KRX 종목이 없어 매핑을 따로 만들어야
        #   했다. NAVER 는 **우리가 종목명으로 질의** 하므로 어느 종목의 기사인지가
        #   처음부터 확실하다. 그 사실을 NewsRecord.symbols 에 실어 하류로 넘긴다 -
        #   그러면 연결 코드가 Alpaca 와 같은 모양이 된다.
        #
        #   한 기사가 여러 종목 질의에 걸리면(예: '삼성전자·SK하이닉스 동반 상승')
        #   Cursor 가 두 번째를 중복으로 걸러 종목 하나를 잃는다. 그래서 페이지 안에서
        #   먼저 **심볼을 합쳐** 두고 한 건으로 내보낸다.
        # ▶ sweep 순회 (2계층). index 를 **부르기 전에** 올린다 - 실패한 slice 에서
        #   멈추면 그 slice 가 영구히 순회를 막는다. 한 번 걸러도 다음 바퀴에 다시
        #   오고, LS 실시간 푸시가 같은 종목을 이미 덮는다.
        active = items
        if sweep_items is not None:
            idx = fetch_page.sweep_index
            fetch_page.sweep_index = idx + 1
            active = list(sweep_items(idx))
            if not active:
                raise NaverNewsError(
                    f"sweep {idx} 대상이 비었다 - 빈 sweep 을 정상으로 보지 않는다")

        by_ext: dict[str, tuple[NewsRecord, set]] = {}
        order: list[str] = []
        raw = 0
        for it in active:
            # 종목 하나가 실패해도 나머지를 조용히 건너뛰지 않는다. 예외를 올린다.
            for rec in client.fetch(it.query, display=display):
                raw += 1
                if rec.external_id in by_ext:
                    by_ext[rec.external_id][1].add(it.symbol)
                else:
                    by_ext[rec.external_id] = (rec, {it.symbol})
                    order.append(rec.external_id)
        fetch_page.raw_items = raw
        fetch_page.merged = raw - len(order)
        return [
            replace(by_ext[e][0], symbols=tuple(sorted(by_ext[e][1]))) for e in order
        ]

    fetch_page.raw_items = 0
    fetch_page.merged = 0
    fetch_page.sweep_index = 0

    if cursor is None:
        # dedup 창은 한 sweep(종목수 × display)보다 커야 한다. 기본 창(2,000)으로
        # 바스켓 350종목을 돌리면 직전 sweep 가 창에서 밀려나 매번 재방출된다
        # (실측 2026-07-31: sweep 2 재방출 3,585건/신규 314건). ×2 는 sweep 사이에
        # 새 기사가 끼어들어도 직전 sweep 전체가 창 안에 남게 하는 여유다.
        cursor = StreamCursor.sized(max(DEDUP_WINDOW, len(items) * display * 2))

    return PollingNewsStream(
        source_id=SOURCE_ID,
        fetch_page=fetch_page,
        interval_seconds=interval_seconds,
        cursor=cursor,
        sleep=sleep,
        clock=clock,
        now=now,
    )


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_SAMPLE = {
    "title": "[오늘의 경제뉴스] <b>삼성전자</b>의 90조원보다 강했던 AI 거품 공포",
    "originallink": "https://www.newsverse.kr/news/articleView.html?idxno=11055",
    "link": "https://n.news.naver.com/mnews/article/023/0003990645",
    "description": "(사진=연합뉴스) <b>삼성전자</b>가 90조원에 육박하는",
    "pubDate": "Fri, 31 Jul 2026 02:04:00 +0900",
}


def _ob() -> datetime:
    return datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)


def _check_parse():
    r = parse_item(_SAMPLE, observed_at=_ob())
    # <b> 태그가 제거돼야 한다
    assert r.title == "[오늘의 경제뉴스] 삼성전자의 90조원보다 강했던 AI 거품 공포", r.title
    assert "<b>" not in r.title
    # KST +0900 -> UTC
    assert r.published_at == datetime(2026, 7, 30, 17, 4, tzinfo=timezone.utc), r.published_at
    assert r.language == "ko" and r.provider == "naver"
    # originallink 가 우선 - link(네이버 미러)를 쓰면 같은 기사가 둘로 갈린다
    assert r.canonical_url == _SAMPLE["originallink"]
    assert r.publisher == "www.newsverse.kr"
    # 본문·요약은 담지 않는다
    assert r.had_summary is True and r.had_content is False
    assert "description" not in {f for f in NewsRecord.__dataclass_fields__}
    print("  파싱                     OK")


def _check_parse_rejects():
    for patch, expect in (
        ({"title": ""}, "title"),
        ({"title": "<b></b>"}, "title"),
        ({"pubDate": ""}, "pubDate"),
        ({"pubDate": "2026-07-31 02:04:00"}, "pubDate"),
        ({"originallink": "", "link": ""}, "URL"),
    ):
        try:
            parse_item({**_SAMPLE, **patch}, observed_at=_ob())
            raise AssertionError(f"{patch} 가 통과했다")
        except NewsStreamError as e:
            assert expect in str(e), f"{expect} 기대했는데 {e}"

    # originallink 가 비면 link 로 떨어진다
    r = parse_item({**_SAMPLE, "originallink": ""}, observed_at=_ob())
    assert r.canonical_url == _SAMPLE["link"]
    print("  불량 입력 거부           OK")


def _check_external_id_stability():
    """같은 기사는 검색어가 달라도 같은 external_id 여야 한다."""
    a = parse_item(_SAMPLE, observed_at=_ob())
    # 다른 검색어로 받아 <b> 위치가 달라진 같은 기사
    other = {**_SAMPLE, "title": "[오늘의 <b>경제</b>뉴스] 삼성전자의 90조원보다 강했던 AI 거품 공포"}
    b = parse_item(other, observed_at=_ob())
    assert a.external_id == b.external_id
    assert a.title == b.title, "제목 정규화가 검색어에 따라 달라진다"
    # 제목이 정정돼도 external_id 는 그대로여야 한다(URL 기반)
    c = parse_item({**_SAMPLE, "title": "완전히 다른 제목"}, observed_at=_ob())
    assert c.external_id == a.external_id
    print("  external_id 안정성       OK")


def _check_client_limits():
    creds = NaverCredentials("id", "secret")
    c = NaverNewsClient(creds)
    for kw, msg in (
        ({"display": 0}, "display"), ({"display": 101}, "display"),
        ({"start": 0}, "start"), ({"start": 1001}, "start"),
    ):
        try:
            c.search("삼성전자", **kw)
            raise AssertionError(f"{kw} 가 통과했다")
        except NaverNewsError as e:
            assert msg in str(e)
    try:
        c.search("   ")
        raise AssertionError("빈 query 가 통과했다")
    except NaverNewsError:
        pass
    assert c.calls == 0, "실패한 호출을 카운트했다"

    # 일일 한도에 도달하면 막는다
    c.calls = DAILY_QUOTA
    try:
        c.search("삼성전자")
        raise AssertionError("한도 초과가 통과했다")
    except NaverNewsError as e:
        assert "한도" in str(e)
    print("  Client 한도              OK")


def _check_relation():
    from news_pipeline import BODY_MATCH_CONFIDENCE

    r = parse_item(_SAMPLE, observed_at=_ob())
    it = WatchItem(None, "005930", "삼성전자")
    assert relation_for(r, it) == ("DEDICATED", DEDICATED_CONFIDENCE)
    # 제목에 종목명이 없으면 본문 매칭 추정으로 낮춘다
    other = parse_item({**_SAMPLE, "title": "코스피 상승 마감"}, observed_at=_ob())
    assert relation_for(other, it) == ("MENTIONS", BODY_MATCH_CONFIDENCE)
    # 부분 문자열 오탐 - 긴 종목명의 일부는 전용이 아니다 (재일님 지적 2026-07-31)
    dsn = WatchItem(None, "000150", "두산")
    ener = parse_item({**_SAMPLE, "title": "두산에너빌리티 대규모 수주"}, observed_at=_ob())
    assert relation_for(ener, dsn, all_names={"두산", "두산에너빌리티"}) \
        == ("MENTIONS", BODY_MATCH_CONFIDENCE), "부분 문자열이 DEDICATED 로 샜다"
    both = parse_item({**_SAMPLE, "title": "두산에너빌리티와 두산 동반 상승"}, observed_at=_ob())
    assert relation_for(both, dsn, all_names={"두산", "두산에너빌리티"})[0] == "DEDICATED"
    print("  관련도 판정              OK")


def _check_stream_contract():
    """폴링이 Stream 계약을 만족하는지. Alpaca WebSocket 과 같은 모양이어야 한다."""
    from news_events import NewsStream

    class _FakeClient:
        def __init__(self):
            self.n = 0

        def fetch(self, query, display=30):
            self.n += 1
            return [parse_item(_SAMPLE, observed_at=_ob())]

    items = [WatchItem(None, "005930", "삼성전자"), WatchItem(None, "000660", "SK하이닉스")]

    # 시계를 주입해 폴링 1회만 돌고 끝나게 한다(실제로 자지 않는다)
    ticks = {"t": 0.0}

    def clock():
        return ticks["t"]

    def sleep(s):
        ticks["t"] += s

    # ▶ now 를 고정한다 - 벽시계를 쓰면 _SAMPLE 의 고정 pubDate 가 MAX_BACKFILL_AGE
    #   (3일)를 넘기는 날 자체점검이 갑자기 TOO_OLD 로 깨진다(실제로 2026-08-03 에
    #   깨졌다). 결정론적 점검은 오늘이 며칠인지에 의존하면 안 된다.
    s = make_watch_stream(_FakeClient(), items, interval_seconds=10.0,
                          sleep=sleep, clock=clock, now=_ob)
    assert isinstance(s, NewsStream)
    assert s.transport is Transport.POLLING and s.source_id == SOURCE_ID

    got = []
    # max_seconds 를 interval 보다 작게 두면 첫 페이지를 다 처리하고 종료한다
    stats = s.run(on_record=got.append, max_seconds=5.0)
    # 종목 2개가 같은 기사를 돌려줬다. Cursor 로 걸러 하나를 버리면 **종목 하나를
    # 잃으므로** 페이지 안에서 심볼을 먼저 합친다.
    assert stats.emitted == 1 and len(got) == 1, stats.summary()
    assert got[0].symbols == ("000660", "005930"), got[0].symbols
    assert stats.duplicates == 0, "병합 대신 중복으로 버렸다 - 종목 연결이 사라진다"
    assert stats.polls == 1, stats.summary()
    # 원본 건수와 병합 건수를 숨기지 않는다
    fp = s._fetch_page
    assert fp.raw_items == 2 and fp.merged == 1, (fp.raw_items, fp.merged)

    # ▶ sweep_items 를 주면 sweep 마다 대상이 바뀐다(2계층 순회)
    seen_idx = []

    def _by_sweep(i):
        seen_idx.append(i)
        return [items[i % len(items)]]

    s2 = make_watch_stream(_FakeClient(), items, interval_seconds=10.0,
                           sleep=sleep, clock=clock, now=_ob,
                           sweep_items=_by_sweep)
    s2.run(on_record=lambda r: None, max_seconds=5.0)
    assert seen_idx == [0], seen_idx
    assert s2._fetch_page.raw_items == 1, "sweep 대상이 1종목인데 전체를 불렀다"
    # 빈 sweep 은 조용히 넘어가지 않는다
    s3 = make_watch_stream(_FakeClient(), items, interval_seconds=10.0,
                           sleep=sleep, clock=clock, now=_ob,
                           sweep_items=lambda i: [])
    try:
        s3.run(on_record=lambda r: None, max_seconds=5.0)
        raise AssertionError("빈 sweep 이 통과했다")
    except NaverNewsError:
        pass

    # 빈 Watchlist 를 정상으로 보지 않는다
    try:
        make_watch_stream(_FakeClient(), [], interval_seconds=1.0)
        raise AssertionError("빈 Watchlist 가 통과했다")
    except NaverNewsError:
        pass
    print("  Stream 계약              OK")


def _check_license_gate():
    """본문·Embedding 권한이 없다는 것을 Registry 가 강제하는지."""
    from source_registry import SourceUseNotAllowed

    env = {"NAVER_CLIENT_ID": "i", "NAVER_CLIENT_SECRET": "s"}
    r = SourceRegistry(env=env)
    r.check_use(SOURCE_ID, UseScope.SEARCH_ONLY)
    r.check_use(SOURCE_ID, UseScope.SNIPPET_STORE)
    for scope in (UseScope.FULLTEXT_STORE, UseScope.EMBEDDING,
                  UseScope.LONG_TERM_ARCHIVE, UseScope.REDISTRIBUTE):
        try:
            r.check_use(SOURCE_ID, scope)
            raise AssertionError(f"{scope} 가 허용됐다")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 Gate            OK")


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _probe(query: str) -> int:
    c = NaverNewsClient()
    recs = c.fetch(query, display=10)
    print(f"  '{query}' -> {len(recs)}건 (호출 {c.calls}회)")
    for r in recs[:6]:
        lag = r.ingest_lag.total_seconds() / 60
        print(f"    [{r.published_at.astimezone(timezone(timedelta(hours=9))):%m-%d %H:%M}] "
              f"{r.publisher[:22]:24} {r.title[:52]}")
        print(f"        lag={lag:.0f}분  {r.external_id[:46]}")
    return 0


def _collect(top: int = 40, symbols: tuple[str, ...] = ()) -> int:
    """Watchlist 를 한 바퀴 돌아 적재한다."""
    from reference_repository import SupabaseReferenceRepository

    ref = SupabaseReferenceRepository()
    try:
        items = load_watchlist(ref, top=top, symbols=symbols)
        print(f"  Watchlist {len(items)}종목: {', '.join(i.name for i in items[:8])} ...")

        client = NaverNewsClient()
        cursor = StreamCursor()
        stream = make_watch_stream(client, items, display=30, interval_seconds=1.0,
                                   cursor=cursor)

        got: list[NewsRecord] = []
        stats = stream.run(on_record=got.append, max_seconds=0.1)
        print(f"  {stats.summary()}")
        print(f"  호출 {client.calls}회 / 일 한도 {DAILY_QUOTA}")
        if not got:
            print("  ⚠ 0건이다 - 빈 결과를 정상으로 보지 않는다")
            return 1

        # 어느 종목 질의에서 나왔는지는 record.symbols 에 실려 있다. 제목 포함 여부는
        # 신뢰도만 가른다 - **질의로 나온 이상 관련은 있다.**
        by_symbol = {it.symbol: it for it in items}
        rel: dict[str, list[tuple[WatchItem, str, str]]] = {}
        for r in got:
            for sym in r.symbols:
                it = by_symbol.get(sym)
                if it is not None:
                    rel.setdefault(r.external_id, []).append(
                        (it, *relation_for(r, it, all_names={w.name for w in items}))
                    )
        ded = sum(1 for v in rel.values() if any(x[1] == "DEDICATED" for x in v))
        multi = sum(1 for v in rel.values() if len(v) > 1)
        print(
            f"  종목 연결: 기사 {len(rel)}/{len(got)}건 (전용 {ded}, 복수종목 {multi})"
        )

        new_src, upd_src, id_by_source = ref.sync_data_sources()
        src = id_by_source.get(SOURCE_ID)
        if src is None:
            raise NaverNewsError(f"data_sources 에 {SOURCE_ID} 가 없다")

        new, upd, id_by_ext = ref.upsert_news_documents(got, source_id=src)
        print(f"  research.documents: 신규 {new} 갱신 {upd}")
        if ref.last_revisions:
            print(f"  ⚠ 정정 탐지 {len(ref.last_revisions)}건 (최초 관측본 유지)")

        links = []
        for ext, pairs in rel.items():
            did = id_by_ext.get(ext)
            if did is None:
                continue
            for it, relation, conf in pairs:
                links.append((did, it.instrument_id, relation, conf))
        deduped = {(d, i, rt): (d, i, rt, c) for d, i, rt, c in links}
        added = ref.link_documents_to_instruments(list(deduped.values()))
        print(f"  document_instruments: 신규 {added} (시도 {len(deduped)})")

        again = ref.link_documents_to_instruments(list(deduped.values()))
        if again:
            raise NaverNewsError("재연결이 새 행을 만들었다")
        print(f"  멱등 재시도: 신규 {again}")
    finally:
        ref.close()
    return 0


def _stream(seconds: float, top: int = 20) -> int:
    from reference_repository import SupabaseReferenceRepository

    ref = SupabaseReferenceRepository()
    try:
        items = load_watchlist(ref, top=top)
    finally:
        ref.close()

    client = NaverNewsClient()
    stream = make_watch_stream(client, items, display=20, interval_seconds=30.0)
    print(f"  {len(items)}종목 폴링 Stream, {seconds}초 (WebSocket 과 같은 인터페이스)")

    def on_record(r: NewsRecord) -> None:
        kst = r.published_at.astimezone(timezone(timedelta(hours=9)))
        print(f"    [{kst:%H:%M}] {r.publisher[:20]:22} {r.title[:56]}")

    stats = stream.run(on_record=on_record, max_seconds=seconds)
    print(f"  {stats.summary()}")
    print(f"  호출 {client.calls}회")
    return 0 if stats.emitted else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        q = sys.argv[i + 1] if len(sys.argv) > i + 1 else "삼성전자"
        print(f"{COLLECTOR_VERSION} 단발 조회")
        raise SystemExit(_probe(q))
    if "--collect" in sys.argv:
        n, syms = 40, ()
        if "--top" in sys.argv:
            n = int(sys.argv[sys.argv.index("--top") + 1])
        if "--symbols" in sys.argv:
            syms = tuple(s.strip() for s in sys.argv[sys.argv.index("--symbols") + 1].split(",") if s.strip())
        print(f"{COLLECTOR_VERSION} Watchlist 수집")
        raise SystemExit(_collect(top=n, symbols=syms))
    if "--stream" in sys.argv:
        secs = 90.0
        if "--seconds" in sys.argv:
            secs = float(sys.argv[sys.argv.index("--seconds") + 1])
        print(f"{COLLECTOR_VERSION} 폴링 Stream")
        raise SystemExit(_stream(secs))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_parse()
    _check_parse_rejects()
    _check_external_id_stability()
    _check_client_limits()
    _check_relation()
    _check_stream_contract()
    _check_license_gate()
    print("NAVER 뉴스 7개 영역 통과. 실제 수집은 --probe / --collect / --stream")
