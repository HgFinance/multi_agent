#!/usr/bin/env python3
"""F11 (트레이딩본부 몫): 승인된 Strategy Signal -> OrderIntent 변환.

소유: 도현 (트레이딩본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 3.1, 4.2
      docs/HEDGE_FUND_MASTER_PLAN.md 10(의사결정 계약), 19.8(Execution Desk)

F11의 앞쪽 절반(Signal 생성)은 퀀트/백테스트본부 소관이다. 여기는 뒤쪽 절반 -
**목표 비중을 현재 포지션과 비교해 주문 후보로 바꾸는 것**만 한다.

경계:
  - 살지 말지는 여기서 정하지 않는다. 그건 이미 Signal이 정했다.
  - 주문을 보내지도 않는다. OrderIntent까지가 끝이고 Risk 심사가 다음이다.
  - 목표 비중과 현재 포지션이 같으면 주문을 만들지 않는다. 0주 주문은 주문이 아니다.

집행 파라미터(지정가 오프셋, 종목당 비중 상한)는 코드가 아니라
trading/philosophies.yaml에 있다.

자체 점검: python trading/intent_builder.py
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path
from uuid import UUID

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import (
    LOT_SIZE,
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    StrategySignal,
    TimeInForce,
    tick_size,
)

PHILOSOPHIES_PATH = Path(__file__).resolve().parent / "philosophies.yaml"


class IntentBuildError(Exception):
    """주문 후보를 만들 수 없는 경우. 조용히 None을 돌려주지 않는다.

    "만들 이유가 없다"(목표 도달)와 "만들면 안 된다"(정책 위반)는 다르다.
    전자는 None, 후자는 예외다. 뭉치면 정책 위반이 조용히 묻힌다.
    """


@dataclass(frozen=True)
class ExecutionPreset:
    """philosophies.yaml의 철학별 집행 프리셋."""

    order_type: str
    limit_offset_bps: Decimal
    max_weight: Decimal
    urgency: str


def load_presets(path: Path = PHILOSOPHIES_PATH) -> dict[str, ExecutionPreset]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        key: ExecutionPreset(
            order_type=block["execution"]["order_type"],
            limit_offset_bps=Decimal(str(block["execution"].get("limit_offset_bps", 0))),
            max_weight=Decimal(str(block["execution"]["max_weight"])),
            urgency=block["execution"].get("urgency", "medium"),
        )
        for key, block in doc["philosophies"].items()
    }


def round_to_tick(price: Decimal, side: Side) -> Decimal:
    """호가 단위에 맞춘다. 거래소가 거부할 가격을 만들지 않는다.

    체결 확률이 낮아지는 쪽으로 라운딩한다 - 매수는 내리고 매도는 올린다.
    반대로 하면 라운딩 때문에 의도보다 비싸게 사게 된다.
    """
    unit = tick_size(price)
    rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
    return (price / unit).quantize(Decimal(1), rounding=rounding) * unit


def _idempotency_key(signal: StrategySignal, delta: Decimal) -> str:
    """같은 시그널 + 같은 목표 격차면 항상 같은 키.

    시그널이 재전송돼도 OMS가 같은 Intent로 인식해 주문이 두 번 나가지 않는다.
    as_of를 넣어 다음 리밸런싱의 같은 수량은 다른 주문으로 취급한다.
    """
    raw = f"{signal.signal_id}|{signal.instrument_id}|{signal.as_of.isoformat()}|{delta}"
    return f"sig_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def build_order_intent(
    signal: StrategySignal,
    *,
    snapshot: MarketSnapshot,
    nav: Decimal,
    current_quantity: Decimal,
    preset: ExecutionPreset,
    trade_case_id: UUID,
    now: datetime,
    env: str = "paper",
    valid_for: timedelta = timedelta(minutes=30),
) -> OrderIntent | None:
    """목표 비중과 현재 포지션의 격차를 주문 후보로 바꾼다.

    None을 돌려주는 경우는 하나뿐이다 - 이미 목표에 도달해 주문할 것이 없을 때.
    정책 위반은 예외로 올린다.

    nav와 current_quantity는 회계본부(F15)의 Projection에서 온다. 여기서
    직접 계산하지 않는다 - 포지션은 체결·원장 Event에서만 만들어진다.
    """
    if not signal.is_tradable_at(now, env):
        raise IntentBuildError(
            f"주문 대상이 아닌 시그널입니다 (stage={signal.stage}, env={env}, "
            f"valid_until={signal.valid_until})"
        )
    if nav <= 0:
        raise IntentBuildError("NAV가 0 이하입니다. 목표 수량을 계산할 수 없습니다")

    reference = snapshot.mid
    if reference is None or reference <= 0:
        raise IntentBuildError("기준가를 만들 수 없는 스냅샷입니다 (bid/ask 없음)")

    # 1. 종목당 비중 상한. 전략이 상한을 넘겨 요청해도 여기서 깎는다.
    #    리스크본부 한도와 별개인 집행 계층의 자체 상한이다(둘 다 통과해야 한다).
    weight = min(signal.target_weight, preset.max_weight)

    # 2. 목표 수량. 매매 단위 미만은 버린다 - 0.3주는 주문할 수 없다.
    target_quantity = (nav * weight / reference).quantize(LOT_SIZE, rounding=ROUND_DOWN)
    delta = target_quantity - current_quantity

    if delta == 0:
        return None  # 이미 목표. 주문할 것이 없다

    side = Side.BUY if delta > 0 else Side.SELL
    quantity = abs(delta)

    # 3. Long-only. 보유 수량을 넘겨 파는 것은 공매도다 (마스터플랜 10장).
    if side is Side.SELL and quantity > current_quantity:
        raise IntentBuildError(
            f"보유({current_quantity})를 초과한 매도({quantity})입니다. open_short은 비활성입니다"
        )

    # 4. 지정가. 철학별 오프셋을 기준가에 적용하고 호가 단위에 맞춘다.
    #    오프셋 부호는 철학이 정한 방향 그대로 쓴다(추세추종 +20, 역발상 -15).
    #    매도는 부호를 뒤집어야 "덜 급하게"가 같은 의미가 된다.
    if preset.order_type != "limit":
        raise IntentBuildError(f"지원하지 않는 order_type입니다: {preset.order_type}")
    signed_offset = preset.limit_offset_bps if side is Side.BUY else -preset.limit_offset_bps
    raw_price = reference * (Decimal(1) + signed_offset / Decimal(10000))
    limit_price = round_to_tick(raw_price.quantize(Decimal(1), rounding=ROUND_HALF_UP), side)
    if limit_price <= 0:
        raise IntentBuildError("지정가가 0 이하로 계산됐습니다")

    # ponytail: 상/하한가(philosophies.yaml의 price_limit_pct) 검사는 안 넣었다.
    #           기준가가 전일 종가인데 MarketSnapshot에 그 필드가 없다. market-api가
    #           전일 종가를 주면 여기서 걸러 브로커 거부 한 번을 아낀다.

    return OrderIntent(
        trade_case_id=trade_case_id,
        fund_id=signal.fund_id,
        book_id=signal.book_id,
        strategy_id=signal.strategy_id,
        instrument_id=signal.instrument_id,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
        valid_until=now + valid_for,
        snapshot=snapshot,
        idempotency_key=_idempotency_key(signal, delta),
        created_by=f"trading-intent-builder/{signal.philosophy}",
        trace_id=signal.trace_id,
        created_at=now,
    )


if __name__ == "__main__":
    from datetime import timezone
    from uuid import uuid4

    from contracts import SnapshotQuality

    now = datetime.now(timezone.utc)
    presets = load_presets()
    D = Decimal

    def signal(**over) -> StrategySignal:
        kw = {
            "strategy_id": uuid4(), "strategy_version": "v1", "fund_id": uuid4(), "book_id": uuid4(),
            "instrument_id": uuid4(), "philosophy": "momentum", "target_weight": D("0.02"),
            "stage": "paper", "as_of": now, "valid_until": now + timedelta(hours=1), "trace_id": "t1",
        }
        kw.update(over)
        return StrategySignal(**kw)

    snap = MarketSnapshot(market_snapshot_id="s1", as_of=now, bid=D("69900"), ask=D("70100"))

    def build(sig=None, *, nav="1000000000", held="0", philosophy="momentum", **over):
        return build_order_intent(
            sig or signal(philosophy=philosophy), snapshot=snap, nav=D(nav),
            current_quantity=D(held), preset=presets[philosophy],
            trade_case_id=uuid4(), now=now, **over,
        )

    def raises(fn, why):
        try:
            fn()
        except (IntentBuildError, ValueError):
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 프리셋이 yaml에서 그대로 온다 (튜닝값은 코드에 없다)
    assert set(presets) == {"trend_following", "value", "momentum", "mean_reversion"}
    assert presets["value"].limit_offset_bps == D("-10")
    assert presets["value"].max_weight == D("0.05")

    # 2. 목표 비중 -> 수량. mid 70000, NAV 10억, 2% -> 285주 (소수점 버림)
    i = build()
    assert i is not None
    assert i.side is Side.BUY and i.quantity == D("285"), i.quantity
    # mid 70000 +10bps = 70070 -> 호가단위 100, 매수는 내림 -> 70000
    assert i.limit_price == D("70000"), i.limit_price

    # 3. 이미 목표에 도달했으면 주문을 만들지 않는다
    assert build(held="285") is None, "0주 주문이 만들어졌다"

    # 4. 초과 보유는 매도로 줄인다
    sell = build(held="300")
    assert sell.side is Side.SELL and sell.quantity == D("15")
    # 매도는 오프셋 반전: 70000 -10bps = 69930 -> 매도는 올림 -> 70000
    assert sell.limit_price == D("70000"), sell.limit_price

    # 5. Long-only - 목표 0은 전량 청산이지 공매도가 아니다
    zero_target = build(signal(target_weight=D("0")), held="100")
    assert zero_target.side is Side.SELL and zero_target.quantity == D("100"), "전량 청산 실패"
    assert build(signal(target_weight=D("0")), held="0") is None, "보유 0에 청산 주문이 났다"
    # 공매도를 막는 실제 방어선은 계약의 target_weight >= 0 이다.
    # build_order_intent의 초과 매도 가드는 그 덕분에 도달 불가능한 이중 방어다.
    raises(lambda: StrategySignal(**{**signal().model_dump(), "target_weight": D("-0.01")}),
           "음수 목표 비중(공매도)")

    # 6. 집행 계층 비중 상한이 전략 요청을 깎는다
    capped = build(signal(target_weight=D("0.90")))
    assert capped.quantity == D("428"), capped.quantity  # max_weight 0.03에서 잘림

    # 7. 승격 안 된 전략의 시그널은 주문이 되지 않는다 (마스터플랜 23장)
    raises(lambda: build(signal(stage="shadow")), "shadow 전략 시그널")
    raises(lambda: build(signal(stage="research")), "research 전략 시그널")
    raises(lambda: build(signal(stage="live")), "live 시그널을 paper 환경에서 사용")
    raises(lambda: build(signal(valid_until=now - timedelta(minutes=1))), "만료된 시그널")

    # 8. 시세 품질이 나쁘면 Intent 자체가 안 만들어진다 (계약 계층 방어)
    stale = MarketSnapshot(market_snapshot_id="s2", as_of=now, bid=D("69900"),
                           ask=D("70100"), quality=SnapshotQuality.STALE)
    raises(
        lambda: build_order_intent(signal(), snapshot=stale, nav=D("1000000000"),
                                   current_quantity=D("0"), preset=presets["momentum"],
                                   trade_case_id=uuid4(), now=now),
        "stale 시세로 주문 후보 생성",
    )
    raises(lambda: build(nav="0"), "NAV 0")

    # 9. 멱등성 - 같은 시그널을 두 번 받아도 같은 키
    s = signal()
    a = build(s)
    b = build(s)
    assert a.idempotency_key == b.idempotency_key, "재전송이 다른 주문이 됐다"
    assert build(signal()).idempotency_key != a.idempotency_key, "다른 시그널이 같은 키"

    # 10. 지정가는 항상 호가 단위에 맞는다 - 거래소가 거부할 가격을 만들지 않는다
    from contracts import is_valid_tick
    for phil in presets:
        for held in ("0", "500"):
            out = build(signal(philosophy=phil), held=held, philosophy=phil)
            if out is not None:
                assert is_valid_tick(out.limit_price), f"{phil}/{held}: {out.limit_price}"

    print("ok - Intent Builder 10개 영역 점검 통과")
