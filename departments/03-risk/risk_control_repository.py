"""Transactional persistence for compiled Mandates and position Risk Plans."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any
from uuid import UUID

from mandate_limit_compiler import MandateLimitCompilation
from position_risk_lifecycle import (
    RiskPlanTransition,
    validate_transition,
)
from position_risk_planner import PlanAction, PositionRiskPlan


class RiskControlPersistenceError(RuntimeError):
    """A canonical Risk control could not be persisted atomically."""


@lru_cache(maxsize=1)
def _driver() -> tuple[Any, Any]:
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise RiskControlPersistenceError(
            "psycopg2-binary is required for canonical Risk control persistence"
        ) from exc
    return Json, ThreadedConnectionPool


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


class RiskControlRepository:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> "RiskControlRepository":
        _, pool_type = _driver()
        return cls(pool_type(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def activate_compilation(self, compilation: MandateLimitCompilation) -> UUID:
        """Activate one compiled policy and all limits in one transaction."""

        if compilation.status != "COMPILED":
            raise RiskControlPersistenceError("only COMPILED mandates can be activated")
        if (
            compilation.policy_id is None
            or compilation.policy_version is None
            or compilation.content_hash is None
        ):
            raise RiskControlPersistenceError("compiled policy identity is incomplete")

        Json, _ = _driver()
        connection = self._pool.getconn()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select content_hash
                    from risk.policies
                    where policy_id = %s
                    for update
                    """,
                    (compilation.policy_id,),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    if str(replay[0]) != compilation.content_hash:
                        raise RiskControlPersistenceError(
                            "policy identity collision with different content"
                        )
                    connection.commit()
                    return compilation.policy_id

                cursor.execute(
                    """
                    update risk.policies
                    set status = 'RETIRED', effective_to = %s
                    where fund_id = %s and policy_code = %s and status = 'ACTIVE'
                      and effective_from < %s
                    """,
                    (
                        compilation.effective_from,
                        compilation.fund_id,
                        compilation.policy_code,
                        compilation.effective_from,
                    ),
                )
                cursor.execute(
                    """
                    insert into risk.policies (
                      policy_id, fund_id, policy_code, version, scope, rules,
                      effective_from, status, content_hash, mandate_version_id
                    ) values (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s)
                    """,
                    (
                        compilation.policy_id,
                        compilation.fund_id,
                        compilation.policy_code,
                        compilation.policy_version,
                        Json(_json_safe(compilation.policy_scope)),
                        Json(_json_safe(compilation.policy_rules)),
                        compilation.effective_from,
                        compilation.content_hash,
                        UUID(compilation.mandate_version_id),
                    ),
                )
                for limit in compilation.limits:
                    cursor.execute(
                        """
                        insert into risk.limits (
                          fund_id, policy_id, scope_type, scope_id, metric,
                          soft_limit, hard_limit, unit, effective_from, status
                        ) values (%s, %s, 'FUND', %s, %s, %s, %s, %s, %s, 'ACTIVE')
                        """,
                        (
                            compilation.fund_id,
                            compilation.policy_id,
                            str(compilation.fund_id),
                            limit.metric,
                            limit.soft_limit,
                            limit.hard_limit,
                            limit.unit,
                            compilation.effective_from,
                        ),
                    )
                cursor.execute(
                    """
                    insert into risk.mandate_version_bindings (
                      mandate_version_id, mandate_id, fund_id, policy_id,
                      mindset, experience, preset_version, compiler_version,
                      input_hash, content_hash, trace_id
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        UUID(compilation.mandate_version_id),
                        compilation.mandate_id,
                        compilation.fund_id,
                        compilation.policy_id,
                        compilation.mindset,
                        compilation.experience,
                        compilation.preset_version,
                        compilation.compiler_version,
                        compilation.input_hash,
                        compilation.content_hash,
                        compilation.trace_id,
                    ),
                )
            connection.commit()
            return compilation.policy_id
        except RiskControlPersistenceError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise RiskControlPersistenceError(
                f"canonical Mandate limit activation failed: {exc}"
            ) from exc
        finally:
            self._pool.putconn(connection)

    def save_plan(self, plan: PositionRiskPlan) -> UUID:
        """Persist a numeric PROPOSE result; DEFER/REDUCE_ONLY are observations."""

        if plan.action is not PlanAction.PROPOSE:
            raise RiskControlPersistenceError("only numeric PROPOSE plans are persisted")
        Json, _ = _driver()
        connection = self._pool.getconn()
        payload = plan.model_dump(mode="python")
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into risk.position_risk_plans (
                      risk_plan_id, fund_id, instrument_id, mandate_version_id,
                      portfolio_snapshot_id, market_snapshot_id, as_of, expires_at,
                      regime, action, state, entry_reference, stop_price,
                      take_profit_price, trailing_activation_price, trailing_distance,
                      position_risk_amount, quantity_cap, current_quantity,
                      reward_risk_ratio, liquidation_stages, calculation_version,
                      input_hash, data_quality, reason_codes, review_triggers,
                      execution_mode, trace_id, task_id
                    ) values (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PROPOSED',
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s
                    )
                    on conflict (fund_id, instrument_id, input_hash, calculation_version)
                    do nothing
                    returning risk_plan_id
                    """,
                    (
                        plan.risk_plan_id,
                        plan.fund_id,
                        plan.instrument_id,
                        UUID(plan.mandate_version_id),
                        plan.portfolio_snapshot_id,
                        plan.market_snapshot_id,
                        plan.as_of,
                        plan.expires_at,
                        plan.regime,
                        plan.action,
                        plan.entry_reference,
                        plan.stop_price,
                        plan.take_profit_price,
                        plan.trailing_activation_price,
                        plan.trailing_distance,
                        plan.position_risk_amount,
                        plan.quantity_cap,
                        plan.current_quantity,
                        plan.reward_risk_ratio,
                        Json(_json_safe(payload["liquidation_stages"])),
                        plan.calculation_version,
                        plan.input_hash,
                        plan.data_quality,
                        Json(plan.reason_codes),
                        Json(plan.review_triggers),
                        plan.execution_mode,
                        plan.trace_id,
                        plan.task_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        select risk_plan_id
                        from risk.position_risk_plans
                        where fund_id = %s and instrument_id = %s
                          and input_hash = %s and calculation_version = %s
                        """,
                        (
                            plan.fund_id,
                            plan.instrument_id,
                            plan.input_hash,
                            plan.calculation_version,
                        ),
                    )
                    row = cursor.fetchone()
                if row is None or UUID(str(row[0])) != plan.risk_plan_id:
                    raise RiskControlPersistenceError("risk plan replay identity mismatch")
            connection.commit()
            return plan.risk_plan_id
        except RiskControlPersistenceError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise RiskControlPersistenceError(
                f"position Risk Plan persistence failed: {exc}"
            ) from exc
        finally:
            self._pool.putconn(connection)

    def transition_plan(self, transition: RiskPlanTransition) -> str:
        """Append one authority event and atomically update the current state."""

        validate_transition(transition)
        connection = self._pool.getconn()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select state from risk.position_risk_plans
                    where risk_plan_id = %s for update
                    """,
                    (transition.risk_plan_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RiskControlPersistenceError("position Risk Plan not found")
                if str(row[0]) == transition.to_state:
                    connection.commit()
                    return str(row[0])
                if str(row[0]) != transition.from_state:
                    raise RiskControlPersistenceError(
                        f"stale transition: canonical state is {row[0]}"
                    )
                cursor.execute(
                    """
                    insert into risk.position_risk_plan_events (
                      risk_plan_id, from_state, to_state, actor_type, actor_id,
                      reason, trace_id, task_id, idempotency_key, occurred_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (idempotency_key) do nothing
                    returning event_id
                    """,
                    (
                        transition.risk_plan_id,
                        transition.from_state,
                        transition.to_state,
                        transition.actor_type,
                        transition.actor_id,
                        transition.reason,
                        transition.trace_id,
                        transition.task_id,
                        transition.idempotency_key,
                        transition.occurred_at,
                    ),
                )
                event = cursor.fetchone()
                if event is None:
                    raise RiskControlPersistenceError(
                        "transition idempotency key was already used"
                    )
                cursor.execute(
                    """
                    update risk.position_risk_plans set state = %s
                    where risk_plan_id = %s and state = %s
                    """,
                    (
                        transition.to_state,
                        transition.risk_plan_id,
                        transition.from_state,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RiskControlPersistenceError("risk plan state update lost race")
            connection.commit()
            return transition.to_state.value
        except RiskControlPersistenceError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise RiskControlPersistenceError(
                f"position Risk Plan transition failed: {exc}"
            ) from exc
        finally:
            self._pool.putconn(connection)


__all__ = [
    "RiskControlPersistenceError",
    "RiskControlRepository",
]
