"""Canonical DB writer for Risk P1 snapshots.

The repository is intentionally opt-in.  It never creates a fund, book,
instrument mapping, or stress scenario as a fallback because those are
governance-owned records and doing so would hide an FK/RLS configuration bug.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from .analytics import P1RiskSnapshot


class RiskP1PersistenceError(RuntimeError):
    """Raised when the complete snapshot transaction cannot be committed."""


class RiskP1Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def save_snapshot(
        self,
        snapshot: P1RiskSnapshot,
        *,
        stress_scenario_ids: Mapping[str, UUID],
        trace_id: UUID,
        kill_switch_transition: Mapping[str, Any] | None = None,
    ) -> UUID:
        """Persist snapshot, components, stress results, and optional switch event atomically."""

        risk_snapshot_id = uuid4()
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                insert into risk.snapshots
                  (risk_snapshot_id, fund_id, book_id, strategy_version_id, as_of,
                   gross_exposure, net_exposure, value_at_risk, expected_shortfall,
                   quality_status, input_hash, calculation_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fund_id, book_id, strategy_version_id, as_of, calculation_version)
                do update set gross_exposure = excluded.gross_exposure,
                              net_exposure = excluded.net_exposure,
                              value_at_risk = excluded.value_at_risk,
                              expected_shortfall = excluded.expected_shortfall,
                              quality_status = excluded.quality_status,
                              input_hash = excluded.input_hash
                returning risk_snapshot_id
                """,
                (
                    risk_snapshot_id,
                    snapshot.fund_id,
                    snapshot.book_id,
                    snapshot.strategy_version_id,
                    snapshot.as_of,
                    snapshot.gross_exposure,
                    snapshot.net_exposure,
                    snapshot.value_at_risk,
                    snapshot.expected_shortfall,
                    snapshot.quality_status,
                    snapshot.input_hash,
                    snapshot.calculation_version,
                ),
            )
            row = cursor.fetchone()
            if not row:
                raise RiskP1PersistenceError("risk snapshot upsert returned no id")
            risk_snapshot_id = row[0]

            for component in snapshot.exposure_components:
                cursor.execute(
                    """
                    insert into risk.exposure_components
                      (risk_snapshot_id, dimension, dimension_id, value, unit, metadata)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (risk_snapshot_id, dimension, dimension_id, unit)
                    do update set value = excluded.value, metadata = excluded.metadata
                    """,
                    (
                        risk_snapshot_id,
                        component["dimension"],
                        component["dimension_id"],
                        component["value"],
                        component["unit"],
                        _json(component.get("metadata", {})),
                    ),
                )

            for scenario_code, loss in snapshot.stress_losses.items():
                scenario_id = stress_scenario_ids.get(scenario_code)
                if scenario_id is None:
                    raise RiskP1PersistenceError(
                        f"approved stress scenario id missing: {scenario_code}"
                    )
                cursor.execute(
                    """
                    insert into risk.stress_results
                      (risk_snapshot_id, scenario_id, loss, breached_limit_ids,
                       component_results, code_version)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (risk_snapshot_id, scenario_id, code_version)
                    do update set loss = excluded.loss,
                                  component_results = excluded.component_results
                    """,
                    (
                        risk_snapshot_id,
                        scenario_id,
                        loss,
                        [],
                        _json({"scenario": scenario_code}),
                        snapshot.calculation_version,
                    ),
                )

            if kill_switch_transition is not None:
                self._insert_kill_switch_event(
                    cursor, snapshot, trace_id, kill_switch_transition
                )
            self.connection.commit()
            return risk_snapshot_id
        except Exception as exc:
            self.connection.rollback()
            if isinstance(exc, RiskP1PersistenceError):
                raise
            raise RiskP1PersistenceError("risk P1 transaction rolled back") from exc
        finally:
            cursor.close()

    @staticmethod
    def _insert_kill_switch_event(
        cursor: Any,
        snapshot: P1RiskSnapshot,
        trace_id: UUID,
        transition: Mapping[str, Any],
    ) -> None:
        required = ("to_state", "trigger_type", "requested_by")
        missing = [key for key in required if not str(transition.get(key, "")).strip()]
        if missing:
            raise RiskP1PersistenceError(
                f"kill switch transition missing: {', '.join(missing)}"
            )
        cursor.execute(
            """
            insert into risk.kill_switch_events
              (fund_id, from_state, to_state, trigger_type, trigger_details,
               evidence, requested_by, approved_release_by, trace_id, occurred_at, released_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                snapshot.fund_id,
                transition.get("from_state"),
                transition["to_state"],
                transition["trigger_type"],
                _json(transition.get("trigger_details", {})),
                _json(
                    transition.get(
                        "evidence", {"risk_snapshot_id": str(snapshot.fund_id)}
                    )
                ),
                transition["requested_by"],
                transition.get("approved_release_by"),
                trace_id,
                transition.get("occurred_at", snapshot.as_of),
                transition.get("released_at"),
            ),
        )


def _json(value: Any) -> Any:
    """Use psycopg2 Json when available, while keeping unit tests dependency-free."""

    try:
        from psycopg2.extras import Json
    except ImportError:  # pragma: no cover - deployment dependency
        return value
    return Json(value)
