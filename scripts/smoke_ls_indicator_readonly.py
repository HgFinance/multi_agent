#!/usr/bin/env python3
"""Guarded read-only LS indicator smoke check.

The script is intentionally inert unless explicitly enabled for PAPER market
data.  It never imports the order broker and never submits an order.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.conditional_rules import ExpressionNode
from orchestration.conditional_rules.indicators import DEFAULT_REGISTRY, IndicatorProviderError


def main() -> int:
    if os.environ.get("LS_INDICATOR_READONLY_SMOKE", "") != "1":
        print("SKIPPED: set LS_INDICATOR_READONLY_SMOKE=1 to enable read-only smoke")
        return 0
    if os.environ.get("LS_ENV", "LIVE").strip().upper() != "PAPER":
        print("SKIPPED: read-only smoke requires LS_ENV=PAPER")
        return 0

    symbol = os.environ.get("LS_SMOKE_SYMBOL", "005930").strip()
    spec = ExpressionNode.model_validate(
        {
            "type": "INDICATOR",
            "name": "MARKET_WARNING_STATUS",
            "output": "VALUE",
            "timeframe": "1D",
            "parameters": {},
            "source": "BROKER",
            "provider": "LS",
        }
    )
    try:
        value = asyncio.run(
            DEFAULT_REGISTRY.resolve(
                symbol,
                spec,
                {
                    "clock": "BAR_CLOSE",
                    "observed_at": datetime.now(timezone.utc),
                    "market_data_source_id": "LS_PAPER_MARKET_DATA",
                },
            )
        )
    except IndicatorProviderError as exc:
        print(f"READ_ONLY_SMOKE_FAIL_CLOSED: code={exc.code}")
        return 2
    print(
        "READ_ONLY_SMOKE_OK: "
        f"indicator={value.indicator} value={value.value} "
        f"data_timestamp={value.data_timestamp.isoformat() if value.data_timestamp else None}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
