#!/usr/bin/env python3
"""Sprint K1: Redis 최신 Trading State.

소유: 동규 (리스크본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 3.1("최신 상태는 Redis에 유지"),
      10.1(Hot State - redis), 12(Sprint K1 "Redis 최신 Trading State"), 14(DoD)
      risk_engine.py의 TradingState(ENABLED/REDUCE_ONLY/ENTRY_BLOCKED/HALTED)를 그대로 쓴다.

risk.trading_states(Supabase)는 공식 이력이고, 여기(Redis)는 Pre-trade Hot Path가 매 주문마다
빠르게 읽는 "지금 상태" 캐시다(팀 가이드 3.1 "Risk Snapshot 전체를 Tick 단위로 Supabase에
쓰지 않는다. 최신 상태는 Redis에 유지"). Supabase 쪽 이력 적재(risk.trading_states,
kill_switch_events)는 accounting.funds가 비어 있어 아직 못 하지만(risk_engine.py와 같은 이유),
Redis는 그 의존이 없어서 바로 배선했다.

Key 원칙: Redis가 응답하지 않으면 ENABLED로 추정하지 않는다. 상태를 모르는데 거래를
허용하는 방향으로 fail-open하지 않는다(CLAUDE.md 개발 원칙 9번 "위험한 기능은 실패 시
거래 확대가 아니라 Entry 차단 방향으로 동작한다"). Key가 아예 없는 것(=한 번도 제한을
건 적 없음)과 Redis 연결 실패(=상태를 모름)는 다르게 취급한다 - 전자만 ENABLED다.

자체 점검: python departments/03-risk/engine/trading_state_store.py (REDIS_URL 필요)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import redis

sys.path.insert(0, str(Path(__file__).resolve().parent))
from risk_engine import TradingState

DEFAULT_KEY_PREFIX = "hgfinance:trading_state:"


class TradingStateStoreError(Exception):
    """Redis에 닿지 않으면 조용히 ENABLED로 추정하지 않는다."""


@dataclass(frozen=True)
class TradingStateRecord:
    state: TradingState
    reason: str
    set_by: str
    set_at: datetime


class RedisTradingStateStore:
    """scope(예: "fund:<uuid>", "book:<uuid>")별 현재 Trading State를 Redis에 유지한다."""

    def __init__(
        self, client: redis.Redis, key_prefix: str = DEFAULT_KEY_PREFIX
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, scope: str) -> str:
        return f"{self._key_prefix}{scope}"

    def set_state(
        self, scope: str, state: TradingState, reason: str, set_by: str
    ) -> TradingStateRecord:
        record = TradingStateRecord(
            state=state, reason=reason, set_by=set_by, set_at=_now()
        )
        payload = json.dumps(
            {
                "state": record.state.value,
                "reason": record.reason,
                "set_by": record.set_by,
                "set_at": record.set_at.isoformat(),
            }
        )
        try:
            # 만료 없이 유지한다 - Trading State는 캐시가 아니라 명시적으로 바뀌기 전까지
            # 계속 유효한 "현재 상태"다.
            self._client.set(self._key(scope), payload)
        except redis.RedisError as e:
            raise TradingStateStoreError(
                f"Redis 쓰기 실패 - {scope} 상태가 반영 안 됐을 수 있습니다: {e}"
            ) from e
        return record

    def get_record(self, scope: str) -> TradingStateRecord | None:
        """Key가 없으면 None(=한 번도 제한을 건 적 없음). Redis 연결 실패는 예외로 올린다."""
        try:
            raw = self._client.get(self._key(scope))
        except redis.RedisError as e:
            raise TradingStateStoreError(
                f"Redis 조회 실패 - {scope} 상태를 확인할 수 없습니다: {e}"
            ) from e
        if raw is None:
            return None
        data = json.loads(raw)
        return TradingStateRecord(
            state=TradingState(data["state"]),
            reason=data["reason"],
            set_by=data["set_by"],
            set_at=datetime.fromisoformat(data["set_at"]),
        )

    def get_state(self, scope: str) -> TradingState:
        """Key 없음 -> ENABLED(정상, 제한을 건 적 없음). Redis 장애는 그대로 예외를 던진다 -
        호출자가 fail-closed 여부를 직접 결정하게 한다."""
        record = self.get_record(scope)
        return record.state if record is not None else TradingState.ENABLED

    def get_state_fail_closed(self, scope: str) -> TradingState:
        """Pre-trade Hot Path 전용 편의 함수. Redis 장애 시 예외를 올리는 대신 HALTED를
        돌려준다 - risk_engine.RiskEngine.check_order는 HALTED면 모든 주문을 막으므로,
        상태를 모를 때 자동으로 안전 쪽(차단)에 떨어진다."""
        try:
            return self.get_state(scope)
        except TradingStateStoreError:
            return TradingState.HALTED

    def clear_state(self, scope: str) -> None:
        """제한을 완전히 해제하고 Key 자체를 지운다(=ENABLED로 되돌아감을 명시적으로 기록하지
        않는 대신, 다음 조회는 '한 번도 제한한 적 없음'과 동일하게 ENABLED를 돌려준다).
        실제 운영에서는 clear 대신 set_state(..., TradingState.ENABLED, ...)로 "누가 언제
        해제했는지"를 남기는 편을 권장한다 - 이 메서드는 테스트/초기화용이다."""
        try:
            self._client.delete(self._key(scope))
        except redis.RedisError as e:
            raise TradingStateStoreError(f"Redis 삭제 실패: {e}") from e


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _BrokenRedisClient:
    """자체 점검에서 Redis 장애 상황을 흉내내는 스텁."""

    def get(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")

    def set(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")

    def delete(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")
    real_client = redis.Redis.from_url(
        os.environ["REDIS_URL"], socket_connect_timeout=8
    )
    assert real_client.ping(), "Redis Cloud에 연결이 안 됩니다"

    test_scope = f"test:{uuid4()}"
    store = RedisTradingStateStore(
        real_client, key_prefix="hgfinance:selfcheck:trading_state:"
    )

    try:
        # 1. 한 번도 안 건드린 scope -> ENABLED
        assert store.get_state(test_scope) is TradingState.ENABLED, (
            "미설정 상태는 ENABLED여야 함"
        )
        assert store.get_record(test_scope) is None

        # 2. ENTRY_BLOCKED로 설정 -> 그대로 읽힘, 메타데이터도 보존됨
        store.set_state(
            test_scope,
            TradingState.ENTRY_BLOCKED,
            "일일 손실 한도 근접",
            "svc_risk_engine",
        )
        record = store.get_record(test_scope)
        assert record is not None
        assert record.state is TradingState.ENTRY_BLOCKED
        assert record.reason == "일일 손실 한도 근접"
        assert record.set_by == "svc_risk_engine"
        assert store.get_state(test_scope) is TradingState.ENTRY_BLOCKED

        # 3. 다른 상태로 덮어쓰기 -> 최신 값으로 교체됨 (이력이 아니라 "현재 상태"라서)
        store.set_state(
            test_scope, TradingState.HALTED, "Kill Switch 발동", "risk-supervisor"
        )
        assert store.get_state(test_scope) is TradingState.HALTED

        # 4. clear -> 다시 미설정(ENABLED)으로 돌아감
        store.clear_state(test_scope)
        assert store.get_state(test_scope) is TradingState.ENABLED

        # 5. Redis 장애 시 get_state는 예외를 올린다 (fail-open 하지 않음)
        broken_store = RedisTradingStateStore(_BrokenRedisClient())  # type: ignore[arg-type]
        try:
            broken_store.get_state(test_scope)
            raise AssertionError("Redis 장애인데 예외 없이 통과됨")
        except TradingStateStoreError:
            pass

        # 6. Redis 장애 시 get_state_fail_closed는 HALTED로 떨어진다 (예외를 안 올리고 차단)
        assert broken_store.get_state_fail_closed(test_scope) is TradingState.HALTED, (
            "Redis 장애를 ENABLED로 잘못 추정하면 안 됨"
        )

        # 7. 쓰기도 장애 시 예외를 올린다 (조용히 무시하지 않음)
        try:
            broken_store.set_state(test_scope, TradingState.ENABLED, "x", "y")
            raise AssertionError("Redis 쓰기 장애인데 예외 없이 통과됨")
        except TradingStateStoreError:
            pass

        # 8. risk_engine.RiskEngine과의 실제 통합 - Redis에서 읽은 상태로 주문이 진짜 막히는지
        from datetime import timedelta
        from decimal import Decimal

        from contracts import (
            MarketSnapshot,
            OrderIntent,
            OrderType,
            Side,
            TimeInForce,
        )
        from risk_engine import (
            CounterpartyHealth,
            CounterpartyStatus,
            LimitSet,
            MandateScope,
            MarketStatus,
            PortfolioState,
            RiskContext,
            RiskEngine,
            RiskVerdict,
        )

        store.set_state(
            test_scope, TradingState.ENTRY_BLOCKED, "통합 테스트", "selfcheck"
        )
        now = _now()
        fund, book, strategy, aapl = uuid4(), uuid4(), uuid4(), uuid4()
        intent = OrderIntent(
            trade_case_id=uuid4(),
            fund_id=fund,
            book_id=book,
            strategy_id=strategy,
            instrument_id=aapl,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal(10),
            limit_price=Decimal(70000),
            time_in_force=TimeInForce.DAY,
            valid_until=now + timedelta(hours=1),
            snapshot=MarketSnapshot(
                market_snapshot_id="s1",
                as_of=now,
                bid=Decimal(69900),
                ask=Decimal(70000),
            ),
            idempotency_key="idem_redis_test",
            created_by="trader-pm-agent",
            trace_id="t1",
            created_at=now,
        )
        ctx = RiskContext(
            mandate=MandateScope(
                fund_id=fund,
                allowed_instrument_ids=None,
                min_order_notional=Decimal(1000),
                max_order_notional=Decimal(1000000000),
            ),
            limits=LimitSet(
                soft_single_issuer_pct=Decimal("0.5"),
                hard_single_issuer_pct=Decimal("0.9"),
                max_daily_turnover_notional=Decimal(1000000000),
                max_daily_order_count=1000,
                max_daily_loss=Decimal(1000000000),
                max_drawdown_pct=Decimal("0.9"),
            ),
            restricted_items=(),
            portfolio=PortfolioState(
                fund_id=fund,
                cash=Decimal(1000000000),
                buying_power=Decimal(1000000000),
                gross_exposure=Decimal(0),
                peak_equity=Decimal(1000000000),
                equity=Decimal(1000000000),
            ),
            market_status=MarketStatus(tradable=True),
            counterparty=CounterpartyStatus("paper", CounterpartyHealth.OK),
            trading_state=store.get_state_fail_closed(
                test_scope
            ),  # <- Redis에서 실제로 읽음
            as_of=now,
        )
        assessment = RiskEngine().check_order(intent, ctx)
        assert assessment.decision.verdict is RiskVerdict.REJECT, (
            "Redis의 ENTRY_BLOCKED 상태가 실제 주문 차단으로 이어져야 함"
        )
        assert assessment.trading_state is TradingState.ENTRY_BLOCKED

        print("ok - Redis Trading State Store 8개 시나리오 점검 통과")
    finally:
        store.clear_state(test_scope)
        real_client.close()
