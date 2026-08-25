from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from apps.api import ceo_mirror_projection
from apps.api.ceo_mirror import CanonicalIngress, InMemoryMirrorStore


class _Node(SimpleNamespace):
    def role(self, *, root_task_id: str) -> str:
        del root_task_id
        return str(self.node_role)


def _workflow() -> SimpleNamespace:
    return SimpleNamespace(
        root_task_id="root-1",
        nodes=(
            _Node(
                task_id="dept-1",
                parents=("root-1",),
                status="completed",
                profile="research-department",
                summary="research complete",
                error=None,
                block_reason=None,
                run_outcome="success",
                is_qa=False,
                node_role="primary",
            ),
            _Node(
                task_id="synthesis-1",
                parents=("root-1",),
                status="done",
                profile="ceo-department",
                summary="final answer",
                error=None,
                block_reason=None,
                run_outcome="success",
                is_qa=False,
                node_role="synthesis",
            ),
        ),
    )


def test_background_reconciliation_projects_terminal_workflow_idempotently() -> None:
    store = InMemoryMirrorStore()
    request = CanonicalIngress(
        query="show status",
        request_id="projection-request-1",
        source="web",
        source_message_id="web:projection-1",
        actor_id="user-1",
    )
    store.claim_request(request)
    store.save_response(request.request_id, {"task_id": "root-1"})

    with patch.object(ceo_mirror_projection, "load_workflow", return_value=_workflow()):
        first = ceo_mirror_projection.reconcile_workflow_projections(store)
        second = ceo_mirror_projection.reconcile_workflow_projections(store)

    events = store.read_events(request.request_id)
    assert first == {"scanned": 1, "projected": 2, "failed": 0}
    assert second == {"scanned": 1, "projected": 2, "failed": 0}
    assert [event.event_type for event in events] == [
        "TASK_COMPLETED",
        "CEO_FINAL",
    ]
    assert len({event.event_id for event in events}) == 2


def test_in_memory_store_exposes_bounded_request_page() -> None:
    store = InMemoryMirrorStore()
    for index in range(3):
        request = CanonicalIngress(
            query=f"query-{index}",
            request_id=f"projection-request-{index}",
            source="web",
            source_message_id=f"web:projection-{index}",
            actor_id="user-1",
        )
        store.claim_request(request)

    assert store.list_request_ids(limit=2) == [
        "projection-request-1",
        "projection-request-2",
    ]
