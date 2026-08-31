from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestration.conditional_rules import (
    ConditionalRuleSpec,
    TemporalSequenceState,
    advance_temporal_sequence,
    validate_rule_spec,
)
from orchestration.conditional_rules.semantic import TemporalSequenceParameters


NOW = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
PARAMETERS = TemporalSequenceParameters(window_bars=20)


def _rule() -> ConditionalRuleSpec:
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
            "condition": {
                "type": "TEMPORAL_SEQUENCE",
                "parameters": {"WINDOW_BARS": 20},
                "children": [
                    {
                        "type": "CROSS",
                        "operator": "BELOW",
                        "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "1D"},
                        "right": {"type": "LITERAL", "value": "30", "unit": "NUMBER"},
                    },
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
                    {
                        "type": "CROSS",
                        "operator": "ABOVE",
                        "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "1D"},
                        "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
                    },
                ],
            },
            "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "5"}},
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
            "expires_at": "2026-09-30T00:00:00+00:00",
            "raw_instruction_sha256": "0" * 64,
        }
    )


def test_temporal_sequence_contract_is_a_valid_root_bar_rule() -> None:
    spec = _rule()

    assert validate_rule_spec(spec) is spec


def test_temporal_sequence_arms_then_triggers_within_the_window() -> None:
    armed = advance_temporal_sequence(
        None,
        parameters=PARAMETERS,
        arm_result=True,
        trigger_result=False,
        cancel_result=False,
        observed_at=NOW,
    )
    assert armed.state == TemporalSequenceState(NOW, 20, NOW)
    assert armed.condition_result is False

    triggered = advance_temporal_sequence(
        armed.state,
        parameters=PARAMETERS,
        arm_result=False,
        trigger_result=True,
        cancel_result=False,
        observed_at=NOW + timedelta(minutes=5),
    )
    assert triggered.condition_result is True
    assert triggered.cancelled is False


def test_temporal_cancel_wins_over_a_same_bar_trigger() -> None:
    state = TemporalSequenceState(NOW, 20, NOW)

    observation = advance_temporal_sequence(
        state,
        parameters=PARAMETERS,
        arm_result=False,
        trigger_result=True,
        cancel_result=True,
        observed_at=NOW + timedelta(minutes=5),
    )

    assert observation.state is None
    assert observation.condition_result is False
    assert observation.cancelled is True


def test_temporal_sequence_expires_after_twenty_subsequent_bars() -> None:
    state = TemporalSequenceState(NOW, 20, NOW)
    observation = None
    for index in range(1, 21):
        observation = advance_temporal_sequence(
            state,
            parameters=PARAMETERS,
            arm_result=False,
            trigger_result=False,
            cancel_result=False,
            observed_at=NOW + timedelta(minutes=5 * index),
        )
        state = observation.state
        if index < 20:
            assert state is not None

    assert observation is not None
    assert observation.window_expired is True
    assert observation.state is None


def test_temporal_sequence_ignores_duplicate_or_older_bars() -> None:
    state = TemporalSequenceState(NOW, 20, NOW)

    duplicate = advance_temporal_sequence(
        state,
        parameters=PARAMETERS,
        arm_result=False,
        trigger_result=True,
        cancel_result=False,
        observed_at=NOW,
    )

    assert duplicate.ignored_stale_bar is True
    assert duplicate.condition_result is False
    assert duplicate.state == state
