"""Durable worker entrypoint for queued advisory portfolio runs.

The API only enqueues and projects runs. This process owns queue claims and can
be restarted without losing queued input; SQLite leases let another worker
reclaim an expired claim without creating a second active execution.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from orchestration.service_health import probe_postgres, probe_sqlite


def _runtime_store_path() -> str:
    configured = os.getenv("PORTFOLIO_RUNTIME_STORE_PATH", "").strip()
    if configured:
        return configured
    store_dir = os.getenv("PORTFOLIO_RUNTIME_STORE_DIR", "/tmp").strip() or "/tmp"
    return str(Path(store_dir) / "hgfinance-portfolio.sqlite3")


def healthcheck() -> None:
    """Probe the durable queue store and control database without claiming work."""

    probe_postgres(dsn_env="DATABASE_URL")
    probe_sqlite(
        _runtime_store_path(),
        required_tables=(
            "portfolio_runtime_snapshots",
            "portfolio_runtime_active",
            "portfolio_runtime_queue",
        ),
    )


def main() -> None:
    from portfolio_runtime import PortfolioRuntime

    from orchestration.langsmith_feedback import LangSmithFeedbackService

    runtime = PortfolioRuntime()
    feedback = LangSmithFeedbackService()
    feedback.start()
    worker_id = os.getenv("PORTFOLIO_WORKER_ID", "").strip() or f"portfolio-worker-{os.getpid()}"
    poll_seconds = max(float(os.getenv("PORTFOLIO_WORKER_POLL_SECONDS", "0.5")), 0.05)
    lease_seconds = max(float(os.getenv("PORTFOLIO_WORKER_LEASE_SECONDS", "60")), 5.0)
    try:
        while True:
            if not runtime.run_once(worker_id=worker_id, lease_seconds=lease_seconds):
                time.sleep(poll_seconds)
    finally:
        feedback.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        healthcheck()
    else:
        main()
