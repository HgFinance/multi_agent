"""Bounded, opt-in LangSmith trace retention for named HgFinance projects.

The worker queries only root traces in the three explicit application
projects.  ``default`` and every unknown project are intentionally outside the
policy.  Querying uses the SmithDB v2 adapter; deletion uses LangSmith's
documented trace-delete endpoint because the SDK's high-level delete helpers
are not a stable read-path contract.

The default is enabled in the scheduler but dry-run.  An operator must set
``LANGSMITH_RETENTION_DRY_RUN=false`` after reviewing the candidate counts to
perform recoverable-at-API-level trace deletion.  Each pass is capped.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from orchestration.langsmith_queries import query_runs, resolve_project_id

LOG = logging.getLogger(__name__)
PROJECT_ENV_NAMES = (
    ("workflow", "LANGSMITH_PROJECT", "First", 30),
    ("metrics", "LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics", 7),
    ("evals", "LANGSMITH_EVALS_PROJECT", "HgFinance-Evals", 30),
)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _project_name(env_name: str, default: str) -> str:
    return os.getenv(env_name, default).strip() or default


def _delete_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/api/v1"):
        return f"{base}/runs/delete"
    if base.endswith("/api"):
        return f"{base}/v1/runs/delete"
    return f"{base}/api/v1/runs/delete"


@dataclass(frozen=True)
class LangSmithRetentionSummary:
    enabled: bool
    available: bool
    dry_run: bool
    scanned: int = 0
    eligible: int = 0
    deleted: int = 0
    skipped: int = 0
    error_code: str | None = None


class LangSmithRetentionWorker:
    """Run one bounded pass over the explicit HgFinance trace projects."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        enabled: bool | None = None,
        dry_run: bool | None = None,
        max_traces: int | None = None,
        scan_window_days: int | None = None,
        opener: Any | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("LANGSMITH_API_KEY", "")).strip()
        self.endpoint = (
            endpoint or os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
        ).strip()
        self.enabled = (
            _env_bool("LANGSMITH_RETENTION_ENABLED", True)
            if enabled is None
            else bool(enabled)
        )
        self.dry_run = (
            _env_bool("LANGSMITH_RETENTION_DRY_RUN", True)
            if dry_run is None
            else bool(dry_run)
        )
        self.max_traces = max_traces or _env_int(
            "LANGSMITH_RETENTION_MAX_TRACES", 100, minimum=1, maximum=1000
        )
        self.scan_window_days = scan_window_days or _env_int(
            "LANGSMITH_RETENTION_SCAN_WINDOW_DAYS", 90, minimum=1, maximum=3650
        )
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> "LangSmithRetentionWorker":
        return cls()

    def _projects(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (scope, _project_name(env_name, default_name), days)
            for scope, env_name, default_name, days in PROJECT_ENV_NAMES
        )

    def _retention_days(self, scope: str, default: int) -> int:
        return _env_int(
            f"LANGSMITH_RETENTION_{scope.upper()}_DAYS",
            default,
            minimum=1,
            maximum=3650,
        )

    @staticmethod
    def _run_id(run: Any) -> str:
        return str(getattr(run, "trace_id", None) or getattr(run, "id", "") or "").strip()

    def _delete_trace_ids(self, project_id: str, trace_ids: list[str]) -> int:
        if not trace_ids:
            return 0
        request = urllib.request.Request(
            _delete_url(self.endpoint),
            data=json.dumps(
                {"trace_ids": trace_ids, "session_id": project_id},
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "X-API-KEY": self.api_key,
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(4):
            try:
                with self.opener(request, timeout=20) as response:
                    response.read()
                return len(trace_ids)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    retry_after = exc.headers.get("Retry-After", "1")
                    try:
                        time.sleep(min(10.0, max(0.5, float(retry_after))))
                    except (TypeError, ValueError):
                        time.sleep(1.0)
                    continue
                raise
        raise RuntimeError("langsmith_delete_retry_exhausted")

    def run_once(
        self,
        *,
        dry_run: bool | None = None,
        now: datetime | None = None,
    ) -> LangSmithRetentionSummary:
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)
        if not self.enabled:
            return LangSmithRetentionSummary(True, False, effective_dry_run, error_code="DISABLED")
        if not self.api_key:
            return LangSmithRetentionSummary(True, False, effective_dry_run, error_code="LANGSMITH_API_KEY_MISSING")

        current = now or datetime.now(timezone.utc)
        scanned = eligible = deleted = skipped = 0
        try:
            from langsmith import Client

            client = Client(hide_inputs=True, hide_outputs=True, hide_metadata=True)
            for scope, project_name, default_days in self._projects():
                if eligible >= self.max_traces:
                    skipped += 1
                    break
                retention_days = self._retention_days(scope, default_days)
                cutoff = current - timedelta(days=retention_days)
                scan_start = current - timedelta(days=max(self.scan_window_days, retention_days))
                project_id = resolve_project_id(client, project_name)
                runs = query_runs(
                    client,
                    project_name=project_name,
                    min_start_time=scan_start,
                    max_start_time=cutoff,
                    is_root=True,
                    page_size=min(100, self.max_traces - eligible),
                    max_results=min(self.max_traces - eligible, 100),
                    selects=["ID", "START_TIME"],
                )
                scanned += len(runs)
                trace_ids = [self._run_id(run) for run in runs if self._run_id(run)]
                trace_ids = trace_ids[: max(0, self.max_traces - eligible)]
                eligible += len(trace_ids)
                if effective_dry_run:
                    continue
                deleted += self._delete_trace_ids(
                    project_id=project_id,
                    trace_ids=trace_ids,
                )
            LOG.info(
                "langsmith-retention enabled=true dry_run=%s scanned=%d eligible=%d deleted=%d skipped=%d",
                str(effective_dry_run).lower(),
                scanned,
                eligible,
                deleted,
                skipped,
            )
            return LangSmithRetentionSummary(
                True,
                True,
                effective_dry_run,
                scanned=scanned,
                eligible=eligible,
                deleted=deleted,
                skipped=skipped,
            )
        except Exception as exc:  # maintenance must not stop other retention jobs
            LOG.warning("langsmith-retention failed error=%s", type(exc).__name__)
            return LangSmithRetentionSummary(
                True,
                False,
                effective_dry_run,
                scanned=scanned,
                eligible=eligible,
                deleted=deleted,
                skipped=skipped,
                error_code=type(exc).__name__,
            )


__all__ = ["LangSmithRetentionSummary", "LangSmithRetentionWorker"]
