#!/usr/bin/env python3
"""판단 시점 기사 열람 도구 - 본문은 읽고 버린다, 저장하지 않는다.

담당: 재일 (리서치/퀀트)
근거: 재일님 승인 2026-07-31 - 가이드 3.3 "Agent 직접 크롤링 금지"의 **판단 시점
      열람 예외**. 사람 애널리스트가 기사를 클릭해 읽고 메모만 남기는 것의
      자동화이며, 저작권법 35조의2(일시적 복제)가 허용하는 브라우징 범위에 머문다.

▶ 이 모듈이 스스로 강제하는 안전장치 (전부 코드다 - 약속이 아니라)
  1. **저장하지 않는다.** 반환값은 메모리 문자열뿐이고, 이 모듈은 DB·파일에
     쓰는 코드가 없다(자체 점검이 import 를 검사한다). 호출부(에이전트)는
     판단·요약(파생 저작물)만 저장한다 - news_sentiment_analyst 의 evidence
     구조에 본문 필드 자체가 없다.
  2. **robots.txt 를 지킨다.** 불허면 안 읽고 SKIPPED_ROBOTS 로 센다.
  3. **언론사(도메인)별 최소 간격**(기본 20초)과 **실행당 열람 상한**(기본 5건).
     대량 수집이 되는 순간 브라우징이 아니라 크롤링이다.
  4. **선별된 기사만** - 호출부가 DEDICATED(0.9) 고신뢰 기사로 제한한다.
  5. 실패는 조용히 지나가지 않고 사유별로 센다(ReadStats).

⚠ 이 도구는 수집기가 아니다. collectors/ 가 아니라 agents/ 에 있는 이유다.
   Runtime 상주·스케줄 실행에 넣지 않는다 - 에이전트 판단 1회의 재료일 뿐이다.
"""
from __future__ import annotations

import html.parser
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

READER_VERSION = "research-article-reader-v1"

# 우리가 누군지 밝힌다 - 숨는 봇은 차단만 부른다
USER_AGENT = "HedgefundResearchReader/0.1 (analysis-at-decision-time; no-archiving)"

PER_DOMAIN_INTERVAL_SECONDS = 20.0
MAX_READS_PER_RUN = 5
MAX_BODY_CHARS = 4000          # LLM 판단 재료로 충분한 만큼만 - 전문 재현이 목적이 아니다
FETCH_TIMEOUT_SECONDS = 10.0


@dataclass
class ReadStats:
    """무엇을 읽고 무엇을 왜 못 읽었는지. 축소를 숨기지 않는다."""

    fetched: int = 0
    skipped_robots: int = 0
    skipped_budget: int = 0
    failed: int = 0
    fail_reasons: dict = field(default_factory=dict)

    def summary(self) -> str:
        s = f"열람 {self.fetched} / robots 거부 {self.skipped_robots} / 한도 초과 {self.skipped_budget} / 실패 {self.failed}"
        if self.fail_reasons:
            top = sorted(self.fail_reasons.items(), key=lambda kv: -kv[1])[:3]
            s += " (" + ", ".join(f"{k}:{v}" for k, v in top) + ")"
        return s


class _TextExtractor(html.parser.HTMLParser):
    """<p>·<article> 중심의 단순 본문 추출. 언론사마다 구조가 달라 완벽할 수 없고,
    완벽할 필요도 없다 - LLM 판단 재료로 충분한 평문이면 된다."""

    _SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "button"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_p = False
        self.paragraphs: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "p" and self._skip_depth == 0:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "p" and self._in_p:
            text = "".join(self._buf).strip()
            if len(text) >= 20:  # 캡션·바이라인 등 짧은 조각은 버린다
                self.paragraphs.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p and self._skip_depth == 0:
            self._buf.append(data)


def extract_text(html_doc: str, *, max_chars: int = MAX_BODY_CHARS) -> str:
    p = _TextExtractor()
    try:
        p.feed(html_doc)
    except Exception:
        pass  # 깨진 HTML 은 그때까지 모은 문단으로 간다
    return "\n".join(p.paragraphs)[:max_chars]


class ArticleReader:
    """판단 1회 수명의 열람기. 수명이 끝나면 아무것도 남지 않는다."""

    def __init__(
        self,
        *,
        max_reads: int = MAX_READS_PER_RUN,
        per_domain_interval: float = PER_DOMAIN_INTERVAL_SECONDS,
        fetch=None,          # 테스트 주입: (url) -> html str
        robots_ok=None,      # 테스트 주입: (url) -> bool
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._max_reads = max_reads
        self._interval = per_domain_interval
        self._fetch = fetch or self._http_fetch
        self._robots_ok = robots_ok or self._robots_allowed
        self._clock = clock
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.stats = ReadStats()

    def read(self, url: str) -> str | None:
        """기사 하나를 읽어 평문을 돌려준다. 못 읽으면 None - 지어내지 않는다."""
        if self.stats.fetched >= self._max_reads:
            self.stats.skipped_budget += 1
            return None
        if not url or not url.startswith(("http://", "https://")):
            self.stats.failed += 1
            self.stats.fail_reasons["BAD_URL"] = self.stats.fail_reasons.get("BAD_URL", 0) + 1
            return None
        if not self._robots_ok(url):
            self.stats.skipped_robots += 1
            return None

        domain = urllib.parse.urlparse(url).netloc
        last = self._last_hit.get(domain)
        if last is not None:
            wait = self._interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[domain] = self._clock()

        try:
            doc = self._fetch(url)
        except Exception as e:
            self.stats.failed += 1
            key = type(e).__name__
            self.stats.fail_reasons[key] = self.stats.fail_reasons.get(key, 0) + 1
            return None

        text = extract_text(doc)
        if len(text) < 100:  # 본문 추출 실패로 본다 - 짧은 조각으로 판단시키지 않는다
            self.stats.failed += 1
            self.stats.fail_reasons["NO_BODY"] = self.stats.fail_reasons.get("NO_BODY", 0) + 1
            return None
        self.stats.fetched += 1
        return text

    # -- 실제 IO ------------------------------------------------------------

    def _http_fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw = resp.read(1_500_000)  # 1.5MB 상한 - 기사 페이지면 충분하다
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    def _robots_allowed(self, url: str) -> bool:
        parts = urllib.parse.urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots_cache.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                rp.read()
            except Exception:
                # robots 를 읽을 수 없으면 **읽지 않는 쪽**을 택한다 (fail-closed)
                rp.disallow_all = True
            self._robots_cache[base] = rp
        return rp.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없이
# ---------------------------------------------------------------------------

_HTML = """<html><head><style>p{color:red}</style></head><body>
<nav><p>메뉴 항목들이 여기 잔뜩 있다고 하자 스무 자는 넘긴다</p></nav>
<article>
<p>삼성전자가 2분기 영업이익 10조원을 기록했다고 31일 공시했다. 반도체 부문 회복이 실적을 이끌었다.</p>
<p>회사 측은 하반기 수요 개선을 전망했다. 시장 컨센서스를 상회하는 결과이며 메모리 가격 반등이 이어질 것으로 내다봤다.</p>
<p>짧은조각</p>
</article>
<script>var x = "본문 아님";</script>
</body></html>"""


def _check_extract():
    text = extract_text(_HTML)
    assert "영업이익 10조원" in text and "하반기 수요 개선" in text
    assert "메뉴 항목" not in text, "nav 가 본문으로 샜다"
    assert "본문 아님" not in text, "script 가 본문으로 샜다"
    assert "짧은조각" not in text, "짧은 조각 필터가 죽었다"
    assert len(extract_text(_HTML, max_chars=50)) == 50
    print("  본문 추출                OK")


def _check_no_persistence():
    """이 모듈은 저장 경로 자체가 없어야 한다 - 열람이지 수집이 아니다."""
    import ast
    from pathlib import Path

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"psycopg2", "reference_repository", "market_repository", "sqlite3"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) \
                else [node.module or ""]
            for n in names:
                assert not any(n.startswith(b) for b in banned), \
                    f"열람 모듈에 저장 경로가 들어왔다: {n}"
    needle = "o" + "pen("  # 이 검사 코드 자신이 걸리지 않게 조립한다
    hits = src.replace("url" + needle, "").replace("read_text", "").count(needle)
    assert hits <= 1, "파일 쓰기 의심 - 열람 모듈은 파일을 만들지 않는다"
    print("  비저장 강제              OK")


def _check_guardrails():
    t = {"now": 0.0}
    slept: list[float] = []
    pages = {"https://a.com/1": _HTML, "https://a.com/2": _HTML, "https://b.com/1": _HTML}

    r = ArticleReader(
        max_reads=2, per_domain_interval=20.0,
        fetch=lambda u: pages[u],
        robots_ok=lambda u: "b.com" not in u,   # b.com 은 robots 불허라고 치자
        clock=lambda: t["now"], sleep=lambda s: slept.append(s) or t.__setitem__("now", t["now"] + s),
    )
    assert r.read("https://b.com/1") is None and r.stats.skipped_robots == 1
    assert r.read("https://a.com/1") is not None
    t["now"] += 5.0
    assert r.read("https://a.com/2") is not None
    assert slept and abs(slept[0] - 15.0) < 1e-6, f"도메인 간격 대기가 없다: {slept}"
    # 실행당 상한 - 2건 읽었으면 끝이다
    assert r.read("https://a.com/1") is None and r.stats.skipped_budget == 1
    assert r.stats.fetched == 2
    print("  안전장치(robots/간격/상한) OK")


def _check_failures_visible():
    def boom(_u):
        raise urllib.error.URLError("down")

    r = ArticleReader(fetch=boom, robots_ok=lambda u: True,
                      clock=lambda: 0.0, sleep=lambda s: None)
    assert r.read("https://a.com/x") is None
    assert r.read("not-a-url") is None
    r2 = ArticleReader(fetch=lambda u: "<p>too short</p>", robots_ok=lambda u: True,
                       clock=lambda: 0.0, sleep=lambda s: None)
    assert r2.read("https://a.com/y") is None
    assert r2.stats.fail_reasons.get("NO_BODY") == 1
    assert "실패 2" in r.stats.summary()
    print("  실패 가시화              OK")


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{READER_VERSION} 자체 점검 (네트워크 없음)")
    _check_extract()
    _check_no_persistence()
    _check_guardrails()
    _check_failures_visible()
    print("열람 도구 4개 영역 통과. 이 모듈은 수집기가 아니다 - 에이전트 판단 재료 전용")
