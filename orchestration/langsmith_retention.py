"""Bounded, opt-in LangSmith run retention for named HgFinance projects.

The worker queries complete trace trees in the three explicit application
projects and deletes only whole trees. ``default`` and every unknown project
are intentionally outside the policy. Querying uses the SmithDB v2 adapter;
deletion uses LangSmith's documented trace-delete endpoint because the SDK's
high-level delete helpers are not a stable read-path contract.

The scheduler can perform recoverable-at-API-level trace deletion.  Each pass
uses a small deletion budget so the provider's hourly deletion limit cannot
turn a normal overflow cleanup into a burst of failed requests.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestration.langsmith_queries import (
    close_query_client,
    query_runs,
    resolve_project_id,
)

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
    queued_runs: int = 0
    pending_visible: int = 0
    visible_overflow: int = 0
    skipped: int = 0
    error_code: str | None = None

    @property
    def queued(self) -> int:
        """Compatibility name for requests accepted by LangSmith.

        The delete endpoint acknowledges a request before the provider has
        physically removed the trace. Keep ``deleted`` for existing callers,
        but expose the truthful operational term too.
        """

        return self.deleted


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
        max_runs: int | None = None,
        max_delete_per_pass: int | None = None,
        scan_window_days: int | None = None,
        pending_state_path: str | os.PathLike[str] | None = None,
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
        # ``max_traces`` remains a source-compatible constructor alias. The
        # enforced cap is run rows, not roots: one LangSmith trace can contain
        # a root plus many child runs and root-only retention leaks quota even
        # when the root count is exactly 1,000.
        configured_max_runs = max_runs if max_runs is not None else max_traces
        if configured_max_runs is None:
            configured_max_runs = _env_int(
                "LANGSMITH_RETENTION_MAX_RUNS",
                _env_int(
                    "LANGSMITH_RETENTION_MAX_TRACES",
                    1000,
                    minimum=1,
                    maximum=1000,
                ),
                minimum=1,
                maximum=1000,
            )
        self.max_runs = max(1, min(int(configured_max_runs), 1000))
        # Existing callers/tests read this name; keep it as an alias while
        # making the semantics explicit in the implementation below.
        self.max_traces = self.max_runs
        self.query_max_runs = _env_int(
            "LANGSMITH_RETENTION_QUERY_MAX_RUNS",
            20000,
            minimum=max(1000, self.max_runs * 2),
            maximum=100000,
        )
        self.max_delete_per_pass = (
            _env_int(
                "LANGSMITH_RETENTION_DELETE_PER_PASS",
                100,
                minimum=1,
                maximum=1000,
            )
            if max_delete_per_pass is None
            else max(1, min(int(max_delete_per_pass), 1000))
        )
        self.scan_window_days = scan_window_days or _env_int(
            "LANGSMITH_RETENTION_SCAN_WINDOW_DAYS", 400, minimum=1, maximum=400
        )
        configured_pending_path = pending_state_path or os.getenv(
            "LANGSMITH_RETENTION_PENDING_PATH", ""
        ).strip()
        self.pending_state_path = (
            Path(configured_pending_path) if configured_pending_path else None
        )
        self.retention_scopes = _retention_scopes()
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> LangSmithRetentionWorker:
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

    def _delete_trace_ids(
        self,
        project_id: str,
        trace_ids: list[str],
        *,
        on_batch_accepted: Callable[[list[str]], None] | None = None,
    ) -> int:
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
                    if on_batch_accepted is not None:
                        on_batch_accepted(batch)
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

    def _load_pending(self) -> dict[str, set[str]]:
        if self.pending_state_path is None:
            return {}
        try:
            payload = json.loads(self.pending_state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(project_id): {
                str(trace_id)
                for trace_id in trace_ids
                if str(trace_id).strip()
            }
            for project_id, trace_ids in payload.items()
            if isinstance(trace_ids, list)
        }

    def _save_pending(self, pending: dict[str, set[str]]) -> None:
        if self.pending_state_path is None:
            return
        path = self.pending_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    project_id: sorted(trace_ids)
                    for project_id, trace_ids in pending.items()
                    if trace_ids
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

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
        queued_runs = 0
        pending_visible = visible_overflow = 0
        pass_error_code: str | None = None
        pending = self._load_pending()
        client: Any | None = None
        try:
            from langsmith import Client

            client = Client(hide_inputs=True, hide_outputs=True, hide_metadata=True)
            for scope, project_name, default_days in self._projects():
                retention_days = self._retention_days(scope, default_days)
                scan_start = current - timedelta(days=max(self.scan_window_days, retention_days))
                project_id = resolve_project_id(client, project_name)
                # Read complete trace trees, not only roots. A root-only query
                # can report exactly 1,000 traces while their child runs keep
                # consuming quota. The query is still bounded; if the bound is
                # reached we fail closed and do not delete from an incomplete
                # view of the project.
                query_limit = self.query_max_runs
                runs = query_runs(
                    client,
                    project_name=project_name,
                    min_start_time=scan_start,
                    max_start_time=current,
                    is_root=None,
                    page_size=100,
                    max_results=query_limit,
                    selects=["ID", "TRACE_ID", "IS_ROOT", "START_TIME"],
                )
                scanned += len(runs)
                if len(runs) >= query_limit:
                    # There may be more rows beyond the page bound. Deleting
                    # from a partial tree view could remove recent data or
                    # leave the project above the cap unpredictably.
                    pass_error_code = pass_error_code or "QUERY_TRUNCATED"
                    LOG.warning(
                        "langsmith-retention query truncated project=%s limit=%d",
                        project_name,
                        query_limit,
                    )
                    continue

                trace_groups: dict[str, list[Any]] = {}
                for run in runs:
                    trace_id = self._run_id(run)
                    if trace_id:
                        trace_groups.setdefault(trace_id, []).append(run)
                if sum(len(group) for group in trace_groups.values()) != len(runs):
                    # A child without a trace_id cannot be safely associated
                    # with a root, so never partially retain/delete it.
                    pass_error_code = pass_error_code or "TRACE_ID_MISSING"
                    LOG.warning(
                        "langsmith-retention run missing trace_id project=%s",
                        project_name,
                    )
                    continue

                # The API returns newest rows first, but the retention decision
                # is based on complete trees and is sorted locally as a second
                # guard. The earliest member is normally the root's timestamp;
                # it also handles sparse root metadata without moving a tree
                # into the future.
                ordered_groups = sorted(
                    trace_groups.items(),
                    key=lambda item: (
                        min(self._started_at(run) for run in item[1]),
                        item[0],
                    ),
                    reverse=True,
                )
                current_ids = set(trace_groups)
                project_pending = pending.setdefault(project_id, set())
                # LangSmith processes deletes asynchronously. Keep only IDs
                # still visible in the bounded scan so a completed deletion
                # leaves the pending ledger naturally.
                project_pending.intersection_update(current_ids)
                visible_overflow += max(0, len(runs) - self.max_runs)

                kept_runs = 0
                candidate_ids: list[str] = []
                for trace_id, members in ordered_groups:
                    member_count = len(members)
                    # Always retain the newest tree, even if a malformed or
                    # unusually large single tree exceeds the cap. Deleting a
                    # current request as a way to satisfy a quota is unsafe.
                    if kept_runs == 0 or kept_runs + member_count <= self.max_runs:
                        kept_runs += member_count
                    else:
                        candidate_ids.append(trace_id)
                        if len(candidate_ids) >= self.max_delete_per_pass:
                            break
                skipped += sum(trace_id in project_pending for trace_id in candidate_ids)
                trace_ids = [
                    trace_id
                    for trace_id in candidate_ids
                    if trace_id not in project_pending
                ]
                eligible += len(trace_ids)
                if effective_dry_run:
                    pending_visible += len(project_pending)
                    continue

                def mark_batch(batch: list[str]) -> None:
                    nonlocal queued_runs
                    project_pending.update(batch)
                    queued_runs += sum(len(trace_groups[trace_id]) for trace_id in batch)
                    # Persist after every accepted batch. A process restart or
                    # provider timeout must not cause the same deletion batch
                    # to be submitted again on the next pass.
                    self._save_pending(pending)

                queued = self._delete_trace_ids(
                    project_id=project_id,
                    trace_ids=trace_ids,
                    on_batch_accepted=mark_batch,
                )
                deleted += queued
                pending_visible += len(project_pending)
                self._save_pending(pending)
            LOG.info(
                "langsmith-retention enabled=true dry_run=%s scanned_runs=%d eligible_trees=%d queued_trees=%d queued_runs=%d pending_visible=%d visible_overflow_runs=%d skipped_pending=%d",
                str(effective_dry_run).lower(),
                scanned,
                eligible,
                deleted,
                queued_runs,
                pending_visible,
                visible_overflow,
                skipped,
            )
            return LangSmithRetentionSummary(
                True,
                True,
                effective_dry_run,
                scanned=scanned,
                eligible=eligible,
                deleted=deleted,
                queued_runs=queued_runs,
                pending_visible=pending_visible,
                visible_overflow=visible_overflow,
                skipped=skipped,
                error_code=pass_error_code,
            )
        except Exception as exc:  # noqa: BLE001 - maintenance must not stop other retention jobs
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
                queued_runs=queued_runs,
                pending_visible=pending_visible,
                visible_overflow=visible_overflow,
                skipped=skipped,
                error_code=error_code,
            )
        finally:
            if client is not None:
                close_query_client(client)


__all__ = [
    "LangSmithRetentionRateLimited",
    "LangSmithRetentionSummary",
    "LangSmithRetentionWorker",
]
