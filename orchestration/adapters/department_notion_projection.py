"""Non-binding Notion projection for completed department tasks.

Only Trading and Quant are projected here. Other departments already own
native Notion reporters and must not be duplicated by this observer.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache
from orchestration.adapters.terminal_projection_utils import (
    iso_timestamp,
    merged_run_metadata,
    safe_json,
    summary,
    task_body,
    task_id,
    terminal_success,
    workflow_root,
)
from orchestration.canonical_profiles import department_for_canonical_profile


DEFAULT_DATABASES = {
    "trading": "2903de9e2a7b4f6d967f709e6640ec16",
    "quant-backtest": "2adc190ac33d4d639a90f1ab86087f42",
}

DATABASE_ENV = {
    "trading": "NOTION_TRADING_DB",
    "quant-backtest": "NOTION_QUANT_BACKTEST_DB",
}

TITLE_PROPERTY = {
    "trading": "제목",
    "quant-backtest": "전략·백테스트 run",
}

PROJECTION_MARKER = "hgfinance.department-notion-projection.v1"


class DepartmentNotionProjectionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NotionTransport:
    version = "2022-06-28"

    def __init__(self, token: str) -> None:
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.notion.com/v1/{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                decoded = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read())
            except Exception:
                detail = str(exc)
            raise DepartmentNotionProjectionError(
                str(detail), status=exc.code
            ) from exc
        except (OSError, ValueError) as exc:
            raise DepartmentNotionProjectionError(str(exc)) from exc

        if not isinstance(decoded, Mapping):
            raise DepartmentNotionProjectionError(
                "Notion returned a non-object response"
            )
        return decoded

    def database_schema(self, database_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"databases/{database_id}")

    def query_title(
        self,
        database_id: str,
        title_property: str,
        title: str,
    ) -> Sequence[Mapping[str, Any]]:
        response = self._request(
            "POST",
            f"databases/{database_id}/query",
            {
                "filter": {
                    "property": title_property,
                    "title": {"equals": title},
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
        return self._request(
            "POST",
            "pages",
            {
                "parent": {"database_id": database_id},
                "properties": dict(properties),
                "children": list(children),
            },
        )


@dataclass(frozen=True)
class DepartmentProjectionResult:
    status: str
    department: str | None = None
    task_id: str | None = None
    page_id: str | None = None
    duplicate: bool = False
    error: str | None = None


def _title(value: str) -> dict[str, Any]:
    return {
        "title": [
            {
                "type": "text",
                "text": {"content": value[:1900]},
            }
        ]
    }


def _rich_text(value: Any) -> dict[str, Any]:
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": str(value or "")[:1900]},
            }
        ]
    }


def _date(value: Any) -> dict[str, Any] | None:
    stamp = iso_timestamp(value)
    if not stamp:
        return None
    return {"date": {"start": stamp}}


def _department(task: Mapping[str, Any]) -> str | None:
    profile = str(
        task.get("assignee")
        or task.get("profile")
        or task.get("assigned_to")
        or ""
    ).strip()
    if not profile:
        return None

    try:
        department = department_for_canonical_profile(profile)
    except Exception:
        return None

    if department == "quant":
        return "quant-backtest"
    return department


def _task_title(task: Mapping[str, Any], department: str) -> str:
    tid = task_id(task)
    raw = str(
        task.get("title")
        or task.get("name")
        or task.get("subject")
        or ""
    ).strip()

    if not raw:
        raw = (
            "Trading department result"
            if department == "trading"
            else "Quant backtest result"
        )

    return f"{tid} · {raw}"[:1900]


def _body_markdown(
    *,
    task: Mapping[str, Any],
    root_task_id: str,
    department: str,
    result_text: str,
) -> str:
    metadata = merged_run_metadata(task)
    original_instruction = task_body(task)

    safe_metadata = safe_json(metadata)

    parts = [
        f"# Department Task Result",
        "",
        f"- Task ID: `{task_id(task)}`",
        f"- Workflow Root Task ID: `{root_task_id}`",
        f"- Department: `{department}`",
        f"- Status: `{task.get('status') or ''}`",
        "",
        "## Original Instruction",
        "",
        original_instruction or "(empty)",
        "",
        "## Result",
        "",
        result_text or "(empty)",
    ]

    if safe_metadata:
        parts.extend(
            [
                "",
                "## Terminal Metadata",
                "",
                "```json",
                json.dumps(
                    safe_metadata,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )[:12000],
                "```",
            ]
        )

    return "\n".join(parts)


class DepartmentNotionProjection:
    """Project terminal Trading/Quant task output into existing Notion DBs."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        transport: Any | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.transport = transport
        self._transport_token: str | None = None
        self._schema_cache = BoundedNotionSchemaCache()

    def _transport_for(self, token: str) -> Any:
        if self.transport is None or (
            self._transport_token is not None
            and self._transport_token != token
        ):
            self.transport = _NotionTransport(token)
            self._transport_token = token
        return self.transport

    def _schema_for(
        self,
        transport: Any,
        database_id: str,
    ) -> tuple[Mapping[str, Any], bool]:
        return self._schema_cache.get(
            database_id,
            lambda: transport.database_schema(database_id),
        )

    def project(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]] = (),
        event: Mapping[str, Any] | None = None,
    ) -> DepartmentProjectionResult:
        del workflow_tasks, event

        tid = task_id(task)
        department = _department(task)

        if department not in {"trading", "quant-backtest"}:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
            )

        if not terminal_success(task):
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
            )

        declared_root = workflow_root(task)
        if declared_root and declared_root != root_task_id:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
                error="workflow_root_mismatch",
            )

        token = str(self.env.get("NOTION_TOKEN") or "").strip()
        db_env = DATABASE_ENV[department]
        database_id = str(
            self.env.get(db_env)
            or DEFAULT_DATABASES[department]
        ).strip()

        if not token:
            return DepartmentProjectionResult(
                "failed",
                department=department,
                task_id=tid,
                error="NOTION_TOKEN missing",
            )

        transport = self._transport_for(token)

        schema, schema_cache_hit = self._schema_for(transport, database_id)
        properties_schema = schema.get("properties") or {}
        title_property = TITLE_PROPERTY[department]
        schema_mismatch = (
            title_property not in properties_schema
            or not isinstance(properties_schema[title_property], Mapping)
            or properties_schema[title_property].get("type") != "title"
        )

        if schema_mismatch and schema_cache_hit:
            # A cached schema may be stale after a Notion property migration.
            # Invalidate and perform one authoritative fresh read before
            # failing closed on the same projection.
            self._schema_cache.invalidate(database_id)
            schema, _ = self._schema_for(transport, database_id)
            properties_schema = schema.get("properties") or {}
            schema_mismatch = (
                title_property not in properties_schema
                or not isinstance(properties_schema[title_property], Mapping)
                or properties_schema[title_property].get("type") != "title"
            )

        if schema_mismatch:
            self._schema_cache.invalidate(database_id)
            return DepartmentProjectionResult(
                "failed",
                department=department,
                task_id=tid,
                error=f"title property missing or incompatible: {title_property}",
            )

        title = _task_title(task, department)

        try:
            existing = transport.query_title(
                database_id,
                title_property,
                title,
            )
        except DepartmentNotionProjectionError as exc:
            if exc.status == 400:
                self._schema_cache.invalidate(database_id)
            raise

        if existing:
            return DepartmentProjectionResult(
                "duplicate",
                department=department,
                task_id=tid,
                duplicate=True,
            )

        metadata = merged_run_metadata(task)
        result_text = summary(task, metadata)

        props: dict[str, Any] = {
            title_property: _title(title),
        }

        if "서술" in properties_schema:
            props["서술"] = _rich_text(result_text)

        if "원본 리포트" in properties_schema:
            props["원본 리포트"] = _rich_text(result_text)

        created = (
            task.get("completed_at")
            or task.get("updated_at")
            or task.get("created_at")
        )
        if "생성 시각" in properties_schema:
            date_value = _date(created)
            if date_value is not None:
                props["생성 시각"] = date_value

        # Domain IDs are never repurposed as Kanban IDs.
        for key in ("trade_case_id", "trace_id"):
            value = metadata.get(key) or task.get(key)
            if value and key in properties_schema:
                props[key] = _rich_text(value)

        # Quant metrics are written only when explicitly present.
        if department == "quant-backtest":
            metric_map = {
                "Sharpe": ("sharpe", "sharpe_ratio"),
                "MDD": ("mdd", "max_drawdown"),
                "수익률": ("return", "return_rate", "total_return"),
            }
            for notion_name, candidates in metric_map.items():
                if notion_name not in properties_schema:
                    continue
                value = next(
                    (
                        metadata.get(key)
                        for key in candidates
                        if metadata.get(key) is not None
                    ),
                    None,
                )
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    props[notion_name] = {"number": float(value)}

        body = _body_markdown(
            task=task,
            root_task_id=root_task_id,
            department=department,
            result_text=result_text,
        )

        children = markdown_to_notion_blocks(body)

        try:
            page = transport.create_page(
                database_id,
                props,
                children,
            )
        except DepartmentNotionProjectionError as exc:
            if exc.status == 400:
                self._schema_cache.invalidate(database_id)
            raise

        return DepartmentProjectionResult(
            "created",
            department=department,
            task_id=tid,
            page_id=str(page.get("id") or "") or None,
        )
