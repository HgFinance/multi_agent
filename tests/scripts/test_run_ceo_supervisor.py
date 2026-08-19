"""Contract tests for the standalone CEO supervisor watch loop."""

from __future__ import annotations

import unittest
import importlib.util
import sys
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

    def test_watch_preamble_and_stop_marker_are_not_events(self) -> None:
        self.assertIsNone(parse_watch_line("Watching kanban events. Ctrl-C to stop."))
        self.assertIsNone(parse_watch_line("(stopped)"))

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
            "claimed,spawned,completed,blocked,gave_up,crashed,timed_out,spawn_failed",
            command,
        )
        self.assertNotIn("reclaimed", command)


if __name__ == "__main__":
    unittest.main()
