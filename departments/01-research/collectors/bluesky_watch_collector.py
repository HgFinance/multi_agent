#!/usr/bin/env python3
"""Bluesky 표적 계정 수집 - 미국 금융 기관 미디어·논객 피드 (무료·무인증).

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-01 "미국 주식·유명 인물 포스트 시도" + 실측:
      public.api.bsky.app 의 getProfile/getAuthorFeed 는 **무인증**으로 열려
      있다(검색 searchPosts 만 403). 볼륨 실측 - Bloomberg ~46/일,
      Reuters ~57/일, WSJ ~27/일, CNBC ~36/일 (기관 4곳 ~166건/일).
      한국 금융 담론은 파이어호스 실측상 사실상 0 이라(레지스트리 주석)
      이 수집기는 **미국·글로벌 신호 전용**이다.

▶ 설계
  - 표적 계정 목록은 config/bluesky_watchlist.txt (한 줄 한 핸들, # 주석).
    계정 추가·삭제는 파일 수정으로 - 코드 배포 없이 조정 가능하다.
  - getAuthorFeed(filter=posts_no_replies)를 계정별 폴링. AT URI 가 안정
    ID 라 external_id 로 그대로 쓴다(on conflict do nothing - 포스트는
    수정 불가·삭제만 가능한 모델이다).
  - **스니펫 정책**: 본문을 300자까지 title 로 저장한다(NAVER·BIGKinds 와
    같은 SNIPPET_STORE 등급 - Bluesky 는 공개 재배포 전제 프로토콜이지만
    보수적으로 시작). canonical_url 은 bsky.app 웹 좌표.
  - 3시각: published_at=record.createdAt, observed_at=수집 시각.
  - ⚠ 삭제 Compliance 백로그: 원 포스트가 삭제되면 우리 사본도 지워야
    한다(가이드 DoD 뉴스 항목의 요건) - v1 은 주기 재검증 미구현. 구현
    전까지 이 데이터는 내부 Evidence 전용이며 재배포하지 않는다.
  - repost 는 건너뛴다(reason 필드 존재) - 원 작성자 것이 아니고, 인용
    가치는 원 포스트 수집으로 충분하다.

사용
  python collectors/bluesky_watch_collector.py            # 자체 점검
  python collectors/bluesky_watch_collector.py --collect  # 1회 폴링·적재
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

COLLECTOR_VERSION = "research-bluesky-watch-v1"
SOURCE_ID = "bluesky"
APPVIEW = "https://public.api.bsky.app/xrpc"
UA = {"User-Agent": "hedgefund-research-collector/0.1 (contact: traderjaeil@gmail.com)"}
SNIPPET_CHARS = 300
FEED_LIMIT = 50
DOCUMENT_TYPE = "SOCIAL_POST"
DEFAULT_WATCHLIST = Path(__file__).resolve().parent.parent / "config" / "bluesky_watchlist.txt"


@dataclass(frozen=True)
class PostRecord:
    external_id: str          # AT URI (at://did/app.bsky.feed.post/rkey)
    title: str                # 본문 스니펫 (<= 300자)
    canonical_url: str
    published_at: datetime
    handle: str


def parse_watchlist(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        h = line.split("#", 1)[0].strip()
        if h and h not in out:
            out.append(h)
    return out


def post_url_from_uri(uri: str, handle: str) -> str:
    """at://did:plc:xxx/app.bsky.feed.post/rkey -> bsky.app 웹 좌표."""
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def parse_feed_item(item: dict, handle: str) -> PostRecord | None:
    """피드 항목 -> PostRecord. repost·결측은 None (지어내지 않는다)."""
    if item.get("reason") is not None:      # repost - 원 작성자 것이 아니다
        return None
    post = item.get("post") or {}
    uri = str(post.get("uri") or "")
    rec = post.get("record") or {}
    text = str(rec.get("text") or "").strip()
    created = str(rec.get("createdAt") or "")
    if not (uri.startswith("at://") and text and created):
        return None
    try:
        published = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if published.tzinfo is None:
        return None
    return PostRecord(
        external_id=uri,
        title=" ".join(text.split())[:SNIPPET_CHARS],
        canonical_url=post_url_from_uri(uri, handle),
        published_at=published,
        handle=handle,
    )


def fetch_author_feed(handle: str) -> list[dict]:
    url = (f"{APPVIEW}/app.bsky.feed.getAuthorFeed?actor={urllib.parse.quote(handle)}"
           f"&limit={FEED_LIMIT}&filter=posts_no_replies")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("feed", [])


def collect(watchlist_path: Path | None = None) -> int:
    import psycopg2
    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    # 라이선스 게이트 - 스니펫 저장 권한이 Registry 에 있어야 적재한다
    SourceRegistry(env=env).check_use(SOURCE_ID, UseScope.SNIPPET_STORE)

    path = watchlist_path or DEFAULT_WATCHLIST
    handles = parse_watchlist(path.read_text(encoding="utf-8"))
    if not handles:
        print(f"워치리스트가 비었다: {path}", flush=True)
        return 1

    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("select source_id from reference.data_sources where source_code=%s",
                        (SOURCE_ID,))
            row = cur.fetchone()
            if row is None:
                # Registry 사양은 있는데 DB 미동기화 - 한 번 동기화한다
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
                from reference_repository import SupabaseReferenceRepository

                repo = SupabaseReferenceRepository()
                repo.sync_data_sources()
                repo.close()
                cur.execute("select source_id from reference.data_sources where source_code=%s",
                            (SOURCE_ID,))
                row = cur.fetchone()
            src_id = row[0]

        observed = datetime.now(timezone.utc)
        total_new = total_seen = failed = 0
        for h in handles:
            try:
                items = fetch_author_feed(h)
            except Exception as e:  # noqa: BLE001 - intentional fallback boundary
                failed += 1
                print(f"  ⚠ {h}: {type(e).__name__} {str(e)[:60]}", flush=True)
                continue
            records = [r for r in (parse_feed_item(i, h) for i in items) if r]
            total_seen += len(records)
            with conn.cursor() as cur:
                for r in records:
                    cur.execute(
                        """
                        insert into research.documents
                          (source_id, external_id, document_type, canonical_url,
                           title, language, published_at, observed_at, status)
                        values (%s, %s, %s, %s, %s, 'en', %s, %s, 'ACTIVE')
                        on conflict (source_id, external_id) do nothing
                        """,
                        (src_id, r.external_id, DOCUMENT_TYPE, r.canonical_url,
                         r.title, r.published_at, observed))
                    total_new += cur.rowcount
            conn.commit()
        print(f"{COLLECTOR_VERSION}: 계정 {len(handles)}곳 / 수신 {total_seen} / "
              f"신규 {total_new} / 실패 {failed}", flush=True)
        return 0 if failed < len(handles) else 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_watchlist_parse():
    text = "bloomberg.com\n# 주석\nwsj.com  # 인라인\n\nbloomberg.com\n"
    assert parse_watchlist(text) == ["bloomberg.com", "wsj.com"]
    assert parse_watchlist("# 전부 주석\n") == []
    print("  워치리스트 파싱          OK")


def _check_feed_item_parse():
    item = {"post": {
        "uri": "at://did:plc:abc/app.bsky.feed.post/3xyz",
        "record": {"text": "  Fed holds rates\nsteady  ",
                   "createdAt": "2026-08-01T12:00:00.000Z"}}}
    r = parse_feed_item(item, "bloomberg.com")
    assert r.external_id == "at://did:plc:abc/app.bsky.feed.post/3xyz"
    assert r.title == "Fed holds rates steady"          # 공백 정규화
    assert r.canonical_url == "https://bsky.app/profile/bloomberg.com/post/3xyz"
    assert r.published_at.tzinfo is not None and r.published_at.hour == 12
    # repost·결측은 None
    assert parse_feed_item({**item, "reason": {"$type": "repost"}}, "h") is None
    assert parse_feed_item({"post": {"uri": "at://x", "record": {"text": "",
                            "createdAt": "2026-08-01T12:00:00Z"}}}, "h") is None
    assert parse_feed_item({"post": {"uri": "bad", "record": {"text": "t",
                            "createdAt": "2026-08-01T12:00:00Z"}}}, "h") is None
    # 스니펫 상한
    long_item = {"post": {"uri": "at://did:plc:a/app.bsky.feed.post/1",
                          "record": {"text": "x" * 500,
                                     "createdAt": "2026-08-01T00:00:00Z"}}}
    assert len(parse_feed_item(long_item, "h").title) == SNIPPET_CHARS
    print("  피드 항목 파싱·스니펫    OK")


def _check_license_gate():
    from source_registry import SourceRegistry, SourceUseNotAllowed, UseScope

    r = SourceRegistry(env={})
    r.check_use(SOURCE_ID, UseScope.SNIPPET_STORE)      # 등재 확인
    for scope in (UseScope.FULLTEXT_STORE, UseScope.EMBEDDING, UseScope.REDISTRIBUTE):
        try:
            r.check_use(SOURCE_ID, scope)
            raise AssertionError(f"{scope} 가 허용됐다 - 삭제 Compliance 전 금지")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 게이트          OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_watchlist_parse()
    _check_feed_item_parse()
    _check_license_gate()
    print("Bluesky 표적 수집 3개 영역 통과. 수집은 --collect")
