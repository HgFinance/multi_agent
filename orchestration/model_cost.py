#!/usr/bin/env python3
"""모델 비용 1벌. 단가를 임의로 정하지 않고 청구서를 토큰으로 나눈다.

## 문제

부서장 모델 `gpt-5.6-luna` 는 Codex 구독 백엔드(`https://chatgpt.com/backend-api/
codex`)로 나간다. **토큰당 청구서가 존재하지 않는다** - Hermes 자신도 그렇게
분류한다(2026-08-27 실측):

    sessions.billing_mode = 'subscription_included'
    sessions.cost_status  = 'included'      cost_source = 'none'
    hermes insights       : "Included: 452 session(s) (subscription — no provider invoice)"

여기서 흔한 실수는 비슷한 종량 모델의 공개 단가를 베껴 오는 것이다. 그렇게 넣은
숫자는 근거가 없는데도 6개월 뒤에는 아무도 출처를 묻지 않는 기준선이 된다.

## 원칙 하나로 통일한다

    비용 = 관측 토큰 × 실효단가
    실효단가 = 그 창의 청구서 ÷ 그 창의 관측 토큰

이러면 임의로 정할 값이 **월 구독료 하나**로 줄고, 그건 우리가 아는 값이다.
같은 식이 회사의 세 가지 지불 형태를 전부 덮는다:

    amortized_subscription : 부서장 Codex        - 월 구독료 ÷ 월 관측 토큰
    amortized_infra        : Worker vLLM AWQ     - 인스턴스 시간요금 ÷ 그 창 토큰
    metered                : 진짜 종량 API       - 청구서 그대로

Worker 쪽도 GPU 시간 상각이지 토큰당 청구서가 아니므로, 두 계층을 같은 식으로
두면 부서장과 Worker 의 비용을 **같은 자로** 비교할 수 있다.

## 분모를 토큰 총합으로 두는 이유

캐시 읽기가 입력의 3배인 턴이 흔하다(실측: input 23,523 / cache_read 66,048).
어느 계기가 구독 쿼터를 얼마나 먹는지는 공개돼 있지 않으므로, 가중치를 지어내는
대신 다섯 계기의 합을 분모로 쓰고 **분해값을 함께 남긴다**(TurnUsage 가 보존한다).
나중에 가중치가 밝혀지면 재수집 없이 다시 계산할 수 있다.

## 모르는 것을 0 으로 만들지 않는다

구독료가 설정되기 전 비용은 0 이 아니라 UNPRICED 다. 0 으로 채우면 "공짜로 돌고
있다"로 읽히고, 그건 예산 판단을 정확히 반대로 만든다(scorecard/cost.py 불변식 3).

자체 점검: python orchestration/model_cost.py
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

# 월 구독료. 부서장은 Codex(ChatGPT) 구독으로 도는데, 우리 기준은 **Plus 요금제**다
# (2026-08-27 결정). Plus 좌석 1개 = 월 USD 20 을 기본값으로 두고, 요금제나 좌석
# 수가 바뀌면 `.env` 의 이 두 값만 고친다 - 코드에 흩어 놓지 않는다.
#
# 이 값은 "임의로 정한 단가"가 아니라 **실제 청구액**이다. 단가는 여기서 정하지
# 않고 청구액을 관측 토큰으로 나눠서 나온다(amortized_rate). 그래서 사용량이
# 늘면 토큰당 단가가 저절로 내려간다 - 구독의 실제 경제와 같은 방향이다.
SUBSCRIPTION_USD_ENV = "CODEX_SUBSCRIPTION_USD_PER_MONTH"
SUBSCRIPTION_SEATS_ENV = "CODEX_SUBSCRIPTION_SEATS"
# ChatGPT Plus 좌석 1개의 월 요금(USD). 요금제가 바뀌면 여기가 아니라 `.env` 를
# 고치는 것이 정상 경로이고, 이 상수는 env 가 비었을 때의 기본값일 뿐이다.
PLUS_SEAT_USD_PER_MONTH = "20"
# Worker 추론 인프라(vLLM). 시간요금 × 가동시간이 그 창의 청구서다.
INFRA_USD_PER_HOUR_ENV = "WORKER_INFRA_USD_PER_HOUR"


class CostBasis(str, Enum):
    AMORTIZED_SUBSCRIPTION = "amortized_subscription"
    AMORTIZED_INFRA = "amortized_infra"
    METERED = "metered"


class CostStatus(str, Enum):
    PRICED = "PRICED"            # 닫힌 창의 청구서로 계산됨
    PROVISIONAL = "PROVISIONAL"  # 직전 창의 단가를 빌려 씀(진행 중인 달)
    UNPRICED = "UNPRICED"        # 청구서 미설정 - 0 이 아니라 모름


@dataclass(frozen=True)
class UnitRate:
    """토큰 1개당 USD. 어느 창의 무슨 청구서에서 나왔는지를 함께 나른다."""

    usd_per_token: Decimal
    basis: CostBasis
    status: CostStatus
    window_label: str = ""
    observed_tokens: int = 0
    invoice_usd: Decimal = Decimal("0")

    @property
    def provisional(self) -> bool:
        return self.status is CostStatus.PROVISIONAL


UNPRICED = UnitRate(
    usd_per_token=Decimal("0"),
    basis=CostBasis.AMORTIZED_SUBSCRIPTION,
    status=CostStatus.UNPRICED,
)


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def amortized_rate(
    *,
    invoice_usd: object,
    observed_tokens: int,
    basis: CostBasis = CostBasis.AMORTIZED_SUBSCRIPTION,
    window_label: str = "",
    provisional: bool = False,
) -> UnitRate:
    """청구서 ÷ 관측 토큰. 둘 중 하나라도 없으면 UNPRICED.

    토큰이 0 인 창은 단가가 무한대가 아니라 **계산 불가**다 - 0 으로 나눈 값을
    억지로 만들면 그 창을 인용한 모든 수치가 조용히 틀린다.
    """

    invoice = _decimal(invoice_usd)
    if invoice is None or observed_tokens <= 0:
        return UnitRate(
            usd_per_token=Decimal("0"),
            basis=basis,
            status=CostStatus.UNPRICED,
            window_label=window_label,
            observed_tokens=max(0, int(observed_tokens)),
            invoice_usd=invoice or Decimal("0"),
        )
    return UnitRate(
        usd_per_token=invoice / Decimal(int(observed_tokens)),
        basis=basis,
        status=CostStatus.PROVISIONAL if provisional else CostStatus.PRICED,
        window_label=window_label,
        observed_tokens=int(observed_tokens),
        invoice_usd=invoice,
    )


def subscription_invoice_usd(
    env: Mapping[str, str] | None = None, *, default_seat_usd: str = ""
) -> Decimal | None:
    """월 구독 청구액(좌석 수 반영). 값을 못 정하면 None -> 비용은 UNPRICED.

    `default_seat_usd` 를 주면 env 가 비었을 때 그 값을 쓴다. 배포는 Plus 기준
    (`PLUS_SEAT_USD_PER_MONTH`)을 넘기고, 라이브러리 기본값은 여전히 "모름"이다 -
    이 함수를 다른 데서 부를 때 조용히 20 달러가 끼어들면 안 된다.
    """

    source = env if env is not None else os.environ
    monthly = _decimal(source.get(SUBSCRIPTION_USD_ENV)) or _decimal(default_seat_usd)
    if monthly is None or monthly == 0:
        return None
    seats = _decimal(source.get(SUBSCRIPTION_SEATS_ENV)) or Decimal("1")
    return monthly * (seats if seats > 0 else Decimal("1"))


def cost_details(
    *, total_tokens: int | None, rate: UnitRate
) -> dict[str, float] | None:
    """Langfuse `cost_details` 로 실어 보낼 값. 값을 못 내면 None(0 을 만들지 않는다).

    Langfuse 는 모델 단가표로도 비용을 계산할 수 있지만 그 표는 **상각을 표현하지
    못한다**(토큰당 고정 단가만 받는다). 그래서 우리가 계산해 직접 싣는다 -
    2026-08-27 실측에서 우리가 준 값이 trace 총액까지 그대로 합산되는 것을 확인했다.
    """

    if not total_tokens or total_tokens <= 0 or rate.status is CostStatus.UNPRICED:
        return None
    return {"total": float(rate.usd_per_token * Decimal(int(total_tokens)))}


def cost_metadata(rate: UnitRate) -> dict[str, object]:
    """비용 숫자 옆에 항상 따라다녀야 하는 라벨.

    이 라벨 없이 숫자만 보면 상각치가 청구서로 읽힌다 - 그 오해는 예산 회의에서
    한 번 터지면 되돌리기 어렵다.
    """

    return {
        "cost_basis": rate.basis.value,
        "cost_provisional": rate.provisional,
    }


if __name__ == "__main__":
    # 1. 구독료가 없으면 UNPRICED 이고, 비용은 0 이 아니라 '없음'이다.
    assert subscription_invoice_usd({}) is None
    assert cost_details(total_tokens=89_802, rate=UNPRICED) is None

    # 2. 좌석 수가 반영된다.
    assert subscription_invoice_usd(
        {SUBSCRIPTION_USD_ENV: "200", SUBSCRIPTION_SEATS_ENV: "4"}
    ) == Decimal("800")
    assert subscription_invoice_usd({SUBSCRIPTION_USD_ENV: "200"}) == Decimal("200")

    # 2-1. Plus 기준(월 $20/좌석)은 **호출자가 명시할 때만** 쓰인다. 라이브러리
    #      기본값이 20 이면 이 함수를 다른 데서 부를 때 조용히 요금이 끼어든다.
    assert subscription_invoice_usd({}) is None
    assert subscription_invoice_usd(
        {}, default_seat_usd=PLUS_SEAT_USD_PER_MONTH
    ) == Decimal("20")
    assert subscription_invoice_usd(
        {SUBSCRIPTION_SEATS_ENV: "3"}, default_seat_usd=PLUS_SEAT_USD_PER_MONTH
    ) == Decimal("60")
    # env 가 있으면 env 가 이긴다(요금제를 코드가 아니라 배포가 정한다).
    assert subscription_invoice_usd(
        {SUBSCRIPTION_USD_ENV: "200"}, default_seat_usd=PLUS_SEAT_USD_PER_MONTH
    ) == Decimal("200")

    # 3. 실측 창으로 상각한다: 24일간 376,485,465 토큰(hermes insights 실측).
    rate = amortized_rate(
        invoice_usd="200", observed_tokens=376_485_465, window_label="2026-08"
    )
    assert rate.status is CostStatus.PRICED
    # 그 창의 실측 턴 1건(총 89,802 토큰)의 몫. 그 달 토큰의 0.0239% 이므로
    # $200 기준 약 $0.0477 이다 - 부서장 턴 하나의 실제 크기가 이 정도라는 뜻이고,
    # 이 수치가 "부서장 질의를 얼마나 자주 돌릴 수 있나"의 기준선이 된다.
    turn_cost = cost_details(total_tokens=89_802, rate=rate)
    assert turn_cost is not None
    assert 0.047 < turn_cost["total"] < 0.048, turn_cost
    assert cost_metadata(rate) == {
        "cost_basis": "amortized_subscription", "cost_provisional": False
    }

    # 4. 진행 중인 달은 직전 달 단가를 빌려 쓰고 반드시 provisional 로 표시된다.
    borrowed = amortized_rate(
        invoice_usd="200", observed_tokens=376_485_465,
        window_label="2026-09", provisional=True,
    )
    assert borrowed.status is CostStatus.PROVISIONAL
    assert cost_metadata(borrowed)["cost_provisional"] is True
    assert borrowed.usd_per_token == rate.usd_per_token

    # 5. 토큰 0 인 창은 단가를 만들지 않는다(0 으로 나누지 않는다).
    empty = amortized_rate(invoice_usd="200", observed_tokens=0)
    assert empty.status is CostStatus.UNPRICED
    assert cost_details(total_tokens=100, rate=empty) is None

    # 6. Worker 인프라 상각도 같은 식으로 성립한다(부서장과 같은 자로 비교된다).
    infra = amortized_rate(
        invoice_usd=Decimal("1.006") * 24,   # 인스턴스 시간요금 × 가동시간
        observed_tokens=12_000_000,
        basis=CostBasis.AMORTIZED_INFRA,
        window_label="2026-08-27",
    )
    assert infra.basis is CostBasis.AMORTIZED_INFRA
    assert infra.status is CostStatus.PRICED
    assert cost_metadata(infra)["cost_basis"] == "amortized_infra"

    # 7. 음수·쓰레기 청구서는 값을 만들지 않는다.
    for junk in ("-5", "abc", None, ""):
        assert amortized_rate(invoice_usd=junk, observed_tokens=100).status is (
            CostStatus.UNPRICED
        )

    print("ok - 모델 비용 상각 계약 점검 통과")
