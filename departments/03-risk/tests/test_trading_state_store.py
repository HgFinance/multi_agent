"""trading_state_store.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (리스크본부). 실제 Redis 연결이 필요한 유일한 자체 점검이라 REDIS_URL이
없으면 skip한다 - CLAUDE.md 개발 원칙(4번/5번 항목 유지)에 따라 새로 자격 증명을
배선하지 않는다. 원본과 동일하게 8개 시나리오를 순서대로 검증한다.

실행: REDIS_URL이 있을 때 python -m pytest departments/03-risk/tests/test_trading_state_store.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import redis

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts")
)
_ENV_PATH = Path(__file__).resolve().parent.parent.parent.parent / ".env"
if _ENV_PATH.exists() and not os.environ.get("REDIS_URL"):
    # load_dotenv()는 .env 전체(DATABASE_URL 등)를 프로세스 환경에 심어 같은 pytest
    # 세션에서 나중에 import되는 다른 부서 앱(06-ai-qa-audit/api/app.py 등)까지 오염시킨다.
    # 여기 필요한 건 REDIS_URL 하나뿐이라 그 값만 읽는다.
    from dotenv import dotenv_values

    _redis_url = dotenv_values(_ENV_PATH).get("REDIS_URL")
    if _redis_url:
        os.environ["REDIS_URL"] = _redis_url

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
from trading_state_store import (
    RedisTradingStateStore,
    TradingState,
    TradingStateStoreError,
    _now,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("REDIS_URL"),
    reason="REDIS_URL 필요 (Redis 연동 자체 점검, 4/5번 항목 유지 - 새로 배선하지 않음)",
)


class _BrokenRedisClient:
    """Redis 장애 상황을 흉내내는 스텁."""

    def get(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")

    def set(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")

    def delete(self, *_a, **_k):
        raise redis.ConnectionError("simulated outage")


@pytest.fixture(scope="module")
def store():
    real_client = redis.Redis.from_url(
        os.environ["REDIS_URL"], socket_connect_timeout=8
    )
    try:
        real_client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis 통합 환경에 연결할 수 없습니다: {exc}")
    test_scope = f"test:{uuid4()}"
    s = RedisTradingStateStore(
        real_client, key_prefix="hgfinance:selfcheck:trading_state:"
    )
    yield s, test_scope
    s.clear_state(test_scope)
    real_client.close()


@pytest.fixture(scope="module")
def broken_store():
    return RedisTradingStateStore(_BrokenRedisClient())  # type: ignore[arg-type]


def test_01_untouched_scope_is_enabled(store):
    s, scope = store
    assert s.get_state(scope) is TradingState.ENABLED, "미설정 상태는 ENABLED여야 함"
    assert s.get_record(scope) is None


def test_02_set_entry_blocked_preserves_metadata(store):
    s, scope = store
    s.set_state(
        scope, TradingState.ENTRY_BLOCKED, "일일 손실 한도 근접", "svc_risk_engine"
    )
    record = s.get_record(scope)
    assert record is not None
    assert record.state is TradingState.ENTRY_BLOCKED
    assert record.reason == "일일 손실 한도 근접"
    assert record.set_by == "svc_risk_engine"
    assert s.get_state(scope) is TradingState.ENTRY_BLOCKED


def test_03_overwrite_replaces_current_state(store):
    s, scope = store
    s.set_state(scope, TradingState.HALTED, "Kill Switch 발동", "risk-supervisor")
    assert s.get_state(scope) is TradingState.HALTED


def test_04_clear_returns_to_enabled(store):
    s, scope = store
    s.clear_state(scope)
    assert s.get_state(scope) is TradingState.ENABLED


def test_05_read_failure_raises_not_fail_open(broken_store):
    with pytest.raises(TradingStateStoreError):
        broken_store.get_state("any")


def test_06_read_failure_fail_closed_wrapper_returns_halted(broken_store):
    assert broken_store.get_state_fail_closed("any") is TradingState.HALTED, (
        "Redis 장애를 ENABLED로 잘못 추정하면 안 됨"
    )


def test_07_write_failure_raises(broken_store):
    with pytest.raises(TradingStateStoreError):
        broken_store.set_state("any", TradingState.ENABLED, "x", "y")


def test_08_risk_engine_integration_reads_real_redis_state(store):
    s, scope = store
    s.set_state(scope, TradingState.ENTRY_BLOCKED, "통합 테스트", "selfcheck")
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
            market_snapshot_id="s1", as_of=now, bid=Decimal(69900), ask=Decimal(70000)
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
        trading_state=s.get_state_fail_closed(scope),
        as_of=now,
    )
    assessment = RiskEngine().check_order(intent, ctx)
    assert assessment.decision.verdict is RiskVerdict.REJECT, (
        "Redis의 ENTRY_BLOCKED 상태가 실제 주문 차단으로 이어져야 함"
    )
    assert assessment.trading_state is TradingState.ENTRY_BLOCKED
