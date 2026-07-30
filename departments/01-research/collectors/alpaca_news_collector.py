#!/usr/bin/env python3
"""Alpaca Market News(Benzinga) 수집 - 미국 종목 뉴스.

소유: 재일 (리서치본부)
근거: https://docs.alpaca.markets/reference/news-3 (REST /v1beta1/news)
      https://docs.alpaca.markets/docs/streaming-real-time-news (WebSocket)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.2(P1), 3.3(수집 금지), 5.4(Vendor 체크리스트)

▶ 왜 Alpaca 인가
  2026-07-31 뉴스 API 5종 조사(Polygon/finlight/Finnhub/AlphaVantage/Alpaca)에서
  **무료 플랜 + 뉴스 WebSocket** 을 동시에 만족하는 유일한 곳이었다.
    - Polygon(현 Massive): 뉴스 WebSocket 자체가 없다. Benzinga 실시간도 REST 다.
    - Finnhub: 뉴스 WS 는 있으나 Premium 전용이고 US·캐나다 한정.
    - Alpha Vantage: 문서 전체에 websocket 문자열이 0회. 무료는 하루 25요청.
    - finlight: 한국어를 포함하지만 무료 플랜은 REST 전용(WS 는 Pro 이상).

▶ 한국 커버리지 - 처음 판단을 실측이 절반 뒤집었다 (2026-07-31)
  KRX 단축코드(005930)로는 **0건** 이 맞다. 그러나 한국 기업의 **미국 상장 심볼**
  로는 그 기업 전용 기사가 실제로 온다. 후보 29개 티커를 90일 창으로 전수 조회한
  결과 KRX 상장 + 전용 기사 확인이 **8종목** 이다(KR_LISTINGS 참고).
  기사의 주체가 한국 발행사이므로 research.document_instruments 를 KRX
  instrument_id 로 채울 수 있다 - Instrument Master 를 미국까지 넓힐 필요가 없다.

  **다만 코스피200+코스닥150(350종목) 대비 2.3% 이고 코스닥은 0개다.**
  지수 유니버스 커버 용도로는 쓸 수 없다. 대형주 보조 신호일 뿐이며 국내 P0
  뉴스(BIGKinds/NAVER)를 대체하지 못한다.

▶ 이 Source 로 할 수 없는 것 - 착각하면 위험한 순서대로
  1. **국내 P0 뉴스를 대체하지 못한다.** 위 커버리지 때문이다. Registry 에
     P1 / FOREIGN_MARKET 으로 등록돼 있고, P0 NEWS Blocked 는 이 Source 가 살아
     있어도 풀리지 않는다(source_registry 의 Scope Gate).
  2. **본문을 저장하지 않는다.** 약관이 "personal and noncommercial access and use"
     이고 "encoded" 를 금지 행위로 열거한다. allowed_uses 가 SEARCH_ONLY 뿐이라
     content 를 DB·Vector 로 넣으려 하면 Registry 가 막는다(가이드 3.3).
     그래서 REST 호출에 include_content 를 보내지 않는다.
  3. **미국 종목은 instrument 로 연결하지 않는다.** document_instruments 의
     instrument_id 가 reference.instruments FK 인데 미국 심볼은 거기 없다. 없는
     종목을 만들지 않고 미해결 심볼 수를 세어서 보고한다. Instrument Master 의
     미국 확장은 ADR 사안이다. 한국 기업은 이 문제가 없다(위 참고).
  4. **한국어 기사는 오지 않는다.** 전부 영문이다.

자체 점검(호출 없음): python departments/01-research/collectors/alpaca_news_collector.py
한국 기업 기사:       python departments/01-research/collectors/alpaca_news_collector.py --korea [--days N]
전체 REST 백필:       python departments/01-research/collectors/alpaca_news_collector.py --backfill [--hours N]
실시간 스트림:        python departments/01-research/collectors/alpaca_news_collector.py --stream [--seconds N]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
from source_registry import SourceRegistry, UseScope, load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-alpaca-news-v1"
SOURCE_ID = "alpaca_news"

DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
NEWS_PATH = "/v1beta1/news"

# 문서: limit 은 1-50. 넘겨 보내면 400 이므로 상한을 코드가 지킨다.
NEWS_LIMIT_MAX = 50

DOCUMENT_TYPE = "NEWS"
LANGUAGE = "en"  # Benzinga 원천이라 전부 영문이다. 한국어 기사는 오지 않는다.

# 발행 시각으로 무엇을 쓸지. Alpaca 는 created_at 과 updated_at 을 둘 다 준다.
# published_at 은 created_at 이다 - updated_at 을 쓰면 기사가 수정될 때마다 과거
# 시점 판단의 근거 시각이 미래로 움직여 PIT 재현이 깨진다(가이드 4.2).
PUBLISHED_FROM = "created_at"


class AlpacaNewsError(RuntimeError):
    """Alpaca 뉴스 수집 실패. 빈 결과로 바꾸지 않는다."""


# ---------------------------------------------------------------------------
# 정규화 계약
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsRecord:
    """research.documents 한 행. 공급자 필드명이 여기서 끊긴다.

    content 를 일부러 담지 않는다 - 담을 수 있게 해 두면 언젠가 저장된다.
    """

    external_id: str
    title: str
    canonical_url: str | None
    published_at: datetime
    updated_at: datetime
    observed_at: datetime
    symbols: tuple[str, ...]
    publisher: str
    author: str
    document_type: str = DOCUMENT_TYPE
    language: str = LANGUAGE
    status: str = "ACTIVE"
    # 원문에 요약이 있었는지만 남긴다. 요약 자체는 저장하지 않는다(SEARCH_ONLY).
    had_summary: bool = False
    had_content: bool = False


def _parse_rfc3339(raw: object, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise AlpacaNewsError(f"{field_name} 가 비었다: {raw!r}")
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise AlpacaNewsError(f"{field_name} 를 RFC-3339 로 읽지 못했다: {raw!r}") from None
    # naive 를 UTC 로 가정하지 않는다. Timezone 없는 시각은 PIT 에서 위험하다.
    if dt.tzinfo is None:
        raise AlpacaNewsError(f"{field_name} 에 Timezone 이 없다: {raw!r}")
    return dt.astimezone(timezone.utc)


def normalize(item: dict, *, observed_at: datetime) -> NewsRecord:
    """REST 항목과 WebSocket 메시지가 같은 필드를 쓰므로 한 함수로 처리한다."""
    news_id = item.get("id")
    if news_id is None:
        raise AlpacaNewsError(f"id 가 없다: keys={sorted(item)}")

    headline = str(item.get("headline") or "").strip()
    if not headline:
        raise AlpacaNewsError(f"headline 이 비었다 id={news_id}")

    created = _parse_rfc3339(item.get("created_at"), "created_at")
    updated_raw = item.get("updated_at")
    updated = _parse_rfc3339(updated_raw, "updated_at") if updated_raw else created

    raw_symbols = item.get("symbols") or []
    if not isinstance(raw_symbols, list):
        raise AlpacaNewsError(f"symbols 가 리스트가 아니다 id={news_id}")
    symbols = tuple(sorted({str(s).strip().upper() for s in raw_symbols if str(s).strip()}))

    url = item.get("url")
    url = str(url).strip() if url else None

    return NewsRecord(
        # source_id 와 함께 Unique 이므로 Provider 안에서만 유일하면 된다.
        external_id=f"alpaca:{news_id}",
        title=headline,
        canonical_url=url or None,
        published_at=created,
        updated_at=updated,
        observed_at=observed_at,
        symbols=symbols,
        publisher=str(item.get("source") or "").strip(),
        author=str(item.get("author") or "").strip(),
        had_summary=bool(str(item.get("summary") or "").strip()),
        had_content=bool(str(item.get("content") or "").strip()),
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlpacaCredentials:
    key_id: str
    secret_key: str
    data_base_url: str
    news_ws_url: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AlpacaCredentials:
        e = env or load_project_env()
        # 키가 없으면 여기서 예외다. 빈 결과로 흘려보내지 않는다.
        SourceRegistry(env=e).require(SOURCE_ID)
        return cls(
            key_id=e["ALPACA_API_KEY_ID"],
            secret_key=e["ALPACA_API_SECRET_KEY"],
            data_base_url=(e.get("ALPACA_DATA_BASE_URL") or DEFAULT_DATA_BASE_URL).rstrip("/"),
            news_ws_url=e.get("ALPACA_NEWS_WS_URL") or DEFAULT_NEWS_WS_URL,
        )

    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "accept": "application/json",
        }


def fetch_news_page(
    creds: AlpacaCredentials,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    symbols: tuple[str, ...] = (),
    limit: int = NEWS_LIMIT_MAX,
    page_token: str | None = None,
    timeout: int = 20,
) -> tuple[list[dict], str | None]:
    """뉴스 한 페이지. (항목, next_page_token).

    include_content 를 보내지 않는다 - 본문 저장 권한이 없으므로 받지도 않는다.
    받아 두면 언젠가 저장된다.
    """
    if not 1 <= limit <= NEWS_LIMIT_MAX:
        raise ValueError(f"limit 은 1~{NEWS_LIMIT_MAX} 다: {limit}")

    params: dict[str, str] = {"limit": str(limit), "sort": "asc"}
    if start is not None:
        params["start"] = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if end is not None:
        params["end"] = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if symbols:
        params["symbols"] = ",".join(symbols)
    if page_token:
        params["page_token"] = page_token

    url = f"{creds.data_base_url}{NEWS_PATH}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET", headers=creds.headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise AlpacaNewsError(f"{NEWS_PATH} HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise AlpacaNewsError(f"{NEWS_PATH} 연결 실패: {e.reason}") from None

    items = payload.get("news")
    if items is None:
        raise AlpacaNewsError(f"news 키가 응답에 없다: keys={sorted(payload)}")
    return items, payload.get("next_page_token")


def fetch_news(
    creds: AlpacaCredentials,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    symbols: tuple[str, ...] = (),
    max_pages: int = 20,
    on_page=None,
) -> list[NewsRecord]:
    """페이지를 이어 받아 정규화한다. max_pages 는 무한루프 방지용 상한이다.

    상한에 걸리면 **잘린 결과를 돌려주지 않고 예외** 다. 조용히 자르면 "그 구간에
    기사가 이만큼밖에 없었다" 로 읽히고, 그건 DQ 를 통과해 버린다(가이드 8.2).
    """
    out: list[NewsRecord] = []
    token: str | None = None
    for page in range(max_pages):
        items, token = fetch_news_page(
            creds, start=start, end=end, symbols=symbols, page_token=token
        )
        observed_at = datetime.now(timezone.utc)
        for it in items:
            out.append(normalize(it, observed_at=observed_at))
        if on_page is not None:
            on_page(page + 1, len(items), len(out))
        if not token:
            return out
    raise AlpacaNewsError(
        f"max_pages={max_pages}({max_pages * NEWS_LIMIT_MAX}건)에서 잘렸다. 아직 "
        f"page_token 이 남아 있다 - 구간을 좁히거나 max_pages 를 올려 재수집할 것"
    )


# ---------------------------------------------------------------------------
# 한국 기업의 미국 상장 경로 - 전부 실측 검증했다
# ---------------------------------------------------------------------------
#
# ▶ 앞선 판단을 실측이 뒤집었다 (2026-07-31)
#   "Alpaca 는 한국을 커버하지 않는다" 가 절반만 맞았다. KRX 단축코드(005930)로는
#   확실히 0건이다. 그러나 **한국 기업의 미국 상장 심볼로는 그 기업 전용 기사가
#   실제로 들어온다.** SK텔레콤·신한지주·우리금융은 영문 분기 실적 기사까지 온다.
#
#   그리고 이 경로가 성립하는 이유가 중요하다 - 기사가 가리키는 **주체가 한국
#   발행사** 이고 그 발행사는 reference.issuers 에 이미 있다. 그래서
#   research.document_instruments 를 KRX instrument_id 로 채울 수 있고,
#   Instrument Master 를 미국까지 넓힐 필요가 없다.
#
# ▶ 검증 방법
#   최근 90일 기사를 받아 헤드라인이 그 회사를 직접 지칭하는 건수를 셌다.
#   0건이면 등록하지 않는다 - 매핑이 틀렸을 수도, 기사가 없었을 수도 있는데
#   구분할 수 없기 때문이다(확인 전에는 넣지 않는다는 Registry 원칙과 같다).

VERIFIED_ON = "2026-07-31"


@dataclass(frozen=True)
class KrListing:
    """한국 기업 하나의 미국 상장 경로."""

    us_symbol: str
    krx_symbol: str | None   # None = KRX 미상장(미국에만 상장된 한국 기업)
    name_ko: str
    # 헤드라인이 이 회사를 직접 지칭하는지 판정하는 정규식(소문자 기준).
    headline_pattern: str
    dedicated_90d: int       # 검증 시점 실측 건수


# ▶ 커버리지 실측 (2026-07-31, 90일 창, 후보 29개 티커 전수 조회)
#   KRX 상장 + 전용 기사 확인 = **8개 종목뿐이다.** 전부 KOSPI 초대형주이고
#   **코스닥은 0개** 다. 코스피200(200) + 코스닥150(150) = 350 종목 대비 2.3% 다.
#   이 Source 로 코스피200·코스닥150 을 커버하는 것은 불가능하다 - 국내 P0 뉴스
#   (BIGKinds/NAVER)를 대체하지 못한다는 결론이 여기서 재확인된다.
KR_LISTINGS: tuple[KrListing, ...] = (
    KrListing("SSNLF", "005930", "삼성전자", r"samsung", 18),
    KrListing("HYMLF", "005380", "현대차", r"hyundai", 7),
    KrListing("SKM", "017670", "SK텔레콤", r"sk telecom", 6),
    KrListing("HYMTF", "005380", "현대차", r"hyundai", 5),
    KrListing("LPL", "034220", "LG디스플레이", r"lg\s*display", 5),
    KrListing("HXSCL", "000660", "SK하이닉스", r"hynix", 4),
    KrListing("SSNGY", "005930", "삼성전자", r"samsung", 2),
    KrListing("PKX", "005490", "POSCO홀딩스", r"posco", 2),
    KrListing("SHG", "055550", "신한지주", r"shinhan", 1),
    KrListing("WF", "316140", "우리금융지주", r"woori", 1),
    # KRX 미상장 - instrument 연결은 불가하지만 한국 기업 뉴스로는 가치가 있다
    KrListing("CPNG", None, "쿠팡", r"coupang", 10),
    KrListing("GRVY", None, "그라비티", r"gravity", 5),
)

# 같은 기업에 미국 심볼이 여럿 붙는다(삼성전자 SSNLF/SSNGY, 현대차 HYMTF/HYMLF).
# HYMLF 는 우선주 계열로 알려져 있으나 어느 KRX 우선주에 대응하는지 확인하지 못했다.
# 뉴스의 **주체는 발행기업** 이므로 둘 다 보통주 instrument 에 연결하고, 정확한
# 주식종류 대응은 확인 전까지 주장하지 않는다.

# 개별 기업이 아니라 시장 단위 신호. instrument 로 연결하지 않는다.
KR_MARKET_PROXIES: tuple[KrListing, ...] = (
    KrListing("EWY", None, "MSCI 한국 ETF", r"korea|kospi|hynix|samsung", 29),
)

# 90일 창에서 전용 기사가 0건이라 등록하지 않은 것. 매핑이 틀린 게 아니라
# **확인되지 않은** 것이다. 기사가 생기면 검증 후 위로 옮긴다.
KR_LISTINGS_UNVERIFIED: dict[str, str] = {
    "KEP": "015760 한국전력 - 90일간 기사 5건 전부 시황 나열, 전용 0",
    "KB": "105560 KB금융 - 90일간 기사 2건, 전용 0",
    "LGEIY": "066570 LG전자 - 90일간 기사 1건, 전용 0",
    "NHNCF": "035420 NAVER - 90일간 기사 1건, 전용 0",
    "KKKKF": "035720 카카오 - 90일간 기사 0건",
    "KIMTF": "000270 기아 - 90일간 기사 0건",
    "LGCLF": "051910 LG화학 - 90일간 기사 0건",
    "SDIFF": "006400 삼성SDI - 90일간 기사 0건",
    "AMSSY": "090430 아모레퍼시픽 - 90일간 기사 0건",
    "SKKKF": "096770 SK이노베이션 - 90일간 기사 0건",
}

# 코스피200 200 + 코스닥150 150 = 350 종목 대비 실제 커버.
KOSPI200_KOSDAQ150_TOTAL = 350
COVERED_KRX_SYMBOLS = frozenset(
    lst.krx_symbol for lst in KR_LISTINGS if lst.krx_symbol is not None
)

# 한 기사에 심볼이 이만큼 넘게 붙어 있으면 개별 기업 기사가 아니라 시황 나열이다.
# 실측: 신한지주로 잡힌 기사 하나에 심볼이 128개였다.
ROUNDUP_SYMBOL_THRESHOLD = 8

_KR_BY_SYMBOL: dict[str, KrListing] = {
    lst.us_symbol: lst for lst in (*KR_LISTINGS, *KR_MARKET_PROXIES)
}


def korea_symbols(*, include_proxies: bool = True) -> tuple[str, ...]:
    """한국 기업 뉴스를 받기 위해 구독할 미국 심볼."""
    out = [lst.us_symbol for lst in KR_LISTINGS]
    if include_proxies:
        out.extend(lst.us_symbol for lst in KR_MARKET_PROXIES)
    return tuple(out)


@dataclass(frozen=True)
class KrRelevance:
    """기사 하나가 어느 한국 기업과 어떻게 관련되는지."""

    listing: KrListing
    # DEDICATED - 헤드라인이 그 회사를 직접 지칭한다
    # MENTIONS  - 심볼 목록에만 있다(시황 나열일 수 있다)
    relation: str
    confidence: Decimal

    @property
    def is_dedicated(self) -> bool:
        return self.relation == "DEDICATED"


def classify_korea(record: NewsRecord) -> tuple[KrRelevance, ...]:
    """기사에 걸린 한국 기업 관련도를 판정한다.

    심볼이 많이 붙은 시황 나열 기사는 헤드라인 지칭이 없으면 신뢰도를 낮춘다.
    **버리지는 않는다** - "여러 종목이 함께 움직였다" 도 정보다. 다만 개별 기업
    기사와 같은 무게로 두면 Agent 가 잘못 읽는다.
    """
    import re as _re

    roundup = len(record.symbols) > ROUNDUP_SYMBOL_THRESHOLD
    title_lc = record.title.lower()
    out: list[KrRelevance] = []
    for sym in record.symbols:
        lst = _KR_BY_SYMBOL.get(sym)
        if lst is None:
            continue
        if _re.search(lst.headline_pattern, title_lc):
            out.append(KrRelevance(lst, "DEDICATED", Decimal("0.95")))
        else:
            out.append(
                KrRelevance(lst, "MENTIONS", Decimal("0.3") if roundup else Decimal("0.6"))
            )
    return tuple(out)


# ---------------------------------------------------------------------------
# 심볼 해결 - 없는 종목을 만들지 않는다
# ---------------------------------------------------------------------------

@dataclass
class SymbolResolution:
    """기사 심볼 중 우리 Instrument Master 에 있는 것과 없는 것."""

    resolved: dict[str, object] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved)


def resolve_symbols(records: list[NewsRecord], known: dict[str, object]) -> SymbolResolution:
    """known 에 있는 심볼만 연결한다. 나머지는 세기만 하고 만들지 않는다.

    지금은 known 이 항상 비어 있다 - reference.instruments 에 미국 종목이 없기
    때문이다. 그래서 unresolved 가 전부인 것이 **정상이며 버그가 아니다.**
    Instrument Master 를 미국까지 넓히는 것은 ADR 사안이다.
    """
    res = SymbolResolution()
    for r in records:
        for s in r.symbols:
            if s in known:
                res.resolved[s] = known[s]
            else:
                res.unresolved.add(s)
    return res


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def stream_news(
    creds: AlpacaCredentials,
    *,
    on_record,
    symbols: tuple[str, ...] = ("*",),
    max_seconds: float | None = None,
    max_messages: int | None = None,
) -> int:
    """뉴스 WebSocket 을 구독하고 정규화된 레코드마다 on_record 를 부른다.

    requirements.txt 의 `websockets`(asyncio) 를 쓴다. websocket-client 가 아니다 -
    저장소에 이미 있는 의존성을 쓰고 새 Library 를 늘리지 않는다(개발 원칙 8).

    인증은 접속 후 **auth 메시지** 로 한다. 문서 예제가 헤더를 보여주지만 실제
    스트림은 접속 뒤 {"action":"auth",...} 를 요구하므로 둘 다 보낸다.
    """
    import asyncio

    try:
        import websockets  # requirements.txt 22행
    except ImportError:
        raise AlpacaNewsError(
            "websockets 가 없다. pip install -r requirements.txt 후 다시 실행한다"
        ) from None

    async def _run() -> int:
        seen = 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with websockets.connect(
            creds.news_ws_url,
            additional_headers=creds.headers(),
            open_timeout=30,
            ping_interval=20,
        ) as ws:
            # 접속 직후 [{"T":"success","msg":"connected"}] 가 온다.
            await ws.recv()
            await ws.send(
                json.dumps({"action": "auth", "key": creds.key_id, "secret": creds.secret_key})
            )
            auth_reply = json.loads(await ws.recv())
            if not _is_success(auth_reply, "authenticated"):
                raise AlpacaNewsError(f"인증 실패: {auth_reply}")

            await ws.send(json.dumps({"action": "subscribe", "news": list(symbols)}))
            await ws.recv()  # subscription 확인

            while True:
                if max_seconds is not None and loop.time() - started >= max_seconds:
                    return seen
                if max_messages is not None and seen >= max_messages:
                    return seen
                remaining = None if max_seconds is None else max(
                    0.1, max_seconds - (loop.time() - started)
                )
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    return seen
                except Exception as e:
                    raise AlpacaNewsError(f"스트림이 끊겼다: {e}") from None
                if not raw:
                    continue
                observed_at = datetime.now(timezone.utc)
                for msg in json.loads(raw):
                    if msg.get("T") != "n":
                        continue
                    on_record(normalize(msg, observed_at=observed_at))
                    seen += 1

    return asyncio.run(_run())


def _is_success(reply: object, msg: str) -> bool:
    return isinstance(reply, list) and any(
        isinstance(m, dict) and m.get("T") == "success" and m.get("msg") == msg for m in reply
    )


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_SAMPLE = {
    "id": 24843171,
    "headline": "Apple Reports Q3 Earnings Beat",
    "author": "Benzinga Newsdesk",
    "created_at": "2026-07-30T20:15:00Z",
    "updated_at": "2026-07-30T20:41:00Z",
    "summary": "Apple reported quarterly earnings above estimates.",
    "content": "<p>Apple Inc reported ...</p>",
    "source": "benzinga",
    "url": "https://www.benzinga.com/news/26/07/24843171/apple-q3",
    "symbols": ["AAPL", "aapl", " MSFT "],
    "images": [{"size": "large", "url": "https://example.invalid/a.png"}],
}


def _check_normalize():
    ob = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
    r = normalize(_SAMPLE, observed_at=ob)
    assert r.external_id == "alpaca:24843171"
    assert r.title == "Apple Reports Q3 Earnings Beat"
    assert r.language == "en" and r.document_type == "NEWS"
    # 심볼은 대문자·중복 제거·정렬
    assert r.symbols == ("AAPL", "MSFT"), r.symbols
    # published_at 은 created_at 이다. updated_at 을 쓰면 PIT 가 미래로 움직인다.
    assert r.published_at == datetime(2026, 7, 30, 20, 15, tzinfo=timezone.utc)
    assert r.updated_at == datetime(2026, 7, 30, 20, 41, tzinfo=timezone.utc)
    assert r.observed_at == ob
    # 본문·요약은 존재 여부만 남기고 값을 들고 있지 않는다
    assert r.had_summary is True and r.had_content is True
    assert not hasattr(r, "content") and not hasattr(r, "summary")
    assert "content" not in {f for f in r.__dataclass_fields__}
    print("  정규화                   OK")


def _check_normalize_rejects():
    ob = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
    bad_cases = [
        ({**_SAMPLE, "id": None}, "id"),
        ({**_SAMPLE, "headline": "  "}, "headline"),
        ({**_SAMPLE, "created_at": ""}, "created_at"),
        ({**_SAMPLE, "created_at": "2026-07-30 20:15:00"}, "Timezone"),  # tz 없음
        ({**_SAMPLE, "created_at": "not-a-date"}, "RFC-3339"),
        ({**_SAMPLE, "symbols": "AAPL"}, "symbols"),
    ]
    for payload, expect in bad_cases:
        try:
            normalize(payload, observed_at=ob)
            raise AssertionError(f"{expect} 불량이 통과했다")
        except AlpacaNewsError as e:
            assert expect in str(e), f"{expect} 기대했는데 {e}"

    # updated_at 이 없으면 created_at 으로 떨어진다(추정이 아니라 동일 시점 처리)
    no_upd = {k: v for k, v in _SAMPLE.items() if k != "updated_at"}
    r = normalize(no_upd, observed_at=ob)
    assert r.updated_at == r.published_at

    # url 이 null 이어도 통과해야 한다(문서상 nullable)
    r2 = normalize({**_SAMPLE, "url": None}, observed_at=ob)
    assert r2.canonical_url is None
    print("  불량 입력 거부           OK")


def _check_license_gate():
    """본문 저장 권한이 없다는 것을 Registry 가 강제하는지."""
    from source_registry import SourceUseNotAllowed

    env = {"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"}
    r = SourceRegistry(env=env)
    r.check_use(SOURCE_ID, UseScope.SEARCH_ONLY)
    for scope in (UseScope.FULLTEXT_STORE, UseScope.EMBEDDING,
                  UseScope.LONG_TERM_ARCHIVE, UseScope.REDISTRIBUTE, UseScope.SNIPPET_STORE):
        try:
            r.check_use(SOURCE_ID, scope)
            raise AssertionError(f"{scope} 가 허용됐다 - 약관 위반")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 Gate            OK")


def _check_scope_does_not_unblock_p0():
    """키가 생겨도 국내 P0 NEWS Blocked 가 풀리면 안 된다."""
    from source_registry import SourceDomain, SourceStatus

    env = {"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"}
    r = SourceRegistry(env=env)
    assert r.status(SOURCE_ID) is SourceStatus.AVAILABLE, "전제가 깨졌다"
    assert SourceDomain.NEWS in r.blocked_p0_domains(), (
        "Alpaca 가 살아 있다고 국내 P0 NEWS Blocked 가 풀렸다"
    )
    print("  P0 Blocked 유지          OK")


def _check_symbol_resolution():
    ob = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
    recs = [normalize(_SAMPLE, observed_at=ob)]

    # 지금 상태 - reference.instruments 에 미국 종목이 없다
    res = resolve_symbols(recs, known={})
    assert res.unresolved == {"AAPL", "MSFT"} and not res.resolved
    assert res.unresolved_count == 2

    # 나중에 Instrument Master 가 넓어지면 자동으로 연결된다
    res2 = resolve_symbols(recs, known={"AAPL": "uuid-aapl"})
    assert res2.resolved == {"AAPL": "uuid-aapl"} and res2.unresolved == {"MSFT"}
    print("  심볼 해결                OK")


def _check_korea_mapping():
    """매핑 자체를 검사한다. 이건 코드가 아니라 데이터라서 조용히 틀어지기 쉽다."""
    # 검증된 것과 미검증인 것이 겹치면 안 된다
    verified_us = {lst.us_symbol for lst in (*KR_LISTINGS, *KR_MARKET_PROXIES)}
    assert not verified_us & set(KR_LISTINGS_UNVERIFIED), "검증/미검증이 겹친다"

    # 미국 심볼은 유일해야 한다(_KR_BY_SYMBOL 이 덮어써서 조용히 하나가 사라진다)
    allsyms = [lst.us_symbol for lst in (*KR_LISTINGS, *KR_MARKET_PROXIES)]
    assert len(allsyms) == len(set(allsyms)), f"미국 심볼 중복: {allsyms}"
    assert len(korea_symbols()) == len(allsyms)

    # 전용 기사 0건짜리를 등록해두면 안 된다 - 검증됐다는 근거가 그 숫자다
    for lst in (*KR_LISTINGS, *KR_MARKET_PROXIES):
        assert lst.dedicated_90d > 0, f"{lst.us_symbol}: 전용 0건인데 등록돼 있다"
        assert lst.headline_pattern, f"{lst.us_symbol}: 판정 정규식이 없다"

    # 커버리지 숫자가 매핑과 일치해야 한다
    assert COVERED_KRX_SYMBOLS == {"005930", "005380", "017670", "034220",
                                   "000660", "005490", "055550", "316140"}
    assert len(COVERED_KRX_SYMBOLS) == 8
    # 같은 기업에 미국 심볼이 여럿이어도 KRX 종목 수는 늘지 않아야 한다
    assert len([l for l in KR_LISTINGS if l.krx_symbol == "005930"]) == 2  # SSNLF, SSNGY
    assert len([l for l in KR_LISTINGS if l.krx_symbol == "005380"]) == 2  # HYMTF, HYMLF
    print("  한국 매핑 정합성         OK")


def _check_classify_korea():
    ob = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)

    def mk(title, syms):
        return normalize({**_SAMPLE, "headline": title, "symbols": list(syms)}, observed_at=ob)

    # 헤드라인이 회사를 지칭 -> DEDICATED
    r = classify_korea(mk("Samsung, Broadcom Ink $200 Billion AI Chip Pact", ["SSNLF", "AVGO"]))
    assert len(r) == 1 and r[0].is_dedicated and r[0].listing.krx_symbol == "005930"
    assert r[0].confidence == Decimal("0.95")

    # 심볼에만 있고 헤드라인 지칭 없음 -> MENTIONS
    r = classify_korea(mk("Chip Stocks Rise On Demand Outlook", ["SSNLF", "AVGO"]))
    assert len(r) == 1 and not r[0].is_dedicated and r[0].confidence == Decimal("0.6")

    # 심볼이 잔뜩 붙은 시황 나열 -> 신뢰도를 더 낮춘다(실측: 128개짜리가 있었다)
    many = ["SHG"] + [f"X{i}" for i in range(ROUNDUP_SYMBOL_THRESHOLD + 2)]
    r = classify_korea(mk("Why SurgePays Shares Are Trading Higher By 59%", many))
    assert len(r) == 1 and r[0].confidence == Decimal("0.3"), r

    # 한국과 무관한 기사는 아무것도 안 나온다
    assert classify_korea(mk("Apple Reports Q3 Beat", ["AAPL", "MSFT"])) == ()

    # 한 기사에 여러 한국 기업
    r = classify_korea(mk("Samsung, SK Hynix to Ink Large Chip Supply Deals", ["SSNLF", "HXSCL"]))
    assert len(r) == 2 and all(x.is_dedicated for x in r)
    assert {x.listing.krx_symbol for x in r} == {"005930", "000660"}

    # 같은 기업의 미국 심볼이 둘 다 붙으면 관계가 둘 생긴다 - 호출자가 dedupe 해야 한다
    r = classify_korea(mk("Samsung Stock Jumps Over 6% In Seoul", ["SSNLF", "SSNGY"]))
    assert len(r) == 2 and {x.listing.krx_symbol for x in r} == {"005930"}
    print("  한국 관련도 판정         OK")


def _check_no_content_requested():
    """REST 파라미터에 include_content 가 들어가지 않는지."""
    captured = {}

    class _FakeCreds(AlpacaCredentials):
        pass

    creds = AlpacaCredentials(key_id="k", secret_key="s",
                              data_base_url="https://x", news_ws_url="wss://y")
    orig = urllib.request.urlopen

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return b'{"news": [], "next_page_token": null}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _Resp()

    urllib.request.urlopen = _fake_urlopen
    try:
        fetch_news_page(creds, start=datetime(2026, 7, 30, tzinfo=timezone.utc))
    finally:
        urllib.request.urlopen = orig

    assert "include_content" not in captured["url"], "본문을 요청하고 있다"
    assert "limit=50" in captured["url"] and "sort=asc" in captured["url"]
    assert "start=2026-07-30T00%3A00%3A00Z" in captured["url"], captured["url"]
    # 헤더 이름은 urllib 가 Title-Case 로 정규화한다
    hk = {k.lower() for k in captured["headers"]}
    assert "apca-api-key-id" in hk and "apca-api-secret-key" in hk

    for bad in (0, 51, -1):
        try:
            fetch_news_page(creds, limit=bad)
            raise AssertionError(f"limit={bad} 가 통과했다")
        except ValueError:
            pass
    print("  본문 미요청/파라미터     OK")


def _backfill(hours: float = 2.0, max_pages: int = 40) -> int:
    """최근 구간 REST 백필. 키가 없으면 여기서 막힌다.

    구간을 하루로 잡으면 전 미국 종목 뉴스가 1,000건을 훌쩍 넘어 max_pages 에 걸린다.
    기본을 짧게 두고 필요하면 --hours 로 넓힌다.
    """
    from reference_repository import SupabaseReferenceRepository

    creds = AlpacaCredentials.from_env()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    print(f"  구간: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} UTC ({hours}시간)")

    def on_page(page: int, got: int, total: int) -> None:
        print(f"    page {page:>2}: +{got:>2}  누적 {total}")

    records = fetch_news(creds, start=start, end=end, max_pages=max_pages, on_page=on_page)
    print(f"  수집: {len(records)}건")
    if not records:
        print("  ⚠ 0건이다. 구간이나 권한을 확인할 것 - 빈 결과를 정상으로 보지 않는다")
        return 1

    # 공급자 안에서 external_id 가 유일한지 확인한다. 중복이면 upsert 가 같은 명령에서
    # 같은 행을 두 번 건드려 실패한다(DART 재무에서 실제로 겪은 문제다).
    seen: dict[str, NewsRecord] = {}
    dupes = 0
    for r in records:
        if r.external_id in seen:
            dupes += 1
        else:
            seen[r.external_id] = r
    records = list(seen.values())
    if dupes:
        print(f"  ⚠ 공급자 응답 내 중복 {dupes}건 - 유니크 {len(records)}건으로 적재")

    res = resolve_symbols(records, known={})
    with_sym = sum(1 for r in records if r.symbols)
    print(
        f"  심볼: 기사 {with_sym}/{len(records)}건에 심볼 있음, "
        f"미해결 {res.unresolved_count}종 (미국 종목이 Instrument Master 에 없다)"
    )
    print(f"  샘플: {', '.join(sorted(res.unresolved)[:14])}")
    print(f"  본문 포함 응답: {sum(1 for r in records if r.had_content)}건 "
          f"(include_content 를 안 보내므로 0 이어야 정상)")

    ref = SupabaseReferenceRepository()
    try:
        new_src, upd_src, id_by_source = ref.sync_data_sources()
        print(f"  data_sources 동기화: 신규 {new_src} 갱신 {upd_src}")
        src_uuid = id_by_source.get(SOURCE_ID)
        if src_uuid is None:
            raise AlpacaNewsError(
                f"data_sources 에 {SOURCE_ID} 가 없다. Registry 등록을 확인할 것"
            )
        new, updated = ref.upsert_news_documents(records, source_id=src_uuid)
        print(f"  research.documents: 신규 {new} 갱신 {updated}")
        again = ref.upsert_news_documents(records, source_id=src_uuid)
        print(f"  멱등 재시도: 신규 {again[0]} 갱신 {again[1]}")
        if again[0]:
            raise AlpacaNewsError("재수집이 새 문서를 만들었다 - external_id 규칙 확인")
    finally:
        ref.close()
    return 0


def _korea(days: float = 7.0) -> int:
    """한국 기업 관련 기사만 받아 KRX instrument 에 연결한다.

    이게 이 Source 를 우리 구조에 붙이는 유일하게 의미 있는 방법이다. 기사의 주체가
    한국 발행사이므로 Instrument Master 를 미국까지 넓히지 않고도 연결이 성립한다.
    """
    from reference_repository import SupabaseReferenceRepository

    creds = AlpacaCredentials.from_env()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    syms = korea_symbols()
    print(f"  구간: {start:%Y-%m-%d} ~ {end:%Y-%m-%d} ({days}일)")
    print(f"  구독 심볼 {len(syms)}개: {', '.join(syms)}")
    print(
        f"  커버 KRX 종목 {len(COVERED_KRX_SYMBOLS)}개 / 코스피200+코스닥150 "
        f"{KOSPI200_KOSDAQ150_TOTAL}개 = {len(COVERED_KRX_SYMBOLS)/KOSPI200_KOSDAQ150_TOTAL:.1%}"
    )

    records = fetch_news(creds, start=start, end=end, symbols=syms, max_pages=40)
    uniq = {r.external_id: r for r in records}
    records = list(uniq.values())
    print(f"  수집: {len(records)}건 (유니크)")
    if not records:
        print("  ⚠ 0건이다 - 빈 결과를 정상으로 보지 않는다")
        return 1

    # 기사별 한국 기업 관련도
    rel_by_ext: dict[str, tuple[KrRelevance, ...]] = {}
    ded = 0
    for r in records:
        rels = classify_korea(r)
        rel_by_ext[r.external_id] = rels
        if any(x.is_dedicated for x in rels):
            ded += 1
    print(f"  전용 기사 {ded}건 / 언급만 {len(records) - ded}건")

    by_company: dict[str, int] = {}
    for rels in rel_by_ext.values():
        for x in rels:
            if x.is_dedicated:
                by_company[x.listing.name_ko] = by_company.get(x.listing.name_ko, 0) + 1
    for name, n in sorted(by_company.items(), key=lambda kv: -kv[1]):
        print(f"    {name:14} {n}건")

    ref = SupabaseReferenceRepository()
    try:
        _, _, id_by_source = ref.sync_data_sources()
        src = id_by_source.get(SOURCE_ID)
        if src is None:
            raise AlpacaNewsError(f"data_sources 에 {SOURCE_ID} 가 없다")

        krx = sorted(COVERED_KRX_SYMBOLS)
        iid_by_symbol = ref.resolve_symbols(krx)
        missing = set(krx) - set(iid_by_symbol)
        if missing:
            print(f"  ⚠ instrument 미해결 {sorted(missing)} - 그 종목 연결은 건너뛴다")

        # 전용이 정확히 한 기업일 때만 issuer_id 를 붙인다. 여러 기업 전용 기사는
        # 어느 발행사 문서인지 단정할 수 없다(issuer_id 는 단수다).
        issuer_by_ext: dict[str, object] = {}
        for ext, rels in rel_by_ext.items():
            ded_syms = {x.listing.krx_symbol for x in rels if x.is_dedicated and x.listing.krx_symbol}
            if len(ded_syms) == 1:
                s = ded_syms.pop()
                iss = ref.issuer_for_symbol(s)
                if iss is not None:
                    issuer_by_ext[ext] = iss

        new, upd, id_by_ext = ref.upsert_news_documents(
            records, source_id=src, issuer_by_external_id=issuer_by_ext
        )
        print(f"  research.documents: 신규 {new} 갱신 {upd}, issuer 연결 {len(issuer_by_ext)}건")

        links = []
        for ext, rels in rel_by_ext.items():
            did = id_by_ext.get(ext)
            if did is None:
                continue
            for x in rels:
                iid = iid_by_symbol.get(x.listing.krx_symbol) if x.listing.krx_symbol else None
                if iid is None:
                    continue
                links.append((did, iid, x.relation, x.confidence))
        # 같은 (document, instrument, relation) 이 중복되면 upsert 가 같은 행을 두 번
        # 건드려 실패한다. 삼성전자처럼 미국 심볼이 둘인 기업에서 실제로 생긴다.
        deduped = {(d, i, rt): (d, i, rt, c) for d, i, rt, c in links}
        added = ref.link_documents_to_instruments(list(deduped.values()))
        print(f"  document_instruments: 신규 {added} (시도 {len(deduped)}, 중복 제거 {len(links)-len(deduped)})")

        again = ref.link_documents_to_instruments(list(deduped.values()))
        print(f"  멱등 재시도: 신규 {again}")
        if again:
            raise AlpacaNewsError("재연결이 새 행을 만들었다 - 충돌 키 확인")
    finally:
        ref.close()
    return 0


def _stream(seconds: float) -> int:
    creds = AlpacaCredentials.from_env()
    print(f"  {creds.news_ws_url} 구독 (news=[*]), {seconds}초")
    got: list[NewsRecord] = []

    def on_record(r: NewsRecord) -> None:
        got.append(r)
        print(f"    [{r.published_at:%H:%M:%S}] {', '.join(r.symbols) or '-':20} {r.title[:70]}")

    n = stream_news(creds, on_record=on_record, max_seconds=seconds)
    print(f"  {n}건 수신")
    return 0 if n else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--backfill" in sys.argv:
        hrs = 2.0
        if "--hours" in sys.argv:
            hrs = float(sys.argv[sys.argv.index("--hours") + 1])
        print(f"{COLLECTOR_VERSION} REST 백필")
        raise SystemExit(_backfill(hours=hrs))
    if "--korea" in sys.argv:
        d = 7.0
        if "--days" in sys.argv:
            d = float(sys.argv[sys.argv.index("--days") + 1])
        print(f"{COLLECTOR_VERSION} 한국 기업 기사 수집")
        raise SystemExit(_korea(days=d))
    if "--stream" in sys.argv:
        secs = 60.0
        if "--seconds" in sys.argv:
            secs = float(sys.argv[sys.argv.index("--seconds") + 1])
        print(f"{COLLECTOR_VERSION} 실시간 스트림")
        raise SystemExit(_stream(secs))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_normalize()
    _check_normalize_rejects()
    _check_license_gate()
    _check_scope_does_not_unblock_p0()
    _check_symbol_resolution()
    _check_korea_mapping()
    _check_classify_korea()
    _check_no_content_requested()
    print("Alpaca 뉴스 8개 영역 통과. 실제 수집은 --backfill / --korea / --stream")
