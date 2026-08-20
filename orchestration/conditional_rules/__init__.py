"""Deterministic contracts and evaluation for authenticated PAPER rules."""

from .contracts import (
    ActionSide,
    ConditionalRuleSpec,
    EvaluationClock,
    EvaluationPolicy,
    ExecutionMode,
    ExpressionNode,
    ExpressionType,
    MarketClosedPolicy,
    RuleAction,
    RuleState,
    SizingPolicy,
    SizingType,
    Timeframe,
    expression_fingerprint,
    rule_fingerprint,
)
from .evaluator import (
    Candle,
    EvaluationContext,
    EvaluationError,
    EvaluationFrame,
    IndicatorEngine,
    evaluate_condition,
)
from .execution import (
    ExecutionGuardInput,
    GuardDecision,
    guard_rule_execution,
)
from .identities import evaluation_id, execution_idempotency_key, trigger_id
from .semantic import RuleSemanticError, validate_rule_spec
from .worker_store import (
    ActiveRule,
    PostgresRuleWorkerStore,
    RuleWorkerStoreError,
    SubmitReadyExecution,
    TriggerClaim,
)

__all__ = [
    "ActionSide",
    "ActiveRule",
    "Candle",
    "ConditionalRuleSpec",
    "EvaluationClock",
    "EvaluationContext",
    "EvaluationError",
    "EvaluationFrame",
    "EvaluationPolicy",
    "ExecutionGuardInput",
    "ExecutionMode",
    "ExpressionNode",
    "ExpressionType",
    "GuardDecision",
    "IndicatorEngine",
    "MarketClosedPolicy",
    "PostgresRuleWorkerStore",
    "RuleAction",
    "RuleSemanticError",
    "RuleState",
    "RuleWorkerStoreError",
    "SizingPolicy",
    "SizingType",
    "Timeframe",
    "SubmitReadyExecution",
    "TriggerClaim",
    "evaluate_condition",
    "evaluation_id",
    "execution_idempotency_key",
    "expression_fingerprint",
    "guard_rule_execution",
    "rule_fingerprint",
    "trigger_id",
    "validate_rule_spec",
]
