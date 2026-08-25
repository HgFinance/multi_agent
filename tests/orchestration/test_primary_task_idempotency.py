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
    reject_invalid_primary_create,
    request_user_input_idempotency_key,
    scoped_primary_identity,
    validate_primary_create,
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
    assert scoped_primary_identity(
        "plain production body",
        "qa-department",
        idempotency_key="root:qa-department:primary",
    ) == ("root", "qa-department")


def test_role_and_key_conflicts_fail_closed_before_create() -> None:
    assert "conflicts" in str(
        reject_invalid_primary_create(
            body("root", role="qa"),
            "qa-department",
            idempotency_key="root:primary:qa-department",
        )
    )
    assert "conflicts" in str(
        reject_invalid_primary_create(
            body("root", role="primary"),
            "research-department",
            idempotency_key="root:qa:research-department",
        )
    )
    assert "not analysis-primary eligible" in str(
        reject_invalid_primary_create(
            "plain production body",
            "qa-department",
            idempotency_key="root:primary:qa-department",
        )
    )


def test_validate_primary_create_allows_governance_and_valid_primary() -> None:
    assert validate_primary_create(body("root", role="qa"), "qa-department") is None
    assert (
        validate_primary_create(
            body("root", role="primary"),
            "research-department",
            idempotency_key="root:primary:research-department",
        )
        is None
    )
    assert (
        validate_primary_create(
            body("root", role="primary"),
            "hr-department",
            idempotency_key="root:primary:hr-department",
        )
        is None
    )
    assert validate_primary_create("plain task", "research-department") is not None
    assert scoped_primary_identity(
        "plain production body",
        "qa-department",
        idempotency_key="root:primary:qa-department",
    ) == ("root", "qa-department")


def test_ceo_primary_create_requires_scope_marker() -> None:
    assert requires_scoped_primary_contract("plain task", "research-department")
    assert not requires_scoped_primary_contract(
        body("root"), "research-department"
    )
    assert not requires_scoped_primary_contract("plain task", "qa-department")


def test_primary_role_contract_excludes_governance_qa_only() -> None:
    assert is_analysis_primary_eligible("research-department")
    assert is_analysis_primary_eligible("RISK-MANAGEMENT")
    assert is_analysis_primary_eligible("hr-department")
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


def test_installed_ceo_create_guard_rejects_invalid_primary_before_create() -> None:
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
    try:
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
    except ValueError as e:
        return f"ERROR: {e}"

class Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, *, name, handler, **kwargs):
        self.handlers[name] = handler

registry = Registry()
registry.register(name="kanban_create", handler=_handle_create)
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
    exec(patched, namespace)
    registered_create = namespace["registry"].handlers["kanban_create"]
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
            qa_primary = registered_create(
                {
                    "title": "QA primary",
                    "body": qa_body,
                    "assignee": "qa-department",
                    "idempotency_key": "root:qa-department:primary",
                }
            )
            qa_primary_retry = registered_create(
                {
                    "title": "QA primary retry",
                    "body": qa_body,
                    "assignee": "qa-department",
                    "idempotency_key": "root:qa-department:primary",
                }
            )
            key_only_qa_primary = registered_create(
                {
                    "title": "QA primary without decorated body",
                    "body": "plain production body",
                    "assignee": "qa-department",
                    "idempotency_key": "key-only-root:qa-department:primary",
                }
            )
            explicit_role_conflict = registered_create(
                {
                    "title": "Conflicting QA role",
                    "body": "plain production body",
                    "assignee": "qa-department",
                    "workflow_role": "qa",
                    "idempotency_key": "key-only-root:primary:qa-department",
                }
            )
            governance = registered_create(
                {
                    "title": "Governance QA",
                    "body": body("root", role="qa"),
                    "assignee": "qa-department",
                }
            )
            research = registered_create(
                {
                    "title": "Research primary",
                    "body": body("mixed-root", role="primary"),
                    "assignee": "research-department",
                    "idempotency_key": "mixed-root:research-department:primary",
                }
            )
            risk = registered_create(
                {
                    "title": "Risk primary",
                    "body": body("mixed-root", role="primary"),
                    "assignee": "risk-management",
                    "idempotency_key": "mixed-root:risk-management:primary",
                }
            )

    assert "not analysis-primary eligible" in str(qa_primary)
    assert "not analysis-primary eligible" in str(qa_primary_retry)
    assert "not analysis-primary eligible" in str(key_only_qa_primary)
    assert "conflicts" in str(explicit_role_conflict)
    assert governance == "task-1"
    assert research == "task-2"
    assert risk == "task-3"
    assert [item["assignee"] for item in kanban.created] == [
        "qa-department",
        "research-department",
        "risk-management",
    ]
    assert "workflow_role=qa" in str(kanban.created[0]["body"])


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


def test_installed_cli_create_guard_rejects_primary_before_durable_create() -> None:
    module_path = "deploy/ceo-kanban/install_primary_idempotency.py"
    spec = importlib.util.spec_from_file_location("primary_cli_patch", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    native = """from __future__ import annotations

import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as ks

def _cmd_create(args):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title=args.title,
            body=args.body,
            assignee=args.assignee,
            created_by=args.created_by,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    return task_id

def kanban_command(args):
    handlers = {"create": _cmd_create}
    return handlers[args.command](args)
"""
    patched = module._install_cli(native)
    assert patched == module._install_cli(patched)
    compile(patched, "<installed-kanban-cli>", "exec")
    assert module.CLI_MARKER in patched

    class FakeKanban:
        def __init__(self) -> None:
            self.create_calls = 0

        def connect_closing(self):
            class Connection:
                def __enter__(self):
                    return object()

                def __exit__(self, *args):
                    return False

            return Connection()

        def create_task(self, connection, **kwargs):
            self.create_calls += 1
            return "task-created"

    fake_kb = FakeKanban()
    from types import ModuleType, SimpleNamespace

    fake_hermes_cli = ModuleType("hermes_cli")
    fake_kanban_swarm = ModuleType("hermes_cli.kanban_swarm")
    fake_hermes_cli.kanban_db = fake_kb
    fake_hermes_cli.kanban_swarm = fake_kanban_swarm
    namespace = {}
    import sys

    with patch.dict(
        sys.modules,
        {
            "hermes_cli": fake_hermes_cli,
            "hermes_cli.kanban_db": fake_kb,
            "hermes_cli.kanban_swarm": fake_kanban_swarm,
        },
    ):
        exec(patched, namespace)

    qa_primary = SimpleNamespace(
        title="QA primary",
        body="workflow_root_task_id=root\nworkflow_role=primary",
        assignee="qa-department",
        created_by="ceo-agent",
        idempotency_key="root:primary:qa-department",
    )
    governance_qa = SimpleNamespace(
        title="Governance QA",
        body="workflow_root_task_id=root\nworkflow_role=qa",
        assignee="qa-department",
        created_by="ceo-agent",
        idempotency_key=None,
    )

    qa_primary.command = "create"
    governance_qa.command = "create"
    assert namespace["kanban_command"](qa_primary) == 2
    assert fake_kb.create_calls == 0
    assert namespace["kanban_command"](governance_qa) == "task-created"
    assert fake_kb.create_calls == 1
