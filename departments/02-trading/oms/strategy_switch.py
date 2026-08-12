#!/usr/bin/env python3
"""전략 가동 스위치. **승격은 자격이고, 가동은 사용자 판단이다.**

소유: 재일 (전략 공장) / 경계는 도현(트레이딩 OMS)과 공유
근거: CLAUDE.md 개발원칙 9("위험한 기능은 실패 시 거래 확대가 아니라 Entry 차단
      방향으로 동작한다"), 마스터플랜 5.6(권한 분리)

▶ 왜 스위치가 따로 있나
  QA 검증과 CEO 승격은 "이 전략이 **자격이 있는가**" 를 답한다. 자격이 있다고
  자본을 걸어야 하는 것은 아니다 - 그 판단은 사용자 것이다. 승격만으로 주문이
  나가면 사용자는 자기 돈이 언제부터 움직이는지 모른 채로 있게 된다.

▶ 기본은 꺼짐이다
  새로 승격된 전략은 **꺼진 채로 태어난다.** 켜는 것은 사용자의 명시적 행위다.
  기본을 켜짐으로 두면 "승격됐는데 아무도 못 봤다" 가 곧 "돈이 나갔다" 가 된다.

▶ 끄기와 켜기는 대칭이 아니다
  - **끄기는 누구나 할 수 있다** - 사용자, 감시 에이전트, 운영자. 끄는 것은
    안전 방향이라 틀려도 손실이 아니라 기회비용이다(원칙 9).
  - **켜기는 사용자만 할 수 있다** - 자본이 걸리는 방향이다. 에이전트가 켤 수
    있으면 사람 서명이 있으나 마나가 된다.

▶ 전송 시점에 다시 본다
  주문 생성 때 켜져 있었다고 전송 때도 켜져 있다고 가정하지 않는다. OMS 가
  Risk 승인을 전송 시점에 재검증하는 것과 같은 이유다 - 사용자가 스위치를 내린
  순간, 아직 안 나간 주문은 나가면 안 된다.

자체 점검:
    python departments/02-trading/oms/strategy_switch.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock


class StrategyDisabled(RuntimeError):
    """전략이 꺼져 있다. 주문을 보내지 않는다."""


@dataclass(frozen=True)
class SwitchEvent:
    """스위치를 누가 언제 왜 움직였는가. 감사 추적이다."""

    strategy_id: str
    enabled: bool
    actor: str
    reason: str
    at: datetime


@dataclass
class StrategySwitchboard:
    """전략별 가동 여부. 프로세스 메모리 판이며 영속 저장은 호출자 몫이다."""

    _state: dict[str, bool] = field(default_factory=dict)
    _log: list[SwitchEvent] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock)

    def is_enabled(self, strategy_id: str) -> bool:
        """**모르는 전략은 꺼진 것으로 본다.** 이것이 이 모듈의 기본값이다."""
        if not strategy_id:
            return False
        with self._lock:
            return self._state.get(strategy_id, False)

    def enable(self, strategy_id: str, *, actor: str, reason: str = "") -> SwitchEvent:
        """가동 시작. **사용자만 부를 수 있다** - 자본이 걸리는 방향이다."""
        if not strategy_id:
            raise ValueError("strategy_id 가 비었습니다")
        if not actor.startswith("user:"):
            # 에이전트가 켤 수 있으면 사람 서명이 있으나 마나가 된다.
            raise PermissionError(
                f"전략 가동은 사용자만 할 수 있습니다(actor={actor!r}). "
                "에이전트·자동화는 끄기만 가능합니다."
            )
        return self._set(strategy_id, True, actor, reason)

    def disable(self, strategy_id: str, *, actor: str, reason: str) -> SwitchEvent:
        """가동 중지. **누구나 부를 수 있다** - 안전 방향이라 막지 않는다."""
        if not strategy_id:
            raise ValueError("strategy_id 가 비었습니다")
        if not reason.strip():
            # 사유 없는 정지는 나중에 되돌릴 근거가 없다.
            raise ValueError("정지에는 사유가 필요합니다")
        return self._set(strategy_id, False, actor, reason)

    def require_enabled(self, strategy_id: str) -> None:
        """전송 직전 관문. 꺼져 있으면 예외다 - 조용히 건너뛰지 않는다."""
        if not self.is_enabled(strategy_id):
            raise StrategyDisabled(
                f"전략 {strategy_id!r} 이 꺼져 있어 주문을 보내지 않습니다"
            )

    def history(self, strategy_id: str | None = None) -> list[SwitchEvent]:
        with self._lock:
            if strategy_id is None:
                return list(self._log)
            return [e for e in self._log if e.strategy_id == strategy_id]

    def _set(self, strategy_id: str, enabled: bool, actor: str, reason: str) -> SwitchEvent:
        event = SwitchEvent(
            strategy_id=strategy_id,
            enabled=enabled,
            actor=actor,
            reason=reason,
            at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._state[strategy_id] = enabled
            self._log.append(event)
        return event


SWITCHBOARD = StrategySwitchboard()


__all__ = [
    "SWITCHBOARD", "StrategySwitchboard", "StrategyDisabled", "SwitchEvent",
]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    board = StrategySwitchboard()

    # 기본은 꺼짐 - 승격만으로는 돌지 않는다
    assert board.is_enabled("STRAT-001") is False
    assert board.is_enabled("") is False
    try:
        board.require_enabled("STRAT-001")
    except StrategyDisabled:
        pass
    else:
        raise AssertionError("꺼진 전략인데 통과시켰다")

    # 켜기는 사용자만
    try:
        board.enable("STRAT-001", actor="agent:trading-department")
    except PermissionError:
        pass
    else:
        raise AssertionError("에이전트가 전략을 켰다")
    board.enable("STRAT-001", actor="user:jaeil", reason="검증 확인함")
    assert board.is_enabled("STRAT-001") is True
    board.require_enabled("STRAT-001")  # 이제 통과

    # 끄기는 누구나 - 감시 에이전트도 멈출 수 있다
    board.disable("STRAT-001", actor="agent:risk-management", reason="슬리피지 급증")
    assert board.is_enabled("STRAT-001") is False

    # 사유 없는 정지는 거부한다
    try:
        board.disable("STRAT-001", actor="user:jaeil", reason="  ")
    except ValueError:
        pass
    else:
        raise AssertionError("사유 없이 껐다")

    # 누가 언제 왜 움직였는지 남는다
    log = board.history("STRAT-001")
    assert [e.enabled for e in log] == [True, False], log
    assert log[1].actor == "agent:risk-management" and "슬리피지" in log[1].reason

    print("strategy_switch 자체 점검 통과")
