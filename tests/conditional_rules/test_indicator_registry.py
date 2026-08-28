from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from orchestration.conditional_rules import (
    Candle,
    ConditionalRuleSpec,
    EvaluationError,
    ExpressionNode,
    IndicatorEngine,
    IndicatorProviderError,
    IndicatorValue,
    RuleSemanticError,
    evaluate_condition,
    get_indicator_definition,
    list_supported_indicators,
    validate_rule_spec,
)
from orchestration.conditional_rules.evaluator import EvaluationContext, indicator_key
from orchestration.conditional_rules.indicators import (
    DEFAULT_REGISTRY,
    IndicatorRegistry,
    LSBrokerIndicatorProvider,
)
from orchestration.conditional_rules.indicators.broker import normalize_ls_payload
from orchestration.conditional_rules.indicators.cache import indicator_cache_key
from orchestration.conditional_rules.indicators.dependency_graph import build_dependency_graph
from apps.api.conditional_rule_worker import _required_history
from orchestration.conditional_rules.worker_store import ActiveRule


NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_live_ls_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep registry contract tests offline even when another module loads .env."""

    for name in (
        "LS_ENV",
        "LS_APP_KEY",
        "LS_APP_KEY_LIVE",
        "LS_APP_KEY_PAPER",
        "LS_APP_SECRET_KEY",
        "LS_APP_SECRET_KEY_LIVE",
        "LS_APP_SECRET_KEY_PAPER",
        "LS_REST_BASE_URL",
        "LS_REST_BASE_URL_LIVE",
        "LS_REST_BASE_URL_PAPER",
    ):
        monkeypatch.delenv(name, raising=False)


def _rule(condition: dict, *, clock: str = "BAR_CLOSE", timeframe: str = "1D") -> ConditionalRuleSpec:
    evaluation = {"clock": clock}
    if clock == "BAR_CLOSE":
        evaluation["primary_timeframe"] = timeframe
    return ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": condition,
            "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "2"}},
            "evaluation": evaluation,
            "execution_mode": "PAPER",
            "repeat_policy": "ONCE",
            "expires_at": "2026-09-19T01:00:00+00:00",
            "raw_instruction_sha256": "0" * 64,
        }
    )


def _candles(count: int = 50) -> list[Candle]:
    values = []
    for index in range(count):
        close = Decimal(100 + index) + (Decimal("0.25") if index % 3 == 0 else Decimal("0"))
        values.append(
            Candle(
                bucket_time=NOW + timedelta(days=index),
                open=close,
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal(1000 + index * 10),
            )
        )
    return values


def _indicator(name: str, *, output: str = "VALUE", parameters: dict | None = None) -> dict:
    return {
        "type": "INDICATOR",
        "name": name,
        "output": output,
        "timeframe": "1D",
        "parameters": parameters or {},
    }


def test_registry_catalog_contains_local_and_broker_capabilities() -> None:
    names = {item["name"] for item in list_supported_indicators()}
    assert {"RSI", "STOCHASTIC", "CCI", "MFI", "OBV", "ROC", "VWAP", "DONCHIAN", "PSAR"} <= names
    assert "FOREIGN_NET_BUY_AMOUNT" in names
    assert get_indicator_definition("외국인순매수대금")["name"] == "FOREIGN_NET_BUY_AMOUNT"
    assert get_indicator_definition("BROKER_SEARCH_MATCH")["provider"] == "LS"


def test_registry_calculators_cover_new_local_indicators() -> None:
    engine = IndicatorEngine()
    candles = _candles()
    outputs = {
        "STOCHASTIC": "K",
        "CCI": "VALUE",
        "MFI": "VALUE",
        "OBV": "VALUE",
        "ROC": "VALUE",
        "VWAP": "VALUE",
        "WILLIAMS_R": "VALUE",
        "DONCHIAN": "UPPER",
        "PSAR": "VALUE",
    }
    for name, output in outputs.items():
        value = engine.compute(ExpressionNode.model_validate(_indicator(name, output=output)), candles)
        assert isinstance(value, Decimal), name
        assert value.is_finite(), name
    assert isinstance(engine.compute(ExpressionNode.model_validate(_indicator("PSAR", output="TREND")), candles), bool)


def test_envelope_bands_are_a_fixed_percentage_offset_from_the_average() -> None:
    """Requested as "엔빌로프(20,2)" on 2026-08-27 and rejected as unsupported.

    Envelope offsets the moving average by a fixed percentage, so unlike
    Bollinger its width does not move with volatility.
    """

    assert get_indicator_definition("엔빌로프")["name"] == "ENVELOPE"
    engine = IndicatorEngine()
    candles = _candles()
    node = ExpressionNode.model_validate(
        _indicator("ENVELOPE", output="MIDDLE") | {"parameters": {"PERIOD": 20, "PERCENT": "2"}}
    )
    middle = engine.compute(node, candles)
    upper = engine.compute(
        ExpressionNode.model_validate(
            _indicator("ENVELOPE", output="UPPER") | {"parameters": {"PERIOD": 20, "PERCENT": "2"}}
        ),
        candles,
    )
    lower = engine.compute(
        ExpressionNode.model_validate(
            _indicator("ENVELOPE", output="LOWER") | {"parameters": {"PERIOD": 20, "PERCENT": "2"}}
        ),
        candles,
    )
    assert upper == middle * Decimal("1.02")
    assert lower == middle * Decimal("0.98")


def test_explicit_source_and_provider_are_part_of_indicator_identity() -> None:
    node = _indicator("RSI") | {"source": "LOCAL"}
    node["provider"] = None
    explicit = _indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}
    local_key = indicator_key(ExpressionNode.model_validate(node))
    broker_key = indicator_key(ExpressionNode.model_validate(explicit))
    assert local_key != broker_key
    assert DEFAULT_REGISTRY.get("외국인순매수대금").source == "BROKER"


def test_broker_search_boolean_signal_can_be_injected_without_calculation() -> None:
    node = _indicator(
        "BROKER_SEARCH_MATCH",
        parameters={"search_id": "A"},
    ) | {"source": "BROKER", "provider": "LS"}
    condition = {"type": "COMPARISON", "operator": "EQ", "left": node, "right": {"type": "LITERAL", "value": True, "unit": "BOOL"}}
    spec = _rule(condition)
    validate_rule_spec(spec)
    parsed = spec.condition.left
    key = indicator_key(parsed, market_data_source_id="LS_PAPER_MARKET_DATA")
    context = IndicatorEngine().build_context(
        spec,
        bars={},
        portfolio={},
        external_indicators={
            key: IndicatorValue(
                value=True,
                indicator="BROKER_SEARCH_MATCH",
                source="BROKER",
                provider="LS",
                observed_at=NOW,
                data_timestamp=NOW,
                calculation_version="v1",
                output="VALUE",
                timeframe="1D",
                parameters={"SEARCH_ID": "A"},
                market_data_source_id="LS_PAPER_MARKET_DATA",
            )
        },
        market_data_source_id="LS_PAPER_MARKET_DATA",
    )
    assert context.market_data_source_id == "LS_PAPER_MARKET_DATA"
    assert evaluate_condition(spec, context) is True


def test_broker_indicator_without_provider_value_fails_closed() -> None:
    node = _indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}
    spec = _rule({"type": "COMPARISON", "operator": "GT", "left": node, "right": {"type": "LITERAL", "value": "0", "unit": "KRW"}})
    validate_rule_spec(spec)
    with pytest.raises(EvaluationError) as raised:
        IndicatorEngine().build_context(spec, bars={}, portfolio={})
    assert raised.value.code == "INDICATOR_PROVIDER_UNAVAILABLE"


def test_source_mismatch_is_rejected_before_runtime() -> None:
    node = _indicator("RSI") | {"source": "BROKER", "provider": "LS"}
    spec = _rule({"type": "COMPARISON", "operator": "LT", "left": node, "right": {"type": "LITERAL", "value": "30", "unit": "NUMBER"}})
    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "INDICATOR_SOURCE_MISMATCH"


def _broker_spec(name: str = "FOREIGN_NET_BUY_AMOUNT", *, clock: str = "BAR_CLOSE") -> ConditionalRuleSpec:
    node = _indicator(name) | {"source": "BROKER", "provider": "LS"}
    return _rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": node,
            "right": {"type": "LITERAL", "value": "0", "unit": "KRW"},
        },
        clock=clock,
    )


def _active(spec: ConditionalRuleSpec) -> ActiveRule:
    return ActiveRule(
        rule_id=UUID("50000000-0000-0000-0000-000000000001"),
        rule_version=1,
        row_version=1,
        spec_sha256="a" * 64,
        spec=spec,
    )


def _ls_registry(resolver) -> IndicatorRegistry:
    registry = IndicatorRegistry([DEFAULT_REGISTRY.get("FOREIGN_NET_BUY_AMOUNT")])
    registry.register_provider(LSBrokerIndicatorProvider(resolver=resolver))
    return registry


def test_calculator_and_resolver_contracts_are_exact() -> None:
    calculator = DEFAULT_REGISTRY.get("SMA").calculator
    assert calculator is not None
    assert tuple(inspect.signature(calculator).parameters) == (
        "name",
        "candles",
        "parameters",
        "output",
    )
    assert tuple(inspect.signature(LSBrokerIndicatorProvider.resolve).parameters) == (
        "self",
        "instrument",
        "indicator_spec",
        "evaluation_context",
    )
    with pytest.raises(ValueError, match="indicator resolver"):
        LSBrokerIndicatorProvider(resolver=lambda instrument, indicator_spec: None)


def test_ls_resolver_returns_only_normalized_indicator_value() -> None:
    async def resolver(instrument, indicator_spec, evaluation_context):
        return IndicatorValue(
            value=Decimal("123"),
            indicator="FOREIGN_NET_BUY_AMOUNT",
            source="BROKER",
            provider="LS",
            observed_at=NOW,
        )

    value = asyncio.run(
        _ls_registry(resolver).resolve(
            "005930",
            ExpressionNode.model_validate(_indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}),
            {"market_data_source_id": "LS_REALTIME"},
        )
    )
    assert isinstance(value, IndicatorValue)
    assert value.output == "VALUE"
    assert value.timeframe == "1D"
    assert value.calculation_version == "v1"
    assert value.market_data_source_id == "LS_REALTIME"


def test_default_ls_resolver_is_bound_but_unconfigured_fails_closed() -> None:
    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            DEFAULT_REGISTRY.resolve(
                "005930",
                ExpressionNode.model_validate(_indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}),
                {},
            )
        )
    assert raised.value.code == "INDICATOR_PROVIDER_UNAVAILABLE"


def test_registry_rejects_unsupported_indicator_and_provider() -> None:
    with pytest.raises(IndicatorProviderError) as unsupported:
        asyncio.run(
            DEFAULT_REGISTRY.resolve(
                "005930", ExpressionNode.model_validate(_indicator("DOES_NOT_EXIST")), {}
            )
        )
    assert unsupported.value.code == "UNSUPPORTED_INDICATOR"

    with pytest.raises(IndicatorProviderError) as mismatch:
        asyncio.run(
            DEFAULT_REGISTRY.resolve(
                "005930",
                ExpressionNode.model_validate(_indicator("RSI") | {"provider": "LS"}),
                {},
            )
        )
    assert mismatch.value.code == "INDICATOR_PROVIDER_MISMATCH"


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("timeout", "INDICATOR_PROVIDER_TIMEOUT"),
        ("raw", "INDICATOR_PROVIDER_INVALID_PAYLOAD"),
        ("partial", "INDICATOR_PROVIDER_PARTIAL_DATA"),
    ),
)
def test_ls_resolver_failure_modes_fail_closed(failure: str, expected: str) -> None:
    async def resolver(instrument, indicator_spec, evaluation_context):
        if failure == "timeout":
            raise TimeoutError("LS timeout")
        if failure == "raw":
            return {"output1": "123"}
        return IndicatorValue(
            value=None,  # type: ignore[arg-type]
            indicator="FOREIGN_NET_BUY_AMOUNT",
            source="BROKER",
            provider="LS",
            observed_at=NOW,
        )

    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            _ls_registry(resolver).resolve(
                "005930",
                ExpressionNode.model_validate(_indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}),
                {},
            )
        )
    assert raised.value.code == expected


def test_ls_unsupported_tr_is_terminal_and_never_falls_back_to_local() -> None:
    called = False

    async def resolver(instrument, indicator_spec, evaluation_context):
        nonlocal called
        called = True
        return IndicatorValue(
            value=Decimal("1"),
            indicator="FOREIGN_NET_BUY_AMOUNT",
            source="BROKER",
            provider="LS",
            observed_at=NOW,
        )

    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            _ls_registry(resolver).resolve(
                "005930",
                ExpressionNode.model_validate(_indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}),
                {"tr_code": "UNSUPPORTED_TR"},
            )
        )
    assert raised.value.code == "INDICATOR_TR_UNSUPPORTED"
    assert called is False


def test_provider_failure_does_not_trigger_local_fallback() -> None:
    async def resolver(instrument, indicator_spec, evaluation_context):
        raise IndicatorProviderError("INDICATOR_PROVIDER_TIMEOUT", "timeout")

    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            _ls_registry(resolver).resolve(
                "005930",
                ExpressionNode.model_validate(_indicator("FOREIGN_NET_BUY_AMOUNT") | {"source": "BROKER", "provider": "LS"}),
                {},
            )
        )
    assert raised.value.code == "INDICATOR_PROVIDER_TIMEOUT"


def test_evaluator_rejects_raw_broker_value_instead_of_calculating_it() -> None:
    spec = _broker_spec()
    validate_rule_spec(spec)
    with pytest.raises(EvaluationError) as raised:
        IndicatorEngine().build_context(
            spec,
            bars={},
            portfolio={},
            external_indicators={indicator_key(spec.condition.left): Decimal("1")},  # type: ignore[arg-type]
        )
    assert raised.value.code == "INDICATOR_PROVIDER_INVALID_PAYLOAD"


def test_bar_close_and_quote_clocks_cannot_mix_realtime_indicator() -> None:
    node = _indicator("VI_STATUS") | {"source": "BROKER", "provider": "LS"}
    bar_spec = _rule(
        {"type": "COMPARISON", "operator": "EQ", "left": node, "right": {"type": "LITERAL", "value": True, "unit": "BOOL"}}
    )
    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(bar_spec)
    assert raised.value.code == "INDICATOR_REQUIRES_REALTIME"

    quote_spec = _rule(
        {"type": "COMPARISON", "operator": "EQ", "left": node, "right": {"type": "LITERAL", "value": True, "unit": "BOOL"}},
        clock="QUOTE",
    )
    validate_rule_spec(quote_spec)
    key = indicator_key(
        quote_spec.condition.left, market_data_source_id="LS_REALTIME"
    )
    context = IndicatorEngine().build_context(
        quote_spec,
        bars={},
        portfolio={},
        current_market={"LAST_PRICE": Decimal("100")},
        external_indicators={
            key: IndicatorValue(
                value=True,
                indicator="VI_STATUS",
                source="BROKER",
                provider="LS",
                observed_at=NOW,
                data_timestamp=NOW,
                output="VALUE",
                timeframe="1D",
                market_data_source_id="LS_REALTIME",
            )
        },
        market_data_source_id="LS_REALTIME",
    )
    assert evaluate_condition(quote_spec, context) is True
    assert context.current.observed_at == NOW


def test_required_history_uses_registry_and_skips_broker_warmup() -> None:
    broker_history = _required_history(_active(_broker_spec()))
    assert broker_history == {}

    local_spec = _rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": _indicator("SMA", parameters={"period": 20}),
            "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
        }
    )
    assert _required_history(_active(local_spec)) == {local_spec.evaluation.primary_timeframe: 20}


def test_dependency_and_cache_identity_include_all_result_dimensions() -> None:
    root = ExpressionNode.model_validate(
        {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                _indicator("RSI", parameters={"period": 14}),
                _indicator("RSI", parameters={"period": 21}),
            ],
        }
    )
    assert len(build_dependency_graph(root).nodes) == 2
    assert build_dependency_graph(
        root, market_data_source_id="KRX_BARS"
    ).nodes_by_key != build_dependency_graph(
        root, market_data_source_id="LS_REALTIME"
    ).nodes_by_key

    base = {
        "indicator": "RSI",
        "source": "LOCAL",
        "provider": "LOCAL",
        "timeframe": "1D",
        "parameters": {"PERIOD": 14},
        "output": "VALUE",
        "market_data_source": "KRX_BARS",
        "calculation_version": "v1",
        "market": "KRX",
        "instrument": "005930",
        "bar_timestamp": NOW.isoformat(),
    }
    for field, value in (
        ("indicator", "EMA"),
        ("source", "BROKER"),
        ("provider", "LS"),
        ("timeframe", "5M"),
        ("parameters", {"PERIOD": 15}),
        ("output", "UPPER"),
        ("market_data_source", "LS_REALTIME"),
        ("calculation_version", "v2"),
    ):
        changed = dict(base)
        changed[field] = value
        assert indicator_cache_key(**changed) != indicator_cache_key(**base), field


def test_ls_raw_payload_parser_is_inside_broker_boundary() -> None:
    value = normalize_ls_payload(
        {"foreign_amount": "123"},
        indicator="FOREIGN_NET_BUY_AMOUNT",
        value_field="foreign_amount",
        observed_at=NOW,
    )
    assert value.value == Decimal("123")
    with pytest.raises(IndicatorProviderError) as raised:
        normalize_ls_payload(
            {},
            indicator="FOREIGN_NET_BUY_AMOUNT",
            value_field="foreign_amount",
            observed_at=NOW,
        )
    assert raised.value.code == "INDICATOR_PROVIDER_PARTIAL_DATA"


def test_bollinger_accepts_the_hts_offset_argument() -> None:
    """Requested as "bollingerband(종가,2,0,20) 중심선 터치" on 2026-08-28.

    Korean HTS notation is positional and always carries an offset argument.
    The interpreter mapped it to ``OFFSET`` and the registry declared only
    ``PERIOD``/``STDDEV``, so the whole rule died on
    ``UNSUPPORTED_INDICATOR_PARAMETER`` before it could become ACTIVE.
    """

    definition = get_indicator_definition("BOLLINGER")
    assert definition["defaults"]["OFFSET"] == "0"
    spec = _rule(
        {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "COMPARISON",
                    "operator": "LTE",
                    "left": {"type": "MARKET", "field": "LOW"},
                    "right": _indicator(
                        "BOLLINGER",
                        output="MIDDLE",
                        parameters={"PERIOD": 20, "STDDEV": "2", "OFFSET": 0},
                    ),
                },
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "HIGH"},
                    "right": _indicator(
                        "BOLLINGER",
                        output="MIDDLE",
                        parameters={"PERIOD": 20, "STDDEV": "2", "OFFSET": 0},
                    ),
                },
            ],
        }
    )
    assert validate_rule_spec(spec) is spec


def test_offset_shifts_every_local_indicator_back_by_completed_bars() -> None:
    engine = IndicatorEngine()
    candles = _candles(30)
    latest = engine.compute(
        ExpressionNode.model_validate(_indicator("SMA", parameters={"PERIOD": 5})),
        candles,
    )
    shifted = engine.compute(
        ExpressionNode.model_validate(
            _indicator("SMA", parameters={"PERIOD": 5, "OFFSET": 3})
        ),
        candles,
    )
    assert shifted == engine.compute(
        ExpressionNode.model_validate(_indicator("SMA", parameters={"PERIOD": 5})),
        candles[:-3],
    )
    assert shifted != latest


def test_offset_extends_the_warm_up_window_it_consumes() -> None:
    definition = DEFAULT_REGISTRY.get("BOLLINGER")
    assert definition.required_history({"PERIOD": 20, "STDDEV": Decimal("2"), "OFFSET": 0}) == 20
    assert definition.required_history({"PERIOD": 20, "STDDEV": Decimal("2"), "OFFSET": 5}) == 25
    engine = IndicatorEngine()
    with pytest.raises(EvaluationError) as raised:
        engine.compute(
            ExpressionNode.model_validate(
                _indicator("SMA", parameters={"PERIOD": 5, "OFFSET": 4})
            ),
            _candles(6),
        )
    assert raised.value.code == "INSUFFICIENT_HISTORY"


def test_offset_is_the_only_parameter_allowed_to_be_zero() -> None:
    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(
            _rule(
                {
                    "type": "COMPARISON",
                    "operator": "GT",
                    "left": {"type": "MARKET", "field": "CLOSE"},
                    "right": _indicator("SMA", parameters={"PERIOD": 0}),
                }
            )
        )
    assert raised.value.code == "INVALID_INDICATOR_PARAMETER"
    with pytest.raises(RuleSemanticError) as negative:
        validate_rule_spec(
            _rule(
                {
                    "type": "COMPARISON",
                    "operator": "GT",
                    "left": {"type": "MARKET", "field": "CLOSE"},
                    "right": _indicator("SMA", parameters={"PERIOD": 5, "OFFSET": -1}),
                }
            )
        )
    assert negative.value.code == "INVALID_INDICATOR_PARAMETER"


def test_unknown_indicator_parameters_are_still_rejected() -> None:
    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(
            _rule(
                {
                    "type": "COMPARISON",
                    "operator": "GT",
                    "left": {"type": "MARKET", "field": "CLOSE"},
                    "right": _indicator(
                        "BOLLINGER",
                        output="UPPER",
                        parameters={"PERIOD": 20, "PRICE": "종가"},
                    ),
                }
            )
        )
    assert raised.value.code == "UNSUPPORTED_INDICATOR_PARAMETER"
