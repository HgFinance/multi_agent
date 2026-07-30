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


class SourceStatus(StrEnum):
    """Source 사용 가능 여부. 판정은 오직 Registry 가 한다.

    KEY_MISSING 과 NOT_CONTRACTED 를 구분하는 이유 - 전자는 발급만 받으면 되고
    후자는 계약·라이선스 검토가 선행이다. 둘을 뭉치면 우선순위를 못 정한다.
    """

    AVAILABLE = "AVAILABLE"
    KEY_MISSING = "KEY_MISSING"
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
        note="요청 파라미터명은 crtfc_key. rcept_no Unique 와 정정 관계 누락 0 이 DQ 기준(가이드 8.2)",
    ),
    SourceSpec(
        source_id="krx_openapi",
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
        source_id="ecos",
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
        display_name="공매도·대차·대주",
        domains=(SourceDomain.MARKET_STATE,),
        tier=SourceTier.P1,
        contracted=False,
        doc_ref="TEAM_JAEIL 3.2",
        note="Long/Short 전략의 required_data_product_ids 에 필요. 공식·계약 Feed 확보가 도입 조건",
    ),
    SourceSpec(
        source_id="consensus",
        display_name="Consensus·실적 추정치",
        domains=(SourceDomain.FINANCIAL,),
        tier=SourceTier.P1,
        contracted=False,
        doc_ref="TEAM_JAEIL 3.2",
        note="기계 수집·모델 입력 권한이 있는 Vendor 계약이 도입 조건",
    ),
)


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
        missing = self.missing_env(source_id)
        return SourceStatus.KEY_MISSING if missing else SourceStatus.AVAILABLE

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
        """
        blocked = []
        for d in SourceDomain:
            p0 = [s for s in self.by_domain(d) if s.tier is SourceTier.P0]
            if p0 and not any(self.is_available(s.source_id) for s in p0):
                blocked.append(d)
        return tuple(blocked)

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

    # 키를 채우면 Blocked 가 풀려야 한다 - 확장 시 회귀 방지
    r2 = SourceRegistry(env={**_FAKE_ENV, "KRX_API_KEY": "krx-key"})
    assert SourceDomain.CALENDAR not in r2.blocked_p0_domains()
    print("  Blocked Domain        OK")


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
    _check_rate_limit_not_invented()
    print("Registry 6개 영역 통과\n")
    print(SourceRegistry().report())
