"""Stable cache identity for shared indicator computation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any


def indicator_cache_key(
    *,
    indicator: str,
    source: str,
    provider: str | None,
    timeframe: str,
    parameters: Mapping[str, Any],
    output: str,
    market_data_source: str | None,
    calculation_version: str,
    market: str,
    instrument: str,
    bar_timestamp: str,
) -> str:
    """Return a collision-resistant identity for one computed value.

    Raw broker TR codes are intentionally not accepted as a separate field;
    adapters own that mapping behind ``provider`` and ``indicator``.  Every
    field that can change the result is explicit, including source and output.
    """

    payload = {
        "schema": "indicator-cache-key.v2",
        "indicator": indicator.strip().upper(),
        "source": source.strip().upper(),
        "provider": (provider or "<none>").strip().upper(),
        "timeframe": timeframe.strip().upper(),
        "output": output.strip().upper(),
        "market_data_source": (market_data_source or "<none>").strip().upper(),
        "calculation_version": calculation_version,
        "market": market.strip().upper(),
        "instrument": instrument.strip().upper(),
        "parameters": {
            str(key).strip().upper(): str(value)
            for key, value in sorted(parameters.items(), key=lambda item: str(item[0]))
        },
        "bar_timestamp": bar_timestamp,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256(encoded).hexdigest()
