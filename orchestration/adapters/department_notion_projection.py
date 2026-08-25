"""Non-binding Notion projection for completed department tasks.

Trading and Quant keep their existing projection contract. Research and Risk
also have native reporters for their standalone department pipelines, but a
CEO/Kanban task is a separate execution boundary: when their database IDs are
explicitly wired into the Supervisor, this observer records that terminal
task once without importing or invoking the native reporter. That avoids a
duplicate cross-boundary upload while making the natural CEO path observable.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.notion_idempotency import (
    NotionIdempotency,
)
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
    "research": "NOTION_RESEARCH_DB",
    "risk": "NOTION_RISK_DB",
}

TITLE_PROPERTY = {
    "trading": "제목",
    "quant-backtest": "전략·백테스트 run",
    "research": "종목",
    "risk": "제목",
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
            raise DepartmentNotionProjectionError(str(detail), status=exc.code) from exc
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

    def update_page(
        self, page_id: str, properties: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._request(
            "PATCH", f"pages/{page_id}", {"properties": dict(properties)}
        )

    def retrieve_page(self, page_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"pages/{page_id}")

    def append_blocks(
        self, page_id: str, children: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        return self._request(
            "PATCH", f"blocks/{page_id}/children", {"children": list(children)}
        )


@dataclass(frozen=True)
class DepartmentProjectionResult:
    status: str
    department: str | None = None
    task_id: str | None = None
    page_id: str | None = None
    duplicate: bool = False
    error: str | None = None
    risk_plan_id: str | None = None
    payload_hash: str | None = None
    delivery_status: str | None = None
    readback_status: str | None = None


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
        task.get("assignee") or task.get("profile") or task.get("assigned_to") or ""
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
        task.get("title") or task.get("name") or task.get("subject") or ""
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
        "# Department Task Result",
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

    risk_plan = metadata.get("position_risk_plan") or metadata.get("risk_plan")
    if department == "risk" and isinstance(risk_plan, Mapping):
        parts.extend(
            [
                "",
                "## Position Risk Plan (read-only projection)",
                "",
                f"- Risk Plan ID: `{risk_plan.get('risk_plan_id') or ''}`",
                f"- Mandate Version: `{risk_plan.get('mandate_version_id') or ''}`",
                f"- State / Action: `{risk_plan.get('state') or 'PROPOSED'}` / `{risk_plan.get('action') or ''}`",
                f"- Regime / As Of: `{risk_plan.get('regime') or ''}` / `{risk_plan.get('as_of') or ''}`",
                f"- Entry / Stop / Take Profit: `{risk_plan.get('entry_reference')}` / `{risk_plan.get('stop_price')}` / `{risk_plan.get('take_profit_price')}`",
                f"- Quantity Cap / Loss Budget: `{risk_plan.get('quantity_cap')}` / `{risk_plan.get('position_risk_amount')}`",
                f"- Trailing Activation / Distance: `{risk_plan.get('trailing_activation_price')}` / `{risk_plan.get('trailing_distance')}`",
                f"- Expires At: `{risk_plan.get('expires_at') or ''}`",
                f"- Calculation / Input Hash: `{risk_plan.get('calculation_version') or ''}` / `{risk_plan.get('input_hash') or ''}`",
                f"- Data Quality: `{risk_plan.get('data_quality') or ''}`",
                "",
                "This page is not authoritative. Canonical state remains in the Risk database.",
            ]
        )

    return "\n".join(parts)


class DepartmentNotionProjection:
    """Project terminal department task output into explicitly wired DBs."""

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
        self._idempotency = NotionIdempotency(
            self.env,
            namespace="department-projection",
        )

    def _transport_for(self, token: str) -> Any:
        if self.transport is None or (
            self._transport_token is not None and self._transport_token != token
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
        del workflow_tasks
        force_upsert = bool((event or {}).get("force_upsert"))
        correction = str((event or {}).get("correction") or "").strip()

        tid = task_id(task)
        department = _department(task)

        if department not in DATABASE_ENV:
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
            self.env.get(db_env) or DEFAULT_DATABASES.get(department, "")
        ).strip()

        # Research/Risk are opt-in here because their standalone reporters
        # remain the owner of their own department pipelines.  The Supervisor
        # only projects them when the corresponding DB is explicitly wired;
        # never guess a database ID or silently write into another department.
        if not database_id:
            return DepartmentProjectionResult(
                "skipped",
                department=department,
                task_id=tid,
                error=f"{db_env} missing",
            )

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

        metadata = merged_run_metadata(task)
        result_text = correction or summary(task, metadata)

        props: dict[str, Any] = {
            title_property: _title(title),
        }

        if "서술" in properties_schema:
            props["서술"] = _rich_text(result_text)

        if "원본 리포트" in properties_schema:
            props["원본 리포트"] = _rich_text(result_text)

        created = (
            task.get("completed_at") or task.get("updated_at") or task.get("created_at")
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
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        risk_plan = metadata.get("position_risk_plan") or metadata.get("risk_plan")
        risk_plan_id = (
            str(risk_plan.get("risk_plan_id") or "").strip()
            if isinstance(risk_plan, Mapping)
            else ""
        )

        children = markdown_to_notion_blocks(body)

        def projection_result(
            status: str, page_id: str | None, *, duplicate: bool = False
        ) -> DepartmentProjectionResult:
            readback_status = "NOT_SUPPORTED"
            retrieve = getattr(transport, "retrieve_page", None)
            if page_id and callable(retrieve):
                try:
                    page = retrieve(page_id)
                    readback_status = (
                        "VERIFIED"
                        if str(page.get("id") or "").replace("-", "")
                        == page_id.replace("-", "")
                        else "FAILED"
                    )
                except Exception:
                    readback_status = "FAILED"
            return DepartmentProjectionResult(
                status,
                department=department,
                task_id=tid,
                page_id=page_id,
                duplicate=duplicate,
                risk_plan_id=risk_plan_id or None,
                payload_hash=payload_hash,
                delivery_status="DELIVERED",
                readback_status=readback_status,
            )

        def lookup() -> Sequence[Mapping[str, Any]]:
            try:
                return transport.query_title(database_id, title_property, title)
            except DepartmentNotionProjectionError as exc:
                if exc.status == 400:
                    self._schema_cache.invalidate(database_id)
                raise

        def create() -> Mapping[str, Any]:
            try:
                return transport.create_page(database_id, props, children)
            except DepartmentNotionProjectionError as exc:
                if exc.status == 400:
                    self._schema_cache.invalidate(database_id)
                raise

        if force_upsert:
            existing = lookup()
            if existing:
                page_id = str(existing[0].get("id") or "").strip()
                update_page = getattr(transport, "update_page", None)
                append_blocks = getattr(transport, "append_blocks", None)
                if not page_id or not callable(update_page):
                    raise DepartmentNotionProjectionError(
                        "Notion transport does not support page upsert"
                    )
                update_page(page_id, props)
                correction_is_in_properties = any(
                    name in props for name in ("서술", "원본 리포트")
                )
                if (
                    correction
                    and callable(append_blocks)
                    and not correction_is_in_properties
                ):
                    append_blocks(page_id, children)
                return projection_result("updated", page_id)
            created = create()
            return projection_result(
                "created", str(created.get("id") or "") or None
            )

        result = self._idempotency.execute(
            database_id,
            f"{department}:{title}",
            lookup=lookup,
            create=create,
        )

        return projection_result(
            "duplicate" if result.duplicate else "created",
            result.page_id,
            duplicate=result.duplicate,
        )
