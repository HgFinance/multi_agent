"""Read-only canonical Instrument mapping repository for the Risk adapter."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .analytics import InstrumentMapping, RiskP1Error


class InstrumentMappingRepositoryError(RiskP1Error):
    """Raised when canonical instrument identity cannot be resolved."""


class PostgresInstrumentMappingRepository:
    """Resolve broker symbols through ``reference.instrument_symbols``.

    The repository is read-only.  It never creates an Instrument row as a
    fallback because doing so would bypass governance and make a Risk decision
    non-reproducible.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def resolve(
        self,
        symbols: Sequence[str],
        *,
        provider: str | None = None,
        as_of: datetime | None = None,
    ) -> tuple[InstrumentMapping, ...]:
        normalized = tuple(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))
        if not normalized:
            raise InstrumentMappingRepositoryError("at least one broker symbol is required")

        cursor = self._connection.cursor()
        try:
            query = """
                select isym.symbol, isym.instrument_id, inst.instrument_type
                from reference.instrument_symbols as isym
                join reference.instruments as inst
                  on inst.instrument_id = isym.instrument_id
            where upper(isym.symbol) = any(%s)
              and inst.status in ('PENDING', 'ACTIVE')
              and isym.valid_from <= %s
              and (isym.valid_to is null or isym.valid_to > %s)
        """
            effective_as_of = as_of or datetime.now(timezone.utc)
            params: list[Any] = [list(normalized), effective_as_of, effective_as_of]
            if provider:
                query += " and isym.provider = %s"
                params.append(provider)
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except Exception as exc:
            raise InstrumentMappingRepositoryError("canonical instrument mapping lookup failed") from exc
        finally:
            cursor.close()

        mappings = tuple(
            InstrumentMapping(
                broker_symbol=str(row[0]),
                instrument_id=row[1],
                instrument_type=str(row[2]),
            )
            for row in rows
        )
        found = {item.broker_symbol.upper() for item in mappings}
        missing = [symbol for symbol in normalized if symbol not in found]
        if missing:
            raise InstrumentMappingRepositoryError(
                f"canonical instrument mapping missing: {', '.join(missing)}"
            )
        return mappings
