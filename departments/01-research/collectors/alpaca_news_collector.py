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

▶ 이 Source 로 할 수 없는 것 - 착각하면 위험한 순서대로
  1. **국내 P0 뉴스를 대체하지 못한다.** Get Assets 의 exchange enum 이
     AMEX/ARCA/BATS/NYSE/NASDAQ/NYSEARCA/OTC/CRYPTO 뿐이라 KRX 종목이 없다.
     그래서 Registry 에 P1 / FOREIGN_MARKET 으로 등록돼 있고, P0 NEWS Blocked 는
     이 Source 가 살아 있어도 풀리지 않는다(source_registry 의 Scope Gate).
  2. **본문을 저장하지 않는다.** 약관이 "personal and noncommercial access and use"
     이고 "encoded" 를 금지 행위로 열거한다. allowed_uses 가 SEARCH_ONLY 뿐이라
     content 를 DB·Vector 로 넣으려 하면 Registry 가 막는다(가이드 3.3).
     그래서 REST 호출에 include_content 를 보내지 않는다.
  3. **심볼을 instrument 로 연결하지 못한다.** research.document_instruments 의
     instrument_id 가 reference.instruments 를 FK 로 걸고 있는데 미국 심볼은 거기
     없다. 없는 종목을 만들어 넣지 않고 **미해결 심볼 수를 세어서 보고한다.**
     이걸 해결하려면 Instrument Master 를 미국까지 넓혀야 하고 그건 ADR 사안이다.

자체 점검(호출 없음): python departments/01-research/collectors/alpaca_news_collector.py
REST 백필:            python departments/01-research/collectors/alpaca_news_collector.py --backfill
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
) -> list[NewsRecord]:
    """페이지를 이어 받아 정규화한다. max_pages 는 무한루프 방지용 상한이다."""
    out: list[NewsRecord] = []
    token: str | None = None
    for page in range(max_pages):
        items, token = fetch_news_page(
            creds, start=start, end=end, symbols=symbols, page_token=token
        )
        observed_at = datetime.now(timezone.utc)
        for it in items:
            out.append(normalize(it, observed_at=observed_at))
        if not token:
            return out
    # 상한에 걸렸으면 조용히 자르지 않고 알린다(가이드 8.2 - 축소를 숨기지 않는다).
    raise AlpacaNewsError(
        f"max_pages={max_pages} 로 잘렸다. 남은 page_token 이 있다 - 구간을 좁혀 재수집할 것"
    )


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


def _backfill(days: int = 1) -> int:
    """최근 구간 REST 백필. 키가 없으면 여기서 막힌다."""
    from reference_repository import SupabaseReferenceRepository

    creds = AlpacaCredentials.from_env()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    print(f"  구간: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} UTC")

    records = fetch_news(creds, start=start, end=end)
    print(f"  수집: {len(records)}건")
    if not records:
        print("  ⚠ 0건이다. 구간이나 권한을 확인할 것 - 빈 결과를 정상으로 보지 않는다")
        return 1

    res = resolve_symbols(records, known={})
    print(f"  심볼: 미해결 {res.unresolved_count}종 (미국 종목이 Instrument Master 에 없다)")
    print(f"  샘플: {', '.join(sorted(res.unresolved)[:12])}")

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
        print(f"{COLLECTOR_VERSION} REST 백필")
        raise SystemExit(_backfill())
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
    _check_no_content_requested()
    print("Alpaca 뉴스 6개 영역 통과. 실제 수집은 --backfill / --stream")
