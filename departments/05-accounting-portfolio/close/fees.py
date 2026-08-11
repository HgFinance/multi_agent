#!/usr/bin/env python3
"""관리보수·성과보수 발생(Accrual). **현금 지급이 아니라 발생 인식이다.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 12.4 8번("관리보수 및 성과보수 발생액 계산"),
      21.16("Management/Performance Fee와 비용 배분 검토")
      튜닝값: accounting_ops.yaml `fees` 블록

보수는 **매일 조금씩 쌓이고 나중에 지급된다.** 지급일에 한 번에 비용으로 잡으면 그날
NAV가 계단식으로 떨어지고, 그 사이 기간의 수익률이 전부 과대평가된다. 그래서
발생일에 비용을 인식하고 현금은 나중에 나간다:

    관리보수  차) 관리보수비용(5200)   대) 미지급보수(2100)
    성과보수  차) 성과보수비용(5300)   대) 미지급보수(2100)

거래 수수료(5000)와 **다른 계정을 쓴다.** 한 계정에 섞으면 TCA에서 "체결이 비쌌는지"와
"운용 보수가 비싼지"를 분리할 수 없다.

**성과보수는 고수위(High-Water Mark) 초과분에만 매긴다.** 그리고 그 고수위는
`accounting.nav_runs`의 **승인된 공식 NAV 최대값**에서 유도한다 - 별도 테이블을 두면
"승인 안 된 NAV로 세운 고수위"가 생길 수 있고, 그건 손실을 회복하기 전에 성과보수를
받는 경로다.

⚠ **보수율·기준자본은 전부 Mandate 미확정이다.** `accounting_ops.yaml`의 값은 계산
경로를 돌려보기 위한 자리표시자이며, 확정되면 그 값으로 갈아끼운다. 여기서 정하지 않는다.

자체 점검: python departments/05-accounting-portfolio/close/fees.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import yaml

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "ledger", _HERE.parent / "portfolio"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ledger import (  # noqa: E402
    CASH,
    MGMT_FEE_EXPENSE,
    FEE_PAYABLE,
    PERF_FEE_EXPENSE,
    Journal,
    JournalLine,
    Ledger,
    decimal_str,
)

OPS_PATH = _HERE.parent / "accounting_ops.yaml"
ZERO = Decimal(0)

# 통화 최소단위. KRW는 1원 미만이 없다. 다통화가 생기면 통화별로 나눈다.
QUANTUM = Decimal("1")


class FeeError(Exception):
    """보수를 계산할 수 없는 경우. 추정치로 분개하지 않는다."""


@dataclass(frozen=True)
class FeeSettings:
    enabled: bool
    management_fee_bps: Decimal
    day_count: int
    performance_fee_rate: Decimal
    performance_base: Decimal
    min_accrual: Decimal


def load_settings(ops: Mapping[str, Any] | None = None,
                  path: Path = OPS_PATH) -> FeeSettings:
    if ops is None:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ops = doc.get("fees")
    if not ops:
        raise FeeError(f"{path} 에 fees 블록이 없습니다")
    day_count = int(ops.get("day_count", 365))
    if day_count <= 0:
        raise FeeError(f"일할 분모가 0 이하입니다: {day_count}")
    rate = Decimal(str(ops.get("performance_fee_rate", 0)))
    if not (ZERO <= rate <= 1):
        raise FeeError(f"성과보수율이 0~1 범위 밖입니다: {rate}")
    bps = Decimal(str(ops.get("management_fee_bps", 0)))
    if bps < 0:
        raise FeeError(f"관리보수율이 음수입니다: {bps}")
    return FeeSettings(
        enabled=bool(ops.get("enabled", False)),
        management_fee_bps=bps,
        day_count=day_count,
        performance_fee_rate=rate,
        performance_base=Decimal(str(ops.get("performance_base", 0))),
        min_accrual=Decimal(str(ops.get("min_accrual", 0))),
    )


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def management_fee(nav: Decimal, *, days: int = 1,
                   settings: FeeSettings | None = None) -> Decimal:
    """일할 관리보수. NAV x 연율 x 경과일/365.

    **NAV가 음수면 0이다.** 자산이 마이너스인데 보수를 걷으면 부호가 뒤집혀
    미지급금이 줄어든다(= 운용사가 펀드에 돈을 주는 분개).
    """
    settings = settings or load_settings()
    if nav <= 0 or days <= 0:
        return ZERO
    annual = nav * settings.management_fee_bps / Decimal(10000)
    return _round(annual * Decimal(days) / Decimal(settings.day_count))


def performance_fee(nav: Decimal, *, high_water_mark: Decimal,
                    settings: FeeSettings | None = None) -> Decimal:
    """고수위 초과분에 대한 성과보수.

    고수위 이하면 0이다 - 손실을 회복하는 구간에서는 보수가 없다. 그게 High-Water
    Mark의 뜻이고, 회복 구간에 걷으면 같은 수익에 두 번 과금하게 된다.
    """
    settings = settings or load_settings()
    if settings.performance_fee_rate <= 0:
        return ZERO
    threshold = max(high_water_mark, settings.performance_base)
    if threshold <= 0:
        # 기준이 없는데 성과를 재면 첫날 NAV 전액이 성과가 된다.
        return ZERO
    excess = nav - threshold
    if excess <= 0:
        return ZERO
    return _round(excess * settings.performance_fee_rate)


def _accrual_journal(ledger: Ledger, *, account: str, amount: Decimal,
                     when: datetime, source_event_id: str,
                     metadata: dict[str, Any]) -> tuple[Journal, bool]:
    """(분개, 새로 쌓았는가). **`post()`는 중복이면 기존 분개를 돌려준다** -
    돌려받은 것을 새 분개로 세면 재실행이 보수를 두 배로 쌓은 것처럼 보고된다."""
    candidate = ledger._journal(   # noqa: SLF001 - 같은 부서의 분개 규칙이다
        "fee_accrual", source_event_id, when,
        [JournalLine(account, debit=amount), JournalLine(FEE_PAYABLE, credit=amount)],
        metadata=metadata,
    )
    posted = ledger.post(candidate)
    return posted, posted is candidate


def accrue(
    ledger: Ledger,
    *,
    nav: Decimal,
    accrual_date: date,
    when: datetime,
    days: int = 1,
    high_water_mark: Decimal = ZERO,
    settings: FeeSettings | None = None,
) -> dict[str, Any]:
    """하루치 보수를 발생시킨다. 만든 분개와 근거를 함께 돌려준다.

    **하루에 한 번이다.** `source_event_id`가 `mgmt_fee:<날짜>` 하나뿐이라 같은 날
    두 번 부르면 `Ledger.post()`의 멱등 검사가 두 번째를 걸러낸다 - 마감을 재실행해도
    보수가 두 배가 되지 않는다.

    최소 발생액 미만은 분개하지 않는다(0원 라인은 DB가 거부한다). 건너뛴 사실은
    `skipped`에 남는다 - 조용히 사라지면 "왜 오늘 보수가 없지"를 나중에 못 푼다.
    """
    settings = settings or load_settings()
    result: dict[str, Any] = {
        "accrual_date": accrual_date.isoformat(),
        "nav": decimal_str(nav),
        "high_water_mark": decimal_str(high_water_mark),
        "management_fee": decimal_str(ZERO),
        "performance_fee": decimal_str(ZERO),
        "journals": [],
        "skipped": [],
        "decided_by": "deterministic",
    }
    if not settings.enabled:
        result["skipped"].append("fees.enabled=false")
        return result

    mgmt = management_fee(nav, days=days, settings=settings)
    perf = performance_fee(nav, high_water_mark=high_water_mark, settings=settings)

    for label, account, amount, prefix in (
        ("management_fee", MGMT_FEE_EXPENSE, mgmt, "mgmt_fee"),
        ("performance_fee", PERF_FEE_EXPENSE, perf, "perf_fee"),
    ):
        if amount < settings.min_accrual:
            # 0원이어도 사유를 남긴다. 조용히 넘어가면 "오늘 왜 보수가 없지"를
            # 나중에 못 푼다 - 요율이 0인지, NAV가 작은지, 꺼져 있는지가 구분되지 않는다.
            result["skipped"].append(
                f"{label}={decimal_str(amount)} < min_accrual "
                f"{decimal_str(settings.min_accrual)}")
            continue
        journal, is_new = _accrual_journal(
            ledger, account=account, amount=amount, when=when,
            source_event_id=f"{prefix}:{accrual_date.isoformat()}",
            metadata={
                "fee_type": label,
                "nav": decimal_str(nav),
                "days": days,
                "rate": decimal_str(
                    settings.management_fee_bps if label == "management_fee"
                    else settings.performance_fee_rate),
                "high_water_mark": decimal_str(high_water_mark),
                "mandate_confirmed": False,   # 요율이 Mandate 미확정이라는 사실을 분개에 남긴다
            },
        )
        result[label] = decimal_str(amount)
        if is_new:
            result["journals"].append(str(journal.journal_id))
        else:
            # 오늘 것이 이미 쌓여 있다. 금액은 그대로 보고하되 분개를 또 세지 않는다.
            result["skipped"].append(f"{label} 이미 발생함 ({journal.journal_id})")
    return result


def accrued_balance(ledger: Ledger) -> Decimal:
    """쌓여 있는 미지급보수. 부채라 대변 잔액이므로 부호를 뒤집는다."""
    return -ledger.trial_balance().get(FEE_PAYABLE, ZERO)


def pay_accrued(ledger: Ledger, *, amount: Decimal, when: datetime,
                reference: str) -> Journal:
    """쌓인 보수를 현금으로 지급한다. **발생과 다른 사건이다.**

      차) 미지급보수 2100    대) 현금 1000

    발생액보다 많이 지급할 수 없다 - 그건 지급이 아니라 선급이고, 선급은 자산이라
    분개가 다르다(그 계정은 아직 없다). 지급 경로가 없으면 부채가 영원히 쌓여
    NAV가 계속 눌리고, 그 상태를 아무도 이상하게 보지 않는다.
    """
    balance = accrued_balance(ledger)
    if amount <= ZERO:
        raise FeeError(f"지급액이 0 이하입니다: {decimal_str(amount)}")
    if amount > balance:
        raise FeeError(
            f"발생액 {decimal_str(balance)}보다 많은 {decimal_str(amount)}을 "
            f"지급할 수 없습니다 (선급은 미지원)"
        )
    return ledger.post(ledger._journal(
        "fee_payment", f"fee_pay:{reference}", when,
        [JournalLine(FEE_PAYABLE, debit=amount), JournalLine(CASH, credit=amount)],
        metadata={"reference": reference, "balance_before": decimal_str(balance)},
    ))


def high_water_mark(repo, fund_id: UUID, *, cur=None) -> Decimal:
    """승인된 공식 NAV의 최대값. 없으면 0.

    **Preliminary NAV로 고수위를 세우지 않는다.** 확정되지 않은 수치로 기준선을
    올리면, 그 수치가 나중에 정정될 때 이미 걷은 성과보수를 되돌려야 한다.
    """
    sql = """
        select coalesce(max(total_nav), 0) from accounting.nav_runs
         where fund_id = %s and run_type = 'OFFICIAL' and status = 'APPROVED'
    """
    if cur is not None:
        cur.execute(sql, (fund_id,))
        return Decimal(str(cur.fetchone()[0]))
    with repo.cursor() as own:
        own.execute(sql, (fund_id,))
        return Decimal(str(own.fetchone()[0]))


if __name__ == "__main__":
    from datetime import timezone
    from uuid import uuid4

    D = Decimal
    ok = lambda label: print(f"  {label:28} OK")  # noqa: E731
    now = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    today = date(2026, 8, 11)
    fund, book = uuid4(), uuid4()

    cfg = FeeSettings(enabled=True, management_fee_bps=D("200"), day_count=365,
                      performance_fee_rate=D("0.20"), performance_base=D("1000000"),
                      min_accrual=D("1"))

    def fresh() -> Ledger:
        led = Ledger(fund_id=fund, book_id=book)
        led.post_capital(D("1000000"), now, "seed")
        return led

    def raises(fn, why, exc=FeeError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 설정은 yaml에서 온다. 코드에 요율이 없다
    live = load_settings()
    assert live.management_fee_bps == D("200") and live.day_count == 365, live
    ok("설정은 yaml 소유")

    # 2. 관리보수 일할. 1,000,000 x 2% / 365 = 54.79 -> 55
    assert management_fee(D("1000000"), settings=cfg) == D("55")
    assert management_fee(D("1000000"), days=10, settings=cfg) == D("548")
    # NAV가 음수면 걷지 않는다(부호가 뒤집힌 분개를 만들지 않는다)
    assert management_fee(D("-500"), settings=cfg) == ZERO
    assert management_fee(D("1000000"), days=0, settings=cfg) == ZERO
    ok("관리보수 일할")

    # 3. 성과보수는 고수위 초과분만. 회복 구간에서는 0
    assert performance_fee(D("1200000"), high_water_mark=D("1000000"), settings=cfg) == D("40000")
    assert performance_fee(D("900000"), high_water_mark=D("1000000"), settings=cfg) == ZERO
    assert performance_fee(D("1100000"), high_water_mark=D("1200000"), settings=cfg) == ZERO
    # 고수위가 기준자본보다 낮으면 기준자본이 이긴다(첫 회차 보호)
    assert performance_fee(D("1050000"), high_water_mark=ZERO, settings=cfg) == D("10000")
    # 기준이 아예 없으면 계산하지 않는다 - 첫날 NAV 전액이 성과가 되는 것을 막는다
    no_base = FeeSettings(True, D("200"), 365, D("0.20"), ZERO, D("1"))
    assert performance_fee(D("1050000"), high_water_mark=ZERO, settings=no_base) == ZERO
    ok("성과보수 고수위")

    # 4. 발생 분개 - 비용은 차변, 미지급금은 대변. 현금은 움직이지 않는다
    led = fresh()
    out = accrue(led, nav=D("1200000"), accrual_date=today, when=now,
                 high_water_mark=D("1000000"), settings=cfg)
    assert out["management_fee"] == "66" and out["performance_fee"] == "40000", out
    tb = led.trial_balance()
    assert tb[MGMT_FEE_EXPENSE] == D("66") and tb[PERF_FEE_EXPENSE] == D("40000")
    assert -tb[FEE_PAYABLE] == D("40066"), tb
    _, cash = led.rebuild()
    assert cash == D("1000000"), "보수 발생이 현금을 움직였다"
    assert sum(tb.values()) == ZERO, "차대가 안 맞는다"
    ok("발생 = 비용/미지급금")

    # 5. 같은 날 두 번 불러도 한 번만 쌓인다 (마감 재실행 안전)
    again = accrue(led, nav=D("1200000"), accrual_date=today, when=now,
                   high_water_mark=D("1000000"), settings=cfg)
    assert again["journals"] == [], again
    assert led.trial_balance()[MGMT_FEE_EXPENSE] == D("66"), "보수가 두 배로 쌓였다"
    ok("일 1회 멱등")

    # 6. 최소 발생액 미만은 분개하지 않고 사유를 남긴다
    tiny = accrue(fresh(), nav=D("100"), accrual_date=today, when=now,
                  high_water_mark=D("999999999"), settings=cfg)
    assert tiny["journals"] == [] and any("min_accrual" in s for s in tiny["skipped"]), tiny
    ok("최소 발생액 미만")

    # 7. 꺼져 있으면 아무것도 하지 않는다 (Mandate 미확정 상태의 기본 방향)
    off = FeeSettings(False, D("200"), 365, D("0.20"), D("1000000"), D("1"))
    stopped = accrue(fresh(), nav=D("1200000"), accrual_date=today, when=now,
                     high_water_mark=ZERO, settings=off)
    assert stopped["journals"] == [] and stopped["skipped"] == ["fees.enabled=false"]
    ok("비활성 시 무동작")

    # 8. 설정이 깨지면 보수를 지어내지 않는다
    raises(lambda: load_settings({}), "빈 fees 블록")
    raises(lambda: load_settings({"day_count": 0}), "일할 분모 0")
    raises(lambda: load_settings({"performance_fee_rate": 1.5}), "성과보수율 150%")
    raises(lambda: load_settings({"management_fee_bps": -1}), "음수 관리보수율")
    ok("설정 fail-closed")

    # 9. 마감된 기간에는 보수도 못 쌓는다 (기간 잠금이 보수 경로에도 걸린다)
    from ledger import PeriodClosedError  # noqa: E402

    locked = fresh()
    locked.closed_through = today
    raises(lambda: accrue(locked, nav=D("1200000"), accrual_date=today, when=now,
                          high_water_mark=D("1000000"), settings=cfg),
           "마감 기간 보수 발생", PeriodClosedError)
    ok("마감 기간 차단")

    print("ok - 보수 발생 9개 영역 점검 통과 "
          "(발생주의, 고수위 초과분만, 일 1회 멱등, 요율은 Mandate 미확정)")
