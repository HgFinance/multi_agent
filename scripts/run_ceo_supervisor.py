#!/usr/bin/env python3
"""Run the CEO closed-loop supervisor beside the standalone Hermes daemon."""

from __future__ import annotations

import argparse
import ast
import hashlib
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    HermesKanbanClient,
    HermesKanbanCommandError,
    SupervisorWorkflowError,
)
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.discord_delivery import DiscordFinalDelivery

WATCH_LINE = re.compile(
    r"^\[(?P<timestamp>[^]]+)\]\s+(?P<task_id>\S+)\s+"
    r"(?P<kind>\S+)\s+\(@(?P<assignee>[^)]*)\)(?P<payload>.*)$"
)


class WatchOutputError(RuntimeError):
    """The Hermes watch output no longer matches its supported contract."""


class WatchProcessError(RuntimeError):
    """The Hermes watch subprocess ended without an intentional shutdown."""


class GracefulShutdown(Exception):
    """Internal signal used to distinguish normal container shutdown."""


def parse_watch_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped or stripped == "Watching kanban events. Ctrl-C to stop." or stripped == "(stopped)":
        return None
    match = WATCH_LINE.match(stripped)
    if match is None:
        raise WatchOutputError(f"malformed hermes kanban watch line: {stripped[:240]!r}")
    payload_text = match.group("payload").strip()
    payload: object = {}
    if payload_text:
        try:
            payload = ast.literal_eval(payload_text)
        except (SyntaxError, ValueError) as exc:
            raise WatchOutputError("watch payload is not a Python-literal dict") from exc
        if not isinstance(payload, dict):
            raise WatchOutputError("watch payload is not a dict")
    event_id = hashlib.sha256(
        f"{match.group('timestamp')}|{match.group('task_id')}|{match.group('kind')}|{payload_text}".encode()
    ).hexdigest()[:24]
    event: dict[str, object] = {
        "task_id": match.group("task_id"),
        "kind": match.group("kind"),
        "assignee": match.group("assignee") or None,
        "event_id": event_id,
    }
    try:
        created_at = datetime.fromisoformat(match.group("timestamp"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        event["_event_created_ms"] = int(created_at.timestamp() * 1000)
    except ValueError:
        event["_event_created_ms"] = 0
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in {
                "task_id",
                "kind",
                "assignee",
                "event_id",
                "_event_created_ms",
                "_event_consumed_ms",
            }:
                event[key] = value
    return event


def watch_events(
    *,
    executable: str,
    interval: float,
    environment: dict[str, str],
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Iterator[dict[str, object]]:
    try:
        process = popen_factory(
            [
                executable,
                "kanban",
                "watch",
                "--kinds",
                "claimed,spawned,started,running,completed,blocked,gave_up,crashed,timed_out,spawn_failed",
                "--interval",
                str(interval),
            ],
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchProcessError("could not start hermes kanban watch") from exc
    if process.stdout is None:
        raise WatchProcessError("hermes kanban watch did not provide stdout")
    saw_stopped = False
    try:
        for line in process.stdout:
            if line.strip() == "(stopped)":
                saw_stopped = True
                continue
            event = parse_watch_line(line)
            if event is not None:
                yield event
        returncode = process.wait()
        if returncode != 0:
            raise WatchProcessError(f"hermes kanban watch exited with code {returncode}")
        if not saw_stopped:
            raise WatchProcessError("hermes kanban watch reached unexpected EOF")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class SupervisorEventQueue:
    """Keep watch stdout draining while prioritizing durable reconciliation.

    Active lifecycle events are best-effort Discord progress hints. Terminal
    events are durable reconciliation boundaries and receive queue priority.
    A queued active event for a task that has since produced a terminal event
    is discarded as obsolete; the terminal event itself is never discarded by
    this queue.
    """

    _ACTIVE_KINDS = frozenset({"claimed", "spawned", "started", "running"})
    _TERMINAL_KINDS = frozenset(
        {
            "done",
            "completed",
            "archived",
            "blocked",
            "gave_up",
            "crashed",
            "timed_out",
            "spawn_failed",
            "failed",
        }
    )

    def __init__(self, service: CeoSupervisorService, *, workers: int = 2) -> None:
        self.service = service
        self.worker_count = max(1, int(workers))
        self._queue: queue.PriorityQueue[
            tuple[int, int, str, dict[str, object] | None]
        ] = queue.PriorityQueue()
        self._sequence = 0
        self._pending: set[str] = set()
        self._obsolete_active: set[str] = set()
        self._terminalized_tasks: set[str] = set()
        self._pending_lock = threading.Lock()
        self._metrics: dict[str, dict[str, list[int]]] = {}
        self._max_queue_depth = 0
        self._threads = tuple(
            threading.Thread(
                target=self._run_worker,
                name=f"ceo-event-worker-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count)
        )
        for thread in self._threads:
            thread.start()

    @classmethod
    def _kind(cls, event: dict[str, object]) -> str:
        return str(event.get("kind") or event.get("event_type") or "").casefold()

    @classmethod
    def _pending_key(cls, event: dict[str, object]) -> str:
        task_id = str(event.get("task_id") or "")
        kind = cls._kind(event)
        if kind in cls._ACTIVE_KINDS:
            return f"active:{task_id}"
        if kind in {"done", "completed", "archived"}:
            return f"terminal:{task_id}:completed"
        if kind in {
            "blocked",
            "gave_up",
            "crashed",
            "timed_out",
            "spawn_failed",
            "failed",
        }:
            return f"terminal:{task_id}:{kind}"
        return f"event:{event.get('event_id') or task_id + ':' + kind}"

    @classmethod
    def _priority(cls, event: dict[str, object]) -> int:
        kind = cls._kind(event)
        if kind in cls._TERMINAL_KINDS:
            return 0
        if kind in cls._ACTIVE_KINDS:
            return 10
        return 5

    @staticmethod
    def _percentile(values: list[int], fraction: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        rank = max(1, int(len(ordered) * fraction + 0.999999))
        return ordered[min(len(ordered) - 1, rank - 1)]

    def metrics_snapshot(self) -> dict[str, object]:
        """Return event-kind latency summaries for before/after comparisons."""

        with self._pending_lock:
            return {
                "by_kind": {
                    kind: {
                        "count": len(values["queue_wait_ms"]),
                        "queue_wait_p50_ms": self._percentile(
                            values["queue_wait_ms"], 0.50
                        ),
                        "queue_wait_p95_ms": self._percentile(
                            values["queue_wait_ms"], 0.95
                        ),
                        "handler_duration_p50_ms": self._percentile(
                            values["handler_duration_ms"], 0.50
                        ),
                        "handler_duration_p95_ms": self._percentile(
                            values["handler_duration_ms"], 0.95
                        ),
                    }
                    for kind, values in sorted(self._metrics.items())
                },
                "max_queue_depth": self._max_queue_depth,
            }

    def submit(self, event: dict[str, object]) -> bool:
        queued_event = dict(event)
        queued_event["_event_consumed_ms"] = time.time_ns() // 1_000_000
        pending_key = self._pending_key(queued_event)
        kind = self._kind(queued_event)
        task_id = str(queued_event.get("task_id") or "")
        with self._pending_lock:
            if kind in self._ACTIVE_KINDS and task_id in self._terminalized_tasks:
                logging.info(
                    "supervisor-event-queue-obsolete task=%s kind=%s",
                    task_id,
                    kind,
                )
                return False
            if pending_key in self._pending:
                logging.info(
                    "supervisor-event-queue-coalesced task=%s kind=%s "
                    "queue_depth=%d pending=%d",
                    queued_event.get("task_id") or "",
                    queued_event.get("kind") or "",
                    self._queue.qsize(),
                    len(self._pending),
                )
                return False
            if kind in self._TERMINAL_KINDS:
                self._terminalized_tasks.add(task_id)
                active_key = f"active:{task_id}"
                if active_key in self._pending:
                    self._obsolete_active.add(active_key)
            self._pending.add(pending_key)
            self._sequence += 1
            sequence = self._sequence
            pending_count = len(self._pending)

        self._queue.put(
            (self._priority(queued_event), sequence, pending_key, queued_event)
        )
        with self._pending_lock:
            self._max_queue_depth = max(self._max_queue_depth, self._queue.qsize())
        logging.info(
            "supervisor-event-queued task=%s kind=%s queue_depth=%d pending=%d",
            queued_event.get("task_id") or "",
            queued_event.get("kind") or "",
            self._queue.qsize(),
            pending_count,
        )
        return True

    def _run_worker(self) -> None:
        while True:
            _priority, _sequence, pending_key, event = self._queue.get()
            if event is None:
                self._queue.task_done()
                return

            handler_started_ms = time.time_ns() // 1_000_000
            kind = self._kind(event)
            task_id = str(event.get("task_id") or "")
            with self._pending_lock:
                obsolete = pending_key in self._obsolete_active or (
                    kind in self._ACTIVE_KINDS and task_id in self._terminalized_tasks
                )
            try:
                if not obsolete:
                    decision = self.service.handle_terminal_event(event)
                    if decision is not None:
                        print(
                            f"ceo-supervisor action={decision.action.value} "
                            f"parent={decision.parent_task_id} reason={decision.reason}",
                            flush=True,
                        )
            except SupervisorWorkflowError as exc:
                print(
                    f"ceo-supervisor workflow-error={exc}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                # The durable Kanban state is replayed by recovery. Keep the
                # worker pool alive so one malformed projection cannot stop
                # event intake permanently.
                logging.exception(
                    "supervisor-event-worker-failed task=%s kind=%s",
                    event.get("task_id") or "",
                    event.get("kind") or "",
                )
            finally:
                handler_completed_ms = time.time_ns() // 1_000_000
                consumed_ms = int(event.get("_event_consumed_ms") or 0)
                with self._pending_lock:
                    self._pending.discard(pending_key)
                    self._obsolete_active.discard(pending_key)
                    if kind in self._TERMINAL_KINDS:
                        self._terminalized_tasks.add(task_id)
                    pending_count = len(self._pending)
                    values = self._metrics.setdefault(
                        kind or "unknown",
                        {"queue_wait_ms": [], "handler_duration_ms": []},
                    )
                    values["queue_wait_ms"].append(
                        handler_started_ms - consumed_ms
                        if consumed_ms > 0 and handler_started_ms >= consumed_ms
                        else -1
                    )
                    values["handler_duration_ms"].append(
                        handler_completed_ms - handler_started_ms
                    )
                self._queue.task_done()
                created_ms = int(event.get("_event_created_ms") or 0)
                logging.info(
                    "supervisor-event-loop-timing task=%s kind=%s obsolete=%s "
                    "event_created=%d event_consumed=%d handler_started=%d "
                    "loop_return=%d created_to_consumed_ms=%d "
                    "queue_wait_ms=%d handler_duration_ms=%d "
                    "queue_depth=%d pending=%d",
                    event.get("task_id") or "",
                    event.get("kind") or "",
                    obsolete,
                    created_ms,
                    consumed_ms,
                    handler_started_ms,
                    handler_completed_ms,
                    consumed_ms - created_ms
                    if created_ms > 0 and consumed_ms >= created_ms
                    else -1,
                    handler_started_ms - consumed_ms
                    if consumed_ms > 0 and handler_started_ms >= consumed_ms
                    else -1,
                    handler_completed_ms - handler_started_ms,
                    self._queue.qsize(),
                    pending_count,
                )

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
        for _thread in self._threads:
            with self._pending_lock:
                self._sequence += 1
                sequence = self._sequence
            self._queue.put((100, sequence, "", None))
        for thread in self._threads:
            thread.join(timeout=5)



# hgfinance-ready-plan-poll-v2
def run_ready_plan_reconciler(
    service: CeoSupervisorService,
    *,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """Poll for complete CEO-authored ready/running delegation plans."""

    poll_interval = max(float(interval), 0.25)

    while not stop_event.is_set():
        try:
            service.materialize_ready_primary_plans()
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor ready-plan-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        stop_event.wait(poll_interval)


# hgfinance-completed-synthesis-reconcile-v1
def run_completed_synthesis_reconciler(
    service: CeoSupervisorService,
    *,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """Poll for completed synthesis tasks whose watch event was missed."""

    poll_interval = max(float(interval), 0.25)

    while not stop_event.is_set():
        try:
            recovered = service.reconcile_completed_syntheses()
            for task_id in recovered:
                print(
                    f"ceo-supervisor completed-synthesis-reconciled task={task_id}",
                    flush=True,
                )
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor synthesis-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        stop_event.wait(poll_interval)


def run_recovery_reconciler(
    service: CeoSupervisorService,
    *,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """Run both board-scan recovery lanes without concurrent CLI scans.

    The watch stream remains the low-latency path. Recovery scans parse the
    entire Kanban board, so running two of them every watch tick can saturate
    the supervisor container as the board grows.
    """

    poll_interval = max(float(interval), 5.0)

    # Startup recovery is intentionally on this background lane. Running it
    # before opening the watch stream leaves a blind window proportional to a
    # full-board scan.
    reconcile_existing = getattr(service, "reconcile_existing_workflows", None)
    if callable(reconcile_existing) and not stop_event.is_set():
        try:
            for decision in reconcile_existing():
                print(
                    f"ceo-supervisor reconcile action={decision.action.value} "
                    f"parent={decision.parent_task_id} reason={decision.reason}",
                    flush=True,
                )
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                f"ceo-supervisor reconcile-error={exc}",
                file=sys.stderr,
                flush=True,
            )

    while not stop_event.is_set():
        try:
            service.materialize_ready_primary_plans()
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor ready-plan-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        try:
            recovered = service.reconcile_completed_syntheses()
            for task_id in recovered:
                print(
                    f"ceo-supervisor completed-synthesis-reconciled task={task_id}",
                    flush=True,
                )
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor synthesis-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        stop_event.wait(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--recovery-interval", type=float, default=None)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-wakeups", type=int, default=8)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def shutdown_handler(signum: int, _frame: Any) -> None:
        raise GracefulShutdown(f"signal {signum}")

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    environment = dict(os.environ)
    client = HermesKanbanClient(environment=environment)
    service = CeoSupervisorService(
        client,
        max_retries=args.max_retries,
        max_wakeups=args.max_wakeups,
        synthesis_projection=CeoNotionProjection(kanban_client=client),
        qa_projection=QaAuditProjection(kanban_client=client),
        discord_delivery=DiscordFinalDelivery(environment=environment),
    )
    event_queue = SupervisorEventQueue(
        service,
        workers=int(environment.get("CEO_SUPERVISOR_EVENT_WORKERS", "2")),
    )

    recovery_stop = threading.Event()
    recovery_interval = (
        args.recovery_interval
        if args.recovery_interval is not None
        else float(environment.get("CEO_SUPERVISOR_RECOVERY_INTERVAL_SECONDS", "15"))
    )
    recovery_thread = threading.Thread(
        target=run_recovery_reconciler,
        kwargs={
            "service": service,
            "interval": recovery_interval,
            "stop_event": recovery_stop,
        },
        name="ceo-recovery-reconciler",
        daemon=True,
    )
    recovery_thread.start()

    try:
        for event in watch_events(
            executable=client.executable,
            interval=args.interval,
            environment=environment,
        ):
            event_queue.submit(event)
    except GracefulShutdown as exc:
        recovery_stop.set()
        event_queue.close(drain=True)
        print(f"ceo-supervisor normal-shutdown={exc}", flush=True)
        return 0
    except (WatchOutputError, WatchProcessError) as exc:
        recovery_stop.set()
        event_queue.close(drain=True)
        print(f"ceo-supervisor fatal-watch-error={exc}", file=sys.stderr, flush=True)
        return 1
    recovery_stop.set()
    event_queue.close(drain=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
