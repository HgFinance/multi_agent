"""Unit tests for the fire-and-forget Hermes Kanban tracking bridge."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

import apps.api.kanban_tracker as kanban_tracker


def _run_synchronously(target) -> None:
    """Replace `_spawn`'s daemon-thread dispatch with an inline call for determinism."""

    target()


class KanbanTrackerDisabledTest(unittest.TestCase):
    def test_disabled_by_default_never_calls_subprocess(self) -> None:
        with mock.patch.object(kanban_tracker, "KANBAN_TRACKING_ENABLED", False), mock.patch.object(
            kanban_tracker, "_run_cli"
        ) as run_cli:
            kanban_tracker.track_department_started("job-1", "research", "title")
            kanban_tracker.track_department_completed("job-1", "research", "COMPLETED", "done")
            kanban_tracker.track_department_blocked("job-1", "research", "blocked")
        run_cli.assert_not_called()


class KanbanTrackerEnabledTest(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(kanban_tracker, "KANBAN_TRACKING_ENABLED", True),
            mock.patch.object(kanban_tracker, "_spawn", side_effect=_run_synchronously),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_track_started_creates_task_with_correct_args_and_reports_id(self) -> None:
        created = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout=json.dumps({"id": "task-42"}), stderr=""
        )
        received_ids: list[str] = []
        with mock.patch.object(kanban_tracker, "_run_cli", return_value=created) as run_cli:
            kanban_tracker.track_department_started(
                "job-1", "research", "research-department — job-1", on_task_id=received_ids.append
            )
        run_cli.assert_called_once_with(
            [
                "kanban",
                "create",
                "research-department — job-1",
                "--assignee",
                "research",
                "--initial-status",
                "running",
                "--idempotency-key",
                "job-1:research",
                "--json",
            ]
        )
        self.assertEqual(received_ids, ["task-42"])

    def test_track_completed_completes_task_when_status_completed(self) -> None:
        created = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout=json.dumps({"id": "task-42"}), stderr=""
        )
        with mock.patch.object(kanban_tracker, "_run_cli", return_value=created) as run_cli:
            kanban_tracker.track_department_completed("job-1", "research", "COMPLETED", "리서치 완료")
        self.assertEqual(run_cli.call_count, 2)
        complete_call = run_cli.call_args_list[1].args[0]
        self.assertEqual(complete_call, ["kanban", "complete", "task-42", "--result", "리서치 완료"])

    def test_track_completed_blocks_task_when_status_not_completed(self) -> None:
        created = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout=json.dumps({"id": "task-42"}), stderr=""
        )
        with mock.patch.object(kanban_tracker, "_run_cli", return_value=created) as run_cli:
            kanban_tracker.track_department_completed("job-1", "risk", "DEGRADED", "부분 실패")
        block_call = run_cli.call_args_list[1].args[0]
        self.assertEqual(block_call, ["kanban", "block", "task-42", "부분 실패", "--kind", "needs_input"])

    def test_track_blocked_blocks_task_with_reason(self) -> None:
        created = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout=json.dumps({"id": "task-9"}), stderr=""
        )
        with mock.patch.object(kanban_tracker, "_run_cli", return_value=created) as run_cli:
            kanban_tracker.track_department_blocked("job-2", "qa", "입력 미준비")
        block_call = run_cli.call_args_list[1].args[0]
        self.assertEqual(block_call, ["kanban", "block", "task-9", "입력 미준비", "--kind", "needs_input"])

    def test_create_failure_short_circuits_without_followup_call(self) -> None:
        failed = subprocess.CompletedProcess(args=["hermes"], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(kanban_tracker, "_run_cli", return_value=failed) as run_cli:
            kanban_tracker.track_department_completed("job-1", "research", "COMPLETED", "done")
        run_cli.assert_called_once()

    def test_subprocess_exception_never_propagates(self) -> None:
        with mock.patch(
            "apps.api.kanban_tracker.subprocess.run", side_effect=OSError("hermes not found")
        ):
            kanban_tracker.track_department_started("job-1", "research", "title")
            kanban_tracker.track_department_completed("job-1", "research", "COMPLETED", "done")
            kanban_tracker.track_department_blocked("job-1", "research", "reason")


if __name__ == "__main__":
    unittest.main()
