"""Non-binding projection of a completed CEO synthesis into Notion."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.notion_http import NotionHttpError, request_json
from orchestration.adapters.notion_idempotency import (
    NotionIdempotency,
    NotionIdempotencyError,
)
from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache
from orchestration.adapters.terminal_projection_utils import (
    action,
    ids_from,
    is_background_research,
    is_request_scoped_role,
    iso_timestamp,
    merged_run_metadata,
    summary,
    task_id,
    terminal_success,
    workflow_mode,
    workflow_role,
    workflow_root,
)
from orchestration.ceo_workflow_scope import selected_primary_profiles_from_task
from orchestration.qa_contract import split_planner_selection

logger = logging.getLogger(__name__)
PROJECTION_MARKER = "hgfinance.ceo-notion-projection.v1"


class NotionProjectionTransport(Protocol):
    def database_schema(self, database_id: str) -> Mapping[str, Any]: ...
    def query_projection(
        self, database_id: str, projection_key: str
    ) -> Sequence[Mapping[str, Any]]: ...

    def create_page(
        self,
        database_id: str,
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

    def update_page(
        self, page_id: str, properties: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def append_blocks(
        self, page_id: str, children: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]: ...


class NotionProjectionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NotionHttpTransport:
    version = "2022-06-28"

    def __init__(self, token: str) -> None:
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            return request_json(
                method,
                path,
                self.token,
                body=body,
                version=self.version,
            )
        except NotionHttpError as exc:
            detail = exc.detail if exc.detail is not None else str(exc)
            raise NotionProjectionError(str(detail), status=exc.status) from exc

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("POST", path, body)

    def _patch(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("PATCH", path, body)

    def database_schema(self, database_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"databases/{database_id}")

    def query_projection(
        self, database_id: str, projection_key: str
    ) -> Sequence[Mapping[str, Any]]:
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

    def query_title(
        self, database_id: str, property_name: str, title: str
    ) -> Sequence[Mapping[str, Any]]:
        response = self._post(
            f"databases/{database_id}/query",
            {
                "filter": {
                    "property": property_name,
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
        return self._post(
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
        return self._patch(f"pages/{page_id}", {"properties": dict(properties)})

    def append_blocks(
        self, page_id: str, children: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        return self._patch(f"blocks/{page_id}/children", {"children": list(children)})


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


def _date(value: Any) -> dict[str, Any]:
    return {"date": {"start": str(value)}} if value else {"date": None}


def _number(value: Any) -> dict[str, Any]:
    return {"number": int(value)}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


_DEPARTMENT_LABELS = {
    "research-department": "리서치 부서",
    "trading-department": "트레이딩 부서",
    "risk-management": "리스크 부서",
    "quant-backtest-department": "퀀트 부서",
    "accounting-portfolio-department": "회계·포트폴리오 부서",
    "qa-department": "품질검증 부서",
    "hr-department": "인사 부서",
}

_WORKFLOW_MODE_LABELS = {
    "analysis": "분석 자문",
    "binding": "승인 검토",
    "fast_advisory": "신속 자문",
}


def _department_label(value: Any) -> str:
    text = str(value or "").strip()
    return _DEPARTMENT_LABELS.get(text, text or "지정되지 않음")


def _workflow_mode_label(value: Any) -> str:
    text = str(value or "").strip()
    return _WORKFLOW_MODE_LABELS.get(text, text or "분석 자문")


def _human_report_title(fields: Mapping[str, Any]) -> str:
    completed_at = str(fields.get("completed_at") or fields.get("created_at") or "")
    # Keep the title readable in the manager view while retaining a stable,
    # human-visible publication timestamp for same-day reports.  The durable
    # projection key remains the idempotency key and is not shown here.
    stamp = completed_at.replace("T", " ").replace("+00:00", " UTC")
    return f"CEO 종합 보고 · {stamp}" if stamp else "CEO 종합 보고"


def _property_type(property_schema: Mapping[str, Any]) -> str:
    return str(property_schema.get("type") or "").casefold()


def _property_options(property_schema: Mapping[str, Any]) -> tuple[str, ...]:
    select = property_schema.get("select")
    if not isinstance(select, Mapping):
        return ()
    options = select.get("options")
    if not isinstance(options, Sequence) or isinstance(
        options, (str, bytes, bytearray)
    ):
        return ()
    return tuple(
        str(option.get("name"))
        for option in options
        if isinstance(option, Mapping) and option.get("name")
    )


def _normalized(value: str) -> str:
    return "".join(value.casefold().split()).replace("·", "")


def _choose_option(
    properties: Mapping[str, Any],
    property_name: str,
    preferred: Sequence[str],
) -> str:
    property_schema = properties.get(property_name)
    if (
        not isinstance(property_schema, Mapping)
        or _property_type(property_schema) != "select"
    ):
        raise NotionProjectionError(
            f"CEO report property {property_name!r} must be a select",
        )
    options = _property_options(property_schema)
    by_normalized = {_normalized(option): option for option in options}
    for candidate in preferred:
        selected = by_normalized.get(_normalized(candidate))
        if selected:
            return selected
    if len(options) == 1:
        return options[0]
    raise NotionProjectionError(
        f"CEO report select {property_name!r} has no recognized option; available={options!r}",
    )


_CEO_REPORT_PROPERTIES = (
    "브리핑명",
    "기준일",
    "상태",
    "구분",
    "전체 업무",
    "완료",
    "진행 중",
    "승인 대기",
    "차단·오류",
    "대표 결정사항",
    "핵심 성과",
    "문제·위험",
    "다음 우선순위",
)


def _schema_properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _is_ceo_report_schema(properties: Mapping[str, Any]) -> bool:
    expected_types = {
        "브리핑명": "title",
        "기준일": "date",
        "상태": "select",
        "구분": "select",
        "전체 업무": "number",
        "완료": "number",
        "진행 중": "number",
        "승인 대기": "number",
        "차단·오류": "number",
        "대표 결정사항": "rich_text",
        "핵심 성과": "rich_text",
        "문제·위험": "rich_text",
        "다음 우선순위": "rich_text",
    }
    return all(
        _property_type(properties.get(name, {})) == expected
        for name, expected in expected_types.items()
    )


def _is_projection_key_schema(properties: Mapping[str, Any]) -> bool:
    return "projection_key" in properties and any(
        _property_type(properties.get(name, {})) == "title"
        for name in ("제목", "title")
    )


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
        self._schema_cache = BoundedNotionSchemaCache()
        self._idempotency = NotionIdempotency(
            self.env,
            namespace="ceo-projection",
        )

    def _comment_marker(self, projection_key: str) -> str:
        return f"{PROJECTION_MARKER} projection_key={projection_key} status=created"

    def _has_comment_marker(self, task: Mapping[str, Any], projection_key: str) -> bool:
        marker = self._comment_marker(projection_key)
        comments = task.get("comments")
        if isinstance(comments, Sequence) and not isinstance(
            comments, (str, bytes, bytearray)
        ):
            return any(
                marker in str(item.get("body") if isinstance(item, Mapping) else item)
                for item in comments
            )
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
        selected_profiles, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_task)
        )
        primary = [
            item
            for item in workflow_tasks
            if is_request_scoped_role(item, root_task_id, "primary")
            and (
                not selected_profiles
                or str(item.get("assignee") or "") in selected_profiles
            )
        ]
        qa = [
            item
            for item in workflow_tasks
            if is_request_scoped_role(item, root_task_id, "qa")
        ]
        departments = list(
            dict.fromkeys(
                str(item.get("assignee")) for item in primary if item.get("assignee")
            )
        )
        primary_ids = [task_id(item) for item in primary]
        qa_ids = list(ids_from(metadata.get("qa_task_ids"))) or [
            task_id(item) for item in qa
        ]
        return {
            "root_task_id": root_task_id,
            "synthesis_task_id": task_id(task),
            "original_query": str(
                metadata.get("original_query")
                or metadata.get("query")
                or original_query
            ),
            "final_answer": summary(task, metadata),
            "selected_departments": departments,
            "workflow_mode": workflow_mode(task)
            or str(metadata.get("workflow_mode") or "analysis"),
            "primary_task_ids": primary_ids,
            "qa_task_ids": qa_ids,
            "created_at": iso_timestamp(task.get("created_at")),
            "completed_at": iso_timestamp(
                task.get("completed_at") or task.get("finished_at")
            ),
            "projection_key": f"ceo-synthesis:{root_task_id}:{task_id(task)}",
        }

    def _ceo_report_properties(
        self,
        properties: Mapping[str, Any],
        fields: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        request_tasks = [
            item
            for item in workflow_tasks
            if task_id(item) == fields["root_task_id"]
            or (
                workflow_root(item) == fields["root_task_id"]
                and not is_background_research(item)
            )
        ]
        terminal = [item for item in request_tasks if terminal_success(item)]
        completed = [
            item
            for item in terminal
            if str(item.get("status") or "").casefold() in {"done", "completed"}
        ]
        running = [
            item
            for item in request_tasks
            if str(item.get("status") or "").casefold()
            in {"claimed", "running", "in_progress"}
        ]
        approval = [
            item
            for item in request_tasks
            if str(item.get("status") or "").casefold()
            in {"approval_pending", "pending_approval", "awaiting_approval"}
        ]
        blocked = [
            item
            for item in request_tasks
            if str(item.get("status") or "").casefold()
            in {"blocked", "failed", "error", "expired"}
        ]
        primary_summaries = "\n".join(
            f"{_department_label(item.get('assignee'))}: "
            f"{summary(item, merged_run_metadata(item))}"
            for item in workflow_tasks
            if is_request_scoped_role(item, fields["root_task_id"], "primary")
        )
        qa_summaries = "\n".join(
            f"{summary(item, merged_run_metadata(item))}"
            for item in workflow_tasks
            if is_request_scoped_role(item, fields["root_task_id"], "qa")
        )
        final_answer = str(fields["final_answer"] or "")
        state = _choose_option(
            properties, "상태", ("완료", "COMPLETED", "DONE", "완료됨")
        )
        category = _choose_option(
            properties, "구분", ("CEO", "CEO 종합", "SYNTHESIS", "보고서", "분석")
        )
        return {
            "브리핑명": _title(_human_report_title(fields)),
            "기준일": _date(fields["completed_at"] or fields["created_at"]),
            "상태": _select(state),
            "구분": _select(category),
            "전체 업무": _number(len(workflow_tasks)),
            "완료": _number(len(completed)),
            "진행 중": _number(len(running)),
            "승인 대기": _number(len(approval)),
            "차단·오류": _number(len(blocked)),
            "대표 결정사항": _rich_text(final_answer),
            "핵심 성과": _rich_text(primary_summaries),
            "문제·위험": _rich_text(
                qa_summaries or "품질검증 부서의 사후 검토는 별도로 진행되지 않았습니다."
            ),
            "다음 우선순위": _rich_text(
                "미확인 위험 요인을 확인하고 필요한 후속 검토를 요청합니다."
            ),
        }

    def _schema(
        self, transport: NotionProjectionTransport, database_id: str
    ) -> Mapping[str, Any]:
        schema_reader = getattr(transport, "database_schema", None)
        if not callable(schema_reader):
            raise NotionProjectionError(
                "Notion transport does not support database schema inspection"
            )
        return schema_reader(database_id)

    def project(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        force_upsert = bool((event or {}).get("force_upsert"))
        correction = str((event or {}).get("correction") or "").strip()
        if workflow_role(task) != "synthesis" or action(task) != "SYNTHESIZE":
            return CeoSynthesisProjectionResult("skipped").as_dict()
        if not terminal_success(task):
            return CeoSynthesisProjectionResult("skipped").as_dict()
        if workflow_root(task) != root_task_id:
            return CeoSynthesisProjectionResult("skipped").as_dict()
        database_id = str(self.env.get("NOTION_CEO_DB") or "")
        token = str(self.env.get("NOTION_TOKEN") or "")
        fields = self._workflow_fields(root_task_id, task, workflow_tasks)
        if correction:
            fields["final_answer"] = correction
        key = fields["projection_key"]
        if not database_id or not token:
            return CeoSynthesisProjectionResult(
                "failed",
                projection_key=key,
                retryable=True,
                error="NOTION_TOKEN/NOTION_CEO_DB missing",
            ).as_dict()
        transport = self.transport or _NotionHttpTransport(token)
        current_report_schema = False
        projection_schema = False
        try:
            schema, schema_cache_hit = self._schema_cache.get(
                database_id,
                lambda: self._schema(transport, database_id),
            )
            schema_properties = _schema_properties(schema)
            current_report_schema = _is_ceo_report_schema(schema_properties)
            projection_schema = _is_projection_key_schema(schema_properties)
            schema_mismatch = not current_report_schema and not projection_schema
            if schema_mismatch and schema_cache_hit:
                # Re-check a cached schema once after a property migration. A
                # stale mapping must never decide which fields are written.
                self._schema_cache.invalidate(database_id)
                schema, _ = self._schema_cache.get(
                    database_id,
                    lambda: self._schema(transport, database_id),
                )
                schema_properties = _schema_properties(schema)
                current_report_schema = _is_ceo_report_schema(schema_properties)
                projection_schema = _is_projection_key_schema(schema_properties)
                schema_mismatch = not current_report_schema and not projection_schema
            if schema_mismatch:
                self._schema_cache.invalidate(database_id)
                return CeoSynthesisProjectionResult(
                    "failed",
                    projection_key=key,
                    retryable=False,
                    error="NOTION_CEO_DB does not match a supported schema",
                ).as_dict()
        except NotionProjectionError as exc:
            # Legacy transports/test doubles predate schema inspection. Keep
            # their projection-key behavior; real HTTP transports implement
            # database_schema() and still fail closed on schema errors.
            if callable(getattr(transport, "database_schema", None)):
                retryable = exc.status is None or exc.status >= 500 or exc.status == 429
                logger.warning(
                    "ceo_notion_projection_schema_failed", extra={"error": str(exc)}
                )
                return CeoSynthesisProjectionResult(
                    "failed", key, retryable=retryable, error=str(exc)
                ).as_dict()
            projection_schema = True
        if current_report_schema and self.kanban_client is None:
            return CeoSynthesisProjectionResult(
                "failed",
                projection_key=key,
                retryable=True,
                error="Kanban client is required for durable Notion projection idempotency",
            ).as_dict()
        selected_departments = ", ".join(
            _department_label(value) for value in fields["selected_departments"]
        ) or "지정되지 않음"
        report = (
            "# CEO 종합 보고\n\n"
            "## 업무 개요\n\n"
            f"- 검토 부서: {selected_departments}\n"
            f"- 업무 유형: {_workflow_mode_label(fields['workflow_mode'])}\n\n"
            "## 최종 판단\n\n"
            f"{fields['final_answer']}"
        )
        if current_report_schema:
            properties = self._ceo_report_properties(
                schema_properties, fields, workflow_tasks
            )
        else:
            properties = {
                "제목": _title(_human_report_title(fields)),
                "projection_key": _rich_text(key),
                "root_task_id": _rich_text(root_task_id),
                "synthesis_task_id": _rich_text(task_id(task)),
                "original_query": _rich_text(fields["original_query"]),
                "selected_departments": _rich_text(
                    json.dumps(fields["selected_departments"], ensure_ascii=False)
                ),
                "workflow_mode": _rich_text(fields["workflow_mode"]),
                "primary_task_ids": _rich_text(json.dumps(fields["primary_task_ids"])),
                "qa_task_ids": _rich_text(json.dumps(fields["qa_task_ids"])),
                "created_at": _rich_text(fields["created_at"]),
                "completed_at": _rich_text(fields["completed_at"]),
            }
        children = markdown_to_notion_blocks(report)
        try:

            def lookup() -> Sequence[Mapping[str, Any]] | Mapping[str, Any]:
                existing: Sequence[Mapping[str, Any]] = ()
                if projection_schema:
                    existing = transport.query_projection(database_id, key)
                    if existing:
                        return existing
                elif current_report_schema and force_upsert:
                    query_title = getattr(transport, "query_title", None)
                    if callable(query_title):
                        # New pages use a human-readable timestamp title. Keep
                        # the former task-id title as a one-time migration
                        # fallback so correction/replay remains idempotent.
                        for title in (
                            _human_report_title(fields),
                            f"CEO Synthesis · {root_task_id}",
                        ):
                            existing = query_title(database_id, "브리핑명", title)
                            if existing:
                                return existing
                if force_upsert:
                    # An explicit correction treats the authoritative Notion
                    # query as truth.  A stale Kanban/idempotency marker must
                    # not hide a page that was deleted or moved.
                    return existing
                refreshed_task = task
                if self.kanban_client is not None and not self._has_comment_marker(
                    task, key
                ):
                    try:
                        refreshed_task = self.kanban_client.show(task_id(task))
                    except Exception:  # noqa: BLE001 - fallback is best effort
                        refreshed_task = task
                if self._has_comment_marker(refreshed_task, key):
                    return {"__notion_existing__": True}
                return existing

            def create() -> Mapping[str, Any]:
                return transport.create_page(database_id, properties, children)

            if force_upsert:
                existing = lookup()
                if isinstance(existing, Mapping):
                    existing_rows: Sequence[Mapping[str, Any]] = ()
                else:
                    existing_rows = existing
                if existing_rows:
                    page_id = str(existing_rows[0].get("id") or "").strip()
                    update_page = getattr(transport, "update_page", None)
                    append_blocks = getattr(transport, "append_blocks", None)
                    if not page_id or not callable(update_page):
                        raise NotionProjectionError(
                            "Notion transport does not support page upsert"
                        )
                    update_page(page_id, properties)
                    if (
                        correction
                        and callable(append_blocks)
                        and not current_report_schema
                    ):
                        append_blocks(page_id, children)
                    return CeoSynthesisProjectionResult(
                        "updated", key, page_id
                    ).as_dict()
                created = create()
                return CeoSynthesisProjectionResult(
                    "created", key, str(created.get("id") or "") or None
                ).as_dict()

            result = self._idempotency.execute(
                database_id,
                key,
                lookup=lookup,
                create=create,
            )
            if result.duplicate:
                return CeoSynthesisProjectionResult(
                    "duplicate",
                    key,
                    result.page_id,
                    duplicate=True,
                ).as_dict()
        except NotionIdempotencyError as exc:
            logger.warning(
                "ceo_notion_projection_claim_failed", extra={"error": str(exc)}
            )
            return CeoSynthesisProjectionResult(
                "failed", key, retryable=True, error=str(exc)
            ).as_dict()
        except NotionProjectionError as exc:
            if exc.status == 400:
                self._schema_cache.invalidate(database_id)
            retryable = exc.status is None or exc.status >= 500 or exc.status == 429
            logger.warning(
                "ceo_notion_projection_create_failed", extra={"error": str(exc)}
            )
            return CeoSynthesisProjectionResult(
                "failed", key, retryable=retryable, error=str(exc)
            ).as_dict()
        try:
            page_id = result.page_id
            if self.kanban_client is not None:
                try:
                    self.kanban_client.comment_task(
                        task_id(task), self._comment_marker(key)
                    )
                except Exception as exc:  # noqa: BLE001 - page is already durable
                    logger.warning(
                        "ceo_notion_projection_marker_failed", extra={"error": str(exc)}
                    )
            return CeoSynthesisProjectionResult("created", key, page_id).as_dict()
        except Exception as exc:  # noqa: BLE001 - non-binding side effect
            logger.warning("ceo_notion_projection_failed", extra={"error": str(exc)})
            return CeoSynthesisProjectionResult(
                "failed", key, retryable=True, error=str(exc)
            ).as_dict()


__all__ = ["PROJECTION_MARKER", "CeoNotionProjection", "CeoSynthesisProjectionResult"]
