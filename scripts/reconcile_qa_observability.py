#!/usr/bin/env python3
"""Reconcile historical QA gateway leases and CEO root traces safely.

The command is intentionally an operator boundary, not a second delivery or
workflow implementation. Discord state is repaired by the canonical
``DiscordIdempotencyStore`` and LangSmith state is closed through the existing
``close_root_trace`` updater. No Discord message, Hermes task, model call, or
order is created by this command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestration.adapters.ceo_supervisor import HermesKanbanClient
from orchestration.ceo_workflow_scope import read_marker, workflow_mode_from_body
from orchestration.discord_idempotency import DiscordIdempotencyStore
from orchestration.langsmith_queries import close_query_client, query_runs

_TERMINAL_TASK_STATES = frozenset(
    {"done", "completed", "archived", "blocked", "failed", "gave_up", "crashed", "timed_out"}
)
_FAILURE_TASK_STATES = frozenset(
    {"blocked", "failed", "gave_up", "crashed", "timed_out", "spawn_failed"}
)
_DELIVERED_MARKER = re.compile(
    r"(?:\"delivery_status\"\s*:\s*\"(?:sent|deduped)\"|"
    r"\bdiscord_status=(?:sent|deduped)|\"response_delivered\"\s*:\s*true)",
    re.IGNORECASE,
)


def reconcile_discord(
    *,
    hermes_home: str | Path,
    profile: str,
) -> dict[str, int]:
    """Run the canonical, local-only stale lease reconciliation."""

    return DiscordIdempotencyStore(hermes_home).reconcile_stale_inbound(
        profile=profile,
    )


def _metadata(run: Any) -> dict[str, Any]:
    extra = getattr(run, "extra", None)
    if not isinstance(extra, Mapping):
        return {}
    value = extra.get("metadata")
    return dict(value) if isinstance(value, Mapping) else {}


def _task_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("id") or payload.get("task_id") or "").strip()


def _has_terminal_output(payload: Mapping[str, Any]) -> bool:
    return any(
        str(payload.get(field) or "").strip()
        for field in ("result", "final_answer", "latest_summary")
    )


def _is_root_candidate(payload: Mapping[str, Any]) -> bool:
    body = str(payload.get("body") or "")
    return read_marker(body, "workflow_role") == "root" or read_marker(
        body, "root_task_role"
    ) == "scope_and_planning"


def _response_candidate(
    client: Any,
    root_id: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Return an authoritative terminal response and its root payload."""

    root = client.show(root_id)
    root_status = str(root.get("status") or "").casefold()
    if root_status not in {"done", "completed", "archived"}:
        return None

    _workflow_root, payloads = client.workflow(root_id)
    synthesis: list[Mapping[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, Mapping) or _task_id(payload) == root_id:
            continue
        body = str(payload.get("body") or "")
        if read_marker(body, "workflow_role") != "synthesis":
            continue
        if read_marker(body, "workflow_root_task_id") != root_id:
            continue
        status = str(payload.get("status") or "").casefold()
        if status in _TERMINAL_TASK_STATES and _has_terminal_output(payload):
            synthesis.append(payload)

    if synthesis:
        selected = max(
            synthesis,
            key=lambda payload: (
                int(payload.get("completed_at") or 0),
                _task_id(payload),
            ),
        )
        return selected, root
    if _has_terminal_output(root):
        return root, root
    return None


def reconcile_pending_langsmith(
    *,
    environment: Mapping[str, str],
    lookback_days: int = 30,
) -> dict[str, int]:
    """Close only pending CEO roots with authoritative terminal Kanban proof."""

    from orchestration.llm_observability import langsmith_enabled

    result = {
        "discovered": 0,
        "closed": 0,
        "skipped": 0,
        "unresolved": 0,
        "errors": 0,
    }
    if not langsmith_enabled():
        return result

    try:
        from langsmith import Client

        from orchestration.llm_observability import (
            close_root_trace,
            langsmith_multipart_ingest_info,
            langsmith_project,
        )

        now = datetime.now(timezone.utc)
        client = Client(
            info=langsmith_multipart_ingest_info(),
            hide_inputs=True,
            hide_outputs=True,
            hide_metadata=False,
            omit_traced_runtime_info=True,
        )
        try:
            runs = query_runs(
                client,
                project_name=langsmith_project("workflow"),
                min_start_time=now - timedelta(days=max(1, min(lookback_days, 365))),
                max_start_time=now,
                is_root=True,
                page_size=100,
                max_results=500,
                selects=["ID", "NAME", "STATUS", "ERROR", "START_TIME", "END_TIME", "EXTRA", "OUTPUTS"],
            )
        finally:
            close_query_client(client)
        pending_runs = [run for run in runs if getattr(run, "end_time", None) is None]
        result["discovered"] = len(pending_runs)
        if not pending_runs:
            return result

        kanban = HermesKanbanClient(environment=environment)
        board_rows = kanban.list_tasks()
        roots_by_request: dict[str, list[str]] = {}
        for row in board_rows:
            if not _is_root_candidate(row):
                continue
            request_id = read_marker(str(row.get("body") or ""), "request_id")
            root_id = _task_id(row)
            if request_id and root_id:
                roots_by_request.setdefault(request_id, []).append(root_id)

        for run in pending_runs:
            metadata = _metadata(run)
            request_id = str(metadata.get("request_id") or "").strip()
            run_id = str(getattr(run, "id", "") or "").strip()
            if not request_id or not run_id:
                result["skipped"] += 1
                continue
            name = str(getattr(run, "name", "") or "").casefold()
            stage = str(metadata.get("stage") or "").casefold()
            if name != "hgfinance.user-query" and stage != "ceo-ingress":
                result["skipped"] += 1
                continue

            for root_id in roots_by_request.get(request_id, ()):
                try:
                    response = _response_candidate(kanban, root_id)
                except Exception:  # noqa: BLE001 - isolate one candidate from provider errors.
                    result["errors"] += 1
                    continue
                if response is None:
                    continue
                response_payload, root_payload = response
                response_status = str(response_payload.get("status") or "").casefold()
                failure = response_status in _FAILURE_TASK_STATES
                root_body = str(root_payload.get("body") or "")
                response_body = str(response_payload.get("body") or "")
                source = str(metadata.get("source") or read_marker(root_body, "source") or "")
                delivery_confirmed = source.casefold() not in {"discord", "discord-bff"} or bool(
                    _DELIVERED_MARKER.search(root_body + "\n" + response_body)
                )
                terminal_status = "blocked" if failure or not delivery_confirmed else "completed"
                error_class = (
                    response_status or "workflow_terminal_failure"
                    if failure
                    else ("discord_unconfirmed" if not delivery_confirmed else None)
                )
                try:
                    workflow_mode = workflow_mode_from_body(root_body)
                except Exception:  # noqa: BLE001 - malformed historical marker uses default mode.
                    workflow_mode = "analysis"
                try:
                    closed = close_root_trace(
                        run_id=run_id,
                        request_id=request_id,
                        root_id=root_id,
                        task_id=_task_id(response_payload) or root_id,
                        department="ceo-workflow",
                        workflow_mode=workflow_mode,
                        source=source or None,
                        status=terminal_status,
                        error_class=error_class,
                        terminal_metadata={
                            "terminal_status": terminal_status,
                            "terminal_reason": error_class
                            or "reconciled_terminal_workflow",
                            "terminal_task_id": _task_id(response_payload) or root_id,
                            "terminal_department": "ceo-workflow",
                            "observability_source": "ceo-pending-trace-reconciliation",
                        },
                    )
                except Exception:  # noqa: BLE001 - isolate one candidate from provider errors.
                    # A provider/API failure is distinct from a safe refusal
                    # to claim closure.  Neither may abort other candidates.
                    result["errors"] += 1
                    break
                if not closed:
                    result["unresolved"] += 1
                    break
                # ``close_root_trace`` is the single v2 write boundary. A
                # duplicate terminal PATCH is idempotent because the Kanban
                # terminal evidence was checked before this call.
                result["closed"] += 1
                break
            else:
                result["skipped"] += 1
    except Exception:  # noqa: BLE001 - reconciliation must return a safe error summary.
        result["errors"] += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("all", "discord", "langsmith"), default="all")
    parser.add_argument("--hermes-home", default=os.getenv("HERMES_HOME", "/opt/data"))
    parser.add_argument("--profile", default=os.getenv("HERMES_PROFILE", "qa-department"))
    args = parser.parse_args()

    output: dict[str, Any] = {}
    if args.only in {"all", "discord"}:
        output["discord"] = reconcile_discord(
            hermes_home=args.hermes_home,
            profile=args.profile,
        )
    if args.only in {"all", "langsmith"}:
        output["langsmith"] = reconcile_pending_langsmith(
            environment=dict(os.environ),
        )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
