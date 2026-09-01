"""Run the existing Kanban, MemoHarness, Notion, Discord, and LangSmith retention jobs.

The domain workers remain the only implementations of their cleanup rules.
This module only supplies process lifecycle, independent schedules, and a
small health record so one failed maintenance job cannot stop the other jobs.
The process remains alive after a domain failure, while its healthcheck reports
the scheduler as degraded until that job completes successfully again.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)
DEFAULT_HEALTH_PATH = "/tmp/hgfinance-maintenance-retention-health.json"


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


@dataclass(frozen=True)
class MaintenanceJob:
    name: str
    interval_seconds: float
    run_once: Callable[[], Any]
    max_run_seconds: float


class MaintenanceResultError(RuntimeError):
    """A domain worker completed with a structured, non-exception error."""

    def __init__(self, error_code: str) -> None:
        self.error_code = str(error_code)
        super().__init__(self.error_code)


# An operator-disabled integration is an intentional skip, not a failed
# maintenance domain. Keeping it out of the failed state lets the scheduler
# remain healthy while the global LangSmith egress circuit breaker is active.
_EXPECTED_SKIP_RESULT_CODES = frozenset({"DISABLED", "LANGSMITH_EGRESS_DISABLED"})


class HealthLedger:
    """Thread-safe, atomic scheduler health projection."""

    def __init__(
        self, path: Path, *, wall_clock: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self.wall_clock = wall_clock
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"heartbeat": self.wall_clock(), "jobs": {}}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(self._state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def heartbeat(self) -> None:
        with self._lock:
            self._state["heartbeat"] = self.wall_clock()
            self._write()

    def started(self, job: MaintenanceJob) -> None:
        with self._lock:
            now = self.wall_clock()
            self._state["heartbeat"] = now
            self._state["jobs"][job.name] = {
                "status": "running",
                "started_at": now,
                "max_run_seconds": job.max_run_seconds,
            }
            self._write()

    def finished(
        self,
        job: MaintenanceJob,
        *,
        error: BaseException | None = None,
        result: Any | None = None,
    ) -> None:
        with self._lock:
            now = self.wall_clock()
            previous = dict(self._state["jobs"].get(job.name) or {})
            previous.update(
                {
                    "status": "failed" if error is not None else "ok",
                    "finished_at": now,
                    "error": type(error).__name__ if error is not None else None,
                    "error_code": (
                        getattr(error, "error_code", None) if error is not None else None
                    ),
                    "max_run_seconds": job.max_run_seconds,
                }
            )
            result_snapshot = {
                field: getattr(result, field)
                for field in (
                    "scanned",
                    "eligible",
                    "deleted",
                    "queued",
                    "queued_runs",
                    "pending_visible",
                    "visible_overflow",
                    "skipped",
                )
                if result is not None and getattr(result, field, None) is not None
            }
            if result_snapshot:
                previous["result"] = result_snapshot
                if result_snapshot.get("pending_visible", 0) or result_snapshot.get(
                    "visible_overflow", 0
                ):
                    previous["warning"] = "LANGSMITH_DELETE_PENDING"
                else:
                    previous.pop("warning", None)
            self._state["heartbeat"] = now
            self._state["jobs"][job.name] = previous
            self._write()


def _job_loop(
    job: MaintenanceJob,
    stop: threading.Event,
    health: HealthLedger,
    *,
    once: bool = False,
) -> None:
    while not stop.is_set():
        started = time.monotonic()
        health.started(job)
        error: BaseException | None = None
        result: Any | None = None
        try:
            result = job.run_once()
            result_error = getattr(result, "error_code", None)
            if result_error and result_error not in _EXPECTED_SKIP_RESULT_CODES:
                error = MaintenanceResultError(str(result_error))
                LOG.warning(
                    "maintenance-retention-job-reported-error job=%s error_code=%s",
                    job.name,
                    result_error,
                )
            elif result_error:
                LOG.info(
                    "maintenance-retention-job-skipped job=%s reason=%s",
                    job.name,
                    result_error,
                )
        except Exception as exc:  # each maintenance domain remains fail-open
            error = exc
            LOG.exception("maintenance-retention-job-failed job=%s", job.name)
        finally:
            health.finished(job, error=error, result=result)
        if once:
            return
        elapsed = time.monotonic() - started
        if stop.wait(max(0.1, job.interval_seconds - elapsed)):
            return


def build_jobs(*, dry_run: bool = False) -> tuple[MaintenanceJob, ...]:
    # Keep the healthcheck path cheap.  These worker modules initialize HTTP,
    # database, and retention dependencies and are needed only by the scheduler
    # process itself.  Importing them for every Docker health probe used to take
    # longer than the five-second health timeout and marked a healthy scheduler
    # unhealthy while a long Kanban purge was running.
    from orchestration.adapters.notion_retention import NotionRetentionWorker
    from orchestration.discord_retention import DiscordRetentionWorker
    from orchestration.experience_retention import ExperienceRetentionWorker
    from orchestration.kanban_retention import _build_worker as build_kanban_worker
    from orchestration.langsmith_retention import LangSmithRetentionWorker

    max_archive_roots = int(os.getenv("KANBAN_RETENTION_MAX_ARCHIVE_ROOTS", "5"))

    def run_kanban() -> Any:
        return build_kanban_worker(
            dry_run=dry_run,
            max_archive_roots=max_archive_roots,
        ).run_once()

    def run_experience() -> Any:
        return ExperienceRetentionWorker.from_env().run_once(dry_run=dry_run)

    def run_notion() -> Any:
        return NotionRetentionWorker.from_env().run_once(dry_run=dry_run)

    def run_discord() -> Any:
        return DiscordRetentionWorker.from_env().run_once(dry_run=dry_run)

    def run_langsmith() -> Any:
        # The worker's own dry-run default remains authoritative. The CLI
        # switch can only make a pass safer, never turn a configured dry-run
        # into an external deletion.
        return LangSmithRetentionWorker.from_env().run_once(dry_run=True if dry_run else None)

    return (
        MaintenanceJob(
            "kanban",
            _env_float("KANBAN_RETENTION_INTERVAL_SECONDS", 900),
            run_kanban,
            _env_float("KANBAN_RETENTION_MAX_RUN_SECONDS", 1800),
        ),
        MaintenanceJob(
            "memo-harness",
            _env_float("MEMOHARNESS_D5_RETENTION_INTERVAL_SECONDS", 86400),
            run_experience,
            _env_float("MEMOHARNESS_D5_RETENTION_MAX_RUN_SECONDS", 300),
        ),
        MaintenanceJob(
            "notion",
            _env_float("NOTION_RETENTION_INTERVAL_SECONDS", 86400),
            run_notion,
            _env_float("NOTION_RETENTION_MAX_RUN_SECONDS", 7200),
        ),
        MaintenanceJob(
            "discord",
            _env_float("DISCORD_RETENTION_INTERVAL_SECONDS", 86400),
            run_discord,
            _env_float("DISCORD_RETENTION_MAX_RUN_SECONDS", 900),
        ),
        MaintenanceJob(
            "langsmith",
            _env_float("LANGSMITH_RETENTION_INTERVAL_SECONDS", 86400),
            run_langsmith,
            _env_float("LANGSMITH_RETENTION_MAX_RUN_SECONDS", 900),
        ),
    )


def healthcheck(
    path: Path,
    *,
    now: float | None = None,
    heartbeat_max_age_seconds: float = 15.0,
) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        if current - float(state["heartbeat"]) > heartbeat_max_age_seconds:
            return False
        for job in dict(state.get("jobs") or {}).values():
            if job.get("status") == "failed":
                return False
            if job.get("status") != "running":
                continue
            if current - float(job["started_at"]) > float(job["max_run_seconds"]):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def run_scheduler(
    jobs: Sequence[MaintenanceJob],
    *,
    health_path: Path,
    once: bool = False,
) -> int:
    stop = threading.Event()
    health = HealthLedger(health_path)

    def shutdown(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    threads = [
        threading.Thread(
            target=_job_loop,
            name=f"retention-{job.name}",
            args=(job, stop, health),
            kwargs={"once": once},
        )
        for job in jobs
    ]
    for thread in threads:
        thread.start()
    if once:
        for thread in threads:
            thread.join()
        return 0
    while not stop.wait(1.0):
        health.heartbeat()
    for thread in threads:
        thread.join(timeout=30)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--health-path",
        default=os.getenv("MAINTENANCE_RETENTION_HEALTH_PATH", DEFAULT_HEALTH_PATH),
    )
    args = parser.parse_args(argv)
    path = Path(args.health_path)
    if args.healthcheck:
        return 0 if healthcheck(path) else 1
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run_scheduler(
        build_jobs(dry_run=args.dry_run), health_path=path, once=args.once
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
