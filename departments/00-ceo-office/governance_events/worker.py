#!/usr/bin/env python3
"""F24 Notification: Domain Event Stream을 소비해 알림으로 변환하는 상주 Worker.

담당: 영주 (CEO Office)
근거: DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md 6.1절(notification-worker),
      departments/06-ai-qa-audit/qa_events/worker.py 패턴.

이 Worker는 아무도 호출하지 않아도 스스로 계속 돈다 - governance-api(동기 HTTP)와
반대로 Redis Stream을 지켜보다가 다른 본부가 이벤트를 발행하면 자동으로 처리한다.

실행: python governance_events/worker.py (governance-api와 같은 Image, command만 다르다)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(_API_DIR))

from app import _governance_event_bus, _handle_governance_event  # noqa: E402


def main() -> None:
    bus = _governance_event_bus()
    if bus is None:
        raise SystemExit("GOVERNANCE_EVENT_REDIS_URL 또는 REDIS_URL이 필요합니다")
    interval = float(os.environ.get("GOVERNANCE_EVENT_POLL_INTERVAL_SECONDS", "1"))
    while True:
        bus.consume_once(_handle_governance_event, count=50, min_idle_ms=1000)
        time.sleep(interval)


if __name__ == "__main__":
    main()
