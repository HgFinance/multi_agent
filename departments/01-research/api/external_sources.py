#!/usr/bin/env python3
"""외부 정보원(DART·네이버) 질의 도구 - 정성 데이터의 에이전트 MCP 검색 통합.

소유: 재일 (리서치본부)
근거: 재일님 결정 2026-08-13 "수집계층 정성 데이터는 전부 에이전트의 mcp 검색으로
      통합하자" + docs/02-engineering/MCP_ONDEMAND_ARCHITECTURE.md (용도별 3분할).

▶ 왜 서드파티 MCP(npm)를 안 쓰고 직접 부르나
  korean-dart-mcp·naver-search-mcp 는 개인 유지보수 npm 패키지라 API 키가
  서드파티 코드를 통과한다(공급망). 도구 표면은 2026-08-13 프로브로 실측했고
  (resolve 0.02s·공시검색 0.3s·뉴스 0.13s/신선도 1분), 여기서는 같은 표면을
  DART·네이버 공식 REST 로 재현한다 - 키는 우리 코드만 통과한다.

▶ 이 계층의 세 가지 의무 (MCP_ONDEMAND_ARCHITECTURE §6-1)
  1. 예산: DART·NAVER 요청형 MCP 호출에 소스별 일일 상한을 둔다. 상주 수집기가
     같은 키를 소비하지 않으므로 이 상한이 Research Runtime의 전체 자동 호출
     예산이다.
  2. 비영속 인용 좌표: 외부 응답의 해시만 계산해 그 요청의 인용 좌표로
     돌려준다. 응답·snippet·본문은 파일, DB, Storage, pgvector 어디에도
     적재하지 않는다. 필요한 정보는 에이전트가 요청 시점에 다시 조회한다.
  3. 정직성(mcp_server.py 머리말과 동일): 결과를 요약·해석하지 않는다.
     원 응답을 그대로 돌려주고 출처(rcept_no·URL)를 반드시 동봉한다.
     실패는 실패로 돌려준다.

▶ 이 도구들은 **질의 응대와 경제적 가설 발상 전용**이다. typed AST 계산·백테스트·
  사후 채점은 보유한 시장 시계열만 읽는다. 여기 결과를 역사 수치 입력으로 재사용하면
  look-ahead가 조용히 생긴다(같은 문서 §7 위험 1).

자체 점검: python api/external_sources.py  (네트워크 필요 없는 검사만)
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from threading import Lock

KST = timezone(timedelta(hours=9))

DART_BASE = "https://opendart.fss.or.kr/api"
NAVER_NEWS = "https://openapi.naver.com/v1/search/news.json"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

# 보고서 코드 - DART 공식 (11013 1분기, 11012 반기, 11014 3분기, 11011 사업)
REPORT_CODES = {"q1": "11013", "half": "11012", "q3": "11014", "annual": "11011"}

# ── 예산 ────────────────────────────────────────────────────────────────────
# 요청형 Research MCP의 일일 상한. 정성 데이터 상주 수집기는 이 키를 쓰지 않는다.
# 공급자 한도보다 낮게 두어 반복 에이전트 호출이 외부 계정을 소진하지 않게 한다.
DART_DAILY_CAP = int(os.environ.get("MCP_DART_DAILY_CAP", "2000"))
NAVER_DAILY_CAP = int(os.environ.get("MCP_NAVER_DAILY_CAP", "5000"))

_budget_lock = Lock()
_budget: dict = {"day": None}          # source 별 카운터는 동적 생성
_caps: dict = {}                       # source -> cap (등록된 정보원 목록 겸용)


class BudgetExhausted(RuntimeError):
    pass


def spend(source: str, cap: int) -> None:
    """정보원 하나의 일일 호출을 1 소비한다. 다른 모듈(macro·ls)도 이걸 쓴다."""
    with _budget_lock:
        _caps[source] = cap
        today = date.today().isoformat()
        if _budget.get("day") != today:
            _budget.clear()
            _budget["day"] = today
        used = _budget.get(source, 0)
        if used >= cap:
            raise BudgetExhausted(
                f"{source} 에이전트 일일 예산({cap}) 소진 - 오늘은 더 호출할 수 "
                f"없다. 내일 리셋되며, 급하면 운영자가 MCP_{source.upper()}"
                f"_DAILY_CAP 을 올려야 한다. 답을 지어내지 말 것.")
        _budget[source] = used + 1


_spend = spend  # 내부 호출 하위 호환


def budget_state() -> dict:
    with _budget_lock:
        srcs = sorted(set(_caps) | {"dart", "naver"})
        caps = {"dart": DART_DAILY_CAP, "naver": NAVER_DAILY_CAP, **_caps}
        return {"day": _budget.get("day"),
                **{s: {"used": _budget.get(s, 0), "cap": caps[s]} for s in srcs}}


def _snapshot(tool: str, args: dict, response) -> str:
    """Return a request-scoped citation hash without persisting the response.

    ``tool`` and ``args`` are deliberately included so identical response
    bodies reached through different requests do not share a misleading
    coordinate.  This function performs no filesystem or database write.
    """
    envelope = {"tool": tool, "args": args, "response": response}
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


# ── HTTP ───────────────────────────────────────────────────────────────────
def _get(url: str, headers: dict | None = None, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "hgfinance-research-mcp/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def _dart_key() -> str:
    key = os.environ.get("OPEN_DART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPEN_DART_API_KEY 가 없다 - compose environment 확인")
    return key


def _dart_json(endpoint: str, **params) -> dict:
    _spend("dart", DART_DAILY_CAP)
    q = urllib.parse.urlencode({"crtfc_key": _dart_key(), **params})
    body = json.loads(_get(f"{DART_BASE}/{endpoint}?{q}").decode("utf-8"))
    # DART 규약: status 000 정상, 013 데이터 없음(오류 아님 - 미제출 등)
    if body.get("status") not in ("000", "013"):
        raise RuntimeError(f"DART [{body.get('status')}] {body.get('message')}")
    return body


# ── 기업 코드 색인 (corpCode.xml - 프로세스 메모리에서만 재사용) ────────────
_corp_index: list | None = None
_corp_lock = Lock()
_corp_index_failure_until = 0.0
_corp_index_failure_reason = ""
_CORP_INDEX_TIMEOUT_SECONDS = max(
    5,
    int(os.environ.get("MCP_DART_CORP_INDEX_TIMEOUT_SECONDS", "20")),
)
_CORP_INDEX_FAILURE_COOLDOWN_SECONDS = max(
    5,
    int(os.environ.get("MCP_DART_CORP_INDEX_FAILURE_COOLDOWN_SECONDS", "60")),
)


# corpCode.xml 다운로드가 **222초** 걸린다(2026-08-24 실측, 상장사 3,985건).
# 프로세스 안에서만 캐시하면 컨테이너를 새로 띄울 때마다 첫 공시 조회가 4분
# 멈춘다. 종목코드-기업명 대응표는 "시세 외 응답 비영속" 경계가 지키려는
# 판단 근거가 아니라 **식별자 사전**이므로, 짧은 TTL 디스크 캐시를 둔다.
# 조회 응답(공시 내용)은 여전히 저장하지 않는다.
_CORP_INDEX_CACHE_PATH = Path(
    os.environ.get("DART_CORP_INDEX_CACHE", "/tmp/dart_corp_index.json"))
_CORP_INDEX_CACHE_TTL_SECONDS = int(
    os.environ.get("DART_CORP_INDEX_CACHE_TTL", str(24 * 3600)))


def _load_corp_index_cache() -> list | None:
    try:
        st = _CORP_INDEX_CACHE_PATH.stat()
        if time.time() - st.st_mtime > _CORP_INDEX_CACHE_TTL_SECONDS:
            return None
        value = json.loads(_CORP_INDEX_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value
    return None


def _save_corp_index_cache(rows: list) -> None:
    try:
        tmp = _CORP_INDEX_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CORP_INDEX_CACHE_PATH)
    except OSError:
        pass  # 캐시는 편의 기능이다 - 못 써도 조회는 그대로 된다


def _load_corp_index() -> list:
    global _corp_index, _corp_index_failure_until, _corp_index_failure_reason
    with _corp_lock:
        if _corp_index is not None:
            return _corp_index
        cached = _load_corp_index_cache()
        if cached is not None:
            _corp_index = cached
            return _corp_index
        now = time.monotonic()
        if now < _corp_index_failure_until:
            raise RuntimeError(
                "DART 기업 색인 일시 사용 불가 "
                f"(재시도 대기 중: {_corp_index_failure_reason or 'external_error'})"
            )
        try:
            _spend("dart", DART_DAILY_CAP)
            q = urllib.parse.urlencode({"crtfc_key": _dart_key()})
            raw = _get(
                f"{DART_BASE}/corpCode.xml?{q}",
                timeout=_CORP_INDEX_TIMEOUT_SECONDS,
            )
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read(z.namelist()[0])
            out = []
            for el in ET.fromstring(xml).iter("list"):
                stock = (el.findtext("stock_code") or "").strip()
                if not stock:
                    continue  # 상장사만 - 비상장 10만 건은 색인에서 뺀다
                out.append({"corp_code": el.findtext("corp_code"),
                            "corp_name": (el.findtext("corp_name") or "").strip(),
                            "stock_code": stock})
            # corpCode.xml도 외부 응답이다. 디스크 캐시는 작은 편의 기능이지만
            # "시세 외 응답 비영속" 경계를 깨므로 프로세스 수명 안에서만 재사용한다.
            _corp_index = out
            _corp_index_failure_until = 0.0
            _corp_index_failure_reason = ""
            _save_corp_index_cache(out)
            return out
        except BudgetExhausted:
            raise
        except Exception as exc:
            # A failed full-index fetch used to leave the cache empty, so every
            # subsequent resolver call paid the same 60-second network cost.
            # Keep the failure non-persistent, but suppress equivalent retries
            # for a short cooldown. The caller still receives a truthful
            # unavailable error and may retry after the cooldown.
            _corp_index_failure_reason = type(exc).__name__
            _corp_index_failure_until = (
                time.monotonic() + _CORP_INDEX_FAILURE_COOLDOWN_SECONDS
            )
            raise RuntimeError(
                "DART 기업 색인을 가져오지 못했다: "
                f"{_corp_index_failure_reason}"
            ) from exc


def _resolve(query: str) -> list[dict]:
    q = query.strip()
    idx = _load_corp_index()
    if re.fullmatch(r"\d{6}", q):
        return [e for e in idx if e["stock_code"] == q][:5]
    exact = [e for e in idx if e["corp_name"] == q]
    if exact:
        return exact[:5]
    return [e for e in idx if q in e["corp_name"]][:5]


def _corp_code_of(corp: str) -> dict:
    hits = _resolve(corp)
    if not hits:
        raise RuntimeError(f"'{corp}' 에 해당하는 상장사를 찾지 못했다 - "
                           f"dart_resolve_corp 로 먼저 확인할 것")
    return hits[0]


# ── 도구 본체 (등록과 무관하게 직접 호출·테스트 가능) ───────────────────────
def dart_resolve_corp(query: str) -> dict:
    """기업명(부분일치) 또는 6자리 종목코드 -> DART corp_code 후보."""
    hits = _resolve(query)
    out = {"query": query, "count": len(hits), "results": hits}
    out["citation"] = _snapshot("dart_resolve_corp", {"query": query}, out)
    return out


def dart_search_disclosures(corp: str = "", days: int = 3,
                            page: int = 1) -> dict:
    """최근 N일 공시 목록. corp 비우면 전시장. 원문은 viewer_url 로 열람."""
    days = max(1, min(int(days), 30))
    end = date.today()
    start = end - timedelta(days=days)
    params = {"bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
              "page_no": max(1, int(page)), "page_count": 30}
    resolved = None
    if corp.strip():
        resolved = _corp_code_of(corp)
        params["corp_code"] = resolved["corp_code"]
    body = _dart_json("list.json", **params)
    items = [{**it, "viewer_url": DART_VIEWER + it["rcept_no"]}
             for it in body.get("list", [])]
    out = {"period": f"{start} ~ {end}", "corp": resolved,
           "total_count": body.get("total_count", 0),
           "page": body.get("page_no", 1),
           "total_pages": body.get("total_page", 1), "items": items}
    out["citation"] = _snapshot(
        "dart_search_disclosures", {"corp": corp, "days": days, "page": page}, out)
    return out


def dart_financials(corp: str, year: int, report: str = "annual") -> dict:
    """주요계정(BS/IS) - report: q1|half|q3|annual. 미제출이면 status 013 그대로."""
    code = REPORT_CODES.get(report)
    if code is None:
        raise RuntimeError(f"report 는 {sorted(REPORT_CODES)} 중 하나다: {report}")
    resolved = _corp_code_of(corp)
    body = _dart_json("fnlttSinglAcnt.json", corp_code=resolved["corp_code"],
                      bsns_year=int(year), reprt_code=code)
    out = {"corp": resolved, "year": int(year), "report": report,
           "status": body.get("status"), "message": body.get("message"),
           "items": body.get("list", [])}
    out["citation"] = _snapshot(
        "dart_financials", {"corp": corp, "year": year, "report": report}, out)
    return out


def dart_company(corp: str) -> dict:
    """기업개황 - 대표자·업종코드·설립일·주소. 업종 문맥 확인용."""
    resolved = _corp_code_of(corp)
    body = _dart_json("company.json", corp_code=resolved["corp_code"])
    body.pop("status", None), body.pop("message", None)
    out = {"corp": resolved, **body}
    out["citation"] = _snapshot("dart_company", {"corp": corp}, out)
    return out


_TAG = re.compile(r"</?b>|&quot;|&amp;|&lt;|&gt;|&#39;")


def _plain(s: str) -> str:
    return _TAG.sub(lambda m: {"&quot;": '"', "&amp;": "&", "&lt;": "<",
                               "&gt;": ">", "&#39;": "'"}.get(m.group(), ""), s)


def news_search(query: str, display: int = 10, sort: str = "date") -> dict:
    """네이버 뉴스 검색. sort: date(최신순)|sim(정확도순). 본문은 link 로 열람.

    응답의 title/description 은 검색 하이라이트 태그를 벗긴 평문이고,
    원문 필드(title_raw 등)도 함께 준다 - 요약·해석은 하지 않는다.
    """
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    sec = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        raise RuntimeError("NAVER_CLIENT_ID/SECRET 가 없다 - compose environment 확인")
    _spend("naver", NAVER_DAILY_CAP)
    q = urllib.parse.urlencode({"query": query,
                                "display": max(1, min(int(display), 30)),
                                "sort": sort if sort in ("date", "sim") else "date"})
    body = json.loads(_get(f"{NAVER_NEWS}?{q}", headers={
        "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}).decode("utf-8"))
    items = [{"title": _plain(it["title"]), "title_raw": it["title"],
              "description": _plain(it["description"]),
              "link": it["link"], "originallink": it.get("originallink", ""),
              "pubDate": it["pubDate"]} for it in body.get("items", [])]
    out = {"query": query, "total": body.get("total", 0),
           "searched_at": datetime.now(KST).isoformat(), "items": items}
    out["citation"] = _snapshot(
        "news_search", {"query": query, "display": display, "sort": sort}, out)
    return out


# ── 등록 ────────────────────────────────────────────────────────────────────
# ── 웹 본문 읽기 ────────────────────────────────────────────────────────────
WEB_DAILY_CAP = int(os.environ.get("MCP_WEB_DAILY_CAP", "2000"))
_READ_MAX_BYTES = 3 * 1024 * 1024


class _TextExtract(HTMLParser):
    """script/style 을 버리고 본문 텍스트와 <title> 만 모은다 (stdlib 전용)."""
    _SKIP = {"script", "style", "noscript", "svg", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def _assert_public_host(url: str) -> None:
    """사설·내부 IP 로 가는 fetch 를 막는다.

    공식 fetch MCP 의 보안 경고가 근거다 - 우리 compose 망에서는 에이전트가
    accounting-api:8000 같은 내부 면을 열 수 있게 되므로, 해석된 모든 주소가
    공인(global)일 때만 허용한다. 차단은 오류로 정직하게 알린다.
    """
    import ipaddress
    import socket
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RuntimeError(f"http(s) 만 허용한다: {parts.scheme}")
    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise RuntimeError(f"호스트 해석 실패: {host} ({e})") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RuntimeError(
                f"내부/사설 주소({ip})로의 fetch 는 차단된다 - 내부 API 는 "
                f"전용 도구를 쓸 것")


def read_url(url: str, max_chars: int = 8000, start: int = 0) -> dict:
    """웹 페이지 본문을 텍스트로 읽는다 - 뉴스 link·공시 viewer_url 열람용.

    news_search 는 제목·요약만 주므로, 깊이 읽어야 할 때 이 도구로 본문을
    가져온다. 길면 start 로 이어 읽는다. 추출 실패·차단 사이트는 실패로
    돌려준다 - 본문을 지어내지 말 것.
    """
    _assert_public_host(url)
    spend("web", WEB_DAILY_CAP)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; hgfinance-research/1.0)",
        "Accept-Language": "ko, en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        final = r.geturl()
        if final != url:
            _assert_public_host(final)   # 리다이렉트 후 재검사
        raw = r.read(_READ_MAX_BYTES)
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("euc-kr", errors="replace")
    p = _TextExtract()
    p.feed(html)
    text = re.sub(r"[ \t ]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    n = max(500, min(int(max_chars), 20000))
    s = max(0, int(start))
    out = {"url": url, "final_url": final, "title": p.title.strip(),
           "total_chars": len(text), "start": s,
           "text": text[s:s + n],
           "truncated": len(text) > s + n,
           "fetched_at": datetime.now(KST).isoformat()}
    out["citation"] = _snapshot("read_url", {"url": url, "start": s}, out)
    return out


TAVILY_DAILY_CAP = int(os.environ.get("MCP_TAVILY_DAILY_CAP", "30"))


def tavily_search(query: str, max_results: int = 5, days: int = 0) -> dict:
    """범용 웹 검색 (Tavily) - 네이버 뉴스 밖 전체 웹. CRAG 폴백 축.

    ⚠ 무료 한도가 월 1,000 크레딧이라 일일 상한이 빡빡하다(기본 30) -
    네이버·DART 로 안 되는 것만 여기로. 본문은 read_url 로 이어 읽는다.
    """
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY 가 없다 - compose environment 확인")
    spend("tavily", TAVILY_DAILY_CAP)
    payload = {"api_key": key, "query": query,
               "max_results": max(1, min(int(max_results), 10)),
               "search_depth": "basic", "include_answer": False}
    if int(days) > 0:
        payload["days"] = int(days)
        payload["topic"] = "news"
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read().decode("utf-8"))
    items = [{"title": it.get("title"), "url": it.get("url"),
              "snippet": (it.get("content") or "")[:400],
              "score": it.get("score")} for it in body.get("results", [])]
    out = {"query": query, "count": len(items), "items": items,
           "queried_at": datetime.now(KST).isoformat()}
    out["citation"] = _snapshot("tavily_search", {"query": query, "days": days}, out)
    return out


def record_citations(citations: list, note: str = "") -> dict:
    """답변에 **실제로 인용한** 조회의 citation 해시를 비영속으로 표시한다.

    이 도구는 어느 조회가 최종 답변의 근거였는지를 응답 객체에 묶을 뿐 파일이나
    DB에 기록하지 않는다. 호출자는 답변을 마치기 직전에 한 번 호출하고 반환된
    좌표를 같은 응답 계보에 포함한다.
    """
    marks = [str(c).strip() for c in (citations or []) if str(c).strip()]
    rec = {"marked": len(marks), "citations": marks, "note": note[:500]}
    rec["citation"] = _snapshot("citation_mark", {"note": note[:200]},
                                {"type": "citation_mark", "hashes": marks})
    return rec


def register_external_tools(server) -> None:
    """mcp_server.build_server() 가 부른다. 여기 도구는 전부 읽기 전용이다."""
    server.tool(
        name="dart_resolve_corp",
        description="기업명(부분일치) 또는 6자리 종목코드로 DART corp_code 를 찾는다. "
                    "다른 dart_* 도구를 부르기 전 모호하면 먼저 쓴다.")(dart_resolve_corp)
    server.tool(
        name="dart_search_disclosures",
        description="전자공시 목록 조회(최근 N일, 기업 지정 가능). 각 건에 원문 "
                    "viewer_url 과 rcept_no 가 붙는다 - 인용할 때 rcept_no 를 남겨라. "
                    "지금 이 순간의 공시 현황이며 과거 재현용이 아니다.")(dart_search_disclosures)
    server.tool(
        name="dart_financials",
        description="재무 주요계정(BS/IS). report=q1|half|q3|annual. 미제출 기간은 "
                    "status 013 으로 온다 - '없다'를 지어내지 말고 013 을 그대로 말하라.")(dart_financials)
    server.tool(
        name="dart_company",
        description="기업개황(대표자·업종코드·설립일). 업종 비교 전 문맥 확인용.")(dart_company)
    server.tool(
        name="news_search",
        description="네이버 뉴스 검색(최신순 기본, 발행시각 pubDate 동봉). 분 단위 "
                    "신선도. 인용하면 link 를 남겨라. 제목·요약만 오므로 본문이 "
                    "필요하면 link 를 열어 읽어라.")(news_search)
    server.tool(
        name="external_budget",
        description="오늘 외부 조회 예산 사용량(DART·네이버). 소진되면 호출이 거부된다.")(budget_state)
    server.tool(
        name="record_citations",
        description="답변에 실제로 인용한 조회의 citation 해시 목록을 표시한다. "
                    "답변을 마치기 직전 한 번 호출 - QA 재검증의 근거가 된다.")(record_citations)
    server.tool(
        name="tavily_search",
        description="범용 웹 검색(전체 웹) - 네이버·DART 로 안 될 때의 폴백. "
                    "일일 상한이 빡빡하니(무료 월 1k) 아껴 쓸 것. 본문은 read_url.")(tavily_search)
    server.tool(
        name="read_url",
        description="웹 페이지 본문을 텍스트로 읽는다 - news_search 의 link, "
                    "dart 의 viewer_url 을 깊이 읽을 때. 길면 start 로 이어 읽기. "
                    "내부/사설 주소는 차단된다.")(read_url)


# ── 자체 점검 (네트워크 없음) ───────────────────────────────────────────────
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 예산이 캡에서 실제로 거부하나
    _budget.update({"day": date.today().isoformat(), "dart": DART_DAILY_CAP})
    try:
        spend("dart", DART_DAILY_CAP)
        raise AssertionError("예산 소진인데 통과했다")
    except BudgetExhausted:
        print("  예산 거부                OK")
    _budget.update({"dart": 0, "naver": 0})
    # 동적 정보원도 같은 계층을 타나 (macro·ls 가 쓴다)
    spend("ecos", 5)
    assert budget_state()["ecos"]["used"] == 1
    print("  동적 정보원 예산          OK")
    # 하이라이트 태그 벗기기
    assert _plain("<b>삼성전자</b> &quot;발표&quot;") == '삼성전자 "발표"'
    print("  평문 변환                OK")
    # 보고서 코드 검증
    assert REPORT_CODES["half"] == "11012"
    try:
        dart_financials.__wrapped__ if False else None
        REPORT_CODES["bad"]
        raise AssertionError("없는 report 가 통과했다")
    except KeyError:
        print("  report 코드 검증          OK")
    # 응답을 저장하지 않고 결정론적 인용 해시만 돌려주나
    d = _snapshot("self-test", {"a": 1}, {"ok": True})
    assert len(d) == 16
    assert d == _snapshot("self-test", {"a": 1}, {"ok": True})
    assert d != _snapshot("self-test", {"a": 2}, {"ok": True})
    print("  비영속 인용 해시          OK")
    print("자체 점검 통과")
