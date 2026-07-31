#!/usr/bin/env python3
"""Sprint J1/F02·F03: LS 실시간 구독 계획 - 주식·선물·옵션, 국내·해외.

소유: 재일 (리서치본부)
근거: docs/06-integrations/ls-openapi/03-stock/16-9a2800c3.md      ([주식] 실시간)
      docs/06-integrations/ls-openapi/04-derivatives/07-57936c91.md ([선물/옵션] 실시간)
      docs/06-integrations/ls-openapi/05-overseas-futures/05-3dc1c51b.md (해외선물 실시간)
      docs/06-integrations/ls-openapi/06-overseas-stock/03-0c023f96.md   (해외주식 실시간)
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F02, F03
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 1.1, 3.1, 8.2

LS 실시간은 **종목별 구독**이다. 요청에 `tr_key`로 종목/종목코드 하나를 넣고 `tr_type`으로
등록/해제한다. "전 종목 구독" 같은 단일 요청은 없다. 동시 구독 상한은 무제한이다
(재일님 확인 2026-07-30, 벤더 문서에는 명시 없음).

TR 은 (시장, 자산군, 데이터종류) 조합마다 다르다. 종목이 어느 시장·자산군인지 모르면
어떤 TR 로 구독할지 정할 수 없다 - F02 Instrument Universe 가 선행 조건인 실질적 이유다.

▶ 범위 주의 (ADR 필요)
  HEDGE_FUND_CORE_PLAN.md 는 "단일 주식시장의 전 종목 실시간"을 전제로 한다. 해외주식·
  파생상품 확장은 그 범위를 넘으므로 문서 규칙상 ADR 승인 대상이다. 이 파일은 **구조만**
  만들어 두고 실제 활성화는 ScopeGate 로 막는다. TR 이 존재한다는 것과 우리가 수집해도
  된다는 것은 다른 문제다.

▶ 구성종목 출처 (2026-07-30 갱신)
  **지수 구성종목을 주는 LS TR 은 없다.** 확인한 것은 종목 마스터(t8430/t8436/t9945,
  해외 g3190/g3104), ETF 구성종목 조회(t1904), 해외선물 마스터(o3101/o3121)뿐이다.

  국내 지수는 KRX_API_KEY 가 확보되어 KRX Data Marketplace 로 정확한 구성종목을 받는다.
  미국 지수(NASDAQ100/S&P500/DJIA)는 지수 사업자 라이선스 대상이고 대체 출처가 없어
  여전히 불가다. 파생은 LS 마스터로 전체 상품을 받을 수 있어 이 제약이 없다.

  가용성은 이 파일이 판정하지 않고 Source Registry 의 키 확보 상태를 따른다
  (constituent_source_available). 같은 사실을 두 곳에 두면 키가 들어와도 한쪽만
  갱신되는 드리프트가 생기기 때문이다. UniverseSpec 은 출처가 없으면 거부한다.

자체 점검: python departments/01-research/collectors/subscription_plan.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

PLAN_VERSION = "research-subscription-plan-v2"


class AssetClass(StrEnum):
    """자산군. TR 을 고르는 축이고 계약·정산 규칙도 여기서 갈린다."""

    EQUITY = "EQUITY"
    INDEX_FUTURE = "INDEX_FUTURE"
    INDEX_OPTION = "INDEX_OPTION"
    STOCK_FUTURE = "STOCK_FUTURE"
    OVERSEAS_FUTURE = "OVERSEAS_FUTURE"
    OVERSEAS_OPTION = "OVERSEAS_OPTION"


class Venue(StrEnum):
    """거래 시장. 해외주식은 LS 가 GSC/GSH 하나로 처리하므로 NASDAQ/NYSE 를 나누지 않는다.

    나눠야 할 필요가 생기면(거래시간·휴장일이 다르므로 언젠가 생긴다) reference 쪽
    속성으로 분리한다. TR 선택에는 지금 불필요하다.
    """

    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"
    KONEX = "KONEX"
    KRX_DERIV = "KRX_DERIV"
    KRX_NIGHT = "KRX_NIGHT"
    US_EQUITY = "US_EQUITY"
    OVERSEAS_DERIV = "OVERSEAS_DERIV"


class ProductGroup(StrEnum):
    """상품군. 해외선물은 한 TR(OVC/OVH)로 들어오지만 상품군마다 성격이 완전히 다르다.

    왜 구분하는가 - 계약단위(승수), 최소가격변동, 증거금, 만기·롤 주기, 거래시간이
    상품군마다 다르다. 하나로 뭉치면 리스크 계산과 Feature 정규화가 조용히 틀어진다.
    주식(EQUITY)에는 적용하지 않는다.

    ▶ LS 마스터(o3101/o3121) 응답의 어느 필드로 이 분류를 채울지는 아직 미확인이다.
      응답 필드를 확인한 뒤 매핑 표를 여기에 붙인다. 그때까지 추정 매핑을 넣지 않는다.
    """

    RATES = "RATES"            # 국채·금리 (T-Note, T-Bond, Euro-Bund 등)
    EQUITY_INDEX = "EQUITY_INDEX"  # 주가지수 (E-mini S&P, Nasdaq, Nikkei 등)
    ENERGY = "ENERGY"          # 원유, 천연가스
    METAL = "METAL"            # 금, 은, 구리
    AGRICULTURE = "AGRICULTURE"  # 곡물, 소프트
    CURRENCY = "CURRENCY"      # 통화선물 (외환)
    CRYPTO = "CRYPTO"          # 상장 암호화폐 선물
    UNCLASSIFIED = "UNCLASSIFIED"  # 마스터에서 분류를 못 읽은 경우. 추정하지 않는다


class DataKind(StrEnum):
    TICK = "TICK"
    QUOTE = "QUOTE"


class SubscribeAction(StrEnum):
    """tr_type 값.

    ▶ 미확인 - 수집 문서에는 `tr_type | 거래 Type | String | 1` 까지만 있고 실제 코드 값이
      없다. 아래는 자리표시자이며 실제 연결 전에 원문 문서나 응답으로 확인해야 한다.
    """

    REGISTER = "3"
    UNREGISTER = "4"


# 전부 수집 문서 TR 목록에서 확인한 코드다. 추측한 값이 없다.
TR_MATRIX: dict[tuple[Venue, AssetClass, DataKind], str] = {
    # [주식] 실시간 시세 - 국내
    (Venue.KOSPI, AssetClass.EQUITY, DataKind.TICK): "S3_",
    (Venue.KOSPI, AssetClass.EQUITY, DataKind.QUOTE): "H1_",
    (Venue.KOSDAQ, AssetClass.EQUITY, DataKind.TICK): "K3_",
    (Venue.KOSDAQ, AssetClass.EQUITY, DataKind.QUOTE): "HA_",
    # [해외주식] 실시간 시세 - 미국
    (Venue.US_EQUITY, AssetClass.EQUITY, DataKind.TICK): "GSC",
    (Venue.US_EQUITY, AssetClass.EQUITY, DataKind.QUOTE): "GSH",
    # [선물/옵션] 실시간 시세 - 국내
    (Venue.KRX_DERIV, AssetClass.INDEX_FUTURE, DataKind.TICK): "FC9",
    (Venue.KRX_DERIV, AssetClass.INDEX_FUTURE, DataKind.QUOTE): "FH9",
    (Venue.KRX_DERIV, AssetClass.INDEX_OPTION, DataKind.TICK): "OC0",
    (Venue.KRX_DERIV, AssetClass.INDEX_OPTION, DataKind.QUOTE): "OH0",
    (Venue.KRX_DERIV, AssetClass.STOCK_FUTURE, DataKind.TICK): "JC0",
    (Venue.KRX_DERIV, AssetClass.STOCK_FUTURE, DataKind.QUOTE): "JH0",
    # KRX 야간파생. 체결 TR 이 DC0 와 C02 로 나뉘어 있어 하나를 고를 근거가 문서에
    # 없다. 확인 전까지 등록하지 않는다 - 잘못 고르면 조용히 다른 상품을 받는다.
    # 해외선물/해외옵션
    (Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_FUTURE, DataKind.TICK): "OVC",
    (Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_FUTURE, DataKind.QUOTE): "OVH",
    (Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_OPTION, DataKind.TICK): "WOC",
    (Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_OPTION, DataKind.QUOTE): "WOH",
}

# WebSocket 접속 경로가 시장별로 다르다. 한 소켓에 다 붙이지 않는다.
WEBSOCKET_PATH: dict[Venue, str] = {
    Venue.KOSPI: "/websocket/stock",
    Venue.KOSDAQ: "/websocket/stock",
    Venue.KONEX: "/websocket/stock",
    Venue.KRX_DERIV: "/websocket/futureoption",
    Venue.KRX_NIGHT: "/websocket/futureoption",
    Venue.US_EQUITY: "/websocket/overseas-stock",
    Venue.OVERSEAS_DERIV: "/websocket/overseas-futureoption",
}


class ScopeStatus(StrEnum):
    """문서 범위 상태. TR 존재와 수집 허용은 다른 문제다."""

    IN_SCOPE = "IN_SCOPE"
    ADR_REQUIRED = "ADR_REQUIRED"


# CORE_PLAN 은 "단일 주식시장"을 전제한다. 국내 주식만 범위 안이다.
VENUE_SCOPE: dict[Venue, ScopeStatus] = {
    Venue.KOSPI: ScopeStatus.IN_SCOPE,
    Venue.KOSDAQ: ScopeStatus.IN_SCOPE,
    Venue.KONEX: ScopeStatus.IN_SCOPE,
    Venue.KRX_DERIV: ScopeStatus.ADR_REQUIRED,
    Venue.KRX_NIGHT: ScopeStatus.ADR_REQUIRED,
    Venue.US_EQUITY: ScopeStatus.ADR_REQUIRED,
    Venue.OVERSEAS_DERIV: ScopeStatus.ADR_REQUIRED,
}


class ConstituentSource(StrEnum):
    """지수 구성종목을 어디서 받는지. 이게 Universe 의 신뢰 근거다."""

    LS_INSTRUMENT_MASTER = "LS_INSTRUMENT_MASTER"   # t8430/t8436/t9945, g3190 - 전체 목록
    LS_FUTURES_MASTER = "LS_FUTURES_MASTER"         # o3101/o3121 - 해외선물·옵션 전체 상품
    LS_ETF_PDF = "LS_ETF_PDF"                       # t1904 - 지수 근사. 정확한 구성종목 아님
    KRX_INDEX = "KRX_INDEX"                         # KRX Data Marketplace - 지수 구성종목 정본
    VENDOR_LICENSED = "VENDOR_LICENSED"             # 지수 사업자 라이선스 - 미계약
    MANUAL_SEED = "MANUAL_SEED"                     # 손으로 넣은 목록. as_of 와 출처를 남긴다


# 구성종목 출처를 Source Registry 의 source_id 에 연결한다.
#
# 가용성을 여기 하드코딩하지 않는 이유 - Registry 가 이미 API Key 확보 상태를 판정하는데
# 같은 사실을 두 곳에 두면 키가 들어와도 한쪽만 갱신돼 드리프트가 생긴다. 실제로
# KRX_API_KEY 가 확보된 뒤에도 이 dict 가 False 로 남아 KOSPI200 이 계속 ETF 근사로
# 떨어지는 문제가 있었다.
#
# None 은 Registry 에 대응 Source 가 없다는 뜻이다. VENDOR_LICENSED 는 지수 사업자
# 라이선스라 우리 Registry 의 Source 가 아니고, MANUAL_SEED 는 사람이 넣는 것이다.
CONSTITUENT_SOURCE_TO_REGISTRY: dict[ConstituentSource, str | None] = {
    ConstituentSource.LS_INSTRUMENT_MASTER: "ls_openapi_rest",
    ConstituentSource.LS_FUTURES_MASTER: "ls_openapi_rest",
    ConstituentSource.LS_ETF_PDF: "ls_openapi_rest",
    ConstituentSource.KRX_INDEX: "krx_openapi",
    ConstituentSource.VENDOR_LICENSED: None,
    ConstituentSource.MANUAL_SEED: None,
}

# Registry 에 없는 출처의 가용성. 사람이 넣는 MANUAL_SEED 만 True 다.
_NON_REGISTRY_AVAILABLE: dict[ConstituentSource, bool] = {
    ConstituentSource.VENDOR_LICENSED: False,   # 지수 사업자 미계약
    ConstituentSource.MANUAL_SEED: True,
}


def constituent_source_available(source: ConstituentSource) -> bool:
    """Source Registry 의 키 확보 상태로 구성종목 출처 가용성을 판정한다."""
    source_id = CONSTITUENT_SOURCE_TO_REGISTRY.get(source)
    if source_id is None:
        return _NON_REGISTRY_AVAILABLE.get(source, False)

    # 지연 import - Registry 는 .env 를 읽으므로 모듈 로드 시점에 붙이지 않는다.
    from source_registry import SourceRegistry

    return SourceRegistry().is_available(source_id)


class SubscriptionNotSupported(RuntimeError):
    """해당 (시장, 자산군, 데이터종류)를 구독할 TR 이 문서에 없다."""


class SubscriptionBudgetExceeded(RuntimeError):
    """우리가 건 구독 예산 초과. LS 자체 상한은 무제한이다."""


class ScopeNotApproved(RuntimeError):
    """문서 범위를 넘는 시장. ADR 승인 없이 수집하지 않는다."""


class ConstituentsUnavailable(RuntimeError):
    """구성종목 출처가 없다. 추정 목록으로 Universe 를 만들지 않는다."""


@dataclass(frozen=True)
class UniverseSpec:
    """명명된 Universe 정의. 구성종목 목록 자체가 아니라 '어디서 어떻게 받는지'다.

    목록을 하드코딩하지 않는 이유 - 지수 구성종목은 정기변경으로 바뀐다. 코드에 박으면
    as_of 없는 사실이 되고, PIT 재현이 깨진다(quant.universe_versions 가 as_of 로
    당시 집합을 고정하는 것과 같은 이유다).
    """

    universe_id: str
    display_name: str
    venue: Venue
    asset_class: AssetClass
    expected_size: int
    constituent_source: ConstituentSource
    fallback_source: ConstituentSource | None = None
    # 해외선물처럼 한 Universe 안에 여러 상품군이 섞이는 경우를 표현한다.
    product_groups: tuple[ProductGroup, ...] = ()
    note: str | None = None

    def resolved_source(self) -> ConstituentSource:
        """쓸 수 있는 출처를 고른다. 둘 다 없으면 예외다."""
        if constituent_source_available(self.constituent_source):
            return self.constituent_source
        if self.fallback_source and constituent_source_available(self.fallback_source):
            return self.fallback_source
        raise ConstituentsUnavailable(
            f"[{self.universe_id}] {self.display_name} 구성종목을 받을 출처가 없다. "
            f"1순위 {self.constituent_source.value}"
            + (f", 대체 {self.fallback_source.value}" if self.fallback_source else "")
            + f". {self.note or ''}".rstrip()
        )


UNIVERSES: tuple[UniverseSpec, ...] = (
    UniverseSpec(
        universe_id="kospi200",
        display_name="KOSPI 200",
        venue=Venue.KOSPI,
        asset_class=AssetClass.EQUITY,
        expected_size=200,
        constituent_source=ConstituentSource.KRX_INDEX,
        fallback_source=ConstituentSource.LS_ETF_PDF,
        note=(
            "LS 에 지수 구성종목 TR 이 없다. 대체안은 t1904 로 KODEX 200 ETF PDF 를 받는 "
            "근사이며 현금비중·괴리 때문에 지수 구성종목과 완전히 같지 않다. 근사로 쓸 때는 "
            "universe_versions 에 출처를 LS_ETF_PDF 로 기록한다"
        ),
    ),
    UniverseSpec(
        universe_id="kosdaq150",
        display_name="KOSDAQ 150",
        venue=Venue.KOSDAQ,
        asset_class=AssetClass.EQUITY,
        expected_size=150,
        constituent_source=ConstituentSource.KRX_INDEX,
        fallback_source=ConstituentSource.LS_ETF_PDF,
        note="KOSPI200 과 같은 제약. 대체 ETF 는 KODEX 코스닥150 계열",
    ),
    UniverseSpec(
        universe_id="nasdaq100",
        display_name="NASDAQ 100",
        venue=Venue.US_EQUITY,
        asset_class=AssetClass.EQUITY,
        expected_size=100,
        constituent_source=ConstituentSource.VENDOR_LICENSED,
        note="Nasdaq 지수 구성종목은 라이선스 대상. QQQ ETF PDF 도 벤더 데이터다",
    ),
    UniverseSpec(
        universe_id="sp500",
        display_name="S&P 500",
        venue=Venue.US_EQUITY,
        asset_class=AssetClass.EQUITY,
        expected_size=500,
        constituent_source=ConstituentSource.VENDOR_LICENSED,
        note="S&P Dow Jones Indices 라이선스 대상",
    ),
    UniverseSpec(
        universe_id="djia30",
        display_name="Dow Jones Industrial Average",
        venue=Venue.US_EQUITY,
        asset_class=AssetClass.EQUITY,
        expected_size=30,
        constituent_source=ConstituentSource.VENDOR_LICENSED,
        note="S&P500 에 대부분 포함되므로 합집합에서 중복 제거 필요",
    ),
    # --- 파생. 지수 구성종목과 달리 LS 마스터로 전체 목록을 받을 수 있다 ---
    UniverseSpec(
        universe_id="krx_index_deriv",
        display_name="KRX 지수선물·옵션 (KOSPI200)",
        venue=Venue.KRX_DERIV,
        asset_class=AssetClass.INDEX_FUTURE,
        expected_size=0,  # 만기·행사가에 따라 변동. 마스터 조회 결과가 기준이다
        constituent_source=ConstituentSource.LS_INSTRUMENT_MASTER,
        product_groups=(ProductGroup.EQUITY_INDEX,),
        note="t8467(지수선물마스터)/t8433(지수옵션마스터). 옵션은 행사가 x 만기로 종목 수가 크다",
    ),
    UniverseSpec(
        universe_id="overseas_futures",
        display_name="해외선물 전 상품군",
        venue=Venue.OVERSEAS_DERIV,
        asset_class=AssetClass.OVERSEAS_FUTURE,
        expected_size=0,  # o3101 마스터 조회 결과가 기준
        constituent_source=ConstituentSource.LS_FUTURES_MASTER,
        product_groups=(
            ProductGroup.RATES,
            ProductGroup.EQUITY_INDEX,
            ProductGroup.ENERGY,
            ProductGroup.METAL,
            ProductGroup.AGRICULTURE,
            ProductGroup.CURRENCY,
        ),
        note=(
            "o3101 해외선물마스터조회로 전체 상품을 받는다 - 국채·금리, 주가지수, 에너지, "
            "금속, 농산물, 통화가 한 TR(OVC/OVH)로 들어오지만 계약단위·증거금·만기·거래시간이 "
            "상품군마다 다르므로 ProductGroup 으로 갈라 저장한다. 마스터 응답의 어느 필드가 "
            "상품군인지는 미확인이라 UNCLASSIFIED 로 두고 추정하지 않는다"
        ),
    ),
    UniverseSpec(
        universe_id="overseas_options",
        display_name="해외옵션 전 상품군",
        venue=Venue.OVERSEAS_DERIV,
        asset_class=AssetClass.OVERSEAS_OPTION,
        expected_size=0,
        constituent_source=ConstituentSource.LS_FUTURES_MASTER,
        product_groups=(ProductGroup.RATES, ProductGroup.EQUITY_INDEX, ProductGroup.ENERGY),
        note="o3121 해외선물옵션 마스터 조회. 행사가 x 만기로 종목 수가 크니 필터가 선행이다",
    ),
)


@dataclass(frozen=True)
class UniverseMember:
    """구독 대상 하나. F02 Universe Snapshot 의 한 행이다."""

    instrument_id: UUID
    provider_symbol: str
    venue: Venue
    asset_class: AssetClass = AssetClass.EQUITY
    # 거래정지·정리매매도 구독은 유지한다. 상태 변화를 관측해야 하고, 신규 주문 제외
    # 판단은 트레이딩·리스크 쪽 책임이다(F02 완료 조건).
    tradable: bool = True
    universe_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubscriptionRequest:
    tr_cd: str
    tr_key: str
    action: SubscribeAction
    instrument_id: UUID
    venue: Venue
    asset_class: AssetClass
    kind: DataKind

    @property
    def websocket_path(self) -> str:
        return WEBSOCKET_PATH[self.venue]

    def to_payload(self) -> dict:
        """LS 요청 Body. header 의 token 은 인증 계층이 채운다."""
        return {
            "header": {"tr_type": self.action.value},
            "body": {"tr_cd": self.tr_cd, "tr_key": self.tr_key},
        }


@dataclass(frozen=True)
class SubscriptionPlan:
    requests: tuple[SubscriptionRequest, ...]
    skipped: tuple[tuple[UniverseMember, str], ...] = ()

    @property
    def count(self) -> int:
        return len(self.requests)

    def by_kind(self, kind: DataKind) -> tuple[SubscriptionRequest, ...]:
        return tuple(r for r in self.requests if r.kind == kind)

    def by_socket(self) -> dict[str, int]:
        """소켓별 구독 수. 연결을 몇 개 열어야 하는지가 여기서 나온다."""
        out: dict[str, int] = {}
        for r in self.requests:
            out[r.websocket_path] = out.get(r.websocket_path, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> str:
        lines = [
            f"{PLAN_VERSION} - 구독 {self.count}건 "
            f"(체결 {len(self.by_kind(DataKind.TICK))} / 호가 {len(self.by_kind(DataKind.QUOTE))})"
        ]
        for path, n in self.by_socket().items():
            lines.append(f"  {path:34} {n}")
        if self.skipped:
            reasons: dict[str, int] = {}
            for _, why in self.skipped:
                reasons[why] = reasons.get(why, 0) + 1
            lines.append(f"  제외 {len(self.skipped)}건")
            for why, n in sorted(reasons.items()):
                lines.append(f"    {why}: {n}")
        return "\n".join(lines)


def tr_code_for(venue: Venue, asset_class: AssetClass, kind: DataKind) -> str:
    """단건 조회. 문서에 없는 조합은 예외다 - 비슷한 TR 로 대체하지 않는다."""
    tr = TR_MATRIX.get((venue, asset_class, kind))
    if tr is None:
        raise SubscriptionNotSupported(
            f"{venue.value} / {asset_class.value} / {kind.value} 를 구독할 TR 이 "
            f"수집 문서에 없다. docs/06-integrations/ls-openapi/ 의 TR 목록 확인"
        )
    return tr


def build_plan(
    members: list[UniverseMember],
    kinds: tuple[DataKind, ...],
    *,
    max_subscriptions: int | None = None,
    action: SubscribeAction = SubscribeAction.REGISTER,
    approved_scopes: frozenset[Venue] = frozenset(),
) -> SubscriptionPlan:
    """Universe 를 구독 요청 목록으로 바꾼다.

    approved_scopes 에 없는 ADR_REQUIRED 시장은 예외다. 호출자가 "이 시장은 승인됐다"를
    명시해야 통과한다 - 기본값이 빈 집합인 이유는 승인 여부를 코드가 추정할 수 없기 때문이다.

    max_subscriptions=None 이면 상한을 걸지 않는다(LS 동시 구독 무제한). 값을 주면
    초과 시 앞에서 자르지 않고 예외다 - 조용히 자르면 구독 실패와 체결 없음이 구분되지 않는다.
    """
    if max_subscriptions is not None and max_subscriptions <= 0:
        raise ValueError("max_subscriptions 는 1 이상이거나 None(무제한)이어야 한다")
    if not kinds:
        raise ValueError("kinds 가 비었다")

    unapproved = {
        m.venue
        for m in members
        if VENUE_SCOPE.get(m.venue) is ScopeStatus.ADR_REQUIRED and m.venue not in approved_scopes
    }
    if unapproved:
        raise ScopeNotApproved(
            f"{sorted(v.value for v in unapproved)} 는 HEDGE_FUND_CORE_PLAN 의 "
            f"'단일 주식시장' 전제를 넘는다. ADR 승인 후 approved_scopes 에 넣으세요 - "
            f"TR 이 존재하는 것과 수집해도 되는 것은 다른 문제다"
        )

    requests: list[SubscriptionRequest] = []
    skipped: list[tuple[UniverseMember, str]] = []

    for m in members:
        for kind in kinds:
            tr = TR_MATRIX.get((m.venue, m.asset_class, kind))
            if tr is None:
                skipped.append((m, f"{m.venue.value}/{m.asset_class.value}/{kind.value} TR 미문서화"))
                continue
            requests.append(
                SubscriptionRequest(
                    tr_cd=tr,
                    tr_key=m.provider_symbol,
                    action=action,
                    instrument_id=m.instrument_id,
                    venue=m.venue,
                    asset_class=m.asset_class,
                    kind=kind,
                )
            )

    if max_subscriptions is not None and len(requests) > max_subscriptions:
        raise SubscriptionBudgetExceeded(
            f"구독 {len(requests)}건이 예산 {max_subscriptions}건을 넘는다. "
            f"LS 동시 구독 자체는 무제한이므로 이 예산은 우리가 건 것이다"
        )

    return SubscriptionPlan(tuple(requests), tuple(skipped))


def universe_readiness() -> str:
    """요청받은 Universe 를 지금 만들 수 있는지 보고한다."""
    lines = [f"{PLAN_VERSION} - Universe {len(UNIVERSES)}개"]
    total_ready = 0
    for u in UNIVERSES:
        scope = VENUE_SCOPE[u.venue]
        try:
            src = u.resolved_source()
            state = f"가능 (출처 {src.value})"
            if src is ConstituentSource.LS_ETF_PDF:
                state += " ※ 근사"
            total_ready += u.expected_size
        except ConstituentsUnavailable:
            state = "불가 - 구성종목 출처 없음"
        gate = "" if scope is ScopeStatus.IN_SCOPE else "  [ADR 필요]"
        lines.append(f"  {u.universe_id:12} {u.expected_size:>4}종목  {state}{gate}")
    lines.append(f"  지금 구성 가능한 종목 합계: 약 {total_ready}개")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _m(n: int, venue: Venue, ac: AssetClass = AssetClass.EQUITY) -> list[UniverseMember]:
    return [
        UniverseMember(
            instrument_id=UUID(int=hash((venue.value, ac.value, i)) & 0xFFFFFFFF),
            provider_symbol=f"{venue.value[:2]}{i:05d}",
            venue=venue,
            asset_class=ac,
        )
        for i in range(n)
    ]


def _check_tr_matrix():
    # 국내 주식
    assert tr_code_for(Venue.KOSPI, AssetClass.EQUITY, DataKind.TICK) == "S3_"
    assert tr_code_for(Venue.KOSPI, AssetClass.EQUITY, DataKind.QUOTE) == "H1_"
    assert tr_code_for(Venue.KOSDAQ, AssetClass.EQUITY, DataKind.TICK) == "K3_"
    assert tr_code_for(Venue.KOSDAQ, AssetClass.EQUITY, DataKind.QUOTE) == "HA_"
    # 해외 주식
    assert tr_code_for(Venue.US_EQUITY, AssetClass.EQUITY, DataKind.TICK) == "GSC"
    assert tr_code_for(Venue.US_EQUITY, AssetClass.EQUITY, DataKind.QUOTE) == "GSH"
    # 국내 파생
    assert tr_code_for(Venue.KRX_DERIV, AssetClass.INDEX_FUTURE, DataKind.TICK) == "FC9"
    assert tr_code_for(Venue.KRX_DERIV, AssetClass.INDEX_OPTION, DataKind.QUOTE) == "OH0"
    assert tr_code_for(Venue.KRX_DERIV, AssetClass.STOCK_FUTURE, DataKind.TICK) == "JC0"
    # 해외 파생
    assert tr_code_for(Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_FUTURE, DataKind.TICK) == "OVC"
    assert tr_code_for(Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_OPTION, DataKind.QUOTE) == "WOH"

    # KONEX 와 KRX 야간파생은 TR 을 확정하지 않았다. 유사 TR 로 대체하지 않는다
    for venue, ac in ((Venue.KONEX, AssetClass.EQUITY), (Venue.KRX_NIGHT, AssetClass.INDEX_FUTURE)):
        try:
            tr_code_for(venue, ac, DataKind.TICK)
            raise AssertionError(f"{venue.value} 가 통과했다")
        except SubscriptionNotSupported:
            pass
    print("  TR 매트릭스 (18조합)      OK")


def _check_scope_gate():
    """ADR 없이 해외·파생을 켤 수 없다."""
    for venue, ac in (
        (Venue.US_EQUITY, AssetClass.EQUITY),
        (Venue.KRX_DERIV, AssetClass.INDEX_FUTURE),
        (Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_FUTURE),
    ):
        try:
            build_plan(_m(1, venue, ac), (DataKind.TICK,))
            raise AssertionError(f"{venue.value} 가 ADR 없이 통과했다")
        except ScopeNotApproved:
            pass

    # 국내 주식은 승인 없이 통과한다
    assert build_plan(_m(2, Venue.KOSPI), (DataKind.TICK,)).count == 2

    # 명시적으로 승인하면 통과한다
    ok = build_plan(
        _m(3, Venue.US_EQUITY), (DataKind.TICK, DataKind.QUOTE),
        approved_scopes=frozenset({Venue.US_EQUITY}),
    )
    assert ok.count == 6
    print("  범위 Gate (ADR)          OK")


def _check_requested_universe_scale():
    """재일님이 요청한 구성. 국내 350 + 미국 630(중복 포함) 규모를 확인한다."""
    kr = [*_m(200, Venue.KOSPI), *_m(150, Venue.KOSDAQ)]
    plan_kr = build_plan(kr, (DataKind.TICK, DataKind.QUOTE))
    assert plan_kr.count == 700, plan_kr.count
    assert plan_kr.by_socket() == {"/websocket/stock": 700}

    # 미국은 지수 3개 합집합. 중복 제거 전 630, 실제로는 S&P500 이 DJIA 를 거의 포함한다
    us = _m(530, Venue.US_EQUITY)
    plan_all = build_plan(
        [*kr, *us], (DataKind.TICK, DataKind.QUOTE),
        approved_scopes=frozenset({Venue.US_EQUITY}),
    )
    assert plan_all.count == 700 + 1060
    # 소켓이 갈린다 - 연결을 두 개 열어야 한다
    assert set(plan_all.by_socket()) == {"/websocket/stock", "/websocket/overseas-stock"}
    print("  요청 Universe 규모        OK")


def _check_constituents_fail_closed():
    """구성종목 출처가 없으면 Universe 를 만들지 않는다."""
    by_id = {u.universe_id: u for u in UNIVERSES}

    # 미국 지수는 라이선스 미계약이고 대체 출처도 없다 - 키와 무관하다
    for uid in ("nasdaq100", "sp500", "djia30"):
        try:
            by_id[uid].resolved_source()
            raise AssertionError(f"{uid} 가 출처 없이 통과했다")
        except ConstituentsUnavailable as e:
            assert uid in str(e)

    # 파생은 LS 마스터로 받는다 - 지수 라이선스 제약이 없다
    assert by_id["overseas_futures"].resolved_source() is ConstituentSource.LS_FUTURES_MASTER
    assert by_id["krx_index_deriv"].resolved_source() is ConstituentSource.LS_INSTRUMENT_MASTER

    # 국내 지수는 KRX 키 확보 여부에 따라 출처가 바뀐다. Registry 판정을 그대로 따른다 -
    # 가용성을 이 모듈에 하드코딩하면 키가 들어와도 근사에 머무는 드리프트가 생긴다.
    import source_registry as sr

    real = sr.SourceRegistry
    ls_env = {"LS_APP_KEY": "x", "LS_APP_SECRET_KEY": "y"}
    try:
        sr.SourceRegistry = lambda: real(env=ls_env)
        assert by_id["kospi200"].resolved_source() is ConstituentSource.LS_ETF_PDF, "KRX 없으면 근사"

        # 키만 있고 서비스 이용 승인이 없으면(NOT_AUTHORIZED) 여전히 근사다.
        # 키 존재만으로 정확한 출처로 올라가면 승인 전에 401 을 맞고, 그 빈 결과를
        # 정상으로 취급하면 잘못된 Universe 가 만들어진다.
        sr.SourceRegistry = lambda: real(env={**ls_env, "KRX_API_KEY": "k"})
        assert by_id["kospi200"].resolved_source() is ConstituentSource.LS_ETF_PDF, (
            "KRX 승인 없이 정확한 출처로 올라갔다"
        )

        # 승인이 떨어지면(관측 기록 제거) 정확한 출처를 쓴다
        saved = sr.NOT_AUTHORIZED_OBSERVED.pop("krx_openapi")
        try:
            assert by_id["kospi200"].resolved_source() is ConstituentSource.KRX_INDEX
            assert by_id["kosdaq150"].resolved_source() is ConstituentSource.KRX_INDEX
        finally:
            sr.NOT_AUTHORIZED_OBSERVED["krx_openapi"] = saved
    finally:
        sr.SourceRegistry = real
    print("  구성종목 출처 연동        OK")


def _check_derivatives_plan():
    """파생은 자산군별로 TR 이 갈린다."""
    members = [
        *_m(2, Venue.KRX_DERIV, AssetClass.INDEX_FUTURE),
        *_m(3, Venue.KRX_DERIV, AssetClass.INDEX_OPTION),
        *_m(1, Venue.OVERSEAS_DERIV, AssetClass.OVERSEAS_FUTURE),
    ]
    plan = build_plan(
        members, (DataKind.TICK, DataKind.QUOTE),
        approved_scopes=frozenset({Venue.KRX_DERIV, Venue.OVERSEAS_DERIV}),
    )
    assert plan.count == 12
    codes = {r.tr_cd for r in plan.requests}
    assert codes == {"FC9", "FH9", "OC0", "OH0", "OVC", "OVH"}, codes
    assert set(plan.by_socket()) == {
        "/websocket/futureoption", "/websocket/overseas-futureoption"
    }
    print("  파생 구독 계획            OK")


def _check_budget_and_unregister():
    assert build_plan(_m(2800, Venue.KOSPI), (DataKind.TICK, DataKind.QUOTE)).count == 5600
    try:
        build_plan(_m(50, Venue.KOSPI), (DataKind.TICK,), max_subscriptions=49)
        raise AssertionError("예산 초과가 통과했다")
    except SubscriptionBudgetExceeded:
        pass
    plan = build_plan(_m(2, Venue.KOSPI), (DataKind.QUOTE,), action=SubscribeAction.UNREGISTER)
    assert plan.requests[0].to_payload()["header"]["tr_type"] == "4"
    print("  예산/해제                 OK")


if __name__ == "__main__":
    print(f"{PLAN_VERSION} 자체 점검")
    _check_tr_matrix()
    _check_scope_gate()
    _check_requested_universe_scale()
    _check_constituents_fail_closed()
    _check_derivatives_plan()
    _check_budget_and_unregister()
    print("구독 계획 6개 영역 통과\n")
    print(universe_readiness())
