#!/usr/bin/env python3
"""웹검색 - SEARCH_HIT 은 Fact 가 아니다. Validator 를 거쳐야 근거가 된다.

담당: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 12.2.1절(Web Search MCP 직원 배정)
        "RES-08 만 SearXNG/Playwright MCP 를 사용하며 나머지 분석가는
         WebSearchRequest 만 제출한다"
        `RQF-WEB-03` 완료 조건 "SEARCH_HIT 이 Validator 전 Fact 로 사용되지 않음"
      docs/03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md 606행
        "실시간 Web Search 는 Historical Replay 와 Backtest 에서 호출하지 않는다"
        "Tavily/SerpApi Quota 는 SearXNG 장애 또는 Material Case Coverage 보완에만"
      재일님 지시 2026-08-03 "웹검색도 구현해서 리서치 부서에서 어떻게 사용할
        것인지 도입방안 세워서 작업"

▶ 계약을 새로 만들지 않는다. 문서가 이미 정했다.
    검색 주체    RES-08 하나. 나머지는 WebSearchRequest 만 낸다
    승격 경로    SEARCH_HIT -> Citation/Time/Numeric Validator -> VERIFIED_EVIDENCE
    Replay       호출 금지. 지금의 웹은 그때의 지면이 아니다
    엔진         SearXNG 주력, Tavily 는 보조 Quota

▶ SearXNG 가 아직 없다 - 그래서 Tavily 로 시작하되 계약은 그대로다
  컨테이너를 올리는 것은 별도 결정이고, 그 전에 계약을 지키는 경로를 만들어 두면
  엔진 교체가 이 파일 하나로 끝난다. **엔진이 무엇이든 승격 규율은 같다.**

▶ SEARCH_HIT 이 왜 Fact 가 아닌가
  검색 결과에는 document_id 가 없다. 우리가 수집·검증한 문서가 아니라 **외부
  주장**이다. 그것을 근거 ID 로 쓰면 "어떤 소스에 기댄 판단이 틀렸나" 를 물을 때
  추적할 대상이 없다. 그래서:
    - SEARCH_HIT 은 url+content_hash 로 식별한다(document_id 를 지어내지 않는다)
    - 이미 DB 에 있는 문서와 겹치면 그쪽 document_id 를 쓴다(중복 승격 방지)
    - 겹치지 않으면 **Evidence 후보**일 뿐이고, fact 주장의 근거로 쓸 수 없다

▶ 하지 않는 것
  ToS 위반 스크레이핑, 로그인 필요 페이지, 유료 담장 우회. 본문 열람은
  agents/article_reader.py 가 robots.txt 를 지키며 하고 **저장하지 않는다**.

실행: python evidence/web_search.py     # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

WEB_SEARCH_VERSION = "research-web-search-v1"

# 검색은 RES-08 만 한다. 다른 페르소나가 부르면 예외다(가이드 12.2.1).
SEARCH_PERSONA = "rag-librarian-evidence-curator"

TAVILY_URL = "https://api.tavily.com/search"

# ── 검색 백엔드 ──────────────────────────────────────────────────────────────
# ▶ 직무기술서(AGENT_EMPLOYEE_PROFILES.md 500-509행)가 RES-08 에게 정한 것은
#   **SearXNG 기반 Web Search MCP** 다. Tavily 직접 호출은 그 자리를 임시로
#   메운 것이었다. SearXNG 는 우리가 띄우는 메타검색이라 (a) 외부 API 키가
#   필요 없고 (b) 질의가 제3자 로그에 남지 않으며 (c) 엔진 구성이 우리 통제다.
#
#   **공개 인스턴스를 쓰지 않는다.** 남의 인스턴스에 자동 질의를 넣는 것은
#   그쪽 ToS 위반이고, 우리가 스크레이퍼를 금지한 이유와 같은 문제다.
#   SEARXNG_URL 은 우리가 운영하는 주소여야 한다.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")
SEARXNG_TIMEOUT = 20


def active_backend() -> str:
    """어느 백엔드가 실제로 쓰이는가. **선언이 아니라 실재를 돌려준다.**"""
    if SEARXNG_URL:
        return "searxng"
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    return "none"
# 한 번에 가져올 상한. 늘리면 비용이 늘고 Validator 부담도 는다.
MAX_RESULTS = 8


class WebSearchError(RuntimeError):
    """검색이 실패했다. **결과 0 과 실패를 구분한다.**"""


class NotAuthorizedToSearch(WebSearchError):
    """이 페르소나는 직접 검색할 수 없다 - WebSearchRequest 를 내야 한다."""


class ReplayForbidden(WebSearchError):
    """과거 재현에서 실시간 웹을 부를 수 없다(PIT)."""


@dataclass(frozen=True)
class SearchHit:
    """검색 결과 하나. **아직 근거가 아니다.**

    status 가 SEARCH_HIT 인 동안은 fact 주장의 근거로 쓸 수 없다.
    Validator 를 통과해야 VERIFIED_EVIDENCE 가 된다.
    """

    url: str
    title: str
    snippet: str
    engine: str
    retrieved_at: datetime
    published_at: datetime | None = None
    score: float | None = None
    status: str = "SEARCH_HIT"          # SEARCH_HIT | VERIFIED_EVIDENCE | REJECTED
    reject_reason: str = ""
    # DB 에 이미 있는 문서와 겹치면 그 id. 없으면 None - **지어내지 않는다**
    document_id: str | None = None

    def content_hash(self) -> str:
        """url+제목으로 만든 안정 식별자. document_id 를 대신하지 않는다."""
        return "sha256:" + hashlib.sha256(
            f"{self.url}|{self.title}".encode()).hexdigest()[:32]

    def evidence_ref(self) -> str:
        """인용 가능한 참조. **승격 전에는 web: 접두사로 구분된다.**"""
        if self.document_id:
            return self.document_id
        return f"web:{self.content_hash()}"


@dataclass
class SearchRequest:
    """분석가가 내는 검색 요청. 직접 검색하지 않고 이것만 낸다(가이드 12.2.1)."""

    requester: str                     # 요청한 페르소나
    question: str
    symbol: str = ""
    reason: str = ""                   # 왜 내부 근거로 부족한가
    max_results: int = MAX_RESULTS
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("빈 질문으로 검색을 요청할 수 없다")
        if not self.reason.strip():
            # 이유 없는 검색은 예산만 태운다. 내부 RAG 로 안 되는 이유를 적게 한다.
            raise ValueError(
                f"{self.requester}: 검색 사유가 없다 - 내부 근거로 왜 부족한지 "
                f"적어야 한다(가이드 12.2.1 'Analyst Unanswered Question')")


def search(req: SearchRequest, *, persona: str = SEARCH_PERSONA,
           run_mode: str = "LIVE", api_key: str | None = None,
           post=None) -> list[SearchHit]:
    """웹검색 1회. **RES-08 만 부를 수 있고 Replay 에서는 금지다.**"""
    if persona != SEARCH_PERSONA:
        raise NotAuthorizedToSearch(
            f"{persona} 는 직접 검색할 수 없다 - WebSearchRequest 를 내면 "
            f"{SEARCH_PERSONA} 가 수행한다(가이드 12.2.1)")
    if str(run_mode).upper() == "REPLAY":
        raise ReplayForbidden(
            "과거 재현에서 실시간 웹검색을 부를 수 없다 - 지금의 웹은 그때의 "
            "지면이 아니다(RESEARCH_DATA_SOURCES 606행)")

    now = datetime.now(timezone.utc)
    n = min(req.max_results, MAX_RESULTS)

    # 직무기술서가 정한 SearXNG 를 먼저 쓴다. 없으면 Tavily 로 떨어지되
    # **어느 엔진이 냈는지 hit 에 남긴다** - 근거의 출처가 섞이면 안 된다.
    if SEARXNG_URL:
        raw = _call(lambda: (post or _get_json)(
            f"{SEARXNG_URL}/search?q={urllib.parse.quote(req.question)}"
            f"&format=json&safesearch=1"))
        return _hits_from_searxng(raw, n, now)

    key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise WebSearchError(
            "검색 백엔드가 없다 - SEARXNG_URL 또는 TAVILY_API_KEY 를 설정한다")
    raw = _call(lambda: (post or _post)(TAVILY_URL, {
        "api_key": key, "query": req.question,
        "max_results": n, "search_depth": "basic"}))
    return _hits_from_tavily(raw, n, now)


def _call(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        raise WebSearchError(f"검색 호출 실패: {type(e).__name__}: {e}") from e


def _hits_from_tavily(raw, n: int, now) -> list[SearchHit]:
    hits = []
    for r in ((raw or {}).get("results") or [])[:n]:
        if not r.get("url"):
            continue
        hits.append(SearchHit(
            url=str(r["url"]),
            title=str(r.get("title") or "")[:200],
            # 본문 전문을 담지 않는다(라이선스) - 스니펫까지다
            snippet=str(r.get("content") or "")[:500],
            engine="tavily",
            retrieved_at=now,
            score=float(r["score"]) if r.get("score") is not None else None,
        ))
    return hits


def _hits_from_searxng(raw, n: int, now) -> list[SearchHit]:
    """SearXNG JSON -> SearchHit.

    SearXNG 는 score 대신 순위를 준다. **순위를 점수로 위장하지 않는다** -
    0.9 같은 값을 만들어 넣으면 Tavily 점수와 비교 가능한 것처럼 읽힌다.
    """
    hits = []
    for r in ((raw or {}).get("results") or [])[:n]:
        if not r.get("url"):
            continue
        hits.append(SearchHit(
            url=str(r["url"]),
            title=str(r.get("title") or "")[:200],
            snippet=str(r.get("content") or "")[:500],
            # 어느 엔진이 냈는지 남긴다. searxng 는 메타검색이라 그 안에서
            # 어떤 엔진이 물어왔는지도 함께 적는다.
            engine="searxng:" + str(r.get("engine") or "?")[:24],
            retrieved_at=now,
            score=None,
        ))
    return hits


def _get_json(url: str, timeout: int = SEARXNG_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Validator - SEARCH_HIT 을 VERIFIED_EVIDENCE 로 올릴지 판정 (RQF-WEB-03)
# ---------------------------------------------------------------------------

# 신뢰할 수 있는 1차 출처. 여기 없다고 거짓은 아니지만 **fact 근거로는 못 쓴다**.
PRIMARY_DOMAINS = (
    "dart.fss.or.kr", "krx.co.kr", "bok.or.kr", "kostat.go.kr", "fss.or.kr",
    "motie.go.kr", "moef.go.kr", "index.go.kr", "ecos.bok.or.kr",
)


def validate_hits(hits: list[SearchHit], *, as_of: datetime,
                  known_urls: dict[str, str] | None = None) -> list[SearchHit]:
    """SEARCH_HIT -> VERIFIED_EVIDENCE 또는 REJECTED.

    ▶ 통과 조건 (전부 만족해야 한다)
      1. as_of 이후 게시가 아니다 - 미래 문서를 근거로 쓸 수 없다
      2. 1차 출처이거나, **이미 우리가 수집한 문서와 같은 URL** 이다
         (후자면 그 document_id 를 물려받아 추적이 이어진다)
      3. 제목·스니펫이 비어 있지 않다 - 내용 없는 링크는 근거가 아니다

    **통과 못 한 것을 버리지 않는다.** REJECTED 로 사유와 함께 남긴다 -
    무엇을 왜 안 썼는지 보여야 사람이 판단할 수 있다.
    """
    known = known_urls or {}
    out: list[SearchHit] = []
    for h in hits:
        doc_id = known.get(h.url)
        reason = ""
        if h.published_at and h.published_at > as_of:
            reason = f"as_of({as_of:%Y-%m-%d}) 이후 게시 - 그때 알 수 없었다"
        elif not (h.title.strip() and h.snippet.strip()):
            reason = "제목 또는 내용이 비었다 - 근거로 쓸 수 없다"
        elif not doc_id and not any(d in h.url for d in PRIMARY_DOMAINS):
            reason = ("1차 출처가 아니고 우리 수집본과도 겹치지 않는다 - "
                      "Evidence 후보로만 남긴다")
        if reason:
            out.append(SearchHit(**{**h.__dict__, "status": "REJECTED",
                                    "reject_reason": reason}))
        else:
            out.append(SearchHit(**{**h.__dict__, "status": "VERIFIED_EVIDENCE",
                                    "document_id": doc_id}))
    return out


def summarize(hits: list[SearchHit]) -> dict:
    """검색 결과 요약. **몇 건이 승격됐고 몇 건이 왜 탈락했는지** 드러낸다."""
    verified = [h for h in hits if h.status == "VERIFIED_EVIDENCE"]
    rejected = [h for h in hits if h.status == "REJECTED"]
    return {
        "hits": len(hits),
        "verified": len(verified),
        "rejected": len(rejected),
        "verified_refs": [h.evidence_ref() for h in verified],
        # 왜 떨어졌는지를 세어 보여준다 - 0건이면 왜 0건인지 알아야 한다
        "reject_reasons": sorted({h.reject_reason for h in rejected}),
        "engines": sorted({h.engine for h in hits}),
    }


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _hit(url, title="제목", snippet="내용", published=None) -> SearchHit:
    return SearchHit(url=url, title=title, snippet=snippet, engine="tavily",
                     retrieved_at=_NOW, published_at=published)


def _check_only_res08_can_search():
    """검색 주체는 RES-08 하나다 (가이드 12.2.1)."""
    req = SearchRequest(requester="fundamental-analyst", question="q",
                        reason="내부 공시에 투자 규모가 없다")
    for persona in ("fundamental-analyst", "news-sentiment-analyst",
                    "research-supervisor"):
        try:
            search(req, persona=persona, api_key="x", post=lambda *a: {})
            raise AssertionError(f"{persona} 가 직접 검색했다")
        except NotAuthorizedToSearch as e:
            assert "WebSearchRequest" in str(e)
    print("  RES-08 전담 강제         OK")


def _check_replay_forbidden():
    req = SearchRequest(requester="x", question="q", reason="r")
    try:
        search(req, run_mode="REPLAY", api_key="x", post=lambda *a: {})
        raise AssertionError("Replay 에서 검색이 통과했다")
    except ReplayForbidden as e:
        assert "그때의 지면이 아니다" in str(e)
    print("  Replay 금지              OK")


def _check_request_requires_reason():
    """사유 없는 검색은 예산만 태운다."""
    for bad in ({"question": "", "reason": "r"}, {"question": "q", "reason": " "}):
        try:
            SearchRequest(requester="x", **bad)
            raise AssertionError(f"불량 요청이 통과했다: {bad}")
        except ValueError:
            pass
    ok = SearchRequest(requester="fundamental-analyst", question="42조 투자 확정?",
                       reason="공시가 '미확정' 이라 규모를 확인할 수 없다")
    assert ok.max_results == MAX_RESULTS
    print("  요청에 사유 강제         OK")


def _check_hit_is_not_fact_until_validated():
    """**이 모듈의 핵심** - SEARCH_HIT 은 근거가 아니다."""
    h = _hit("https://news.example.com/a")
    assert h.status == "SEARCH_HIT"
    # 승격 전 참조는 web: 접두사로 구분된다 - document_id 를 지어내지 않는다
    assert h.evidence_ref().startswith("web:sha256:"), h.evidence_ref()
    assert h.document_id is None

    v = validate_hits([h], as_of=_NOW)
    assert v[0].status == "REJECTED", v[0].status
    assert "1차 출처가 아니고" in v[0].reject_reason
    print("  SEARCH_HIT != Fact       OK")


def _check_primary_source_and_known_doc():
    # 1차 출처는 승격된다
    dart = _hit("https://dart.fss.or.kr/x/1")
    assert validate_hits([dart], as_of=_NOW)[0].status == "VERIFIED_EVIDENCE"
    # 우리가 이미 수집한 URL 이면 그 document_id 를 물려받는다(추적 유지)
    known = _hit("https://news.example.com/b")
    v = validate_hits([known], as_of=_NOW,
                      known_urls={"https://news.example.com/b": "doc-42"})
    assert v[0].status == "VERIFIED_EVIDENCE" and v[0].document_id == "doc-42"
    assert v[0].evidence_ref() == "doc-42", "수집본 id 를 안 물려받았다"
    print("  1차 출처·수집본 승계     OK")


def _check_future_and_empty_rejected():
    future = _hit("https://dart.fss.or.kr/x/2",
                  published=datetime(2026, 12, 1, tzinfo=timezone.utc))
    r = validate_hits([future], as_of=_NOW)[0]
    assert r.status == "REJECTED" and "이후 게시" in r.reject_reason
    empty = _hit("https://dart.fss.or.kr/x/3", snippet="  ")
    assert validate_hits([empty], as_of=_NOW)[0].status == "REJECTED"
    print("  미래·빈 내용 거부        OK")


def _check_summary_shows_why_zero():
    """0건일 때 **왜 0건인지** 보여야 한다."""
    hits = validate_hits([_hit("https://news.example.com/c")], as_of=_NOW)
    s = summarize(hits)
    assert s == {"hits": 1, "verified": 0, "rejected": 1, "verified_refs": [],
                 "reject_reasons": [("1차 출처가 아니고 우리 수집본과도 겹치지 "
                                    "않는다 - Evidence 후보로만 남긴다")],
                 "engines": ["tavily"]}, s
    print("  0건의 사유 표시          OK")


def _check_snippet_not_full_body():
    """본문 전문을 담지 않는다(라이선스)."""
    long_body = "가" * 5000
    hits = search(SearchRequest(requester="x", question="q", reason="r"),
                  api_key="k",
                  post=lambda *a: {"results": [
                      {"url": "https://a.com", "title": "t", "content": long_body}]})
    assert len(hits) == 1 and len(hits[0].snippet) <= 500, len(hits[0].snippet)
    print("  스니펫 상한(본문 금지)   OK")


def _check_searxng_backend():
    """SearXNG 가 실제로 쓰이고, **엔진 출처가 남는가.**

    직무기술서가 정한 백엔드는 SearXNG 다. 붙였는데 hit 에 엔진이 안 남으면
    Tavily 결과와 섞여 어느 엔진이 낸 근거인지 못 가린다.
    """
    global SEARXNG_URL
    orig = SEARXNG_URL
    try:
        globals()["SEARXNG_URL"] = "http://127.0.0.1:8888"
        assert active_backend() == "searxng", active_backend()
        fake = {"results": [
            {"url": "https://a.com/1", "title": "t1", "content": "c1",
             "engine": "google"},
            {"url": "", "title": "버림", "content": "x"},
            {"url": "https://a.com/2", "title": "t2", "content": "c2",
             "engine": "bing"}]}
        req = SearchRequest(question="q", reason="내부 근거 없음",
                            requester=SEARCH_PERSONA)
        hits = search(req, post=lambda url: fake)
        assert len(hits) == 2, hits              # url 없는 행은 버린다
        assert hits[0].engine == "searxng:google", hits[0]
        assert hits[1].engine == "searxng:bing", hits[1]
        # ▶ 순위를 점수로 위장하지 않는다 - Tavily 점수와 비교 가능한 것처럼
        #   읽히면 안 된다
        assert all(h.score is None for h in hits), hits
        # 스니펫 상한은 백엔드가 바뀌어도 지켜진다(라이선스)
        big = {"results": [{"url": "https://a.com/3", "title": "t",
                            "content": "가" * 5000, "engine": "e"}]}
        assert len(search(req, post=lambda url: big)[0].snippet) <= 500
    finally:
        globals()["SEARXNG_URL"] = orig


def _check_backend_absent_is_explicit():
    """백엔드가 없으면 **조용히 0건이 아니라 사유가 있는 실패**여야 한다."""
    global SEARXNG_URL
    orig, key = SEARXNG_URL, os.environ.pop("TAVILY_API_KEY", None)
    try:
        globals()["SEARXNG_URL"] = ""
        assert active_backend() == "none"
        req = SearchRequest(question="q", reason="r", requester=SEARCH_PERSONA)
        try:
            search(req, api_key="")
        except WebSearchError as e:
            assert "SEARXNG_URL" in str(e), str(e)
        else:
            raise AssertionError("백엔드 없이 검색이 성공했다")
    finally:
        globals()["SEARXNG_URL"] = orig
        if key is not None:
            os.environ["TAVILY_API_KEY"] = key


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{WEB_SEARCH_VERSION} 자체 점검 (네트워크 없음)")
    _check_only_res08_can_search()
    _check_replay_forbidden()
    _check_request_requires_reason()
    _check_hit_is_not_fact_until_validated()
    _check_primary_source_and_known_doc()
    _check_future_and_empty_rejected()
    _check_summary_shows_why_zero()
    _check_snippet_not_full_body()
    _check_searxng_backend();          print("  SearXNG 백엔드·출처      OK")
    _check_backend_absent_is_explicit();print("  백엔드 부재 = 명시 실패  OK")
    print("웹검색 10개 영역 통과.")
