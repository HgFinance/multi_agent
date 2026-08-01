#!/usr/bin/env python3
"""research-api - Evidence 읽기 전용 조회면 (FastAPI).

담당: 재일 (리서치/퀀트)
근거: TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md Sprint J2 "research-api Evidence 조회"(미착수였음)
      CLAUDE.md 개발 원칙 1 (Agent보다 데이터 계약 먼저) - 에이전트는 DB에 직접
      붙지 않고 이 API로 Evidence 를 읽는다. LangGraph 직원의 tool 이 여기 붙는다.

경계 셋을 코드로 강제한다.

1. **읽기 전용이다.** 쓰기 Endpoint 가 없고, DB 세션 자체를
   default_transaction_read_only=on 으로 연다 - 코드 실수로도 못 쓴다.
2. **PIT 가 기본이다.** 모든 Evidence 질의는 as_of(기본=지금)를 받아
   **observed_at <= as_of** 만 돌려준다. 백테스트가 실시간과 같은 API 를 쓰면서
   미래 정보를 볼 수 없다. 가중치도 View 의 now() 가 아니라 as_of 기준으로
   다시 계산한다 - View 를 그대로 노출하면 과거 재현에서 미래 감쇠가 샌다.
3. **본문이 없다.** documents 에는 제목·URL뿐이다(가이드 3.3 라이선스).
   예외는 공시 원문 발췌뿐이다 - opendart 는 UseScope.EMBEDDING 이 허용된
   소스라 rag_librarian 이 적재한 evidence_chunks 를 /evidence/search 가
   발췌(300자)로 내준다. 뉴스 스니펫은 여전히 안 나간다.

실행:   uvicorn departments.01-research.api.main:app --port 8035   (경로 하이픈 탓에
        실제로는 compose 가 api/ 디렉터리에서 uvicorn main:app 으로 띄운다)
점검:   python api/main.py          # DB 없이 자체 점검
        python api/main.py --probe  # 실 DB 관통 (Endpoint 함수 직접 호출)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from evidence.story_cluster import build_stories  # noqa: E402
from source_registry import load_project_env  # noqa: E402

API_VERSION = "research-api-v1"
KST = timezone(timedelta(hours=9))

# 가중치 반감기(시간). news_recent_weighted View 와 같은 상수다 - 다르게 두면
# 실시간(View)과 재현(API)이 다른 점수를 낸다.
WEIGHT_HALF_LIFE_HOURS = 6.0

# /evidence/search 상수. 모델·차원은 agents/rag_librarian.py 인덱서와 같아야
# 한다 - 다른 모델로 질의를 임베딩하면 코사인 거리가 무의미해진다. import 로
# 공유하고 싶지만 컨테이너 이미지에 agents/ 가 없어(Dockerfile COPY 목록) 상수를
# 복제한다 - 인덱서 모델을 바꾸면 여기도 함께 바꾼다.
EMBED_MODEL = "bge-m3"
EMBED_DIMS = 1024
SEARCH_K_DEFAULT = 5
SEARCH_K_MAX = 20          # 청크 발췌 k건이면 충분하다 - 큰 k 는 DB 부하만 늘린다
SEARCH_EXCERPT_CHARS = 300

app = FastAPI(title="Research Evidence API", version="0.1.0")

_conn = None


def get_conn():
    """읽기 전용 DB 연결(지연 생성·재사용). 끊겼으면 다시 만든다."""
    global _conn
    import psycopg2

    if _conn is not None and not _conn.closed:
        return _conn
    _conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=10)
    with _conn.cursor() as cur:
        cur.execute("set default_transaction_read_only = on")
    _conn.commit()
    return _conn


def _query(sql: str, params: tuple):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        conn.rollback()  # read-only 라도 트랜잭션은 닫는다
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def _as_of_or_now(as_of: Optional[datetime]) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        # 모호한 naive 시각은 거부한다 - KST 인지 UTC 인지 추측하면 PIT 가 9시간 샌다
        raise HTTPException(422, "as_of 는 timezone 이 있어야 한다 (예: ...+09:00)")
    return as_of.astimezone(timezone.utc)


def recency_weight(published_at: datetime, as_of: datetime,
                   half_life_hours: float = WEIGHT_HALF_LIFE_HOURS) -> float:
    """as_of 기준 지수 시간감쇠. 미래 게시(공급자 시계 이상)는 1.0 로 상한."""
    age_h = (as_of - published_at).total_seconds() / 3600.0
    if age_h <= 0:
        return 1.0
    return 2.0 ** (-age_h / half_life_hours)


class NewsEvidence(BaseModel):
    document_id: str
    source: str
    title: str
    url: Optional[str]
    published_at: datetime
    observed_at: datetime = Field(description="이 시각 이후에만 '알 수 있었던' 기사다")
    relation_type: Optional[str]
    confidence: Optional[float]
    weight: float = Field(description=f"2^(-age/{WEIGHT_HALF_LIFE_HOURS}h), as_of 기준")


class HealthDomain(BaseModel):
    domain: str
    rows: int
    last_observed_kst: Optional[str]


@app.get("/health")
def health() -> dict:
    rows = _query(
        """
        select s.source_code as domain, count(*) as rows,
               to_char(max(d.observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI') as last
        from research.documents d join reference.data_sources s using (source_id)
        group by 1
        union all
        select 'financial_facts', count(*),
               to_char(max(observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI')
        from research.financial_facts
        union all
        select 'macro_observations', count(*),
               to_char(max(observed_at) at time zone 'Asia/Seoul', 'MM-DD HH24:MI')
        from research.macro_observations
        """,
        (),
    )
    return {
        "version": API_VERSION,
        "read_only": True,
        "domains": [HealthDomain(domain=r["domain"], rows=r["rows"],
                                 last_observed_kst=r["last"]).model_dump()
                    for r in rows],
    }


@app.get("/evidence/news", response_model=list[NewsEvidence])
def evidence_news(
    symbol: str = Query(..., min_length=6, max_length=6, description="KRX 종목코드"),
    as_of: Optional[datetime] = Query(None, description="PIT 기준 시각(tz 필수). 없으면 지금"),
    hours: float = Query(24.0, gt=0, le=24 * 7, description="published_at 소급 창"),
    limit: int = Query(50, gt=0, le=200),
):
    """종목의 뉴스 Evidence. observed_at <= as_of 만 - 백테스트가 그대로 쓴다."""
    ts = _as_of_or_now(as_of)
    rows = _query(
        """
        select d.document_id::text, s.source_code as source, d.title, d.canonical_url as url,
               d.published_at, d.observed_at, di.relation_type, di.confidence
        from research.documents d
        join reference.data_sources s using (source_id)
        join research.document_instruments di using (document_id)
        join reference.instrument_symbols isym
          on isym.instrument_id = di.instrument_id and isym.is_primary
        where d.document_type = 'NEWS' and d.status = 'ACTIVE'
          and isym.symbol = %s
          and d.observed_at <= %s
          and d.published_at > %s - make_interval(secs => %s)
        order by d.published_at desc
        limit %s
        """,
        (symbol, ts, ts, hours * 3600.0, limit),
    )
    return [
        NewsEvidence(
            document_id=r["document_id"], source=r["source"], title=r["title"],
            url=r["url"], published_at=r["published_at"], observed_at=r["observed_at"],
            relation_type=r["relation_type"],
            confidence=float(r["confidence"]) if r["confidence"] is not None else None,
            weight=round(recency_weight(r["published_at"], ts), 4),
        )
        for r in rows
    ]


@app.get("/evidence/disclosures")
def evidence_disclosures(
    symbol: Optional[str] = Query(None, min_length=6, max_length=6),
    as_of: Optional[datetime] = Query(None),
    days: float = Query(7.0, gt=0, le=90),
    limit: int = Query(50, gt=0, le=200),
):
    """공시 Evidence. ⚠ published_at 이 날짜뿐(DART 한계)이라 시각 판단은 observed_at."""
    ts = _as_of_or_now(as_of)
    sym_join = (
        "join reference.instruments i on i.issuer_id = d.issuer_id "
        "join reference.instrument_symbols isym "
        "  on isym.instrument_id = i.instrument_id and isym.is_primary "
        if symbol else ""
    )
    sym_cond = "and isym.symbol = %s" if symbol else ""
    params: list = [ts, ts, days * 86400.0]
    if symbol:
        params.append(symbol)
    params.append(limit)
    return _query(
        f"""
        select d.document_id::text, d.title, d.canonical_url as url,
               d.published_at, d.observed_at, d.status
        from research.documents d
        join reference.data_sources s using (source_id)
        {sym_join}
        where s.source_code = 'opendart'
          and d.observed_at <= %s
          and d.observed_at > %s - make_interval(secs => %s)
          {sym_cond}
        order by d.observed_at desc
        limit %s
        """,
        tuple(params),
    )


@app.get("/evidence/financials")
def evidence_financials(
    symbol: str = Query(..., min_length=6, max_length=6),
    as_of: Optional[datetime] = Query(None),
    limit: int = Query(100, gt=0, le=500),
):
    """재무 Evidence. account_code 는 dart_major_account_nm scheme 이다(가이드 J2)."""
    ts = _as_of_or_now(as_of)
    return _query(
        """
        select f.account_code, f.value, f.unit, f.currency, f.period_end,
               f.consolidation_scope, f.published_at, f.observed_at
        from research.financial_facts f
        join reference.issuers iss on iss.issuer_id = f.issuer_id
        join reference.instruments i on i.issuer_id = iss.issuer_id
        join reference.instrument_symbols isym
          on isym.instrument_id = i.instrument_id and isym.is_primary
        where isym.symbol = %s and f.observed_at <= %s
        order by f.period_end desc, f.account_code
        limit %s
        """,
        (symbol, ts, limit),
    )


@app.get("/macro/observations")
def macro_observations(
    codes: str = Query(..., description="쉼표 구분 external_series_code (최대 30개)"),
    days: int = Query(120, gt=0, le=3650, description="period 소급 창(일)"),
    as_of: Optional[datetime] = Query(None, description="PIT 기준 시각(tz 필수)"),
):
    """거시·지정학 시계열 조회 (RES-09 지정학 분석가의 재료).

    **PIT 핵심 두 가지**
    1. `published_at <= as_of` - 그 시점에 실제로 공표된 값만 준다. GPR 은
       4~5일 늦게 나오므로(수집기가 period+7일로 보수 기록) 사건 당일에
       지수를 알았다고 착각하는 누수를 여기서 원천 차단한다.
    2. 같은 period 에 여러 vintage 가 있으면 as_of 시점 **가장 최신 vintage**
       하나만 준다(개정 이력은 append 되므로 골라야 한다).
    """
    ts = _as_of_or_now(as_of)
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(422, "codes 가 비었다")
    if len(code_list) > 30:
        raise HTTPException(422, f"codes 는 30개 이하 (요청 {len(code_list)}개)")
    return _query(
        """
        select distinct on (s.external_series_code, o.period)
               s.external_series_code as code, o.period, o.value,
               o.published_at, o.vintage_date
        from research.macro_observations o
        join research.macro_series s on s.series_id = o.series_id
        where s.external_series_code = any(%s)
          and o.published_at <= %s
          and o.period >= (%s::date - make_interval(days => %s))
        order by s.external_series_code, o.period,
                 o.vintage_date desc, o.revision desc
        """,
        (code_list, ts, ts, days),
    )


# ---------------------------------------------------------------------------
# /evidence/search - 공시 원문 시맨틱 검색 (rag_librarian 이 적재한 청크)
# ---------------------------------------------------------------------------

def _require_query(q: str) -> str:
    """빈/공백 질의는 422 로 끊는다 - 빈 질의를 임베딩하면 무의미한 벡터로
    '그럴듯한' 결과가 나와 오히려 위험하다."""
    q = q.strip()
    if not q:
        raise HTTPException(422, "q 는 빈 문자열일 수 없다")
    return q


# 한 번에 임베딩할 텍스트 수. 실측(2026-08-01, 000660 48h 제목 996건): 300~500
# 건을 한 요청으로 보내면 Ollama 내부 runner 가 간헐로 죽어 400("/tokenize
# connection refused")이 난다 - 100건 서브배치는 996건 전체가 11.6초에 안정
# 통과했다. rag_librarian 의 EMBED_BATCH 서브배치와 같은 패턴이다.
EMBED_BATCH_SIZE = 100


def _embed_batch(texts: list[str], *, base: Optional[str] = None,
                 timeout: int = 120) -> list[list[float]]:
    """텍스트 배치 -> 벡터 목록 (내부는 서브배치). Ollama 불가는 503.

    빈 결과([])로 위장하지 않는다 - 호출자(에이전트)가 '관련 근거 없음'과
    '임베딩 인프라 죽음'을 구분하지 못하면 잘못된 결론(근거 없음)을 낸다.
    """
    base = (base or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
    vecs: list[list[float]] = []
    try:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            req = urllib.request.Request(
                base + "/api/embed", method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"model": EMBED_MODEL,
                                 "input": texts[i:i + EMBED_BATCH_SIZE]}).encode())
            with urllib.request.urlopen(req, timeout=timeout) as r:
                vecs.extend(json.loads(r.read())["embeddings"])
    except (urllib.error.URLError, OSError) as e:
        # HTTPError(모델 미설치 404 등)도 URLError 하위라 여기로 온다
        raise HTTPException(503, f"임베딩 실패 - Ollama({base}) 불가: "
                                 f"{type(e).__name__} {str(e)[:120]}")
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(503, f"Ollama({base}) 응답 형식 이상: {type(e).__name__} {e}")
    if len(vecs) != len(texts) or any(len(v) != EMBED_DIMS for v in vecs):
        raise HTTPException(503, f"임베딩 형상 이상 ({len(vecs)}개/{EMBED_DIMS}차원 "
                                 f"기대) - 인덱스와 다른 모델이다({EMBED_MODEL} 확인)")
    return vecs


def _embed_query(text: str, *, base: Optional[str] = None) -> str:
    """질의문 -> pgvector 리터럴 (_embed_batch 1건 + 리터럴 변환)."""
    vec = _embed_batch([text], base=base, timeout=60)[0]
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


@app.get("/evidence/search")
def evidence_search(
    q: str = Query(..., min_length=1, description="자연어 질의 (공백만이면 422)"),
    k: int = Query(SEARCH_K_DEFAULT, ge=1, le=SEARCH_K_MAX, description="상위 k건"),
    as_of: Optional[datetime] = Query(
        None, description="PIT 상한 - 이 시각까지 **관측된** 청크만 (tz 필수)"),
):
    """공시 원문 청크 코사인 상위 k. 읽기 전용 GET - 세션도 read-only 다.

    as_of 없으면 전체 코퍼스 탐색(리서치용), 있으면 observed_at <= as_of 로
    가시성을 자른 PIT 검색(재현용) - 다른 Evidence Endpoint 와 같은 규칙이다.
    ⚠ 청크 observed_at 은 **인덱싱 시각**이다: 원문 자체는 그 전부터 Storage
    에 있었을 수 있으므로 이 필터는 보수적(늦은) 가시성이다 - 미래를 당겨오지
    않는 쪽으로만 틀린다. 정렬 연산(<=>)은 HNSW 인덱스를 태운다.
    """
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of 는 timezone 이 있어야 한다 (PIT 9시간 오차 방지)")
    qvec = _embed_query(_require_query(q))
    pit_cond = "" if as_of is None else "and c.observed_at <= %s"
    # 같은 문서의 인접 청크가 상위를 도배하는 실측 한계 - 후보를 3배로 받아
    # 문서당 최고 유사도 청크 1건만 남긴다(결정론 후처리 - DISTINCT ON 은
    # HNSW 정렬과 충돌한다)
    fetch_n = min(k * 3, 60)
    params: tuple = (qvec, SEARCH_EXCERPT_CHARS)
    if as_of is not None:
        params += (as_of,)
    params += (qvec, fetch_n)
    rows = _query(
        f"""
        select c.document_version_id::text,
               c.metadata->>'title' as title,
               c.published_at,
               c.observed_at,
               1 - (c.embedding <=> %s::extensions.vector) as cosine_sim,
               left(c.content, %s) as content
        from research.evidence_chunks c
        where c.embedding is not null {pit_cond}
        order by c.embedding <=> %s::extensions.vector
        limit %s
        """,
        params,
    )
    for r in rows:
        r["cosine_sim"] = round(float(r["cosine_sim"]), 4)
    return _dedupe_by_doc(rows, k)


def _dedupe_by_doc(rows: list[dict], k: int) -> list[dict]:
    """문서당 최고 유사도 청크 1건 - 입력은 유사도 내림차순이 전제(순수 함수)."""
    seen: set = set()
    out = []
    for r in rows:
        doc = r.get("document_version_id")
        if doc in seen:
            continue
        seen.add(doc)
        out.append(r)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# /evidence/stories - 뉴스 스토리 군집 (근접중복 -> 사건 단위)
# ---------------------------------------------------------------------------

# 군집 임계 운영 범위. 0.5 미만은 무관 기사까지 사슬로 묶이고(연결 성분 특성상
# 저임계는 폭주한다), 0.99 초과는 사실상 완전 일치라 군집의 의미가 없다.
STORY_THRESHOLD_MIN = 0.5
STORY_THRESHOLD_MAX = 0.99
# cluster_titles 의 O(n^2) 가드(1500)보다 낮게 - DB 에서부터 창을 자른다
STORY_FETCH_LIMIT = 1000


def _require_threshold(threshold: float) -> float:
    """임계 범위 강제. Query 검증은 HTTP 경로에서만 돌므로 직접 호출
    (자체점검·probe)에서도 같은 규칙이 서도록 함수로 분리해 다시 검증한다."""
    if not (STORY_THRESHOLD_MIN <= threshold <= STORY_THRESHOLD_MAX):
        raise HTTPException(422, f"threshold 는 {STORY_THRESHOLD_MIN}~"
                                 f"{STORY_THRESHOLD_MAX} 여야 한다: {threshold}")
    return threshold


@app.get("/evidence/stories")
def evidence_stories(
    symbol: str = Query(..., min_length=6, max_length=6, description="KRX 종목코드"),
    as_of: Optional[datetime] = Query(None, description="PIT 기준 시각(tz 필수). 없으면 지금"),
    hours: float = Query(24.0, gt=0, le=24 * 7, description="published_at 소급 창"),
    threshold: float = Query(0.85, ge=STORY_THRESHOLD_MIN, le=STORY_THRESHOLD_MAX,
                             description="코사인 유사도 임계 (0.5~0.99)"),
):
    """종목 뉴스를 스토리(같은 사건) 단위로 군집해 돌려준다.

    /evidence/news 와 같은 소스·같은 PIT 규칙(observed_at <= as_of)의 기사를
    제목 임베딩(bge-m3) -> 코사인 연결 성분으로 묶는다 - 감성·Packet 이 한
    사건을 여러 기사로 중복 가중하는 문제의 해법(story_cluster.py docstring).
    Ollama 불가는 503(빈 결과 위장 금지), 기사 0건은 note 로 자기 기술한다.
    """
    _require_threshold(threshold)   # DB·Ollama 도달 전에 끊는다
    ts = _as_of_or_now(as_of)
    rows = _query(
        """
        select d.document_id::text, s.source_code as source, d.title,
               d.published_at, d.observed_at
        from research.documents d
        join reference.data_sources s using (source_id)
        join research.document_instruments di using (document_id)
        join reference.instrument_symbols isym
          on isym.instrument_id = di.instrument_id and isym.is_primary
        where d.document_type = 'NEWS' and d.status = 'ACTIVE'
          and isym.symbol = %s
          and d.observed_at <= %s
          and d.published_at > %s - make_interval(secs => %s)
        order by d.published_at desc
        limit %s
        """,
        (symbol, ts, ts, hours * 3600.0, STORY_FETCH_LIMIT),
    )
    head = {"symbol": symbol, "as_of": ts.isoformat(), "hours": hours}
    if not rows:
        return {**head, "threshold": threshold, "total": 0,
                "stories": [], "singletons": 0, "clustered_ratio": 0.0,
                "note": "기사 없음"}
    return {**head, **build_stories(rows, embed=_embed_batch,
                                    threshold=threshold)}


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없이
# ---------------------------------------------------------------------------

def _check_weight():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    assert recency_weight(now, now) == 1.0
    assert abs(recency_weight(now - timedelta(hours=6), now) - 0.5) < 1e-9, "반감기 6h"
    assert abs(recency_weight(now - timedelta(hours=12), now) - 0.25) < 1e-9
    assert recency_weight(now + timedelta(minutes=5), now) == 1.0, "미래 게시는 1.0 상한"
    print("  가중치 감쇠              OK")


def _check_as_of():
    from fastapi import HTTPException as HE

    ts = _as_of_or_now(None)
    assert ts.tzinfo is not None
    kst = datetime(2026, 7, 31, 9, 0, tzinfo=KST)
    assert _as_of_or_now(kst).hour == 0, "KST 09:00 은 UTC 00:00 이다"
    try:
        _as_of_or_now(datetime(2026, 7, 31, 9, 0))
        raise AssertionError("naive as_of 가 통과했다 - PIT 9시간 오차 위험")
    except HE:
        pass
    print("  as_of 규칙               OK")


def _check_readonly_surface():
    """쓰기 Endpoint 가 없는지 - 경로 목록으로 강제한다."""
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        assert not (methods - {"GET", "HEAD", "OPTIONS"}), \
            f"읽기 전용 API 에 쓰기 메서드가 있다: {route.path} {methods}"
    print("  읽기 전용 표면           OK")


def _check_search_rules():
    """검색 질의 검증·장애 노출 규칙 - Ollama·DB 없이 확인한다."""
    from fastapi import HTTPException as HE

    # 빈/공백 질의는 422 - 조용히 전체 코퍼스 유사도를 내주면 안 된다
    for bad in ("", "   ", "\t\n"):
        try:
            _require_query(bad)
            raise AssertionError(f"빈 질의 {bad!r} 가 통과했다")
        except HE as e:
            assert e.status_code == 422
    assert _require_query(" 유상증자 결정 ") == "유상증자 결정"

    # 검색 표면은 GET 전용이고 등록돼 있다 (읽기 전용 원칙에 편승)
    route = next(r for r in app.routes
                 if getattr(r, "path", None) == "/evidence/search")
    assert getattr(route, "methods", set()) == {"GET"}, "검색은 GET 전용이다"

    # PIT: naive as_of 는 422 - 다른 Evidence Endpoint 와 같은 규칙 (임베딩
    # 호출 전에 거부되므로 Ollama 없이 검증 가능)
    try:
        evidence_search(q="유상증자", k=1, as_of=datetime(2026, 8, 1, 9, 0))
        raise AssertionError("naive as_of 가 통과했다")
    except HE as e:
        assert e.status_code == 422 and "timezone" in str(e.detail)

    # Ollama 불가 -> 503 (빈 결과 위장 금지). 닫힌 로컬 포트라 즉시 거부되고
    # 외부 네트워크·실행 중인 서비스가 필요 없다.
    try:
        _embed_query("x", base="http://127.0.0.1:1")
        raise AssertionError("도달 불가 Ollama 가 통과했다")
    except HE as e:
        assert e.status_code == 503 and "Ollama" in str(e.detail), \
            f"503+사유가 아니다: {e.status_code} {e.detail}"
    # 문서당 중복 제거 - 유사도 내림차순 입력에서 문서별 최고 1건, k 에서 끊김
    rows = [{"document_version_id": "d1", "cosine_sim": 0.9},
            {"document_version_id": "d1", "cosine_sim": 0.8},
            {"document_version_id": "d2", "cosine_sim": 0.7},
            {"document_version_id": "d3", "cosine_sim": 0.6}]
    dd = _dedupe_by_doc(rows, 2)
    assert [r["document_version_id"] for r in dd] == ["d1", "d2"] and dd[0]["cosine_sim"] == 0.9
    print("  검색 질의·503 규칙       OK")


def _check_stories_rules():
    """스토리 군집 검증·읽기 전용 표면 - DB·Ollama 없이 확인한다."""
    from fastapi import HTTPException as HE

    # threshold 범위 밖은 422 - Endpoint 직접 호출이 DB·임베딩 도달 전에
    # 끊긴다 (0.5 미만은 연결 성분 사슬 폭주, 0.99 초과는 무의미)
    for bad in (0.3, 0.499, 0.991, 1.0):
        try:
            evidence_stories(symbol="000660", as_of=None, hours=24.0,
                             threshold=bad)
            raise AssertionError(f"threshold={bad} 가 통과했다")
        except HE as e:
            assert e.status_code == 422 and "threshold" in str(e.detail), \
                f"422+사유가 아니다: {e.status_code} {e.detail}"
    # 경계값(0.5, 0.99)은 통과 - 헬퍼 단독 검사라 DB·Ollama 유무와 무관하다
    for edge in (0.5, 0.85, 0.99):
        assert _require_threshold(edge) == edge

    # naive as_of 는 다른 Evidence Endpoint 와 같은 422 규칙
    try:
        evidence_stories(symbol="000660", as_of=datetime(2026, 8, 1, 9, 0),
                         hours=24.0, threshold=0.85)
        raise AssertionError("naive as_of 가 통과했다")
    except HE as e:
        assert e.status_code == 422 and "timezone" in str(e.detail)

    # 스토리 표면도 GET 전용으로 등록돼 있다 - 읽기 전용 원칙 유지
    route = next(r for r in app.routes
                 if getattr(r, "path", None) == "/evidence/stories")
    assert getattr(route, "methods", set()) == {"GET"}, "스토리는 GET 전용이다"

    # 배치 임베딩도 Ollama 불가 503 (빈 결과 위장 금지) - 닫힌 로컬 포트
    try:
        _embed_batch(["a", "b"], base="http://127.0.0.1:1")
        raise AssertionError("도달 불가 Ollama 배치가 통과했다")
    except HE as e:
        assert e.status_code == 503 and "Ollama" in str(e.detail)

    # 군집 계산부(순수 함수)는 story_cluster 자체 점검이 담당한다 - 여기서는
    # 결선(가짜 임베딩 -> 스토리 요약 형상)만 재확인한다
    fake = build_stories(
        [{"title": "A 상한가", "source": "naver_apihub", "document_id": "d1",
          "published_at": "2026-08-01T01:00:00+00:00"},
         {"title": "A 가격제한폭", "source": "ls_news", "document_id": "d2",
          "published_at": "2026-08-01T02:00:00+00:00"}],
        embed=lambda ts_: [[1.0, 0.0], [1.0, 0.0]], threshold=0.85)
    assert fake["stories"][0]["size"] == 2, fake
    assert fake["stories"][0]["representative_title"] == "A 상한가", fake
    print("  스토리 군집 규칙         OK")


def _probe():
    """실 DB 관통 - 서버 없이 Endpoint 함수를 직접 부른다."""
    h = health()
    print(f"  /health: {len(h['domains'])}개 도메인")
    for d in h["domains"]:
        print(f"    {d['domain']:20} {d['rows']:>7,}  {d['last_observed_kst']}")

    news = evidence_news(symbol="005930", as_of=None, hours=24.0, limit=5)
    print(f"  /evidence/news?symbol=005930 (24h): {len(news)}건")
    for n in news[:3]:
        print(f"    w={n.weight:.3f} [{n.published_at.astimezone(KST):%H:%M}] {n.title[:44]}")
    assert news, "삼성전자 24시간 뉴스가 0건일 리 없다 - 수집 확인"

    # PIT 회귀: 오늘 06:00(KST) 기준이면 그 이후 관측 기사는 안 보여야 한다
    cutoff = datetime.now(KST).replace(hour=6, minute=0, second=0, microsecond=0)
    past = evidence_news(symbol="005930", as_of=cutoff, hours=24.0, limit=200)
    assert all(n.observed_at <= cutoff.astimezone(timezone.utc) for n in past), \
        "as_of 이후 관측 기사가 샜다 - PIT 위반"
    print(f"  PIT 재현(as_of=06:00): {len(past)}건, 전부 observed<=as_of")

    dis = evidence_disclosures(symbol="005930", as_of=None, days=7.0, limit=5)
    print(f"  /evidence/disclosures?symbol=005930 (7d): {len(dis)}건")
    fin = evidence_financials(symbol="005930", as_of=None, limit=5)
    print(f"  /evidence/financials?symbol=005930: {len(fin)}건")

    # 시맨틱 검색 관통 - 호스트에서는 127.0.0.1:11434 의 Ollama 를 그대로 쓴다
    hits = evidence_search(q="유상증자 결정", k=3)
    print(f"  /evidence/search?q=유상증자 결정: {len(hits)}건")
    for h in hits:
        print(f"    sim={h['cosine_sim']:.4f} {str(h['title'])[:44]}")
    assert hits and hits[0]["cosine_sim"] > 0.4, "유상증자 검색 상위가 비었거나 무관하다"

    # 스토리 군집 관통 - NAVER·LS 근접중복이 실제로 묶이는지 (48h 창)
    st = evidence_stories(symbol="000660", as_of=None, hours=48.0, threshold=0.85)
    print(f"  /evidence/stories?symbol=000660 (48h): 기사 {st['total']}건 -> "
          f"스토리 {len(st['stories'])}개 / 단독 {st['singletons']}건 "
          f"(clustered_ratio={st['clustered_ratio']})")
    for s_ in st["stories"][:2]:
        print(f"    size={s_['size']} sources={s_['sources']} "
              f"{str(s_['representative_title'])[:44]}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--probe" in sys.argv:
        print(f"{API_VERSION} 실 DB 관통")
        raise SystemExit(_probe())

    print(f"{API_VERSION} 자체 점검 (DB 없이)")
    _check_weight()
    _check_as_of()
    _check_readonly_surface()
    _check_search_rules()
    _check_stories_rules()
    print("research-api 5개 영역 통과. 관통은 --probe, 서버는 compose research-api")
