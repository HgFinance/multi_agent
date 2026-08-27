from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "reconcile_qa_observability.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "qa_observability_reconciliation", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
reconciliation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconciliation)


def _root() -> dict[str, object]:
    return {
        "id": "t_root",
        "status": "done",
        "body": "\n".join(
            (
                "workflow_role=root",
                "root_task_role=scope_and_planning",
                "request_id=req-1",
                "workflow_mode=analysis",
                "source=web",
            )
        ),
    }


def _synthesis(*, delivered: bool = True) -> dict[str, object]:
    delivery = '"delivery_status": "sent"' if delivered else '"delivery_status": "unconfirmed"'
    return {
        "id": "t_synthesis",
        "status": "done",
        "completed_at": 10,
        "result": "결과",
        "body": "\n".join(
            (
                "workflow_role=synthesis",
                "workflow_root_task_id=t_root",
                delivery,
            )
        ),
    }


def test_pending_trace_is_closed_from_terminal_kanban_evidence(monkeypatch) -> None:
    run = SimpleNamespace(
        id="run-pending",
        name="hgfinance.user-query",
        end_time=None,
        extra={"metadata": {"request_id": "req-1", "source": "web"}},
    )

    class FakeKanban:
        def __init__(self, **_kwargs):
            pass

        def list_tasks(self):
            return (_root(),)

        def show(self, task_id):
            assert task_id == "t_root"
            return _root()

        def workflow(self, root_id):
            assert root_id == "t_root"
            return root_id, (_root(), _synthesis())

    monkeypatch.setattr(reconciliation, "HermesKanbanClient", FakeKanban)
    monkeypatch.setattr(reconciliation, "query_runs", lambda *_args, **_kwargs: [run])
    monkeypatch.setattr(
        "orchestration.llm_observability.langsmith_enabled",
        lambda: True,
    )
    close = monkeypatch.context()
    with close:
        monkeypatch.setitem(
            __import__("sys").modules,
            "langsmith",
            SimpleNamespace(Client=lambda **_kwargs: SimpleNamespace()),
        )
        with patch(
            "orchestration.llm_observability.close_root_trace",
            return_value=True,
        ) as close_root:
            result = reconciliation.reconcile_pending_langsmith(
                environment={"HERMES_KANBAN_DB": "/tmp/kanban.db"},
            )

    assert result == {
        "discovered": 1,
        "closed": 1,
        "skipped": 0,
        "unresolved": 0,
        "errors": 0,
    }
    kwargs = close_root.call_args.kwargs
    assert kwargs["run_id"] == "run-pending"
    assert kwargs["root_id"] == "t_root"
    assert kwargs["task_id"] == "t_synthesis"
    assert kwargs["status"] == "completed"


def test_pending_trace_without_terminal_response_is_left_untouched(monkeypatch) -> None:
    run = SimpleNamespace(
        id="run-pending",
        name="hgfinance.user-query",
        end_time=None,
        extra={"metadata": {"request_id": "req-1", "source": "web"}},
    )

    class FakeKanban:
        def __init__(self, **_kwargs):
            pass

        def list_tasks(self):
            return (_root(),)

        def show(self, _task_id):
            return _root()

        def workflow(self, _root_id):
            return "t_root", (_root(),)

    monkeypatch.setattr(reconciliation, "HermesKanbanClient", FakeKanban)
    monkeypatch.setattr(reconciliation, "query_runs", lambda *_args, **_kwargs: [run])
    monkeypatch.setattr(
        "orchestration.llm_observability.langsmith_enabled",
        lambda: True,
    )
    with patch.dict(
        "sys.modules",
        {"langsmith": SimpleNamespace(Client=lambda **_kwargs: object())},
    ), patch("orchestration.llm_observability.close_root_trace") as close_root:
        result = reconciliation.reconcile_pending_langsmith(
            environment={"HERMES_KANBAN_DB": "/tmp/kanban.db"},
        )

    assert result == {
        "discovered": 1,
        "closed": 0,
        "skipped": 1,
        "unresolved": 0,
        "errors": 0,
    }
    close_root.assert_not_called()
