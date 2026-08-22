from __future__ import annotations

import importlib.util
import os
import tempfile
from unittest.mock import patch

from orchestration.primary_task_idempotency import (
    ensure_request_user_input_task,
    find_existing_scoped_primary,
    is_analysis_primary_eligible,
    requires_scoped_primary_contract,
    request_user_input_idempotency_key,
    scoped_primary_identity,
)


def body(root: str, *, role: str = "primary", producer: str = "ceo-hermes-direct") -> str:
    return "\n".join(
        (
            "hgfinance.ceo-workflow-scope.v1",
            f"workflow_root_task_id={root}",
            f"workflow_role={role}",
            f"producer={producer}",
        )
    )


def task(
    task_id: str,
    root: str,
    assignee: str,
    *,
    title: str = "primary",
    **extra: object,
) -> dict[str, object]:
    role = str(extra.pop("role", "primary"))
    return {
        "id": task_id,
        "title": title,
        "assignee": assignee,
        "body": body(
            root,
            role=role,
            producer=str(extra.pop("producer", "ceo-hermes-direct")),
        ),
        "created_at": extra.pop("created_at", 1),
        **extra,
    }


def test_first_scoped_primary_is_not_an_existing_task() -> None:
    assert find_existing_scoped_primary([], root_task_id="root", assignee="research-department") is None


def test_second_same_root_role_assignee_reuses_existing_regardless_of_status() -> None:
    existing = task("first", "root", "research-department", status="done")
    assert (
        find_existing_scoped_primary(
            [existing], root_task_id="root", assignee="research-department"
        )
        == "first"
    )


def test_identity_ignores_title_body_producer_but_not_scope_role_or_assignee() -> None:
    existing = task("first", "root", "research-department", title="old wording", producer="legacy")
    assert (
        find_existing_scoped_primary(
            [existing], root_task_id="root", assignee="research-department"
        )
        == "first"
    )
    assert find_existing_scoped_primary([existing], root_task_id="root", assignee="quant-backtest-department") is None
    assert find_existing_scoped_primary([existing], root_task_id="other", assignee="research-department") is None
    assert find_existing_scoped_primary(
        [task("control", "root", "research-department", role="control")],
        root_task_id="root",
        assignee="research-department",
    ) is None


def test_duplicate_history_reuses_oldest_durable_primary() -> None:
    tasks = [
        task("newer", "root", "research-department", created_at=20),
        task("older", "root", "research-department", created_at=10),
    ]
    assert find_existing_scoped_primary(tasks, root_task_id="root", assignee="research-department") == "older"


def test_scoped_identity_requires_canonical_marker_and_primary_role() -> None:
    assert scoped_primary_identity(body("root"), "research-department") == (
        "root",
        "research-department",
    )
    assert scoped_primary_identity("RUN_QA\nworkflow_root_task_id=root", "research-department") is None
    assert scoped_primary_identity(body("root", role="qa"), "research-department") is None


def test_ceo_primary_create_requires_scope_marker() -> None:
    assert requires_scoped_primary_contract("plain task", "research-department")
    assert not requires_scoped_primary_contract(
        body("root"), "research-department"
    )
    assert not requires_scoped_primary_contract("plain task", "qa-department")


def test_primary_role_contract_excludes_governance_qa_only() -> None:
    assert is_analysis_primary_eligible("research-department")
    assert is_analysis_primary_eligible("RISK-MANAGEMENT")
    assert not is_analysis_primary_eligible("qa-department")


def test_request_user_input_helper_is_durable_and_exactly_once() -> None:
    class FakeKanban:
        def __init__(self) -> None:
            self.tasks: list[dict[str, object]] = []
            self.created: list[dict[str, object]] = []

        def list_tasks(self, connection, **kwargs):
            return tuple(self.tasks)

        def create_task(self, connection, **kwargs):
            task_id = f"control-{len(self.created) + 1}"
            record = {"id": task_id, **kwargs}
            self.created.append(record)
            self.tasks.append(record)
            return task_id

    kanban = FakeKanban()
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(
            os.environ,
            {"HERMES_KANBAN_DB": os.path.join(tmp, "kanban.db")},
            clear=False,
        ):
            first = ensure_request_user_input_task(
                kanban,
                object(),
                root_task_id="root",
                created_by="ceo-agent",
            )
            second = ensure_request_user_input_task(
                kanban,
                object(),
                root_task_id="root",
                created_by="ceo-agent",
            )

    assert first == second == "control-1"
    assert len(kanban.created) == 1
    assert kanban.created[0]["assignee"] == "ceo-agent"
    assert kanban.created[0]["idempotency_key"] == request_user_input_idempotency_key("root")
    assert "workflow_role=control" in str(kanban.created[0]["body"])


def test_installed_ceo_create_guard_blocks_qa_primary_but_preserves_qa_role() -> None:
    module_path = "deploy/ceo-kanban/install_primary_idempotency.py"
    spec = importlib.util.spec_from_file_location("primary_patch_live_path", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    native = """from __future__ import annotations

import os

def _handle_create(args, **kw):
    title = args.get("title")
    body = args.get("body")
    assignee = args.get("assignee")
    parents = args.get("parents") or []
    tenant = args.get("tenant")
    priority = args.get("priority")
    session_id = args.get("session_id")
    idempotency_key = args.get("idempotency_key")
    kb, conn = _connect()
    if True:
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                idempotency_key=idempotency_key,
                initial_status="running",
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            return new_tid
"""
    patched = module._install(native)
    compile(patched, "<installed-kanban-tool>", "exec")

    class FakeKanban:
        def __init__(self) -> None:
            self.tasks: list[dict[str, object]] = []
            self.created: list[dict[str, object]] = []

        def list_tasks(self, connection, **kwargs):
            return tuple(self.tasks)

        def create_task(self, connection, **kwargs):
            task_id = f"task-{len(self.created) + 1}"
            record = {"id": task_id, **kwargs}
            self.created.append(record)
            self.tasks.append(record)
            return task_id

        def get_task(self, connection, task_id):
            return next(item for item in self.tasks if item["id"] == task_id)

    kanban = FakeKanban()
    namespace = {"_connect": lambda: (kanban, object())}
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(
            os.environ,
            {
                "HERMES_KANBAN_DB": os.path.join(tmp, "kanban.db"),
                "HERMES_PROFILE": "ceo-agent",
            },
            clear=False,
        ):
            qa_body = body("root", role="primary") + "\nworkflow_mode=analysis"
            first = exec(patched, namespace) or namespace["_handle_create"](
                {
                    "title": "QA primary",
                    "body": qa_body,
                    "assignee": "qa-department",
                    "idempotency_key": "root:qa-department:primary",
                }
            )
            second = namespace["_handle_create"](
                {
                    "title": "QA primary retry",
                    "body": qa_body,
                    "assignee": "qa-department",
                    "idempotency_key": "root:qa-department:primary",
                }
            )
            governance = namespace["_handle_create"](
                {
                    "title": "Governance QA",
                    "body": body("root", role="qa"),
                    "assignee": "qa-department",
                }
            )
            research = namespace["_handle_create"](
                {
                    "title": "Research primary",
                    "body": body("mixed-root", role="primary"),
                    "assignee": "research-department",
                    "idempotency_key": "mixed-root:research-department:primary",
                }
            )
            mixed_qa = namespace["_handle_create"](
                {
                    "title": "Mixed QA primary",
                    "body": body("mixed-root", role="primary"),
                    "assignee": "qa-department",
                    "idempotency_key": "mixed-root:qa-department:primary",
                }
            )
            risk = namespace["_handle_create"](
                {
                    "title": "Risk primary",
                    "body": body("mixed-root", role="primary"),
                    "assignee": "risk-management",
                    "idempotency_key": "mixed-root:risk-management:primary",
                }
            )

    assert first == second == "task-1"
    assert governance == "task-2"
    assert research == "task-3"
    assert mixed_qa == "task-4"
    assert risk == "task-5"
    assert [item["assignee"] for item in kanban.created] == [
        "ceo-agent",
        "qa-department",
        "research-department",
        "ceo-agent",
        "risk-management",
    ]
    assert "workflow_role=control" in str(kanban.created[0]["body"])
    assert "workflow_role=qa" in str(kanban.created[1]["body"])


def test_installer_patches_native_kanban_create_once() -> None:
    module_path = "deploy/ceo-kanban/install_primary_idempotency.py"
    spec = importlib.util.spec_from_file_location("primary_patch", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    native = """from __future__ import annotations

def _handle_create(args, **kw):
    body = args.get("body")
    assignee = args.get("assignee")
    idempotency_key = args.get("idempotency_key")
    kb, conn = _connect()
            new_tid = kb.create_task(
                conn,
                title=str(args["title"]).strip(),
                body=body,
                assignee=str(assignee),
            )
            new_task = kb.get_task(conn, new_tid)
            return new_tid
"""
    first = module._install(native)
    second = module._install(first)
    assert first == second
    assert "scoped_primary_create_lock" in first
    assert "requires_scoped_primary_contract" in first
    assert first.count("new_tid = kb.create_task(") == 2
