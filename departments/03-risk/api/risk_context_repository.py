"""Read-only canonical Risk context loader.

RiskEngine remains deterministic and side-effect free. This adapter resolves
Fund, Governance, Portfolio, Market, Policy and Counterparty inputs from the
canonical Supabase schemas at one PIT cutoff; the Redis Trading State is
passed in separately because Redis is the live kill-switch source.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from engine.risk_engine import (
    CounterpartyHealth,
    CounterpartyStatus,
    LimitSet,
    MandateScope,
    MarketStatus,
    PortfolioState,
    RestrictedItem,
    RestrictionType,
    RiskContext,
    TradingState,
)


class RiskContextLoadError(RuntimeError):
    """Raised when canonical inputs cannot produce a complete Risk context."""


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskContextLoadError(
            f"invalid canonical Risk field: {field_name}"
        ) from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RiskContextLoadError(
            f"canonical Risk JSON field is not an object: {field_name}"
        )
    return value


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _required_decimal(
    mapping: Mapping[str, Any], field_name: str, *keys: str
) -> Decimal:
    value = _first(mapping, *keys)
    if value is None:
        raise RiskContextLoadError(f"canonical Risk policy field missing: {field_name}")
    return _decimal(value, field_name)


def _uuid_set(value: Any, field_name: str) -> frozenset[UUID] | None:
    if value is None:
        raise RiskContextLoadError(f"canonical Risk policy field missing: {field_name}")
    if not isinstance(value, (list, tuple, set)):
        raise RiskContextLoadError(
            f"canonical Risk policy field is not a list: {field_name}"
        )
    try:
        return frozenset(UUID(str(item)) for item in value)
    except (TypeError, ValueError) as exc:
        raise RiskContextLoadError(
            f"invalid canonical Risk UUID list: {field_name}"
        ) from exc


def _row_value(row: Any, index: int, field_name: str) -> Any:
    if row is None or len(row) <= index:
        raise RiskContextLoadError(
            f"canonical Risk query returned incomplete row: {field_name}"
        )
    return row[index]


def _restriction(value: Any) -> RestrictionType:
    raw = str(value)
    try:
        return RestrictionType(raw)
    except ValueError:
        try:
            return RestrictionType[raw.upper()]
        except KeyError as exc:
            raise RiskContextLoadError("unknown canonical restriction type") from exc


class PostgresRiskContextRepository:
    """Load one complete, read-only Risk context from canonical PostgreSQL."""

    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    @classmethod
    def connect(cls, dsn: str) -> PostgresRiskContextRepository:
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RiskContextLoadError(
                "psycopg2-binary is required for canonical Risk context"
            ) from exc
        return cls(lambda: psycopg2.connect(dsn, connect_timeout=6))

    def load(
        self,
        *,
        fund_id: UUID,
        book_id: UUID,
        instrument_id: UUID,
        broker_adapter: str,
        as_of: datetime,
        trading_state: TradingState,
    ) -> RiskContext:
        connection = None
        try:
            connection = self._connection_factory()
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT current_setting('transaction_read_only')")
                if str(cursor.fetchone()[0]).lower() not in {"on", "true", "1"}:
                    raise RiskContextLoadError(
                        "canonical Risk transaction is not read-only"
                    )

                mandate = self._load_mandate(cursor, fund_id, as_of)
                limits = self._load_limits(cursor, fund_id, as_of)
                restricted = self._load_restricted(
                    cursor, fund_id, instrument_id, as_of
                )
                portfolio = self._load_portfolio(cursor, fund_id, book_id, as_of)
                market = self._load_market(cursor, instrument_id, as_of)
                counterparty = self._load_counterparty(cursor, broker_adapter, as_of)
            connection.rollback()
            return RiskContext(
                mandate=mandate,
                limits=limits,
                restricted_items=restricted,
                portfolio=portfolio,
                market_status=market,
                counterparty=counterparty,
                trading_state=trading_state,
                as_of=as_of,
            )
        except RiskContextLoadError:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as exc:
            if connection is not None:
                connection.rollback()
            raise RiskContextLoadError("canonical Risk context query failed") from exc
        finally:
            if connection is not None:
                connection.close()

    def _load_mandate(
        self, cursor: Any, fund_id: UUID, as_of: datetime
    ) -> MandateScope:
        cursor.execute(
            """
            SELECT p.scope,
                   p.rules,
                   (
                       SELECT mv.universe_policy
                       FROM governance.mandates m2
                       JOIN governance.mandate_versions mv
                         ON mv.mandate_id = m2.mandate_id
                       WHERE m2.fund_id = p.fund_id
                         AND m2.status = 'ACTIVE'
                         AND mv.version = m2.current_version
                         AND mv.effective_from <= %s
                         AND (mv.effective_to IS NULL OR mv.effective_to > %s)
                       LIMIT 1
                   ) AS universe_policy,
                   (
                       SELECT mv.risk_bounds
                       FROM governance.mandates m3
                       JOIN governance.mandate_versions mv
                         ON mv.mandate_id = m3.mandate_id
                       WHERE m3.fund_id = p.fund_id
                         AND m3.status = 'ACTIVE'
                         AND mv.version = m3.current_version
                         AND mv.effective_from <= %s
                         AND (mv.effective_to IS NULL OR mv.effective_to > %s)
                       LIMIT 1
                   ) AS risk_bounds
            FROM risk.policies p
            JOIN governance.approvals a ON a.approval_id = p.approval_id
            WHERE p.fund_id = %s AND p.status = 'ACTIVE'
              AND a.decision = 'APPROVED'
              AND (a.expires_at IS NULL OR a.expires_at > %s)
              AND p.effective_from <= %s
              AND (p.effective_to IS NULL OR p.effective_to > %s)
              AND EXISTS (
                  SELECT 1
                  FROM governance.mandates m
                  JOIN governance.mandate_versions mv ON mv.mandate_id = m.mandate_id
                  WHERE m.fund_id = p.fund_id
                    AND m.status = 'ACTIVE'
                    AND mv.version = m.current_version
                    AND mv.effective_from <= %s
                    AND (mv.effective_to IS NULL OR mv.effective_to > %s)
              )
            ORDER BY p.effective_from DESC, p.version DESC
            LIMIT 1
            """,
            (fund_id, as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of),
        )
        row = cursor.fetchone()
        if row is None:
            raise RiskContextLoadError("no active PIT Risk policy for Fund")
        scope = _mapping(row[0], "risk.policies.scope")
        rules = _mapping(row[1], "risk.policies.rules")
        allowed = _first(rules, "allowed_instrument_ids", "allowed_instruments")
        if allowed is None:
                allowed = _first(scope, "allowed_instrument_ids", "allowed_instruments")
        universe = _mapping(
            row[2] if len(row) > 2 and row[2] else {},
            "governance.mandate_versions.universe_policy",
        )
        risk_bounds = _mapping(
            row[3] if len(row) > 3 and row[3] else {},
            "governance.mandate_versions.risk_bounds",
        )
        allowed_asset_classes = _first(universe, "allowed_asset_classes")
        forbidden_asset_classes = _first(universe, "forbidden_asset_classes") or []
        preferred_sectors = _first(universe, "preferred_sectors") or []
        excluded_sectors = _first(universe, "excluded_sectors") or []
        return MandateScope(
            fund_id=fund_id,
            allowed_instrument_ids=_uuid_set(allowed, "allowed_instrument_ids"),
            min_order_notional=_required_decimal(
                rules,
                "min_order_notional",
                "min_order_notional",
                "minimum_order_notional",
            ),
            max_order_notional=_required_decimal(
                rules,
                "max_order_notional",
                "max_order_notional",
                "maximum_order_notional",
            ),
            max_instrument_weight=(
                _decimal(risk_bounds["max_instrument_weight"], "max_instrument_weight")
                if risk_bounds.get("max_instrument_weight") is not None
                else None
            ),
            max_sector_weight=(
                _decimal(risk_bounds["max_sector_weight"], "max_sector_weight")
                if risk_bounds.get("max_sector_weight") is not None
                else None
            ),
            max_gross_exposure=(
                _decimal(risk_bounds["max_gross_exposure"], "max_gross_exposure")
                if risk_bounds.get("max_gross_exposure") is not None
                else None
            ),
            allowed_asset_classes=(
                frozenset(str(value) for value in allowed_asset_classes)
                if allowed_asset_classes is not None
                else None
            ),
            forbidden_asset_classes=frozenset(str(value) for value in forbidden_asset_classes),
            preferred_sectors=frozenset(str(value) for value in preferred_sectors),
            excluded_sectors=frozenset(str(value) for value in excluded_sectors),
        )

    def _load_limits(self, cursor: Any, fund_id: UUID, as_of: datetime) -> LimitSet:
        cursor.execute(
            """
            SELECT l.metric, l.soft_limit, l.hard_limit
            FROM risk.limits l
            JOIN risk.policies p ON p.policy_id = l.policy_id
            WHERE l.fund_id = %s AND l.status = 'ACTIVE'
              AND p.status = 'ACTIVE'
              AND l.effective_from <= %s
              AND (l.effective_to IS NULL OR l.effective_to > %s)
            ORDER BY l.effective_from DESC
            """,
            (fund_id, as_of, as_of),
        )
        limits: dict[str, tuple[Any, Any]] = {}
        for metric, soft_limit, hard_limit in cursor.fetchall():
            limits[str(metric).lower()] = (soft_limit, hard_limit)

        def value(*names: str, soft: bool = False) -> Decimal:
            for name in names:
                if name in limits:
                    raw = limits[name][0 if soft else 1]
                    if raw is None:
                        break
                    return _decimal(raw, name)
            raise RiskContextLoadError(f"canonical Risk limit missing: {names[0]}")

        return LimitSet(
            soft_single_issuer_pct=value(
                "single_issuer_pct", "single_issuer_soft_pct", soft=True
            ),
            hard_single_issuer_pct=value("single_issuer_pct", "single_issuer_hard_pct"),
            max_daily_turnover_notional=value(
                "daily_turnover_notional", "max_daily_turnover_notional"
            ),
            max_daily_order_count=int(
                value("daily_order_count", "max_daily_order_count")
            ),
            max_daily_loss=value("daily_loss", "max_daily_loss"),
            max_drawdown_pct=value("drawdown_pct", "max_drawdown_pct"),
        )

    def _load_restricted(
        self,
        cursor: Any,
        fund_id: UUID,
        instrument_id: UUID,
        as_of: datetime,
    ) -> tuple[RestrictedItem, ...]:
        cursor.execute(
            """
            SELECT ri.instrument_id, ri.restriction_type, ri.effective_from, ri.effective_to
            FROM risk.restricted_items ri
            WHERE ri.fund_id = %s AND ri.status = 'ACTIVE'
              AND (ri.instrument_id = %s OR ri.instrument_id IS NULL)
              AND ri.effective_from <= %s
              AND (ri.effective_to IS NULL OR ri.effective_to > %s)
            """,
            (fund_id, instrument_id, as_of, as_of),
        )
        result: list[RestrictedItem] = []
        for (
            restricted_instrument_id,
            restriction_type,
            effective_from,
            effective_to,
        ) in cursor.fetchall():
            result.append(
                RestrictedItem(
                    instrument_id=restricted_instrument_id or instrument_id,
                    restriction_type=_restriction(restriction_type),
                    effective_from=effective_from,
                    effective_to=effective_to,
                )
            )
        return tuple(result)

    def _load_portfolio(
        self, cursor: Any, fund_id: UUID, book_id: UUID, as_of: datetime
    ) -> PortfolioState:
        cursor.execute(
            """
            SELECT gross_exposure, nav
            FROM accounting.portfolio_snapshots
            WHERE fund_id = %s AND book_id = %s AND as_of <= %s
              AND quality_status IN ('PASS', 'WARN')
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (fund_id, book_id, as_of),
        )
        snapshot = cursor.fetchone()
        if snapshot is None:
            raise RiskContextLoadError("no usable PIT portfolio snapshot for Fund/Book")
        gross_exposure = _decimal(
            _row_value(snapshot, 0, "gross_exposure"), "gross_exposure"
        )
        nav = _decimal(_row_value(snapshot, 1, "nav"), "nav")

        cursor.execute(
            """
            SELECT instrument_id, quantity, realized_pnl, COALESCE(i.issuer_id::text, instrument_id::text)
            FROM accounting.positions p
            LEFT JOIN reference.instruments i ON i.instrument_id = p.instrument_id
            WHERE p.fund_id = %s AND p.book_id = %s AND p.as_of <= %s
            ORDER BY p.as_of DESC
            """,
            (fund_id, book_id, as_of),
        )
        positions: dict[UUID, Decimal] = {}
        issuer_of: dict[UUID, str] = {}
        realized = Decimal(0)
        for position_instrument_id, quantity, realized_pnl, issuer in cursor.fetchall():
            positions[position_instrument_id] = _decimal(quantity, "position.quantity")
            issuer_of[position_instrument_id] = str(issuer)
            realized += _decimal(realized_pnl, "position.realized_pnl")

        cursor.execute(
            """
            SELECT COALESCE(SUM(settled_amount + unsettled_amount - reserved_amount), 0)
            FROM accounting.cash_balances
            WHERE fund_id = %s AND book_id = %s AND as_of <= %s
            """,
            (fund_id, book_id, as_of),
        )
        cash = _decimal(cursor.fetchone()[0], "cash_balances.available")
        cursor.execute(
            """
            SELECT COALESCE(SUM(unrealized_pnl), 0)
            FROM accounting.pnl_snapshots
            WHERE fund_id = %s AND book_id = %s AND as_of <= %s
            """,
            (fund_id, book_id, as_of),
        )
        unrealized = _decimal(cursor.fetchone()[0], "pnl_snapshots.unrealized_pnl")
        cursor.execute(
            """
            SELECT COALESCE(i.issuer_id::text, v.instrument_id::text),
                   COALESCE(SUM(v.base_market_value), 0)
            FROM accounting.valuations v
            LEFT JOIN reference.instruments i ON i.instrument_id = v.instrument_id
            WHERE v.fund_id = %s AND v.book_id = %s AND v.as_of <= %s
              AND v.quality_status IN ('PASS', 'WARN')
            GROUP BY COALESCE(i.issuer_id::text, v.instrument_id::text)
            """,
            (fund_id, book_id, as_of),
        )
        issuer_exposure = {
            str(issuer): _decimal(value, "valuation.base_market_value")
            for issuer, value in cursor.fetchall()
        }
        return PortfolioState(
            fund_id=fund_id,
            cash=cash,
            buying_power=cash,
            gross_exposure=gross_exposure,
            positions=positions,
            issuer_of=issuer_of,
            issuer_exposure=issuer_exposure,
            realized_pnl_today=realized,
            unrealized_pnl_today=unrealized,
            peak_equity=nav,
            equity=nav,
        )

    def _load_market(
        self, cursor: Any, instrument_id: UUID, as_of: datetime
    ) -> MarketStatus:
        cursor.execute(
            """
            SELECT quality_status, COALESCE(mid, last_price)
            FROM execution.market_snapshots
            WHERE instrument_id = %s AND as_of <= %s
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (instrument_id, as_of),
        )
        row = cursor.fetchone()
        if row is None:
            raise RiskContextLoadError("no PIT market snapshot for instrument")
        quality, price = str(row[0]), row[1]
        tradable = quality in {"PASS", "WARN"} and price is not None
        return MarketStatus(tradable=tradable, reason=f"market_quality={quality}")

    def _load_counterparty(
        self, cursor: Any, broker_adapter: str, as_of: datetime
    ) -> CounterpartyStatus:
        cursor.execute(
            """
            SELECT status, health
            FROM risk.counterparties
            WHERE counterparty_code = %s AND observed_at <= %s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (broker_adapter, as_of),
        )
        row = cursor.fetchone()
        if row is None:
            raise RiskContextLoadError("no PIT counterparty health for broker adapter")
        status, health_raw = str(row[0]), _mapping(row[1], "counterparty.health")
        health_value = str(_first(health_raw, "status", "health") or "DOWN").upper()
        if status in {"RESTRICTED", "DEFAULTED", "CLOSED"}:
            health_value = "DOWN"
        elif health_value not in {"OK", "DEGRADED", "DOWN"}:
            health_value = "DEGRADED"
        return CounterpartyStatus(
            broker_adapter=broker_adapter,
            health=CounterpartyHealth(health_value.lower()),
        )
