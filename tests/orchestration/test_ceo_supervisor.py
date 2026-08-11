"""CEO closed-loop supervisor policy and Hermes boundary tests."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

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
        self.comments: list[dict[str, str]] = []

    def workflow(self, task_id: str):
        return "root", tuple(self.payloads)

    def show(self, task_id: str):
        return {"id": task_id, "comments": list(self.comments)}

    def create_task(self, **kwargs):
        self.created.append(kwargs)
        task_id = f"new-{len(self.created)}"
        # Supervisor-created children are the durable action/idempotency
        # record.  Ordinary test fixtures remain easy to update in-place.
        self.payloads.append(
            {
                "id": task_id,
                "assignee": kwargs["assignee"],
                "status": "ready",
                "body": kwargs["body"],
            }
        )
        return {"id": task_id}

    def comment_task(self, task_id: str, text: str) -> None:
        self.comments.append({"task_id": task_id, "body": text})

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

        qa_payload = next(payload for payload in client.payloads if payload["id"] == "new-1")
        qa_payload.update(id="qa", status="done", summary="qa passed")
        second = service.handle_terminal_event({"event_id": "e2", "task_id": "qa", "kind": "completed"})
        self.assertEqual(second.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.created[1]["assignee"], "ceo-agent")
        self.assertEqual(client.created[1]["parent_task_ids"], ("r", "risk", "qa"))

    def test_reclaimed_does_not_wake_supervisor(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "reclaimed-1", "task_id": "r", "kind": "reclaimed"}
            )
        )
        self.assertEqual(client.created, [])
        self.assertEqual(client.comments, [])

    def test_one_terminal_child_does_not_synthesize_before_sibling(self) -> None:
        client = FakeClient()
        client.payloads[1]["status"] = "running"
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "r-only", "task_id": "r", "kind": "completed"}
            )
        )
        self.assertEqual(client.created, [])

    def test_concurrent_sibling_events_only_wake_once(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        events = [
            {"event_id": "r-concurrent", "task_id": "r", "kind": "completed"},
            {"event_id": "risk-concurrent", "task_id": "risk", "kind": "completed"},
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            decisions = list(executor.map(service.handle_terminal_event, events))

        self.assertEqual(sum(decision is not None for decision in decisions), 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["assignee"], "qa-department")

    def test_duplicate_event_is_idempotent_across_service_restart(self) -> None:
        client = FakeClient()
        event = {"event_id": "duplicate-1", "task_id": "r", "kind": "completed"}

        first = CeoSupervisorService(client).handle_terminal_event(event)
        second = CeoSupervisorService(client).handle_terminal_event(event)

        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertIsNone(second)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            sum("event=duplicate-1" in comment["body"] and "state=done" in comment["body"] for comment in client.comments),
            1,
        )

    def test_restart_preserves_wakeup_guard(self) -> None:
        client = FakeClient()
        client.comments = [
            {
                "task_id": "root",
                "body": f"hgfinance.ceo-supervisor.wakeup.v1 event=old-{i} state=done action=NONE",
            }
            for i in range(8)
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "ninth", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])

    def test_invalid_persisted_assignee_aborts_only_workflow(self) -> None:
        client = FakeClient()
        client.payloads[1]["assignee"] = "risk-department"

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "bad-assignee", "task_id": "r", "kind": "completed"}
        )

        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])

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
