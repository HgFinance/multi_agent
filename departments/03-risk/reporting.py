"""Risk pipeline report and observability helpers.

The helpers are deliberately local to the Risk department. They keep the
binding decision separate from narrative/reporting concerns and never expose
credentials in the LangSmith handoff metadata.
"""

from __future__ import annotations

import json
import os
from typing import Any


def md_cell(value: Any) -> str:
    """Render one value safely inside a Markdown table cell."""

    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def json_cell(value: Any) -> str:
    return md_cell(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def langsmith_handoff(trace_id: str) -> dict[str, Any]:
    enabled = os.environ.get("LANGSMITH_TRACING", "")
    enabled = enabled.casefold() in {"1", "true", "yes", "on"}
    project = os.environ.get("LANGSMITH_PROJECT")
    run_id = os.environ.get("LANGSMITH_RUN_ID")
    return {
        "trace_id": str(trace_id),
        "langsmith": {
            "enabled": enabled,
            "project": project,
            "run_id": run_id,
            "handoff_status": "configured" if enabled else "not_configured",
        },
    }


def evaluation_metrics(
    out: dict[str, Any], report_markdown: str = ""
) -> dict[str, Any]:
    checks = out.get("check_results") or []
    passed = sum(1 for check in checks if check.get("passed") is True)
    notion = out.get("notion_upload") or {}
    return {
        "verdict": out.get("verdict"),
        "deterministic_check_count": len(checks),
        "passed_check_count": passed,
        "failed_check_count": len(checks) - passed,
        "fallback_count": len(out.get("fallbacks") or []),
        "escalated": bool(out.get("escalate")),
        "notion_upload_ok": notion.get("ok") if notion else None,
        "report_markdown_chars": len(report_markdown),
        "langsmith_enabled": bool(
            (out.get("observability") or {}).get("langsmith", {}).get("enabled")
        ),
    }


def notion_rich_text_chunks(
    value: Any, *, chunk_size: int = 1900
) -> list[dict[str, dict[str, str]]]:
    """Keep full Markdown in Notion rich_text without crossing its 2k limit."""

    text = "" if value is None else str(value)
    if not text:
        return [{"text": {"content": ""}}]
    return [
        {"text": {"content": text[i : i + chunk_size]}}
        for i in range(0, len(text), chunk_size)
    ]
