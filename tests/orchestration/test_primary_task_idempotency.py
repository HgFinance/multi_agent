from __future__ import annotations

import importlib.util

from orchestration.primary_task_idempotency import (
    find_existing_scoped_primary,
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
    assert first.count("new_tid = kb.create_task(") == 2
