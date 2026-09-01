"""Isolated E2E coverage for the ordered conditional-order stress suite.

These tests reuse the production CEO admission, conditional-rule orchestrator,
worker, deterministic guard, and PAPER submission seam.  Market/OMS effects are
fakes; no live order or second parser/executor is introduced here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from apps.api import conditional_rule_orchestrator as orchestrator
from apps.api.conditional_rule_worker import (
    ConditionalRuleWorker,
    RuntimeInputs,
)
from apps.api.conditional_rules import ConditionalRuleCandidate
from orchestration.conditional_rules import (
    ActiveRule,
    EvaluationContext,
    EvaluationFrame,
    ExpressionType,
)
from orchestration.conditional_rules.evaluator import indicator_key
from tests.api.test_conditional_rule_orchestrator import USER_ID, _install_workflow
from tests.conditional_rules.test_worker import FakeClient, FakeStore


RAW_STRESS_1 = (
    "삼성전자 60분봉 20이평을 상향 돌파하고 RSI(14)가 50 이상, "
    "거래량이 20봉 평균의 1.5배 이상이면 10주 매수"
)
RAW_STRESS_2 = (
    "삼성전자 볼린저밴드 상단 돌파 + RSI 70 이상 또는 "
    "거래량 2배 이상이면 보유 수량 50% 매도"
)
RAW_STRESS_2_COMPLETE = (
    "삼성전자 일봉 볼린저밴드 상단 돌파 + RSI 70 이상 또는 "
    "거래량 2배 이상이면 보유 수량 50% 매도"
)
RAW_STRESS_3 = (
    "삼성전자 일봉이 20일 이평 위에 있을 때, "
    "5분봉 RSI가 30을 재돌파하면 5주 매수"
)
RAW_STRESS_4 = (
    "삼성전자 수익률 10% 이상 + 포트폴리오 비중 20% 초과 + "
    "15분봉 20이평 하향 돌파 시 초과 비중 매도"
)
RAW_STRESS_5 = (
    "삼성전자 MACD 골든크로스 발생 시 가용 현금의 10%를 매수하되, "
    "최대 주문금액은 100만원으로 제한"
)
RAW_STRESS_5_COMPLETE = (
    "삼성전자 일봉 MACD 골든크로스 발생 시 가용 현금의 10%를 매수하되, "
    "최대 주문금액은 100만원으로 제한"
)
RAW_STRESS_6_AMBIGUOUS = (
    "삼성전자 RSI(14)가 30을 하향 돌파한 이후 20봉 이내 20이평을 "
    "상향 돌파하면 5주 매수, 그 전에 RSI(14)가 70을 상향 돌파하면 조건 취소"
)
RAW_STRESS_6_COMPLETE = (
    "삼성전자 일봉 RSI(14)가 30을 하향 돌파한 이후 20봉 이내 20일 이평을 "
    "상향 돌파하면 5주 매수, 그 전에 RSI(14)가 70을 상향 돌파하면 조건 취소"
)
RAW_STRESS_7 = (
    "삼성전자 수익률이 15%를 넘은 이후 최고 수익률을 추적하고, "
    "최고점 대비 5%p 하락하면 보유 수량 50% 매도"
)


def _candidate_1() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "LOGICAL",
                "operator": "AND",
                "children": [
                    {
                        "type": "CROSS",
                        "operator": "ABOVE",
                        "left": {"type": "MARKET", "field": "CLOSE"},
                        "right": {
                            "type": "INDICATOR",
                            "name": "SMA",
                            "timeframe": "1H",
                            "parameters": {"PERIOD": 20},
                        },
                    },
                    {
                        "type": "COMPARISON",
                        "operator": "GTE",
                        "left": {
                            "type": "INDICATOR",
                            "name": "RSI",
                            "timeframe": "1H",
                            "parameters": {"PERIOD": 14},
                        },
                        "right": {"type": "LITERAL", "value": "50", "unit": "NUMBER"},
                    },
                    {
                        "type": "COMPARISON",
                        "operator": "GTE",
                        "left": {"type": "MARKET", "field": "VOLUME"},
                        "right": {
                            "type": "ARITHMETIC",
                            "operator": "MUL",
                            "left": {
                                "type": "INDICATOR",
                                "name": "VOLUME_AVERAGE",
                                "timeframe": "1H",
                                "parameters": {"PERIOD": 20},
                            },
                            "right": {
                                "type": "LITERAL",
                                "value": "1.5",
                                "unit": "NUMBER",
                            },
                        },
                    },
                ],
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "10"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1H"},
        }
    )


def _candidate_2() -> ConditionalRuleCandidate:
    bollinger_cross = {
        "type": "CROSS",
        "operator": "ABOVE",
        "left": {"type": "MARKET", "field": "CLOSE"},
        "right": {
            "type": "INDICATOR",
            "name": "BOLLINGER",
            "output": "UPPER",
            "timeframe": "1D",
            "parameters": {"PERIOD": 20, "STDDEV": "2"},
        },
    }
    rsi = {
        "type": "COMPARISON",
        "operator": "GTE",
        "left": {
            "type": "INDICATOR",
            "name": "RSI",
            "timeframe": "1D",
            "parameters": {"PERIOD": 14},
        },
        "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
    }
    doubled_volume = {
        "type": "COMPARISON",
        "operator": "GTE",
        "left": {"type": "MARKET", "field": "VOLUME"},
        "right": {
            "type": "ARITHMETIC",
            "operator": "MUL",
            "left": {
                "type": "INDICATOR",
                "name": "VOLUME_AVERAGE",
                "timeframe": "1D",
                "parameters": {"PERIOD": 20},
            },
            "right": {"type": "LITERAL", "value": "2", "unit": "NUMBER"},
        },
    }
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "LOGICAL",
                "operator": "OR",
                "children": [
                    {
                        "type": "LOGICAL",
                        "operator": "AND",
                        "children": [bollinger_cross, rsi],
                    },
                    doubled_volume,
                ],
            },
            "action": {
                "side": "SELL",
                "sizing": {"type": "POSITION_PERCENT", "value": "0.5"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
        }
    )


def _candidate_3() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "LOGICAL",
                "operator": "AND",
                "children": [
                    {
                        "type": "COMPARISON",
                        "operator": "GT",
                        "left": {
                            "type": "INDICATOR",
                            "name": "SMA",
                            "timeframe": "1D",
                            "parameters": {"PERIOD": 1},
                        },
                        "right": {
                            "type": "INDICATOR",
                            "name": "SMA",
                            "timeframe": "1D",
                            "parameters": {"PERIOD": 20},
                        },
                    },
                    {
                        "type": "CROSS",
                        "operator": "ABOVE",
                        "left": {
                            "type": "INDICATOR",
                            "name": "RSI",
                            "timeframe": "5M",
                            "parameters": {"PERIOD": 14},
                        },
                        "right": {"type": "LITERAL", "value": "30", "unit": "NUMBER"},
                    },
                ],
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "5"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
        }
    )


def _candidate_4() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "LOGICAL",
                "operator": "AND",
                "children": [
                    {
                        "type": "COMPARISON",
                        "operator": "GTE",
                        "left": {"type": "PORTFOLIO", "field": "PNL_PERCENT"},
                        "right": {"type": "LITERAL", "value": "0.10", "unit": "RATIO"},
                    },
                    {
                        "type": "COMPARISON",
                        "operator": "GT",
                        "left": {"type": "PORTFOLIO", "field": "POSITION_WEIGHT"},
                        "right": {"type": "LITERAL", "value": "0.20", "unit": "RATIO"},
                    },
                    {
                        "type": "CROSS",
                        "operator": "BELOW",
                        "left": {"type": "MARKET", "field": "CLOSE"},
                        "right": {
                            "type": "INDICATOR",
                            "name": "SMA",
                            "timeframe": "15M",
                            "parameters": {"PERIOD": 20},
                        },
                    },
                ],
            },
            "action": {
                "side": "SELL",
                "sizing": {"type": "TARGET_POSITION_WEIGHT", "value": "0.20"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "15M"},
        }
    )


def _candidate_5() -> ConditionalRuleCandidate:
    parameters = {"FAST": 12, "SLOW": 26, "SIGNAL": 9}
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "CROSS",
                "operator": "ABOVE",
                "left": {
                    "type": "INDICATOR",
                    "name": "MACD",
                    "output": "MACD",
                    "timeframe": "1D",
                    "parameters": parameters,
                },
                "right": {
                    "type": "INDICATOR",
                    "name": "MACD",
                    "output": "SIGNAL",
                    "timeframe": "1D",
                    "parameters": parameters,
                },
            },
            "action": {
                "side": "BUY",
                "sizing": {
                    "type": "AVAILABLE_CASH_PERCENT_CAPPED",
                    "value": "0.10",
                    "cap_krw": "1000000",
                },
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
        }
    )


def _candidate_6() -> ConditionalRuleCandidate:
    def rsi_cross(operator: str, level: str):
        return {
            "type": "CROSS",
            "operator": operator,
            "left": {
                "type": "INDICATOR",
                "name": "RSI",
                "timeframe": "1D",
                "parameters": {"PERIOD": 14},
            },
            "right": {"type": "LITERAL", "value": level, "unit": "NUMBER"},
        }

    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "TEMPORAL_SEQUENCE",
                "parameters": {"WINDOW_BARS": 20},
                "children": [
                    rsi_cross("BELOW", "30"),
                    {
                        "type": "CROSS",
                        "operator": "ABOVE",
                        "left": {"type": "MARKET", "field": "CLOSE"},
                        "right": {
                            "type": "INDICATOR",
                            "name": "SMA",
                            "timeframe": "1D",
                            "parameters": {"PERIOD": 20},
                        },
                    },
                    rsi_cross("ABOVE", "70"),
                ],
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "5"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
        }
    )


def _candidate_7() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "TRAILING_STOP",
                "parameters": {
                    "DRAWDOWN": "0.05",
                    "DRAWDOWN_MODE": "RETURN_POINTS",
                    "ACTIVATION_RETURN": "0.15",
                },
            },
            "action": {
                "side": "SELL",
                "sizing": {"type": "POSITION_PERCENT", "value": "0.5"},
            },
            "evaluation": {"clock": "QUOTE"},
        }
    )


def _walk(node):
    yield node
    for child in (node.left, node.right, node.operand, *(node.children or ())):
        if child is not None:
            yield from _walk(child)


def _indicator_values(spec, values: dict[tuple[str, str, int], str]):
    result = {}
    for node in _walk(spec.condition):
        if node.type is not ExpressionType.INDICATOR:
            continue
        identity = (
            node.name or "",
            node.output or "VALUE",
            int((node.parameters or {}).get("PERIOD", 0)),
        )
        result[indicator_key(node)] = Decimal(values[identity])
    return result


def _runtime_inputs(spec, *, case: int) -> RuntimeInputs:
    now = datetime.now(timezone.utc)
    if case == 1:
        current_market = {
            "LAST_PRICE": Decimal("101"),
            "CLOSE": Decimal("101"),
            "VOLUME": Decimal("150"),
        }
        previous_market = {
            "LAST_PRICE": Decimal("99"),
            "CLOSE": Decimal("99"),
            "VOLUME": Decimal("90"),
        }
        current_indicators = _indicator_values(
            spec,
            {
                ("SMA", "VALUE", 20): "100",
                ("RSI", "VALUE", 14): "55",
                ("VOLUME_AVERAGE", "VALUE", 20): "100",
            },
        )
        previous_indicators = _indicator_values(
            spec,
            {
                ("SMA", "VALUE", 20): "100",
                ("RSI", "VALUE", 14): "49",
                ("VOLUME_AVERAGE", "VALUE", 20): "100",
            },
        )
        position_quantity = sellable_quantity = Decimal("0")
    elif case == 2:
        current_market = {
            "LAST_PRICE": Decimal("101"),
            "CLOSE": Decimal("101"),
            "VOLUME": Decimal("100"),
        }
        previous_market = {
            "LAST_PRICE": Decimal("99"),
            "CLOSE": Decimal("99"),
            "VOLUME": Decimal("100"),
        }
        current_indicators = _indicator_values(
            spec,
            {
                ("BOLLINGER", "UPPER", 20): "100",
                ("RSI", "VALUE", 14): "70",
                ("VOLUME_AVERAGE", "VALUE", 20): "100",
            },
        )
        previous_indicators = _indicator_values(
            spec,
            {
                ("BOLLINGER", "UPPER", 20): "100",
                ("RSI", "VALUE", 14): "69",
                ("VOLUME_AVERAGE", "VALUE", 20): "100",
            },
        )
        position_quantity = sellable_quantity = Decimal("20")
    elif case == 3:
        current_market = {"LAST_PRICE": Decimal("101")}
        previous_market = {"LAST_PRICE": Decimal("100")}
        current_indicators = _indicator_values(
            spec,
            {
                ("SMA", "VALUE", 1): "110",
                ("SMA", "VALUE", 20): "100",
                ("RSI", "VALUE", 14): "31",
            },
        )
        previous_indicators = _indicator_values(
            spec,
            {
                ("SMA", "VALUE", 1): "109",
                ("SMA", "VALUE", 20): "100",
                ("RSI", "VALUE", 14): "29",
            },
        )
        position_quantity = sellable_quantity = Decimal("0")
        portfolio = {}
        portfolio_nav = None
        available_cash = Decimal("1000000")
    elif case == 4:
        current_market = {
            "LAST_PRICE": Decimal("900"),
            "CLOSE": Decimal("900"),
        }
        previous_market = {
            "LAST_PRICE": Decimal("1100"),
            "CLOSE": Decimal("1100"),
        }
        current_indicators = _indicator_values(
            spec, {("SMA", "VALUE", 20): "1000"}
        )
        previous_indicators = _indicator_values(
            spec, {("SMA", "VALUE", 20): "1000"}
        )
        portfolio = {
            "PNL_PERCENT": Decimal("0.10"),
            "POSITION_WEIGHT": Decimal("0.27"),
            "PORTFOLIO_NAV": Decimal("1000000"),
        }
        portfolio_nav = Decimal("1000000")
        position_quantity = sellable_quantity = Decimal("300")
        available_cash = Decimal("730000")
    else:
        current_market = {"LAST_PRICE": Decimal("100000")}
        previous_market = {"LAST_PRICE": Decimal("99000")}
        current_indicators = _indicator_values(
            spec,
            {
                ("MACD", "MACD", 0): "1",
                ("MACD", "SIGNAL", 0): "0",
            },
        )
        previous_indicators = _indicator_values(
            spec,
            {
                ("MACD", "MACD", 0): "-1",
                ("MACD", "SIGNAL", 0): "0",
            },
        )
        portfolio = {}
        portfolio_nav = None
        position_quantity = sellable_quantity = Decimal("0")
        available_cash = Decimal("20000000")

    if case in {1, 2}:
        portfolio = {}
        portfolio_nav = None
        available_cash = Decimal("1000000")

    current = EvaluationFrame(
        market=current_market,
        portfolio=portfolio,
        indicators=current_indicators,
        observed_at=now,
    )
    previous = EvaluationFrame(
        market=previous_market,
        portfolio=portfolio,
        indicators=previous_indicators,
        observed_at=now,
    )
    return RuntimeInputs(
        evaluation_context=EvaluationContext(current=current, previous=previous),
        evaluation_key=f"BAR_CLOSE:stress-{case}:{now.isoformat()}",
        context_sha256=str(case) * 64,
        data_watermark=now,
        membership_active=True,
        fund_active=True,
        book_active=True,
        market_session_available=True,
        market_open=True,
        data_complete=True,
        quote_fresh=True,
        current_price=current_market["LAST_PRICE"],
        available_cash=available_cash,
        position_quantity=position_quantity,
        sellable_quantity=sellable_quantity,
        lot_size=Decimal("1"),
        portfolio_nav=portfolio_nav,
    )


@pytest.mark.parametrize(
    ("case", "raw", "candidate_factory", "expected_quantity"),
    (
        (1, RAW_STRESS_1, _candidate_1, Decimal("10")),
        (2, RAW_STRESS_2_COMPLETE, _candidate_2, Decimal("10")),
        (3, RAW_STRESS_3, _candidate_3, Decimal("5")),
        (4, RAW_STRESS_4, _candidate_4, Decimal("77")),
        (5, RAW_STRESS_5_COMPLETE, _candidate_5, Decimal("10")),
    ),
)
def test_stress_rule_reaches_one_guarded_paper_submission(
    monkeypatch: pytest.MonkeyPatch,
    case: int,
    raw: str,
    candidate_factory,
    expected_quantity: Decimal,
) -> None:
    orders, rules, tasks = _install_workflow(monkeypatch, raw_instruction=raw)
    admission = next(iter(orders._records.values()))
    assert admission.raw_instruction == raw
    assert admission.raw_instruction_sha256
    assert raw in tasks["t_root1"]["body"]
    assert raw in tasks["t_trade1"]["body"]

    activation = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=candidate_factory(),
    )
    assert activation["binding"] is True
    assert activation["mode"] == "PAPER"
    assert activation["state"] == "ACTIVE"
    assert orchestrator.get_user_conditional_paper_rule_status(
        root_task_id="t_root1", trading_task_id="t_trade1"
    )["workflow_state"] == "WAITING_FOR_TRIGGER"

    stored = rules.get(activation["rule_id"], user_id=USER_ID)
    assert stored is not None
    active = ActiveRule(
        rule_id=UUID(stored.rule_id),
        rule_version=stored.rule_version,
        row_version=1,
        spec_sha256=stored.spec_sha256,
        spec=stored.spec,
    )
    store = FakeStore(active)
    client = FakeClient(_runtime_inputs(stored.spec, case=case))

    counts = ConditionalRuleWorker(
        store, client, batch_size=1, max_workers=1
    ).process_once()

    assert counts["evaluated"] == 1
    assert counts["triggered"] == 1
    assert counts["submitted"] == 1
    assert counts["errors"] == 0
    assert client.submit_calls == 1
    assert store.execution_decisions == [
        (True, "READY_FOR_PAPER_DIRECTIVE", expected_quantity)
    ]


@pytest.mark.parametrize(
    ("raw", "candidate_factory"),
    (
        (RAW_STRESS_2, _candidate_2),
        (RAW_STRESS_5, _candidate_5),
        (RAW_STRESS_6_AMBIGUOUS, _candidate_6),
    ),
)
def test_indicator_stress_rule_without_timeframe_stays_unbound(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    candidate_factory,
) -> None:
    """A canonical 1D AST must not silently supply a missing user timeframe."""

    _install_workflow(monkeypatch, raw_instruction=raw)
    activation = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=candidate_factory(),
    )

    assert activation["binding"] is False
    assert activation["rule_active"] is False
    assert activation["reason_codes"] == ["TIMEFRAME_NOT_IN_INSTRUCTION"]


def _temporal_inputs(spec, *, armed: bool) -> RuntimeInputs:
    now = datetime.now(timezone.utc)
    rsi_current = "29" if armed else "40"
    rsi_previous = "31" if armed else "39"
    close_current = Decimal("99" if armed else "101")
    close_previous = Decimal("99")
    values_current = _indicator_values(
        spec,
        {
            ("RSI", "VALUE", 14): rsi_current,
            ("SMA", "VALUE", 20): "100",
        },
    )
    values_previous = _indicator_values(
        spec,
        {
            ("RSI", "VALUE", 14): rsi_previous,
            ("SMA", "VALUE", 20): "100",
        },
    )
    return RuntimeInputs(
        evaluation_context=EvaluationContext(
            current=EvaluationFrame(
                market={"LAST_PRICE": close_current, "CLOSE": close_current},
                portfolio={},
                indicators=values_current,
                observed_at=now,
            ),
            previous=EvaluationFrame(
                market={"LAST_PRICE": close_previous, "CLOSE": close_previous},
                portfolio={},
                indicators=values_previous,
                observed_at=now,
            ),
        ),
        evaluation_key=f"BAR_CLOSE:temporal:{now.isoformat()}",
        context_sha256=("a" if armed else "b") * 64,
        data_watermark=now,
        membership_active=True,
        fund_active=True,
        book_active=True,
        market_session_available=True,
        market_open=True,
        data_complete=True,
        quote_fresh=True,
        current_price=close_current,
        available_cash=Decimal("1000000"),
        position_quantity=Decimal("0"),
        sellable_quantity=Decimal("0"),
        lot_size=Decimal("1"),
    )


def test_temporal_stress_rule_persists_arm_then_submits_on_later_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orders, rules, _tasks = _install_workflow(
        monkeypatch, raw_instruction=RAW_STRESS_6_COMPLETE
    )
    activation = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate_6(),
    )
    assert activation["state"] == "ACTIVE"
    stored = rules.get(activation["rule_id"], user_id=USER_ID)
    assert stored is not None
    active = ActiveRule(
        rule_id=UUID(stored.rule_id),
        rule_version=stored.rule_version,
        row_version=1,
        spec_sha256=stored.spec_sha256,
        spec=stored.spec,
    )
    store = FakeStore(active)
    client = FakeClient(_temporal_inputs(stored.spec, armed=True))
    worker = ConditionalRuleWorker(store, client, batch_size=1, max_workers=1)

    first = worker.process_once()
    assert first["evaluated"] == 1
    assert first["triggered"] == 0
    assert store.temporal_state is not None
    assert store.temporal_state.remaining_bars == 20

    client.runtime_inputs = _temporal_inputs(stored.spec, armed=False)
    second = worker.process_once()
    assert second["triggered"] == 1
    assert second["submitted"] == 1
    assert store.execution_decisions == [
        (True, "READY_FOR_PAPER_DIRECTIVE", Decimal("5"))
    ]


def _trailing_return_points_inputs(*, price: str, observed: datetime) -> RuntimeInputs:
    current_price = Decimal(price)
    return RuntimeInputs(
        evaluation_context=EvaluationContext(
            current=EvaluationFrame(
                market={"LAST_PRICE": current_price},
                portfolio={"AVG_ENTRY_PRICE": Decimal("100")},
                indicators={},
                observed_at=observed,
            )
        ),
        evaluation_key=f"QUOTE:trailing-return-points:{observed.isoformat()}",
        context_sha256="7" * 64,
        data_watermark=observed,
        membership_active=True,
        fund_active=True,
        book_active=True,
        market_session_available=True,
        market_open=True,
        data_complete=True,
        quote_fresh=True,
        current_price=current_price,
        available_cash=Decimal("1000000"),
        position_quantity=Decimal("20"),
        sellable_quantity=Decimal("20"),
        lot_size=Decimal("1"),
    )


def test_return_points_trailing_stress_rule_tracks_then_submits_half_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orders, rules, _tasks = _install_workflow(
        monkeypatch, raw_instruction=RAW_STRESS_7
    )
    activation = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate_7(),
    )
    assert activation["state"] == "ACTIVE"
    stored = rules.get(activation["rule_id"], user_id=USER_ID)
    assert stored is not None
    active = ActiveRule(
        rule_id=UUID(stored.rule_id),
        rule_version=stored.rule_version,
        row_version=1,
        spec_sha256=stored.spec_sha256,
        spec=stored.spec,
    )
    start = datetime.now(timezone.utc)
    store = FakeStore(active)
    client = FakeClient(_trailing_return_points_inputs(price="115", observed=start))
    worker = ConditionalRuleWorker(store, client, batch_size=1, max_workers=1)

    armed = worker.process_once()
    client.runtime_inputs = _trailing_return_points_inputs(
        price="110", observed=start + timedelta(seconds=1)
    )
    exited = worker.process_once()

    assert armed["triggered"] == 0
    assert exited["triggered"] == 1
    assert exited["submitted"] == 1
    assert store.execution_decisions == [
        (True, "READY_FOR_PAPER_DIRECTIVE", Decimal("10"))
    ]
