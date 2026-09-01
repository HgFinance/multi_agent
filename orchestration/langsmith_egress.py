"""Single opt-out boundary for every outbound LangSmith operation."""

from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def langsmith_egress_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether this process may contact LangSmith at all.

    The switch defaults to enabled to preserve existing deployments. Setting
    ``HGFINANCE_LANGSMITH_EGRESS_ENABLED=false`` stops publishers, readers,
    retention, and reconciliation before an SDK client or HTTP request exists.
    """

    source = environment if environment is not None else os.environ
    raw = source.get("HGFINANCE_LANGSMITH_EGRESS_ENABLED")
    return raw is None or str(raw).strip().casefold() in _TRUE_VALUES
