import unittest

from orchestration.adapters.ceo_supervisor import (
    ChildTaskState,
    SupervisorAction,
    SupervisorState,
    decide_supervisor,
)
from orchestration.ceo_workflow_scope import build_root_body
from orchestration.qa_contract import (
    canonical_qa_contract,
    split_planner_selection,
)


class QaContractTest(unittest.TestCase):
    def test_analysis_and_binding_have_separate_intent_flags(self) -> None:
        analysis = canonical_qa_contract(workflow_mode="analysis")
        binding = canonical_qa_contract(workflow_mode="binding")
        excluded = canonical_qa_contract(
            workflow_mode="analysis", body="qa_enabled=false\nqa_blocks_response=false"
        )

        self.assertEqual((analysis.qa_enabled, analysis.qa_blocks_response), (True, False))
        self.assertEqual((binding.qa_enabled, binding.qa_blocks_response), (True, True))
        self.assertEqual((excluded.qa_enabled, excluded.qa_blocks_response), (False, False))

    def test_legacy_false_preserves_explicit_async_marker_only(self) -> None:
        async_legacy = canonical_qa_contract(
            workflow_mode="analysis",
            body="governance_plane=async_qa\nqa_required=false",
        )
        excluded = canonical_qa_contract(
            workflow_mode="analysis", body="qa_required=false"
        )

        self.assertTrue(async_legacy.qa_enabled)
        self.assertFalse(async_legacy.qa_blocks_response)
        self.assertFalse(excluded.qa_enabled)

    def test_planner_qa_is_not_an_analysis_primary(self) -> None:
        primary, qa_requested = split_planner_selection(
            ("research-department", "qa-department", "risk-management")
        )

        self.assertEqual(primary, ("research-department", "risk-management"))
        self.assertTrue(qa_requested)

    def test_materialization_is_runtime_fact(self) -> None:
        state = SupervisorState(
            "root",
            (
                ChildTaskState.from_hermes(
                    {
                        "id": "qa",
                        "assignee": "qa-department",
                        "status": "running",
                        "body": "workflow_root_task_id=root\nworkflow_role=qa",
                    }
                ),
            ),
        )
        self.assertTrue(state.qa_materialized)
        self.assertFalse(state.qa_legacy_primary_present)

    def test_legacy_primary_is_observable_but_not_canonical_qa(self) -> None:
        state = SupervisorState(
            "root",
            (
                ChildTaskState.from_hermes(
                    {
                        "id": "legacy-qa",
                        "assignee": "qa-department",
                        "status": "running",
                        "body": "workflow_root_task_id=root\nworkflow_role=primary",
                    }
                ),
            ),
        )
        self.assertFalse(state.qa_materialized)
        self.assertTrue(state.qa_legacy_primary_present)

    def test_analysis_does_not_wait_for_qa_when_disabled(self) -> None:
        primary = ChildTaskState.from_hermes(
            {
                "id": "research",
                "assignee": "research-department",
                "status": "done",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
                "result": "usable evidence",
                "final_answer": "usable evidence",
            }
        )
        state = SupervisorState(
            "root",
            (primary,),
            workflow_mode="analysis",
            qa_enabled=False,
            qa_blocks_response=False,
            selected_primary_profiles=("research-department",),
        )

        self.assertEqual(decide_supervisor(state).action, SupervisorAction.SYNTHESIZE)

    def test_root_body_emits_canonical_flags(self) -> None:
        analysis = build_root_body("q", "req-analysis")
        binding = build_root_body("q", "req-binding", workflow_mode="binding")
        excluded = build_root_body("q", "req-excluded", qa_enabled=False)

        self.assertIn("qa_enabled=true", analysis)
        self.assertIn("qa_blocks_response=false", analysis)
        self.assertIn("qa_enabled=true", binding)
        self.assertIn("qa_blocks_response=true", binding)
        self.assertIn("qa_enabled=false", excluded)
        self.assertIn("qa_blocks_response=false", excluded)


if __name__ == "__main__":
    unittest.main()
