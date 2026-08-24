#!/usr/bin/env python3
"""F19 improvement-worker: workforce.eval.v1을 소비해 개선 후보를 Shadow/Reject로 전이하는
상주 Worker.

담당: 영주 (Agent Workforce 인사팀)
근거: DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md 6.8절(improvement-worker),
      departments/00-ceo-office/governance_events/worker.py 패턴.

이 Worker는 아무도 호출하지 않아도 스스로 계속 돈다 - workforce-api(동기 HTTP)와
반대로 Redis Stream을 지켜보다가 QA가 workforce.eval.v1을 발행하면 자동으로 처리한다.

실행: python workforce_events/worker.py (workforce-api와 같은 Image, command만 다르다)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1] / "api"
_WORKFORCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API_DIR))
sys.path.insert(0, str(_WORKFORCE_DIR))

from workforce_api_loader import load_workforce_api

_workforce_api = load_workforce_api()
_handle_workforce_event = _workforce_api._handle_workforce_event
_workforce_event_bus = _workforce_api._workforce_event_bus


def main() -> None:
    bus = _workforce_event_bus()
    if bus is None:
        raise SystemExit("WORKFORCE_EVENT_REDIS_URL 또는 REDIS_URL이 필요합니다")
    interval = float(os.environ.get("WORKFORCE_EVENT_POLL_INTERVAL_SECONDS", "1"))
    while True:
        bus.consume_once(_handle_workforce_event, count=50, min_idle_ms=1000)
        time.sleep(interval)


if __name__ == "__main__":
    main()
