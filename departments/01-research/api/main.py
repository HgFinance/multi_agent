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

실행:   uvicorn departments.01-research.api.main:app --port 8035   (경로 하이픈 탓에
        실제로는 compose 가 api/ 디렉터리에서 uvicorn main:app 으로 띄운다)
점검:   python api/main.py          # DB 없이 자체 점검
        python api/main.py --probe  # 실 DB 관통 (Endpoint 함수 직접 호출)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from source_registry import load_project_env  # noqa: E402

API_VERSION = "research-api-v1"
KST = timezone(timedelta(hours=9))

# 가중치 반감기(시간). news_recent_weighted View 와 같은 상수다 - 다르게 두면
# 실시간(View)과 재현(API)이 다른 점수를 낸다.
WEIGHT_HALF_LIFE_HOURS = 6.0

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
    print("research-api 3개 영역 통과. 관통은 --probe, 서버는 compose research-api")
