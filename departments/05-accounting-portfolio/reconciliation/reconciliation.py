#!/usr/bin/env python3
"""Sprint D2 (잔여): OMS / Fill / Ledger Reconciliation.

소유: 도현 (회계/포트폴리오본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.5, 8.4, 11(DoD 7번)
      docs/HEDGE_FUND_MASTER_PLAN.md 11.2(브로커-내부 불일치 시 Kill Switch), 12.4

대사는 "맞다"를 증명하는 작업이 아니라 "다르다"를 숨기지 않는 작업이다.
Break는 자동 생성되고 임의로 숨겨지지 않는다 (팀 가이드 DoD 7번).

매칭 순서 (팀 가이드 4.5):
  1. Broker Fill ID 완전 일치
  2. Client Order ID 완전 일치
  3. 종목·방향·수량·가격·시간창 속성 매칭
  4. Fuzzy는 후보만 제시하고 자동 확정하지 않는다
  5. Material Break는 리스크본부와 QA로 전달한다

가장 위험한 것은 "브로커에는 있는데 우리에겐 없는 체결"이다. 모르는 포지션을
들고 있다는 뜻이므로 무조건 material이다.

자체 점검: python departments/05-accounting-portfolio/reconciliation/reconciliation.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

# 트레이딩본부 contracts.py를 직접 참조한다. contracts/가 아직 공용 최상위 경계로
# 분리되지 않았기 때문이며(REPOSITORY_DEPARTMENT_STRUCTURE.md 4절), 그 전까지는
# 본부 간 의존 방향이 이 파일에 그대로 남는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"))

from contracts import Side  # noqa: E402

ZERO = Decimal("0")
DEFAULT_TIME_WINDOW = timedelta(minutes=5)
CASH_TOLERANCE = Decimal("1")  # 원 단위 반올림 차이는 Break로 올리지 않는다


class MatchMethod(StrEnum):
    BROKER_ID = "broker_id"
    CLIENT_ORDER_ID = "client_order_id"
    ATTRIBUTE = "attribute"
    FUZZY_CANDIDATE = "fuzzy_candidate"
    UNMATCHED = "unmatched"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MATERIAL = "material"


# 자동 확정 가능한 매칭. fuzzy는 여기 없다 - 사람이나 상위 절차가 확정한다.
CONFIRMED_METHODS = frozenset(
    {MatchMethod.BROKER_ID, MatchMethod.CLIENT_ORDER_ID, MatchMethod.ATTRIBUTE}
)


@dataclass(frozen=True)
class FillRecord:
    """내부와 외부(브로커) 체결을 같은 모양으로 놓고 비교한다."""

    instrument_id: UUID
    side: Side
    quantity: Decimal
    price: Decimal
    event_time: datetime
    broker_fill_id: str | None = None
    client_order_id: str | None = None
    fee: Decimal = ZERO
    tax: Decimal = ZERO
    ref: str = ""  # 내부 fill_id 또는 외부 명세서 라인 참조


@dataclass
class ReconItem:
    match_method: MatchMethod
    internal_ref: str | None = None
    external_ref: str | None = None
    internal_value: Decimal | None = None
    external_value: Decimal | None = None
    detail: str = ""
    # 짝은 찾았지만 값이 어긋난 경우. internal_value/external_value는 수량이라
    # 가격·비용 불일치는 difference로 드러나지 않는다. Break를 만들 때 함께 세운다.
    has_discrepancy: bool = False

    @property
    def difference(self) -> Decimal | None:
        if self.internal_value is None or self.external_value is None:
            return None
        return self.internal_value - self.external_value

    @property
    def is_confirmed(self) -> bool:
        return (
            self.match_method in CONFIRMED_METHODS
            and not self.difference
            and not self.has_discrepancy
        )


@dataclass
class Break:
    break_id: UUID
    kind: str
    severity: Severity
    detail: str
    internal_ref: str | None = None
    external_ref: str | None = None
    status: str = "open"

    @property
    def escalates(self) -> bool:
        """리스크본부와 QA로 이벤트를 보내야 하는가 (팀 가이드 4.5 5번)."""
        return self.severity is Severity.MATERIAL


@dataclass
class ReconResult:
    recon_type: str
    rule_version: str
    as_of: datetime
    items: list[ReconItem] = field(default_factory=list)
    breaks: list[Break] = field(default_factory=list)

    @property
    def result(self) -> str:
        if not self.breaks:
            return "matched"
        return "break" if all(not i.is_confirmed for i in self.items) else "partial"

    @property
    def material_breaks(self) -> list[Break]:
        return [b for b in self.breaks if b.escalates]


def _break(kind: str, severity: Severity, detail: str, **refs) -> Break:
    return Break(break_id=uuid4(), kind=kind, severity=severity, detail=detail, **refs)


# ---------------------------------------------------------------------------
# 체결 대사
# ---------------------------------------------------------------------------


def reconcile_fills(
    internal: list[FillRecord],
    external: list[FillRecord],
    as_of: datetime | None = None,
    time_window: timedelta = DEFAULT_TIME_WINDOW,
    rule_version: str = "fill-recon-v1",
) -> ReconResult:
    """내부 체결과 브로커 명세서를 대사한다."""
    as_of = as_of or datetime.now(tz=internal[0].event_time.tzinfo) if internal else as_of
    res = ReconResult("fill", rule_version, as_of or datetime.now())

    remaining = list(external)

    def take(pred) -> FillRecord | None:
        for i, cand in enumerate(remaining):
            if pred(cand):
                return remaining.pop(i)
        return None

    for own in internal:
        # 1. Broker Fill ID
        hit = take(lambda c: c.broker_fill_id and c.broker_fill_id == own.broker_fill_id)
        method = MatchMethod.BROKER_ID

        # 2. Client Order ID + 수량·가격
        if hit is None:
            hit = take(lambda c: (
                c.client_order_id and c.client_order_id == own.client_order_id
                and c.quantity == own.quantity and c.price == own.price
            ))
            method = MatchMethod.CLIENT_ORDER_ID

        # 3. 속성 매칭 (종목·방향·수량·가격 일치 + 시간창)
        if hit is None:
            hit = take(lambda c: (
                c.instrument_id == own.instrument_id and c.side == own.side
                and c.quantity == own.quantity and c.price == own.price
                and abs(c.event_time - own.event_time) <= time_window
            ))
            method = MatchMethod.ATTRIBUTE

        # 4. Fuzzy 후보 - 종목·방향만 같고 수량 또는 가격이 다르다.
        #    자동 확정하지 않는다. 후보로만 제시하고 Break를 만든다.
        if hit is None:
            hit = take(lambda c: (
                c.instrument_id == own.instrument_id and c.side == own.side
                and abs(c.event_time - own.event_time) <= time_window
            ))
            method = MatchMethod.FUZZY_CANDIDATE

        if hit is None:
            # 내부에는 있는데 브로커에 없다. 우리가 안 난 주문을 났다고 기록한 것.
            res.items.append(ReconItem(MatchMethod.UNMATCHED, internal_ref=own.ref,
                                       internal_value=own.quantity,
                                       detail="브로커 명세서에 없는 내부 체결"))
            res.breaks.append(_break(
                "internal_only_fill", Severity.MATERIAL,
                f"내부 체결 {own.ref}가 브로커에 없습니다. 포지션이 과대 계상됩니다.",
                internal_ref=own.ref,
            ))
            continue

        item = ReconItem(method, internal_ref=own.ref, external_ref=hit.ref,
                         internal_value=own.quantity, external_value=hit.quantity)
        res.items.append(item)

        if method is MatchMethod.FUZZY_CANDIDATE:
            item.detail = "후보 매칭 - 자동 확정하지 않음"
            item.has_discrepancy = True
            res.breaks.append(_break(
                "fuzzy_match", Severity.HIGH,
                f"수량/가격 불일치 후보: 내부 {own.quantity}@{own.price} vs "
                f"브로커 {hit.quantity}@{hit.price}. 사람 확인 필요.",
                internal_ref=own.ref, external_ref=hit.ref,
            ))
        elif own.price != hit.price:
            item.has_discrepancy = True
            res.breaks.append(_break(
                "price_mismatch", Severity.HIGH,
                f"체결가 불일치: 내부 {own.price} vs 브로커 {hit.price}",
                internal_ref=own.ref, external_ref=hit.ref,
            ))
        elif own.fee != hit.fee or own.tax != hit.tax:
            item.has_discrepancy = True
            res.breaks.append(_break(
                "cost_mismatch", Severity.MEDIUM,
                f"비용 불일치: 내부 fee={own.fee}/tax={own.tax} vs "
                f"브로커 fee={hit.fee}/tax={hit.tax}",
                internal_ref=own.ref, external_ref=hit.ref,
            ))

    # 브로커에는 있는데 우리에겐 없는 체결. 모르는 포지션을 들고 있다는 뜻이다.
    for orphan in remaining:
        res.items.append(ReconItem(MatchMethod.UNMATCHED, external_ref=orphan.ref,
                                   external_value=orphan.quantity,
                                   detail="내부에 없는 브로커 체결"))
        res.breaks.append(_break(
            "external_only_fill", Severity.MATERIAL,
            f"브로커 체결 {orphan.ref}가 내부에 없습니다. 미인지 포지션입니다.",
            external_ref=orphan.ref,
        ))

    return res


# ---------------------------------------------------------------------------
# 포지션·현금 대사
# ---------------------------------------------------------------------------


def reconcile_positions(
    internal: dict[UUID, Decimal],
    external: dict[UUID, Decimal],
    as_of: datetime | None = None,
    rule_version: str = "position-recon-v1",
) -> ReconResult:
    """원장 projection과 브로커 잔고를 대사한다.

    수량 불일치는 항상 material이다. 마스터플랜 11.2가 브로커와 내부 포지션이
    어긋나면 Kill Switch 대상이라고 규정한다.
    """
    res = ReconResult("position", rule_version, as_of or datetime.now())

    for instrument in sorted(set(internal) | set(external), key=str):
        own = internal.get(instrument, ZERO)
        theirs = external.get(instrument, ZERO)
        item = ReconItem(
            MatchMethod.ATTRIBUTE if own == theirs else MatchMethod.UNMATCHED,
            internal_ref=str(instrument), external_ref=str(instrument),
            internal_value=own, external_value=theirs,
        )
        res.items.append(item)
        if own != theirs:
            res.breaks.append(_break(
                "position_mismatch", Severity.MATERIAL,
                f"포지션 불일치 {instrument}: 내부 {own} vs 브로커 {theirs} (차이 {own - theirs})",
                internal_ref=str(instrument),
            ))
    return res


def reconcile_cash(
    internal: Decimal,
    external: Decimal,
    as_of: datetime | None = None,
    tolerance: Decimal = CASH_TOLERANCE,
    rule_version: str = "cash-recon-v1",
) -> ReconResult:
    """현금 대사. 반올림 수준 차이는 Break로 올리지 않는다."""
    res = ReconResult("cash", rule_version, as_of or datetime.now())
    diff = internal - external
    res.items.append(ReconItem(
        MatchMethod.ATTRIBUTE if abs(diff) <= tolerance else MatchMethod.UNMATCHED,
        internal_value=internal, external_value=external,
    ))
    if abs(diff) > tolerance:
        # 금액이 크면 material, 작으면 high. 어느 쪽이든 조용히 넘기지 않는다.
        severity = Severity.MATERIAL if abs(diff) > tolerance * 1000 else Severity.HIGH
        res.breaks.append(_break(
            "cash_mismatch", severity,
            f"현금 불일치: 내부 {internal} vs 브로커 {external} (차이 {diff})",
        ))
    return res


if __name__ == "__main__":
    from datetime import timezone

    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    stock, other = uuid4(), uuid4()

    def rec(qty, price, *, bfid=None, coid=None, ref="", t=now, fee=ZERO, tax=ZERO,
            inst=None, side=Side.BUY):
        return FillRecord(
            instrument_id=inst or stock, side=side, quantity=Decimal(qty),
            price=Decimal(price), event_time=t, broker_fill_id=bfid,
            client_order_id=coid, fee=Decimal(fee), tax=Decimal(tax), ref=ref,
        )

    # 1. Broker Fill ID 완전 일치 -> Break 없음
    r = reconcile_fills([rec("100", "70000", bfid="bf1", ref="i1")],
                        [rec("100", "70000", bfid="bf1", ref="e1")])
    assert r.result == "matched" and not r.breaks
    assert r.items[0].match_method is MatchMethod.BROKER_ID
    assert r.items[0].is_confirmed

    # 2. Broker ID가 없으면 Client Order ID로 내려간다
    r = reconcile_fills([rec("100", "70000", coid="c1", ref="i1")],
                        [rec("100", "70000", coid="c1", ref="e1")])
    assert r.items[0].match_method is MatchMethod.CLIENT_ORDER_ID and not r.breaks

    # 3. ID가 전혀 없으면 속성 + 시간창
    r = reconcile_fills([rec("100", "70000", ref="i1")],
                        [rec("100", "70000", ref="e1", t=now + timedelta(minutes=2))])
    assert r.items[0].match_method is MatchMethod.ATTRIBUTE and not r.breaks

    # 시간창을 벗어나면 속성 매칭도 실패한다
    r = reconcile_fills([rec("100", "70000", ref="i1")],
                        [rec("100", "70000", ref="e1", t=now + timedelta(hours=3))])
    assert {b.kind for b in r.breaks} == {"internal_only_fill", "external_only_fill"}

    # 4. Fuzzy는 자동 확정하지 않는다 (팀 가이드 4.5 4번)
    r = reconcile_fills([rec("100", "70000", ref="i1")],
                        [rec("99", "70000", ref="e1")])
    assert r.items[0].match_method is MatchMethod.FUZZY_CANDIDATE
    assert not r.items[0].is_confirmed, "fuzzy가 자동 확정됐다"
    assert r.breaks[0].kind == "fuzzy_match" and r.breaks[0].severity is Severity.HIGH

    # 5. 브로커에만 있는 체결 = 미인지 포지션 = material
    r = reconcile_fills([], [rec("50", "70000", bfid="bf9", ref="e9")])
    assert r.breaks[0].kind == "external_only_fill"
    assert r.breaks[0].severity is Severity.MATERIAL
    assert r.material_breaks and r.breaks[0].escalates, "리스크본부로 안 올라간다"

    # 6. 내부에만 있는 체결도 material
    r = reconcile_fills([rec("50", "70000", bfid="bf8", ref="i8")], [])
    assert r.breaks[0].kind == "internal_only_fill"
    assert r.breaks[0].severity is Severity.MATERIAL

    # 7. 체결가 불일치 - ID는 맞는데 가격이 다르다
    r = reconcile_fills([rec("100", "70000", bfid="bf2", ref="i2")],
                        [rec("100", "70100", bfid="bf2", ref="e2")])
    assert r.breaks[0].kind == "price_mismatch"
    assert not r.items[0].is_confirmed, "가격이 다른데 확정 처리됐다"

    # 8. 수수료·세금 차이는 medium (금액이 작고 정정 가능)
    r = reconcile_fills([rec("100", "70000", bfid="bf3", ref="i3", fee="1050")],
                        [rec("100", "70000", bfid="bf3", ref="e3", fee="1100")])
    assert r.breaks[0].kind == "cost_mismatch" and r.breaks[0].severity is Severity.MEDIUM

    # 9. 여러 건 중 한 건만 어긋나면 partial
    r = reconcile_fills(
        [rec("100", "70000", bfid="bf4", ref="i4"), rec("50", "71000", bfid="bf5", ref="i5")],
        [rec("100", "70000", bfid="bf4", ref="e4"), rec("50", "71500", bfid="bf5", ref="e5")],
    )
    assert r.result == "partial" and len(r.breaks) == 1

    # 10. 포지션 대사 - 불일치는 항상 material (마스터플랜 11.2)
    r = reconcile_positions({stock: Decimal("100")}, {stock: Decimal("100")})
    assert r.result == "matched"
    r = reconcile_positions({stock: Decimal("100")}, {stock: Decimal("90")})
    assert r.breaks[0].severity is Severity.MATERIAL and r.breaks[0].kind == "position_mismatch"
    # 한쪽에만 있는 종목도 잡힌다
    r = reconcile_positions({stock: Decimal("100")}, {other: Decimal("10")})
    assert len(r.breaks) == 2

    # 11. 현금 대사 - 반올림은 통과, 큰 차이는 material
    assert reconcile_cash(Decimal("1000000"), Decimal("1000000.5")).result == "matched"
    assert reconcile_cash(Decimal("1000000"), Decimal("999900")).breaks[0].severity is Severity.HIGH
    big = reconcile_cash(Decimal("1000000"), Decimal("0"))
    assert big.breaks[0].severity is Severity.MATERIAL and big.material_breaks

    # 12. 매칭된 브로커 체결은 재사용되지 않는다 (같은 건에 두 번 매칭 금지)
    r = reconcile_fills(
        [rec("100", "70000", ref="i1"), rec("100", "70000", ref="i2")],
        [rec("100", "70000", ref="e1")],
    )
    matched = [i for i in r.items if i.match_method is MatchMethod.ATTRIBUTE]
    assert len(matched) == 1, "브로커 체결 하나가 두 번 매칭됐다"
    assert any(b.kind == "internal_only_fill" for b in r.breaks)

    print("ok - 대사 12개 점검 통과")
