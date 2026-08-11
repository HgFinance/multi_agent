"""CEO closed-loop supervisor policy and Hermes boundary tests."""

from __future__ import annotations

import unittest

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    HermesKanbanClient,
    SupervisorAction,
    SupervisorState,
    SupervisorValidationError,
    ChildTaskState,
    decide_supervisor,
    parse_supervisor_output,
)


def child(
    task_id: str,
    profile: str,
    status: str,
    *,
    body: str = "",
    block_kind: str = "",
    block_reason: str = "",
    retry_count: int = 0,
    summary: str = "summary",
) -> ChildTaskState:
    return ChildTaskState(
        task_id=task_id,
        profile=profile,
        status=status,
        body=body,
        block_kind=block_kind,
        block_reason=block_reason,
        retry_count=retry_count,
        summary=summary,
    )


class SupervisorPolicyTest(unittest.TestCase):
    def test_dynamic_routing_runs_qa_for_selected_primary_children_only(self) -> None:
        state = SupervisorState(
            "root",
            (
                child("r", "research-department", "done"),
                child("risk", "risk-management", "done"),
            ),
        )
        decision = decide_supervisor(state)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(decision.assignee, "qa-department")
        self.assertEqual(decision.parent_task_ids, ("r", "risk"))

    def test_blocked_transient_can_retry_and_other_blocked_can_replan(self) -> None:
        retry = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "blocked", block_kind="transient"),),
            )
        )
        self.assertEqual(retry.action, SupervisorAction.RETRY_TASK)

        replan = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "blocked", block_reason="source unavailable"),),
            )
        )
        self.assertEqual(replan.action, SupervisorAction.CREATE_TASK)
        self.assertEqual(replan.assignee, "research-department")

    def test_blocked_needs_input_is_not_treated_as_failure(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child(
                        "r",
                        "research-department",
                        "blocked",
                        block_kind="needs_input",
                        block_reason="user input required",
                    ),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.REQUEST_USER_INPUT)

    def test_retry_and_wakeup_limits_abort(self) -> None:
        retry_limit = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "failed", retry_count=2),),
            )
        )
        self.assertEqual(retry_limit.action, SupervisorAction.BLOCK_ABORT)
        wakeup_limit = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "done"),),
                wakeups=8,
            )
        )
        self.assertEqual(wakeup_limit.action, SupervisorAction.BLOCK_ABORT)

    def test_qa_done_triggers_final_synthesis(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "done"),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.assignee, "ceo-agent")

    def test_ceo_can_explicitly_skip_default_qa(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (child("trading", "trading-department", "done"),),
                qa_required=False,
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)

    def test_unknown_structured_assignee_is_rejected(self) -> None:
        with self.assertRaises(SupervisorValidationError):
            parse_supervisor_output(
                {
                    "action": "CREATE_TASK",
                    "parent_task_id": "root",
                    "assignee": "risk-department",
                    "title": "risk",
                    "body": "body",
                }
            )


class FakeClient:
    def __init__(self) -> None:
        self.payloads = [
            {"id": "r", "assignee": "research-department", "status": "done", "summary": "research"},
            {"id": "risk", "assignee": "risk-management", "status": "done", "summary": "risk"},
        ]
        self.created: list[dict[str, object]] = []
        self.unblocked: list[str] = []
        self.blocked: list[str] = []

    def workflow(self, task_id: str):
        return "root", tuple(self.payloads)

    def create_task(self, **kwargs):
        self.created.append(kwargs)
        return {"id": f"new-{len(self.created)}"}

    def unblock_task(self, task_id: str) -> None:
        self.unblocked.append(task_id)

    def block_task(self, task_id: str, reason: str) -> None:
        self.blocked.append(task_id)


class SupervisorWakeupTest(unittest.TestCase):
    def test_terminal_child_wakes_then_qa_done_wakes_final_synthesis(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        first = service.handle_terminal_event({"event_id": "e1", "task_id": "r", "kind": "completed"})
        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertEqual(client.created[0]["assignee"], "qa-department")

        client.payloads.append(
            {
                "id": "qa",
                "assignee": "qa-department",
                "status": "done",
                "summary": "qa passed",
            }
        )
        second = service.handle_terminal_event({"event_id": "e2", "task_id": "qa", "kind": "completed"})
        self.assertEqual(second.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.created[1]["assignee"], "ceo-agent")
        self.assertEqual(client.created[1]["parent_task_ids"], ("r", "risk", "qa"))

    def test_hermes_show_json_task_projection_is_flattened(self) -> None:
        import json
        import subprocess

        completed = subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps(
                {
                    "task": {
                        "id": "r",
                        "assignee": "research-department",
                        "status": "done",
                        "body": "research",
                    },
                    "parents": ["root"],
                    "children": [],
                    "runs": [],
                    "latest_summary": "research summary",
                }
            ),
            stderr="",
        )

        def runner(*args, **kwargs):
            return completed

        task = HermesKanbanClient(runner=runner).show("r")
        self.assertEqual(task["id"], "r")
        self.assertEqual(task["assignee"], "research-department")
        self.assertEqual(task["parents"], ["root"])
        self.assertEqual(task["latest_summary"], "research summary")


if __name__ == "__main__":
    unittest.main()
