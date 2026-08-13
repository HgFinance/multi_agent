"""Non-binding projection of a completed CEO synthesis into Notion."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.terminal_projection_utils import (
    action,
    ids_from,
    iso_timestamp,
    merged_run_metadata,
    summary,
    task_id,
    terminal_success,
    workflow_mode,
    workflow_role,
    workflow_root,
)

logger = logging.getLogger(__name__)
PROJECTION_MARKER = "hgfinance.ceo-notion-projection.v1"


class NotionProjectionTransport(Protocol):
    def query_projection(self, database_id: str, projection_key: str) -> Sequence[Mapping[str, Any]]: ...

    def create_page(
        self,
        database_id: str,
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


class NotionProjectionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NotionHttpTransport:
    version = "2022-06-28"

    def __init__(self, token: str) -> None:
        self.token = token

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"https://api.notion.com/v1/{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                decoded = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read())
            except (TypeError, ValueError):
                detail = str(exc)
            raise NotionProjectionError(str(detail), status=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise NotionProjectionError(str(exc)) from exc
        if not isinstance(decoded, Mapping):
            raise NotionProjectionError("Notion returned a non-object response")
        return decoded

    def query_projection(self, database_id: str, projection_key: str) -> Sequence[Mapping[str, Any]]:
        response = self._post(
            f"databases/{database_id}/query",
            {
                "filter": {
                    "property": "projection_key",
                    "rich_text": {"equals": projection_key},
                },
                "page_size": 1,
            },
        )
        results = response.get("results", [])
        return results if isinstance(results, Sequence) else ()

    def create_page(
        self,
        database_id: str,
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return self._post(
            "pages",
            {
                "parent": {"database_id": database_id},
                "properties": dict(properties),
                "children": list(children),
            },
        )


@dataclass(frozen=True)
class CeoSynthesisProjectionResult:
    status: str
    projection_key: str | None = None
    page_id: str | None = None
    duplicate: bool = False
    retryable: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "projection_key": self.projection_key,
            "page_id": self.page_id,
            "duplicate": self.duplicate,
            "retryable": self.retryable,
            "error": self.error,
        }


def _rich_text(value: Any) -> dict[str, Any]:
    text = str(value or "")
    return {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}


def _title(value: Any) -> dict[str, Any]:
    text = str(value or "CEO Synthesis")
    return {"title": [{"type": "text", "text": {"content": text[:1900]}}]}


class CeoNotionProjection:
    """Observe terminal synthesis without changing the workflow decision."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        transport: NotionProjectionTransport | None = None,
        kanban_client: Any | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.transport = transport
        self.kanban_client = kanban_client

    def _comment_marker(self, projection_key: str) -> str:
        return f"{PROJECTION_MARKER} projection_key={projection_key} status=created"

    def _has_comment_marker(self, task: Mapping[str, Any], projection_key: str) -> bool:
        marker = self._comment_marker(projection_key)
        comments = task.get("comments")
        if isinstance(comments, Sequence) and not isinstance(comments, (str, bytes, bytearray)):
            return any(marker in str(item.get("body") if isinstance(item, Mapping) else item) for item in comments)
        return marker in str(task.get("comment") or "")

    def _workflow_fields(
        self,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        metadata = merged_run_metadata(task)
        root_task = next(
            (item for item in workflow_tasks if task_id(item) == root_task_id),
            {},
        )
        root_body = str(root_task.get("body") or "")
        original_query = ""
        if "## User request" in root_body:
            original_query = root_body.split("## User request", 1)[1].strip()
        primary = [
            item
            for item in workflow_tasks
            if workflow_root(item) == root_task_id and workflow_role(item) == "primary"
        ]
        qa = [
            item
            for item in workflow_tasks
            if workflow_root(item) == root_task_id
            and (workflow_role(item) == "qa" or action(item) == "RUN_QA")
        ]
        departments = list(
            dict.fromkeys(
                str(item.get("assignee"))
                for item in primary
                if item.get("assignee")
            )
        )
        declared_departments = metadata.get("selected_departments")
        if declared_departments:
            departments = list(ids_from(declared_departments))
        primary_ids = list(ids_from(metadata.get("primary_task_ids"))) or [task_id(item) for item in primary]
        qa_ids = list(ids_from(metadata.get("qa_task_ids"))) or [task_id(item) for item in qa]
        return {
            "root_task_id": root_task_id,
            "synthesis_task_id": task_id(task),
            "original_query": str(
                metadata.get("original_query") or metadata.get("query") or original_query
            ),
            "final_answer": summary(task, metadata),
            "selected_departments": departments,
            "workflow_mode": workflow_mode(task) or str(metadata.get("workflow_mode") or "analysis"),
            "primary_task_ids": primary_ids,
            "qa_task_ids": qa_ids,
            "created_at": iso_timestamp(task.get("created_at")),
            "completed_at": iso_timestamp(task.get("completed_at") or task.get("finished_at")),
            "projection_key": f"ceo-synthesis:{root_task_id}:{task_id(task)}",
        }

    def project(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if workflow_role(task) != "synthesis" or action(task) != "SYNTHESIZE":
            return CeoSynthesisProjectionResult("skipped").as_dict()
        if not terminal_success(task):
            return CeoSynthesisProjectionResult("skipped").as_dict()
        if workflow_root(task) not in {None, root_task_id}:
            return CeoSynthesisProjectionResult("skipped").as_dict()
        database_id = str(self.env.get("NOTION_CEO_DB") or "")
        token = str(self.env.get("NOTION_TOKEN") or "")
        fields = self._workflow_fields(root_task_id, task, workflow_tasks)
        key = fields["projection_key"]
        if not database_id or not token:
            return CeoSynthesisProjectionResult(
                "failed", projection_key=key, retryable=True, error="NOTION_TOKEN/NOTION_CEO_DB missing"
            ).as_dict()
        transport = self.transport or _NotionHttpTransport(token)
        try:
            existing = transport.query_projection(database_id, key)
        except NotionProjectionError as exc:
            if exc.status != 400:
                logger.warning("ceo_notion_projection_query_failed", extra={"error": str(exc)})
                return CeoSynthesisProjectionResult(
                    "failed", projection_key=key, retryable=True, error=str(exc)
                ).as_dict()
            # A 400 generally means the optional projection_key property is
            # absent in an older Notion DB; use the durable Kanban marker below.
            existing = ()
        except Exception as exc:  # noqa: BLE001 - projection must not affect workflow
            logger.warning("ceo_notion_projection_query_failed", extra={"error": str(exc)})
            return CeoSynthesisProjectionResult(
                "failed", projection_key=key, retryable=True, error=str(exc)
            ).as_dict()
        if existing:
            page_id = str(existing[0].get("id") or "") if isinstance(existing[0], Mapping) else None
            return CeoSynthesisProjectionResult("duplicate", key, page_id, duplicate=True).as_dict()
        refreshed_task = task
        if self.kanban_client is not None and self._has_comment_marker(task, key) is False:
            try:
                refreshed_task = self.kanban_client.show(task_id(task))
            except Exception:  # noqa: BLE001 - fallback is best effort
                refreshed_task = task
        if self._has_comment_marker(refreshed_task, key):
            return CeoSynthesisProjectionResult("duplicate", key, duplicate=True).as_dict()
        report = (
            "# CEO Final Synthesis\n\n"
            f"- Root task: `{fields['root_task_id']}`\n"
            f"- Synthesis task: `{fields['synthesis_task_id']}`\n"
            f"- Selected departments: {', '.join(fields['selected_departments']) or 'none'}\n"
            f"- Workflow mode: `{fields['workflow_mode']}`\n\n"
            f"{fields['final_answer']}"
        )
        properties = {
            "제목": _title(f"CEO Synthesis · {root_task_id}"),
            "projection_key": _rich_text(key),
            "root_task_id": _rich_text(root_task_id),
            "synthesis_task_id": _rich_text(task_id(task)),
            "original_query": _rich_text(fields["original_query"]),
            "selected_departments": _rich_text(json.dumps(fields["selected_departments"], ensure_ascii=False)),
            "workflow_mode": _rich_text(fields["workflow_mode"]),
            "primary_task_ids": _rich_text(json.dumps(fields["primary_task_ids"])),
            "qa_task_ids": _rich_text(json.dumps(fields["qa_task_ids"])),
            "created_at": _rich_text(fields["created_at"]),
            "completed_at": _rich_text(fields["completed_at"]),
        }
        children = markdown_to_notion_blocks(report)
        try:
            page = transport.create_page(database_id, properties, children)
        except NotionProjectionError as exc:
            if exc.status != 400:
                raise
            # Older CEO DBs may not yet have the optional projection fields.
            # Create a useful page with the established title/body fields and
            # use the durable Kanban comment marker for idempotency.
            page = transport.create_page(
                database_id,
                {"제목": _title(f"CEO Synthesis · {root_task_id}"), "서술": _rich_text(report)},
                children,
            )
        try:
            page_id = str(page.get("id") or "")
            if self.kanban_client is not None:
                try:
                    self.kanban_client.comment_task(task_id(task), self._comment_marker(key))
                except Exception as exc:  # noqa: BLE001 - page is already durable
                    logger.warning("ceo_notion_projection_marker_failed", extra={"error": str(exc)})
            return CeoSynthesisProjectionResult("created", key, page_id).as_dict()
        except Exception as exc:  # noqa: BLE001 - non-binding side effect
            logger.warning("ceo_notion_projection_failed", extra={"error": str(exc)})
            return CeoSynthesisProjectionResult("failed", key, retryable=True, error=str(exc)).as_dict()


__all__ = ["PROJECTION_MARKER", "CeoNotionProjection", "CeoSynthesisProjectionResult"]
