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
                "claimed,spawned,completed,blocked,gave_up,crashed,timed_out,spawn_failed",
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
    """Keep watch stdout draining while bounded workers run heavy reconcile."""

    def __init__(self, service: CeoSupervisorService, *, workers: int = 2) -> None:
        self.service = service
        self.worker_count = max(1, int(workers))
        self._queue: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
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

    @staticmethod
    def _pending_key(event: dict[str, object]) -> str:
        task_id = str(event.get("task_id") or "")
        kind = str(event.get("kind") or "").casefold()
        if kind in {"done", "completed", "archived"}:
            return f"terminal:{task_id}:completed"
        return f"event:{event.get('event_id') or task_id + ':' + kind}"

    def submit(self, event: dict[str, object]) -> bool:
        queued_event = dict(event)
        queued_event["_event_consumed_ms"] = time.time_ns() // 1_000_000
        pending_key = self._pending_key(queued_event)
        with self._pending_lock:
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
            self._pending.add(pending_key)
            pending_count = len(self._pending)

        self._queue.put((pending_key, queued_event))
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
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return

            pending_key, event = item
            handler_started_ms = time.time_ns() // 1_000_000
            try:
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
            finally:
                handler_completed_ms = time.time_ns() // 1_000_000
                with self._pending_lock:
                    self._pending.discard(pending_key)
                    pending_count = len(self._pending)
                self._queue.task_done()
                consumed_ms = int(event.get("_event_consumed_ms") or 0)
                created_ms = int(event.get("_event_created_ms") or 0)
                logging.info(
                    "supervisor-event-loop-timing task=%s kind=%s "
                    "event_created=%d event_consumed=%d handler_started=%d "
                    "loop_return=%d created_to_consumed_ms=%d "
                    "queue_wait_ms=%d handler_duration_ms=%d "
                    "queue_depth=%d pending=%d",
                    event.get("task_id") or "",
                    event.get("kind") or "",
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
            self._queue.put(None)
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
