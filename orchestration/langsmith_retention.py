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
TRACE_DELETE_BATCH_SIZE = 100
PROJECT_ENV_NAMES = (
    ("workflow", "LANGSMITH_PROJECT", "First", 30),
    ("metrics", "LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics", 7),
    ("evals", "LANGSMITH_EVALS_PROJECT", "HgFinance-Evals", 30),
)
RETENTION_SCOPE_NAMES = frozenset(scope for scope, *_ in PROJECT_ENV_NAMES)


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


def _retention_scopes() -> frozenset[str] | None:
    raw = os.getenv("LANGSMITH_RETENTION_SCOPES")
    if raw is None:
        return None
    values = {
        item.strip().casefold()
        for item in raw.split(",")
        if item.strip().casefold() in RETENTION_SCOPE_NAMES
    }
    # An empty/invalid explicit value fails safe to First rather than silently
    # widening the deletion scope.
    return frozenset(values or {"workflow"})


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


class LangSmithRetentionRateLimited(RuntimeError):
    """LangSmith accepted the request shape but blocked deletion by policy."""


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
            "LANGSMITH_RETENTION_MAX_TRACES", 1000, minimum=1, maximum=1000
        )
        self.scan_window_days = scan_window_days or _env_int(
            "LANGSMITH_RETENTION_SCAN_WINDOW_DAYS", 400, minimum=1, maximum=400
        )
        self.retention_scopes = _retention_scopes()
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> "LangSmithRetentionWorker":
        return cls()

    def _projects(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (scope, _project_name(env_name, default_name), days)
            for scope, env_name, default_name, days in PROJECT_ENV_NAMES
            if self.retention_scopes is None or scope in self.retention_scopes
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

    @staticmethod
    def _started_at(run: Any) -> datetime:
        value = getattr(run, "start_time", None)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    def _delete_trace_ids(self, project_id: str, trace_ids: list[str]) -> int:
        if not trace_ids:
            return 0
        deleted = 0
        for offset in range(0, len(trace_ids), TRACE_DELETE_BATCH_SIZE):
            batch = trace_ids[offset : offset + TRACE_DELETE_BATCH_SIZE]
            request = urllib.request.Request(
                _delete_url(self.endpoint),
                data=json.dumps(
                    {"trace_ids": batch, "session_id": project_id},
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
                    deleted += len(batch)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < 3:
                        retry_after = exc.headers.get("Retry-After", "1")
                        response_body = exc.read(1000).decode("utf-8", "replace").casefold()
                        if "hourly trace deletion limit exceeded" in response_body:
                            raise LangSmithRetentionRateLimited(
                                "TRACE_DELETE_HOURLY_LIMIT"
                            ) from exc
                        try:
                            time.sleep(min(10.0, max(0.5, float(retry_after))))
                        except (TypeError, ValueError):
                            time.sleep(1.0)
                        continue
                    raise
            else:
                raise RuntimeError("langsmith_delete_retry_exhausted")
        return deleted

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
                retention_days = self._retention_days(scope, default_days)
                scan_start = current - timedelta(days=max(self.scan_window_days, retention_days))
                project_id = resolve_project_id(client, project_name)
                # Read one retention-cap window plus one bounded deletion
                # batch.  Asking for cap+1 only detects that an overflow
                # exists; it leaves the rest of the oldest tail behind and
                # makes a 1,000-trace cap converge one item per pass.
                query_limit = min(self.max_traces * 2, 2000)
                runs = query_runs(
                    client,
                    project_name=project_name,
                    min_start_time=scan_start,
                    max_start_time=current,
                    is_root=True,
                    page_size=100,
                    max_results=query_limit,
                    selects=["ID", "START_TIME"],
                )
                scanned += len(runs)
                # The API returns newest roots first. Sort locally as a second
                # guard so the retention decision never depends on response
                # ordering. Only the oldest rows beyond the per-project cap
                # are candidates; the age setting controls the bounded scan
                # window, not an unbounded delete.
                ordered = sorted(runs, key=self._started_at, reverse=True)
                excess = ordered[self.max_traces : self.max_traces * 2]
                trace_ids = [self._run_id(run) for run in excess if self._run_id(run)]
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
            error_code = (
                "TRACE_DELETE_HOURLY_LIMIT"
                if isinstance(exc, LangSmithRetentionRateLimited)
                else type(exc).__name__
            )
            return LangSmithRetentionSummary(
                True,
                False,
                effective_dry_run,
                scanned=scanned,
                eligible=eligible,
                deleted=deleted,
                skipped=skipped,
                error_code=error_code,
            )


__all__ = [
    "LangSmithRetentionRateLimited",
    "LangSmithRetentionSummary",
    "LangSmithRetentionWorker",
]
