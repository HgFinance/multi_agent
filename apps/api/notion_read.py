#!/usr/bin/env python3
"""CEO 오피스 Notion 리포트 읽기. **Read-only이고, Notion 토큰은 서버에만 있다.**

소유: 영주 (CEO/Agent 인사팀)
근거: ai-office/CLAUDE.md "브라우저에서 비밀 credential, Broker API, Hermes
      내부 DB를 직접 호출하지 않는다"

▶ 왜 BFF를 거치나
  `NOTION_TOKEN`은 읽기 전용 토큰이 아니다. 같은 토큰으로 페이지를 만들고
  고치고 보관 처리할 수 있어서, 번들에 들어가는 순간 누구나 회사 Notion을
  쓸 수 있다. 그래서 토큰은 이 프로세스 환경변수에만 두고, 화면에는 정규화된
  리포트 목록과 마크다운 본문만 내려보낸다.

▶ 이 값이 무엇이고 무엇이 아닌가
  `CeoNotionProjection`(orchestration/adapters/ceo_notion_projection.py)이
  synthesis 완료마다 `NOTION_CEO_DB`에 남긴 **비구속 projection**이다. 원장도
  Risk 판정도 아니고, 여기 숫자로 NAV·PnL·한도를 확정하지 않는다. 그래서
  `authoritative: false`를 항상 싣는다(개발원칙 5).

▶ 여기서 하지 않는 것
  쓰지 않는다. GET/query 세 개만 부른다. 요약·해석하지 않는다.

▶ 아무 페이지나 열어 주지 않는다
  `page_id`는 브라우저가 보내는 값이다. 검사 없이 blocks를 넘기면 이 토큰이
  볼 수 있는 **워크스페이스 전체**를 이 API로 읽을 수 있다. 그래서 본문을
  주기 전에 페이지의 `parent.database_id`가 `NOTION_CEO_DB`인지 먼저 확인한다.

자체 점검:
    python apps/api/notion_read.py
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

try:
    from departments.notion_markdown import notion_blocks_to_markdown
except ImportError:  # pragma: no cover - direct ``python apps/api/notion_read.py``
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from departments.notion_markdown import notion_blocks_to_markdown

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

router = APIRouter(prefix="/ui/notion", tags=["notion"])


class NotionReportCard(BaseModel):
    schema_version: Literal["ui.notion-report-card.v1"] = "ui.notion-report-card.v1"
    page_id: str
    url: str
    title: str
    """`구분` select. 스키마에 없으면 `None`이고 화면은 배지를 안 그린다."""
    category: str | None = None
    """`상태` select."""
    state: str | None = None
    """`기준일` date. 목록 정렬 기준이자 화면의 "발행 시각"."""
    published_at: str | None = None


class NotionReportListResponse(BaseModel):
    schema_version: Literal["ui.notion-reports.v1"] = "ui.notion-reports.v1"
    source: Literal["notion"] = "notion"
    authoritative: bool = False
    database_id: str
    reports: list[NotionReportCard]


class NotionReportDetail(BaseModel):
    schema_version: Literal["ui.notion-report.v1"] = "ui.notion-report.v1"
    source: Literal["notion"] = "notion"
    authoritative: bool = False
    page_id: str
    url: str
    title: str
    category: str | None = None
    state: str | None = None
    published_at: str | None = None
    """Notion 블록을 되돌린 마크다운. 화면이 그대로 렌더링한다."""
    markdown: str
    """본문 블록이 100개를 넘어 잘렸는지. 잘렸으면 화면이 "Notion에서 열기"를 권한다."""
    truncated: bool = False


def credentials() -> tuple[str, str]:
    """(토큰, DB id). 하나라도 비면 503.

    설정이 없으면 빈 목록을 주지 않는다. 빈 목록은 "리포트가 없다"로 읽히는데
    실제로는 "못 읽었다"이고, 둘은 화면에서 구분돼야 한다.
    """

    token = os.getenv("NOTION_TOKEN", "").strip()
    database_id = os.getenv("NOTION_CEO_DB", "").strip()
    if not token or not database_id:
        raise HTTPException(
            503, "NOTION_TOKEN / NOTION_CEO_DB가 설정되지 않았습니다."
        )
    return token, database_id


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _explain(status: int) -> HTTPException:
    if status == 401:
        return HTTPException(502, "Notion 토큰이 거부됐습니다(401). 토큰을 재발급하세요.")
    if status == 404:
        return HTTPException(
            502,
            "Notion이 대상을 찾지 못했습니다(404). 통합이 이 데이터베이스에 "
            "연결돼 있는지 확인하세요(페이지 ⋯ → 연결 → 통합 추가).",
        )
    if status == 429:
        return HTTPException(503, "Notion rate limit(429). 잠시 후 다시 시도하세요.")
    return HTTPException(502, f"Notion 조회 실패(HTTP {status}).")


# discord_read와 같은 이유의 프로세스 메모리 TTL 캐시. 결과물 창고는 부서 카드를
# 열 때마다 뜨고 목록은 10초 주기로 갱신되는데, 그때마다 Notion을 때리면 429가
# 뜨고 429는 조회 전체를 잠근다. 워커를 늘리면 캐시도 워커 수만큼 생기므로,
# 그때는 Redis로 옮긴다.
_CACHE: dict[str, tuple[float, Any]] = {}
_TTL_SECONDS = max(5, int(os.getenv("NOTION_READ_CACHE_SECONDS", "30")))
# 본문은 열어 본 리포트마다 항목이 하나씩 생긴다. TTL만 두면 만료된 항목이
# 지워지지 않고 남아, 오래 뜬 BFF에서 열어 본 리포트 수만큼 계속 늘어난다.
_MAX_ENTRIES = 64


def _cached(key: str, load: Any) -> Any:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]

    # 실패한 조회는 아무것도 남기지 않는다 - 예외가 그대로 올라간다.
    value = load()

    if key not in _CACHE and len(_CACHE) >= _MAX_ENTRIES:
        for stale in [k for k, (at, _) in _CACHE.items() if now - at >= _TTL_SECONDS]:
            _CACHE.pop(stale, None)
        if len(_CACHE) >= _MAX_ENTRIES:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[key] = (now, value)
    return value


def _title_property(properties: Mapping[str, Any]) -> str:
    """제목 property의 값. 이름(`브리핑명`/`제목`)이 아니라 **type으로** 찾는다.

    CEO DB는 `브리핑명`, 옛 projection 스키마는 `제목`을 쓴다. 이름을 하드코딩하면
    스키마가 하나 바뀔 때 목록의 제목이 통째로 빈다.
    """

    for value in properties.values():
        if not isinstance(value, Mapping) or value.get("type") != "title":
            continue
        title = value.get("title")
        if not isinstance(title, Sequence) or isinstance(title, (str, bytes)):
            continue
        return "".join(
            str(item.get("plain_text") or "")
            for item in title
            if isinstance(item, Mapping)
        ).strip()
    return ""


def _select_property(properties: Mapping[str, Any], name: str) -> str | None:
    value = properties.get(name)
    if not isinstance(value, Mapping) or value.get("type") != "select":
        return None
    select = value.get("select")
    if not isinstance(select, Mapping):
        return None
    return str(select.get("name") or "") or None


def _date_property(properties: Mapping[str, Any], name: str) -> str | None:
    value = properties.get(name)
    if not isinstance(value, Mapping) or value.get("type") != "date":
        return None
    date = value.get("date")
    if not isinstance(date, Mapping):
        return None
    return str(date.get("start") or "") or None


def normalize_page(page: Mapping[str, Any]) -> NotionReportCard | None:
    """Notion page 객체 하나를 화면 계약으로 줄인다. id가 없으면 버린다."""

    page_id = str(page.get("id") or "").strip()
    if not page_id:
        return None
    properties = page.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    return NotionReportCard(
        page_id=page_id,
        url=str(page.get("url") or ""),
        title=_title_property(properties) or "제목 없는 리포트",
        category=_select_property(properties, "구분"),
        state=_select_property(properties, "상태"),
        published_at=(
            _date_property(properties, "기준일")
            or str(page.get("last_edited_time") or "")
            or None
        ),
    )


def query_reports(token: str, database_id: str, limit: int) -> list[dict[str, Any]]:
    """`기준일` 최신순 페이지 목록.

    정렬을 서버(Notion)에 맡긴다. 클라이언트에서 정렬하려면 DB 전체를 끌어와야
    하는데, 지금 이 DB는 이미 수백 건이고 계속 는다.
    """

    def load() -> list[dict[str, Any]]:
        response = httpx.post(
            f"{NOTION_API}/databases/{database_id}/query",
            headers=_headers(token),
            json={
                "sorts": [{"property": "기준일", "direction": "descending"}],
                "page_size": limit,
            },
            timeout=10.0,
        )
        if response.status_code == 400:
            # `기준일`이 없는 스키마(옛 projection DB)도 목록은 보여준다.
            response = httpx.post(
                f"{NOTION_API}/databases/{database_id}/query",
                headers=_headers(token),
                json={
                    "sorts": [{"timestamp": "created_time", "direction": "descending"}],
                    "page_size": limit,
                },
                timeout=10.0,
            )
        if response.status_code != 200:
            raise _explain(response.status_code)
        payload = response.json()
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise HTTPException(502, "Notion 응답에 results 배열이 없습니다.")
        return results

    return _cached(f"reports:{database_id}:{limit}", load)


def retrieve_page(token: str, page_id: str) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        response = httpx.get(
            f"{NOTION_API}/pages/{page_id}",
            headers=_headers(token),
            timeout=10.0,
        )
        if response.status_code != 200:
            raise _explain(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(502, "Notion page 응답이 객체가 아닙니다.")
        return payload

    return _cached(f"page:{page_id}", load)


def _child_blocks(token: str, block_id: str, page_size: int) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{NOTION_API}/blocks/{block_id}/children",
        headers=_headers(token),
        params={"page_size": page_size},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise _explain(response.status_code)
    payload = response.json()
    results = payload.get("results") if isinstance(payload, Mapping) else None
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


# 본문 한 번에 읽는 블록 수 상한. 리포트 한 장을 화면에 띄우려고 긴 페이지를
# 끝까지 페이지네이션하지 않는다 - 그건 Notion 왕복이 늘 뿐이고, 그렇게 긴
# 리포트는 어차피 Notion에서 보는 편이 낫다(잘리면 `truncated`로 알린다).
_MAX_BLOCKS = 100


def page_markdown(token: str, page_id: str) -> tuple[str, bool]:
    """페이지 본문을 마크다운으로. (마크다운, 잘렸는지)."""

    def load() -> tuple[str, bool]:
        blocks = _child_blocks(token, page_id, _MAX_BLOCKS)
        truncated = len(blocks) >= _MAX_BLOCKS
        # 표는 행이 자식 블록이라 한 번 더 읽어야 한다. 표가 없는 리포트는
        # 이 루프가 한 번도 안 돈다.
        for block in blocks:
            if block.get("type") == "table" and block.get("has_children"):
                block["children"] = _child_blocks(
                    token, str(block.get("id") or ""), _MAX_BLOCKS
                )
        return notion_blocks_to_markdown(blocks), truncated

    return _cached(f"markdown:{page_id}", load)


@router.get("/reports", response_model=NotionReportListResponse)
def read_reports(
    limit: int = Query(default=20, ge=1, le=100),
) -> NotionReportListResponse:
    """CEO 오피스 Notion DB에 실제로 발행된 리포트만 최신순으로 준다."""

    token, database_id = credentials()
    cards = [
        card
        for card in (normalize_page(page) for page in query_reports(token, database_id, limit))
        if card is not None
    ]
    return NotionReportListResponse(database_id=database_id, reports=cards)


@router.get("/reports/{page_id}", response_model=NotionReportDetail)
def read_report(page_id: str) -> NotionReportDetail:
    """리포트 한 장의 본문. `NOTION_CEO_DB` 소속이 아니면 열어 주지 않는다."""

    token, database_id = credentials()
    page = retrieve_page(token, page_id)

    parent = page.get("parent")
    parent = parent if isinstance(parent, Mapping) else {}
    parent_db = str(parent.get("database_id") or "").replace("-", "")
    if parent_db != database_id.replace("-", ""):
        raise HTTPException(404, "CEO 오피스 리포트 데이터베이스의 페이지가 아닙니다.")

    card = normalize_page(page)
    if card is None:
        raise HTTPException(502, "Notion page 응답에 id가 없습니다.")

    markdown, truncated = page_markdown(token, page_id)
    return NotionReportDetail(
        page_id=card.page_id,
        url=card.url,
        title=card.title,
        category=card.category,
        state=card.state,
        published_at=card.published_at,
        markdown=markdown,
        truncated=truncated,
    )


if __name__ == "__main__":
    # 네트워크 없이 도는 자체 점검. 정규화·권한 경계 규칙만 본다.
    from unittest.mock import patch

    sample_page = {
        "id": "2adc190a-c33d-4d63-9a90-f1ab86087f42",
        "url": "https://www.notion.so/CEO-Synthesis-t-cc8081c5",
        "parent": {"type": "database_id", "database_id": "db-1"},
        "properties": {
            "브리핑명": {
                "type": "title",
                "title": [{"plain_text": "CEO Synthesis · t_cc8081c5"}],
            },
            "구분": {"type": "select", "select": {"name": "저녁 브리핑"}},
            "상태": {"type": "select", "select": {"name": "보고 완료"}},
            "기준일": {"type": "date", "date": {"start": "2026-08-26T17:12:00+09:00"}},
            "완료": {"type": "number", "number": 3},
        },
    }

    card = normalize_page(sample_page)
    assert card is not None
    assert card.title == "CEO Synthesis · t_cc8081c5", card.title
    assert (card.category, card.state) == ("저녁 브리핑", "보고 완료")
    assert card.published_at == "2026-08-26T17:12:00+09:00"
    assert normalize_page({"properties": {}}) is None, "id 없는 page는 버린다"

    # 제목은 이름이 아니라 type으로 찾는다 - 옛 projection 스키마(`제목`)도 읽힌다.
    legacy = normalize_page(
        {
            "id": "p2",
            "properties": {"제목": {"type": "title", "title": [{"plain_text": "옛 스키마"}]}},
        }
    )
    assert legacy is not None and legacy.title == "옛 스키마", legacy
    assert legacy.category is None, "없는 select는 배지를 만들지 않는다"

    untitled = normalize_page({"id": "p3", "properties": {}})
    assert untitled is not None and untitled.title == "제목 없는 리포트"

    # 설정이 없으면 빈 목록이 아니라 503이다.
    with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_CEO_DB": ""}, clear=False):
        try:
            credentials()
        except HTTPException as exc:
            assert exc.status_code == 503, exc.status_code
        else:
            raise AssertionError("자격증명 없이 통과시켰다")

    # 남의 DB 페이지는 본문을 안 준다.
    env = {"NOTION_TOKEN": "tok", "NOTION_CEO_DB": "db-1"}
    other = dict(sample_page, parent={"type": "database_id", "database_id": "db-other"})
    with patch.dict(os.environ, env), patch(f"{__name__}.retrieve_page", lambda *_: other):
        try:
            read_report("2adc190ac33d4d639a90f1ab86087f42")
        except HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("다른 데이터베이스의 페이지를 열어 줬다")

    # 하이픈 유무는 같은 DB로 본다 - Notion이 두 표기를 섞어 준다.
    hyphenated = dict(
        sample_page, parent={"type": "database_id", "database_id": "db-1"}
    )
    with patch.dict(os.environ, env), patch(
        f"{__name__}.retrieve_page", lambda *_: hyphenated
    ), patch(f"{__name__}.page_markdown", lambda *_: ("# 본문", False)):
        detail = read_report("2adc190ac33d4d639a90f1ab86087f42")
        assert detail.markdown == "# 본문" and not detail.truncated
        assert detail.authoritative is False, "권위 있는 값이 아니다"

    # TTL 캐시는 히트하면 네트워크 로더를 안 부른다.
    _CACHE["k"] = (time.monotonic(), "cached")
    assert _cached("k", lambda: "fresh") == "cached", "TTL 안이면 로더를 안 탄다"

    # 실패한 조회는 캐시에 남기지 않는다 - 남기면 다음 요청이 같은 오류를 캐시로
    # 되돌려 준다.
    def boom() -> str:
        raise RuntimeError("upstream down")

    try:
        _cached("boom", boom)
    except RuntimeError:
        pass
    assert "boom" not in _CACHE, "실패를 캐시했다"

    # 열어 본 리포트 수만큼 무한히 늘지 않는다.
    _CACHE.clear()
    for index in range(_MAX_ENTRIES + 20):
        _cached(f"entry-{index}", lambda index=index: index)
    assert len(_CACHE) <= _MAX_ENTRIES, len(_CACHE)
    _CACHE.clear()

    print("notion_read 자체 점검 통과")
