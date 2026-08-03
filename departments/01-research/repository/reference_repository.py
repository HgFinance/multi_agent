#!/usr/bin/env python3
"""Sprint J1/F02: Supabase reference Repository - Instrument Master와 Symbol Mapping.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 5.1(reference Schema), 8.1(공통 ID), 8.3
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F02(Instrument Universe)
      supabase/migrations/20260729000100_foundation_reference.sql

여기가 영구 instrument_id 를 발급하는 유일한 곳이다. LS 종목코드는 Alias 이며
instrument_symbols 가 (provider, market, symbol) -> instrument_id 매핑을 시간축과 함께
들고 있다. Ticker 가 바뀌어도 같은 Instrument 를 추적하기 위한 구조다(F02 완료 조건).

▶ 스키마 관례 (마이그레이션에 CHECK 가 없어 자유 텍스트다. 여기서 정하고 문서에 남긴다)
    market  = 'KRX'                 거래소. TimescaleDB market.market_ticks.market 과 같은 축이라
                                    Application 계층 Join 이 성립한다(가이드 2.2)
    venue   = 'KOSPI' | 'KOSDAQ'    세부 시장(Board). LS 실시간 TR 선택 축이다
    asset_class = 'EQUITY'
    instrument_type = 'STOCK' | 'ETF' | 'ETN'
    currency = 'KRW'

▶ 멱등성
    instruments        unique nulls not distinct (isin) 을 자연키로 upsert 한다.
                       **NULLS NOT DISTINCT 라 isin 이 NULL 인 행은 전체에서 하나만
                       허용된다.** 그래서 isin 없는 종목은 적재하지 않고 세어서 돌려준다.
    instrument_symbols unique (provider, market, symbol, valid_from). 이미 열려 있는
                       (valid_to is null) 매핑이 같은 instrument 를 가리키면 넣지 않는다.
                       매번 now() 로 넣으면 실행마다 행이 늘어난다.

자체 점검(DB 없이): python departments/01-research/repository/reference_repository.py
실제 적재:          python departments/01-research/repository/reference_repository.py --ingest
"""
from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
from ls_client import StockMasterRow  # noqa: E402
from source_registry import UseScope, load_project_env  # noqa: E402
from subscription_plan import AssetClass, UniverseMember, Venue  # noqa: E402

REPOSITORY_VERSION = "research-reference-repository-v1"

MARKET_KRX = "KRX"
PROVIDER_LS = "LS"

# document_instruments.extraction_version 에 남길 값. 언급 관계 판정 규칙이 바뀌면
# 올린다 - 어떤 규칙으로 붙인 관계인지 나중에 구분할 수 있어야 한다.
NEWS_LINK_VERSION = "news-symbol-map-v1"
CURRENCY_KRW = "KRW"


@dataclass(frozen=True)
class InstrumentRecord:
    """reference.instruments 한 행. Column 이름과 1:1로 맞춘다."""

    isin: str
    display_name: str
    market: str
    venue: str
    asset_class: str
    instrument_type: str
    currency: str
    lot_size: Decimal
    status: str = "ACTIVE"
    price_scale: int = 0
    quantity_scale: int = 0
    metadata: dict = field(default_factory=dict)

    # 매핑용. instruments Column 이 아니라 instrument_symbols 로 간다.
    provider_symbol: str = ""


@dataclass(frozen=True)
class IngestResult:
    """적재 결과. 건너뛴 것을 숨기지 않는다(가이드 8.2 DQ)."""

    attempted: int
    instruments_inserted: int
    instruments_updated: int
    symbols_inserted: int
    skipped_no_isin: int
    skipped_duplicate_isin: int

    def summary(self) -> str:
        return (
            f"instruments {self.instruments_inserted} 신규 / {self.instruments_updated} 갱신, "
            f"symbols {self.symbols_inserted} 신규, "
            f"제외 isin없음 {self.skipped_no_isin} / isin중복 {self.skipped_duplicate_isin} "
            f"(입력 {self.attempted})"
        )


def _to_decimal(raw: str, default: str = "1") -> Decimal:
    """숫자 문자열을 Decimal 로. 파싱 실패는 기본값으로 떨어지지 않고 예외다."""
    text = (raw or "").strip()
    if not text:
        return Decimal(default)
    try:
        v = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"수량 단위를 숫자로 읽을 수 없다: {raw!r}") from None
    return v if v > 0 else Decimal(default)


def master_row_to_record(row: StockMasterRow, *, as_of: datetime) -> InstrumentRecord:
    """t8436 한 행을 reference.instruments 계약으로 옮긴다.

    tick_size 는 넣지 않는다 - KRX 호가단위는 가격대별로 달라 단일값이 아니고 t8436 에도
    없다. 추정값을 넣으면 주문 검증이 조용히 틀어진다.
    """
    if row.is_etf:
        itype = "ETF"
    elif row.is_etn:
        itype = "ETN"
    else:
        itype = "STOCK"

    return InstrumentRecord(
        isin=row.expcode,
        display_name=row.hname or row.shcode,
        market=MARKET_KRX,
        venue=row.venue.value,
        asset_class=AssetClass.EQUITY.value,
        instrument_type=itype,
        currency=CURRENCY_KRW,
        # KRX 주식은 원 단위 정수 가격·수량이므로 scale 0 이다.
        lot_size=_to_decimal(row.order_unit),
        price_scale=0,
        quantity_scale=0,
        metadata={
            "source": "ls:t8436",
            "as_of": as_of.isoformat(),
            "shcode": row.shcode,
            "security_group": row.security_group,
            "is_spac": row.is_spac,
            # 상·하한가와 전일가는 스냅샷이다. 정본은 시계열이며 참고용으로만 남긴다.
            "snapshot": {
                "prev_close": row.prev_close,
                "upper_limit": row.upper_limit,
                "lower_limit": row.lower_limit,
            },
        },
        provider_symbol=row.shcode,
    )


def fact_key(fact) -> tuple:
    """financial_facts 의 멱등 키 중 revision 을 뺀 부분.

    revision 은 이 키 안에서 rcept_no 순서로 매겨진다 - 그래서 키에서 뺀다.
    """
    return (fact.corp_code, fact.account_code, str(fact.period_end),
            fact.period_type, fact.consolidation_scope)


def assign_revisions(facts: list, known: dict) -> tuple[list, int]:
    """각 fact 에 revision 을 매긴다. (revision 목록, 새 개정 수).

    known: {fact_key: {rcept_no: revision}} - DB 에 이미 있는 것.
    규칙 (순수 함수라 자체 점검이 전부 검사한다):
      - 같은 rcept_no 가 이미 있으면 **그 revision 을 그대로 쓴다**(재수집 멱등).
      - 새 rcept_no 면 그 키의 max(revision)+1 (없으면 1).
      - 한 배치 안에 같은 키의 rcept_no 가 여럿이면 **rcept_no 오름차순**으로
        번호를 준다 - 접수번호 앞 8자리가 접수일이라 나중 접수가 큰 수다.
      - rcept_no 가 없으면 revision 1 - 개정 순서를 매길 근거가 없으므로
        원본 자리에 둔다(그 자리에 이미 다른 문서가 있으면 덮지 않는다).

    **원본을 지우지 않는 것이 이 함수의 목적이다.** 정정본은 새 행이 된다.
    """
    # 키별로 (rcept_no -> revision) 을 알려진 것에서 시작해 채워 나간다
    table: dict = {k: dict(v) for k, v in (known or {}).items()}
    fresh = 0

    # 배치 안에서도 접수번호 순서로 처리해야 번호가 시간 순서와 맞는다
    order = sorted(range(len(facts)),
                   key=lambda i: (fact_key(facts[i]),
                                  str(getattr(facts[i], "rcept_no", "") or "")))
    revisions = [1] * len(facts)
    for i in order:
        f = facts[i]
        k = fact_key(f)
        rc = str(getattr(f, "rcept_no", "") or "").strip()
        seen = table.setdefault(k, {})
        if not rc:
            revisions[i] = 1
            continue
        if rc in seen:
            revisions[i] = seen[rc]
            continue
        revisions[i] = (max(seen.values()) + 1) if seen else 1
        seen[rc] = revisions[i]
        fresh += 1
    return revisions, fresh


class ReferenceRepository(ABC):
    """Instrument Master 경계. 영구 instrument_id 는 이 뒤에서만 발급된다."""

    @abstractmethod
    def ingest_instruments(
        self, records: list[InstrumentRecord], *, provider: str, as_of: datetime
    ) -> IngestResult:
        ...

    @abstractmethod
    def resolve_symbols(
        self, symbols: list[str], *, provider: str, market: str = MARKET_KRX
    ) -> dict[str, UUID]:
        """열려 있는(valid_to is null) 매핑만 돌려준다. 없는 symbol 은 key 가 없다."""

    @abstractmethod
    def close(self) -> None:
        ...


class SupabaseReferenceRepository(ReferenceRepository):
    """psycopg2 기반 Supabase 구현.

    DSN 은 .env 의 DATABASE_URL 을 그대로 libpq 에 넘긴다. 손으로 쪼개지 않는다 -
    비밀번호가 percent-encoding 돼 있어서 직접 파싱하면 디코딩을 빼먹는다(실제로 겪었다).
    """

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg2
        from psycopg2.extras import execute_values, register_uuid

        register_uuid()
        self._execute_values = execute_values
        # upsert_news_documents 가 채운다. 공급자가 정정한 기사의 external_id 다 -
        # 저장은 최초 관측본을 유지하므로(PIT) 정정 사실은 여기로만 드러난다.
        self.last_revisions: tuple[str, ...] = ()
        self._conn = psycopg2.connect(
            dsn or load_project_env()["DATABASE_URL"], connect_timeout=20
        )
        self._conn.autocommit = False

    def ingest_instruments(
        self, records: list[InstrumentRecord], *, provider: str, as_of: datetime
    ) -> IngestResult:
        attempted = len(records)

        # isin 없는 행은 적재하지 않는다. NULLS NOT DISTINCT 라 두 건 이상이면
        # unique 위반이고, 하나만 넣는 것도 임의 선택이라 옳지 않다.
        with_isin = [r for r in records if r.isin]
        skipped_no_isin = attempted - len(with_isin)

        # 같은 isin 이 두 번 오면 첫 건만 쓴다. 조용히 덮어쓰지 않고 센다.
        by_isin: dict[str, InstrumentRecord] = {}
        dup = 0
        for r in with_isin:
            if r.isin in by_isin:
                dup += 1
                continue
            by_isin[r.isin] = r
        rows = list(by_isin.values())

        before = self._count('reference.instruments')

        values = [
            (
                r.isin, r.display_name, r.market, r.venue, r.asset_class, r.instrument_type,
                r.currency, r.status, r.price_scale, r.quantity_scale, r.lot_size,
                json.dumps(r.metadata, ensure_ascii=False),
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            self._execute_values(
                cur,
                """
                insert into reference.instruments
                  (isin, display_name, market, venue, asset_class, instrument_type,
                   currency, status, price_scale, quantity_scale, lot_size, metadata)
                values %s
                on conflict (isin) do update set
                  display_name = excluded.display_name,
                  venue = excluded.venue,
                  instrument_type = excluded.instrument_type,
                  lot_size = excluded.lot_size,
                  metadata = excluded.metadata,
                  updated_at = now()
                """,
                values,
                page_size=500,
            )
            # isin -> instrument_id 를 다시 읽어 매핑을 만든다.
            cur.execute(
                "select isin, instrument_id from reference.instruments where isin = any(%s)",
                ([r.isin for r in rows],),
            )
            id_by_isin = {isin: iid for isin, iid in cur.fetchall()}
        after = self._count('reference.instruments')
        inserted = after - before
        updated = len(rows) - inserted

        symbols_inserted = self._ingest_symbols(rows, id_by_isin, provider=provider, as_of=as_of)
        self._conn.commit()

        return IngestResult(
            attempted=attempted,
            instruments_inserted=inserted,
            instruments_updated=updated,
            symbols_inserted=symbols_inserted,
            skipped_no_isin=skipped_no_isin,
            skipped_duplicate_isin=dup,
        )

    def _ingest_symbols(
        self,
        rows: list[InstrumentRecord],
        id_by_isin: dict[str, UUID],
        *,
        provider: str,
        as_of: datetime,
    ) -> int:
        """열려 있는 매핑이 이미 같은 instrument 를 가리키면 넣지 않는다.

        이렇게 해야 재실행이 멱등이다. valid_from 에 now() 를 넣고 매번 insert 하면
        실행마다 행이 쌓이고, 어느 것이 현재 매핑인지 알 수 없게 된다.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select symbol, instrument_id from reference.instrument_symbols
                where provider = %s and market = %s and valid_to is null
                """,
                (provider, MARKET_KRX),
            )
            open_map = {sym: iid for sym, iid in cur.fetchall()}

            pending = []
            for r in rows:
                iid = id_by_isin.get(r.isin)
                if iid is None or not r.provider_symbol:
                    continue
                if open_map.get(r.provider_symbol) == iid:
                    continue  # 이미 같은 매핑이 열려 있다
                pending.append((iid, provider, MARKET_KRX, r.provider_symbol, "TRADING", as_of, True))

            if not pending:
                return 0
            # fetch=True 없이 cur.fetchall() 을 하면 마지막 page 의 RETURNING 만 잡힌다.
            # 실측에서 4293건 삽입을 293건(= 4293 mod 500)으로 셌다.
            returned = self._execute_values(
                cur,
                """
                insert into reference.instrument_symbols
                  (instrument_id, provider, market, symbol, symbol_type, valid_from, is_primary)
                values %s
                on conflict (provider, market, symbol, valid_from) do nothing
                returning 1
                """,
                pending,
                page_size=500,
                fetch=True,
            )
            return len(returned)

    def sync_data_sources(self, specs=None) -> tuple[int, int, dict[str, UUID]]:
        """Source Registry 를 reference.data_sources 에 동기화한다. (신규, 갱신, 매핑).

        Registry 가 "Git 쪽 선언"이고 DB 가 운영 원장이라고 문서에 적어뒀는데, 실제로
        동기화하는 코드가 없었다. research.documents.source_id 가 NOT NULL FK 라
        이걸 먼저 채워야 문서를 적재할 수 있다.

        license/retention 의 최종 판단은 Data Steward 가 한다. 이 함수는 Registry 의
        선언을 옮기는 것이지 승인을 대신하지 않는다.

        ▶ status 를 한 방향으로만 움직인다 (2026-07-31)
          Registry 가 DISABLED 인데 DB 가 ACTIVE 로 남으면 **DB 만 보는 사람은 쓸 수
          있다고 오해한다.** 그래서 DISABLED 는 SUSPENDED 로 내린다. 반대로 올리지는
          않는다 - 재활성화는 Data Steward 의 판단이고, 코드가 자동으로 되돌리면
          사람이 내린 중단 결정을 덮어쓴다. 차단 방향으로만 움직이는 게 안전하다
          (개발 원칙 9).
        """
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
        from source_registry import SOURCES, SourceRegistry

        registry = SourceRegistry()
        rows = []
        for s in (specs if specs is not None else SOURCES):
            rows.append(
                (
                    s.source_id,
                    s.display_name,
                    # source_type 은 자유 텍스트다. Domain 을 그대로 쓰면 여러 개라
                    # 표현이 안 되므로 tier 와 함께 성격만 남긴다.
                    "VENDOR_API",
                    "research-department",
                    json.dumps(
                        {
                            "tier": s.tier.value,
                            "registry_status": registry.status(s.source_id).value,
                            "disabled_reason": s.disabled_reason,
                            "doc_ref": s.doc_ref,
                            "note": s.note,
                            "rate_limit_per_sec": float(s.rate_limit_per_sec)
                            if s.rate_limit_per_sec is not None
                            else None,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"declared_in": "collectors/source_registry.py"}, ensure_ascii=False),
                    [u.value for u in s.allowed_uses],
                    # 명시적으로 허용하지 않은 용도는 금지다. 두 목록을 함께 넣어
                    # DB 만 보는 사람도 경계를 알 수 있게 한다.
                    [u.value for u in UseScope if u not in s.allowed_uses],
                    "SUSPENDED" if s.disabled_reason else "ACTIVE",
                )
            )

        before = self._count("reference.data_sources")
        with self._conn.cursor() as cur:
            self._execute_values(
                cur,
                """
                insert into reference.data_sources
                  (source_code, name, source_type, owner, license_terms,
                   retention_policy, allowed_uses, prohibited_uses, status)
                values %s
                on conflict (source_code) do update set
                  name = excluded.name,
                  license_terms = excluded.license_terms,
                  allowed_uses = excluded.allowed_uses,
                  prohibited_uses = excluded.prohibited_uses,
                  -- 차단 방향으로만 움직인다. Registry 가 DISABLED 면 SUSPENDED 로
                  -- 내리고, 그 반대(SUSPENDED -> ACTIVE)는 하지 않는다 - 사람이
                  -- 내린 중단 결정을 코드가 자동으로 되돌리면 안 된다.
                  status = case when excluded.status = 'SUSPENDED' then 'SUSPENDED'
                                else reference.data_sources.status end,
                  updated_at = now()
                """,
                rows,
                page_size=200,
            )
            cur.execute(
                "select source_code, source_id from reference.data_sources where source_code = any(%s)",
                ([r[0] for r in rows],),
            )
            mapping = {code: sid for code, sid in cur.fetchall()}
        after = self._count("reference.data_sources")
        self._conn.commit()
        return after - before, len(rows) - (after - before), mapping

    def upsert_issuers(self, issuers: list[tuple[str, str]]) -> tuple[int, int, dict[str, UUID]]:
        """(corp_code, name) 목록을 reference.issuers 에 적재한다. (신규, 갱신, 매핑).

        Open DART 공시검색은 corp_code 와 corp_name 만 준다. industry_code, fiscal_month 는
        기업개황 API(2019002)가 회사별 1회 호출이라 여기서 채우지 않는다 - 없는 값을
        기본값으로 채우면 그게 사실처럼 굳는다. NULL 로 두고 후속에서 보강한다.

        legal_name 과 display_name 이 둘 다 NOT NULL 인데 공시에는 하나뿐이다.
        같은 값을 넣고 metadata 에 근거를 남긴다 - 법인명과 표시명이 다를 수 있으므로
        기업개황 수집 시 legal_name 을 갱신해야 한다.
        """
        seen: dict[str, str] = {}
        for code, name in issuers:
            code, name = (code or "").strip(), (name or "").strip()
            if not code or not name:
                continue
            seen.setdefault(code, name)
        if not seen:
            return 0, 0, {}

        rows = [
            (
                code, name, name, "KR",
                json.dumps({"source": "opendart:2019001", "name_source": "corp_name",
                            "legal_name_verified": False}, ensure_ascii=False),
            )
            for code, name in seen.items()
        ]
        before = self._count("reference.issuers")
        with self._conn.cursor() as cur:
            self._execute_values(
                cur,
                """
                insert into reference.issuers
                  (corp_code, legal_name, display_name, country_code, metadata)
                values %s
                on conflict (corp_code) do update set
                  display_name = excluded.display_name,
                  updated_at = now()
                """,
                rows,
                page_size=500,
            )
            cur.execute(
                "select corp_code, issuer_id from reference.issuers where corp_code = any(%s)",
                (list(seen),),
            )
            mapping = {c: i for c, i in cur.fetchall()}
        after = self._count("reference.issuers")
        self._conn.commit()
        return after - before, len(rows) - (after - before), mapping

    def upsert_documents(self, records: list, *, source_id: UUID,
                         issuer_by_corp: dict[str, UUID]) -> tuple[int, int, int]:
        """research.documents 에 적재한다. (신규, 갱신, issuer 미연결).

        멱등 키는 unique nulls not distinct (source_id, external_id) 다. external_id 가
        rcept_no 이므로 같은 공시를 다시 받아도 행이 늘지 않는다.

        정정공시는 원본을 덮어쓰지 않는다 - rcept_no 가 다르므로 별개 문서로 들어간다.
        원본과의 관계 연결(document_relations)은 rcept_no 만으로 알 수 없어 후속이다.

        **research.documents 에 metadata Column 이 없다.** DocumentRecord.metadata 의
        PIT 정밀도 같은 정보는 문서 단위가 아니라 Source 속성이므로
        reference.data_sources.license_terms 에 남긴다(sync_data_sources 참고).
        document_versions 는 만들지 않는다 - object_path/content_hash 가 NOT NULL 인데
        원문 파일(API 2019003)을 받지 않았고, 없는 경로를 조작해 넣지 않는다.
        """
        rows = []
        unlinked = 0
        for r in records:
            iid = issuer_by_corp.get(r.corp_code)
            if iid is None:
                unlinked += 1
            rows.append(
                (
                    source_id, r.external_id, r.document_type, r.canonical_url, r.title,
                    "ko", iid, r.published_at, r.observed_at, r.status,
                )
            )
        if not rows:
            return 0, 0, 0

        before = self._count("research.documents")
        with self._conn.cursor() as cur:
            self._execute_values(
                cur,
                """
                insert into research.documents
                  (source_id, external_id, document_type, canonical_url, title, language,
                   issuer_id, published_at, observed_at, status)
                values %s
                on conflict (source_id, external_id) do update set
                  title = excluded.title,
                  status = excluded.status,
                  issuer_id = coalesce(excluded.issuer_id, research.documents.issuer_id),
                  updated_at = now()
                """,
                rows,
                page_size=500,
            )
        after = self._count("research.documents")
        self._conn.commit()
        return after - before, len(rows) - (after - before), unlinked

    def upsert_news_documents(
        self, records: list, *, source_id: UUID, issuer_by_external_id: dict | None = None
    ) -> tuple[int, int, dict[str, UUID]]:
        """뉴스 기사를 research.documents 에 적재한다. (신규, 갱신, external_id -> document_id).

        upsert_documents 와 나눈 이유 - 그쪽은 DART 전용이라 language 를 'ko' 로
        고정하고 corp_code 로 issuer 를 붙인다. 뉴스는 언어가 Source 마다 다르고
        발행사가 issuer 가 아니다.

        **issuer_id 는 호출자가 판정해서 넘긴 것만 채운다.** 기사에 실린 심볼은 그 자체로
        발행기업 식별자가 아니다. 한 기사가 여러 기업을 언급할 수 있는데 issuer_id 는
        단수이므로, 호출자가 "이 기사는 이 발행사 전용" 이라고 판정한 것만 넘긴다.
        나머지 언급 관계는 link_documents_to_instruments 로 간다.

        본문(content)은 여기 넣지 않는다. document_versions/evidence_chunks 로 가야
        하는데 그건 Source 의 allowed_uses 에 FULLTEXT_STORE 가 있어야 한다 -
        호출자가 Registry.check_use 로 먼저 확인한다.
        """
        by_ext = issuer_by_external_id or {}
        rows = [
            (
                source_id, r.external_id, r.document_type, r.canonical_url, r.title,
                r.language, by_ext.get(r.external_id), r.published_at, r.observed_at, r.status,
            )
            for r in records
        ]
        if not rows:
            return 0, 0, {}

        # 이미 있는 문서의 제목을 미리 읽는다. 공급자가 기사를 정정하면 external_id 는
        # 그대로인 채 headline 만 바뀌는데, 그걸 그냥 덮어쓰면 정정 전 시점의
        # Backtest 가 정정 후 문장을 보게 된다(개발 원칙 5).
        exts = [r.external_id for r in records]
        with self._conn.cursor() as cur:
            cur.execute(
                "select external_id, title, document_id from research.documents "
                "where source_id = %s and external_id = any(%s)",
                (source_id, exts),
            )
            stored = {e: (t, d) for e, t, d in cur.fetchall()}
        stored_title = {e: t for e, (t, _d) in stored.items()}
        changed = [r for r in records
                   if r.external_id in stored_title
                   and stored_title[r.external_id] != r.title]
        self.last_revisions = tuple(r.external_id for r in changed)

        # ▶ 탐지한 정정을 **영속한다** (2026-08-03)
        #   예전에는 위 튜플(메모리)에만 담고 프로세스가 끝나면 사라졌다.
        #   저장본은 PIT 규율상 최초 관측 문장을 유지하므로 정정 사실은 그
        #   튜플이 유일한 흔적이었는데 그것마저 휘발됐다. 그러면 로드맵 6.2 의
        #   중단 조건("수정 이력 없는 문서는 거래 근거로 쓰지 않는다")을 판정할
        #   데이터가 없다. 원본은 그대로 두고 변경 사실만 append 한다.
        #   기록 실패가 수집을 죽이지 않는다 - 이력은 보조 기록이다.
        if changed:
            try:
                rows = [(stored[r.external_id][1], "title",
                         stored_title[r.external_id], r.title, "news_pipeline")
                        for r in changed]
                with self._conn.cursor() as cur:
                    self._execute_values(cur, """
                        insert into research.document_revisions
                          (document_id, field, old_value, new_value, detected_by)
                        values %s on conflict do nothing
                    """, rows, page_size=200)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ document_revisions 기록 실패(수집은 계속): "
                      f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)

        before = self._count("research.documents")
        with self._conn.cursor() as cur:
            returned = self._execute_values(
                cur,
                """
                insert into research.documents
                  (source_id, external_id, document_type, canonical_url, title, language,
                   issuer_id, published_at, observed_at, status)
                values %s
                on conflict (source_id, external_id) do update set
                  -- **title 과 published_at 을 덮어쓰지 않는다.** 최초 관측 문장을
                  -- 유지해야 observed_at <= decision_time 재현이 성립한다. 정정본을
                  -- 반영하려면 document_versions 가 필요한데 object_path/content_hash
                  -- 가 NOT NULL 이라 제목만 있는 뉴스로는 만들 수 없다. 그래서 정정은
                  -- 저장하는 대신 last_revisions 로 **드러낸다**.
                  --
                  -- canonical_url 은 coalesce 다. normalize() 가 url 없음을 허용하므로
                  -- 무조건 대입하면 있던 URL 이 NULL 로 지워진다.
                  canonical_url = coalesce(excluded.canonical_url,
                                           research.documents.canonical_url),
                  status = excluded.status,
                  -- 한 번 붙은 issuer 를 NULL 로 되돌리지 않는다. 판정 규칙이 좋아져서
                  -- 새로 붙는 것은 허용하되, 못 붙였다고 지우지는 않는다.
                  issuer_id = coalesce(excluded.issuer_id, research.documents.issuer_id),
                  updated_at = now()
                returning external_id, document_id
                """,
                rows,
                page_size=500,
                fetch=True,
            )
        after = self._count("research.documents")
        self._conn.commit()
        # fetch=True 가 필수다. page_size 로 쪼개진 문장의 마지막 것만 잡히면 앞쪽
        # document_id 를 통째로 잃는다(market_repository 와 같은 함정).
        id_by_ext = {ext: did for ext, did in returned}
        return after - before, len(rows) - (after - before), id_by_ext

    def issuers_needing_profile(self, *, limit: int | None = None) -> list[tuple[UUID, str]]:
        """기업개황 보강이 필요한 (issuer_id, corp_code).

        industry_code 나 fiscal_month 가 비어 있는 발행사만이며, **instrument 에
        연결된 발행사를 먼저** 준다 - 기업개황이 회사별 1회 호출이라 호출 예산을
        Universe 에 실제로 쓰이는 회사부터 쓴다.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select iss.issuer_id, iss.corp_code,
                       count(i.instrument_id) as linked
                from reference.issuers iss
                left join reference.instruments i on i.issuer_id = iss.issuer_id
                where iss.corp_code is not null
                  and (iss.industry_code is null or iss.fiscal_month is null)
                group by iss.issuer_id, iss.corp_code
                order by linked desc, iss.corp_code
                limit %s
                """,
                (limit,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def apply_company_profiles(self, profiles: list) -> tuple[int, int]:
        """기업개황(2019002) 결과를 issuers 에 반영한다. (실변경, 시도).

        - legal_name 은 기업개황의 corp_name(정식명칭)으로 **검증하며 갱신** 한다.
          upsert_issuers 가 공시의 corp_name 을 임시로 넣고 legal_name_verified=false
          로 표시해 뒀는데, 이 API 의 corp_name 이 바로 그 검증 근거다.
        - display_name 은 건드리지 않는다. 표시명은 종목명 계열이라 층위가 다르다.
        - **실변경만 센다.** IS DISTINCT FROM 가드가 없으면 재실행마다 전부
          갱신으로 집계되어 멱등 여부를 확인할 수 없다.
        """
        if not profiles:
            return 0, 0
        rows = [
            (
                p.corp_code, p.legal_name, p.industry_code, p.fiscal_month,
                json.dumps(p.metadata_patch(), ensure_ascii=False),
            )
            for p in profiles
        ]
        with self._conn.cursor() as cur:
            returned = self._execute_values(
                cur,
                """
                update reference.issuers as iss
                set legal_name = coalesce(nullif(v.legal_name, ''), iss.legal_name),
                    industry_code = coalesce(v.industry_code, iss.industry_code),
                    fiscal_month = coalesce(v.fiscal_month::smallint, iss.fiscal_month),
                    metadata = iss.metadata || v.metadata::jsonb,
                    updated_at = now()
                from (values %s) as v(corp_code, legal_name, industry_code, fiscal_month, metadata)
                where iss.corp_code = v.corp_code
                  and (iss.legal_name is distinct from coalesce(nullif(v.legal_name, ''), iss.legal_name)
                       or iss.industry_code is distinct from coalesce(v.industry_code, iss.industry_code)
                       or iss.fiscal_month is distinct from coalesce(v.fiscal_month::smallint, iss.fiscal_month)
                       or not iss.metadata @> v.metadata::jsonb)
                returning iss.corp_code
                """,
                rows,
                page_size=200,
                fetch=True,
            )
        self._conn.commit()
        return len(returned), len(rows)

    def issuer_for_symbol(
        self, symbol: str, *, provider: str = PROVIDER_LS, market: str = MARKET_KRX
    ) -> UUID | None:
        """종목코드 -> issuer_id. 연결이 없으면 None 이고 만들지 않는다."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select i.issuer_id
                from reference.instrument_symbols s
                join reference.instruments i using (instrument_id)
                where s.symbol = %s and s.provider = %s and i.market = %s
                  and i.issuer_id is not null
                limit 1
                """,
                (symbol, provider, market),
            )
            r = cur.fetchone()
            return None if r is None else r[0]

    def link_documents_to_instruments(self, links: list[tuple]) -> int:
        """research.document_instruments 에 언급 관계를 넣는다.

        links 는 (document_id, instrument_id, relation_type, confidence) 다.
        instrument_id 가 reference.instruments FK 이므로 **우리 Instrument Master 에
        있는 종목만** 넣을 수 있다. 해외 심볼은 여기 들어오지 못한다 - 호출자가
        걸러서 넘긴다.
        """
        if not links:
            return 0
        before = self._count("research.document_instruments")
        with self._conn.cursor() as cur:
            self._execute_values(
                cur,
                """
                insert into research.document_instruments
                  (document_id, instrument_id, relation_type, confidence, extraction_version)
                values %s
                on conflict (document_id, instrument_id, relation_type) do update set
                  confidence = excluded.confidence,
                  extraction_version = excluded.extraction_version
                """,
                [(d, i, rt, c, NEWS_LINK_VERSION) for d, i, rt, c in links],
                page_size=500,
            )
        after = self._count("research.document_instruments")
        self._conn.commit()
        return after - before

    def upsert_financial_facts(self, facts: list, *, source_id: UUID) -> tuple[int, int, int]:
        """research.financial_facts 에 적재한다. (신규, 갱신, issuer 미연결).

        멱등 키는 unique (issuer_id, source_id, account_code, period_end, period_type,
        consolidation_scope, revision) 이다.

        ▶ 정정 재무제표는 **원본을 덮지 않고 새 revision 으로 쌓는다** (2026-08-02)
          예전에는 revision 을 1 로 고정하고 conflict 시 value·observed_at 까지
          덮어썼다. 그래서 DART 정정본이 들어오면 **최초 보고치와 그 관측
          시각이 사라졌다.** 그러면 정정 이전 시점의 Backtest·Packet 재현이
          미래 수치를 보게 된다 - DATA_GOVERNANCE_GUIDE 9절이 직접 금지하는
          상황이고, 우리가 가진 유일하게 강한 PIT 축(observed_at <= as_of)이
          재무에서만 뚫려 있었다.

          개정 순서는 **rcept_no 오름차순**이다(앞 8자리가 접수일이라 나중
          접수가 큰 수 - opendart_financial.published_at_from_rcept 와 같은
          근거). 같은 rcept_no 를 다시 수집하면 같은 revision 이라 멱등이고,
          새 rcept_no 는 max+1 로 append 된다.

          conflict 시에도 **value·observed_at·published_at 을 덮지 않는다** -
          같은 revision 이면 같은 문서에서 온 같은 값이므로 덮을 이유가 없고,
          덮을 이유가 있는 상황이라면 그건 새 revision 이어야 한다.

        issuer 가 없는 행은 적재하지 않는다 - issuer_id 가 NOT NULL FK 다.
        """
        with self._conn.cursor() as cur:
            codes = sorted({f.corp_code for f in facts})
            cur.execute(
                "select corp_code, issuer_id from reference.issuers where corp_code = any(%s)",
                (codes,),
            )
            issuer_by_corp = {c: i for c, i in cur.fetchall()}

            # rcept_no 로 원본 문서를 연결한다. 수집 범위 밖이면 NULL 이다.
            rcepts = sorted({f.rcept_no for f in facts if f.rcept_no})
            doc_by_rcept: dict[str, UUID] = {}
            if rcepts:
                cur.execute(
                    "select external_id, document_id from research.documents "
                    "where source_id = %s and external_id = any(%s)",
                    (source_id, rcepts),
                )
                doc_by_rcept = {e: d for e, d in cur.fetchall()}

            # 이미 있는 (키, rcept_no) -> revision 을 읽어 개정 순서를 잇는다.
            # metadata.rcept_no 로 대조한다 - 전용 컬럼이 없어서다(백로그).
            known: dict = {}
            if codes:
                cur.execute("""
                    select iss.corp_code, ff.account_code, ff.period_end::text,
                           ff.period_type, ff.consolidation_scope,
                           ff.metadata->>'rcept_no', ff.revision
                    from research.financial_facts ff
                    join reference.issuers iss using (issuer_id)
                    where ff.source_id = %s and iss.corp_code = any(%s)
                """, (source_id, codes))
                for corp, acct, pe, pt, scope, rc, rev in cur.fetchall():
                    if not rc:
                        continue
                    known.setdefault((corp, acct, pe, pt, scope), {})[rc] = int(rev)

            revisions, fresh_revisions = assign_revisions(facts, known)

            rows = []
            unlinked = 0
            for f, rev in zip(facts, revisions):
                iid = issuer_by_corp.get(f.corp_code)
                if iid is None:
                    unlinked += 1
                    continue
                meta = dict(f.metadata or {})
                # rcept_no 를 metadata 에 남긴다 - 다음 수집이 개정 순서를 이으려면
                # '이 행이 어느 접수번호에서 왔는지'가 있어야 한다
                if f.rcept_no:
                    meta.setdefault("rcept_no", f.rcept_no)
                rows.append(
                    (
                        iid, source_id, f.account_code, f.account_name,
                        None, f.period_end, f.period_type, f.consolidation_scope,
                        f.value, f.unit, f.currency, f.published_at, f.observed_at, rev,
                        doc_by_rcept.get(f.rcept_no),
                        json.dumps(meta, ensure_ascii=False),
                    )
                )
            if not rows:
                return 0, 0, unlinked

            before = self._count("research.financial_facts")
            self._execute_values(
                cur,
                """
                insert into research.financial_facts
                  (issuer_id, source_id, account_code, account_name, period_start,
                   period_end, period_type, consolidation_scope, value, unit, currency,
                   published_at, observed_at, revision, source_document_id, metadata)
                values %s
                on conflict (issuer_id, source_id, account_code, period_end,
                             period_type, consolidation_scope, revision) do update set
                  -- **value·observed_at·published_at 을 덮지 않는다.**
                  -- 같은 revision 이면 같은 접수번호에서 온 같은 값이라 덮을
                  -- 이유가 없고, 값이 달라졌다면 그건 정정이므로 새 revision 으로
                  -- 가야 한다(assign_revisions). 관측 시각을 덮으면 그 순간
                  -- observed_at <= as_of 재현이 미래를 보게 된다.
                  account_name = coalesce(research.financial_facts.account_name,
                                          excluded.account_name),
                  source_document_id = coalesce(excluded.source_document_id,
                                               research.financial_facts.source_document_id),
                  metadata = research.financial_facts.metadata || excluded.metadata
                """,
                rows,
                page_size=500,
            )
            after = self._count("research.financial_facts")
        self._conn.commit()
        return after - before, len(rows) - (after - before), unlinked

    def upsert_macro_series(self, specs: list, *, source_ids: dict) -> tuple[int, int, dict]:
        """research.macro_series 적재. 멱등 키는 unique (source_id, external_series_code)."""
        rows = []
        for s in specs:
            sid = source_ids.get(s.source_code)
            if sid is None:
                continue
            rows.append((
                sid, s.external_series_code, s.name, s.unit, s.frequency,
                s.seasonal_adjustment, s.country_code,
                json.dumps({"declared_in": "collectors/macro_collector.py",
                            "item_name": s.item_name}, ensure_ascii=False),
            ))
        if not rows:
            return 0, 0, {}
        before = self._count("research.macro_series")
        with self._conn.cursor() as cur:
            self._execute_values(cur, """
                insert into research.macro_series
                  (source_id, external_series_code, name, unit, frequency,
                   seasonal_adjustment, country_code, definition)
                values %s
                on conflict (source_id, external_series_code) do update set
                  name = excluded.name, unit = excluded.unit,
                  definition = excluded.definition
                """, rows, page_size=200)
            cur.execute(
                "select external_series_code, series_id from research.macro_series "
                "where external_series_code = any(%s)",
                ([r[1] for r in rows],),
            )
            mapping = {c: i for c, i in cur.fetchall()}
        after = self._count("research.macro_series")
        self._conn.commit()
        return after - before, len(rows) - (after - before), mapping

    def upsert_macro_observations(self, obs: list, *, series_ids: dict) -> tuple[int, int]:
        """research.macro_observations 적재.

        멱등 키는 unique (series_id, period, vintage_date, revision) 다. vintage_date 가
        키에 있으므로 **개정본이 새 행으로 쌓인다** - 덮어쓰지 않는 것이 PIT 재현의 전제다
        (가이드 5.2 - Revision Append). revision 은 1 로 고정한다. 같은 vintage 에 값이
        두 번 오는 경우는 수집 단계에서 이미 걸렀다.
        """
        rows = []
        for o in obs:
            sid = series_ids.get(o.external_series_code)
            if sid is None:
                continue
            rows.append((
                sid, o.period, o.value, o.published_at, o.observed_at, o.vintage_date, 1,
                json.dumps(o.metadata, ensure_ascii=False),
            ))
        if not rows:
            return 0, 0
        before = self._count("research.macro_observations")
        with self._conn.cursor() as cur:
            self._execute_values(cur, """
                insert into research.macro_observations
                  (series_id, period, value, published_at, observed_at, vintage_date,
                   revision, metadata)
                values %s
                on conflict (series_id, period, vintage_date, revision) do update set
                  value = excluded.value, observed_at = excluded.observed_at,
                  metadata = excluded.metadata
                """, rows, page_size=500)
            after = self._count("research.macro_observations")
        self._conn.commit()
        return after - before, len(rows) - (after - before)

    def link_issuers_to_instruments(self, pairs: list[tuple[str, str]], *,
                                    provider: str = PROVIDER_LS) -> tuple[int, int]:
        """(corp_code, stock_code) 쌍으로 instruments.issuer_id 를 연결한다. (연결, 미해결).

        왜 필요한가 - LS 종목 마스터는 corp_code 를 주지 않고 DART 는 stock_code 를 항상
        주지 않는다(주요사항보고서 API 는 corp_code 만 준다). 둘을 잇는 유일한 접점이
        공시검색 응답의 (corp_code, stock_code) 쌍이다. 이걸 연결하지 않으면
        corporate_actions.instrument_id(NOT NULL FK)를 채울 수 없다.

        완전한 매핑은 고유번호 API(2019018)가 ZIP 으로 준다. 지금은 공시검색에서 관측된
        쌍만 쓰므로 **공시를 낸 회사만 연결된다** - 미연결 수를 함께 돌려주는 이유다.
        """
        seen: dict[str, str] = {}
        for corp, stock in pairs:
            corp, stock = (corp or "").strip(), (stock or "").strip()
            if corp and stock:
                seen.setdefault(stock, corp)
        if not seen:
            return 0, 0

        with self._conn.cursor() as cur:
            cur.execute(
                "select symbol, instrument_id from reference.instrument_symbols "
                "where provider = %s and valid_to is null and symbol = any(%s)",
                (provider, list(seen)),
            )
            inst_by_symbol = {s: i for s, i in cur.fetchall()}
            cur.execute(
                "select corp_code, issuer_id from reference.issuers where corp_code = any(%s)",
                (sorted(set(seen.values())),),
            )
            issuer_by_corp = {c: i for c, i in cur.fetchall()}

            rows = []
            unresolved = 0
            for stock, corp in seen.items():
                iid, isid = inst_by_symbol.get(stock), issuer_by_corp.get(corp)
                if iid is None or isid is None:
                    unresolved += 1
                    continue
                rows.append((iid, isid))
            if not rows:
                return 0, unresolved

            self._execute_values(
                cur,
                """
                update reference.instruments as i
                set issuer_id = v.issuer_id::uuid, updated_at = now()
                from (values %s) as v(instrument_id, issuer_id)
                where i.instrument_id = v.instrument_id::uuid
                  and (i.issuer_id is null or i.issuer_id <> v.issuer_id::uuid)
                """,
                [(str(a), str(b)) for a, b in rows],
                page_size=500,
            )
            linked = cur.rowcount
        self._conn.commit()
        return linked, unresolved

    def resolve_instrument_by_corp(self, corp_codes: list[str]) -> dict[str, UUID]:
        """corp_code -> instrument_id. issuer_id 연결이 선행돼야 한다.

        한 발행사가 여러 종목(보통주·우선주)을 가질 수 있다. 그 경우 어느 종목의
        Corporate Action 인지 API 가 알려주지 않으므로 **보통주 하나만 고를 근거가 없다.**
        지금은 종목이 하나인 발행사만 돌려주고 여러 개면 제외한다 - 임의로 고르면
        잘못된 종목에 배당·분할이 붙는다.
        """
        if not corp_codes:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select s.corp_code, min(i.instrument_id::text), count(*)
                from reference.issuers s
                join reference.instruments i on i.issuer_id = s.issuer_id
                where s.corp_code = any(%s) and i.status = 'ACTIVE'
                  and i.instrument_type = 'STOCK'
                group by s.corp_code
                """,
                (corp_codes,),
            )
            out = {}
            for corp, iid, n in cur.fetchall():
                if n == 1:
                    out[corp] = UUID(iid)
            return out

    def upsert_corporate_actions(self, actions: list, *, source_id: UUID) -> tuple[int, int, int]:
        """reference.corporate_actions 적재. (신규, 갱신, instrument 미매핑).

        멱등 키는 unique nulls not distinct (source_id, external_id, revision) 다.
        external_id 가 `{rcept_no}:{endpoint}` 이므로 같은 공시를 다시 받아도 늘지 않는다.

        instrument_id 가 NOT NULL FK 인데 주요사항보고서 API 는 corp_code 만 준다.
        resolve_instrument_by_corp 로 잇고, 여러 종목을 가진 발행사는 제외한다 -
        어느 종목의 Action 인지 알 수 없는 상태로 임의 배정하면 가격 조정이 틀어진다.
        """
        codes = sorted({a.corp_code for a in actions})
        inst_by_corp = self.resolve_instrument_by_corp(codes)

        rows = []
        unmapped = 0
        for a in actions:
            iid = inst_by_corp.get(a.corp_code)
            if iid is None:
                unmapped += 1
                continue
            rows.append((
                iid, source_id, a.external_id, a.action_type, a.announced_at,
                a.ex_date, None, a.effective_at, a.ratio, a.cash_amount, a.currency,
                1, a.status, json.dumps(a.payload, ensure_ascii=False, default=str),
                a.observed_at,
            ))
        if not rows:
            return 0, 0, unmapped

        before = self._count("reference.corporate_actions")
        with self._conn.cursor() as cur:
            self._execute_values(cur, """
                insert into reference.corporate_actions
                  (instrument_id, source_id, external_id, action_type, announced_at,
                   ex_date, record_date, effective_at, ratio, cash_amount, currency,
                   revision, status, payload, observed_at)
                values %s
                on conflict (source_id, external_id, revision) do update set
                  action_type = excluded.action_type,
                  announced_at = excluded.announced_at,
                  effective_at = excluded.effective_at,
                  cash_amount = excluded.cash_amount,
                  status = excluded.status,
                  payload = excluded.payload,
                  observed_at = excluded.observed_at
                """, rows, page_size=300)
            after = self._count("reference.corporate_actions")
        self._conn.commit()
        return after - before, len(rows) - (after - before), unmapped

    def _count(self, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"select count(*) from {table}")
            return int(cur.fetchone()[0])

    def upsert_calendar(self, draft) -> tuple[UUID, int, bool]:
        """Calendar Draft 를 적재한다. (calendar_version_id, session 수, 신규여부).

        같은 내용이면 새 Version 을 만들지 않는다 - unique(market, content_hash) 가
        그걸 표현하도록 설계돼 있다. 내용이 같은데 version 만 올리면 어느 것이 진짜
        변경인지 알 수 없어진다.

        draft 는 calendar_collector.CalendarDraft 다. 본부 안이라 타입 힌트를 생략하고
        Duck Typing 으로 받는다 - collectors 를 repository 가 import 하면 방향이 역전된다.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "select calendar_version_id from reference.market_calendar_versions "
                "where market = %s and content_hash = %s",
                (draft.market, draft.content_hash),
            )
            hit = cur.fetchone()
            if hit:
                return hit[0], 0, False

            cur.execute(
                "select coalesce(max(version), 0) + 1 from reference.market_calendar_versions "
                "where market = %s",
                (draft.market,),
            )
            version = int(cur.fetchone()[0])

            cur.execute(
                """
                insert into reference.market_calendar_versions
                  (market, version, effective_from, effective_to, content_hash, published_at)
                values (%s, %s, %s, %s, %s, now())
                returning calendar_version_id
                """,
                (
                    draft.market, version, draft.effective_from, draft.effective_to,
                    draft.content_hash,
                ),
            )
            version_id = cur.fetchone()[0]

            rows = [
                (
                    version_id, draft.market, s.trade_date, s.session_type,
                    s.opens_at, s.closes_at, s.is_trading_day,
                    json.dumps(s.metadata, ensure_ascii=False),
                )
                for s in draft.sessions
            ]
            self._execute_values(
                cur,
                """
                insert into reference.market_sessions
                  (calendar_version_id, market, trade_date, session_type,
                   opens_at, closes_at, is_trading_day, metadata)
                values %s
                on conflict (calendar_version_id, market, trade_date, session_type) do nothing
                """,
                rows,
                page_size=500,
            )
        self._conn.commit()
        return version_id, len(rows), True

    def market_session(
        self, trade_date: date, *, market: str = MARKET_KRX, session_type: str = "REGULAR"
    ) -> tuple[bool, datetime | None, datetime | None] | None:
        """최신 Version 에서 그 날의 (거래일 여부, 개장, 폐장).

        Calendar 에 그 날짜가 아예 없으면 None 이다. **None 을 '비거래일'로 바꾸지
        않는다** - Calendar 가 아직 그 날까지 안 채워진 것과 실제 휴장은 다른 사실이고,
        섞으면 수집기가 조용히 아무 것도 안 하게 된다(개발 원칙 9).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select s.is_trading_day, s.opens_at, s.closes_at
                from reference.market_sessions s
                where s.calendar_version_id = (
                    select calendar_version_id from reference.market_calendar_versions
                    where market = %s order by version desc limit 1
                )
                  and s.market = %s and s.trade_date = %s and s.session_type = %s
                """,
                (market, market, trade_date, session_type),
            )
            r = cur.fetchone()
            return None if r is None else (bool(r[0]), r[1], r[2])

    def recent_trading_sessions(
        self,
        *,
        market: str = MARKET_KRX,
        session_type: str = "REGULAR",
        limit: int = 20,
        through: date | None = None,
    ) -> list[tuple[date, datetime | None, datetime | None]]:
        """최신 Version 의 거래일 세션을 최신순으로. 개장 시각 일관성 확인에도 쓴다.

        **오늘(KST)까지만 돌려준다.** 선언 Calendar(calendar_declared)가 미래
        거래일을 갖게 되면서 이 가드가 필요해졌다 - 호출부(직전 세션 탐색,
        make_trading_day_check)는 전부 '이미 지난 세션'을 전제하므로 미래 날짜가
        섞이면 PRIOR_CLOSE 가 미래를 집는다. 미래 조회는 market_session(날짜)로 한다.
        """
        if through is None:
            through = datetime.now(timezone(timedelta(hours=9))).date()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select s.trade_date, s.opens_at, s.closes_at
                from reference.market_sessions s
                where s.calendar_version_id = (
                    select calendar_version_id from reference.market_calendar_versions
                    where market = %s order by version desc limit 1
                )
                  and s.market = %s and s.session_type = %s and s.is_trading_day
                  and s.trade_date <= %s
                order by s.trade_date desc
                limit %s
                """,
                (market, market, session_type, through, limit),
            )
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    def listed_counts(self, *, market: str = MARKET_KRX) -> dict[str, int]:
        """venue 별 상장 보통주 수. Breadth 합계가 그럴듯한지 보는 교차검증용이다.

        ETF/ETN 은 업종 등락종목수에 안 들어가므로 instrument_type='STOCK' 만 센다.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select venue, count(*) from reference.instruments
                where market = %s and instrument_type = 'STOCK' and status = 'ACTIVE'
                group by venue
                """,
                (market,),
            )
            return {str(v): int(n) for v, n in cur.fetchall()}

    def trading_days(self, market: str = MARKET_KRX) -> tuple[int, int]:
        """최신 Version 의 (거래일, 전체일). Calendar 적재 확인용이다."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select count(*) filter (where s.is_trading_day), count(*)
                from reference.market_sessions s
                where s.calendar_version_id = (
                    select calendar_version_id from reference.market_calendar_versions
                    where market = %s order by version desc limit 1
                )
                """,
                (market,),
            )
            r = cur.fetchone()
            return (int(r[0] or 0), int(r[1] or 0))

    def resolve_symbols(
        self, symbols: list[str], *, provider: str = PROVIDER_LS, market: str = MARKET_KRX
    ) -> dict[str, UUID]:
        if not symbols:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select symbol, instrument_id from reference.instrument_symbols
                where provider = %s and market = %s and symbol = any(%s) and valid_to is null
                """,
                (provider, market, symbols),
            )
            return {sym: iid for sym, iid in cur.fetchall()}

    def universe_members(self, *, provider: str = PROVIDER_LS) -> list[UniverseMember]:
        """적재된 Master 에서 구독 계획 입력을 만든다. instrument_id 는 DB 발급본이다."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select s.symbol, i.instrument_id, i.venue, i.instrument_type
                from reference.instrument_symbols s
                join reference.instruments i on i.instrument_id = s.instrument_id
                where s.provider = %s and s.valid_to is null and i.status = 'ACTIVE'
                  and i.asset_class = 'EQUITY'
                order by s.symbol
                """,
                (provider,),
            )
            out = []
            for symbol, iid, venue, itype in cur.fetchall():
                try:
                    v = Venue(venue)
                except ValueError:
                    continue  # 알 수 없는 venue 는 추정하지 않고 건너뛴다
                out.append(
                    UniverseMember(
                        instrument_id=iid,
                        provider_symbol=symbol,
                        venue=v,
                        asset_class=AssetClass.EQUITY,
                    )
                )
            return out

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 연결 없음
# ---------------------------------------------------------------------------

def _row(code: str, isin: str, venue: Venue, *, etf=False, etn=False, spac=False, unit="1"):
    return StockMasterRow(
        shcode=code, expcode=isin, hname=f"종목{code}", venue=venue,
        is_etf=etf, is_etn=etn, is_spac=spac, security_group="01",
        order_unit=unit, prev_close="1000", upper_limit="1300", lower_limit="700",
    )


def _check_mapping():
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    r = master_row_to_record(_row("005930", "KR7005930003", Venue.KOSPI), as_of=now)
    assert r.market == "KRX" and r.venue == "KOSPI"
    assert r.asset_class == "EQUITY" and r.instrument_type == "STOCK"
    assert r.currency == "KRW" and r.status == "ACTIVE"
    assert r.price_scale == 0 and r.quantity_scale == 0
    assert r.lot_size == Decimal("1")
    assert r.isin == "KR7005930003" and r.provider_symbol == "005930"
    assert r.metadata["source"] == "ls:t8436" and r.metadata["shcode"] == "005930"

    assert master_row_to_record(_row("069500", "KR7069500007", Venue.KOSPI, etf=True), as_of=now).instrument_type == "ETF"
    assert master_row_to_record(_row("500001", "KR7500001000", Venue.KOSPI, etn=True), as_of=now).instrument_type == "ETN"
    assert master_row_to_record(_row("123456", "KR7123456789", Venue.KOSDAQ, spac=True), as_of=now).metadata["is_spac"] is True

    # 주문 단위가 10 인 종목
    assert master_row_to_record(_row("000010", "KR7000010000", Venue.KOSPI, unit="10"), as_of=now).lot_size == Decimal("10")

    # tick_size 는 넣지 않는다 - 추정값이 주문 검증을 깨뜨린다
    assert not hasattr(r, "tick_size")
    print("  t8436 -> instruments 매핑   OK")


def _check_numeric_parsing():
    assert _to_decimal("1") == Decimal("1")
    assert _to_decimal("") == Decimal("1")
    assert _to_decimal("10") == Decimal("10")
    # 0 이나 음수는 lot_size CHECK(> 0) 을 위반하므로 기본값으로 올린다
    assert _to_decimal("0") == Decimal("1")
    try:
        _to_decimal("abc")
        raise AssertionError("숫자 아닌 값이 통과했다")
    except ValueError:
        pass
    print("  수량 단위 파싱              OK")


class _F:
    """assign_revisions 점검용 최소 fact - 실제 FinancialFact 의 키 필드만."""

    def __init__(self, corp, acct, pe, rcept, pt="ANNUAL", scope="CONSOLIDATED"):
        self.corp_code, self.account_code, self.period_end = corp, acct, pe
        self.period_type, self.consolidation_scope = pt, scope
        self.rcept_no = rcept


def _check_revision_assignment():
    """정정본이 원본을 덮지 않고 새 revision 으로 쌓이는가.

    2026-08-02 이전에는 revision 이 1 로 고정이고 conflict 시 value·observed_at
    까지 덮어써서, DART 정정 재무제표가 들어오면 최초 보고치와 관측 시각이
    사라졌다 - 정정 이전 시점의 재현이 미래 수치를 보게 되는 PIT 위반이다.
    """
    # 최초 보고 - 아무것도 모르는 상태에서 revision 1
    a = _F("00126380", "IS:매출액", "2025.12.31", "20260315000001")
    rev, fresh = assign_revisions([a], {})
    assert rev == [1] and fresh == 1, (rev, fresh)

    # 같은 접수번호 재수집 = 같은 revision (멱등 - 새 행이 생기면 안 된다)
    known = {fact_key(a): {"20260315000001": 1}}
    rev2, fresh2 = assign_revisions([a], known)
    assert rev2 == [1] and fresh2 == 0, (rev2, fresh2)

    # **정정본(다른 rcept_no)은 revision 2** - 원본은 그대로 남는다
    b = _F("00126380", "IS:매출액", "2025.12.31", "20260520000009")
    rev3, fresh3 = assign_revisions([b], known)
    assert rev3 == [2] and fresh3 == 1, (rev3, fresh3)

    # 한 배치에 원본·정정본이 같이 와도 접수번호 오름차순으로 번호가 붙는다
    rev4, _ = assign_revisions([b, a], {})     # 일부러 역순 입력
    assert rev4 == [2, 1], f"접수번호 순서가 revision 순서여야 한다: {rev4}"

    # 키가 다르면 서로의 revision 에 영향을 주지 않는다
    other = _F("00126380", "IS:영업이익", "2025.12.31", "20260520000009")
    rev5, _ = assign_revisions([other], known)
    assert rev5 == [1], "다른 계정인데 남의 개정 순서를 물려받았다"
    # 기간이 달라도 마찬가지
    prev_year = _F("00126380", "IS:매출액", "2024.12.31", "20260520000009")
    assert assign_revisions([prev_year], known)[0] == [1]

    # rcept_no 가 없으면 1 - 개정 순서를 매길 근거가 없다(지어내지 않는다)
    norc = _F("00126380", "IS:매출액", "2025.12.31", "")
    assert assign_revisions([norc], known)[0] == [1]

    # 3차 정정까지 - 접수번호가 커질수록 revision 이 는다
    k2 = {fact_key(a): {"20260315000001": 1, "20260520000009": 2}}
    c = _F("00126380", "IS:매출액", "2025.12.31", "20260801000003")
    assert assign_revisions([c], k2)[0] == [3]
    print("  정정 revision 배정       OK")


def _check_upsert_preserves_original():
    """SQL 이 원본의 value·observed_at 을 덮지 않는가 - 문자열로 강제한다.

    이 규칙이 무너지면 조용히 PIT 가 뚫리므로, 실제 DB 없이도 계약을 지킨다.
    """
    import inspect

    src = inspect.getsource(SupabaseReferenceRepository.upsert_financial_facts)
    body = src[src.index("on conflict (issuer_id"):]
    for forbidden in ("value = excluded.value",
                      "observed_at = excluded.observed_at",
                      "published_at = excluded.published_at"):
        assert forbidden not in body, \
            f"정정본이 원본을 덮는다: '{forbidden}' - 새 revision 으로 가야 한다"
    assert "assign_revisions(facts, known)" in src, \
        "revision 이 다시 고정값이 됐다 - 정정본이 원본 자리를 차지한다"
    print("  원본 보존 SQL 계약       OK")


def _check_ingest_result_invariant():
    r = IngestResult(
        attempted=10, instruments_inserted=7, instruments_updated=1,
        symbols_inserted=8, skipped_no_isin=1, skipped_duplicate_isin=1,
    )
    assert "제외 isin없음 1" in r.summary()
    assert r.instruments_inserted + r.instruments_updated + r.skipped_no_isin + r.skipped_duplicate_isin == r.attempted
    print("  IngestResult                OK")


def _ingest() -> int:
    """실제 LS 마스터를 받아 Supabase 에 적재한다."""
    sys.stdout.reconfigure(encoding="utf-8")
    from ls_client import LsRestClient, fetch_stock_master
    from subscription_plan import DataKind, build_plan

    as_of = datetime.now(timezone.utc)
    client = LsRestClient()
    print(f"  LS_ENV={client.environment}, 토큰 확보")

    rows, unclassified = fetch_stock_master(client)
    print(f"  t8436: {len(rows)}개 (분류 실패 {unclassified})")

    records = [master_row_to_record(r, as_of=as_of) for r in rows]
    repo = SupabaseReferenceRepository()
    try:
        result = repo.ingest_instruments(records, provider=PROVIDER_LS, as_of=as_of)
        print(f"  적재: {result.summary()}")

        # DB 가 발급한 instrument_id 로 구독 계획을 다시 만든다.
        members = repo.universe_members()
        print(f"  DB 기준 Universe: {len(members)}개")
        plan = build_plan(members, (DataKind.TICK, DataKind.QUOTE))
        print(f"  구독 계획: {plan.count}건")

        probe = ["005930", "000660", "035720"]
        resolved = repo.resolve_symbols(probe, provider=PROVIDER_LS)
        for s in probe:
            print(f"    {s} -> {resolved.get(s, '미매핑')}")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    if "--ingest" in sys.argv:
        print(f"{REPOSITORY_VERSION} 실제 적재")
        raise SystemExit(_ingest())

    print(f"{REPOSITORY_VERSION} 자체 점검 (DB 없이)")
    _check_mapping()
    _check_numeric_parsing()
    _check_revision_assignment()
    _check_upsert_preserves_original()
    _check_ingest_result_invariant()
    print("Reference Repository 5개 영역 통과. 실제 적재는 --ingest")
