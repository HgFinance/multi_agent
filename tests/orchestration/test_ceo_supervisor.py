"""CEO closed-loop supervisor policy and Hermes boundary tests."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    ChildTaskState,
    HermesKanbanClient,
    HermesKanbanCommandError,
    SupervisorAction,
    SupervisorState,
    SupervisorValidationError,
    decide_supervisor,
    parse_supervisor_output,
)
from orchestration.ceo_workflow_scope import (
    WorkflowScopeViolation,
    build_root_body,
    build_scoped_task_body,
    infer_workflow_mode,
    primary_idempotency_key,
    selected_primary_profiles_from_body,
    selected_primary_profiles_from_task,
    validate_workflow_scope,
    workflow_mode_from_body,
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
    if "workflow_root_task_id=" not in body:
        body = f"workflow_root_task_id=root\n{body}" if body else "workflow_root_task_id=root"
    if "workflow_role=" not in body:
        role = "qa" if profile == "qa-department" else "primary"
        body = f"{body}\nworkflow_role={role}"
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

    def test_primary_results_ready_creates_async_qa_and_fast_synthesis(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "done",
                    body="workflow_role=primary",
                ),
                child(
                    "accounting",
                    "accounting-portfolio-department",
                    "done",
                    body="workflow_role=primary",
                ),
            ),
        )

        first = decide_supervisor(state)
        self.assertIsNotNone(first)
        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertEqual(first.reason, "primary_results_ready_async_audit")

        with_qa = SupervisorState(
            "root",
            state.children
            + (
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_role=qa",
                ),
            ),
        )
        second = decide_supervisor(with_qa)
        self.assertIsNotNone(second)
        self.assertEqual(second.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(second.reason, "primary_results_ready_fast_path")
        self.assertEqual(second.parent_task_ids, ("research", "accounting"))
        self.assertNotIn("qa", second.parent_task_ids)
        self.assertIn("QA runs independently", second.body)
        self.assertIn("not a synthesis prerequisite", second.body)
        self.assertNotIn("after QA", second.body.casefold())

    def test_replan_is_scope_bound_without_root_execution_dependency(self) -> None:
        client = FakeClient()
        client.payloads[0].update(status="blocked", block_reason="source unavailable")
        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "replan-1", "task_id": "r", "kind": "blocked"}
        )
        self.assertEqual(decision.action, SupervisorAction.CREATE_TASK)
        self.assertEqual(client.created, [])
        self.assertEqual(client.unblocked, ["r"])

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

    def test_analysis_synthesis_is_eligible_while_qa_runs(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("risk", "risk-management", "done"),
                    child("qa", "qa-department", "running"),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("r", "risk"))
        self.assertNotIn("qa", decision.parent_task_ids)

    def test_unselected_primary_does_not_trigger_retry_or_block(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("research", "research-department", "done", body="workflow_role=primary"),
                    child("quant", "quant-backtest-department", "failed", body="workflow_role=primary"),
                ),
                selected_primary_profiles=("research-department",),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(decision.parent_task_ids, ("research",))

    def test_binding_synthesis_keeps_qa_gate(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "running"),
                ),
                workflow_mode="binding",
            )
        )
        self.assertIsNone(decision)

    def test_analysis_qa_failure_does_not_block_response_synthesis(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "failed"),
                ),
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("r",))

    def test_analysis_qa_terminal_outcomes_do_not_enter_fast_path_failure(self) -> None:
        for qa_status in ("blocked", "crashed", "timed_out", "gave_up", "failed"):
            with self.subTest(qa_status=qa_status):
                decision = decide_supervisor(
                    SupervisorState(
                        "root",
                        (
                            child("r", "research-department", "done"),
                            child("qa", "qa-department", qa_status),
                        ),
                    )
                )
                self.assertIsNotNone(decision)
                self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
                self.assertEqual(decision.parent_task_ids, ("r",))

    def test_explicit_roles_prevent_control_or_qa_from_becoming_analysis(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "primary",
                    "accounting-portfolio-department",
                    "done",
                    body="workflow_role=primary",
                ),
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_role=qa",
                ),
                child(
                    "synthesis",
                    "ceo-agent",
                    "todo",
                    body="workflow_role=synthesis",
                ),
            ),
        )
        self.assertEqual([c.task_id for c in state.analysis_children], ["primary"])
        self.assertEqual([c.task_id for c in state.qa_children], ["qa"])

    def test_root_selection_is_machine_readable_and_legacy_prose_is_supported(self) -> None:
        self.assertEqual(
            selected_primary_profiles_from_body(
                "selected_primary_profiles=research-department,risk-management"
            ),
            ("research-department", "risk-management"),
        )
        self.assertEqual(
            selected_primary_profiles_from_body(
                "Dynamic departments selected:\n"
                "Research, Risk Management, and Accounting/Portfolio."
            ),
            (
                "research-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_root_selection_falls_back_to_root_comment(self) -> None:
        selected = selected_primary_profiles_from_task(
            {
                "id": "root",
                "body": "hgfinance.ceo-workflow-scope.v1\nworkflow_mode=analysis",
                "comments": [
                    {
                        "body": (
                            "selected_primary_profiles=research-department,"
                            "quant-backtest-department,risk-management,"
                            "accounting-portfolio-department"
                        )
                    }
                ],
            }
        )
        self.assertEqual(
            selected,
            (
                "research-department",
                "quant-backtest-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_precise_root_comment_overrides_legacy_body_prose(self) -> None:
        selected = selected_primary_profiles_from_task(
            {
                "id": "root",
                "body": (
                    "Dynamic departments selected:\n"
                    "Research and Risk Management.\n"
                    "After all primary tasks complete, run independent QA by default."
                ),
                "comments": [
                    {
                        "body": (
                            "selected_primary_profiles=research-department,"
                            "quant-backtest-department,risk-management,"
                            "accounting-portfolio-department"
                        )
                    }
                ],
            }
        )
        self.assertEqual(
            selected,
            (
                "research-department",
                "quant-backtest-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_selected_primary_set_excludes_foreign_and_background_tasks(self) -> None:
        selected = (
            "research-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        scoped = "workflow_root_task_id=root\nworkflow_role=primary"
        background = (
            "workflow_root_task_id=root\n"
            "workflow_plane=continuous_research\n"
            "workflow_role=background_research"
        )
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=scoped),
                child("risk", "risk-management", "done", body=scoped),
                child("accounting", "accounting-portfolio-department", "done", body=scoped),
                child(
                    "foreign",
                    "research-department",
                    "done",
                    body="workflow_root_task_id=other-root\nworkflow_role=primary",
                ),
                child("background", "continuous-research", "done", body=background),
            ),
            selected_primary_profiles=selected,
        )
        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(decision.parent_task_ids, ("research", "risk", "accounting"))

    def test_duplicate_primary_set_is_not_hidden_by_readiness_selector(self) -> None:
        selected = (
            "research-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(f"{profile}-{suffix}", profile, "done", body=body)
            for suffix in ("a", "b")
            for profile in selected
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)
        self.assertEqual(state.duplicate_primary_profiles, selected)
        self.assertEqual(state.ready_count, 0)
        self.assertFalse(state.primary_ready)
        with self.assertLogs("orchestration.adapters.ceo_supervisor", level="WARNING") as logs:
            self.assertIsNone(decide_supervisor(state))
        self.assertTrue(
            any("primary-duplicate-detected" in message for message in logs.output)
        )
        self.assertFalse(any("primary-ready" in message for message in logs.output))

    def test_readiness_is_unique_profile_count_and_never_exceeds_selected(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        state = SupervisorState(
            "root",
            tuple(
                child(f"{profile}-1", profile, "done", body="workflow_role=primary")
                for profile in selected
            ),
            selected_primary_profiles=selected,
        )
        self.assertEqual(state.ready_count, 4)
        self.assertLessEqual(state.ready_count, len(selected))
        self.assertTrue(state.primary_ready)

    def test_failed_primary_retry_is_not_ready_until_same_task_recovers(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        children = (
            child("research", "research-department", "running", body="workflow_role=primary"),
            child("quant", "quant-backtest-department", "done", body="workflow_role=primary"),
            child("risk", "risk-management", "done", body="workflow_role=primary"),
            child("accounting", "accounting-portfolio-department", "done", body="workflow_role=primary"),
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)
        self.assertEqual(state.ready_count, 3)
        self.assertFalse(state.primary_ready)
        self.assertIsNone(decide_supervisor(state))

    def test_failed_primary_retries_same_task_without_creating_duplicate(self) -> None:
        client = FakeClient()
        client.payloads[0].update(
            status="failed",
            runs=[{"outcome": "failed"}],
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "same-primary-retry", "task_id": "r", "kind": "crashed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.RETRY_TASK)
        self.assertEqual(client.unblocked, ["r"])
        self.assertEqual(client.created, [])

    def test_analysis_synthesis_waits_for_every_selected_primary(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=body),
                child("risk", "risk-management", "done", body=body),
            ),
            qa_required=False,
            selected_primary_profiles=selected,
        )

        self.assertEqual(
            state.missing_primary_profiles,
            ("quant-backtest-department", "accounting-portfolio-department"),
        )
        self.assertIsNone(decide_supervisor(state))

    def test_analysis_synthesis_requires_unique_complete_selected_set(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(profile.split("-")[0], profile, "done", body=body)
            for profile in selected
        )

        decision = decide_supervisor(
            SupervisorState(
                "root",
                children,
                qa_required=False,
                selected_primary_profiles=selected,
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            decision.parent_task_ids,
            tuple(item.task_id for item in children),
        )

    def test_analysis_synthesis_stays_async_while_qa_runs(self) -> None:
        selected = ("research-department", "risk-management")
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=body),
                child("risk", "risk-management", "done", body=body),
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_root_task_id=root\nworkflow_role=qa",
                ),
            ),
            selected_primary_profiles=selected,
        )

        decision = decide_supervisor(state)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertNotIn("qa", decision.parent_task_ids)

    def test_analysis_synthesis_includes_terminal_blocked_primary(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(
                profile.split("-")[0],
                profile,
                "blocked" if profile == "accounting-portfolio-department" else "done",
                body=body,
                block_reason=(
                    "financial statement unavailable"
                    if profile == "accounting-portfolio-department"
                    else ""
                ),
            )
            for profile in selected
        )

        decision = decide_supervisor(
            SupervisorState(
                "root",
                children,
                qa_required=False,
                selected_primary_profiles=selected,
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, tuple(item.task_id for item in children))
        self.assertIn("financial statement unavailable", decision.body)

    def test_four_selected_primary_parents_exclude_unmarked_duplicates(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        marked_body = "workflow_root_task_id=root\nworkflow_role=primary"
        unmarked_body = "workflow_root_task_id=root\nworkflow_role="
        children = tuple(
            child(f"marked-{index}", profile, "done", body=marked_body)
            for index, profile in enumerate(selected)
        ) + tuple(
            child(f"legacy-{index}", profile, "done", body=unmarked_body)
            for index, profile in enumerate(selected)
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)

        self.assertEqual(len(state.analysis_children), 4)
        first = decide_supervisor(state)
        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertEqual(first.parent_task_ids, tuple(f"marked-{i}" for i in range(4)))

        with_qa = SupervisorState(
            "root",
            children + (child("qa", "qa-department", "running", body="workflow_role=qa"),),
            selected_primary_profiles=selected,
        )
        synthesis = decide_supervisor(with_qa)
        self.assertEqual(synthesis.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            synthesis.parent_task_ids,
            tuple(f"marked-{i}" for i in range(4)),
        )

    def test_selected_set_waits_without_request_user_input_when_children_are_late(self) -> None:
        state = SupervisorState(
            "root",
            (),
            selected_primary_profiles=("research-department",),
        )
        self.assertIsNone(decide_supervisor(state))

    def test_primary_idempotency_key_is_stable(self) -> None:
        self.assertEqual(
            primary_idempotency_key("root", "research-department"),
            "root:primary:research-department",
        )

    def test_continuous_research_is_excluded_even_with_research_profile(self) -> None:
        background_tasks = tuple(
            child(
                f"background-{index}",
                "research-department",
                "done",
                body=(
                    "workflow_root_task_id=root\n"
                    "workflow_plane=continuous_research\n"
                    "workflow_role=background_research\n"
                    "hgfinance.continuous-research.v1"
                ),
            )
            for index in range(100)
        )
        state = SupervisorState(
            "root",
            (
                child("request-research", "research-department", "done"),
                child(
                    "foreign-primary",
                    "research-department",
                    "done",
                    body="workflow_root_task_id=old-root\nworkflow_role=primary",
                ),
                *background_tasks,
            ),
        )

        self.assertEqual(
            [item.task_id for item in state.analysis_children], ["request-research"]
        )
        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(decision.parent_task_ids, ("request-research",))

        fast_path = decide_supervisor(
            SupervisorState(
                "root",
                state.children
                + (child("qa", "qa-department", "running", body="workflow_role=qa"),),
            )
        )
        self.assertEqual(fast_path.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(fast_path.parent_task_ids, ("request-research",))

    def test_background_research_can_use_unregistered_future_profile(self) -> None:
        task = ChildTaskState.from_hermes(
            {
                "id": "background",
                "assignee": "research-intelligence",
                "status": "done",
                "body": (
                    "workflow_plane=continuous_research\n"
                    "workflow_role=background_research"
                ),
            }
        )
        self.assertTrue(task.is_background_research)
        self.assertFalse(task.is_analysis)

    def test_background_research_failure_does_not_enter_fast_loop(self) -> None:
        state = SupervisorState(
            "root",
            (
                child("request-research", "research-department", "done"),
                child(
                    "continuous-failure",
                    "research-intelligence",
                    "crashed",
                    body=(
                        "workflow_plane=continuous_research\n"
                        "workflow_role=background_research"
                    ),
                ),
            ),
        )

        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(decision.parent_task_ids, ("request-research",))



class FakeClient:
    def __init__(self) -> None:
        self.payloads = [
            {
                "id": "r",
                "assignee": "research-department",
                "status": "done",
                "summary": "research",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            },
            {
                "id": "risk",
                "assignee": "risk-management",
                "status": "done",
                "summary": "risk",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            },
        ]
        self.created: list[dict[str, object]] = []
        self.unblocked: list[str] = []
        self.blocked: list[str] = []
        self.comments: list[dict[str, str]] = []
        self.root_body = ""

    def workflow(self, task_id: str):
        return "root", tuple(self.payloads)

    def show(self, task_id: str):
        payload = {"id": task_id, "comments": list(self.comments)}
        if task_id == "root":
            payload["body"] = self.root_body
        return payload

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
    def test_startup_reconciliation_recovers_direct_root_and_blocked_primary(self) -> None:
        class ExistingRootClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                selected = (
                    "research-department",
                    "quant-backtest-department",
                    "risk-management",
                    "accounting-portfolio-department",
                )
                self.root_body = (
                    "hgfinance.ceo-workflow-scope.v1\n"
                    "workflow_role=planning\n"
                    "producer=ceo-hermes-direct\n"
                    "request_class=non-binding advisory analysis\n"
                    "selected_primary_profiles="
                    + ",".join(selected)
                )
                self.payloads = [
                    {
                        "id": f"{profile}-task",
                        "assignee": profile,
                        "status": "blocked" if profile.endswith("portfolio-department") else "done",
                        "summary": profile,
                        "block_reason": "data gap"
                        if profile.endswith("portfolio-department")
                        else "",
                        "body": "workflow_root_task_id=root\nworkflow_role=primary",
                    }
                    for profile in selected
                ]

            def list_tasks(self):
                return ({"id": "root", "status": "done", "body": self.root_body},)

            def show(self, task_id: str):
                payload = super().show(task_id)
                if task_id == "root":
                    payload.update(status="done", body=self.root_body)
                return payload

        client = ExistingRootClient()
        decisions = CeoSupervisorService(client).reconcile_existing_workflows()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, SupervisorAction.RUN_QA)
        self.assertEqual([item["assignee"] for item in client.created], ["qa-department", "ceo-agent"])
        self.assertEqual(
            set(client.created[1]["parent_task_ids"]),
            {
                "research-department-task",
                "quant-backtest-department-task",
                "risk-management-task",
                "accounting-portfolio-department-task",
            },
        )

    def test_terminal_child_creates_parallel_qa_and_synthesis(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)

        first = service.handle_terminal_event(
            {"event_id": "e1", "task_id": "r", "kind": "completed"}
        )

        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertEqual(client.created[0]["assignee"], "qa-department")
        self.assertEqual(client.created[0]["parent_task_ids"], ("r", "risk"))
        self.assertEqual(client.created[1]["assignee"], "ceo-agent")
        self.assertEqual(client.created[1]["parent_task_ids"], ("r", "risk"))
        self.assertNotIn("qa", client.created[1]["parent_task_ids"])
        self.assertIn("workflow_role=qa", client.created[0]["body"])
        self.assertIn("workflow_role=synthesis", client.created[1]["body"])
        self.assertEqual(
            sum(item["assignee"] == "ceo-agent" for item in client.created),
            1,
        )

    def test_synthesis_does_not_wait_for_qa_visibility_after_qa_create(self) -> None:
        class StaleWorkflowClient(FakeClient):
            def create_task(self, **kwargs):
                self.created.append(kwargs)
                self.comments.append(
                    {"task_id": "root", "body": "created supervisor task"}
                )
                return f"new-{len(self.created)}"

        client = StaleWorkflowClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {
                "event_id": "qa-create-visible-late",
                "task_id": "r",
                "kind": "completed",
            }
        )

        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["qa-department", "ceo-agent"],
        )
        self.assertNotIn("qa", client.created[1]["parent_task_ids"])

    def test_supervisor_restores_selected_set_from_root_comment(self) -> None:
        client = FakeClient()
        client.payloads.append(
            {
                "id": "accounting",
                "assignee": "accounting-portfolio-department",
                "status": "done",
                "summary": "accounting",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            }
        )
        client.comments = [
            {
                "task_id": "root",
                "body": (
                    "selected_primary_profiles=research-department,"
                    "risk-management,accounting-portfolio-department"
                ),
            }
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "selected-comment", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(
            client.created[0]["parent_task_ids"],
            ("r", "risk", "accounting"),
        )
        self.assertEqual(
            client.created[1]["parent_task_ids"],
            ("r", "risk", "accounting"),
        )

    def test_qa_create_refresh_prevents_duplicate_synthesis(self) -> None:
        """A sibling event may create synthesis while QA creation is in flight."""

        class ConcurrentSynthesisClient(FakeClient):
            def create_task(self, **kwargs):
                self.created.append(kwargs)
                if kwargs["assignee"] == "qa-department":
                    self.payloads.append(
                        {
                            "id": "qa-created",
                            "assignee": "qa-department",
                            "status": "ready",
                            "body": build_scoped_task_body(
                                "QA", "root", role="qa"
                            ),
                        }
                    )
                    # Model a concurrent sibling event that already created the
                    # response-plane task. The current handler must observe it
                    # on its post-create workflow refresh.
                    self.payloads.append(
                        {
                            "id": "synthesis-existing",
                            "assignee": "ceo-agent",
                            "status": "ready",
                            "body": build_scoped_task_body(
                                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE\n"
                                "CEO synthesis",
                                "root",
                                role="synthesis",
                            ),
                        }
                    )
                return {"id": f"created-{len(self.created)}"}

        client = ConcurrentSynthesisClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "qa-refresh", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["qa-department"],
        )

    def test_binding_synthesis_is_parented_by_completed_qa(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body(
            "Samsung order request", "req-binding", workflow_mode="binding"
        )
        client.payloads.append(
            {
                "id": "qa",
                "assignee": "qa-department",
                "status": "done",
                "summary": "qa passed",
                "body": "workflow_root_task_id=root\nworkflow_role=qa",
            }
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "binding-qa-done", "task_id": "qa", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.created[0]["parent_task_ids"], ("qa",))
        self.assertIn("workflow_mode=binding", client.created[0]["body"])

    def test_root_body_declares_scope_only_planning_contract(self) -> None:
        body = build_root_body("Samsung", "req-1")
        self.assertIn("root_task_role=scope_and_planning", body)
        self.assertIn("primary_execution_parent=none", body)
        self.assertIn("planning_terminal_state=done_after_child_creation", body)
        self.assertNotIn("child_parent_required=current_root_task_id", body)
        self.assertIn("workflow_mode=analysis", body)
        self.assertIn("analysis_response_rule=primary_results_ready_allows_immediate_ceo_synthesis", body)
        self.assertNotIn("then CEO synthesis", body)
        self.assertIn("response_plane=primary_results_ready", body)
        self.assertIn("governance_plane=async_qa", body)
        self.assertIn("qa_is_not_synthesis_prerequisite=true", body)
        self.assertNotIn("QA then synthesis", body)

    def test_binding_mode_is_explicit_and_legacy_scoped_roots_remain_gated(self) -> None:
        self.assertEqual(infer_workflow_mode("삼성전자 분석"), "analysis")
        self.assertEqual(infer_workflow_mode("삼성전자 주문을 집행해"), "binding")
        self.assertEqual(
            infer_workflow_mode("삼성전자 주문이나 집행은 하지 말고 분석만 해줘"),
            "analysis",
        )
        self.assertEqual(workflow_mode_from_body(build_root_body("q", "r")), "analysis")
        self.assertEqual(workflow_mode_from_body("hgfinance.ceo-workflow-scope.v1"), "binding")

    def test_direct_non_binding_root_without_workflow_mode_is_analysis(self) -> None:
        body = (
            "hgfinance.ceo-workflow-scope.v1\n"
            "workflow_role=planning\n"
            "request_class=non-binding advisory analysis\n"
            "selected_primary_profiles=research-department"
        )
        self.assertEqual(workflow_mode_from_body(body), "analysis")

    def test_invalid_workflow_mode_aborts_only_current_workflow(self) -> None:
        client = FakeClient()
        client.root_body = (
            "hgfinance.ceo-workflow-scope.v1\n"
            "workflow_mode=unsupported\n"
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "invalid-mode", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])
        self.assertTrue(
            any("ceo-workflow-scope-error" in comment["body"] for comment in client.comments)
        )

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

    def test_planning_root_terminal_event_does_not_wake_supervisor(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body("Samsung", "req-1")
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "root-done", "task_id": "root", "kind": "completed"}
            )
        )
        self.assertEqual(client.created, [])

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
        self.assertEqual(len(client.created), 2)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["qa-department", "ceo-agent"],
        )

    def test_duplicate_event_is_idempotent_across_service_restart(self) -> None:
        client = FakeClient()
        event = {"event_id": "duplicate-1", "task_id": "r", "kind": "completed"}

        first = CeoSupervisorService(client).handle_terminal_event(event)
        second = CeoSupervisorService(client).handle_terminal_event(event)

        self.assertEqual(first.action, SupervisorAction.RUN_QA)
        self.assertIsNone(second)
        self.assertEqual(len(client.created), 2)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["qa-department", "ceo-agent"],
        )
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
        self.assertEqual(decision.action, SupervisorAction.RUN_QA)
        self.assertEqual(client.blocked, [])

    def test_retry_budget_still_blocks_after_restart(self) -> None:
        client = FakeClient()
        client.payloads[0].update(status="failed")
        client.comments = [
            {
                "task_id": "root",
                "body": (
                    "hgfinance.ceo-supervisor.wakeup.v1 "
                    f"event=old-{i} state=done action=RETRY_TASK "
                    "budget_consumed=true"
                ),
            }
            for i in range(8)
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "ninth-retry", "task_id": "r", "kind": "crashed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])

    def test_restart_preserves_retry_guard_from_hermes_runs(self) -> None:
        client = FakeClient()
        client.payloads[0].update(
            status="failed",
            runs=[{"outcome": "failed"}, {"outcome": "failed"}],
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "failed-after-restart", "task_id": "r", "kind": "crashed"}
        )

        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.unblocked, [])
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

    def test_invalid_hermes_json_is_a_command_error(self) -> None:
        import subprocess

        completed = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout="not-json", stderr=""
        )

        def runner(*args, **kwargs):
            return completed

        with self.assertRaises(HermesKanbanCommandError):
            HermesKanbanClient(runner=runner).show("r")

    def test_scope_marker_discovers_parentless_primary_tasks(self) -> None:
        import json
        import subprocess

        root = "t_aaaaaaaa"
        research = "t_bbbbbbbb"
        risk = "t_cccccccc"
        qa = "t_dddddddd"
        synthesis = "t_eeeeeeee"
        old_research = "t_ffffffff"
        payloads = {
            root: {
                "id": root,
                "assignee": "ceo-agent",
                "status": "done",
                "body": build_root_body("Samsung", "req-1"),
                "parents": [],
                "children": [],
            },
            research: {
                "id": research,
                "assignee": "research-department",
                "status": "done",
                "body": build_scoped_task_body("research", root, role="primary"),
                "parents": [],
                "children": [],
            },
            risk: {
                "id": risk,
                "assignee": "risk-management",
                "status": "done",
                "body": build_scoped_task_body("risk", root, role="primary"),
                "parents": [],
                "children": [],
            },
            qa: {
                "id": qa,
                "assignee": "qa-department",
                "status": "ready",
                "body": build_scoped_task_body("qa", root, role="qa"),
                "parents": [research, risk],
                "children": [],
            },
            synthesis: {
                "id": synthesis,
                "assignee": "ceo-agent",
                "status": "todo",
                "body": build_scoped_task_body("synthesis", root, role="synthesis"),
                "parents": [qa],
                "children": [],
            },
            old_research: {
                "id": old_research,
                "assignee": "research-department",
                "status": "done",
                "body": build_scoped_task_body(
                    "old workflow", "t_11111111", role="primary"
                ),
                "parents": [],
                "children": [],
            },
        }

        def runner(args, **kwargs):
            command = list(args)
            if command[1:3] == ["kanban", "list"]:
                stdout = json.dumps(list(payloads.values()))
            else:
                task_id = command[3]
                stdout = json.dumps({"task": payloads[task_id]})
            return subprocess.CompletedProcess(args, 0, stdout, "")

        client = HermesKanbanClient(runner=runner)
        discovered_root, children = client.workflow(research)
        self.assertEqual(discovered_root, root)
        self.assertEqual(
            {task["id"] for task in children}, {research, risk, qa, synthesis}
        )
        self.assertNotIn(old_research, {task["id"] for task in children})
        self.assertEqual(payloads[research]["parents"], [])
        self.assertEqual(payloads[risk]["parents"], [])
        self.assertEqual(payloads[qa]["parents"], [research, risk])
        self.assertEqual(payloads[synthesis]["parents"], [qa])

    def test_primary_scope_task_cannot_depend_on_scope_root(self) -> None:
        root = "t_aaaaaaaa"
        primary = build_scoped_task_body("research", root, role="primary")
        with self.assertRaises(WorkflowScopeViolation):
            validate_workflow_scope(
                root_task_id=root,
                root_payload={"id": root, "body": build_root_body("q", "req")},
                descendants=[
                    {
                        "id": "t_bbbbbbbb",
                        "assignee": "research-department",
                        "body": primary,
                        "parents": [root],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
