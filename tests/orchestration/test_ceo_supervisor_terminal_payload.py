from orchestration.adapters.ceo_supervisor import CeoSupervisorService, ChildTaskState


def test_explicit_response_accepts_projected_child_state() -> None:
    child = ChildTaskState(
        task_id="accounting",
        profile="accounting-portfolio-department",
        status="done",
        summary="internal handoff",
        result="user-ready accounting report",
    )

    assert (
        CeoSupervisorService._root_explicit_response_content(child)
        == "user-ready accounting report"
    )
