#!/usr/bin/env python3
"""리서치 Packet -> OrderIntent 접수 게이트 (Contract Test 포함).

소유: 도현 (트레이딩본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.2,
      docs/PROJECT_IMPLEMENTATION_STATUS.md 4.2 "재일님의 ResearchPacketV2 Fixture를
      받아 Trading API가 같은 ID를 유지한 OrderIntent를 생성하는 Contract Test"
      departments/01-research/contracts/research_v2.py (재일님 소유 계약)

**Packet은 주문이 아니다.** 리서치본부가 준 근거 묶음이고, 그게 주문이 되려면
전략(Signal)과 현재 포지션과 시세가 더 있어야 한다. 이 모듈은 그 사이에서
"이 Packet으로 이 주문을 내도 되는가"만 판정한다. 수량·가격은 F11(intent_builder)이
정하고 여기서 만들지 않는다.

**Packet을 여기서 만들지 않는다.** `research_v2.ResearchPacketV2`를 그대로 import해서
검사하므로 재일님이 계약을 바꾸면 이 파일이 같이 깨진다 - 그게 Contract Test의 목적이다.
우리가 복사본 스키마를 들고 있으면 계약이 갈라져도 아무도 모른다.

게이트 다섯 (전부 통과해야 Intent가 만들어진다):
  1. `status == PUBLISHED`. DRAFT/PARTIAL/INSUFFICIENT는 아직 근거가 아니다.
  2. Case와 종목이 Signal과 같아야 한다. 다른 Packet의 근거 위에 주문이 올라타지 않는다.
  3. Point-in-Time: `as_known_at`이 시세 시각보다 미래면 거부(마스터플랜 9.3).
  4. Long-only: 두 Outlook이 모두 negative면 매수하지 않는다.
  5. `uncalibrated`인 confidence를 수량으로 바꾸지 않는다 - 수량은 Signal의
     target_weight에서만 나온다.

자체 점검: python departments/02-trading/contracts/packet_gate.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from contracts import MarketSnapshot, Side, StrategySignal

PUBLISHED = "PUBLISHED"
NEGATIVE = "negative"


class PacketGateError(Exception):
    """Packet으로 이 주문을 낼 수 없는 경우. 통과시키지 않는다."""


@dataclass(frozen=True)
class PacketRef:
    """주문에 남길 근거 참조. **Packet 원문을 Intent에 싣지 않는다.**

    규약: Event Payload에 전체 보고서를 넣지 않고 id와 hash를 넣는다. 원문은
    리서치본부가 소유하고 우리는 어느 Packet이었는지만 가리킨다.
    """

    packet_id: str
    case_id: str
    as_known_at: datetime
    status: str


def check_packet_admissible(packet, signal: StrategySignal,
                            snapshot: MarketSnapshot) -> PacketRef:
    """이 Packet으로 이 Signal의 주문을 접수해도 되는지 판정한다.

    LLM이 판정하지 않는다 - 전부 결정론적 비교다(마스터플랜 5.9).
    """
    if packet.status != PUBLISHED:
        raise PacketGateError(
            f"PUBLISHED가 아닌 Packet으로 주문할 수 없습니다: {packet.status}")

    # 종목 동일성. Packet은 문자열 id를 쓰고 우리는 UUID를 쓴다 - 형이 다르므로
    # 문자열 비교로 뭉개지 않고 UUID로 파싱해서 비교한다. 파싱 실패도 불일치다.
    try:
        packet_instrument = UUID(str(packet.instrument_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PacketGateError(
            f"Packet의 instrument_id가 UUID가 아닙니다: {packet.instrument_id!r}. "
            "symbol이라면 reference.instrument_symbols로 먼저 해석해야 합니다"
        ) from exc
    if packet_instrument != signal.instrument_id:
        raise PacketGateError(
            f"Packet 종목({packet_instrument})과 Signal 종목({signal.instrument_id})이 다릅니다")

    # Point-in-Time. 분석 시점이 시세 시각보다 미래면 그 근거는 아직 없던 것이다.
    if packet.as_known_at > snapshot.as_of:
        raise PacketGateError(
            f"Packet 관측 시각({packet.as_known_at.isoformat()})이 시세 시각"
            f"({snapshot.as_of.isoformat()})보다 미래입니다")

    # Long-only. 두 Outlook이 다 부정적인데 매수하면 근거와 주문이 반대다.
    if signal.target_weight > 0 and (
        packet.macro_outlook.direction == NEGATIVE
        and packet.micro_outlook.direction == NEGATIVE
    ):
        raise PacketGateError(
            "Macro·Micro Outlook이 모두 negative인 Packet으로 매수할 수 없습니다")

    return PacketRef(packet_id=packet.packet_id, case_id=packet.case_id,
                     as_known_at=packet.as_known_at, status=packet.status)


if __name__ == "__main__":
    from dataclasses import replace as _replace  # noqa: F401  (미사용 - pydantic 모델이라 model_copy를 쓴다)
    from datetime import timedelta, timezone
    from decimal import Decimal
    from uuid import uuid4

    ROOT = _HERE.parents[2]
    sys.path.insert(0, str(ROOT))
    from departments.risk_qa_testkit.research_packet import make_canonical_test_packet

    D = Decimal
    now = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)

    # 동규님 testkit의 canonical Fixture를 그대로 쓴다. 우리 손으로 Packet을 지어내면
    # 재일님 계약이 바뀌어도 이 검사가 안 깨진다 - 그러면 Contract Test가 아니다.
    canonical = make_canonical_test_packet(as_known_at=now - timedelta(hours=1))
    packet = canonical.research_packet

    instrument_id = uuid4()
    canonical_packet = packet.model_copy(update={"instrument_id": str(instrument_id)})
    # 0. **동규님 canonical Fixture는 그대로는 주문이 안 된다.** status가 PARTIAL이다.
    #    이게 정상이며, 여기서 통과하기 시작하면 게이트가 죽은 것이다.
    assert canonical_packet.status == "PARTIAL", canonical_packet.status
    packet = canonical_packet.model_copy(update={"status": PUBLISHED})

    def signal(weight: str = "0.05") -> StrategySignal:
        return StrategySignal(
            strategy_id=uuid4(), strategy_version="v1", fund_id=uuid4(), book_id=uuid4(),
            instrument_id=instrument_id, philosophy="momentum",
            target_weight=D(weight), stage="paper", as_of=now,
            valid_until=now + timedelta(hours=6), trace_id="trace_gate",
        )

    def snap(as_of: datetime = now) -> MarketSnapshot:
        return MarketSnapshot(market_snapshot_id="snap_gate", as_of=as_of,
                              bid=D("70000"), ask=D("70100"))

    def rejects(fn, why: str) -> None:
        try:
            fn()
        except PacketGateError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 정상 Packet은 통과하고 근거 참조를 돌려준다
    ref = check_packet_admissible(packet, signal(), snap())
    assert ref.packet_id == packet.packet_id and ref.case_id == packet.case_id
    assert ref.status == PUBLISHED, packet.status

    # 2. **ID가 유지된다.** Packet의 case_id가 그대로 근거 참조에 남는다.
    #    이 값이 trade_case_id로 이어져야 한 Case를 끝까지 추적할 수 있다.
    assert ref.case_id == packet.case_id
    assert ref.as_known_at == packet.as_known_at

    # 3. PUBLISHED가 아니면 주문이 안 된다. 초안 근거로 주문이 나가는 경로가 없어야 한다
    rejects(lambda: check_packet_admissible(canonical_packet, signal(), snap()),
            "canonical Fixture 원본(PARTIAL)")
    for status in ("DRAFT", "PARTIAL", "INSUFFICIENT"):
        draft = packet.model_copy(update={"status": status})
        rejects(lambda p=draft: check_packet_admissible(p, signal(), snap()),
                f"{status} Packet")

    # 4. 다른 종목의 Packet 위에 주문이 올라타지 않는다
    other = packet.model_copy(update={"instrument_id": str(uuid4())})
    rejects(lambda: check_packet_admissible(other, signal(), snap()), "다른 종목 Packet")

    # 5. Point-in-Time - 시세보다 미래에 관측된 근거는 아직 없던 것이다
    future = packet.model_copy(update={"as_known_at": now + timedelta(minutes=1)})
    rejects(lambda: check_packet_admissible(future, signal(), snap()), "미래 관측 Packet")
    # 같은 시각은 통과한다. 경계를 배타로 잡으면 종가 기준 주문이 전부 막힌다
    same = packet.model_copy(update={"as_known_at": now})
    assert check_packet_admissible(same, signal(), snap()).as_known_at == now

    # 6. Long-only - Macro·Micro가 모두 negative면 매수하지 않는다
    bearish = packet.model_copy(update={
        "macro_outlook": packet.macro_outlook.model_copy(update={"direction": NEGATIVE}),
        "micro_outlook": packet.micro_outlook.model_copy(update={"direction": NEGATIVE}),
    })
    rejects(lambda: check_packet_admissible(bearish, signal(), snap()), "전면 부정 Packet 매수")
    # 비중 0(청산)이면 부정적 근거와 방향이 맞으므로 통과한다
    assert check_packet_admissible(bearish, signal("0"), snap()) is not None

    # 7. **confidence를 수량으로 쓰지 않는다.** uncalibrated Packet의 확률을 비중으로
    #    바꾸면 표본 없는 수치가 주문 크기가 된다. 게이트 결과에 수량이 없다는 것이
    #    그 계약이다 - PacketRef에 confidence/weight 필드가 생기면 여기서 깨진다.
    assert not any(f in PacketRef.__dataclass_fields__
                   for f in ("confidence", "target_weight", "quantity")), \
        "게이트가 수량·확률을 들고 나가기 시작했다"
    assert packet.uncalibrated is True, "Fixture가 calibrated로 바뀌었다 - 7번 전제 확인 필요"

    # 8. instrument_id가 symbol 문자열이면 거부한다. 조용히 넘기면 종목이 어긋난다
    symbolic = packet.model_copy(update={"instrument_id": "005930"})
    rejects(lambda: check_packet_admissible(symbolic, signal(), snap()), "symbol 문자열 종목")

    print("ok - Packet 접수 게이트 8개 영역 점검 통과 "
          "(재일님 ResearchPacketV2 계약 + 동규님 canonical Fixture 사용)")
