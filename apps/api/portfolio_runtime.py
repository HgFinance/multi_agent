"""BFF adapter for the advisory portfolio-recommendation LangGraph.

The browser never runs a worker. This module starts the existing orchestration
graph in a background task, records only its runtime projection, and exposes a
safe, non-binding portfolio result to the operator BFF.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from orchestration.workflows.portfolio_recommendation import (
    run_portfolio_recommendation_pipeline_async,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(__file__).with_name("portfolio_catalog.json")

STAGE_DEPARTMENT = {
    "research": "research-department",
    "trading": "trading-department",
    "risk": "risk-management",
    "qa": "qa-department",
    "accounting": "accounting-portfolio-department",
    "ceo": "ceo-agent",
}
STAGE_ORDER = tuple(STAGE_DEPARTMENT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one_line(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() or "실행 결과 요약이 없습니다."


def load_test_catalog() -> list[dict[str, Any]]:
    """Load the replaceable local catalog used when Supabase is unavailable."""

    try:
        value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _runtime_department(code: str) -> dict[str, Any]:
    return {
        "department_code": code,
        "status": "IDLE",
        "current_stage": None,
        "active_worker_ids": [],
        "last_message": None,
        "updated_at": _now(),
    }


class PortfolioRuntime:
    """Small process-local projection; it is not a source of financial truth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, threading.Thread] = {}
        self._job: dict[str, Any] | None = None

    def _base_job(self, job_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": job_id,
            "workflow": "portfolio-recommendation-full",
            "status": "QUEUED",
            "phase": "사용자 프로필 검증 대기",
            "started_at": None,
            "updated_at": _now(),
            "profile_user_id": str(profile.get("user_id", "")),
            "active_workers": [],
            "departments": {code: _runtime_department(code) for code in STAGE_DEPARTMENT.values()},
            "active_handoff": None,
            "messages": [],
            "result": None,
            "error": None,
        }

    def start(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._job and self._job.get("status") in {"QUEUED", "RUNNING"}:
                raise RuntimeError("portfolio_recommendation_already_running")
            job_id = f"portfolio-run-{uuid4().hex}"
            self._job = self._base_job(job_id, profile)
            task = threading.Thread(target=lambda: asyncio.run(self._run(job_id, dict(profile))), daemon=True)
            self._tasks[job_id] = task
            task.start()
            return {"run_id": job_id, "status": "QUEUED", "workflow": self._job["workflow"]}

    def _job_for(self, job_id: str) -> dict[str, Any] | None:
        return self._job if self._job and self._job.get("run_id") == job_id else None

    def _message(self, job: dict[str, Any], text: str, *, kind: str, department: str | None = None, worker_id: str | None = None) -> None:
        job["messages"].append({
            "id": f"runtime-{uuid4().hex}",
            "occurred_at": _now(),
            "kind": kind,
            "department_code": department,
            "worker_id": worker_id,
            "text": _one_line(text),
        })
        job["messages"] = job["messages"][-60:]

    def _handoff(self, job: dict[str, Any], from_stage: str, to_stage: str, summary: str) -> None:
        source = STAGE_DEPARTMENT[from_stage]
        target = STAGE_DEPARTMENT[to_stage]
        job["active_handoff"] = {
            "from_department": source,
            "to_department": target,
            "from_head": f"{source}:head",
            "to_head": f"{target}:head",
            "status": "RUNNING",
            "title": f"{source} → {target}",
            "message": _one_line(summary or "부서장 간 실행 컨텍스트를 인계합니다."),
            "occurred_at": _now(),
            "expires_at": datetime.now(timezone.utc).timestamp() + 3.0,
        }
        self._message(job, job["active_handoff"]["message"], kind="department_handoff", department=source)

    def _event(self, job_id: str, event: Mapping[str, Any]) -> None:
        with self._lock:
            job = self._job_for(job_id)
            if job is None:
                return
            kind = str(event.get("kind", ""))
            stage = str(event.get("stage", ""))
            department = STAGE_DEPARTMENT.get(stage)
            job["status"] = "RUNNING"
            job["started_at"] = job["started_at"] or _now()
            job["updated_at"] = _now()
            if kind == "department_started" and department:
                worker_ids = [str(item) for item in event.get("worker_ids", [])]
                row = job["departments"][department]
                row.update({"status": "RUNNING", "current_stage": stage, "active_worker_ids": worker_ids, "updated_at": _now()})
                job["phase"] = f"{department} worker 실행 중"
                self._message(job, f"{department}의 독립 Worker {len(worker_ids)}개 그래프 실행을 시작합니다.", kind="department_started", department=department)
            elif kind == "worker_started" and department:
                worker = {
                    "worker_id": str(event.get("worker_id", "")),
                    "department_code": department,
                    "stage": stage,
                    "role": str(event.get("role", "")),
                    "status": "RUNNING",
                    "started_at": _now(),
                    "summary": None,
                }
                job["active_workers"] = [item for item in job["active_workers"] if item["worker_id"] != worker["worker_id"]]
                job["active_workers"].append(worker)
                job["departments"][department]["active_worker_ids"] = [item["worker_id"] for item in job["active_workers"] if item["department_code"] == department]
            elif kind == "worker_completed" and department:
                worker_id = str(event.get("worker_id", ""))
                summary = _one_line(event.get("summary"))
                job["active_workers"] = [item for item in job["active_workers"] if item["worker_id"] != worker_id]
                job["departments"][department]["active_worker_ids"] = [item["worker_id"] for item in job["active_workers"] if item["department_code"] == department]
                self._message(job, summary, kind="worker_summary", department=department, worker_id=worker_id)
            elif kind == "department_completed" and department:
                status = str(event.get("status", "DEGRADED"))
                row = job["departments"][department]
                row.update({"status": "COMPLETED" if status == "COMPLETED" else "DEGRADED", "current_stage": None, "active_worker_ids": [], "last_message": _one_line(event.get("message")), "updated_at": _now()})
                current_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
                if current_index >= 0 and current_index + 1 < len(STAGE_ORDER):
                    next_stage = STAGE_ORDER[current_index + 1]
                    self._handoff(job, stage, next_stage, str(event.get("message", "")))
            elif kind == "department_blocked" and department:
                job["departments"][department].update({"status": "BLOCKED", "current_stage": None, "active_worker_ids": [], "updated_at": _now()})
                self._message(job, str(event.get("message", "실행 입력이 준비되지 않아 안전하게 중단했습니다.")), kind="department_blocked", department=department)

    async def _run(self, job_id: str, profile: dict[str, Any]) -> None:
        try:
            # The BFF is a local read-only projection; never send a browser-triggered
            # prototype run to an external LangSmith endpoint.
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ["LANGSMITH_TRACING"] = "false"
            database_url = os.getenv("DATABASE_URL", "").strip()
            if database_url:
                result = await run_portfolio_recommendation_pipeline_async(profile, event_callback=lambda event: self._event(job_id, event))
            else:
                result = await run_portfolio_recommendation_pipeline_async(
                    profile,
                    load_test_catalog(),
                    event_callback=lambda event: self._event(job_id, event),
                )
            with self._lock:
                job = self._job_for(job_id)
                if job is None:
                    return
                job["active_workers"] = []
                job["active_handoff"] = None
                job["result"] = result
                job["status"] = str(result.get("pipeline_status", "DEGRADED"))
                job["phase"] = "포트폴리오 추천 결과 준비 완료" if job["status"] == "COMPLETED" else "안전 보류 — 추가 검토 필요"
                job["updated_at"] = _now()
                self._message(job, "추천 결과가 준비되었습니다. 주문·승인·원장 변경은 수행하지 않았습니다.", kind="run_completed")
        except Exception as exc:  # noqa: BLE001 - BFF boundary fails closed.
            with self._lock:
                job = self._job_for(job_id)
                if job is None:
                    return
                job["active_workers"] = []
                job["active_handoff"] = None
                job["status"] = "ERROR"
                job["phase"] = "실행 오류 — 안전 보류"
                job["error"] = f"{type(exc).__name__}: {_one_line(exc)}"
                job["updated_at"] = _now()
                self._message(job, "LangGraph 실행이 실패해 추천 결과를 확정하지 않았습니다.", kind="run_error")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None:
                return {"status": "OFFLINE", "run": None}
            job = json.loads(json.dumps(self._job, default=str))
            handoff = job.get("active_handoff")
            if handoff and float(handoff.get("expires_at", 0)) <= datetime.now(timezone.utc).timestamp():
                job["active_handoff"] = None
            return {"status": self._job.get("status", "OFFLINE"), "run": job}

    def get(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        run = snapshot.get("run")
        return run if isinstance(run, dict) and run.get("run_id") == run_id else None


RUNTIME = PortfolioRuntime()


def runtime_snapshot() -> dict[str, Any]:
    return RUNTIME.snapshot()
