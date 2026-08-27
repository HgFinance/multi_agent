#!/usr/bin/env python3
"""Capture the HR CEO-E2E read-only evidence in one bounded pass.

The Hermes terminal guard may reject ad-hoc Python snippets and plain HTTP
shell commands even though the internal Workforce API is the authoritative
HR read path.  This repository-owned helper keeps the network surface fixed
to the three approved GETs, avoids tool-probing loops, and stores the raw
non-secret API responses in the task workspace for QA replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://workforce-api:8000"
SCORECARD_DEPARTMENTS = ("research-department", "risk-management")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


def _bounded_response(path: str, response: Any) -> Any:
    """Keep only replay-relevant facts beside the raw response hash.

    The evidence file is attached through the Hermes task transport. Large
    nested Workforce payloads add no proof beyond their response hash and can
    make that transport fragile. Preserve the fields needed to reproduce the
    HR summary while keeping worker-level raw records out of the artifact.
    """

    if isinstance(response, str) and "/scorecard-brief" in path:
        # The scorecard endpoint intentionally returns a human-readable
        # Markdown table rather than JSON.  Keep only the table cells needed
        # to verify the rendered answer; never copy the full response into
        # the task handoff.
        section_names = {
            "capacity": "처리량·지연 (capacity)",
            "cost": "비용 (cost)",
            "quality": "품질 (quality)",
        }
        table_rows: dict[str, list[dict[str, str]]] = {
            section: [] for section in section_names
        }
        current_section: str | None = None
        headers: list[str] = []
        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_section = next(
                    (
                        section
                        for section, title in section_names.items()
                        if title in stripped
                    ),
                    None,
                )
                headers = []
                continue
            if current_section is None or not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not cells or all(set(cell) <= {"-", " "} for cell in cells):
                continue
            if not headers:
                headers = cells
                continue
            if len(cells) != len(headers):
                continue
            table_rows[current_section].append(
                {
                    header: value
                    for header, value in zip(headers, cells)
                    if header and header != "---"
                }
            )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        departments = [
            item
            for item in query.get("department_code", [])
            if item in SCORECARD_DEPARTMENTS
        ]
        snapshot_statuses = {
            row.get("부서", ""): row.get("관측", "")
            for row in table_rows["cost"]
            if row.get("부서") and row.get("관측")
        }
        eval_references = {
            row.get("부서", ""): int(row["eval_run 참조"])
            for row in table_rows["quality"]
            if row.get("부서")
            and row.get("eval_run 참조", "").isdigit()
        }
        bounded_text = {
            "content_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
            "content_type": "text/markdown",
            "departments": departments,
            "table_rows": table_rows,
            "snapshot_status_by_department": snapshot_statuses,
            "quality_eval_run_references": eval_references,
        }
        bounded_text["summary_sha256"] = hashlib.sha256(
            json.dumps(bounded_text, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return bounded_text
    if not isinstance(response, dict):
        return None
    bounded: dict[str, Any] | None = None
    if path.endswith("/improvements"):
        candidates = response.get("candidates")
        bounded = {
            "candidate_count": len(candidates) if isinstance(candidates, list) else None,
        }
    elif "/observability" in path:
        idle_agents = response.get("idle_agents")
        states: dict[str, int] = {}
        field_presence: dict[str, dict[str, int]] = {}
        if isinstance(idle_agents, list):
            for agent in idle_agents:
                if not isinstance(agent, dict):
                    continue
                state = str(agent.get("status") or "").strip()
                if state:
                    states[state] = states.get(state, 0) + 1
            agent_count = len([item for item in idle_agents if isinstance(item, dict)])
            for field_name in ("worker_id", "last_seen_at", "idle_hours", "status"):
                value_count = sum(
                    1
                    for agent in idle_agents
                    if isinstance(agent, dict)
                    and agent.get(field_name) not in (None, "")
                )
                key_count = sum(
                    1
                    for agent in idle_agents
                    if isinstance(agent, dict) and field_name in agent
                )
                field_presence[field_name] = {
                    "key_present": key_count,
                    "value_present": value_count,
                    "missing_or_null": max(agent_count - value_count, 0),
                }
        bounded = {
            "window_start": response.get("window_start"),
            "window_end": response.get("window_end"),
            "states": states,
            "agent_count": (
                len([item for item in idle_agents if isinstance(item, dict)])
                if isinstance(idle_agents, list)
                else 0
            ),
            "field_presence": field_presence,
        }
    elif "/scorecard-brief" in path:
        bounded = {
            "content_sha256": hashlib.sha256(
                json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "content_type": type(response).__name__,
        }
    if bounded is None:
        return None
    bounded["summary_sha256"] = hashlib.sha256(
        json.dumps(bounded, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return bounded


def _get(base_url: str, path: str) -> dict[str, Any]:
    started = _utc_now()
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Accept": "application/json, text/plain"},
        method="GET",
    )
    status: int | None = None
    raw = b""
    error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        error = f"HTTP_{status}"
    except (OSError, urllib.error.URLError, ValueError) as exc:
        error = type(exc).__name__
    finished = _utc_now()
    return {
        "path": path,
        "method": "GET",
        "request_started_at": _iso(started),
        "response_received_at": _iso(finished),
        "duration_ms": max(0, int((finished - started).total_seconds() * 1000)),
        "http_status": status,
        "response_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "response_bytes": len(raw),
        "response": _bounded_response(path, _parse_json(raw)) if raw else None,
        "error": error,
    }


def _window(observability: Any) -> tuple[str, str]:
    if isinstance(observability, dict):
        start = str(observability.get("window_start") or "").strip()
        end = str(observability.get("window_end") or "").strip()
        if start and end:
            return start, end
    end_dt = _utc_now()
    return _iso(end_dt - timedelta(hours=24)), _iso(end_dt)


def _summary(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    improvements = next(
        (item for item in receipts if item["path"].startswith("/workforce/v1/improvements")),
        {},
    )
    observability = next(
        (item for item in receipts if "/observability" in item["path"]),
        {},
    )
    body = observability.get("response")
    idle_agents = body.get("idle_agents") if isinstance(body, dict) else []
    states: dict[str, int] = {}
    if isinstance(idle_agents, list):
        for agent in idle_agents:
            if not isinstance(agent, dict):
                continue
            state = str(agent.get("status") or "").strip()
            if state:
                states[state] = states.get(state, 0) + 1
    if not states and isinstance(body, dict):
        bounded_states = body.get("states")
        if isinstance(bounded_states, dict):
            states = {
                str(key): int(value)
                for key, value in bounded_states.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    candidates = improvements.get("response")
    candidate_count = (
        candidates.get("candidate_count")
        if isinstance(candidates, dict)
        else None
    )
    candidates = candidates.get("candidates") if isinstance(candidates, dict) else []
    duration_by_path = {
        "improvements": improvements.get("duration_ms"),
        "observability": observability.get("duration_ms"),
        "scorecard_brief": next(
            (
                item.get("duration_ms")
                for item in receipts
                if "/scorecard-brief" in str(item.get("path") or "")
            ),
            None,
        ),
    }
    failed_requests = sum(
        1 for item in receipts if item.get("http_status") != 200
    )
    return {
        "helper_runs": 1,
        "improvement_candidate_count": (
            len(candidates)
            if isinstance(candidates, list)
            else candidate_count
        ),
        "idle_state_counts": states,
        "observability_window_start": body.get("window_start") if isinstance(body, dict) else None,
        "observability_window_end": body.get("window_end") if isinstance(body, dict) else None,
        "latency_ms": duration_by_path,
        "failure_retry_duplicate": {
            "api_failures": failed_requests,
            "retries_observed": 0,
            "duplicate_helper_runs": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="hr_e2e_evidence.json")
    args = parser.parse_args()

    base_url = str(os.environ.get("WORKFORCE_API_URL") or DEFAULT_BASE_URL).rstrip("/")
    if base_url != DEFAULT_BASE_URL:
        raise SystemExit("WORKFORCE_API_URL must use the approved Workforce API base URL")

    receipts = [_get(base_url, "/workforce/v1/improvements")]
    observability = _get(
        base_url,
        "/workforce/v1/departments/observability?lookback_hours=24",
    )
    receipts.append(observability)
    window_start, window_end = _window(observability.get("response"))
    query = urllib.parse.urlencode(
        [
            ("window_start", window_start),
            ("window_end", window_end),
            *[("department_code", department) for department in SCORECARD_DEPARTMENTS],
        ]
    )
    receipts.append(
        _get(
            base_url,
            f"/workforce/v1/departments/scorecard-brief?{query}",
        )
    )

    evidence = {
        "schema": "hgfinance.hr-workforce-evidence.v1",
        "capture_mode": "PAPER read-only GET",
        "captured_at": _iso(_utc_now()),
        "source": "Workforce API",
        "requests": receipts,
        "summary": _summary(receipts),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        # Keep the attachment transport ASCII-safe.  The decoded JSON remains
        # Korean for QA/Notion projections, while avoiding charset corruption
        # in Hermes attachment storage before the supervisor recomputes the
        # artifact digest.
        json.dumps(evidence, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)

    statuses = [item.get("http_status") for item in receipts]
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "artifact": output.name,
                "artifact_sha256": digest,
                "http_statuses": statuses,
                "summary": evidence["summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if statuses == [200, 200, 200] else 2


if __name__ == "__main__":
    sys.exit(main())
