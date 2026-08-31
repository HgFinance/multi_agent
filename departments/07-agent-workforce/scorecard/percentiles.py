"""Small shared percentile helper for Workforce scorecard metrics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar


Number = TypeVar("Number", int, float)


def percentile(values: Sequence[Number], fraction: float) -> Number | None:
    """Return the existing lower-middle-compatible nearest percentile value."""

    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]
