#!/usr/bin/env python3
"""LS 실시간 뉴스 상주 수집 - NWS push 를 documents 로 즉시 적재.

담당: 재일 (리서치/퀀트)
근거: 실측 2026-07-31 (NWS 구독 ack 정상, 뉴스에 realkey·종목코드 동봉,
      t3102 로 본문 3,560자 수신 관통) + 재일님 방침 "NAVER 와 병행 실측 후
      우세하면 주 소스 전환".

▶ NAVER 폴링과 결정적으로 다른 점
  - **진짜 push 다.** 폴링 주기·일 한도가 없다(발생 즉시 수신).
  - 종목코드가 패킷에 붙어 온다(code: 6자리 연결 문자열) - 이름 매칭 추정이
    필요 없다. 단 제목 판정(전용/언급)은 NAVER 와 같은 기준을 쓴다.
  - realkey 가 안정 ID 다 - external_id 로 그대로 쓴다(URL 조립 불필요).
  - 본문은 t3102 로 얻을 수 있으나 **저장하지 않는다**(Registry ls_news 주석 -
    약관 확인 전. 판단 시점 이용은 news_sentiment_analyst 쪽 경로).

▶ 운영: 24시간 상주(뉴스는 장외에도 흐른다). 소켓이 끊기면 백오프 재접속,
  Sink 는 유지(버퍼 보존). 적재는 기존 NewsSink 그대로 - source 만 ls_news.

사용
  python collectors/ls_news_collector.py            # 자체 점검 (접속 없음)
  python collectors/ls_news_collector.py --probe 60 # 60초 수신 실측 (적재 없음)
  python collectors/ls_news_collector.py --run      # 상주 적재 (컨테이너 entrypoint)
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from news_events import NewsRecord, clean_title  # noqa: E402
from source_registry import SourceRegistry, load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-ls-news-v1"
SOURCE_ID = "ls_news"
KST = timezone(timedelta(hours=9))
RECONNECT_BACKOFF = (2.0, 5.0, 15.0, 60.0)
HEARTBEAT_SECONDS = 300.0  # 뉴스는 한산할 수 있어 심박 간격을 시세보다 길게


def parse_codes(raw: str) -> tuple[str, ...]:
    """NWS code 필드: 6자리 단축코드 연결('000000145270' -> 145270).

    '000000' 패딩 블록은 종목 아님으로 버린다(실측). 중복 제거, 순서 유지.
    """
    raw = (raw or "").strip()
    out: list[str] = []
    for i in range(0, len(raw) - 5, 6):
        code = raw[i:i + 6]
        if code != "000000" and code not in out and code.isdigit():
            out.append(code)
    return tuple(out)


def parse_packet(body: dict, *, observed_at: datetime) -> NewsRecord | None:
    """NWS 패킷 -> NewsRecord. 필수 필드가 빠지면 None (조용히 만들어내지 않는다)."""
    realkey = str(body.get("realkey") or "").strip()
    title = clean_title(str(body.get("title") or ""))
    date, tm = str(body.get("date") or ""), str(body.get("time") or "")
    if not (realkey and title and len(date) == 8 and len(tm) == 6):
        return None
    published = datetime.strptime(date + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    return NewsRecord(
        external_id=f"lsnws:{realkey}",
        title=title,
        canonical_url=None,           # LS 뉴스는 URL 이 없다 - realkey 가 좌표다
        published_at=published,
        observed_at=observed_at,
        language="ko",
        provider="ls",
        symbols=parse_codes(str(body.get("code") or "")),
    )


async def run_service(stop: asyncio.Event) -> int:
    import websockets

    from ls_client import LsRestClient
    from ls_realtime_worker import build_subscribe_message, classify_message
    from news_pipeline import NewsSink, krx_symbol_resolver, seed_title_window_from_db
    from reference_repository import SupabaseReferenceRepository

    env = load_project_env()
    SourceRegistry(env=env).require(SOURCE_ID)
    mode = (env.get("LS_ENV") or "PAPER").upper()
    base = env["LS_WS_BASE_URL_PAPER"] if mode == "PAPER" else env["LS_WS_BASE_URL"]
    ws_url = base.rstrip("/") + "/websocket"

    ref = SupabaseReferenceRepository()
    _, _, id_by_source = ref.sync_data_sources()
    source_id = id_by_source.get(SOURCE_ID)
    if source_id is None:
        raise RuntimeError(f"data_sources 에 {SOURCE_ID} 가 없다")
    with ref._conn.cursor() as cur:
        cur.execute("""
            select s.symbol, i.instrument_id, i.display_name
            from reference.instruments i
            join reference.instrument_symbols s using (instrument_id)
            where i.market = 'KRX' and i.instrument_type = 'STOCK'
              and i.status = 'ACTIVE' and s.is_primary
        """)
        rows = cur.fetchall()
    inst_by_symbol = {sym: iid for sym, iid, _n in rows}
    names = {sym: n for sym, _iid, n in rows}
    resolver = krx_symbol_resolver(inst_by_symbol, dedicated_names=names,
                                   known_names=set(names.values()))
    sink = NewsSink(ref, source_id=source_id, link_resolver=resolver,
                    max_batch=10, max_delay_seconds=3.0,
                    title_dedup_window=5000)  # 같은 뉴스 재전송(새 realkey) 차단
    seeded = seed_title_window_from_db(sink, ref._conn, source_id)
    client = LsRestClient()
    print(f"{COLLECTOR_VERSION}: {mode} {ws_url} - 전 종목 뉴스 push, "
          f"연결 가능 심볼 {len(inst_by_symbol):,}, 제목 창 예열 {seeded:,}", flush=True)

    received = stored_mark = 0
    attempt = 0
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(ws_url, open_timeout=20,
                                              ping_interval=30) as ws:
                    await ws.send(build_subscribe_message("NWS", "NWS001", client.token()))
                    attempt = 0
                    last_beat = asyncio.get_running_loop().time()
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            sink.tick()
                            now = asyncio.get_running_loop().time()
                            if now - last_beat >= HEARTBEAT_SECONDS:
                                print(f"[{datetime.now(KST):%H:%M:%S}] 수신 {received:,} | "
                                      f"{sink.stats.summary()}", flush=True)
                                last_beat = now
                            continue
                        kind, _tr, payload = classify_message(json.loads(raw))
                        if kind == "ack":
                            code = str(payload.get("rsp_cd"))
                            print(f"  구독 ack {code} {payload.get('rsp_msg')}", flush=True)
                            if code != "00000":
                                raise RuntimeError(f"구독 거절 {code}")
                            continue
                        if kind != "data":
                            continue
                        rec = parse_packet(payload, observed_at=datetime.now(timezone.utc))
                        if rec is None:
                            continue
                        received += 1
                        sink.add(rec)
                        if received - stored_mark >= 20:
                            stored_mark = received
                            print(f"[{datetime.now(KST):%H:%M:%S}] 수신 {received:,} | "
                                  f"{sink.stats.summary()}", flush=True)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                backoff = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                print(f"⚠ 연결 오류: {type(e).__name__}: {str(e)[:80]} - "
                      f"{backoff:.0f}초 후 재접속({attempt})", flush=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
    finally:
        try:
            sink.close()
        except Exception as e:
            print(f"⚠ 종료 Flush 실패 - {sink.pending}건 유실: {e}", flush=True)
        ref.close()
    print(f"종료: 수신 {received:,} | {sink.stats.summary()}", flush=True)
    return 0


def main() -> int:
    stop = asyncio.Event()

    async def _run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop.set())
        return await run_service(stop)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 자체 점검 - 접속 없이
# ---------------------------------------------------------------------------

def _check_parse_codes():
    assert parse_codes("000000145270") == ("145270",)
    assert parse_codes("005930000660005930") == ("005930", "000660")
    assert parse_codes("") == ()
    assert parse_codes("00593") == ()          # 잘린 조각은 버린다
    assert parse_codes("ABCDEF005930") == ("005930",)
    print("  종목코드 파싱            OK")


def _check_parse_packet():
    ob = datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
    b = {"date": "20260731", "time": "155559", "id": "01",
         "realkey": "202607311555592195909342",
         "title": "케이탑리츠, <b>지분</b> 변동", "code": "000000145270"}
    r = parse_packet(b, observed_at=ob)
    assert r.external_id == "lsnws:202607311555592195909342"
    assert r.title == "케이탑리츠, 지분 변동", r.title      # 태그 제거
    assert r.published_at.hour == 15 and r.published_at.tzinfo is KST
    assert r.symbols == ("145270",) and r.provider == "ls"
    assert r.canonical_url is None
    # 필수 결측은 None - 지어내지 않는다
    for k in ("realkey", "title", "date", "time"):
        assert parse_packet({**b, k: ""}, observed_at=ob) is None, k
    print("  패킷 파싱                OK")


def _check_registry_gate():
    from source_registry import SourceUseNotAllowed, UseScope

    r = SourceRegistry(env={"LS_APP_KEY": "k", "LS_APP_SECRET_KEY": "s"})
    r.check_use(SOURCE_ID, UseScope.SNIPPET_STORE)
    for scope in (UseScope.FULLTEXT_STORE, UseScope.EMBEDDING):
        try:
            r.check_use(SOURCE_ID, scope)
            raise AssertionError(f"{scope} 가 허용됐다 - 약관 확인 전 본문 저장 금지")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 Gate            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        raise SystemExit(main())
    if "--probe" in sys.argv:
        # 수신만 확인(적재 없음)하고 싶을 때는 --run 을 짧게 돌리고 끊는 쪽이
        # 사실 더 정확하다 - probe 는 접속·ack·파싱 확인용 최소 경로만 남긴다
        print("probe 는 --run 을 잠깐 돌리는 것으로 대체한다 (Ctrl+C 로 중단)")
        raise SystemExit(0)

    print(f"{COLLECTOR_VERSION} 자체 점검 (접속 없음)")
    _check_parse_codes()
    _check_parse_packet()
    _check_registry_gate()
    print("LS 뉴스 3개 영역 통과. 상주는 --run")
