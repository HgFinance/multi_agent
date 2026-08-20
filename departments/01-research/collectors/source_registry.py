#!/usr/bin/env python3
"""Sprint J1: 수집 Source Registry와 Collector 계약.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(P0 수집), 3.2(P1 후보),
      3.3(수집 금지), 5.1(reference.data_sources), 8.2(DQ Rule)
      docs/03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md 5.x
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F03, F04

왜 이 파일이 필요한가 - API Key 확보 상태가 Source 마다 다르다. 지금 LS, Open DART,
Tavily 는 키가 있고 KRX, BIGKinds, NAVER, ECOS, KOSIS, FRED 는 없다. 키가 없는
Source 를 코드 곳곳에서 if 로 분기하면 "조용히 빈 결과"가 정상값으로 흘러들어간다.

그래서 두 가지를 이 계층이 강제한다.

  1. 키 없는 Source 호출은 예외다. 빈 결과를 정상으로 취급하지 않는다.
     (개발 원칙 9 - 실패 시 확대가 아니라 차단 방향, multi-agent-workflow on_failure 원칙)
  2. Rate Limit, License, 허용 용도는 코드가 아니라 Registry 에 둔다.
     (RESEARCH_DATA_SOURCES_AND_LIBRARIES 7절 - 한도를 코드에 하드코딩하지 않는다)

Source 추가 방법 - SOURCES 에 SourceSpec 한 줄을 등록하고 Collector Protocol 을
구현한다. 기존 코드는 고치지 않는다. 그게 이 파일의 목적이다.

Registry 는 Supabase `reference.data_sources` 의 Git 쪽 선언이다. 운영 원장은 DB 이며
license/retention 의 최종 판단은 Data Steward 가 한다 - 이 파일이 승인을 대신하지 않는다.

자체 점검: python departments/01-research/collectors/source_registry.py
"""
from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

REGISTRY_VERSION = "research-source-registry-v1"

# 이 파일 기준 저장소 Root. departments/01-research/collectors/ 에서 3단 위다.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _pg_keepalives(dsn: str) -> str:
    """Postgres DSN 에 TCP keepalive 를 심는다. **URI 가 아니면 건드리지 않는다.**

    ▶ 왜 (2026-08-13 실측 + Supabase 공식 discussion #23272)
      풀러 경유 연결에서 `SSL connection has been closed unexpectedly` 가 하루
      2회 났다. 원인의 정석: 유휴 연결을 풀러/NAT 가 끊은 뒤 **죽은 소켓을
      재사용**해서다. 장수 컨테이너(수확기·dispatcher)는 카드가 없을 때 유휴가
      길어 가장 잘 물린다. 처방도 정석이 있다 - keepalive 로 소켓 생사를
      드라이버가 알게 하는 것.

      **여기 한 곳에 심는 이유**: 연결을 여는 자리가 컨테이너 23개에 흩어져
      있다. 컨테이너마다 제각각 설정하면 "수확기가 네 곳만 읽는" 류의 경계
      결함이 연결 설정에서 또 난다. 모두가 이 로더를 거치므로 여기가 정본이다.

      이미 keepalives 가 적혀 있으면 존중한다(운영자가 손으로 튜닝한 값을
      덮으면 안 된다).
    """
    if not dsn or not dsn.startswith(("postgres://", "postgresql://")):
        return dsn
    if "keepalives" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return (dsn + sep
            + "keepalives=1&keepalives_idle=30"
            + "&keepalives_interval=10&keepalives_count=3")


def load_project_env(repo_root: Path | None = None) -> dict[str, str]:
    """저장소 .env 와 프로세스 환경변수를 합친다. 환경변수가 우선이다.

    수집기마다 .env 를 손으로 파싱하지 않게 하려고 여기 한 곳에 둔다.
    encoding='utf-8' 을 반드시 명시한다 - 한국어 Windows 기본값(cp949)으로 열면
    .env 주석의 비ASCII 문자에서 UnicodeDecodeError 가 난다.

    os.environ 을 변경하지 않는다. Registry 는 주입된 dict 만 보므로 테스트가
    전역 상태를 오염시키지 않는다.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    env_path = root / ".env"

    file_env: dict[str, str] = {}
    if env_path.exists():
        try:
            from dotenv import dotenv_values  # requirements.txt 에 선언돼 있다

            file_env = {k: v for k, v in dotenv_values(env_path, encoding="utf-8").items() if v is not None}
        except ImportError:
            # dotenv 없이도 Registry 가 동작해야 한다. 한 줄 key=value 만 읽는다.
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                file_env[k.strip()] = v.strip()

    # 환경변수 우선 (news.py 와 같은 규칙). 빈 문자열은 미설정으로 보고 .env 값을 쓴다.
    merged = dict(file_env)
    for k, v in os.environ.items():
        if v.strip():
            merged[k] = v
    # 모든 소비자가 이 로더를 거치므로 keepalive 는 여기가 정본이다.
    for key in ("DATABASE_URL", "TIMESCALE_DATABASE_URL"):
        if merged.get(key):
            merged[key] = _pg_keepalives(merged[key])
    return merged


class SourceTier(StrEnum):
    """가이드 3.1 = P0, 3.2 = P1. P0 가 하나라도 KEY_MISSING 이면 수집 범위를 좁혀야 한다."""

    P0 = "P0"
    P1 = "P1"


class SourceDomain(StrEnum):
    """가이드 3.1 표의 Domain 열과 같은 구분."""

    REALTIME_PRICE = "REALTIME_PRICE"
    REALTIME_QUOTE = "REALTIME_QUOTE"
    MARKET_STATE = "MARKET_STATE"
    DERIVATIVE = "DERIVATIVE"
    INSTRUMENT_MASTER = "INSTRUMENT_MASTER"
    CALENDAR = "CALENDAR"
    DISCLOSURE = "DISCLOSURE"
    FINANCIAL = "FINANCIAL"
    ISSUER = "ISSUER"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    NEWS = "NEWS"
    MACRO = "MACRO"
    IR = "IR"


class MarketScope(StrEnum):
    """Source 가 실제로 덮는 시장. Domain 과 별개 축이다.

    ▶ 왜 필요한가 (2026-07-31 추가)
      Domain 만으로 판정하면 **미국 전용 뉴스 Source 를 P0 NEWS 로 한 줄 등록하는
      순간 한국 종목 뉴스가 0건인데도 NEWS Blocked 가 풀린다.** 그러면 "데이터
      장애 시 신규 진입이 자동 차단된다"(HEDGE_FUND_CORE_PLAN.md 성공 조건)는 방어가
      조용히 무너진다. 뉴스 API 5종 조사(2026-07-31)에서 실재하는 구멍으로 확인됐다.

      subscription_plan 의 approved_scopes Gate 가 구독 계획 계층에서 하던 방어를
      Source Registry 계층에도 둔다 - 두 계층의 방어 수준이 달라서는 안 된다.

    KR_MARKET        - 한국 시장의 종목·기업을 대상으로 하는 데이터. CORE_PLAN 3.1 의 대상.
    MACRO_BACKGROUND - 특정 종목이 아닌 거시 변수. 시장 범위 확장이 아니라 배경이므로
                       미국 지표(FRED)라도 범위 안이다.
    FOREIGN_MARKET   - 해외 개별종목·거래소 대상. 범위 밖이며 ADR 없이는 P0 가 될 수 없다.
                       (subscription_plan 이 LS 해외 TR 을 ScopeNotApproved 로 막는 것과 같은 기준)
    """

    KR_MARKET = "KR_MARKET"
    MACRO_BACKGROUND = "MACRO_BACKGROUND"
    FOREIGN_MARKET = "FOREIGN_MARKET"


# P0 Domain 을 채운 것으로 인정되는 Scope. 여기 없는 Scope 만 가진 Source 는
# 아무리 AVAILABLE 이어도 그 Domain 의 Blocked 를 풀지 못한다.
IN_SCOPE_FOR_P0: frozenset[MarketScope] = frozenset(
    {MarketScope.KR_MARKET, MarketScope.MACRO_BACKGROUND}
)


class SourceStatus(StrEnum):
    """Source 사용 가능 여부. 판정은 오직 Registry 가 한다.

    네 가지를 구분하는 이유 - 조치 주체와 방법이 다 다르다.
      KEY_MISSING     발급만 받으면 된다
      NOT_AUTHORIZED  키는 유효한데 해당 서비스 이용 승인이 없다
      NOT_CONTRACTED  계약·라이선스 검토가 선행이다
      DISABLED        우리가 의도적으로 껐다
    뭉치면 우선순위를 못 정한다.

    NOT_AUTHORIZED 를 따로 둔 계기 - KRX Open API 는 인증키 발급과 **서비스별 활용
    신청 승인**이 별개다. 키가 .env 에 있어도 승인 전에는 401 이 온다. 실측(2026-07-30)
    에서 헤더 AUTH_KEY 로 호출하면 "Unauthorized API Call"(키는 인식됨, 호출 권한 없음),
    잘못된 헤더면 "Unauthorized Key"(키 자체를 못 찾음)로 응답이 갈렸다.

    Registry 는 키 **존재**만 판정할 수 있다. 실제 호출 권한은 호출해 봐야 알기 때문에
    관측 결과를 NOT_AUTHORIZED_OBSERVED 에 근거와 함께 기록한다.
    """

    AVAILABLE = "AVAILABLE"
    KEY_MISSING = "KEY_MISSING"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    NOT_CONTRACTED = "NOT_CONTRACTED"
    DISABLED = "DISABLED"


class UseScope(StrEnum):
    """허용 용도. 가이드 3.3의 금지 사항을 Source 단위로 표현한다.

    뉴스가 특히 중요하다 - 검색 / Snippet 저장 / 전문 저장 / Embedding 권한이
    각각 다르므로(가이드 3.1 뉴스 행) 한 덩어리로 다루면 라이선스를 위반한다.
    """

    SEARCH_ONLY = "SEARCH_ONLY"
    SNIPPET_STORE = "SNIPPET_STORE"
    FULLTEXT_STORE = "FULLTEXT_STORE"
    EMBEDDING = "EMBEDDING"
    LONG_TERM_ARCHIVE = "LONG_TERM_ARCHIVE"
    REDISTRIBUTE = "REDISTRIBUTE"


class SourceSpec(BaseModel):
    """시장 수집 또는 요청형 MCP Source 한 개의 Git 쪽 계약이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z0-9_]+$", max_length=48)
    display_name: str = Field(min_length=1, max_length=64)
    domains: tuple[SourceDomain, ...] = Field(min_length=1)
    tier: SourceTier

    # 이 Source 가 실제로 덮는 시장. 기본값을 두지 않는다 - 기본값이 있으면 범위 밖
    # Source 를 추가할 때 아무도 이 필드를 안 보고 지나간다.
    market_scopes: tuple[MarketScope, ...] = Field(min_length=1)

    # 이 Source 를 쓰기 위해 반드시 있어야 하는 환경변수. 하나라도 비면 KEY_MISSING 이다.
    required_env: tuple[str, ...] = ()
    credential_mode_env: str | None = None
    required_env_by_mode: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # 없어도 동작하지만 있으면 기능이 늘어나는 것. 상태 판정에 넣지 않는다.
    optional_env: tuple[str, ...] = ()

    # 계약·라이선스가 아직 없는 Source. required_env 와 무관하게 NOT_CONTRACTED 다.
    contracted: bool = True
    # 의도적으로 끈 Source. 이유를 반드시 남긴다.
    disabled_reason: str | None = None

    allowed_uses: tuple[UseScope, ...] = ()
    # SEARCH_ONLY source가 실제로 노출하는 요청형 MCP 도구명. 계약·라이선스가
    # 있어도 이 값과 구현이 없으면 AVAILABLE이라고 보고해서는 안 된다.
    request_tool: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$", max_length=64
    )

    # 한도는 Vendor 가이드에서 확인한 값만 넣는다. 확인 전에는 None 으로 남긴다 -
    # 추측값을 넣으면 그게 사실처럼 굳는다(가이드 7절 - 코드에 하드코딩하지 않는다).
    rate_limit_per_sec: float | None = None
    rate_limit_per_day: int | None = None

    raw_bucket: str | None = Field(default=None, max_length=64)
    normalized_target: str | None = Field(default=None, max_length=64)
    doc_ref: str = Field(min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=300)


# ---------------------------------------------------------------------------
# 카탈로그 - 가이드 3.1(P0)과 3.2(P1)를 그대로 옮긴다.
# Source 를 추가할 때 여기에 한 줄 넣는 것으로 끝나야 한다.
# ---------------------------------------------------------------------------

SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        source_id="ls_openapi_ws",
        # LS 는 해외 TR 도 갖고 있지만 우리가 승인받은 범위는 국내다. subscription_plan
        # 의 ScopeNotApproved Gate 와 같은 기준으로 국내만 선언한다.
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="LS증권 Open API WebSocket",
        domains=(SourceDomain.REALTIME_PRICE, SourceDomain.REALTIME_QUOTE, SourceDomain.MARKET_STATE),
        tier=SourceTier.P0,
        required_env=(),
        credential_mode_env="LS_ENV",
        required_env_by_mode={
            "PAPER": ("LS_APP_KEY_PAPER", "LS_APP_SECRET_KEY_PAPER"),
            "LIVE": ("LS_APP_KEY", "LS_APP_SECRET_KEY"),
        },
        optional_env=("LS_WS_BASE_URL_PAPER",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="market-archive-private",
        normalized_target="market.market_ticks / market.market_quotes",
        doc_ref="docs/06-integrations/ls-openapi/, TEAM_JAEIL 3.1",
        note=(
            "가격·체결·호가의 확정 Source. 다른 Source 가격을 같은 price 에 섞지 않는다"
            "(가이드 3.3). 동시 구독 상한은 무제한(재일님 확인 2026-07-30, 벤더 문서에는"
            " 명시 없음) - 체결·호가는 tr_key 종목별 구독이며 subscription_plan.py 참고"
        ),
    ),
    SourceSpec(
        source_id="ls_openapi_rest",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="LS증권 Open API REST",
        domains=(SourceDomain.INSTRUMENT_MASTER, SourceDomain.MARKET_STATE,
                 SourceDomain.DERIVATIVE),
        tier=SourceTier.P0,
        required_env=("LS_REST_BASE_URL",),
        credential_mode_env="LS_ENV",
        required_env_by_mode={
            "PAPER": ("LS_APP_KEY_PAPER", "LS_APP_SECRET_KEY_PAPER"),
            "LIVE": ("LS_APP_KEY", "LS_APP_SECRET_KEY"),
        },
        optional_env=("LS_REST_BASE_URL_PAPER",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="reference.instruments",
        doc_ref="docs/06-integrations/ls-openapi/01-oauth, 03-stock, 04-derivatives",
        note="모의투자 REST Domain 은 수집 문서 기준 전부 '-'(미제공)이다. "
             "파생 시세(t2301/t2111 등)는 실전 키에 기본 개방 - 2026-07-31 실측",
    ),
    SourceSpec(
        source_id="opendart",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="Open DART",
        domains=(
            SourceDomain.DISCLOSURE,
            SourceDomain.FINANCIAL,
            SourceDomain.ISSUER,
            SourceDomain.CORPORATE_ACTION,
            SourceDomain.IR,
        ),
        tier=SourceTier.P0,
        required_env=("OPEN_DART_API_KEY",),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        request_tool="dart_search_disclosures",
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/api/external_sources.py, docs/06-integrations/opendart/",
        note=(
            "공시·재무·기업 정보는 MCP가 요청 시점에 조회하며 documents, financial_facts, "
            "Storage 또는 pgvector에 지속 적재하지 않는다. 응답의 rcept_dt는 날짜 정밀도라 "
            "PIT 실험 근거로 승격할 때 별도의 관측 시각 증명이 필요하다"
        ),
    ),
    SourceSpec(
        source_id="krx_openapi",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="KRX Data Marketplace Open API",
        domains=(
            SourceDomain.CALENDAR,
            SourceDomain.INSTRUMENT_MASTER,
            SourceDomain.CORPORATE_ACTION,
            SourceDomain.MARKET_STATE,
        ),
        tier=SourceTier.P0,
        required_env=("KRX_API_KEY",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="reference.market_calendars / reference.corporate_actions",
        doc_ref="TEAM_JAEIL 3.1, RESEARCH_DATA_SOURCES 5.x",
        note="거래 Calendar 의 공식 기준. 키 미확보 상태이므로 휴장·장 구간을 추정하지 않는다"
             " (CALENDAR Domain 자체는 krx_public_notice 선언 경로가 덮는다 - 2026-07-31)",
    ),
    SourceSpec(
        source_id="ls_news",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="LS 실시간 뉴스 (NWS + t3102)",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        required_env=("LS_APP_KEY", "LS_APP_SECRET_KEY"),
        disabled_reason=(
            "NWS/t3102 상주 수집기는 폐기됐고 요청형 MCP 어댑터도 구현돼 있지 않다. "
            "한국 뉴스 요청은 NAVER news_search만 사용한다"
        ),
        allowed_uses=(),
        raw_bucket=None,
        normalized_target=None,
        doc_ref="retired ls_news_collector lineage, ls-openapi 07-misc/03-stock",
        note=(
            "NWS/t3102 상주 구독, snippet 저장, research.documents 적재 및 장기 "
            "보관은 운영 경계 밖이다. 도구가 생기기 전 AVAILABLE로 판정하지 않는다"
        ),
    ),
    SourceSpec(
        source_id="krx_public_notice",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="KRX 휴장일 공표 (선언 Calendar)",
        domains=(SourceDomain.CALENDAR,),
        tier=SourceTier.P0,
        # API 가 아니라서 키가 없다 - 공표된 휴장일(사실 정보)을 선언 목록으로
        # 유지하고, calendar_declared 가 관측 Calendar(t8410 역산)와 **전 구간
        # 일치를 강제**한다. 불일치·검증불능이면 적재 자체가 거부된다(fail-closed).
        required_env=(),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="reference.market_calendar_versions (calendar_declared.py)",
        doc_ref="TEAM_JAEIL 3.1, Sprint J1 선언 Calendar",
        note="2026-07-31 도입 (재일님 지시 '캘린더는 알아서 수집, API 없이 괜찮음'). "
             "설·추석(음력)과 임시공휴일은 규칙으로 못 만들므로 매년 공표를 보고 "
             "목록을 갱신한다 - DECLARED_THROUGH 가 다음 해 생성을 막는다",
    ),
    SourceSpec(
        source_id="bigkinds",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="BIGKinds",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        required_env=("BIGKINDS_API_KEY",),
        # ▶ 재일님 결정 2026-07-31: 도입하지 않는다.
        #   KEY_MISSING 으로 두지 않는 이유 - 그 상태는 "발급만 받으면 된다" 는
        #   뜻인데 BIGKinds 는 API 이용이 유료 회원(월 5만원대)이라 사실과 다르다.
        #   조치 주체와 방법이 다르므로 상태를 구분한다(SourceStatus docstring).
        #   NAVER 가 P0 NEWS 를 덮고 있어 이 Source 없이도 Domain 은 열려 있다.
        disabled_reason=(
            "2026-07-31 가입 불가 확정(재일님) - 유료 회원(월 5만원대) 가입이 어렵다. "
            "이에 따라 **뉴스 분석은 헤드라인 기반으로 확정**했고, 본문이 필요한 "
            "분석은 전문 저장 권리가 있는 공시 원문(DART 2019003)으로 충당한다. "
            "재검토 조건: 가입 여건 변화 또는 헤드라인 분석의 한계 실측"
        ),
        # 저작권상 본문이 첫 200자로 제한될 수 있다(.env 주석, 가이드 3.1).
        # 전문 저장·Embedding 권한은 별도 확인 전까지 부여하지 않는다.
        allowed_uses=(UseScope.SEARCH_ONLY,),
        raw_bucket=None,
        normalized_target=None,
        doc_ref="TEAM_JAEIL 3.1, .env 7절",
        note="검색/Snippet/전문/Embedding/Archive/재배포 권한을 각각 따로 확인할 것",
    ),
    SourceSpec(
        source_id="x_twitter",
        market_scopes=(MarketScope.KR_MARKET, MarketScope.FOREIGN_MARKET),
        display_name="X (Twitter) 소셜 신호",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P1,
        required_env=("X_API_KEY",),
        # ▶ 조사 확정 2026-08-01 (재일님 "무료 라이브러리 없나" 질의):
        #   공식 무료 티어는 2026-02 부로 신규 개발자 읽기 종료(pay-per-use
        #   $0.005/read 기본). 무료 라이브러리는 존재하나(twikit·twscrape -
        #   계정 자격증명으로 내부 GraphQL 을 긁는 방식) **X ToS 위반 + 계정
        #   정지 위험 + 파이프라인 취약**이라 도입하지 않는다 - robots
        #   fail-closed·라이선스 게이트를 지켜온 이 Registry 의 원칙과 정면
        #   충돌한다. BIGKinds 포기와 같은 결의 결정이다.
        disabled_reason=(
            "무료 합법 읽기 경로 없음(2026-02 무료 티어 종료). 비공식 라이브러리"
            "(twikit/twscrape)는 ToS 위반이라 도입 불가. 재검토 조건: 유료 전환 "
            "결정(참고: $0.005/read - 일 1,000읽기 ~ 월 $150) 또는 for-good "
            "무료 승인. 합법 무료 대안 후보: Bluesky AT Protocol(공개 API)"
        ),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        normalized_target=None,
        doc_ref="가이드 3.1 X Watchlist(P1), 조사 2026-08-01",
        note="X Watchlist 는 이 Source 활성화 전까지 미착수 유지 - 승인 계정 "
             "Registry·삭제 Compliance 요건은 가이드 DoD 뉴스 항목 참고",
    ),
    SourceSpec(
        source_id="truth_social",
        market_scopes=(MarketScope.FOREIGN_MARKET,),
        display_name="Truth Social (Trump Media) 정책 발화",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P1,
        required_env=(),
        # ▶ 조사 확정 2026-08-01 (재일님 "트럼프 미디어에 투자 글 있지 않나" 질의):
        #   기술은 열려 있다 - Mastodon 포크라 /api/v1/accounts/lookup 과
        #   .../statuses 가 무인증 200(검색만 401), max_id 페이지네이션 작동.
        #   그러나 **두 축이 동시에 막는다**:
        #   (1) ToS 명시 금지 - "you will not access the Service through
        #       automated or non-human means, whether through a bot, script,
        #       or otherwise" + "data mining, robots, or similar data gathering
        #       and extraction tools" 금지. twikit·twscrape 를 거절한 것과
        #       같은 사유이며, robots 없음(빈 robots.txt)이 허락은 아니다.
        #   (2) 신호 밀도 미달 - realDonaldTrump 120건(5.6일, 21.4건/일) 표본에서
        #       시장 키워드 3%(4건), 그중 실질 시장 발화는 1건. 27%는 본문 없는
        #       미디어. 금융 기관은 사실상 부재(zerohedge 0포스트, djt 2포스트,
        #       treasury 0포스트) - 투자 담론장이 아니다.
        disabled_reason=(
            "ToS 가 자동 접근·데이터 수집을 명시 금지(API 는 기술적으로 열려 "
            "있으나 권리가 없다). 신호 밀도도 미달 - 실측 시장 관련 3%. "
            "재검토 조건: 공식 데이터 라이선스 제공 또는 ToS 개정. 대안(권리 "
            "청정): 정책 충격은 Federal Register API·백악관 Presidential "
            "Actions RSS(미 공무저작물)가 권위 있게 덮고, 발언 보도는 이미 "
            "수집 중인 Bluesky 기관 미디어가 수 분 내 덮는다"
        ),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        normalized_target=None,
        doc_ref="ToS help.truthsocial.com/legal/terms-of-service, 실측 2026-08-01",
        note="판단 시점 열람(비저장)까지가 한계 - 가이드 3.3 무권리 적재 금지",
    ),
    SourceSpec(
        source_id="naver_apihub",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="NAVER API HUB",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        required_env=("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        request_tool="news_search",
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/api/external_sources.py",
        note="MCP 요청 시점 검색 전용. snippet·본문·embedding을 운영 DB에 적재하지 않는다",
    ),
    SourceSpec(
        source_id="tavily",
        # 한국어 쿼리가 되므로 국내 범위다. 미국 전용 뉴스 API 들과 갈리는 지점이다
        # (2026-07-31 조사: Polygon/Finnhub/AlphaVantage/Alpaca 는 전부 FOREIGN_MARKET).
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="Tavily Search",
        domains=(SourceDomain.NEWS,),
        # 가이드 3.1의 뉴스 Source 는 BIGKinds/NAVER/계약 Vendor 다. Tavily 는
        # 그 목록에 없는 현행 Baseline 이므로 P1 로 두고 용도를 좁힌다.
        tier=SourceTier.P1,
        required_env=("TAVILY_API_KEY",),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        request_tool="tavily_search",
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/api/external_sources.py, CLAUDE.md",
        note=(
            "MCP 탐색 전용. 본문·snippet·embedding을 Storage, pgvector 또는 "
            "research.documents에 적재하지 않는다"
        ),
    ),
    SourceSpec(
        source_id="ecos",
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="한국은행 ECOS",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("ECOS_API_KEY",),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        request_tool="ecos_search",
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/api/external_macro.py",
        note="MCP 요청 시점 조회 전용. macro_observations에 상주 적재하지 않는다",
    ),
    SourceSpec(
        source_id="kosis",
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="KOSIS Open API",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("KOSIS_API_KEY",),
        disabled_reason="요청형 KOSIS MCP 어댑터가 아직 구현되지 않아 운영 사용을 차단한다",
        allowed_uses=(),
        raw_bucket=None,
        normalized_target=None,
        doc_ref="request-time MCP source contract",
        note="어댑터 구현 전에는 키가 있어도 AVAILABLE로 판정하지 않는다",
    ),
    SourceSpec(
        source_id="fred",
        # 미국 지표지만 특정 종목이 아니라 배경 변수다. 시장 범위 확장이 아니다.
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="FRED / ALFRED",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("FRED_API_KEY",),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        request_tool="fred_search",
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/api/external_macro.py",
        note="MCP 요청 시점 조회 전용. PIT 사용 시 ALFRED vintage를 응답 근거에 포함한다",
    ),
    SourceSpec(
        source_id="gpr",
        # 지정학 리스크는 특정 시장 데이터가 아니라 배경 변수다 (FRED 와 같은 취급).
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="GPR 지정학 리스크 지수 (Caldara-Iacoviello)",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P1,
        required_env=(),
        disabled_reason="요청형 GPR MCP 어댑터가 아직 구현되지 않아 운영 사용을 차단한다",
        # ▶ 실측 2026-08-01 (재일님 "국제정치로 시장이 들썩인다" 요구):
        #   일별 파일 무인증 다운로드 3.2MB, 15,183행 = 1985-01-01 ~ 현재.
        #   GPRD(종합)·GPRD_ACT(실제 사건)·GPRD_THREAT(위협·언사) 3열 -
        #   "폭격한다니 만다니"(THREAT)와 실제 타격(ACT)이 **분리돼 있다**.
        #   40년 일별 히스토리라 백테스트 팩터로 바로 쓸 수 있다.
        #   출처 표기 조건 공개 데이터(논문 인용 요건) - 재배포는 하지 않는다.
        allowed_uses=(),
        normalized_target=None,
        doc_ref="matteoiacoviello.com/gpr.htm, 실측 2026-08-01",
        note=(
            "MCP 요청 시점 조회 전용이며 운영 DB에 상주 적재하지 않는다. 게시 지연을 "
            "보수적으로 반영한 PIT 근거가 없는 값은 백테스트 입력으로 승격하지 않는다"
        ),
    ),
    SourceSpec(
        source_id="gdelt",
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="GDELT 전세계 보도량·톤 (지정학 실시간 축)",
        domains=(SourceDomain.MACRO, SourceDomain.NEWS),
        tier=SourceTier.P1,
        required_env=(),
        disabled_reason="요청형 GDELT MCP 어댑터가 아직 구현되지 않아 운영 사용을 차단한다",
        # ▶ 실측 2026-08-01: DOC 2.0 API 무인증 관통(timelinevol/timelinetone).
        #   테마별 보도 점유율 곡선이라 "충격 배율"(피크/중앙)로 이벤트를 잡는다.
        #   실측 - North Korea missile 최근/중앙 3.7배, Iran strike 피크 2.1배.
        #   라이선스: "unlimited and unrestricted use for any academic,
        #   commercial, or governmental use of any kind without fee" +
        #   출처 표기·링크 의무. 레이트리밋 5초/요청(429 실측) - 준수한다.
        allowed_uses=(),
        normalized_target=None,
        doc_ref="gdeltproject.org (출처 표기 의무), 실측 2026-08-01",
        note=(
            "MCP 요청 시점 조회 전용. 집계 지표와 기사 본문 모두 운영 DB에 상주 적재하지 "
            "않으며 응답을 사용할 때 GDELT Project 출처를 남긴다"
        ),
    ),
    SourceSpec(
        source_id="kind",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="KRX KIND",
        domains=(SourceDomain.IR, SourceDomain.DISCLOSURE),
        tier=SourceTier.P1,
        contracted=False,
        allowed_uses=(),
        doc_ref="TEAM_JAEIL 3.1(기업 IR)",
        note=(
            "2026-07-31 조사: KIND 자체 공식 Open API 는 없다. KRX 의 공식 경로는 "
            "Data Marketplace Open API(krx_openapi)뿐이며 거기에도 IR/공시 서비스는 없다. "
            "서드파티 스크래퍼(Apify 등)는 가이드 3.3 금지 경로다. IR 공지는 DART "
            "공시(기업설명회 개최)로 이미 들어오므로 KIND 는 대체 경로 확보 전까지 미사용"
        ),
    ),
    # --- 가이드 3.2 P1 후보. 계약 전이므로 NOT_CONTRACTED 로 남긴다 ---
    SourceSpec(
        source_id="short_interest",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="공매도·대차·대주",
        domains=(SourceDomain.MARKET_STATE,),
        tier=SourceTier.P1,
        contracted=False,
        doc_ref="TEAM_JAEIL 3.2",
        note="Long/Short 전략의 required_data_product_ids 에 필요. 공식·계약 Feed 확보가 도입 조건",
    ),
    SourceSpec(
        source_id="consensus",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="Consensus·실적 추정치",
        domains=(SourceDomain.FINANCIAL,),
        tier=SourceTier.P1,
        contracted=False,
        doc_ref="TEAM_JAEIL 3.2",
        note="기계 수집·모델 입력 권한이 있는 Vendor 계약이 도입 조건",
    ),
)


# 키는 있지만 실제 호출이 거부된 Source. 관측 사실이라 근거와 날짜를 함께 남긴다.
# 승인이 떨어지면 여기서 항목을 지우는 것으로 해제된다 - 코드 수정이 아니라 사실 갱신이다.
NOT_AUTHORIZED_OBSERVED: dict[str, str] = {
    # kosis: 2026-07-31 재일님 키 갱신 후 실측 통과(소비자물가 DT_1J22042 수신)로
    #        해제. err 11 이 다시 관측되면 여기 기록을 되살린다.
    "krx_openapi": (
        "2026-07-30 실측: 헤더 AUTH_KEY 로 https://data-dbg.krx.co.kr/svc/apis/sto/"
        "stk_bydd_trd 호출 시 401 'Unauthorized API Call'. 키는 인식되나(잘못된 헤더는 "
        "'Unauthorized Key' 로 다르게 응답) 서비스 이용 승인이 없다. "
        "openapi.krx.co.kr 에서 사용할 서비스별로 'API 활용 신청' 후 관리자 승인 필요. "
        "2026-07-31 추가 실측: /svc/sample/apis/{category}/{api_id} 샘플 경로도 401 이다 - "
        "승인 없이 우회할 경로는 없다"
    ),
}


class SourceRegistry:
    """Source 상태 판정과 조회. 수집기는 반드시 이 클래스를 통해 Source 를 얻는다."""

    _ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

    def __init__(
        self,
        specs: tuple[SourceSpec, ...] = SOURCES,
        env: dict[str, str] | None = None,
    ) -> None:
        seen: set[str] = set()
        for s in specs:
            if s.source_id in seen:
                raise ValueError(f"source_id 중복: {s.source_id}")
            seen.add(s.source_id)
            mode_envs = (s.credential_mode_env,) if s.credential_mode_env else ()
            mode_required = tuple(
                name
                for names in s.required_env_by_mode.values()
                for name in names
            )
            for name in (*s.required_env, *s.optional_env, *mode_envs, *mode_required):
                if not self._ENV_NAME.match(name):
                    raise ValueError(f"{s.source_id}: 환경변수명 형식 위반 {name!r}")
            if s.required_env_by_mode and not s.credential_mode_env:
                raise ValueError(
                    f"{s.source_id}: required_env_by_mode에는 credential_mode_env가 필요합니다"
                )
            for mode in s.required_env_by_mode:
                if not self._ENV_NAME.match(mode):
                    raise ValueError(f"{s.source_id}: 모드명 형식 위반 {mode!r}")
        self._specs = {s.source_id: s for s in specs}
        # env 를 주입받는다. 테스트가 os.environ 을 오염시키지 않게 하려는 것이다.
        # 주입이 없으면 .env + 환경변수를 합쳐서 읽는다.
        self._env = load_project_env() if env is None else dict(env)

    def spec(self, source_id: str) -> SourceSpec:
        try:
            return self._specs[source_id]
        except KeyError:
            raise KeyError(
                f"등록되지 않은 source_id: {source_id!r}. "
                f"SOURCES 에 SourceSpec 을 추가하세요"
            ) from None

    def status(self, source_id: str) -> SourceStatus:
        s = self.spec(source_id)
        if s.disabled_reason:
            return SourceStatus.DISABLED
        if not s.contracted:
            return SourceStatus.NOT_CONTRACTED
        if self.missing_env(source_id):
            return SourceStatus.KEY_MISSING
        # 키는 있다. 실제 호출이 거부된 관측이 있으면 그것이 사실이다.
        if source_id in NOT_AUTHORIZED_OBSERVED:
            return SourceStatus.NOT_AUTHORIZED
        return SourceStatus.AVAILABLE

    def missing_env(self, source_id: str) -> tuple[str, ...]:
        """빈 문자열도 미확보로 본다. .env 에 이름만 있고 값이 없는 상태가 대부분이다."""
        s = self.spec(source_id)
        required = list(s.required_env)
        if s.required_env_by_mode:
            mode = (self._env.get(s.credential_mode_env or "") or "LIVE").strip().upper()
            mode_required = s.required_env_by_mode.get(mode)
            if mode_required is None:
                # 알 수 없는 모드는 사용 불가로 취급한다. 실제 모드 오류는
                # 해당 adapter가 더 구체적인 메시지로 다시 검증한다.
                mode_required = tuple(
                    name
                    for names in s.required_env_by_mode.values()
                    for name in names
                )
            required.extend(mode_required)
        return tuple(name for name in required if not self._env.get(name, "").strip())

    def is_available(self, source_id: str) -> bool:
        return self.status(source_id) is SourceStatus.AVAILABLE

    def require(self, source_id: str) -> SourceSpec:
        """사용 직전 호출한다. 사용 불가면 예외다 - 빈 결과로 넘기지 않는다.

        개발 원칙 9와 workflow on_failure 원칙을 이 한 곳에서 강제한다.
        수집기가 각자 if 로 분기하면 언젠가 하나가 조용히 통과한다.
        """
        st = self.status(source_id)
        if st is SourceStatus.AVAILABLE:
            return self.spec(source_id)

        s = self.spec(source_id)
        if st is SourceStatus.KEY_MISSING:
            detail = f"환경변수 미확보: {', '.join(self.missing_env(source_id))}"
        elif st is SourceStatus.NOT_AUTHORIZED:
            detail = f"키는 있으나 호출 권한 없음 - {NOT_AUTHORIZED_OBSERVED[source_id]}"
        elif st is SourceStatus.NOT_CONTRACTED:
            detail = f"계약·라이선스 미확보 ({s.note or '도입 조건 확인 필요'})"
        else:
            detail = f"비활성: {s.disabled_reason}"
        raise SourceUnavailable(
            f"[{source_id}] {s.display_name} 사용 불가 - {st.value}. {detail}"
        )

    def check_use(self, source_id: str, scope: UseScope) -> None:
        """허용 용도 검사. 라이선스 밖 사용을 코드 단계에서 막는다(가이드 3.3)."""
        s = self.spec(source_id)
        if scope not in s.allowed_uses:
            raise SourceUseNotAllowed(
                f"[{source_id}] {scope.value} 는 허용 용도가 아니다. "
                f"허용: {[u.value for u in s.allowed_uses] or '없음'}. "
                f"{s.note or ''}".strip()
            )

    def by_status(self, status: SourceStatus) -> tuple[SourceSpec, ...]:
        return tuple(s for sid, s in self._specs.items() if self.status(sid) is status)

    def by_domain(self, domain: SourceDomain) -> tuple[SourceSpec, ...]:
        return tuple(s for s in self._specs.values() if domain in s.domains)

    def blocked_p0_domains(self) -> tuple[SourceDomain, ...]:
        """P0 Source 가 전부 사용 불가인 Domain. 수집 범위 축소 판단에 쓴다.

        Domain 하나에 P0 Source 가 여러 개면(예: INSTRUMENT_MASTER 는 LS + KRX)
        하나만 살아 있어도 Blocked 가 아니다.

        ▶ Scope 를 함께 본다 (2026-07-31)
          Domain 만 보면 **범위 밖 Source 하나로 Blocked 가 풀린다.** 예를 들어
          미국 전용 뉴스 API 를 P0 NEWS 로 등록하면 한국 종목 뉴스가 0건인데도
          NEWS 가 Blocked 에서 빠진다. IN_SCOPE_FOR_P0 에 드는 Scope 를 가진
          Source 만 Blocked 해제 자격이 있다.
        """
        blocked = []
        for d in SourceDomain:
            p0 = [
                s for s in self.by_domain(d)
                if s.tier is SourceTier.P0 and IN_SCOPE_FOR_P0.intersection(s.market_scopes)
            ]
            if p0 and not any(self.is_available(s.source_id) for s in p0):
                blocked.append(d)
        return tuple(blocked)

    def out_of_scope_p0_sources(self) -> tuple[str, ...]:
        """P0 인데 범위 밖 Scope 만 가진 Source. 등록 자체가 모순이므로 드러낸다."""
        return tuple(
            s.source_id
            for s in self._specs.values()
            if s.tier is SourceTier.P0 and not IN_SCOPE_FOR_P0.intersection(s.market_scopes)
        )

    def report(self) -> str:
        """DQ Status 와 AI Office Market View 에 그대로 실을 수 있는 요약(가이드 6.3)."""
        lines = [f"{REGISTRY_VERSION} - Source {len(self._specs)}개"]
        for st in SourceStatus:
            group = self.by_status(st)
            if not group:
                continue
            lines.append(f"  [{st.value}] {len(group)}개")
            for s in sorted(group, key=lambda x: (x.tier.value, x.source_id)):
                extra = ""
                if st is SourceStatus.KEY_MISSING:
                    extra = f" <- {', '.join(self.missing_env(s.source_id))}"
                elif st is SourceStatus.NOT_AUTHORIZED:
                    extra = " <- 키 있음, 서비스 이용 승인 필요"
                lines.append(f"    {s.tier.value} {s.source_id:18} {s.display_name}{extra}")
        blocked = self.blocked_p0_domains()
        lines.append(
            "  P0 Blocked Domain: " + (", ".join(d.value for d in blocked) if blocked else "없음")
        )
        return "\n".join(lines)


class SourceUnavailable(RuntimeError):
    """Source 사용 불가. 호출자가 이 예외를 삼켜서 빈 결과로 바꾸지 않는다."""


class SourceUseNotAllowed(PermissionError):
    """라이선스 허용 범위를 벗어난 사용."""


@runtime_checkable
class Collector(Protocol):
    """수집기 계약. Source 를 추가하려면 이것만 구현한다.

    collect() 는 원본을 반환하지 않고 정규화된 Domain 객체를 반환한다.
    Raw 보존은 Archive 계층 책임이며 수집기가 Domain 객체에 Payload 를 섞지 않는다.
    """

    source_id: str

    def collect(self, *, trace_id: str, **params: object) -> object:
        ...


# ---------------------------------------------------------------------------
# 자체 점검 - Registry 6개 영역
# ---------------------------------------------------------------------------

_FAKE_ENV = {
    "LS_ENV": "LIVE",
    "LS_APP_KEY": "k" * 36,
    "LS_APP_SECRET_KEY": "s" * 32,
    "LS_REST_BASE_URL": "https://example.test",
    "OPEN_DART_API_KEY": "d" * 40,
    "TAVILY_API_KEY": "tvly-x",
    # 미확보 Source 를 그대로 재현한다 - 이름은 있고 값이 빈 상태
    "KRX_API_KEY": "",
    "BIGKINDS_API_KEY": "",
    "NAVER_CLIENT_ID": "",
    "NAVER_CLIENT_SECRET": "",
    "ECOS_API_KEY": "   ",
    "KOSIS_API_KEY": "",
    "FRED_API_KEY": "",
}


def _check_catalog():
    r = SourceRegistry(env=_FAKE_ENV)
    assert len(SOURCES) >= 13
    # 가이드 3.1의 P0 Domain 이 최소 하나의 Source 로 덮여 있어야 한다
    for d in (
        SourceDomain.REALTIME_PRICE,
        SourceDomain.REALTIME_QUOTE,
        SourceDomain.CALENDAR,
        SourceDomain.DISCLOSURE,
        SourceDomain.FINANCIAL,
        SourceDomain.NEWS,
        SourceDomain.MACRO,
    ):
        assert r.by_domain(d), f"{d.value} 를 담당하는 Source 가 없다"

    # 계약·구현이 활성인 요청형 Source는 실제 MCP 도구명을 반드시 선언한다.
    for source in SOURCES:
        if (
            source.contracted
            and not source.disabled_reason
            and UseScope.SEARCH_ONLY in source.allowed_uses
        ):
            assert source.request_tool, (
                f"{source.source_id} is SEARCH_ONLY but has no request-time MCP tool"
            )

    # source_id 중복은 생성 시점에 막는다
    try:
        SourceRegistry(specs=(SOURCES[0], SOURCES[0]), env=_FAKE_ENV)
        raise AssertionError("source_id 중복이 통과했다")
    except ValueError:
        pass
    print("  카탈로그              OK")


def _check_status():
    r = SourceRegistry(env=_FAKE_ENV)
    assert r.status("ls_openapi_ws") is SourceStatus.AVAILABLE
    assert r.status("opendart") is SourceStatus.AVAILABLE
    assert r.status("tavily") is SourceStatus.AVAILABLE
    assert r.status("krx_openapi") is SourceStatus.KEY_MISSING
    assert r.status("naver_apihub") is SourceStatus.KEY_MISSING
    for source_id in ("ls_news", "kosis", "gpr", "gdelt"):
        assert r.status(source_id) is SourceStatus.DISABLED
    # 공백만 있는 값도 미확보로 본다
    assert r.status("ecos") is SourceStatus.KEY_MISSING
    # 계약 자체가 없는 Source 는 키와 무관하다
    assert r.status("kind") is SourceStatus.NOT_CONTRACTED
    assert r.status("short_interest") is SourceStatus.NOT_CONTRACTED

    assert r.missing_env("naver_apihub") == ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")
    assert r.missing_env("opendart") == ()

    # 등록되지 않은 Source 는 조용히 None 이 아니라 예외다
    try:
        r.status("nonexistent")
        raise AssertionError("미등록 source_id 가 통과했다")
    except KeyError:
        pass
    # 키가 있어도 호출 권한 관측이 있으면 NOT_AUTHORIZED 다
    r2 = SourceRegistry(env={**_FAKE_ENV, "KRX_API_KEY": "k" * 40})
    assert r2.status("krx_openapi") is SourceStatus.NOT_AUTHORIZED, "키만 보고 AVAILABLE 로 판정했다"
    try:
        r2.require("krx_openapi")
        raise AssertionError("NOT_AUTHORIZED 가 통과했다")
    except SourceUnavailable as e:
        assert "호출 권한 없음" in str(e) and "활용 신청" in str(e)

    # 승인이 떨어지면 관측 기록을 지우는 것으로 해제된다
    saved = NOT_AUTHORIZED_OBSERVED.pop("krx_openapi")
    try:
        assert r2.status("krx_openapi") is SourceStatus.AVAILABLE
    finally:
        NOT_AUTHORIZED_OBSERVED["krx_openapi"] = saved
    print("  상태 판정             OK")


def _check_require_fails_closed():
    r = SourceRegistry(env=_FAKE_ENV)
    assert r.require("opendart").source_id == "opendart"

    # 사용 불가 사유가 네 가지 다 있어야 한다 - 조치 주체와 방법이 다르므로
    # require 가 사유를 구분해서 알려줘야 운영 중에 판단할 수 있다.
    # NOT_AUTHORIZED 는 키가 있을 때만 나온다 - 키가 없으면 KEY_MISSING 이 맞다.
    cases = [
        ("krx_openapi", SourceStatus.NOT_AUTHORIZED, {"KRX_API_KEY": "k"}),  # 승인 필요
        ("ecos", SourceStatus.KEY_MISSING, {}),            # 발급만 하면 됨
        ("kind", SourceStatus.NOT_CONTRACTED, {}),         # 계약 검토 선행
        ("bigkinds", SourceStatus.DISABLED, {}),           # 우리가 의도적으로 끔
    ]
    for sid, expected, extra in cases:
        rr = SourceRegistry(env={**_FAKE_ENV, **extra}) if extra else r
        assert rr.status(sid) is expected, f"{sid}: {rr.status(sid)} != {expected}"
        try:
            rr.require(sid)
            raise AssertionError(f"{sid} 가 사용 불가인데 통과했다")
        except SourceUnavailable as e:
            msg = str(e)
            assert sid in msg and expected.value in msg, msg
            # 사유가 비어 있으면 무엇을 해야 할지 알 수 없다
            assert len(msg.split(expected.value, 1)[1].strip(". ")) > 5, msg

    # DISABLED 는 키가 생겨도 풀리지 않는다 - 우리가 끈 것이지 못 쓰는 게 아니다
    r2 = SourceRegistry(env={**_FAKE_ENV, "BIGKINDS_API_KEY": "k"})
    assert r2.status("bigkinds") is SourceStatus.DISABLED
    print("  Fail-closed           OK")


def _check_license_scope():
    r = SourceRegistry(env=_FAKE_ENV)
    # Tavily 는 탐색 전용이다. 본문 저장·Embedding 을 막는다(가이드 3.3)
    r.check_use("tavily", UseScope.SEARCH_ONLY)
    for scope in (UseScope.FULLTEXT_STORE, UseScope.EMBEDDING, UseScope.LONG_TERM_ARCHIVE):
        try:
            r.check_use("tavily", scope)
            raise AssertionError(f"tavily {scope.value} 가 통과했다")
        except SourceUseNotAllowed:
            pass

    # 비시장 정보원은 snippet조차 지속 적재하지 않는 요청형 조회 전용이다.
    r.check_use("bigkinds", UseScope.SEARCH_ONLY)
    try:
        r.check_use("bigkinds", UseScope.FULLTEXT_STORE)
        raise AssertionError("bigkinds 전문 저장이 통과했다")
    except SourceUseNotAllowed:
        pass

    # 모든 요청형 정보원에 대해 저장·임베딩·재배포 권한이 닫혔는지 독립 검증한다.
    r.check_use("opendart", UseScope.SEARCH_ONLY)
    for source_id in (
        "opendart", "ls_news", "bigkinds", "x_twitter", "truth_social",
        "naver_apihub", "tavily", "ecos", "kosis", "fred", "gpr", "gdelt",
    ):
        for scope in (
            UseScope.SNIPPET_STORE,
            UseScope.FULLTEXT_STORE,
            UseScope.EMBEDDING,
            UseScope.LONG_TERM_ARCHIVE,
            UseScope.REDISTRIBUTE,
        ):
            try:
                r.check_use(source_id, scope)
                raise AssertionError(f"{source_id} {scope.value} unexpectedly allowed")
            except SourceUseNotAllowed:
                pass
    print("  라이선스 Scope        OK")


def _check_blocked_domains():
    r = SourceRegistry(env=_FAKE_ENV)
    blocked = r.blocked_p0_domains()
    # CALENDAR 는 키가 하나도 없어도 Blocked 가 아니다 - krx_public_notice(선언 +
    # 관측 검증, 2026-07-31)가 키 없이 덮는다. calendar_declared 가 fail-closed 를
    # 맡으므로 Registry 는 경로 존재만 본다.
    assert SourceDomain.CALENDAR not in blocked, "선언 Calendar 가 있는데 CALENDAR 가 막혔다"
    assert r.status("krx_public_notice") is SourceStatus.AVAILABLE
    # 키가 없어서 지금 막힌 Domain
    assert SourceDomain.MACRO in blocked
    # LS 뉴스 수집기는 폐기됐고 요청형 어댑터도 없다. NAVER 키가 없으면 NEWS는
    # 정직하게 막혀야 하며, LS 시세 키가 그 상태를 풀어서는 안 된다.
    assert SourceDomain.NEWS in blocked
    # LS 로 덮이는 Domain 은 막히지 않는다
    assert SourceDomain.REALTIME_PRICE not in blocked
    assert SourceDomain.REALTIME_QUOTE not in blocked
    assert SourceDomain.DERIVATIVE not in blocked, "파생은 ls_openapi_rest 가 덮는다"
    # LS 키가 사라지면 LS 단독 시장 Domain은 막힌다. NEWS 상태는 NAVER가 소유한다.
    no_ls = SourceRegistry(env={**_FAKE_ENV, "LS_APP_KEY": "", "LS_APP_SECRET_KEY": ""})
    b2 = no_ls.blocked_p0_domains()
    assert SourceDomain.DERIVATIVE in b2 and SourceDomain.REALTIME_PRICE in b2
    with_naver = SourceRegistry(env={
        **_FAKE_ENV,
        "NAVER_CLIENT_ID": "client",
        "NAVER_CLIENT_SECRET": "secret",
    })
    assert SourceDomain.NEWS not in with_naver.blocked_p0_domains()
    # DART 로 덮이는 Domain
    assert SourceDomain.DISCLOSURE not in blocked
    assert SourceDomain.FINANCIAL not in blocked
    # INSTRUMENT_MASTER 는 LS + KRX 인데 LS 가 살아 있으므로 Blocked 가 아니다
    assert SourceDomain.INSTRUMENT_MASTER not in blocked

    # KRX API 자체는 키를 채워도 서비스 이용 승인이 없으면 NOT_AUTHORIZED 다.
    # 키 존재만으로 풀리면 안 된다 - 승인 전에 호출하면 401 이고, 그걸 빈 결과로
    # 취급하면 데이터를 추정하게 된다. (CALENDAR Blocked 와는 이제 무관하다)
    r2 = SourceRegistry(env={**_FAKE_ENV, "KRX_API_KEY": "krx-key"})
    assert r2.status("krx_openapi") is SourceStatus.NOT_AUTHORIZED

    # 승인이 떨어지면(관측 기록 제거) AVAILABLE 로 돌아온다 - 회귀 방지
    saved = NOT_AUTHORIZED_OBSERVED.pop("krx_openapi")
    try:
        assert r2.status("krx_openapi") is SourceStatus.AVAILABLE
    finally:
        NOT_AUTHORIZED_OBSERVED["krx_openapi"] = saved
    print("  Blocked Domain        OK")


def _check_scope_gate():
    """범위 밖 Source 가 P0 Blocked 를 풀지 못하는지 (2026-07-31 뉴스 API 조사 결과).

    Polygon/Finnhub/AlphaVantage/Alpaca 를 조사했을 때 넷 다 미국 전용이었다. 이런
    Source 를 P0 NEWS 로 등록하면 한국 종목 뉴스가 0건인데 NEWS Blocked 가 풀린다.
    그러면 '데이터 장애 시 신규 진입 자동 차단' 이 조용히 무너진다.
    """
    # 실제 국내 뉴스 도구(NAVER)는 비운 채 범위 밖 Source가 NEWS를 풀지 못하게 한다.
    env_with_news = {**_FAKE_ENV, "LS_APP_KEY": "", "LS_APP_SECRET_KEY": "",
                     "FOREIGN_NEWS_API_KEY": "k"}

    foreign = SourceSpec(
        source_id="foreign_news_probe",
        display_name="가상 해외 뉴스 API",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        market_scopes=(MarketScope.FOREIGN_MARKET,),
        required_env=("FOREIGN_NEWS_API_KEY",),
        allowed_uses=(UseScope.SEARCH_ONLY,),
        doc_ref="self-check",
    )
    r = SourceRegistry(env=env_with_news, specs=SOURCES + (foreign,))
    assert r.status("foreign_news_probe") is SourceStatus.AVAILABLE, "전제가 깨졌다"
    assert SourceDomain.NEWS in r.blocked_p0_domains(), (
        "미국 전용 뉴스 Source 가 NEWS Blocked 를 풀었다 - Scope Gate 가 동작하지 않는다"
    )
    assert "foreign_news_probe" in r.out_of_scope_p0_sources()

    # 같은 Source 가 국내 범위였다면 풀려야 한다(Gate 가 Scope 로만 판단하는지 확인)
    domestic = foreign.model_copy(update={"market_scopes": (MarketScope.KR_MARKET,)})
    r2 = SourceRegistry(env=env_with_news, specs=SOURCES + (domestic,))
    assert SourceDomain.NEWS not in r2.blocked_p0_domains()
    assert not r2.out_of_scope_p0_sources()

    # 실제 카탈로그에는 범위 밖 P0 가 없어야 한다
    assert not SourceRegistry(env=_FAKE_ENV).out_of_scope_p0_sources()
    # market_scopes 는 기본값이 없다 - 새 Source 추가 시 반드시 선언하게 한다
    try:
        SourceSpec(source_id="no_scope", display_name="x", domains=(SourceDomain.NEWS,),
                   tier=SourceTier.P1, doc_ref="self-check")
        raise AssertionError("market_scopes 없이 SourceSpec 이 만들어졌다")
    except Exception as e:  # noqa: BLE001 - intentional fallback boundary
        assert "market_scopes" in str(e)
    print("  Scope Gate            OK")


def _check_rate_limit_not_invented():
    """한도를 추측해서 넣지 않았는지 확인한다(가이드 7절).

    확인된 값이 없으면 None 이어야 한다. 숫자가 들어오는 순간 그게 사실처럼 쓰인다.
    """
    for s in SOURCES:
        if s.rate_limit_per_sec is not None:
            assert s.rate_limit_per_sec > 0, f"{s.source_id}: 한도가 0 이하다"
        if s.rate_limit_per_day is not None:
            assert s.rate_limit_per_day > 0
    unverified = [s.source_id for s in SOURCES if s.rate_limit_per_sec is None]
    assert unverified, "모든 한도가 채워졌다면 Vendor 확인 근거를 note 에 남겼는지 보세요"
    print(f"  한도 미확인 {len(unverified)}개       OK (추측값 없음)")


if __name__ == "__main__":
    print(f"{REGISTRY_VERSION} 자체 점검")
    _check_catalog()
    _check_status()
    _check_require_fails_closed()
    _check_license_scope()
    _check_blocked_domains()
    _check_scope_gate()
    _check_rate_limit_not_invented()
    print("Registry 6개 영역 통과\n")
    print(SourceRegistry().report())
