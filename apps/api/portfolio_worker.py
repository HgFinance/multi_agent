"""Durable worker entrypoint for queued advisory portfolio runs.

The API only enqueues and projects runs. This process owns queue claims and can
be restarted without losing queued input; SQLite leases let another worker
reclaim an expired claim without creating a second active execution.
"""

from __future__ import annotations

import os
import time

from portfolio_runtime import PortfolioRuntime


def main() -> None:
    runtime = PortfolioRuntime()
    worker_id = os.getenv("PORTFOLIO_WORKER_ID", "").strip() or f"portfolio-worker-{os.getpid()}"
    poll_seconds = max(float(os.getenv("PORTFOLIO_WORKER_POLL_SECONDS", "0.5")), 0.05)
    lease_seconds = max(float(os.getenv("PORTFOLIO_WORKER_LEASE_SECONDS", "60")), 5.0)
    while True:
        if not runtime.run_once(worker_id=worker_id, lease_seconds=lease_seconds):
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
