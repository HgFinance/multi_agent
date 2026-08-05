"""Read-only operator projection for the AI Office.

The browser never reads Hermes state, department SQLite files, Redis, or
credentials directly. Registry metadata is separate from live runtime events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from portfolio_runtime import runtime_snapshot
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.portfolio_runtime import runtime_snapshot

try:
    from agent_status import agent_status_snapshot
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.agent_status import agent_status_snapshot

import yaml

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "docs" / "02-engineering" / "contracts" / "event-registry.v1.json"

DEPARTMENT_PROFILES: tuple[dict[str, str], ...] = (
    {"department_code": "ceo-agent", "name": "CEO Office", "domain": "governance", "profile": "00-ceo-office"},
    {"department_code": "hr-department", "name": "Agent Workforce 인사팀", "domain": "workforce", "profile": "07-agent-workforce"},
    {"department_code": "research-department", "name": "리서치본부", "domain": "research", "profile": "01-research"},
    {"department_code": "trading-department", "name": "트레이딩본부", "domain": "trading", "profile": "02-trading"},
    {"department_code": "risk-management", "name": "리스크관리본부", "domain": "risk", "profile": "03-risk"},
    {"department_code": "quant-backtest-department", "name": "퀀트·백테스트본부", "domain": "quant", "profile": "04-quant-backtest"},
    {"department_code": "accounting-portfolio-department", "name": "회계·포트폴리오본부", "domain": "accounting", "profile": "05-accounting-portfolio"},
    {"department_code": "qa-department", "name": "AI QA·감사본부", "domain": "qa", "profile": "06-ai-qa-audit"},
)


def _profile_data(profile: str) -> dict[str, Any]:
    path = ROOT / "departments" / profile / "hermes" / "config.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _registry() -> dict[str, Any]:
    try:
        value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": []}
    return value if isinstance(value, dict) else {"events": []}


def _department_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in DEPARTMENT_PROFILES:
        config = _profile_data(item["profile"])
        registry = config.get("staff_registry") if isinstance(config.get("staff_registry"), dict) else {}
        runtime = config.get("employee_runtime") if isinstance(config.get("employee_runtime"), dict) else {}
        model = config.get("model") if isinstance(config.get("model"), dict) else {}
        agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
        workers = config.get("workers") if isinstance(config.get("workers"), dict) else {}
        rows.append(
            {
                **item,
                "status": "OFFLINE",
                "status_reason": "Kanban Status Bridge와 runtime heartbeat가 BFF에 연결되지 않았습니다.",
                "runtime_observed": False,
                "head_persona": agent.get("head_persona"),
                "head_provider": model.get("provider"),
                "head_model": model.get("default"),
                "executor": runtime.get("executor"),
                "worker_model": runtime.get("model_default"),
                "output_contract": runtime.get("output_contract"),
                "failure_action": runtime.get("failure_action"),
                "worker_count": registry.get("worker_count", registry.get("active_worker_count", 0)),
                "active_worker_count": 0,
                "conditional_worker_count": registry.get("conditional_worker_count", 0),
                "active_workers": [],
                "current_stage": None,
                "workers": [
                    {
                        "worker_id": worker_id,
                        "status": worker.get("status", "registered") if isinstance(worker, dict) else "registered",
                        "trigger": worker.get("trigger") if isinstance(worker, dict) else None,
                    }
                    for worker_id, worker in workers.items()
                ],
                "source_profile": f"departments/{item['profile']}/hermes/config.yaml",
            }
        )
    return rows


def _communication_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in _registry().get("events", []):
        if not isinstance(event, dict):
            continue
        rows.append(
            {
                "event_type": event.get("event_type"),
                "status": str(event.get("status", "PLANNED")),
                "layer": event.get("layer", "cross_domain"),
                "producer": event.get("producer"),
                "consumers": event.get("consumers", []),
                "case_binding": event.get("case_binding"),
                "source": event.get("source"),
                "live": False,
                "transport": "registry-only",
            }
        )
    return rows


def build_operations_snapshot() -> dict[str, Any]:
    """Build the browser projection without runtime credentials."""

    observed_at = datetime.now(timezone.utc).isoformat()
    departments = _department_rows()
    communications = _communication_rows()
    runtime = runtime_snapshot()
    run = runtime.get("run") if isinstance(runtime.get("run"), dict) else None

    if run:
        runtime_departments = run.get("departments", {})
        for row in departments:
            live = runtime_departments.get(row["department_code"], {})
            if not isinstance(live, dict):
                continue
            live_status = str(live.get("status", "IDLE"))
            row["status"] = "IDLE" if live_status == "COMPLETED" else live_status
            row["runtime_observed"] = True
            row["active_worker_count"] = len(live.get("active_worker_ids", []))
            row["active_workers"] = list(live.get("active_worker_ids", []))
            row["current_stage"] = live.get("current_stage")
            row["status_reason"] = (
                str(live.get("last_message"))
                if live_status == "COMPLETED" and live.get("last_message")
                else f"{run.get('phase', 'LangGraph 실행')} · 실제 runtime projection"
            )

    live_messages = list(run.get("messages", [])) if run else []
    agent_statuses = agent_status_snapshot()
    for row in departments:
        department_agents = [
            item
            for item in agent_statuses["agents"]
            if item.get("department_code") == row["department_code"]
        ]
        if not department_agents:
            continue
        active_agents = [
            item["agent_id"]
            for item in department_agents
            if item.get("status") in {"QUEUED", "RUNNING", "WAITING_APPROVAL"}
        ]
        row["runtime_observed"] = True
        row["active_worker_count"] = len(active_agents)
        row["active_workers"] = active_agents
        if active_agents:
            row["status"] = "RUNNING"
        elif any(item.get("status") == "ERROR" for item in department_agents):
            row["status"] = "ERROR"
        elif any(item.get("status") == "BLOCKED" for item in department_agents):
            row["status"] = "BLOCKED"
        else:
            row["status"] = "IDLE"
    implemented = sum(1 for item in communications if str(item["status"]).startswith("IMPLEMENTED"))
    planned = sum(1 for item in communications if item["status"] == "PLANNED")
    runtime_status = str(runtime.get("status", "OFFLINE"))
    runtime_connected = run is not None

    return {
        "schema_version": "operator-operations.v1",
        "observed_at": observed_at,
        "status": "CONNECTED" if runtime_connected and runtime_status in {"QUEUED", "RUNNING", "COMPLETED"} else "DEGRADED",
        "runtime_connected": runtime_connected,
        "event_bridge_connected": bool(agent_statuses["sequence"]),
        "sequence": agent_statuses["sequence"],
        "agent_statuses": agent_statuses["agents"],
        "agent_status_events": agent_statuses["events"],
        "message_count": len(live_messages),
        "implemented_event_contracts": implemented,
        "planned_event_contracts": planned,
        "departments": departments,
        "communications": communications,
        "runtime": {
            "status": runtime_status,
            "run_id": run.get("run_id") if run else None,
            "workflow": run.get("workflow") if run else None,
            "phase": run.get("phase") if run else None,
            "departments": run.get("departments", {}) if run else {},
            "active_workers": run.get("active_workers", []) if run else [],
            "performance_metrics": run.get("performance_metrics", []) if run else [],
            "active_handoff": run.get("active_handoff") if run else None,
            "observability": runtime.get("observability", {}),
            "messages": live_messages,
            "result": run.get("result") if run else None,
            "approval": run.get("approval") if run else None,
            "error": run.get("error") if run else None,
        },
        "warnings": [
            "실행 중인 LangGraph가 없으면 직원은 이동하거나 대화하지 않습니다.",
            "Event 목록은 Registry 계약이며 live message와 구분됩니다.",
            "포트폴리오 결과는 비바인딩·수동 검토 대상이며 주문·승인을 수행하지 않습니다.",
            *(["현재 BFF 프로세스가 실제 LangGraph runtime event를 관찰 중입니다."] if runtime_connected else ["실시간 LangGraph runtime이 없어 모든 직원은 대기 상태입니다."]),
        ],
    }


@lru_cache(maxsize=1)
def cached_registry_counts() -> tuple[int, int]:
    """Keep the small deterministic helper used by health checks and tests."""

    snapshot = build_operations_snapshot()
    return snapshot["implemented_event_contracts"], snapshot["planned_event_contracts"]
