"""Root-scoped Kanban retention maintenance lane.

This module is intentionally outside the supervisor and request paths. Reads
use the existing Hermes Kanban JSON boundary and the existing authoritative
workflow reconstruction. The final archive/purge mutation uses one SQLite
transaction because Hermes' public CLI only offers per-task archive/delete.

No task payload or LLM trace is written to the audit store. The raw Hermes
rows are held in memory only while a candidate is being evaluated.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Protocol

from apps.api.ceo_kanban_read import (
    KanbanTaskNotFound,
    KanbanUnavailable,
    Workflow,
    WorkflowNode,
    is_ceo_root_body,
    list_tasks,
    load_workflow,
)
from orchestration.adapters.ceo_supervisor import (
    TERMINAL_STATUSES,
    HermesKanbanClient,
    HermesKanbanCommandError,
)
from orchestration.discord_delivery import (
    DiscordCorrelation,
    correlation_from_task,
)
from orchestration.discord_idempotency import canonical_discord_dedup_key
from orchestration.kanban_retention_lock import (
    kanban_db_path,
    workflow_mutation_lock,
)

LOG = logging.getLogger(__name__)


def _age_days(name: str, default_days: float) -> int:
    """Resolve one retention window, in days, from the maintenance environment.

    These are read once at import because the only caller is the long-lived
    maintenance container, whose environment is fixed for the life of the
    process.  Keeping them overridable matters operationally: the board's size
    is what makes `hermes kanban show` slow, and a slow show is what turns a
    PAPER order into HTTP 503 `paper_order_kanban_unavailable` at the BFF.
    Shrinking the window is therefore a latency lever, not just housekeeping.
    """

    try:
        days = float(os.getenv(name, "") or default_days)
    except ValueError:
        days = default_days
    # Never let a typo purge the live board out from under a running workflow;
    # one day is already below every archive guard in this module.
    return int(max(1.0, days) * 24 * 60 * 60)


ACTIVE_AGE_SECONDS = 24 * 60 * 60
PURGE_AGE_SECONDS = _age_days("KANBAN_RETENTION_PURGE_AGE_DAYS", 7)
# A blocked/triage card remains actionable for the archive window.  After that
# it is archived like the rest of its root graph, including user-input/approval
# blocks, so abandoned conversations cannot accumulate forever.
BLOCKED_ARCHIVE_AGE_SECONDS = _age_days("KANBAN_RETENTION_BLOCKED_AGE_DAYS", 7)
ARCHIVED_STATUS = "archived"
AUDIT_CAPSULE_SCHEMA_VERSION = "qa-hr.audit.v1"
ACTIVE_RUN_STATUSES = frozenset({"running", "claimed", "spawned", "processing"})
RECOVERY_PENDING_VALUES = frozenset(
    {"pending", "queued", "running", "retry", "retrying", "required", "open"}
)
RECOVERY_TERMINAL_EVENTS = frozenset(
    {"recovered", "recovery_completed", "recovery_succeeded", "recovery_closed"}
)
_ROOT_ROLE_RE = re.compile(r"(?m)^(?:workflow_role|root_task_role)=([\w-]+)")
_REQUEST_ID_RE = re.compile(r"(?m)^request_id=(\S+)\s*$")
_LEGACY_ROOT_PLACEHOLDERS = frozenset(
    {
        "assigned-on-create",
        "pending",
        "root_pending",
        "root_task_id_to_be_filled_by_system",
        "root_to_be_filled",
        "this-task",
        "this_task_id",
        "to_be_filled",
    }
)


class RetentionError(RuntimeError):
    """Retention could not safely complete a maintenance operation."""


@dataclass(frozen=True)
class DeliveryState:
    status: str
    message_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class RetentionDecision:
    root_id: str
    eligible: bool
    reason: str
    terminal_at: int | None = None
    delivery: DeliveryState | None = None


@dataclass(frozen=True)
class AuditMetadata:
    root_id: str
    request_id: str | None
    final_status: str
    created_at: int | None
    terminal_at: int | None
    completed_at: int | None
    departments: tuple[str, ...]
    total_latency_ms: int | None
    final_result_ref: str | None
    discord_thread_id: str | None
    discord_message_id: str | None
    qa_hr_capsule_json: str


@dataclass(frozen=True)
class RetentionRun:
    active_task_count: int
    active_root_count: int
    archived_root_count: int
    archived_count: int
    purged_count: int
    list_ms: float
    reconstruction_ms: float
    recovery_ms: float
    cleanup_ms: float
    skipped: tuple[tuple[str, str], ...] = ()
    eligible_root_ids: tuple[str, ...] = ()
    would_archive_root_ids: tuple[str, ...] = ()
    eligible_task_count: int = 0
    would_archive_task_count: int = 0
    blocked_reason_histogram: tuple[tuple[str, int], ...] = ()
    artifact_removed_count: int = 0
    artifact_cleanup_skipped_count: int = 0
    capsule_backfilled_count: int = 0


class KanbanMaintenance(Protocol):
    def archive_workflow(self, root_id: str, task_ids: Sequence[str]) -> bool: ...

    def purge_workflow(self, root_id: str, task_ids: Sequence[str]) -> bool: ...

    def workflow_exists(self, root_id: str) -> bool: ...


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = int(value)
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.isdigit():
                return int(text)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _raw_value(payload: Mapping[str, Any], names: Iterable[str]) -> Any:
    lowered = {str(key).casefold(): value for key, value in payload.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _iter_nested(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        yield payload
        for value in payload.values():
            if isinstance(value, (Mapping, list, tuple)):
                yield from _iter_nested(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _iter_nested(value)


def _run_rows(node: WorkflowNode) -> tuple[Mapping[str, Any], ...]:
    runs = node.raw.get("runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in runs if isinstance(item, Mapping))


def _safe_capsule_label(value: Any) -> str | None:
    """Return a bounded enum-like label; never persist free-form error text."""

    text = str(value or "").strip().upper()
    if not text or len(text) > 64 or not re.fullmatch(r"[A-Z0-9_.:-]+", text):
        return None
    return text


def _is_hr_node(node: WorkflowNode) -> bool:
    profile = node.profile.casefold()
    department = node.department.casefold()
    return profile in {"hr-department", "workforce", "workforce-department"} or department in {
        "hr",
        "workforce",
    }


def _count_labels(values: Iterable[str | None]) -> dict[str, int]:
    counts = Counter(value for value in values if value)
    return dict(sorted(counts.items()))


def _node_failure_category(node: WorkflowNode) -> str | None:
    for key in ("failure_category", "error_category", "block_kind", "protocol_state"):
        label = _safe_capsule_label(node.raw.get(key))
        if label:
            return label
    return None


def _run_summary(node: WorkflowNode) -> dict[str, int]:
    runs = _run_rows(node)
    protocol_violations = 0
    gave_up = 0
    for run in runs:
        values = [
            str(run.get(key) or "").casefold()
            for key in ("failure_category", "error_category", "protocol_state", "outcome", "status")
        ]
        if any("protocol_violation" in value for value in values):
            protocol_violations += 1
        if any(value in {"gave_up", "give_up"} for value in values) or bool(run.get("gave_up")):
            gave_up += 1
    return {
        "run_count": len(runs),
        "retry_count": max(len(runs) - 1, 0),
        "protocol_violation_count": protocol_violations,
        "gave_up_count": gave_up,
    }


def _capsule_section(nodes: Sequence[WorkflowNode], *, root_task_id: str) -> dict[str, Any]:
    roles = [node.role(root_task_id=root_task_id) for node in nodes]
    run_totals = Counter()
    for node in nodes:
        run_totals.update(_run_summary(node))
    return {
        "task_count": len(nodes),
        "task_ids": [node.task_id for node in nodes],
        "roles": _count_labels(roles),
        "profiles": _count_labels(node.profile for node in nodes),
        "statuses": _count_labels(node.status for node in nodes),
        "failure_categories": _count_labels(_node_failure_category(node) for node in nodes),
        **dict(sorted(run_totals.items())),
    }


def build_qa_hr_capsule(workflow: Workflow) -> str:
    """Build the compact post-purge evidence retained for QA and HR.

    This deliberately excludes body, title, summary, result, comments, raw
    events, raw run errors, provider output, and user content. Task IDs and
    aggregate execution fields are retained so the two review functions can
    judge lifecycle, assignment, retry, and protocol quality after purge.
    """

    nodes = workflow.nodes
    qa_nodes = tuple(node for node in nodes if node.is_qa)
    hr_nodes = tuple(node for node in nodes if _is_hr_node(node))
    capsule = {
        "schema_version": AUDIT_CAPSULE_SCHEMA_VERSION,
        "root_id": workflow.root_task_id,
        "workflow": {
            "task_count": len(nodes),
            "task_ids": [node.task_id for node in nodes],
            "final_status": workflow.status,
            "qa_enabled": bool(workflow.qa_enabled),
            "qa_blocks_response": bool(workflow.qa_blocks_response),
            "qa_materialized": bool(workflow.qa_materialized),
            "qa_legacy_primary_present": bool(workflow.qa_legacy_primary_present),
        },
        "qa": _capsule_section(qa_nodes, root_task_id=workflow.root_task_id),
        "hr": _capsule_section(hr_nodes, root_task_id=workflow.root_task_id),
    }
    return json.dumps(capsule, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _has_active_execution(node: WorkflowNode) -> bool:
    status = node.status.casefold()
    if status in ACTIVE_RUN_STATUSES:
        return True
    for key in ("current_run_id", "claim_lock", "claim_expires", "worker_pid"):
        value = node.raw.get(key)
        if value not in (None, "", 0, False):
            return True
    for run in _run_rows(node):
        run_status = str(run.get("status") or "").casefold()
        if run_status in ACTIVE_RUN_STATUSES:
            return True
        if run.get("ended_at") in (None, "") and not run.get("outcome"):
            return True
    # A spawned/claimed event without a later terminal event is still unsafe
    # even when a legacy Hermes build omits task_runs from ``show --json``.
    events = node.raw.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes, bytearray)):
        latest_active = False
        for event in events:
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or "").casefold()
            if kind in {"claimed", "spawned", "started", "running"}:
                latest_active = True
            elif kind in {
                "completed",
                "done",
                "blocked",
                "failed",
                "crashed",
                "timed_out",
                "spawn_failed",
                "gave_up",
                "reclaimed",
                "archived",
            }:
                latest_active = False
        if latest_active:
            return True
    return False


def _has_recovery_pending(node: WorkflowNode) -> bool:
    for payload in _iter_nested(node.raw):
        for key, value in payload.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized not in {
                "recovery_pending",
                "pending_recovery",
                "recovery_status",
                "recovery_state",
            }:
                continue
            if isinstance(value, bool) and value:
                return True
            if str(value or "").strip().casefold() in RECOVERY_PENDING_VALUES:
                return True

    events = node.raw.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes, bytearray)):
        pending = False
        for event in events:
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or "").casefold()
            if kind in RECOVERY_TERMINAL_EVENTS:
                pending = False
            elif "recovery" in kind and ("pending" in kind or "retry" in kind):
                pending = True
        if pending:
            return True

    for text in (node.body, node.error, node.block_reason):
        if re.search(r"(?im)^(?:recovery_pending|pending_recovery)=(?:true|pending|1)\s*$", text):
            return True
    return False


def _blocked_at(node: WorkflowNode, *, fallback: int | None = None) -> int | None:
    """Find the latest durable block timestamp without trusting free text."""

    block_timestamps: list[int] = []
    for key in ("blocked_at", "block_started_at"):
        timestamp = _epoch(node.raw.get(key))
        if timestamp is not None:
            block_timestamps.append(timestamp)
    events = node.raw.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes, bytearray)):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or "").casefold().replace("-", "_")
            if kind in {"blocked", "block", "status_blocked"}:
                timestamp = _epoch(event.get("created_at"))
                if timestamp is not None:
                    block_timestamps.append(timestamp)
    if block_timestamps:
        return max(block_timestamps)
    # Legacy rows may not have a block event or explicit blocked_at.  Their
    # creation time is the only safe lower bound; updated_at is preferred when
    # available because it is normally written at the state transition.
    for key in ("updated_at", "created_at"):
        timestamp = _epoch(node.raw.get(key))
        if timestamp is not None:
            return timestamp
    return fallback


def _correlation(workflow: Workflow) -> DiscordCorrelation:
    values: dict[str, str | None] = {}
    for node in (workflow.synthesis_node, workflow.root):
        if node is None:
            continue
        current = correlation_from_task(node.raw)
        for key in ("request_id", "message_id", "guild_id", "channel_id", "thread_id", "session_id"):
            if values.get(key) is None:
                values[key] = getattr(current, key)
    return DiscordCorrelation(**values)


class DiscordLedgerReader:
    """Read existing Discord idempotency state without changing delivery code."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)

    def _path(self) -> Path:
        home = Path(self.environment.get("HERMES_HOME", "/opt/data"))
        profile_home = home / "profiles" / self.environment.get("HERMES_CEO_PROFILE", "ceo-agent")
        if not profile_home.exists():
            profile_home = home
        return profile_home / "gateway" / "discord_message_recovery.db"

    def state(self, workflow: Workflow) -> DeliveryState:
        correlation = _correlation(workflow)
        has_discord_context = any(
            getattr(correlation, field)
            for field in ("message_id", "channel_id", "thread_id", "guild_id")
        )
        if not has_discord_context:
            return DeliveryState("not_required")

        path = self._path()
        if not path.exists():
            return DeliveryState("unknown", thread_id=correlation.thread_id)
        keys: set[str] = set()
        with closing(sqlite3.connect(path, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            if correlation.message_id:
                row = conn.execute(
                    "SELECT dedup_key FROM discord_idempotency_inbound "
                    "WHERE profile = ? AND message_id = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (self.environment.get("HERMES_CEO_PROFILE", "ceo-agent"), correlation.message_id),
                ).fetchone()
                if row:
                    keys.add(str(row["dedup_key"]))
            if correlation.channel_id and correlation.message_id:
                keys.add(
                    canonical_discord_dedup_key(
                        correlation.guild_id,
                        correlation.channel_id,
                        correlation.message_id,
                    )
                )
            if not keys:
                return DeliveryState("unknown", thread_id=correlation.thread_id)
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                "SELECT state, response_message_id, response_key FROM "
                "discord_idempotency_outbound WHERE profile = ? "
                f"AND dedup_key IN ({placeholders}) ORDER BY updated_at DESC",
                (self.environment.get("HERMES_CEO_PROFILE", "ceo-agent"), *sorted(keys)),
            ).fetchall()
        if not rows:
            return DeliveryState("pending", thread_id=correlation.thread_id)
        failed = False
        processing = False
        for row in rows:
            response_key = str(row["response_key"] or "")
            suffix = response_key.rsplit(":", 1)[-1]
            # Department progress cards share the same inbound dedup key but
            # are not the required final response. Only the final/synthesis
            # response keys close the retention delivery guard.
            is_final = (
                suffix == "final"
                or ":synthesis-detail:" in response_key
                or ":single-primary-detail:" in response_key
                or ":ceo-direct:" in response_key
            )
            if not is_final:
                continue
            state = str(row["state"] or "").casefold()
            if state == "completed":
                return DeliveryState(
                    "completed",
                    message_id=str(row["response_message_id"] or "") or correlation.message_id,
                    thread_id=correlation.thread_id,
                )
            if state == "failed":
                failed = True
            elif state in {"processing", "claimed", "running"}:
                processing = True
        if failed:
            return DeliveryState("failed", thread_id=correlation.thread_id)
        if processing:
            return DeliveryState("pending", thread_id=correlation.thread_id)
        return DeliveryState("pending", thread_id=correlation.thread_id)


def evaluate_workflow(
    workflow: Workflow,
    *,
    now: int | None = None,
    delivery: DeliveryState | None = None,
) -> RetentionDecision:
    """Evaluate every archive guard against one authoritative snapshot."""

    current = int(time.time()) if now is None else int(now)
    root = workflow.root
    blocked_nodes = tuple(
        node for node in workflow.nodes if node.status in {"blocked", "triage"}
    )
    root_terminal_at = _epoch(
        _raw_value(root.raw, ("terminal_at", "completed_at"))
    )
    root_expired = bool(
        root_terminal_at is not None
        and current >= root_terminal_at
        and current - root_terminal_at > PURGE_AGE_SECONDS
    )
    terminal_at = root_terminal_at
    if blocked_nodes:
        blocked_at = max(
            (
                timestamp
                for node in blocked_nodes
                for timestamp in (_blocked_at(node, fallback=terminal_at),)
                if timestamp is not None
            ),
            default=terminal_at,
        )
        if blocked_at is None:
            return RetentionDecision(workflow.root_task_id, False, "blocked_at_missing")
        terminal_at = blocked_at
        if (
            not root_expired
            and (current < blocked_at or current - blocked_at <= BLOCKED_ARCHIVE_AGE_SECONDS)
        ):
            return RetentionDecision(
                workflow.root_task_id,
                False,
                "blocked_under_7d",
                terminal_at,
            )
        if root_expired:
            # A terminal root must not be revived indefinitely by late retry
            # noise. Active runs and explicit recovery state are still checked
            # below before archive is allowed.
            terminal_at = root_terminal_at
    else:
        if terminal_at is None:
            return RetentionDecision(workflow.root_task_id, False, "root_terminal_at_missing")
        if current < terminal_at or current - terminal_at <= ACTIVE_AGE_SECONDS:
            return RetentionDecision(workflow.root_task_id, False, "terminal_under_24h", terminal_at)
    if root.status not in TERMINAL_STATUSES:
        return RetentionDecision(workflow.root_task_id, False, "root_not_terminal", terminal_at)

    for node in workflow.nodes:
        if node.status not in TERMINAL_STATUSES:
            return RetentionDecision(workflow.root_task_id, False, f"unfinished:{node.task_id}", terminal_at)
        if _has_active_execution(node):
            return RetentionDecision(workflow.root_task_id, False, f"active_execution:{node.task_id}", terminal_at)
        if _has_recovery_pending(node):
            return RetentionDecision(workflow.root_task_id, False, f"recovery_pending:{node.task_id}", terminal_at)

    observed_delivery = delivery or DeliveryState("unknown")
    # After the explicit seven-day board TTL, stale delivery/synthesis state
    # must not keep an otherwise terminal graph forever. Active executions and
    # recovery work were still checked above; preserve the last known delivery
    # metadata in the compact audit capsule and allow archive/purge to proceed.
    if terminal_at is not None and current - terminal_at > PURGE_AGE_SECONDS:
        return RetentionDecision(
            workflow.root_task_id,
            True,
            "safe_expired",
            terminal_at,
            observed_delivery,
        )
    if observed_delivery.status not in {"completed", "not_required", "deduped"}:
        return RetentionDecision(
            workflow.root_task_id,
            False,
            f"discord_delivery_{observed_delivery.status}",
            terminal_at,
            observed_delivery,
        )

    # Any multi-primary/QA workflow must have a terminal synthesis task. The
    # existing supervisor also has a narrow single-primary passthrough where
    # that primary is the final processor; accept it only when its result is
    # durable and delivery is already terminal. A root-only direct answer has
    # no separate synthesis task and is already final.
    synthesis = workflow.synthesis_node
    if synthesis is not None and synthesis.status not in TERMINAL_STATUSES:
        return RetentionDecision(workflow.root_task_id, False, "synthesis_not_terminal", terminal_at)
    if (workflow.primary_nodes or workflow.qa_nodes) and synthesis is None:
        single_primary_passthrough = (
            len(workflow.primary_nodes) == 1
            and not workflow.qa_nodes
        )
        if single_primary_passthrough and not _has_final_processing(workflow.primary_nodes[0]):
            return RetentionDecision(
                workflow.root_task_id,
                False,
                "final_processing_pending",
                terminal_at,
                observed_delivery,
            )
        if not single_primary_passthrough:
            return RetentionDecision(workflow.root_task_id, False, "required_synthesis_missing", terminal_at)
    return RetentionDecision(
        workflow.root_task_id,
        True,
        "safe",
        terminal_at,
        observed_delivery,
    )


def retention_block_category(reason: str) -> str:
    """Map internal fail-closed reasons to rollout-safe report buckets."""

    normalized = str(reason or "")
    if normalized == "terminal_under_24h":
        return "TERMINAL_UNDER_24H"
    if normalized == "blocked_under_7d":
        return "BLOCKED_UNDER_7D"
    if normalized.startswith("blocked_needs_input:"):
        return "BLOCKED_NEEDS_INPUT"
    if normalized == "blocked_at_missing":
        return "BLOCKED_TIMESTAMP_MISSING"
    if normalized == "root_not_terminal" or normalized.startswith("unfinished:"):
        return "ACTIVE_DESCENDANT"
    if normalized.startswith("active_execution:"):
        return "CLAIM_OR_RUN_PRESENT"
    if normalized.startswith("recovery_pending:"):
        return "RECOVERY_PENDING"
    if normalized in {"synthesis_not_terminal", "required_synthesis_missing"}:
        return "SYNTHESIS_PENDING"
    if normalized == "final_processing_pending":
        return "FINAL_PROCESSING_PENDING"
    if normalized == "discord_delivery_missing_thread":
        return "DISCORD_MISSING_THREAD"
    if normalized == "discord_delivery_failed":
        return "DISCORD_DELIVERY_FAILED"
    if normalized == "discord_delivery_pending":
        return "DISCORD_DELIVERY_PENDING"
    return "UNKNOWN/LEGACY"


def _root_candidate(row: Mapping[str, Any]) -> bool:
    task_id = str(row.get("id") or row.get("task_id") or "").strip()
    body = str(row.get("body") or "")
    if not task_id or not body:
        return False
    if is_ceo_root_body(body):
        return True
    roles = {match.group(1).casefold() for match in _ROOT_ROLE_RE.finditer(body)}
    if roles & {"root", "planning", "scope", "scope_and_planning"}:
        # Several legacy producers persisted a placeholder or omitted the
        # workflow_root_task_id while still declaring the authoritative root
        # role. The role line is sufficient to keep those graphs collectible.
        return True
    owns_marker = f"workflow_root_task_id={task_id}" in body
    return owns_marker and not roles


def _standalone_candidate(row: Mapping[str, Any]) -> bool:
    """Include old diagnostic cards that predate workflow-root markers.

    The shared board contains legitimate one-card tests and factory diagnostics
    created before the CEO workflow marker was introduced.  They have no
    durable parent graph, so leaving them outside the retention scan makes an
    old ``done``/``blocked`` card immortal.  A card carrying another root's
    marker is deliberately excluded; its owning root must be archived
    atomically with all descendants.
    """

    task_id = str(row.get("id") or row.get("task_id") or "").strip()
    status = str(row.get("status") or "").casefold()
    body = str(row.get("body") or "")
    if not task_id or status not in TERMINAL_STATUSES or _root_candidate(row):
        return False
    marker = re.search(r"(?m)^workflow_root_task_id=(\S+)", body)
    return marker is None or marker.group(1).strip().casefold() in _LEGACY_ROOT_PLACEHOLDERS


def _has_marker_descendant(
    rows: Sequence[Mapping[str, Any]],
    root_id: str,
) -> bool:
    """Detect legacy descendants that have a root marker but no task link."""

    for row in rows:
        task_id = str(row.get("id") or row.get("task_id") or "").strip()
        if not task_id or task_id == root_id:
            continue
        body = str(row.get("body") or "")
        marker = re.search(r"(?m)^workflow_root_task_id=(\S+)", body)
        if marker and marker.group(1).strip() == root_id:
            return True
    return False


def _archive_scan_root_ids(
    rows: Sequence[Mapping[str, Any]],
    *,
    linked_root_ids: Iterable[str] = (),
    linked_task_ids: Iterable[str] = (),
    now: int | None = None,
) -> tuple[str, ...]:
    """Order root inspection toward the oldest terminal workflows first.

    The authoritative workflow reconstruction remains the eligibility gate;
    row fields are used only to choose scan order. This prevents a bounded
    maintenance pass from reconstructing the entire historical board before
    it can archive its first small batch.
    """

    linked_roots = {str(value) for value in linked_root_ids if str(value).strip()}
    linked_tasks = {str(value) for value in linked_task_ids if str(value).strip()}
    candidates: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("id") or row.get("task_id") or "").strip()
        standalone = _standalone_candidate(row) and task_id not in linked_tasks
        if not (_root_candidate(row) or standalone or task_id in linked_roots):
            continue
        if now is not None:
            status = str(row.get("status") or "").casefold()
            observed_at = _epoch(
                _raw_value(row, ("blocked_at", "block_started_at", "completed_at", "terminal_at", "created_at"))
            )
            age_limit = (
                BLOCKED_ARCHIVE_AGE_SECONDS
                if status in {"blocked", "triage"}
                else ACTIVE_AGE_SECONDS
            )
            # A legacy/minimal row may not expose a durable timestamp.  Do
            # not silently drop it from the scan; let the authoritative
            # workflow evaluator fail closed (or recover the timestamp from
            # the reconstructed nodes).
            if status not in TERMINAL_STATUSES or (
                observed_at is not None and now - observed_at <= age_limit
            ):
                continue
        root_id = task_id
        if root_id:
            candidates.setdefault(root_id, row)

    def order(item: tuple[str, Mapping[str, Any]]) -> tuple[int, int, str]:
        root_id, row = item
        terminal_rank = (
            0 if str(row.get("status") or "").casefold() in TERMINAL_STATUSES else 1
        )
        observed_at = (
            _epoch(row.get("completed_at"))
            or _epoch(row.get("terminal_at"))
            or _epoch(row.get("created_at"))
            or 2**63 - 1
        )
        return terminal_rank, observed_at, root_id

    return tuple(root_id for root_id, _row in sorted(candidates.items(), key=order))


def _request_id(workflow: Workflow) -> str | None:
    correlation = _correlation(workflow)
    if correlation.request_id:
        return correlation.request_id
    match = _REQUEST_ID_RE.search(workflow.root.body)
    return match.group(1) if match else None


def _metadata_value(workflow: Workflow, names: Iterable[str]) -> Any:
    lowered = {name.casefold() for name in names}
    for payload in _iter_nested(workflow.root.raw):
        for key, value in payload.items():
            if str(key).casefold() in lowered and value not in (None, ""):
                return value
    return None


def _has_final_processing(node: WorkflowNode) -> bool:
    """Whether a terminal direct-response node has a durable final result."""

    if node.summary.strip():
        return True
    for payload in _iter_nested(node.raw):
        for key in ("final_answer", "final_result", "result", "summary", "latest_summary"):
            if payload.get(key) not in (None, "", [], {}):
                return True
    return False


def _archived_at(workflow: Workflow, *, fallback: int) -> int:
    """Recover the original archive event time for legacy archived rows."""

    timestamps: list[int] = []
    for node in workflow.nodes:
        events = node.raw.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if str(event.get("kind") or "").casefold() == ARCHIVED_STATUS:
                timestamp = _epoch(event.get("created_at"))
                if timestamp is not None:
                    timestamps.append(timestamp)
    return max(timestamps, default=int(fallback))


def _evaluate_legacy_archived_workflow(
    workflow: Workflow,
    *,
    now: int,
    delivery: DeliveryState,
) -> tuple[Workflow, RetentionDecision]:
    """Use a durable archive event only for a legacy missing terminal time.

    The repaired snapshot is then passed through the normal evaluator, so an
    active run, recovery marker, delivery failure, or incomplete synthesis
    still blocks retention. No separate legacy eligibility policy is created.
    """

    decision = evaluate_workflow(workflow, now=now, delivery=delivery)
    if decision.reason != "root_terminal_at_missing":
        return workflow, decision
    if workflow.root.status != ARCHIVED_STATUS:
        return workflow, decision

    archived_at = _archived_at(workflow, fallback=now)
    if archived_at >= now:
        return workflow, decision
    repaired_root = dict(workflow.root.raw, terminal_at=archived_at)
    repaired_nodes = tuple(
        WorkflowNode.from_hermes(repaired_root)
        if node.task_id == workflow.root_task_id
        else node
        for node in workflow.nodes
    )
    repaired = Workflow(
        root_task_id=workflow.root_task_id,
        nodes=repaired_nodes,
        metadata=workflow.metadata,
        root_payload=repaired_root,
    )
    return repaired, evaluate_workflow(repaired, now=now, delivery=delivery)


def build_audit_metadata(workflow: Workflow, delivery: DeliveryState) -> AuditMetadata:
    root_created = _epoch(workflow.root.raw.get("created_at"))
    terminal_at = _epoch(workflow.root.raw.get("terminal_at") or workflow.root.raw.get("completed_at"))
    completed_values = [
        _epoch(node.raw.get("completed_at"))
        for node in workflow.nodes
        if _epoch(node.raw.get("completed_at")) is not None
    ]
    completed_at = max(completed_values, default=terminal_at)
    latency = None
    if root_created is not None and completed_at is not None and completed_at >= root_created:
        latency = (completed_at - root_created) * 1000
    departments = tuple(dict.fromkeys(node.department for node in workflow.primary_nodes if node.department))
    result_ref = _metadata_value(workflow, ("final_result_ref", "result_ref", "artifact_ref"))
    if result_ref in (None, ""):
        final_node = workflow.synthesis_node or workflow.root
        result_ref = f"kanban-task:{final_node.task_id}"
    correlation = _correlation(workflow)
    return AuditMetadata(
        root_id=workflow.root_task_id,
        request_id=_request_id(workflow),
        final_status=workflow.status,
        created_at=root_created,
        terminal_at=terminal_at,
        completed_at=completed_at,
        departments=departments,
        total_latency_ms=latency,
        final_result_ref=str(result_ref),
        discord_thread_id=delivery.thread_id or correlation.thread_id,
        discord_message_id=delivery.message_id or correlation.message_id,
        qa_hr_capsule_json=build_qa_hr_capsule(workflow),
    )


class AuditStore:
    """Minimal durable audit metadata, separate from the Kanban database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS workflow_retention_audit (
                root_id TEXT PRIMARY KEY,
                request_id TEXT,
                final_status TEXT NOT NULL,
                created_at INTEGER,
                terminal_at INTEGER,
                completed_at INTEGER,
                departments TEXT NOT NULL,
                total_latency_ms INTEGER,
                final_result_ref TEXT,
                discord_thread_id TEXT,
                discord_message_id TEXT,
                archived_at INTEGER NOT NULL,
                purged_at INTEGER,
                qa_hr_capsule_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(workflow_retention_audit)")
        }
        if "qa_hr_capsule_json" not in columns:
            # Compatibility migration for the existing minimal audit DB. This
            # is the audit store only; the operational Kanban schema/data is
            # never rewritten by this migration.
            conn.execute(
                "ALTER TABLE workflow_retention_audit "
                "ADD COLUMN qa_hr_capsule_json TEXT NOT NULL DEFAULT '{}'"
            )
        conn.commit()
        return conn

    def save_archive(self, metadata: AuditMetadata, *, archived_at: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO workflow_retention_audit
                (root_id, request_id, final_status, created_at, terminal_at,
                 completed_at, departments, total_latency_ms, final_result_ref,
                 discord_thread_id, discord_message_id, archived_at,
                 qa_hr_capsule_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_id) DO UPDATE SET
                  request_id=excluded.request_id,
                  final_status=excluded.final_status,
                  created_at=excluded.created_at,
                  terminal_at=excluded.terminal_at,
                  completed_at=excluded.completed_at,
                  departments=excluded.departments,
                  total_latency_ms=excluded.total_latency_ms,
                  final_result_ref=excluded.final_result_ref,
                  discord_thread_id=excluded.discord_thread_id,
                  discord_message_id=excluded.discord_message_id,
                  qa_hr_capsule_json=excluded.qa_hr_capsule_json,
                  archived_at=MIN(workflow_retention_audit.archived_at, excluded.archived_at)""",
                (
                    metadata.root_id,
                    metadata.request_id,
                    metadata.final_status,
                    metadata.created_at,
                    metadata.terminal_at,
                    metadata.completed_at,
                    json.dumps(metadata.departments, ensure_ascii=False),
                    metadata.total_latency_ms,
                    metadata.final_result_ref,
                    metadata.discord_thread_id,
                    metadata.discord_message_id,
                    int(archived_at),
                    metadata.qa_hr_capsule_json,
                ),
            )
            conn.commit()

    def get(self, root_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM workflow_retention_audit WHERE root_id = ?", (root_id,)
            ).fetchone()

    def update_capsule_if_missing(self, root_id: str, capsule_json: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT qa_hr_capsule_json FROM workflow_retention_audit WHERE root_id = ?",
                (root_id,),
            ).fetchone()
            if row is None or str(row[0] or "{}").strip() not in {"", "{}"}:
                return False
            conn.execute(
                "UPDATE workflow_retention_audit SET qa_hr_capsule_json = ? WHERE root_id = ?",
                (capsule_json, root_id),
            )
            conn.commit()
            return True

    def purge_candidates(self, *, now: int) -> tuple[sqlite3.Row, ...]:
        with closing(self._connect()) as conn:
            return tuple(
                conn.execute(
                    "SELECT * FROM workflow_retention_audit "
                    "WHERE purged_at IS NULL "
                    # The board retention clock starts when work becomes
                    # terminal, not when a delayed maintenance pass archives
                    # it.  The purge path still reconstructs the graph and
                    # requires every node to be archived before deletion.
                    "AND COALESCE(terminal_at, completed_at, archived_at) < ? "
                    "ORDER BY COALESCE(terminal_at, completed_at, archived_at)",
                    (int(now - PURGE_AGE_SECONDS),),
                ).fetchall()
            )

    def mark_purged(self, root_id: str, *, purged_at: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE workflow_retention_audit SET purged_at = ? WHERE root_id = ?",
                (int(purged_at), root_id),
            )
            conn.commit()


class SQLiteKanbanMaintenance:
    """Root-atomic mutation adapter for the existing Hermes SQLite board."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)
        self.db_path = kanban_db_path(self.environment)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def workflow_exists(self, root_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? LIMIT 1",
                (str(root_id),),
            ).fetchone() is not None

    def has_task_links(self, task_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id = ? OR child_id = ? LIMIT 1",
                (str(task_id), str(task_id)),
            ).fetchone() is not None

    def root_candidate_ids(self, task_ids: Sequence[str]) -> set[str]:
        """Return marker-less graph roots so legacy workflows stay atomic."""

        ids = tuple(dict.fromkeys(str(value) for value in task_ids if str(value)))
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT parent_id, child_id FROM task_links "
                f"WHERE parent_id IN ({placeholders}) OR child_id IN ({placeholders})",
                (*ids, *ids),
            ).fetchall()
        parents = {str(row[0]) for row in rows}
        children = {str(row[1]) for row in rows}
        task_id_set = set(ids)
        return {value for value in parents if value in task_id_set and value not in children}

    def linked_task_ids(self, task_ids: Sequence[str]) -> set[str]:
        """Return every task in a durable legacy graph, including children."""

        ids = tuple(dict.fromkeys(str(value) for value in task_ids if str(value)))
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT parent_id, child_id FROM task_links "
                f"WHERE parent_id IN ({placeholders}) OR child_id IN ({placeholders})",
                (*ids, *ids),
            ).fetchall()
        task_id_set = set(ids)
        return {
            str(value)
            for row in rows
            for value in (row[0], row[1])
            if str(value) in task_id_set
        }

    def existing_task_ids(self, task_ids: Sequence[str]) -> set[str]:
        """Filter stale Hermes list rows with one bounded SQLite lookup."""

        ids = tuple(dict.fromkeys(str(value) for value in task_ids if str(value)))
        if not ids:
            return set()
        existing: set[str] = set()
        with closing(self._connect()) as conn:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                rows = conn.execute(
                    f"SELECT id FROM tasks WHERE id IN ({self._placeholders(chunk)})",
                    chunk,
                ).fetchall()
                existing.update(str(row[0]) for row in rows)
        return existing

    @staticmethod
    def _placeholders(values: Sequence[str]) -> str:
        return ",".join("?" for _ in values)

    def _validate_ids(self, conn: sqlite3.Connection, task_ids: Sequence[str], *, status: str | None = None) -> bool:
        ids = tuple(dict.fromkeys(str(value) for value in task_ids if str(value)))
        if not ids:
            return False
        rows = conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({self._placeholders(ids)})", ids
        ).fetchall()
        if {str(row["id"]) for row in rows} != set(ids):
            return False
        if status is not None and any(str(row["status"]) != status for row in rows):
            return False
        if status is None and any(str(row["status"]) not in TERMINAL_STATUSES for row in rows):
            return False
        active = conn.execute(
            f"SELECT 1 FROM task_runs WHERE task_id IN ({self._placeholders(ids)}) "
            "AND (ended_at IS NULL OR status IN ('running','claimed','spawned','processing')) LIMIT 1",
            ids,
        ).fetchone()
        if active is not None:
            return False
        # A terminal status is not enough if an older Hermes worker left a
        # claim marker behind. Retention must fail closed for claimed work.
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        claim_columns = columns & {"claim_lock", "claim_expires", "worker_pid"}
        if claim_columns:
            predicates = [f"{column} IS NOT NULL" for column in sorted(claim_columns)]
            if "claim_expires" in claim_columns:
                predicates.append("claim_expires != 0")
            claimed = conn.execute(
                f"SELECT 1 FROM tasks WHERE id IN ({self._placeholders(ids)}) "
                f"AND ({' OR '.join(predicates)}) LIMIT 1",
                ids,
            ).fetchone()
            if claimed is not None:
                return False
        return self._scope_matches(conn, root_id=str(ids[0]), task_ids=ids)

    def _scope_matches(
        self,
        conn: sqlite3.Connection,
        *,
        root_id: str,
        task_ids: Sequence[str],
    ) -> bool:
        """Ensure the mutation set is the complete durable root scope.

        The worker normally supplies the set from authoritative reconstruction.
        Rechecking links/markers inside the same ``BEGIN IMMEDIATE`` closes the
        gap where a newly spawned descendant could otherwise be omitted.
        Tiny test/legacy schemas may not have ``body``; their link closure is
        still checked.
        """

        expected = set(task_ids)
        discovered = {root_id}
        links = conn.execute(
            # Dependency edges are directional, but legacy roots and marker-
            # only primaries were not always linked from the owning root.
            # Root-atomic maintenance therefore validates the entire connected
            # component in both directions; the later outside-link guard uses
            # the same complete mutation set.
            "WITH RECURSIVE connected(id) AS ("
            "SELECT ? UNION SELECT CASE "
            "WHEN task_links.parent_id = connected.id THEN task_links.child_id "
            "ELSE task_links.parent_id END FROM task_links "
            "JOIN connected ON task_links.parent_id = connected.id "
            "OR task_links.child_id = connected.id) "
            "SELECT id FROM connected",
            (root_id,),
        ).fetchall()
        discovered.update(str(row[0]) for row in links)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "body" in columns:
            escaped_root = (
                root_id.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            marker = f"%workflow_root_task_id={escaped_root}%"
            marked = conn.execute(
                "SELECT id FROM tasks WHERE body LIKE ? ESCAPE '\\'", (marker,)
            ).fetchall()
            discovered.update(str(row[0]) for row in marked)
        return discovered == expected

    def archive_workflow(self, root_id: str, task_ids: Sequence[str]) -> bool:
        ids = tuple(dict.fromkeys((root_id, *task_ids)))
        with workflow_mutation_lock(root_id, environment=self.environment):  # noqa: SIM117 - keep the mutation lock outermost.
            with closing(self._connect()) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._validate_ids(conn, ids):
                        conn.rollback()
                        return False
                    current_rows = conn.execute(
                        f"SELECT status FROM tasks WHERE id IN ({self._placeholders(ids)})",
                        ids,
                    ).fetchall()
                    if all(str(row["status"]) == ARCHIVED_STATUS for row in current_rows):
                        # A repeated worker pass (or an operator retry) is a
                        # successful no-op. Do not append duplicate archive
                        # events or mutate the original terminal timestamps.
                        conn.commit()
                        return True
                    now = int(time.time())
                    placeholders = self._placeholders(ids)
                    conn.execute(
                        f"UPDATE tasks SET status='archived', claim_lock=NULL, "
                        f"claim_expires=NULL, worker_pid=NULL WHERE id IN ({placeholders})",
                        ids,
                    )
                    for task_id in ids:
                        conn.execute(
                            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                            (task_id, "archived", json.dumps({"retention": "archive", "root_id": root_id}), now),
                        )
                    conn.commit()
                    return True
                except Exception:
                    conn.rollback()
                    raise

    def purge_workflow(self, root_id: str, task_ids: Sequence[str]) -> bool:
        ids = tuple(dict.fromkeys((root_id, *task_ids)))
        with workflow_mutation_lock(root_id, environment=self.environment):  # noqa: SIM117 - keep the mutation lock outermost.
            with closing(self._connect()) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._validate_ids(conn, ids, status=ARCHIVED_STATUS):
                        conn.rollback()
                        return False
                    placeholders = self._placeholders(ids)
                    outside_link = conn.execute(
                        f"SELECT 1 FROM task_links WHERE (parent_id IN ({placeholders}) "
                        f"OR child_id IN ({placeholders})) AND NOT "
                        f"(parent_id IN ({placeholders}) AND child_id IN ({placeholders})) LIMIT 1",
                        (*ids, *ids, *ids, *ids),
                    ).fetchone()
                    if outside_link is not None:
                        conn.rollback()
                        return False
                    for table in (
                        "task_attachments",
                        "kanban_notify_subs",
                        "task_comments",
                        "task_events",
                        "task_runs",
                        "task_links",
                    ):
                        if table == "task_links":
                            conn.execute(
                                f"DELETE FROM task_links WHERE parent_id IN ({placeholders}) "
                                f"AND child_id IN ({placeholders})",
                                (*ids, *ids),
                            )
                        else:
                            conn.execute(
                                f"DELETE FROM {table} WHERE task_id IN ({placeholders})",
                                ids,
                            )
                    deleted = conn.execute(
                        f"DELETE FROM tasks WHERE id IN ({placeholders})", ids
                    ).rowcount
                    if deleted != len(ids):
                        conn.rollback()
                        return False
                    conn.commit()
                    return True
                except Exception:
                    conn.rollback()
                    raise


class FilesystemArtifactCleaner:
    """Remove only task-scoped Kanban artifacts after a DB purge commits.

    Hermes stores these artifacts outside SQLite. The cleaner intentionally
    knows only the three task-scoped roots and refuses path traversal or
    symlink escapes. It never scans or removes an entire archive directory.
    """

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        env = dict(environment or os.environ)
        configured = env.get("HERMES_KANBAN_HOME", "").strip()
        self.kanban_home = Path(configured or (Path.home() / ".hermes" / "shared-kanban")).expanduser()
        self.artifact_root = (self.kanban_home / "kanban").resolve()
        self.logs_root = (self.artifact_root / "logs").resolve()
        self.workspaces_root = (self.artifact_root / "workspaces").resolve()
        self.attachments_root = (self.artifact_root / "attachments").resolve()

    @staticmethod
    def _safe_task_id(task_id: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", str(task_id)))

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    def _remove(self, path: Path, root: Path) -> tuple[int, int]:
        if not self._inside(path, root) or not path.exists() and not path.is_symlink():
            return 0, 0
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            return 1, 0
        except OSError as exc:
            LOG.warning("kanban-retention-artifact-cleanup-failed path=%s error=%s", path.name, type(exc).__name__)
            return 0, 1

    def cleanup(self, task_ids: Sequence[str]) -> tuple[int, int]:
        removed = 0
        skipped = 0
        for task_id in dict.fromkeys(str(value) for value in task_ids):
            if not self._safe_task_id(task_id):
                skipped += 1
                continue
            for path, root in (
                (self.logs_root / f"{task_id}.log", self.logs_root),
                (self.workspaces_root / task_id, self.workspaces_root),
                (self.attachments_root / task_id, self.attachments_root),
            ):
                count, failures = self._remove(path, root)
                removed += count
                skipped += failures
        return removed, skipped


class RetentionWorker:
    def __init__(
        self,
        *,
        maintenance: KanbanMaintenance,
        audit: AuditStore,
        environment: Mapping[str, str] | None = None,
        workflow_loader: Callable[..., Workflow] = load_workflow,
        row_lister: Callable[..., list[dict[str, Any]]] = list_tasks,
        delivery_reader: DiscordLedgerReader | None = None,
        clock: Callable[[], float] = time.time,
        dry_run: bool = False,
        allow_purge: bool = True,
        max_archive_roots: int | None = None,
        root_workers: int | None = None,
        artifact_cleanup: Callable[[Sequence[str]], tuple[int, int]] | None = None,
    ) -> None:
        self.maintenance = maintenance
        self.audit = audit
        self.environment = dict(environment or os.environ)
        self.workflow_loader = workflow_loader
        self.row_lister = row_lister
        self.delivery_reader = delivery_reader or DiscordLedgerReader(self.environment)
        self.clock = clock
        self.dry_run = dry_run
        self.allow_purge = allow_purge
        self.artifact_cleanup = artifact_cleanup or FilesystemArtifactCleaner(self.environment).cleanup
        if max_archive_roots is not None and max_archive_roots <= 0:
            raise ValueError("max_archive_roots must be positive")
        self.max_archive_roots = max_archive_roots
        configured_root_workers = (
            root_workers
            if root_workers is not None
            else self.environment.get("KANBAN_RETENTION_ROOT_WORKERS", "2")
        )
        try:
            configured_root_workers = int(configured_root_workers)
        except (TypeError, ValueError):
            configured_root_workers = 2
        self.root_workers = max(1, min(configured_root_workers, 4))

    def _load(
        self,
        root_id: str,
        *,
        include_archived: bool,
        listed_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> Workflow:
        if listed_rows is not None:
            row = next(
                (
                    item
                    for item in listed_rows
                    if str(item.get("id") or item.get("task_id") or "") == root_id
                    and _standalone_candidate(item)
                    and not bool(
                        getattr(self.maintenance, "has_task_links", lambda _task_id: False)(root_id)
                    )
                ),
                None,
            )
            if row is not None and not _has_marker_descendant(listed_rows, root_id):
                # A number of old workflows have no task_links but do have
                # marker-owned descendants. Treating their root as a
                # standalone card makes the later atomic-scope CAS fail and
                # leaves the graph immortal. Only take the fast singleton
                # path after proving no marker descendant exists in the
                # authoritative board snapshot.
                payload = dict(row)
                return Workflow(
                    root_task_id=root_id,
                    nodes=(WorkflowNode.from_hermes(payload),),
                    root_payload=payload,
                )
        kwargs: dict[str, Any] = {"include_archived": include_archived}
        try:
            parameters = signature(self.workflow_loader).parameters.values()
            accepts_listed_rows = any(
                parameter.name == "listed_rows"
                or parameter.kind is Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_listed_rows = True
        if accepts_listed_rows:
            kwargs["listed_rows"] = listed_rows
        return self.workflow_loader(root_id, **kwargs)

    def _inspect_active_root(
        self,
        root_id: str,
        *,
        active_rows: Sequence[Mapping[str, Any]],
        now: int,
    ) -> tuple[str, Workflow | None, RetentionDecision | None, str | None]:
        """Reconstruct and evaluate one root without mutating the board."""

        try:
            workflow = self._load(
                root_id,
                # A legacy pass can leave a terminal root with one or more
                # already-archived descendants.  Include those descendants
                # so the next pass can repair the graph atomically instead of
                # treating the root as an incomplete workflow.
                include_archived=True,
                listed_rows=active_rows,
            )
            decision = evaluate_workflow(
                workflow,
                now=now,
                delivery=self.delivery_reader.state(workflow),
            )
            return root_id, workflow, decision, None
        except (KanbanTaskNotFound, KanbanUnavailable, RetentionError, sqlite3.Error) as exc:
            return root_id, None, None, f"maintenance_error:{type(exc).__name__}"

    def _inspect_active_roots(
        self,
        root_ids: Sequence[str],
        *,
        active_rows: Sequence[Mapping[str, Any]],
        now: int,
    ) -> Iterable[tuple[str, Workflow | None, RetentionDecision | None, str | None]]:
        """Inspect roots in a small pool while keeping workflow graphs bounded."""

        if not root_ids:
            return
        workers = min(self.root_workers, len(root_ids))
        if workers == 1:
            for root_id in root_ids:
                yield self._inspect_active_root(root_id, active_rows=active_rows, now=now)
            return

        # Keep only ``workers`` futures in flight. Replenishing as soon as a
        # root completes avoids head-of-line blocking when one Hermes CLI call
        # is slow, while still bounding reconstructed workflow graphs and
        # subprocess memory.
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="kanban-retention",
        ) as pool:
            root_iter = iter(root_ids)
            futures = {
                pool.submit(
                    self._inspect_active_root,
                    root_id,
                    active_rows=active_rows,
                    now=now,
                )
                for root_id in [next(root_iter, None) for _ in range(workers)]
                if root_id is not None
            }
            while futures:
                done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.remove(future)
                    yield future.result()
                    next_root = next(root_iter, None)
                    if next_root is not None:
                        futures.add(
                            pool.submit(
                                self._inspect_active_root,
                                next_root,
                                active_rows=active_rows,
                                now=now,
                            )
                        )

    def _purge_archived_workflows(
        self,
        purge_rows: Sequence[Any],
        *,
        archived_rows: Sequence[Mapping[str, Any]],
        now: int,
        skipped: list[tuple[str, str]],
    ) -> tuple[int, int, int, float]:
        """Purge due workflows before the expensive active-board scan."""

        cleanup_started = time.perf_counter()
        purged_count = 0
        artifact_removed_count = 0
        artifact_cleanup_skipped_count = 0
        for row in purge_rows:
            root_id = str(row["root_id"])
            try:
                # Audit can lag a committed purge if the process exits after
                # deleting the board graph. Resolve that idempotent case with
                # the local existence check instead of invoking the slow CLI
                # fallback for a root that is already gone.
                if not self.maintenance.workflow_exists(root_id):
                    self.audit.mark_purged(root_id, purged_at=now)
                    purged_count += 1
                    continue
                # Do not trust the audit row's old graph membership. The
                # archived board is reconstructed again before purge.
                workflow = self._load(
                    root_id,
                    include_archived=True,
                    listed_rows=archived_rows,
                )
                if any(node.status != ARCHIVED_STATUS for node in workflow.nodes):
                    skipped.append((root_id, "purge_not_fully_archived"))
                    continue
                # Explicit success check required by policy: a purge cannot
                # proceed merely because an earlier INSERT was attempted.
                existing_audit = self.audit.get(root_id)
                if existing_audit is None:
                    skipped.append((root_id, "audit_summary_missing"))
                    continue
                # Legacy root repair or a late detached task can make the
                # authoritative archived graph broader than the capsule saved
                # during archive. Refresh the metadata-only capsule before
                # deleting any detail so every purged task remains represented
                # in the durable QA/HR evidence boundary.
                self.audit.save_archive(
                    build_audit_metadata(
                        workflow,
                        DeliveryState(
                            "not_required",
                            message_id=str(existing_audit["discord_message_id"] or "") or None,
                            thread_id=str(existing_audit["discord_thread_id"] or "") or None,
                        ),
                    ),
                    archived_at=int(existing_audit["archived_at"]),
                )
                task_ids = tuple(node.task_id for node in workflow.nodes)
                if self.maintenance.purge_workflow(
                    root_id,
                    [node.task_id for node in workflow.nodes if node.task_id != root_id],
                ):
                    removed, skipped_artifacts = self.artifact_cleanup(task_ids)
                    artifact_removed_count += removed
                    artifact_cleanup_skipped_count += skipped_artifacts
                    self.audit.mark_purged(root_id, purged_at=now)
                    purged_count += 1
                else:
                    skipped.append((root_id, "purge_cas_failed"))
            except KanbanTaskNotFound:
                # A previous purge may have committed the board deletion and
                # died before marking the audit row. Only a missing root is an
                # idempotent completion: a missing/stale descendant must not
                # falsely mark a still-existing workflow as purged.
                if (
                    self.audit.get(root_id) is not None
                    and not self.maintenance.workflow_exists(root_id)
                ):
                    self.audit.mark_purged(root_id, purged_at=now)
                    purged_count += 1
                else:
                    skipped.append((root_id, "purge_graph_missing_root_still_exists"))
            except (KanbanUnavailable, RetentionError, sqlite3.Error) as exc:
                skipped.append((root_id, f"purge_error:{type(exc).__name__}"))
        return (
            purged_count,
            artifact_removed_count,
            artifact_cleanup_skipped_count,
            (time.perf_counter() - cleanup_started) * 1000,
        )

    def run_once(self) -> RetentionRun:
        now = int(self.clock())
        rows_started = time.perf_counter()
        active_rows = self.row_lister(include_archived=False)
        list_ms = (time.perf_counter() - rows_started) * 1000
        recovery_started = time.perf_counter()
        # Read the archived view once per maintenance pass. Besides avoiding a
        # second list call for purge, this lets a deployment safely adopt rows
        # archived by the legacy per-task CLI: they receive a minimal audit row
        # before they can become purge candidates.
        archived_rows = self.row_lister(include_archived=True)
        # Hermes' archived list can briefly retain rows whose DB graph was
        # purged in an earlier pass. Remove those stale projections in one
        # local query so parent traversal never falls back to a guaranteed-
        # missing CLI ``show`` call.
        listed_task_ids = tuple(
            str(row.get("id") or row.get("task_id") or "")
            for row in (*active_rows, *archived_rows)
        )
        existing_task_ids = getattr(
            self.maintenance,
            "existing_task_ids",
            lambda task_ids: {task_id for task_id in task_ids if task_id},
        )(listed_task_ids)
        active_rows = [
            row
            for row in active_rows
            if str(row.get("id") or row.get("task_id") or "") in existing_task_ids
        ]
        archived_rows = [
            row
            for row in archived_rows
            if str(row.get("id") or row.get("task_id") or "") in existing_task_ids
        ]
        archived_root_ids = tuple(
            dict.fromkeys(
                str(row.get("id") or row.get("task_id"))
                for row in archived_rows
                if (
                    _root_candidate(row)
                    or (
                        _standalone_candidate(row)
                        and not bool(
                            getattr(
                                self.maintenance,
                                "has_task_links",
                                lambda _task_id: False,
                            )(
                                str(row.get("id") or row.get("task_id") or "")
                            )
                        )
                    )
                )
                and str(row.get("status") or "").casefold() == ARCHIVED_STATUS
            )
        )
        linked_root_ids = getattr(self.maintenance, "root_candidate_ids", lambda _task_ids: set())(
            tuple(str(row.get("id") or row.get("task_id") or "") for row in active_rows)
        )
        linked_task_ids = getattr(self.maintenance, "linked_task_ids", lambda _task_ids: set())(
            tuple(str(row.get("id") or row.get("task_id") or "") for row in active_rows)
        )
        root_ids = _archive_scan_root_ids(
            active_rows,
            linked_root_ids=linked_root_ids,
            linked_task_ids=linked_task_ids,
            now=now,
        )
        reconstruction_started = time.perf_counter()
        skipped: list[tuple[str, str]] = []
        blocked_reasons: Counter[str] = Counter()
        capsule_backfilled_count = 0
        archived_count = 0
        # Keep only the bounded mutation batch in memory. Holding every
        # reconstructed workflow here retains full event/run graphs until the
        # pass ends and can exceed the maintenance container's memory limit on
        # a historical board.
        eligible_root_ids_list: list[str] = []
        archive_records: list[tuple[str, Workflow, RetentionDecision]] = []
        eligible_task_count = 0
        for root_id in archived_root_ids:
            if self.dry_run:
                continue
            existing_audit = self.audit.get(root_id)
            if existing_audit is not None:
                if str(existing_audit["qa_hr_capsule_json"] or "{}").strip() not in {"", "{}"}:
                    continue
                try:
                    workflow = self._load(
                        root_id,
                        include_archived=True,
                        listed_rows=archived_rows,
                    )
                    if self.audit.update_capsule_if_missing(
                        root_id,
                        build_qa_hr_capsule(workflow),
                    ):
                        capsule_backfilled_count += 1
                except (KanbanTaskNotFound, KanbanUnavailable, RetentionError, sqlite3.Error) as exc:
                    skipped.append((root_id, f"capsule_backfill_error:{type(exc).__name__}"))
                continue
            try:
                workflow = self._load(
                    root_id,
                    include_archived=True,
                    listed_rows=archived_rows,
                )
                fully_archived = all(
                    node.status == ARCHIVED_STATUS for node in workflow.nodes
                )
                delivery = self.delivery_reader.state(workflow)
                workflow, decision = _evaluate_legacy_archived_workflow(
                    workflow,
                    now=now,
                    delivery=delivery,
                )
                if not decision.eligible:
                    skipped.append((root_id, f"legacy_{decision.reason}"))
                    continue
                metadata = build_audit_metadata(
                    workflow,
                    decision.delivery or DeliveryState("not_required"),
                )
                self.audit.save_archive(
                    metadata,
                    archived_at=_archived_at(workflow, fallback=now),
                )
                if not fully_archived and not self.maintenance.archive_workflow(
                    root_id,
                    [node.task_id for node in workflow.nodes if node.task_id != root_id],
                ):
                    skipped.append((root_id, "legacy_archive_cas_failed"))
            except (KanbanTaskNotFound, KanbanUnavailable, RetentionError, sqlite3.Error) as exc:
                skipped.append((root_id, f"legacy_audit_error:{type(exc).__name__}"))

        purge_rows = () if self.dry_run or not self.allow_purge else self.audit.purge_candidates(now=now)
        recovery_ms = (time.perf_counter() - recovery_started) * 1000
        (
            purged_count,
            artifact_removed_count,
            artifact_cleanup_skipped_count,
            cleanup_ms,
        ) = self._purge_archived_workflows(
            purge_rows,
            archived_rows=archived_rows,
            now=now,
            skipped=skipped,
        )
        if purge_rows:
            LOG.info(
                "kanban-retention-purge due=%d purged=%d cleanup_ms=%.2f",
                len(purge_rows),
                purged_count,
                cleanup_ms,
            )
        # Include archived descendants in reconstruction. Legacy archive
        # passes could partially archive marker-only graphs before root-atomic
        # retention existed; using only the active list would hide those
        # descendants and make the compare-and-swap fail forever.
        reconstruction_rows = (*active_rows, *archived_rows)
        for root_id, workflow, decision, error in self._inspect_active_roots(
            root_ids,
            active_rows=reconstruction_rows,
            now=now,
        ):
            if error is not None:
                skipped.append((root_id, error))
                blocked_reasons["UNKNOWN/LEGACY"] += 1
                continue
            assert workflow is not None and decision is not None
            if not decision.eligible:
                skipped.append((root_id, decision.reason))
                blocked_reasons[retention_block_category(decision.reason)] += 1
                continue
            eligible_root_ids_list.append(root_id)
            eligible_task_count += len(workflow.nodes)
            if self.max_archive_roots is None or len(archive_records) < self.max_archive_roots:
                archive_records.append((root_id, workflow, decision))
            # Production mutations are deliberately bounded. Once this pass
            # has a full safe batch, continuing to hydrate every other root
            # only delays archive/purge by tens of minutes. Dry-run keeps the
            # exhaustive scan so it can still report the whole board.
            if (
                not self.dry_run
                and self.max_archive_roots is not None
                and len(archive_records) >= self.max_archive_roots
            ):
                break

        eligible_root_ids = tuple(eligible_root_ids_list)
        would_archive_root_ids = tuple(root_id for root_id, _workflow, _decision in archive_records)

        if not self.dry_run:
            for root_id, workflow, decision in archive_records:
                try:
                    metadata = build_audit_metadata(
                        workflow,
                        decision.delivery or DeliveryState("not_required"),
                    )
                    # The audit row is durable before the board mutation. If
                    # the process dies after this point, the next pass
                    # rechecks the authoritative board and can safely finish.
                    self.audit.save_archive(metadata, archived_at=now)
                    if self.maintenance.archive_workflow(
                        root_id,
                        [node.task_id for node in workflow.nodes if node.task_id != root_id],
                    ):
                        archived_count += 1
                    else:
                        skipped.append((root_id, "archive_cas_failed"))
                except (KanbanTaskNotFound, KanbanUnavailable, RetentionError, sqlite3.Error) as exc:
                    skipped.append((root_id, f"maintenance_error:{type(exc).__name__}"))
        reconstruction_ms = (time.perf_counter() - reconstruction_started) * 1000

        return RetentionRun(
            active_task_count=len(active_rows),
            active_root_count=len(root_ids),
            archived_root_count=len(purge_rows),
            archived_count=archived_count,
            purged_count=purged_count,
            list_ms=list_ms,
            reconstruction_ms=reconstruction_ms,
            recovery_ms=recovery_ms,
            cleanup_ms=cleanup_ms,
            skipped=tuple(skipped),
            eligible_root_ids=eligible_root_ids,
            would_archive_root_ids=would_archive_root_ids,
            eligible_task_count=eligible_task_count,
            would_archive_task_count=sum(len(workflow.nodes) for _, workflow, _ in archive_records),
            blocked_reason_histogram=tuple(sorted(blocked_reasons.items())),
            artifact_removed_count=artifact_removed_count,
            artifact_cleanup_skipped_count=artifact_cleanup_skipped_count,
            capsule_backfilled_count=capsule_backfilled_count,
        )


def default_audit_path(environment: Mapping[str, str] | None = None) -> Path:
    env = environment or os.environ
    configured = env.get("HERMES_KANBAN_RETENTION_AUDIT_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return kanban_db_path(dict(env)).with_name("retention-audit.db")


def _build_worker(
    environment: Mapping[str, str] | None = None,
    *,
    dry_run: bool = False,
    allow_purge: bool = True,
    max_archive_roots: int | None = None,
) -> RetentionWorker:
    env = dict(environment or os.environ)
    # Reuse the supervisor's Hermes-native read-only ``show`` transport.  It
    # serializes through Hermes' own kanban_db helpers and falls back to the
    # CLI if unavailable, while avoiding one Python subprocess per workflow
    # node during retention scans.
    kanban_client = HermesKanbanClient(environment=env)

    def load_retention_workflow(root_id: str, **kwargs: Any) -> Workflow:
        def fetch(task_id: str) -> dict[str, Any]:
            try:
                return kanban_client.show(task_id)
            except HermesKanbanCommandError as exc:
                raise KanbanUnavailable(
                    "Hermes workflow read is unavailable during retention"
                ) from exc

        return load_workflow(
            root_id,
            fetch=fetch,
            known_root=True,
            **kwargs,
        )

    def list_retention_tasks(
        *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        try:
            return list(kanban_client.list_tasks(include_archived=include_archived))
        except HermesKanbanCommandError as exc:
            raise KanbanUnavailable(
                "Hermes Kanban list is unavailable during retention"
            ) from exc

    return RetentionWorker(
        maintenance=SQLiteKanbanMaintenance(env),
        audit=AuditStore(default_audit_path(env)),
        environment=env,
        workflow_loader=load_retention_workflow,
        row_lister=list_retention_tasks,
        dry_run=dry_run,
        allow_purge=allow_purge,
        max_archive_roots=max_archive_roots,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Root-scoped Hermes Kanban retention worker")
    parser.add_argument("--interval", type=float, default=float(os.getenv("KANBAN_RETENTION_INTERVAL_SECONDS", "900")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="evaluate only; perform no audit/archive/purge mutation")
    parser.add_argument("--archive-only", action="store_true", help="disable production purge for this run")
    parser.add_argument("--max-archive-roots", type=int, default=None, help="bound archive mutations to this many eligible roots")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("KANBAN_RETENTION_LOG_LEVEL", "INFO"), format="%(message)s")
    stop = False

    def shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    worker = _build_worker(
        dry_run=args.dry_run,
        allow_purge=not args.archive_only,
        max_archive_roots=args.max_archive_roots,
    )
    while not stop:
        started = time.perf_counter()
        try:
            result = worker.run_once()
            LOG.info(
                "kanban-retention active_tasks=%d active_roots=%d eligible_roots=%d "
                "would_archive_roots=%d archived=%d purged=%d "
                "artifacts_removed=%d artifact_cleanup_skipped=%d "
                "capsules_backfilled=%d "
                "list_ms=%.2f reconstruction_ms=%.2f recovery_ms=%.2f cleanup_ms=%.2f skipped=%d",
                result.active_task_count,
                result.active_root_count,
                len(result.eligible_root_ids),
                len(result.would_archive_root_ids),
                result.archived_count,
                result.purged_count,
                result.artifact_removed_count,
                result.artifact_cleanup_skipped_count,
                result.capsule_backfilled_count,
                result.list_ms,
                result.reconstruction_ms,
                result.recovery_ms,
                result.cleanup_ms,
                len(result.skipped),
            )
            if args.dry_run:
                LOG.info(
                    "kanban-retention-preview %s",
                    json.dumps(
                        {
                            "eligible_root_count": len(result.eligible_root_ids),
                            "eligible_task_count": result.eligible_task_count,
                            "would_archive_root_ids": list(result.would_archive_root_ids),
                            "would_archive_task_count": result.would_archive_task_count,
                            "blocked_root_count": sum(count for _, count in result.blocked_reason_histogram),
                            "block_reason_histogram": dict(result.blocked_reason_histogram),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
        except Exception:
            LOG.exception("kanban-retention-run-failed")
        if args.once:
            break
        elapsed = time.perf_counter() - started
        stop = stop or args.interval <= 0
        if not stop:
            time.sleep(max(0.1, args.interval - elapsed))
    return 0


__all__ = [
    "ACTIVE_AGE_SECONDS",
    "AUDIT_CAPSULE_SCHEMA_VERSION",
    "AuditMetadata",
    "AuditStore",
    "DeliveryState",
    "DiscordLedgerReader",
    "FilesystemArtifactCleaner",
    "RetentionDecision",
    "RetentionRun",
    "RetentionWorker",
    "SQLiteKanbanMaintenance",
    "build_audit_metadata",
    "build_qa_hr_capsule",
    "default_audit_path",
    "evaluate_workflow",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
