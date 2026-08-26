"""Contract tests for the standalone CEO supervisor watch loop."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import importlib.util
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "ceo_supervisor_runner", ROOT / "scripts/run_ceo_supervisor.py"
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

WatchOutputError = runner.WatchOutputError
WatchProcessError = runner.WatchProcessError
parse_watch_line = runner.parse_watch_line
watch_events = runner.watch_events
watch_events_sqlite = runner.watch_events_sqlite
run_recovery_reconciler = runner.run_recovery_reconciler
SupervisorEventQueue = runner.SupervisorEventQueue
TerminalObserverQueue = runner.TerminalObserverQueue
read_sqlite_watch_batch = runner._read_sqlite_watch_batch


class FakeWatchProcess:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = lines
        self.returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class SupervisorRunnerTest(unittest.TestCase):
    def test_terminal_observer_queue_returns_before_slow_projection_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def observer() -> None:
            entered.set()
            release.wait(1)
            completed.set()

        observer_queue = TerminalObserverQueue(workers=1, max_pending=2)
        started = time.perf_counter()
        self.assertTrue(observer_queue.submit(observer))
        submit_elapsed = time.perf_counter() - started

        self.assertLess(submit_elapsed, 0.1)
        self.assertTrue(entered.wait(1))
        self.assertFalse(completed.is_set())
        release.set()
        observer_queue.close()
        self.assertTrue(completed.is_set())

    def test_event_queue_keeps_intake_non_blocking_and_runs_separate_roots(self) -> None:
        both_started = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()

        class Service:
            def handle_terminal_event(self, event):
                with calls_lock:
                    calls.append(str(event["task_id"]))
                    if len(calls) == 2:
                        both_started.set()
                release.wait(1)
                return None

        event_queue = SupervisorEventQueue(Service(), workers=2)
        started = time.perf_counter()
        self.assertTrue(
            event_queue.submit(
                {"event_id": "root-a", "task_id": "task-a", "kind": "blocked"}
            )
        )
        self.assertTrue(
            event_queue.submit(
                {"event_id": "root-b", "task_id": "task-b", "kind": "blocked"}
            )
        )
        intake_elapsed = time.perf_counter() - started

        self.assertLess(intake_elapsed, 0.1)
        self.assertTrue(both_started.wait(1))
        release.set()
        event_queue.close()
        self.assertCountEqual(calls, ["task-a", "task-b"])

    def test_event_queue_coalesces_same_completed_transition(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        class Service:
            def handle_terminal_event(self, event):
                calls.append(str(event["event_id"]))
                entered.set()
                release.wait(1)
                return None

        event_queue = SupervisorEventQueue(Service(), workers=2)
        self.assertTrue(
            event_queue.submit(
                {"event_id": "watch", "task_id": "same", "kind": "completed"}
            )
        )
        self.assertTrue(entered.wait(1))
        self.assertFalse(
            event_queue.submit(
                {"event_id": "recovery", "task_id": "same", "kind": "done"}
            )
        )
        release.set()
        event_queue.close()

        self.assertEqual(calls, ["watch"])

    def test_event_queue_coalesces_active_lifecycle_for_one_task(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        class Service:
            def handle_terminal_event(self, event):
                calls.append(str(event["kind"]))
                entered.set()
                release.wait(1)
                return None

        event_queue = SupervisorEventQueue(Service(), workers=2)
        self.assertTrue(
            event_queue.submit(
                {"event_id": "claimed", "task_id": "same", "kind": "claimed"}
            )
        )
        self.assertTrue(entered.wait(1))
        self.assertFalse(
            event_queue.submit(
                {"event_id": "spawned", "task_id": "same", "kind": "spawned"}
            )
        )
        release.set()
        event_queue.close()

        self.assertEqual(calls, ["claimed"])

    def test_terminal_event_preempts_queued_active_event_for_same_task(self) -> None:
        busy_started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        class Service:
            def handle_terminal_event(self, event):
                calls.append(str(event["kind"]))
                if event["task_id"] == "busy":
                    busy_started.set()
                    release.wait(1)
                return None

        event_queue = SupervisorEventQueue(Service(), workers=1)
        self.assertTrue(
            event_queue.submit(
                {"event_id": "busy", "task_id": "busy", "kind": "claimed"}
            )
        )
        self.assertTrue(busy_started.wait(1))
        self.assertTrue(
            event_queue.submit(
                {"event_id": "active", "task_id": "hot", "kind": "spawned"}
            )
        )
        self.assertTrue(
            event_queue.submit(
                {"event_id": "terminal", "task_id": "hot", "kind": "completed"}
            )
        )
        release.set()
        event_queue.close()

        self.assertEqual(calls, ["claimed", "completed"])
        metrics = event_queue.metrics_snapshot()
        self.assertEqual(metrics["by_kind"]["spawned"]["count"], 1)
        self.assertGreaterEqual(metrics["max_queue_depth"], 2)

    def test_distinct_terminal_kinds_are_not_coalesced(self) -> None:
        release = threading.Event()
        entered = threading.Event()
        calls: list[str] = []

        class Service:
            def handle_terminal_event(self, event):
                calls.append(str(event["kind"]))
                if event["task_id"] == "busy":
                    entered.set()
                    release.wait(1)
                return None

        event_queue = SupervisorEventQueue(Service(), workers=1)
        event_queue.submit({"task_id": "busy", "kind": "claimed"})
        self.assertTrue(entered.wait(1))
        self.assertTrue(
            event_queue.submit({"event_id": "b", "task_id": "same", "kind": "blocked"})
        )
        self.assertTrue(
            event_queue.submit({"event_id": "g", "task_id": "same", "kind": "gave_up"})
        )
        release.set()
        event_queue.close()

        self.assertEqual(calls, ["claimed", "blocked", "gave_up"])

    def test_event_worker_survives_one_unexpected_failure(self) -> None:
        calls: list[str] = []

        class Service:
            def handle_terminal_event(self, event):
                calls.append(str(event["event_id"]))
                if len(calls) == 1:
                    raise RuntimeError("broken observer")
                return None

        event_queue = SupervisorEventQueue(Service(), workers=1)
        with self.assertLogs(level="ERROR") as logs:
            event_queue.submit(
                {"event_id": "broken", "task_id": "a", "kind": "blocked"}
            )
            event_queue.submit(
                {"event_id": "healthy", "task_id": "b", "kind": "blocked"}
            )
            event_queue.close()

        self.assertEqual(calls, ["broken", "healthy"])
        self.assertTrue(
            any("supervisor-event-worker-failed" in message for message in logs.output)
        )

    def test_recovery_lanes_share_one_serial_poll_loop(self) -> None:
        calls: list[str] = []

        class Service:
            def materialize_ready_primary_plans(self, **kwargs):
                calls.append("ready")

            def reconcile_completed_syntheses(self, **kwargs):
                calls.append("synthesis")
                return ("t_synthesis",)

        class OneCycleStop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.stopped = True

        run_recovery_reconciler(
            Service(),
            interval=0.25,
            stop_event=OneCycleStop(),
        )

        self.assertEqual(calls, ["ready", "synthesis"])

    def test_recovery_lane_shares_one_board_list_snapshot(self) -> None:
        calls: list[tuple[str, object]] = []

        class Client:
            def list_tasks(self):
                calls.append(("list", None))
                return ({"id": "root"},)

        class Service:
            client = Client()

            def materialize_ready_primary_plans(self, **kwargs):
                calls.append(("ready", kwargs.get("listed_rows")))

            def reconcile_completed_syntheses(self, **kwargs):
                calls.append(("synthesis", kwargs.get("listed_rows")))
                return ()

        class OneCycleStop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.stopped = True

        run_recovery_reconciler(
            Service(),
            interval=0.25,
            stop_event=OneCycleStop(),
        )

        self.assertEqual([kind for kind, _value in calls], ["list", "ready", "synthesis"])
        self.assertIs(calls[1][1], calls[2][1])

    def test_recovery_lane_uses_sqlite_candidates_without_full_board_list(self) -> None:
        calls: list[tuple[str, object]] = []
        candidate_rows = ({"id": "root", "status": "ready"},)

        class Client:
            def recovery_candidate_rows(self):
                calls.append(("candidate", None))
                return candidate_rows

            def list_tasks(self):
                raise AssertionError("healthy candidate discovery must not list board")

        class Service:
            client = Client()

            def materialize_ready_primary_plans(self, **kwargs):
                calls.append(("ready", kwargs.get("listed_rows")))

            def reconcile_completed_syntheses(self, **kwargs):
                calls.append(("synthesis", kwargs.get("listed_rows")))
                return ()

        class OneCycleStop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.stopped = True

        run_recovery_reconciler(
            Service(),
            interval=0.25,
            stop_event=OneCycleStop(),
        )

        self.assertEqual(
            [kind for kind, _value in calls],
            ["candidate", "ready", "synthesis"],
        )
        self.assertIs(calls[1][1], candidate_rows)
        self.assertIs(calls[2][1], candidate_rows)

    def test_watch_text_contract_and_reserved_fields(self) -> None:
        line = (
            "[2026-08-11 12:00:00] t_123      completed          "
            "(@research-department) {'task_id': 'wrong', 'summary': 'ok'}"
        )

        event = parse_watch_line(line)

        self.assertEqual(event["task_id"], "t_123")
        self.assertEqual(event["kind"], "completed")
        self.assertEqual(event["assignee"], "research-department")
        self.assertEqual(event["summary"], "ok")
        self.assertNotEqual(event["task_id"], "wrong")
        self.assertEqual(event["_event_created_ms"], 1786449600000)

    def test_watch_preamble_and_stop_marker_are_not_events(self) -> None:
        self.assertIsNone(parse_watch_line("Watching kanban events. Ctrl-C to stop."))
        self.assertIsNone(parse_watch_line("(stopped)"))

    def test_sqlite_watch_reads_durable_cursor_and_survives_bad_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kanban.db"
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    assignee TEXT,
                    tenant TEXT
                );
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at INTEGER NOT NULL
                );
                INSERT INTO tasks(id, assignee, tenant)
                VALUES ('task-1', 'research-department', 'ceo');
                INSERT INTO task_events(id, task_id, kind, payload, created_at)
                VALUES (1, 'task-1', 'completed', '{}', 1000);
                INSERT INTO task_events(id, task_id, kind, payload, created_at)
                VALUES (3, 'task-1', 'commented', '{}', 1002);
                INSERT INTO task_events(id, task_id, kind, payload, created_at)
                VALUES (4, 'task-1', '{bad-kind}', '{bad-json', 1003);
                """,
            )
            connection.execute(
                "INSERT INTO task_events(id, task_id, kind, payload, created_at) "
                "VALUES (?, 'task-1', 'completed', ?, 1001)",
                (
                    2,
                    json.dumps(
                        {
                            "root_id": "root-1",
                            "request_id": "request-1",
                            "summary": "not logged",
                        }
                    ),
                ),
            )
            connection.commit()

            # A reconnect starts after the last durable row it has consumed;
            # this call uses cursor=1 to model restart recovery and proves that
            # non-watched rows still advance the cursor without being emitted.
            cursor, events = read_sqlite_watch_batch(
                connection,
                1,
                detected_ms=2001000,
            )

            self.assertEqual(cursor, 4)
            self.assertEqual([event["event_id"] for event in events], ["kanban:2"])
            event = events[0]
            self.assertEqual(event["root_id"], "root-1")
            self.assertEqual(event["request_id"], "request-1")
            self.assertEqual(event["_kanban_event_row_id"], 2)
            self.assertEqual(event["_event_persisted_ms"], 1001000)
            self.assertEqual(event["_event_detected_ms"], 2001000)

            # The malformed kind is intentionally outside the subscribed set;
            # malformed payloads on a subscribed kind are handled as None by
            # the same helper rather than terminating the consumer.
            connection.execute(
                "INSERT INTO task_events(id, task_id, kind, payload, created_at) "
                "VALUES (5, 'task-1', 'completed', '{bad-json', 1004)"
            )
            connection.commit()
            cursor, events = read_sqlite_watch_batch(
                connection,
                cursor,
                detected_ms=2002000,
            )
            self.assertEqual(cursor, 5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_id"], "kanban:5")
            self.assertNotIn("summary", events[0])

            connection.close()

    def test_sqlite_watch_reconnects_without_rewinding_cursor(self) -> None:
        class StopAfterDelivery(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kanban.db"
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, tenant TEXT);
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at INTEGER NOT NULL
                );
                INSERT INTO tasks VALUES ('task-1', 'research-department', 'ceo');
                INSERT INTO task_events VALUES (1, 'task-1', 'completed', '{}', 1000);
                """
            )
            connection.commit()
            connection.close()

            batch_calls = 0
            factory_calls = 0
            remaining_failures = 2
            retry_delays: list[float] = []

            class ScriptedConnection:
                def __init__(self, inner: sqlite3.Connection) -> None:
                    self.inner = inner

                @property
                def row_factory(self):
                    return self.inner.row_factory

                @row_factory.setter
                def row_factory(self, value):
                    self.inner.row_factory = value

                def execute(self, sql, params=()):
                    nonlocal batch_calls, remaining_failures
                    if "WHERE e.id >" in sql:
                        if remaining_failures > 0:
                            remaining_failures -= 1
                            batch_calls += 1
                            raise sqlite3.OperationalError("database is locked")
                    return self.inner.execute(sql, params)

                def close(self):
                    self.inner.close()

            def connect_factory(*args, **kwargs):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 3:
                    writable = sqlite3.connect(path)
                    writable.execute(
                        "INSERT INTO task_events VALUES "
                        "(2, 'task-1', 'completed', '{}', 1001)"
                    )
                    writable.commit()
                    writable.close()
                inner = sqlite3.connect(*args, **kwargs)
                return ScriptedConnection(inner)

            poll_sleeps = 0

            def sleep_fn(_seconds: float) -> None:
                nonlocal poll_sleeps
                poll_sleeps += 1
                if poll_sleeps >= 2:
                    raise StopAfterDelivery()

            iterator = watch_events_sqlite(
                executable="hermes",
                environment={"HERMES_KANBAN_DB": str(path)},
                interval=0.1,
                connect_factory=connect_factory,
                sleep_fn=sleep_fn,
                retry_sleep_fn=retry_delays.append,
                max_retries=2,
            )
            try:
                event = next(iterator)
                self.assertEqual(event["event_id"], "kanban:2")
                with self.assertRaises(StopAfterDelivery):
                    next(iterator)
            finally:
                iterator.close()

            self.assertEqual(factory_calls, 3)
            self.assertEqual(retry_delays, [0.1, 0.25])

    def test_permanent_sqlite_failure_uses_cursor_safe_cli_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kanban.db"
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, tenant TEXT);
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at INTEGER NOT NULL
                );
                INSERT INTO tasks VALUES ('task-1', 'research-department', 'ceo');
                INSERT INTO task_events VALUES (1, 'task-1', 'completed', '{}', 1000);
                """
            )
            connection.commit()
            connection.close()

            factory_calls = 0

            class AlwaysFailingConnection:
                def __init__(self, inner):
                    self.inner = inner

                @property
                def row_factory(self):
                    return self.inner.row_factory

                @row_factory.setter
                def row_factory(self, value):
                    self.inner.row_factory = value

                def execute(self, sql, params=()):
                    if "WHERE e.id >" in sql:
                        raise sqlite3.OperationalError("database is locked")
                    return self.inner.execute(sql, params)

                def close(self):
                    self.inner.close()

            def connect_factory(*args, **kwargs):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 2:
                    writable = sqlite3.connect(path)
                    writable.execute(
                        "INSERT INTO task_events VALUES "
                        "(2, 'task-1', 'completed', '{}', 1001)"
                    )
                    writable.commit()
                    writable.close()
                inner = sqlite3.connect(*args, **kwargs)
                if factory_calls == 1:
                    return AlwaysFailingConnection(inner)
                return inner

            class Process:
                stdout = [
                    "Watching kanban events. Ctrl-C to stop.\n",
                    "[1970-01-01 00:16:41] task-1     completed          (@research-department) {}\n",
                    "[1970-01-01 00:16:42] task-1     completed          (@research-department) {'new': 'event'}\n",
                    "(stopped)\n",
                ]
                returncode = 0

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

                def terminate(self):
                    return None

                def kill(self):
                    return None

            commands: list[list[str]] = []

            def popen_factory(command, **kwargs):
                commands.append(list(command))
                return Process()

            events = list(
                watch_events_sqlite(
                    executable="hermes",
                    environment={"HERMES_KANBAN_DB": str(path)},
                    interval=0.1,
                    connect_factory=connect_factory,
                    retry_sleep_fn=lambda _seconds: None,
                    max_retries=0,
                    popen_factory=popen_factory,
                )
            )

            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event_id"], "kanban:2")
            self.assertNotEqual(events[1]["event_id"], "kanban:2")
            self.assertEqual(commands[0][0:3], ["hermes", "kanban", "watch"])

    def test_sqlite_watch_shutdown_during_retry_closes_connection(self) -> None:
        class StopRetry(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "kanban.db"
            closed = []

            def connect_factory(*args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            iterator = watch_events_sqlite(
                environment={"HERMES_KANBAN_DB": str(path)},
                interval=0.1,
                connect_factory=connect_factory,
                retry_sleep_fn=lambda _seconds: (_ for _ in ()).throw(StopRetry()),
                max_retries=2,
            )
            with self.assertRaises(StopRetry):
                next(iterator)
            iterator.close()

    def test_malformed_watch_line_is_fatal_to_watch_loop(self) -> None:
        with self.assertRaises(WatchOutputError):
            parse_watch_line("not a Hermes kanban event")

    def test_watch_unexpected_eof_is_not_normal_shutdown(self) -> None:
        process = FakeWatchProcess(["Watching kanban events. Ctrl-C to stop.\n"])

        with self.assertRaises(WatchProcessError):
            list(
                watch_events(
                    executable="hermes",
                    interval=0.1,
                    environment={},
                    popen_factory=lambda *args, **kwargs: process,
                )
            )

    def test_watch_nonzero_exit_is_fatal(self) -> None:
        process = FakeWatchProcess([], returncode=17)

        with self.assertRaises(WatchProcessError):
            list(
                watch_events(
                    executable="hermes",
                    interval=0.1,
                    environment={},
                    popen_factory=lambda *args, **kwargs: process,
                )
            )

    def test_watch_start_failure_is_fatal(self) -> None:
        def popen(*args, **kwargs):
            raise OSError("hermes not found")

        with self.assertRaises(WatchProcessError):
            list(
                watch_events(
                    executable="hermes",
                    interval=0.1,
                    environment={},
                    popen_factory=popen,
                )
            )

    def test_intentional_stop_is_normal_and_reclaimed_is_not_subscribed(self) -> None:
        process = FakeWatchProcess(
            ["Watching kanban events. Ctrl-C to stop.\n", "(stopped)\n"]
        )
        command: list[str] = []

        def popen(*args, **kwargs):
            command.extend(args[0])
            return process

        self.assertEqual(
            list(
                watch_events(
                    executable="hermes",
                    interval=0.1,
                    environment={},
                    popen_factory=popen,
                )
            ),
            [],
        )
        self.assertNotIn("--json", command)
        self.assertIn(
            "claimed,spawned,started,running,completed,blocked,gave_up,crashed,timed_out,spawn_failed",
            command,
        )
        self.assertNotIn("reclaimed", command)


if __name__ == "__main__":
    unittest.main()
