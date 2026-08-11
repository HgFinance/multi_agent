"""Canonical Hermes profile and Kanban create-boundary contracts."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from apps.api import hermes_cli
from apps.api import kanban_tracker
from orchestration.canonical_profiles import (
    CanonicalKanbanTaskRequest,
    CanonicalProfileError,
    canonical_profile_for_department,
    validate_canonical_profile,
)


class CanonicalProfileTest(unittest.TestCase):
    def test_department_codes_resolve_to_canonical_profiles(self) -> None:
        self.assertEqual(canonical_profile_for_department("risk"), "risk-management")
        self.assertEqual(canonical_profile_for_department("qa"), "qa-department")
        self.assertEqual(canonical_profile_for_department("accounting"), "accounting-portfolio-department")

    def test_legacy_or_unknown_names_are_rejected(self) -> None:
        for value in ("risk-department", "ai-qa-audit-department", "not-a-profile"):
            with self.assertRaises(CanonicalProfileError):
                validate_canonical_profile(value)
            with self.assertRaises(CanonicalProfileError):
                canonical_profile_for_department(value)

    def test_typed_create_request_rejects_unknown_assignee(self) -> None:
        with self.assertRaises(CanonicalProfileError):
            CanonicalKanbanTaskRequest(
                assignee="risk-department",
                title="risk",
                body="body",
                idempotency_key="id-1",
            )


class HermesCreateBoundaryTest(unittest.TestCase):
    def test_raw_assignee_never_reaches_subprocess(self) -> None:
        with mock.patch.object(hermes_cli.subprocess, "run") as run:
            with self.assertRaises(CanonicalProfileError):
                hermes_cli.create_kanban_task(
                    assignee="risk-department",
                    title="risk",
                    body="body",
                    idempotency_key="id-1",
                )
        run.assert_not_called()

    def test_create_uses_canonical_assignee(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"id": "task-1", "status": "ready"}),
            stderr="",
        )
        with mock.patch.dict(os.environ, {"ENABLE_KANBAN_TASK_TRACKING": "true"}), mock.patch.object(
            hermes_cli.subprocess, "run", return_value=completed
        ) as run:
            result = hermes_cli.create_kanban_task(
                assignee="risk-management",
                title="risk",
                body="body",
                idempotency_key="id-1",
            )
        self.assertEqual(result["task_id"], "task-1")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--assignee") + 1], "risk-management")

    def test_tracker_rejects_unknown_department_before_disabled_flag(self) -> None:
        with mock.patch.object(kanban_tracker, "KANBAN_TRACKING_ENABLED", False):
            with self.assertRaises(CanonicalProfileError):
                kanban_tracker.track_department_started("job-1", "risk-department", "risk")


if __name__ == "__main__":
    unittest.main()
