#!/usr/bin/env python3
"""Run the CEO closed-loop supervisor beside the standalone Hermes daemon."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    HermesKanbanClient,
    HermesKanbanCommandError,
    SupervisorWorkflowError,
    cli_lane,
)
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.discord_delivery import DiscordFinalDelivery
from orchestration.kanban_root_index import RootScopedIndexUnavailable

WATCH_LINE = re.compile(
    r"^\[(?P<timestamp>[^]]+)\]\s+(?P<task_id>\S+)\s+"
    r"(?P<kind>\S+)\s+\(@(?P<assignee>[^)]*)\)(?P<payload>.*)$"
)
WATCH_KINDS = frozenset(
    {
        "claimed",
        "spawned",
        "started",
        "running",
        "completed",
        "blocked",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }
)
WATCH_KIND_ARGUMENT = (
    "claimed,spawned,started,running,completed,blocked,gave_up,crashed,timed_out,spawn_failed"
)
WATCH_EVENT_QUERY = (
    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
    "       t.assignee, t.tenant "
    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
    "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200"
)
SQLITE_WATCH_MAX_RETRIES = 3
SQLITE_WATCH_RETRY_BACKOFF_SECONDS = (0.10, 0.25, 0.50)
_WATCH_RESERVED_FIELDS = frozenset(
    {
        "task_id",
        "kind",
        "assignee",
        "event_id",
        "_kanban_event_row_id",
        "_event_persisted_ms",
        "_event_created_ms",
        "_event_detected_ms",
        "_event_emitted_ms",
        "_event_read_ms",
        "_event_enqueued_ms",
        "_event_consumed_ms",
    }
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
    if environment.get("CEO_SUPERVISOR_WATCH_SOURCE", "hermes").casefold() == "sqlite":
        yield from watch_events_sqlite(
            executable=executable,
            environment=environment,
            interval=interval,
            popen_factory=popen_factory,
        )
        return

    try:
        process = popen_factory(
            [
                executable,
                "kanban",
                "watch",
                "--kinds",
                WATCH_KIND_ARGUMENT,
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
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _kanban_db_uri(environment: dict[str, str]) -> str:
    configured = environment.get("HERMES_KANBAN_DB")
    if not configured:
        home = environment.get("HERMES_KANBAN_HOME")
        if home:
            configured = str(Path(home) / "kanban.db")
    if not configured:
        raise WatchProcessError("HERMES_KANBAN_DB is required for sqlite watch")
    # Read-only URI mode keeps the watch path from creating or mutating a DB.
    return f"file:{quote(str(Path(configured)), safe='/')}?mode=ro"


def _close_watch_connection(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        pass


def _open_sqlite_watch_connection(
    environment: dict[str, str],
    connect_factory: Callable[..., sqlite3.Connection],
) -> sqlite3.Connection:
    connection = connect_factory(
        _kanban_db_uri(environment),
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _watch_event_signature(event: Mapping[str, object]) -> str:
    """Build a redaction-free identity used only for CLI handoff deduplication."""

    payload = {
        str(key): value
        for key, value in event.items()
        if key not in _WATCH_RESERVED_FIELDS
    }
    created_ms = int(event.get("_event_created_ms") or 0)
    return json.dumps(
        {
            "task_id": str(event.get("task_id") or ""),
            "kind": str(event.get("kind") or ""),
            "assignee": str(event.get("assignee") or ""),
            "created_at": created_ms // 1000,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hermes_watch_command(
    executable: str,
    interval: float,
) -> list[str]:
    return [
        executable,
        "kanban",
        "watch",
        "--kinds",
        WATCH_KIND_ARGUMENT,
        "--interval",
        str(interval),
    ]


def _watch_events_cli_fallback(
    *,
    executable: str,
    interval: float,
    environment: dict[str, str],
    cursor: int,
    connect_factory: Callable[..., sqlite3.Connection],
    popen_factory: Callable[..., Any],
) -> Iterator[dict[str, object]]:
    """Continue through Hermes CLI only after a durable cursor handoff.

    Hermes' current ``kanban watch`` CLI has no ``--since-id`` option and seeds
    its own cursor at ``MAX(task_events.id)``.  Starting it blindly would skip
    rows created while SQLite was reconnecting.  We therefore catch up from
    the supervisor cursor first, retain signatures for that handoff window,
    and suppress the CLI's duplicate text for the same rows.  If the durable
    catch-up cannot be opened, no unsafe CLI fallback is attempted.
    """

    handoff_signatures: Counter[str] = Counter()
    handoff_cursor = cursor

    def catch_up() -> Iterator[dict[str, object]]:
        nonlocal handoff_cursor
        catchup_connection: sqlite3.Connection | None = None
        try:
            catchup_connection = _open_sqlite_watch_connection(
                environment,
                connect_factory,
            )
            while True:
                next_cursor, events = _read_sqlite_watch_batch(
                    catchup_connection,
                    handoff_cursor,
                )
                for event in events:
                    handoff_signatures[_watch_event_signature(event)] += 1
                    yield event
                if next_cursor == handoff_cursor:
                    break
                handoff_cursor = next_cursor
        finally:
            _close_watch_connection(catchup_connection)

    process: Any | None = None
    try:
        try:
            # Establish a durable handoff before starting the CLI.  Then
            # repeat it immediately after process startup to cover the race
            # between the first read and Hermes' MAX(id) cursor seed.  This
            # also prevents a large catch-up from filling the CLI stdout pipe.
            yield from catch_up()
            process = popen_factory(
                _hermes_watch_command(executable, interval),
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                env=environment,
            )
            if process.stdout is None:
                raise WatchProcessError(
                    "hermes kanban watch fallback did not provide stdout"
                )
            yield from catch_up()
        except (OSError, sqlite3.Error) as exc:
            raise WatchProcessError(
                "cannot establish durable cursor handoff for hermes watch fallback"
            ) from exc

        saw_stopped = False
        for line in process.stdout:
            if line.strip() == "(stopped)":
                saw_stopped = True
                continue
            event = parse_watch_line(line)
            if event is None:
                continue
            signature = _watch_event_signature(event)
            if handoff_signatures[signature] > 0:
                handoff_signatures[signature] -= 1
                continue
            yield event
        returncode = process.wait()
        if returncode != 0:
            raise WatchProcessError(
                f"hermes kanban watch fallback exited with code {returncode}"
            )
        if not saw_stopped:
            raise WatchProcessError(
                "hermes kanban watch fallback reached unexpected EOF"
            )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _read_sqlite_watch_batch(
    connection: sqlite3.Connection,
    cursor: int,
    *,
    detected_ms: int | None = None,
) -> tuple[int, tuple[dict[str, object], ...]]:
    """Read one durable event cursor page without introducing a new source of truth."""

    rows = connection.execute(WATCH_EVENT_QUERY, (cursor,)).fetchall()
    detected_ms = detected_ms or time.time_ns() // 1_000_000
    next_cursor = cursor
    events: list[dict[str, object]] = []
    for row in rows:
        row_id = int(row["id"])
        next_cursor = max(next_cursor, row_id)
        if str(row["kind"] or "") not in WATCH_KINDS:
            continue
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except (TypeError, ValueError, json.JSONDecodeError):
            # A malformed payload must not kill the durable event consumer.
            payload = None
        emitted_ms = time.time_ns() // 1_000_000
        persisted_ms = int(row["created_at"] or 0) * 1000
        event: dict[str, object] = {
            "task_id": str(row["task_id"] or ""),
            "kind": str(row["kind"] or ""),
            "assignee": row["assignee"] or None,
            # The SQLite row id is the durable event identity.  It is stable
            # across reconnect/restart and is stronger than hashing minute-
            # precision Hermes watch text.
            "event_id": f"kanban:{row_id}",
            "_kanban_event_row_id": row_id,
            "_event_persisted_ms": persisted_ms,
            "_event_created_ms": persisted_ms,
            "_event_detected_ms": detected_ms,
            "_event_emitted_ms": emitted_ms,
        }
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key not in _WATCH_RESERVED_FIELDS:
                    event[key] = value
        events.append(event)
    return next_cursor, tuple(events)


def watch_events_sqlite(
    *,
    executable: str = "hermes",
    environment: dict[str, str],
    interval: float,
    connect_factory: Callable[..., sqlite3.Connection] = sqlite3.connect,
    sleep_fn: Callable[[float], None] = time.sleep,
    retry_sleep_fn: Callable[[float], None] | None = None,
    max_retries: int = SQLITE_WATCH_MAX_RETRIES,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Iterator[dict[str, object]]:
    """Watch the existing durable event table through one incremental cursor.

    This is deliberately a read-only, restart-safe replacement for the
    subprocess/stdout transport.  It keeps the same one-second poll cadence;
    the optimization removes process/pipe/line parsing work rather than
    hiding load behind a busy loop or a new event store.
    """

    retry_sleep = retry_sleep_fn or sleep_fn
    retry_limit = max(0, int(max_retries))
    connection: sqlite3.Connection | None = None
    cursor: int | None = None
    consecutive_failures = 0
    try:
        while True:
            if connection is None:
                try:
                    connection = _open_sqlite_watch_connection(
                        environment,
                        connect_factory,
                    )
                    if cursor is None:
                        row = connection.execute(
                            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
                        ).fetchone()
                        cursor = int(row["m"] if row is not None else 0)
                except (OSError, sqlite3.Error) as exc:
                    _close_watch_connection(connection)
                    connection = None
                    if cursor is None and consecutive_failures >= retry_limit:
                        raise WatchProcessError(
                            "could not open read-only Kanban event watch"
                        ) from exc
                    if consecutive_failures >= retry_limit:
                        yield from _watch_events_cli_fallback(
                            executable=executable,
                            interval=interval,
                            environment=environment,
                            cursor=cursor,
                            connect_factory=connect_factory,
                            popen_factory=popen_factory,
                        )
                        return
                    delay = SQLITE_WATCH_RETRY_BACKOFF_SECONDS[
                        min(consecutive_failures, len(SQLITE_WATCH_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    consecutive_failures += 1
                    retry_sleep(delay)
                    continue

            try:
                batch_cursor, events = _read_sqlite_watch_batch(
                    connection,
                    int(cursor or 0),
                )
            except (OSError, sqlite3.Error):
                _close_watch_connection(connection)
                connection = None
                if consecutive_failures >= retry_limit:
                    yield from _watch_events_cli_fallback(
                        executable=executable,
                        interval=interval,
                        environment=environment,
                        cursor=int(cursor or 0),
                        connect_factory=connect_factory,
                        popen_factory=popen_factory,
                    )
                    return
                delay = SQLITE_WATCH_RETRY_BACKOFF_SECONDS[
                    min(consecutive_failures, len(SQLITE_WATCH_RETRY_BACKOFF_SECONDS) - 1)
                ]
                consecutive_failures += 1
                retry_sleep(delay)
                continue

            consecutive_failures = 0
            if events:
                for event in events:
                    # The cursor advances only after the consumer resumes the
                    # generator, i.e. after the event has been handed to the
                    # supervisor.  A failure during the handoff therefore
                    # retries this row instead of skipping the remainder of a
                    # fetched batch.
                    yield event
                    row_id = int(event.get("_kanban_event_row_id") or 0)
                    if row_id > int(cursor or 0):
                        cursor = row_id
                cursor = max(int(cursor or 0), batch_cursor)
            else:
                # Non-subscribed rows are safely acknowledged because they
                # never enter supervisor processing.
                cursor = batch_cursor
            sleep_fn(max(0.1, interval))
    finally:
        _close_watch_connection(connection)


class TerminalObserverQueue:
    """Bound slow Discord/Notion terminal projections off the event workers."""

    def __init__(self, *, workers: int = 2, max_pending: int = 128) -> None:
        self.worker_count = max(1, int(workers))
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(
            maxsize=max(1, int(max_pending))
        )
        self._threads = tuple(
            threading.Thread(
                target=self._run_worker,
                name=f"ceo-terminal-observer-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count)
        )
        for thread in self._threads:
            thread.start()

    def submit(self, callback: Callable[[], None]) -> bool:
        try:
            self._queue.put_nowait(callback)
        except queue.Full:
            logging.warning(
                "supervisor-observer-queue-full queue_depth=%d",
                self._queue.qsize(),
            )
            return False
        logging.info(
            "supervisor-observer-queued queue_depth=%d",
            self._queue.qsize(),
        )
        return True

    def _run_worker(self) -> None:
        while True:
            callback = self._queue.get()
            if callback is None:
                self._queue.task_done()
                return
            try:
                with cli_lane("observer"):
                    callback()
            except Exception:
                logging.exception("supervisor-observer-worker-failed")
            finally:
                self._queue.task_done()

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
        for _thread in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=5)


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
        read_ms = time.time_ns() // 1_000_000
        queued_event["_event_read_ms"] = read_ms
        queued_event["_event_consumed_ms"] = read_ms
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

        queued_event["_event_enqueued_ms"] = time.time_ns() // 1_000_000
        self._queue.put(
            (self._priority(queued_event), sequence, pending_key, queued_event)
        )
        with self._pending_lock:
            self._max_queue_depth = max(self._max_queue_depth, self._queue.qsize())
        logging.info(
            "supervisor-event-queued task=%s kind=%s event=%s root=%s request=%s "
            "queue_depth=%d pending=%d persisted=%d detected=%d emitted=%d "
            "read=%d enqueued=%d",
            queued_event.get("task_id") or "",
            queued_event.get("kind") or "",
            queued_event.get("event_id") or "",
            queued_event.get("root_id") or "",
            queued_event.get("request_id") or "",
            self._queue.qsize(),
            pending_count,
            int(queued_event.get("_event_persisted_ms") or 0),
            int(queued_event.get("_event_detected_ms") or 0),
            int(queued_event.get("_event_emitted_ms") or 0),
            int(queued_event.get("_event_read_ms") or 0),
            int(queued_event.get("_event_enqueued_ms") or 0),
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
                    with cli_lane("event"):
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
                persisted_ms = int(event.get("_event_persisted_ms") or created_ms)
                detected_ms = int(event.get("_event_detected_ms") or 0)
                emitted_ms = int(event.get("_event_emitted_ms") or 0)
                read_ms = int(event.get("_event_read_ms") or consumed_ms)
                enqueued_ms = int(event.get("_event_enqueued_ms") or consumed_ms)
                logging.info(
                    "supervisor-event-loop-timing task=%s kind=%s obsolete=%s "
                    "event=%s root=%s request=%s event_created=%d persisted=%d "
                    "detected=%d emitted=%d read=%d enqueued=%d event_consumed=%d "
                    "handler_started=%d "
                    "loop_return=%d created_to_consumed_ms=%d "
                    "persisted_to_detected_ms=%d detected_to_emit_ms=%d "
                    "emit_to_read_ms=%d read_to_enqueue_ms=%d "
                    "persisted_to_consumed_ms=%d queue_wait_ms=%d "
                    "enqueue_to_handler_start_ms=%d handler_duration_ms=%d "
                    "queue_depth=%d pending=%d",
                    event.get("task_id") or "",
                    event.get("kind") or "",
                    obsolete,
                    event.get("event_id") or "",
                    event.get("root_id") or "",
                    event.get("request_id") or "",
                    created_ms,
                    persisted_ms,
                    detected_ms,
                    emitted_ms,
                    read_ms,
                    enqueued_ms,
                    consumed_ms,
                    handler_started_ms,
                    handler_completed_ms,
                    consumed_ms - created_ms
                    if created_ms > 0 and consumed_ms >= created_ms
                    else -1,
                    detected_ms - persisted_ms
                    if detected_ms > 0 and detected_ms >= persisted_ms
                    else -1,
                    emitted_ms - detected_ms
                    if emitted_ms > 0 and detected_ms > 0 and emitted_ms >= detected_ms
                    else -1,
                    read_ms - emitted_ms
                    if read_ms > 0 and emitted_ms > 0 and read_ms >= emitted_ms
                    else -1,
                    enqueued_ms - read_ms
                    if enqueued_ms >= read_ms
                    else -1,
                    consumed_ms - persisted_ms
                    if persisted_ms > 0 and consumed_ms >= persisted_ms
                    else -1,
                    handler_started_ms - consumed_ms
                    if consumed_ms > 0 and handler_started_ms >= consumed_ms
                    else -1,
                    handler_started_ms - enqueued_ms
                    if enqueued_ms > 0 and handler_started_ms >= enqueued_ms
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
            with cli_lane("recovery"):
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
            with cli_lane("synthesis-recovery"):
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
            with cli_lane("startup"):
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
        # Both recovery checks share one discovery projection for this cycle.
        # Production clients obtain candidate rows from the read-only SQLite
        # discovery path; only an unavailable/uncertain index falls back to
        # one authoritative full-board CLI list. Selected candidates still
        # revalidate through authoritative show() calls in the service.
        listed_rows = None
        client = getattr(service, "client", None)
        candidate_rows = getattr(client, "recovery_candidate_rows", None)
        list_tasks = getattr(client, "list_tasks", None)
        if callable(candidate_rows):
            try:
                with cli_lane("recovery"):
                    listed_rows = candidate_rows()
            except (
                RootScopedIndexUnavailable,
                SupervisorWorkflowError,
                HermesKanbanCommandError,
            ) as exc:
                print(
                    "ceo-supervisor recovery-discovery-error="
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                logging.warning(
                    "kanban-full-board-fallback lane=recovery "
                    "reason=discovery-unavailable root=unknown"
                )
                if callable(list_tasks):
                    try:
                        with cli_lane("recovery"):
                            listed_rows = list_tasks()
                    except (SupervisorWorkflowError, HermesKanbanCommandError) as list_exc:
                        print(
                            "ceo-supervisor recovery-board-list-error="
                            f"{type(list_exc).__name__}",
                            file=sys.stderr,
                            flush=True,
                        )
                        listed_rows = None
        elif callable(list_tasks):
            try:
                with cli_lane("recovery"):
                    listed_rows = list_tasks()
            except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
                print(
                    "ceo-supervisor recovery-board-list-error="
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                listed_rows = None

        try:
            reconcile_expired = getattr(service, "reconcile_expired_workflows", None)
            if callable(reconcile_expired):
                with cli_lane("workflow-timeout"):
                    expired_roots = reconcile_expired(listed_rows=listed_rows)
                for root_id in expired_roots:
                    print(
                        f"ceo-supervisor workflow-timeout-reconciled root={root_id}",
                        flush=True,
                    )
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor workflow-timeout-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        try:
            with cli_lane("recovery"):
                if listed_rows is None:
                    service.materialize_ready_primary_plans()
                else:
                    service.materialize_ready_primary_plans(listed_rows=listed_rows)
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            print(
                "ceo-supervisor ready-plan-reconcile-error="
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        try:
            with cli_lane("synthesis-recovery"):
                if listed_rows is None:
                    recovered = service.reconcile_completed_syntheses()
                else:
                    recovered = service.reconcile_completed_syntheses(
                        listed_rows=listed_rows
                    )
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
    observer_queue = TerminalObserverQueue(
        workers=int(environment.get("CEO_SUPERVISOR_OBSERVER_WORKERS", "2")),
        max_pending=int(environment.get("CEO_SUPERVISOR_OBSERVER_MAX_PENDING", "128")),
    )
    service = CeoSupervisorService(
        client,
        max_retries=args.max_retries,
        max_wakeups=args.max_wakeups,
        synthesis_projection=CeoNotionProjection(kanban_client=client),
        qa_projection=QaAuditProjection(kanban_client=client),
        discord_delivery=DiscordFinalDelivery(environment=environment),
        terminal_observer_submit=observer_queue.submit,
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
        observer_queue.close(drain=True)
        print(f"ceo-supervisor normal-shutdown={exc}", flush=True)
        return 0
    except (WatchOutputError, WatchProcessError) as exc:
        recovery_stop.set()
        event_queue.close(drain=True)
        observer_queue.close(drain=True)
        print(f"ceo-supervisor fatal-watch-error={exc}", file=sys.stderr, flush=True)
        return 1
    recovery_stop.set()
    event_queue.close(drain=True)
    observer_queue.close(drain=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
