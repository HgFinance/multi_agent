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
    """수집 Source 한 개의 선언. Supabase reference.data_sources 의 Git 쪽 사본이다."""

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
    # 없어도 동작하지만 있으면 기능이 늘어나는 것. 상태 판정에 넣지 않는다.
    optional_env: tuple[str, ...] = ()

    # 계약·라이선스가 아직 없는 Source. required_env 와 무관하게 NOT_CONTRACTED 다.
    contracted: bool = True
    # 의도적으로 끈 Source. 이유를 반드시 남긴다.
    disabled_reason: str | None = None

    allowed_uses: tuple[UseScope, ...] = ()

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
        required_env=("LS_APP_KEY", "LS_APP_SECRET_KEY"),
        optional_env=("LS_APP_KEY_PAPER", "LS_APP_SECRET_KEY_PAPER", "LS_WS_BASE_URL_PAPER"),
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
        domains=(SourceDomain.INSTRUMENT_MASTER, SourceDomain.MARKET_STATE),
        tier=SourceTier.P0,
        required_env=("LS_APP_KEY", "LS_APP_SECRET_KEY"),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="reference.instruments",
        doc_ref="docs/06-integrations/ls-openapi/01-oauth, 03-stock",
        note="모의투자 REST Domain 은 수집 문서 기준 전부 '-'(미제공)이다",
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
        allowed_uses=(
            UseScope.FULLTEXT_STORE,
            UseScope.EMBEDDING,
            UseScope.LONG_TERM_ARCHIVE,
        ),
        raw_bucket="research-documents-private",
        normalized_target="research.documents / research.financial_facts",
        doc_ref="docs/06-integrations/opendart/, TEAM_JAEIL 3.1",
        note=(
            "요청 파라미터명은 crtfc_key. rcept_no Unique 와 정정 관계 누락 0 이 DQ 기준"
            "(가이드 8.2). ▶ PIT 한계: list.json 의 rcept_dt 는 날짜뿐이고 접수 시각이 "
            "없다(실측 2026-07-30). published_at 정밀도가 DAY 이므로 Backtest·Agent 는 "
            "observed_at 을 기준으로 판단해야 한다(가이드 4.2). 정확한 시각이 필요하면 "
            "공시원문 API 2019003 이나 별도 Source 가 필요하다"
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
        note="거래 Calendar 의 공식 기준. 키 미확보 상태이므로 휴장·장 구간을 추정하지 않는다",
    ),
    SourceSpec(
        source_id="bigkinds",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="BIGKinds",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        required_env=("BIGKINDS_API_KEY",),
        # 저작권상 본문이 첫 200자로 제한될 수 있다(.env 주석, 가이드 3.1).
        # 전문 저장·Embedding 권한은 별도 확인 전까지 부여하지 않는다.
        allowed_uses=(UseScope.SEARCH_ONLY, UseScope.SNIPPET_STORE),
        raw_bucket="research-documents-private",
        normalized_target="research.documents",
        doc_ref="TEAM_JAEIL 3.1, .env 7절",
        note="검색/Snippet/전문/Embedding/Archive/재배포 권한을 각각 따로 확인할 것",
    ),
    SourceSpec(
        source_id="naver_apihub",
        market_scopes=(MarketScope.KR_MARKET,),
        display_name="NAVER API HUB",
        domains=(SourceDomain.NEWS,),
        tier=SourceTier.P0,
        required_env=("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
        allowed_uses=(UseScope.SEARCH_ONLY, UseScope.SNIPPET_STORE),
        raw_bucket="research-documents-private",
        normalized_target="research.documents",
        doc_ref="TEAM_JAEIL 3.1, .env 7절",
        note="Search API 가 개발자센터에서 이관 중(2026-07). Endpoint 에 결합하지 말고 Adapter 로 분리",
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
        raw_bucket=None,
        normalized_target=None,
        doc_ref="departments/01-research/collectors/news.py, CLAUDE.md",
        note=(
            "탐색 전용 Baseline. 본문을 Storage·pgvector 에 적재하지 않는다(가이드 3.3). "
            "research.documents 의 공식 Source 로 승격하려면 사용권 확인이 선행이다"
        ),
    ),
    SourceSpec(
        source_id="alpaca_news",
        display_name="Alpaca Market News (Benzinga)",
        domains=(SourceDomain.NEWS,),
        # ▶ 왜 P1 / FOREIGN_MARKET 인가 (재일님 지시 2026-07-31로 도입, 범위는 정직하게)
        #   2026-07-31 뉴스 API 5종 조사에서 '무료 + 뉴스 WebSocket' 을 동시에 만족하는
        #   유일한 곳이었다(Finnhub 는 Premium, Polygon·AlphaVantage 는 WS 자체가 없음).
        #   다만 Get Assets 의 exchange enum 이 AMEX/ARCA/BATS/NYSE/NASDAQ/NYSEARCA/OTC/
        #   CRYPTO 뿐이라 KRX 종목은 조회되지 않는다. 그래서 국내 P0 NEWS 를 대체하지
        #   못하며, P0 로 올리면 BIGKinds/NAVER 없이 NEWS Blocked 가 거짓으로 풀린다.
        tier=SourceTier.P1,
        market_scopes=(MarketScope.FOREIGN_MARKET,),
        required_env=("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"),
        optional_env=("ALPACA_NEWS_WS_URL", "ALPACA_DATA_BASE_URL"),
        # 약관이 "personal and noncommercial access and use" 이고 "encoded" 를 금지
        # 행위로 열거한다. 본문 저장·Embedding·재배포는 상업 계약 없이는 부여하지
        # 않는다(가이드 3.3). 승격은 Data Steward 판단이며 이 파일이 대신하지 않는다.
        allowed_uses=(UseScope.SEARCH_ONLY,),
        raw_bucket=None,
        normalized_target="research.documents (제목·URL·시각만)",
        doc_ref="docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.2, 5.4",
        note=(
            "미국 종목 전용. KRX 미커버라 국내 P0 를 대체하지 않는다. 심볼이 "
            "reference.instruments 에 없으므로 document_instruments 연결이 불가하며 "
            "미해결 심볼은 날조하지 않고 카운트만 한다. 본문 저장은 라이선스 확보 후"
        ),
    ),
    SourceSpec(
        source_id="ecos",
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="한국은행 ECOS",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("ECOS_API_KEY",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="research.macro_observations",
        doc_ref="TEAM_JAEIL 3.1, RESEARCH_DATA_SOURCES 5.x",
        note="Revision 은 덮어쓰지 않고 vintage_date 로 Append 한다(가이드 5.2)",
    ),
    SourceSpec(
        source_id="kosis",
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="KOSIS Open API",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("KOSIS_API_KEY",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="research.macro_observations",
        doc_ref="TEAM_JAEIL 3.1",
    ),
    SourceSpec(
        source_id="fred",
        # 미국 지표지만 특정 종목이 아니라 배경 변수다. 시장 범위 확장이 아니다.
        market_scopes=(MarketScope.MACRO_BACKGROUND,),
        display_name="FRED / ALFRED",
        domains=(SourceDomain.MACRO,),
        tier=SourceTier.P0,
        required_env=("FRED_API_KEY",),
        allowed_uses=(UseScope.FULLTEXT_STORE, UseScope.LONG_TERM_ARCHIVE),
        raw_bucket="research-raw-private",
        normalized_target="research.macro_observations",
        doc_ref="TEAM_JAEIL 3.1",
        note="ALFRED 의 vintage 가 PIT 재현에 필요하다",
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
        note="공식 API 확인 전. Website 비공개 Endpoint 를 공식 API 처럼 쓰지 않는다(가이드 3.3)",
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
    "kosis": (
        "2026-07-30 실측: statisticsList.do 호출 시 {'err': '11', 'errMsg': "
        "'유효하지않은 인증KEY입니다.'}. 키가 .env 에 있으나 KOSIS 가 거부한다 - "
        "발급처(kosis.kr/openapi)에서 키 유효성과 활용 신청 상태를 확인할 것"
    ),
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
            for name in (*s.required_env, *s.optional_env):
                if not self._ENV_NAME.match(name):
                    raise ValueError(f"{s.source_id}: 환경변수명 형식 위반 {name!r}")
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
        return tuple(name for name in s.required_env if not self._env.get(name, "").strip())

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
    "LS_APP_KEY": "k" * 36,
    "LS_APP_SECRET_KEY": "s" * 32,
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

    for sid in ("krx_openapi", "bigkinds", "ecos", "kind"):
        try:
            r.require(sid)
            raise AssertionError(f"{sid} 가 사용 불가인데 통과했다")
        except SourceUnavailable as e:
            # 메시지에 원인이 들어 있어야 운영 중에 판단할 수 있다
            assert sid in str(e) and ("미확보" in str(e))
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

    # BIGKinds 는 Snippet 까지만
    r.check_use("bigkinds", UseScope.SNIPPET_STORE)
    try:
        r.check_use("bigkinds", UseScope.FULLTEXT_STORE)
        raise AssertionError("bigkinds 전문 저장이 통과했다")
    except SourceUseNotAllowed:
        pass

    # DART 는 전문·Embedding 허용
    r.check_use("opendart", UseScope.FULLTEXT_STORE)
    r.check_use("opendart", UseScope.EMBEDDING)
    print("  라이선스 Scope        OK")


def _check_blocked_domains():
    r = SourceRegistry(env=_FAKE_ENV)
    blocked = r.blocked_p0_domains()
    # 키가 없어서 지금 막힌 Domain
    assert SourceDomain.CALENDAR in blocked, "KRX 키가 없으면 CALENDAR 는 Blocked 여야 한다"
    assert SourceDomain.MACRO in blocked
    assert SourceDomain.NEWS in blocked, "BIGKinds/NAVER 둘 다 없으면 NEWS 는 Blocked 다"
    # LS 로 덮이는 Domain 은 막히지 않는다
    assert SourceDomain.REALTIME_PRICE not in blocked
    assert SourceDomain.REALTIME_QUOTE not in blocked
    # DART 로 덮이는 Domain
    assert SourceDomain.DISCLOSURE not in blocked
    assert SourceDomain.FINANCIAL not in blocked
    # INSTRUMENT_MASTER 는 LS + KRX 인데 LS 가 살아 있으므로 Blocked 가 아니다
    assert SourceDomain.INSTRUMENT_MASTER not in blocked

    # KRX 키를 채워도 서비스 이용 승인이 없으면 CALENDAR 는 여전히 Blocked 다.
    # 키 존재만으로 풀리면 안 된다 - 승인 전에 호출하면 401 이고, 그걸 빈 결과로
    # 취급하면 휴장일을 추정하게 된다.
    r2 = SourceRegistry(env={**_FAKE_ENV, "KRX_API_KEY": "krx-key"})
    assert r2.status("krx_openapi") is SourceStatus.NOT_AUTHORIZED
    assert SourceDomain.CALENDAR in r2.blocked_p0_domains(), "승인 없이 CALENDAR 가 풀렸다"

    # 승인이 떨어지면(관측 기록 제거) 풀린다 - 확장 시 회귀 방지
    saved = NOT_AUTHORIZED_OBSERVED.pop("krx_openapi")
    try:
        assert SourceDomain.CALENDAR not in r2.blocked_p0_domains()
    finally:
        NOT_AUTHORIZED_OBSERVED["krx_openapi"] = saved
    print("  Blocked Domain        OK")


def _check_scope_gate():
    """범위 밖 Source 가 P0 Blocked 를 풀지 못하는지 (2026-07-31 뉴스 API 조사 결과).

    Polygon/Finnhub/AlphaVantage/Alpaca 를 조사했을 때 넷 다 미국 전용이었다. 이런
    Source 를 P0 NEWS 로 등록하면 한국 종목 뉴스가 0건인데 NEWS Blocked 가 풀린다.
    그러면 '데이터 장애 시 신규 진입 자동 차단' 이 조용히 무너진다.
    """
    env_with_news = {**_FAKE_ENV, "FOREIGN_NEWS_API_KEY": "k"}

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
    except Exception as e:
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
