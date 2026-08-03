#!/usr/bin/env python3
"""F25: Corporate Action - 배당·분할·종목코드 변경의 원장 반영.

소유: 도현 (회계·포트폴리오본부)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md 4절 F25
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 8.2
        "Corporate Action은 Announcement가 아니라 실제 Effective Event로 Posting한다"
      departments/05-accounting-portfolio/hermes/config.yaml 백로그 3번

이 모듈이 강제하는 것 세 가지.

1. **공시(Announcement)로 분개하지 않는다.** 발표된 배당은 취소되거나 금액이
   바뀐다. `EFFECTIVE`가 아니면 거부하고, Effective 시각이 미래여도 거부한다.
   `corporate-actions-valuation-agent` 프롬프트의 "never post a final entry from
   an incomplete notice"를 코드로 옮긴 것이다.
2. **선택형(Elective) Action은 승인 없이 못 넘어간다.** 유상증자 청약처럼
   운용자가 고르는 사건은 `approval_id` 없이는 거부한다. 의무형(Mandatory)만
   자동 반영된다.
3. **분할·종목변경은 가치를 만들지 않는다.** 수량과 단가만 바뀌고 취득원가
   총액은 그대로다. 그래서 차변·대변이 같은 금액인 유가증권 대체 분개로 낸다 -
   Position은 바뀌고 NAV는 안 바뀐다.

자체 점검: python departments/05-accounting-portfolio/corporate_actions/corporate_actions.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "ledger"))

from ledger import (
    CASH,
    REALIZED_PNL,
    SECURITIES,
    TAX_EXPENSE,
    ZERO,
    Journal,
    JournalLine,
    Ledger,
    Position,
)


class ActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPLIT = "SPLIT"                  # 액면분할. ratio > 1
    SYMBOL_CHANGE = "SYMBOL_CHANGE"  # 종목코드 변경, 합병 후 존속법인 승계 등


class ActionStatus(StrEnum):
    """공시 -> 확정 -> 발효. 분개는 EFFECTIVE에서만 일어난다."""

    ANNOUNCED = "ANNOUNCED"    # 공시만 됨. 금액·비율이 바뀔 수 있다
    CONFIRMED = "CONFIRMED"    # 조건 확정. 아직 발효 전
    EFFECTIVE = "EFFECTIVE"    # 실제 발효. 이때만 Posting한다
    CANCELLED = "CANCELLED"


class CorporateActionError(Exception):
    """Corporate Action을 반영할 수 없는 경우. 부분 반영하지 않는다."""


@dataclass(frozen=True)
class CorporateAction:
    """참조 데이터가 준 기업행위 한 건.

    우리가 만들지 않는다 - `reference-api`가 준 것을 받아 쓴다(팀 가이드 3장 148행).
    `record_date`는 이 사건의 수혜 대상을 정하는 기준일이라, 우리가 그날 몇 주를
    들고 있었는지와 대조해야 한다. 지금 보유 수량으로 배당을 계산하면
    배당락 이후 매매한 만큼 틀린다.
    """

    action_id: str
    action_type: ActionType
    instrument_id: UUID
    record_date: datetime
    effective_at: datetime
    status: ActionStatus = ActionStatus.ANNOUNCED
    mandatory: bool = True
    approval_id: str | None = None

    # CASH_DIVIDEND
    amount_per_share: Decimal = ZERO
    withholding_tax: Decimal = ZERO
    # SPLIT — 2:1 분할이면 2. 병합(역분할)은 1보다 작은 값
    ratio: Decimal = ZERO
    # SYMBOL_CHANGE
    new_instrument_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.record_date > self.effective_at:
            raise CorporateActionError(
                f"기준일({self.record_date})이 발효일({self.effective_at})보다 늦습니다"
            )
        if self.action_type is ActionType.CASH_DIVIDEND:
            if self.amount_per_share <= 0:
                raise CorporateActionError("배당금이 0 이하입니다")
            if self.withholding_tax < 0:
                raise CorporateActionError("원천징수세가 음수입니다")
        elif self.action_type is ActionType.SPLIT:
            if self.ratio <= 0:
                raise CorporateActionError(f"분할 비율이 0 이하입니다: {self.ratio}")
            if self.ratio == 1:
                raise CorporateActionError("비율 1은 아무것도 바꾸지 않습니다")
        elif self.action_type is ActionType.SYMBOL_CHANGE:
            if self.new_instrument_id is None:
                raise CorporateActionError("변경 후 종목이 없습니다")
            if self.new_instrument_id == self.instrument_id:
                raise CorporateActionError("변경 전후 종목이 같습니다")


def apply_corporate_action(
    ledger: Ledger,
    action: CorporateAction,
    position: Position,
    *,
    record_date_quantity: Decimal | None = None,
    now: datetime | None = None,
) -> Journal:
    """확정·발효된 기업행위 하나를 분개로 만든다.

    `record_date_quantity`는 기준일 보유 수량이다. 배당은 이 수량으로 계산한다 -
    현재 수량을 쓰면 배당락 이후의 매매가 배당금액에 섞인다. 생략하면 현재
    수량을 쓰되, 그건 배당락 후 거래가 없었다는 가정이다.
    """
    now = now or datetime.now(timezone.utc)

    # -- Gate 1: 공시로 분개하지 않는다 (팀 가이드 8.2) -----------------------
    if action.status is not ActionStatus.EFFECTIVE:
        raise CorporateActionError(
            f"{action.status}는 Posting 대상이 아닙니다. EFFECTIVE만 반영합니다"
        )
    if action.effective_at > now:
        raise CorporateActionError(
            f"발효일({action.effective_at})이 아직 오지 않았습니다"
        )

    # -- Gate 2: 선택형은 승인이 선행 조건 ------------------------------------
    if not action.mandatory and not action.approval_id:
        raise CorporateActionError(
            "선택형 Action은 승인(approval_id) 없이 반영할 수 없습니다"
        )

    # -- Gate 3: 대상 포지션이 있어야 한다 ------------------------------------
    if position.instrument_id != action.instrument_id:
        raise CorporateActionError("Action 종목과 포지션 종목이 다릅니다")
    if position.quantity <= 0:
        raise CorporateActionError(
            f"보유 수량이 {position.quantity}입니다. 반영할 포지션이 없습니다"
        )

    if action.action_type is ActionType.CASH_DIVIDEND:
        lines = _dividend_lines(action, position, record_date_quantity)
    elif action.action_type is ActionType.SPLIT:
        lines = _split_lines(action, position)
    else:
        lines = _symbol_change_lines(action, position)

    journal = Journal(
        journal_id=uuid4(),
        fund_id=ledger.fund_id,
        book_id=ledger.book_id,
        event_type=f"corporate_action_{action.action_type.lower()}",
        # action_id가 멱등 키다. 같은 Action이 두 번 와도 분개는 한 번만 생긴다.
        source_event_id=action.action_id,
        effective_at=action.effective_at,
        accounting_date=action.effective_at.date(),
        lines=lines,
    )
    return ledger.post(journal)


def _dividend_lines(
    action: CorporateAction, position: Position, record_date_quantity: Decimal | None
) -> list[JournalLine]:
    """현금배당.  차) 현금 + 세금비용   대) 실현손익(총액)

    원천징수세를 손익에서 빼지 않고 세금비용으로 따로 잡는다. 체결 분개에서
    수수료·세금을 손익과 분리한 것과 같은 이유다 - 섞으면 나중에 세후 성과와
    세전 알파를 나눌 수 없다.
    """
    quantity = record_date_quantity if record_date_quantity is not None else position.quantity
    if quantity <= 0:
        raise CorporateActionError(f"기준일 보유 수량이 {quantity}입니다")

    gross = quantity * action.amount_per_share
    tax = action.withholding_tax
    if tax > gross:
        raise CorporateActionError(f"원천징수세({tax})가 배당총액({gross})보다 큽니다")

    lines = [JournalLine(CASH, debit=gross - tax)] if gross > tax else []
    if tax > 0:
        lines.append(JournalLine(TAX_EXPENSE, debit=tax))
    # ponytail: 배당수익을 실현손익(4000)에 넣는다. 계정과목 최소 세트에 배당수익이
    #           없어서인데, 이러면 매매 실현손익과 배당이 한 계정에 섞여 성과기여도
    #           분해가 안 된다. 계정 4200(배당수익) 추가는 DB 담당에게 넘길 델타다.
    lines.append(JournalLine(REALIZED_PNL, credit=gross))
    return lines


def _split_lines(action: CorporateAction, position: Position) -> list[JournalLine]:
    """액면분할.  차) 유가증권(신주)   대) 유가증권(구주) — 같은 금액

    취득원가 총액은 변하지 않는다. 그래서 차·대 금액이 같고 NAV도 안 움직인다.
    바뀌는 것은 수량뿐이며, 새 평균단가는 Ledger.rebuild()가 이 분개의
    금액 ÷ 수량으로 다시 만든다 - 여기서 단가를 계산해 넘기면 나눗셈 오차가
    원장에 그대로 굳는다.
    """
    cost_basis = position.cost_basis
    if cost_basis <= 0:
        raise CorporateActionError(f"취득원가가 {cost_basis}입니다. 대체할 금액이 없습니다")

    new_quantity = position.quantity * action.ratio
    # ponytail: 단주(端株)를 그대로 소수 수량으로 남긴다. 실제로는 현금 지급되므로
    #           market-api/reference-api가 단주 대금을 주면 별도 분개가 필요하다.
    return [
        JournalLine(SECURITIES, credit=cost_basis, instrument_id=action.instrument_id,
                    quantity=-position.quantity, unit_price=position.average_cost),
        JournalLine(SECURITIES, debit=cost_basis, instrument_id=action.instrument_id,
                    quantity=new_quantity),
    ]


def _symbol_change_lines(action: CorporateAction, position: Position) -> list[JournalLine]:
    """종목코드 변경.  차) 유가증권(신 종목)   대) 유가증권(구 종목)

    수량과 원가를 그대로 옮긴다. 새 종목으로 매수한 것이 아니므로 평균단가가
    바뀌면 안 되고, 실현손익도 생기지 않는다.
    """
    cost_basis = position.cost_basis
    if cost_basis <= 0:
        raise CorporateActionError(f"취득원가가 {cost_basis}입니다. 옮길 금액이 없습니다")

    return [
        JournalLine(SECURITIES, credit=cost_basis, instrument_id=action.instrument_id,
                    quantity=-position.quantity, unit_price=position.average_cost),
        JournalLine(SECURITIES, debit=cost_basis, instrument_id=action.new_instrument_id,
                    quantity=position.quantity, unit_price=position.average_cost),
    ]


if __name__ == "__main__":
    from ledger import CAPITAL  # noqa: F401  (시산표 확인용)

    now = datetime.now(timezone.utc)
    past = now.replace(year=now.year - 1)
    fund, book = uuid4(), uuid4()
    stock, new_stock = uuid4(), uuid4()

    def raises(fn, why, exc=CorporateActionError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    def fresh_ledger() -> tuple[Ledger, Position]:
        """자본금 10억 + 100주 @70,000 보유 상태."""
        led = Ledger(fund_id=fund, book_id=book)
        led.post_capital(Decimal(1000000000), past, f"cap_{uuid4()}")
        led.post(Journal(
            journal_id=uuid4(), fund_id=fund, book_id=book,
            event_type="fill", source_event_id=f"buy_{uuid4()}",
            effective_at=past, accounting_date=past.date(),
            lines=[
                JournalLine(SECURITIES, debit=Decimal(7000000), instrument_id=stock,
                            quantity=Decimal(100), unit_price=Decimal(70000)),
                JournalLine(CASH, credit=Decimal(7000000)),
            ],
        ))
        positions, _ = led.rebuild()
        return led, positions[stock]

    def effective(**kw) -> CorporateAction:
        base = dict(action_id=f"ca_{uuid4()}", instrument_id=stock,
                    record_date=past, effective_at=past, status=ActionStatus.EFFECTIVE)
        return CorporateAction(**{**base, **kw})

    # 1. 계약 검증 — 말이 안 되는 Action은 만들어지지 않는다
    raises(lambda: effective(action_type=ActionType.CASH_DIVIDEND,
                             amount_per_share=ZERO), "배당금 0")
    raises(lambda: effective(action_type=ActionType.SPLIT, ratio=Decimal(1)),
           "비율 1 분할")
    raises(lambda: effective(action_type=ActionType.SYMBOL_CHANGE), "변경 후 종목 없음")
    raises(lambda: CorporateAction(action_id="x", action_type=ActionType.SPLIT,
                                   instrument_id=stock, record_date=now,
                                   effective_at=past, ratio=Decimal(2)),
           "기준일이 발효일보다 늦음")

    # 2. 공시만으로는 분개하지 않는다 (팀 가이드 8.2)
    led, pos = fresh_ledger()
    for bad_status in (ActionStatus.ANNOUNCED, ActionStatus.CONFIRMED, ActionStatus.CANCELLED):
        raises(lambda s=bad_status: apply_corporate_action(
            led, effective(action_type=ActionType.SPLIT, ratio=Decimal(2), status=s), pos),
            f"{bad_status} 상태로 Posting")
    assert len(led.journals) == 2, "거부됐는데 분개가 생겼다"

    # 3. 발효일이 미래면 거부한다
    future = now.replace(year=now.year + 1)
    raises(lambda: apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal(2),
        record_date=past, effective_at=future), pos), "미래 발효일")

    # 4. 선택형은 승인 없이 못 넘어간다
    raises(lambda: apply_corporate_action(led, effective(
        action_type=ActionType.CASH_DIVIDEND, amount_per_share=Decimal(500),
        mandatory=False), pos), "승인 없는 선택형")
    approved = apply_corporate_action(led, effective(
        action_type=ActionType.CASH_DIVIDEND, amount_per_share=Decimal(500),
        mandatory=False, approval_id="apr_1"), pos)
    assert approved.event_type == "corporate_action_cash_dividend"

    # 5. 현금배당 — 세후 현금이 들어오고 세금은 따로 잡힌다
    led, pos = fresh_ledger()
    _, cash_before = led.rebuild()
    div = apply_corporate_action(led, effective(
        action_type=ActionType.CASH_DIVIDEND,
        amount_per_share=Decimal(500), withholding_tax=Decimal(7700)), pos)
    positions, cash_after = led.rebuild()
    gross = Decimal(100) * Decimal(500)          # 50,000
    assert cash_after - cash_before == gross - Decimal(7700), "세후 현금이 틀리다"
    assert led.trial_balance()[TAX_EXPENSE] == Decimal(7700), "원천징수세가 안 잡혔다"
    assert led.trial_balance()[REALIZED_PNL] == -gross, "배당총액이 수익으로 안 잡혔다"
    assert positions[stock].quantity == Decimal(100), "배당이 수량을 바꿨다"
    assert sum(led.trial_balance().values()) == ZERO, "차대가 안 맞는다"

    # 6. 기준일 수량으로 계산한다 — 배당락 후 매매가 섞이면 안 된다
    led, pos = fresh_ledger()
    apply_corporate_action(led, effective(
        action_type=ActionType.CASH_DIVIDEND, amount_per_share=Decimal(500)),
        pos, record_date_quantity=Decimal(40))
    assert led.trial_balance()[REALIZED_PNL] == -Decimal(20000), \
        "현재 수량(100)으로 계산했다. 기준일 수량(40)이어야 한다"

    # 7. 원천징수세가 배당총액보다 클 수 없다
    led, pos = fresh_ledger()
    raises(lambda: apply_corporate_action(led, effective(
        action_type=ActionType.CASH_DIVIDEND, amount_per_share=Decimal(1),
        withholding_tax=Decimal(999999)), pos), "세금 > 배당")

    # 8. 액면분할 — 수량은 2배, 취득원가 총액과 NAV는 그대로
    led, pos = fresh_ledger()
    _, cash_before = led.rebuild()
    apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal(2)), pos)
    positions, cash_after = led.rebuild()
    assert positions[stock].quantity == Decimal(200), "분할 후 수량이 틀리다"
    assert positions[stock].average_cost == Decimal(35000), "평균단가가 안 반토막났다"
    assert positions[stock].cost_basis == Decimal(7000000), "취득원가 총액이 변했다"
    assert cash_after == cash_before, "분할이 현금을 움직였다"
    # 손익 계정 자체가 안 생겨야 정상이다. 0원 라인도 만들지 않는다
    assert led.trial_balance().get(REALIZED_PNL, ZERO) == ZERO, "분할로 손익이 생겼다"
    assert sum(led.trial_balance().values()) == ZERO

    # 9. 역분할(병합)도 같은 규칙 — 5주를 1주로
    led, pos = fresh_ledger()
    apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal("0.2")), pos)
    positions, _ = led.rebuild()
    assert positions[stock].quantity == Decimal(20)
    assert positions[stock].cost_basis == Decimal(7000000), "역분할이 원가를 바꿨다"

    # 10. 종목코드 변경 — 구 종목은 사라지고 신 종목이 원가를 승계한다
    led, pos = fresh_ledger()
    apply_corporate_action(led, effective(
        action_type=ActionType.SYMBOL_CHANGE, new_instrument_id=new_stock), pos)
    positions, _ = led.rebuild()
    assert stock not in positions, "구 종목이 남아 있다"
    assert positions[new_stock].quantity == Decimal(100)
    assert positions[new_stock].average_cost == Decimal(70000), "평균단가가 승계 안 됐다"
    assert led.trial_balance().get(REALIZED_PNL, ZERO) == ZERO, "종목 변경으로 손익이 생겼다"

    # 11. 멱등 — 같은 action_id가 두 번 와도 한 번만 반영된다
    led, pos = fresh_ledger()
    act = effective(action_type=ActionType.SPLIT, ratio=Decimal(2))
    first = apply_corporate_action(led, act, pos)
    before = len(led.journals)
    again = apply_corporate_action(led, act, pos)
    assert again.journal_id == first.journal_id, "같은 Action이 새 분개를 만들었다"
    assert len(led.journals) == before, "중복 분개가 생겼다"
    positions, _ = led.rebuild()
    assert positions[stock].quantity == Decimal(200), "두 번 반영돼 400주가 됐다"

    # 12. 포지션이 없거나 종목이 다르면 거부한다
    led, pos = fresh_ledger()
    raises(lambda: apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal(2)), Position(stock)),
        "보유 0인 포지션")
    raises(lambda: apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal(2),
        instrument_id=new_stock), pos), "종목 불일치")

    # 13. 정정은 Reversal로만 — 원본 분개는 남는다
    led, pos = fresh_ledger()
    j = apply_corporate_action(led, effective(
        action_type=ActionType.SPLIT, ratio=Decimal(2)), pos)
    led.reverse(j.journal_id, "발효 취소 통보")
    positions, _ = led.rebuild()
    assert j.status == "reversed", "원본을 지웠다"
    assert positions[stock].quantity == Decimal(100), "반대분개 후 수량이 안 돌아왔다"
    assert sum(led.trial_balance().values()) == ZERO

    print("ok - Corporate Action 13개 영역 점검 통과")
