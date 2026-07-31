#!/usr/bin/env python3
"""news-sentiment-analyst - 리서치본부 첫 LangGraph형 직원 구현.

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 news-sentiment-analyst 페르소나
      CLAUDE.md 개발 원칙 2(LLM 출력은 Pydantic 검증)·개발 원칙(LLM 은 관련성
      판단·서술만, 규칙 판정은 결정론적 Python - skills/agentic-rag 와 같은 원칙)
      마스터플랜 5.5 (직원은 사건별 동적 실행 Node)

흐름 (fetch -> judge -> verify -> aggregate, agentic-rag 와 같은 단계 분리):
  1. fetch      research-api 에서 Evidence 를 읽는다. **DB 직접 접근 금지** -
                에이전트는 조회면만 안다. as_of 를 그대로 전달하므로 백테스트
                재현에서도 같은 코드가 돈다.
  2. judge      LLM(Claude)이 기사별 sentiment(-1/0/+1)·salience 를 판정한다.
                출력은 Pydantic Schema 로 검증하고 실패 시 1회 재시도.
  3. verify     결정론적 검증 - 인용된 document_id 가 입력 집합에 없으면 그
                판정을 **버린다**(환각 인용). 버린 것은 개수로 드러낸다.
  4. aggregate  결정론적 집계 - score = Σ(sentiment × salience × weight) / Σ(...)
                LLM 은 집계에 관여하지 않는다. 증거가 없으면 NO_EVIDENCE,
                판정이 전부 무효면 INCONCLUSIVE 다 - 점수를 지어내지 않는다.

이 출력은 **Agent Decision 이 아니다.** Research Packet 의 입력 재료이며, 매매
신호·주문과 동일시하지 않는다(CLAUDE.md: Agent Decision != Signal != Order).

실행:
  python agents/news_sentiment_analyst.py            # 자체 점검 (LLM·API 없음)
  python agents/news_sentiment_analyst.py --run 005930 [--hours 24] [--as-of ISO]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from source_registry import load_project_env  # noqa: E402

AGENT_VERSION = "research-news-sentiment-analyst-v1"
KST = timezone(timedelta(hours=9))

RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")
# 리서치본부 배정 키는 ANTHROPIC 이다 (CLAUDE.md env 규약 - 아무 키나 쓰지 않는다)
MODEL = os.environ.get("NEWS_SENTIMENT_MODEL", "claude-sonnet-5")
MAX_ARTICLES = 40  # 한 판정에 넣는 기사 상한 - 초과분은 가중치 상위만


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙 2: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class ArticleJudgement(BaseModel):
    document_id: str = Field(description="입력에 있던 document_id 그대로")
    sentiment: int = Field(ge=-1, le=1, description="-1 악재 / 0 중립 / +1 호재")
    salience: float = Field(ge=0.0, le=1.0, description="이 종목에 얼마나 중대한가")
    reason: str = Field(max_length=200)


class JudgementBatch(BaseModel):
    judgements: list[ArticleJudgement]


@dataclass(frozen=True)
class SentimentReport:
    """최종 산출물. verdict 가 SCORED 일 때만 score 가 의미를 갖는다."""

    symbol: str
    as_of: datetime
    verdict: str                  # SCORED | NO_EVIDENCE | INCONCLUSIVE
    score: Optional[float]        # -1.0 ~ +1.0 (가중 평균)
    articles_used: int
    articles_dropped: int         # 환각 인용 등으로 버린 판정 수
    evidence: tuple[dict, ...]    # 인용 가능한 근거 (document_id 포함)
    detail: str = ""

    def summary(self) -> str:
        s = f"{self.symbol} @ {self.as_of.astimezone(KST):%m-%d %H:%M} -> {self.verdict}"
        if self.verdict == "SCORED":
            s += f" score={self.score:+.3f} (기사 {self.articles_used}건"
            if self.articles_dropped:
                s += f", 무효 {self.articles_dropped}"
            s += ")"
        return s


# ---------------------------------------------------------------------------
# 1. fetch - research-api 만 안다
# ---------------------------------------------------------------------------

def fetch_evidence(symbol: str, *, hours: float, as_of: Optional[datetime],
                   api_base: str = RESEARCH_API) -> list[dict]:
    url = f"{api_base}/evidence/news?symbol={symbol}&hours={hours}&limit=200"
    if as_of is not None:
        url += f"&as_of={urllib.parse.quote(as_of.isoformat())}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        rows = json.loads(resp.read())
    # 가중치 상위만 판정에 넣는다 - 오래된 기사 수십 건이 토큰만 태우는 것을 막는다
    rows.sort(key=lambda r: r.get("weight", 0.0), reverse=True)
    return rows[:MAX_ARTICLES]


# ---------------------------------------------------------------------------
# 2. judge - LLM 은 여기서만 쓴다
# ---------------------------------------------------------------------------

_SYSTEM = """You are the News Sentiment Analyst of a research department.
Judge each Korean news headline's sentiment FOR THE GIVEN STOCK only.
Rules:
- sentiment: -1 (negative for the stock), 0 (neutral/unrelated), +1 (positive).
- salience: 0.0~1.0, how material the news is to this stock's fundamentals/price.
  Sector roundups or mere mentions get low salience (<=0.3).
- Use ONLY the given headlines. Do not invent facts. Keep reason short, Korean.
- Return judgements for every article, same document_id as given.
Respond with JSON only: {"judgements":[{"document_id":...,"sentiment":...,"salience":...,"reason":...}]}"""


def judge_articles(symbol: str, articles: list[dict], *, llm=None) -> JudgementBatch:
    """기사 묶음을 LLM 으로 판정한다. Schema 불합격이면 한 번 고쳐 부르고, 또
    실패하면 예외다 - 판정을 지어내지 않는다(호출부가 INCONCLUSIVE 처리)."""
    payload = [
        {"document_id": a["document_id"], "title": a["title"],
         "relation": a.get("relation_type"), "published_kst":
             a["published_at"][:16],
         # 판단 시점 열람으로 붙은 본문(메모리뿐). 프롬프트 재료로만 쓰고 버린다
         **({"body": a["body"][:1500]} if a.get("body") else {})}
        for a in articles
    ]
    prompt = (f"Stock: {symbol}\nArticles ({len(payload)}):\n"
              + json.dumps(payload, ensure_ascii=False, indent=1))

    call = llm or _anthropic_call
    last_err = None
    for attempt in range(2):
        text = call(_SYSTEM, prompt if attempt == 0 else
                    prompt + f"\n\nYour previous output failed validation: {last_err}. "
                             f"Return ONLY valid JSON for the schema.")
        try:
            start = text.find("{")
            end = text.rfind("}")
            return JudgementBatch.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"LLM 판정이 Schema 를 두 번 어겼다: {last_err}")


def _anthropic_call(system: str, user: str) -> str:
    import anthropic

    key = load_project_env().get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 가 없다 (리서치본부 배정 키)")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=MODEL, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


# ---------------------------------------------------------------------------
# 3+4. verify + aggregate - 결정론적. LLM 이 손대지 못한다
# ---------------------------------------------------------------------------

def verify_and_aggregate(
    symbol: str, as_of: datetime, articles: list[dict], batch: JudgementBatch
) -> SentimentReport:
    by_id = {a["document_id"]: a for a in articles}
    valid: list[tuple[ArticleJudgement, dict]] = []
    dropped = 0
    seen: set[str] = set()
    for j in batch.judgements:
        art = by_id.get(j.document_id)
        if art is None or j.document_id in seen:
            dropped += 1  # 환각 인용 또는 중복 - 조용히 세지 않고 드러낸다
            continue
        seen.add(j.document_id)
        valid.append((j, art))

    if not articles:
        return SentimentReport(symbol, as_of, "NO_EVIDENCE", None, 0, dropped, (),
                               "창 안에 기사가 없다")
    if not valid:
        return SentimentReport(symbol, as_of, "INCONCLUSIVE", None, 0, dropped, (),
                               "유효한 판정이 없다 - 점수를 지어내지 않는다")

    num = den = 0.0
    evidence = []
    for j, art in valid:
        w = float(art.get("weight", 0.0)) * j.salience
        num += j.sentiment * w
        den += w
        evidence.append({
            "document_id": j.document_id, "title": art["title"],
            "sentiment": j.sentiment, "salience": j.salience,
            "weight": art.get("weight"), "reason": j.reason,
            "published_at": art["published_at"], "observed_at": art["observed_at"],
        })
    if den == 0.0:
        return SentimentReport(symbol, as_of, "INCONCLUSIVE", None, len(valid), dropped,
                               tuple(evidence), "가중치 합이 0 - 전부 무의미 판정")
    return SentimentReport(
        symbol, as_of, "SCORED", round(num / den, 4), len(valid), dropped,
        tuple(evidence),
    )


def attach_bodies(articles: list[dict], *, reader=None,
                  min_confidence: float = 0.9) -> tuple[list[dict], str]:
    """전용(DEDICATED 고신뢰) 기사만 판단 시점에 열람해 body 를 **메모리에만** 붙인다.

    재일님 승인(2026-07-31) 패턴: 열람 -> 판단 -> 본문 폐기. body 는 LLM 프롬프트
    재료로만 쓰이고 evidence·DB 어디에도 실리지 않는다(아래 verify_and_aggregate 의
    evidence 구조에 body 키가 없고, 자체 점검이 이를 강제한다). 열람 실패는
    헤드라인 판단으로 자연 폴백된다 - 본문이 없다고 판단을 멈추지 않는다.
    """
    from article_reader import ArticleReader

    r = reader or ArticleReader()
    for a in articles:
        conf = float(a.get("confidence") or 0)
        if conf >= min_confidence and a.get("url"):
            body = r.read(a["url"])
            if body:
                a["body"] = body  # 메모리뿐 - 저장 경로 없음
    return articles, r.stats.summary()


def run(symbol: str, *, hours: float = 24.0, as_of: Optional[datetime] = None,
        llm=None, api_base: str = RESEARCH_API, reader=None,
        read_bodies: bool = True) -> SentimentReport:
    ts = as_of or datetime.now(timezone.utc)
    articles = fetch_evidence(symbol, hours=hours, as_of=as_of, api_base=api_base)
    if not articles:
        return SentimentReport(symbol, ts, "NO_EVIDENCE", None, 0, 0, (),
                               "창 안에 기사가 없다")
    read_note = ""
    if read_bodies:
        # as_of 재현(백테스트)에서는 열람하지 않는다 - 지금의 웹페이지는 그때의
        # 지면이 아니므로 PIT 가 깨진다. 실시간 판단에서만 연다.
        if as_of is None:
            articles, read_note = attach_bodies(articles, reader=reader)
        else:
            read_note = "as_of 재현이라 열람 생략(PIT)"
    try:
        batch = judge_articles(symbol, articles, llm=llm)
    except RuntimeError as e:
        return SentimentReport(symbol, ts, "INCONCLUSIVE", None, 0, 0, (), str(e))
    report = verify_and_aggregate(symbol, ts, articles, batch)
    if read_note:
        report = SentimentReport(
            report.symbol, report.as_of, report.verdict, report.score,
            report.articles_used, report.articles_dropped, report.evidence,
            (report.detail + " | 열람: " + read_note).strip(" |"),
        )
    return report


# ---------------------------------------------------------------------------
# 자체 점검 - LLM·API 없이 (가짜 judge 주입)
# ---------------------------------------------------------------------------

def _art(i: int, w: float) -> dict:
    return {"document_id": f"d{i}", "title": f"기사{i}", "relation_type": "DEDICATED",
            "weight": w, "published_at": "2026-07-31T04:00:00+00:00",
            "observed_at": "2026-07-31T04:05:00+00:00"}


def _check_aggregate():
    ts = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
    arts = [_art(1, 1.0), _art(2, 0.5), _art(3, 0.1)]
    batch = JudgementBatch(judgements=[
        ArticleJudgement(document_id="d1", sentiment=1, salience=1.0, reason="호재"),
        ArticleJudgement(document_id="d2", sentiment=-1, salience=1.0, reason="악재"),
        ArticleJudgement(document_id="d3", sentiment=1, salience=0.0, reason="무관"),
    ])
    r = verify_and_aggregate("005930", ts, arts, batch)
    # (1*1.0 - 1*0.5 + 0) / (1.0+0.5+0) = 0.3333 - 가중 평균이 결정론적으로 나온다
    assert r.verdict == "SCORED" and abs(r.score - 0.3333) < 1e-3, r.summary()
    assert r.articles_used == 3 and r.articles_dropped == 0
    print("  결정론 집계              OK")


def _check_hallucinated_citation():
    ts = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
    arts = [_art(1, 1.0)]
    batch = JudgementBatch(judgements=[
        ArticleJudgement(document_id="d1", sentiment=1, salience=0.8, reason="ok"),
        ArticleJudgement(document_id="없는문서", sentiment=1, salience=1.0, reason="환각"),
        ArticleJudgement(document_id="d1", sentiment=-1, salience=1.0, reason="중복"),
    ])
    r = verify_and_aggregate("005930", ts, arts, batch)
    assert r.articles_used == 1 and r.articles_dropped == 2, "환각·중복 인용이 살아남았다"
    assert r.score == 1.0
    # 전부 환각이면 점수를 만들지 않는다
    r2 = verify_and_aggregate("005930", ts, arts, JudgementBatch(judgements=[
        ArticleJudgement(document_id="x", sentiment=1, salience=1.0, reason="환각")]))
    assert r2.verdict == "INCONCLUSIVE" and r2.score is None
    print("  환각 인용 차단           OK")


def _check_schema_retry():
    calls = {"n": 0}

    def flaky_llm(_s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return "죄송하지만 JSON 이 아닙니다"
        return json.dumps({"judgements": [
            {"document_id": "d1", "sentiment": 1, "salience": 0.5, "reason": "ok"}]})

    b = judge_articles("005930", [_art(1, 1.0)], llm=flaky_llm)
    assert calls["n"] == 2 and len(b.judgements) == 1

    def broken_llm(_s, _u):
        return "no json ever"

    try:
        judge_articles("005930", [_art(1, 1.0)], llm=broken_llm)
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  Schema 검증/재시도       OK")


def _check_body_never_persists():
    """열람 본문이 판단 재료로만 쓰이고 evidence(저장 대상)에 새지 않는지."""
    ts = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
    art = {**_art(1, 1.0), "confidence": 0.9, "url": "https://x.invalid/1"}

    class _FakeReader:
        def __init__(self):
            from types import SimpleNamespace
            self.stats = SimpleNamespace(summary=lambda: "열람 1")
        def read(self, url):
            return "본문 텍스트 " * 50

    arts, note = attach_bodies([art], reader=_FakeReader())
    assert arts[0].get("body"), "본문이 안 붙었다"
    batch = JudgementBatch(judgements=[
        ArticleJudgement(document_id="d1", sentiment=1, salience=0.9, reason="ok")])
    r = verify_and_aggregate("005930", ts, arts, batch)
    for e in r.evidence:
        assert "body" not in e, "열람 본문이 evidence(저장 대상)로 샜다 - 비저장 계약 위반"
    print("  본문 비저장 계약         OK")


def _check_no_evidence():
    r = run("005930", llm=lambda s, u: "안 불려야 한다",
            api_base="http://127.0.0.1:1",  # 닫힌 포트 - fetch 실패는 예외로 드러난다
            )
    raise AssertionError("닫힌 API 가 조용히 지나갔다: " + r.summary())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        sym = opt("--run", "005930")
        as_of = opt("--as-of", None)
        ts = datetime.fromisoformat(as_of) if as_of else None
        print(f"{AGENT_VERSION} 실행: {sym}")
        rep = run(sym, hours=float(opt("--hours", 24.0)), as_of=ts)
        print(f"  {rep.summary()}")
        if rep.detail:
            print(f"  {rep.detail}")
        for e in rep.evidence[:8]:
            print(f"    [{e['sentiment']:+d} sal={e['salience']:.1f} w={e['weight']:.2f}] "
                  f"{e['title'][:48]}")
            print(f"        {e['reason'][:70]}")
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (LLM·API 없음)")
    _check_aggregate()
    _check_hallucinated_citation()
    _check_schema_retry()
    _check_body_never_persists()
    try:
        _check_no_evidence()
    except AssertionError as e:
        raise
    except Exception:
        print("  API 장애 fail-closed     OK")  # URLError 등 - 조용히 0점 내지 않는다
    print("직원 에이전트 6개 영역 통과. 실행은 --run <종목코드>")
