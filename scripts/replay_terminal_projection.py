#!/usr/bin/env python3
"""기존 terminal task를 재실행하지 않고 projection만 다시 적용한다.

이 스크립트는 Hermes의 읽기 전용 ``kanban show/list`` 경계만 사용한다.
task 생성, work/complete, dispatcher 호출, LLM 호출은 의도적으로 제공하지
않는다. 실제 side effect는 선택한 projection adapter에만 위임한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.ceo_supervisor import (
    HermesKanbanClient,
    HermesKanbanCommandError,
)
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.adapters.terminal_projection_utils import (
    action,
    merged_run_metadata,
    task_id,
    terminal_success,
    workflow_role,
    workflow_root,
)


class ReplayValidationError(ValueError):
    """Replay 대상이 durable projection 계약과 맞지 않는다."""


def _effective_environment(
    env: Mapping[str, str] | None = None,
    kanban_db: str | None = None,
) -> dict[str, str]:
    """Build the subprocess environment without opening the Kanban DB here."""

    effective = dict(env or os.environ)
    if kanban_db:
        effective["HERMES_KANBAN_DB"] = kanban_db
    return effective


def _latest_task_run_id(task: Mapping[str, Any]) -> str | None:
    """출력용 task_run 식별자를 읽되, 새 run을 만들지는 않는다."""

    runs = task.get("runs")
    candidates: list[Mapping[str, Any]] = []
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        candidates = [item for item in runs if isinstance(item, Mapping)]
    for candidate in reversed(candidates):
        value = (
            candidate.get("task_run_id")
            or candidate.get("run_id")
            or candidate.get("id")
        )
        if value:
            return str(value)

    task_run = task.get("task_run")
    if isinstance(task_run, Mapping):
        value = task_run.get("task_run_id") or task_run.get("run_id") or task_run.get("id")
        if value:
            return str(value)
    for key in ("task_run_id", "run_id"):
        if task.get(key):
            return str(task[key])
    return None


def _workflow_payloads(
    client: Any,
    *,
    root_task_id: str,
    task_id_value: str,
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    """읽기 전용 Kanban workflow snapshot을 가져오고 root 관계를 검증한다."""

    discovered_root, discovered_tasks = client.workflow(task_id_value)
    if str(discovered_root) != root_task_id:
        raise ReplayValidationError(
            f"task {task_id_value} belongs to root {discovered_root}, "
            f"not requested root {root_task_id}"
        )

    root = dict(client.show(root_task_id))
    target = next(
        (
            dict(item)
            for item in (root, *discovered_tasks)
            if task_id(item) == task_id_value
        ),
        None,
    )
    if target is None:
        raise ReplayValidationError(
            f"task {task_id_value} was not returned in root workflow {root_task_id}"
        )

    workflow_tasks = tuple(
        [root]
        + [dict(item) for item in discovered_tasks if task_id(item) != root_task_id]
    )
    marker_root = workflow_root(target)
    if marker_root != root_task_id:
        raise ReplayValidationError(
            f"task {task_id_value} has workflow_root_task_id={marker_root!r}"
        )
    if not terminal_success(target):
        raise ReplayValidationError(
            f"task {task_id_value} is not a successful terminal task "
            f"(status={target.get('status')!r}, outcome={target.get('outcome')!r})"
        )
    return root, target, workflow_tasks


def _validate_projection_type(projection_type: str, task: Mapping[str, Any]) -> None:
    role = workflow_role(task)
    task_action = action(task)
    if projection_type == "qa":
        if role != "qa" or task_action != "RUN_QA":
            raise ReplayValidationError(
                "QA replay requires workflow_role=qa and action=RUN_QA"
            )
        assignee = str(task.get("assignee") or "")
        if assignee and assignee != "qa-department":
            raise ReplayValidationError(
                f"QA replay requires qa-department assignee, got {assignee!r}"
            )
        return
    if role != "synthesis" or task_action != "SYNTHESIZE":
        raise ReplayValidationError(
            "Notion replay requires workflow_role=synthesis and action=SYNTHESIZE"
        )
    assignee = str(task.get("assignee") or "")
    if assignee and assignee != "ceo-agent":
        raise ReplayValidationError(
            f"Notion replay requires ceo-agent assignee, got {assignee!r}"
        )


class _DryRunAuditRepository:
    def persist_kanban_qa(self, record: Any) -> dict[str, Any]:
        return {"duplicate": False, "eval_run_id": record.eval_run_id, "dry_run": True}


class _DryRunNotionTransport:
    def query_projection(self, database_id: str, projection_key: str) -> Sequence[Mapping[str, Any]]:
        return ()

    def create_page(
        self,
        database_id: str,
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return {"id": "dry-run-page"}


def replay_terminal_projection(
    *,
    projection_type: str,
    root_task_id: str,
    task_id_value: str,
    client: Any | None = None,
    dry_run: bool = False,
    qa_repository: Any | None = None,
    notion_transport: Any | None = None,
    env: Mapping[str, str] | None = None,
    kanban_db: str | None = None,
) -> dict[str, Any]:
    """Read one existing workflow and invoke exactly one projection adapter."""

    if projection_type not in {"qa", "notion"}:
        raise ReplayValidationError(f"unsupported projection type: {projection_type}")
    if not root_task_id or not task_id_value:
        raise ReplayValidationError("root_task_id and task_id are required")

    effective_env = _effective_environment(env, kanban_db)
    replay_client = client or HermesKanbanClient(environment=effective_env)
    _root, task, workflow_tasks = _workflow_payloads(
        replay_client,
        root_task_id=root_task_id,
        task_id_value=task_id_value,
    )
    _validate_projection_type(projection_type, task)
    metadata = merged_run_metadata(task)

    if projection_type == "qa":
        projection = QaAuditProjection(
            repository=qa_repository or (_DryRunAuditRepository() if dry_run else None),
            kanban_client=None if dry_run else replay_client,
            env=effective_env,
        )
        projection_result = projection.project(
            root_task_id=root_task_id,
            task=task,
            workflow_tasks=workflow_tasks,
            event={"event_type": "TERMINAL_PROJECTION_REPLAY", "task_id": task_id_value},
        )
        projection_key = projection_result.get("projection_key")
        identity = projection_result.get("eval_run_id")
        detected_verdict = metadata.get("verdict") or metadata.get("qa_verdict")
        success_statuses = {"persisted", "duplicate"}
    else:
        projection = CeoNotionProjection(
            env=(
                effective_env
                or os.environ
                if not dry_run
                else {"NOTION_TOKEN": "dry-run", "NOTION_CEO_DB": "dry-run"}
            ),
            transport=notion_transport or (_DryRunNotionTransport() if dry_run else None),
            kanban_client=None if dry_run else replay_client,
        )
        projection_result = projection.project(
            root_task_id=root_task_id,
            task=task,
            workflow_tasks=workflow_tasks,
            event={"event_type": "TERMINAL_PROJECTION_REPLAY", "task_id": task_id_value},
        )
        projection_key = projection_result.get("projection_key")
        identity = projection_key
        detected_verdict = None
        success_statuses = {"created", "duplicate"}

    result = {
        "type": projection_type,
        "root_task_id": root_task_id,
        "task_id": task_id_value,
        "task_run_id": _latest_task_run_id(task),
        "projection_key": projection_key,
        "eval_run_id": identity if projection_type == "qa" else None,
        "detected_verdict": detected_verdict,
        "target_adapter": (
            "orchestration.adapters.qa_audit_projection.QaAuditProjection"
            if projection_type == "qa"
            else "orchestration.adapters.ceo_notion_projection.CeoNotionProjection"
        ),
        "dry_run": dry_run,
        "projection": projection_result,
    }
    if projection_result.get("status") not in success_statuses:
        raise ReplayValidationError(json.dumps(result, ensure_ascii=False, default=str))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", dest="projection_type", choices=("qa", "notion"), required=True)
    parser.add_argument("--root-task-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--kanban-db",
        help="Pin HERMES_KANBAN_DB for the read-only Hermes Kanban client.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = replay_terminal_projection(
            projection_type=args.projection_type,
            root_task_id=args.root_task_id,
            task_id_value=args.task_id,
            dry_run=args.dry_run,
            kanban_db=args.kanban_db,
        )
    except (ReplayValidationError, HermesKanbanCommandError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
