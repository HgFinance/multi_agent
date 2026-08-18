"""Shared contracts for exchange-derived daily market observations.

This module intentionally contains no network or persistence code.  Market
collectors use it instead of importing the retired macro collector merely to
share row shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


FREQ_DAILY = "DAILY"


@dataclass(frozen=True)
class SeriesSpec:
    source_code: str
    external_series_code: str
    name: str
    frequency: str
    unit: str | None = None
    country_code: str | None = None
    item_name: str | None = None
    seasonal_adjustment: str | None = None


@dataclass(frozen=True)
class Observation:
    external_series_code: str
    period: date
    value: Decimal | None
    published_at: datetime
    observed_at: datetime
    vintage_date: date
    metadata: dict = field(default_factory=dict)
