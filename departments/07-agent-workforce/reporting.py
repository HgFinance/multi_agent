"""Agent Workforce (HR) pipeline report and observability helpers.

담당: 영주 (Agent Workforce 인사팀)
근거: departments/03-risk/reporting.py, departments/06-ai-qa-audit/reporting.py와 같은
      부서 로컬 패턴 - Risk/QA 원안 그대로, verdict/check_results 자리만 F19 Candidate
      계약(scripts.py v2, 2026-08-03)으로 바꿨다.

이 헬퍼는 이 부서에만 쓴다(공유하지 않는다).
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


def evaluation_metrics(out: dict[str, Any], report_markdown: str = "") -> dict[str, Any]:
    """F19 Candidate 계약 기준 지표 - QA/CEO 게이트 통과 여부와 업로드 결과를 센다."""

    notion = out.get("notion_upload") or {}
    return {
        "status": out.get("status"),
        "qa_verified": out.get("qa_verified"),
        "ceo_approved": out.get("ceo_approved"),
        "notion_upload_ok": notion.get("ok") if notion else None,
        "report_markdown_chars": len(report_markdown),
        "langsmith_enabled": bool((out.get("observability") or {}).get("langsmith", {}).get("enabled")),
    }


def notion_rich_text_chunks(value: Any, *, chunk_size: int = 1900) -> list[dict[str, dict[str, str]]]:
    """Keep full Markdown in Notion rich_text without crossing its 2k limit."""

    text = "" if value is None else str(value)
    if not text:
        return [{"text": {"content": ""}}]
    return [{"text": {"content": text[i : i + chunk_size]}} for i in range(0, len(text), chunk_size)]
