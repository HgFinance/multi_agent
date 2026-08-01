#!/usr/bin/env python3
"""Risk Decision Stream을 QA Audit 수신 이력으로 적재하는 Worker."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_QA_DIR))

from api.app import _qa_event_bus, _record_risk_event  # noqa: E402


def main() -> None:
    bus = _qa_event_bus()
    if bus is None:
        raise SystemExit("RISK_QA_EVENT_REDIS_URL 또는 REDIS_URL이 필요합니다")
    interval = float(os.environ.get("QA_EVENT_POLL_INTERVAL_SECONDS", "1"))
    while True:
        bus.consume_once(_record_risk_event, count=50, min_idle_ms=1000)
        time.sleep(interval)


if __name__ == "__main__":
    main()
