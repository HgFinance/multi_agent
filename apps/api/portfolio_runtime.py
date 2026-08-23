"""BFF adapter for the advisory portfolio-recommendation LangGraph.

The browser never runs a worker. This module starts the existing orchestration
graph in a background task, records only its durable runtime projection, and
exposes a safe, non-binding portfolio result to the operator BFF.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from portfolio_universe import DEFAULT_UNIVERSE_ID, enrich_suitability_result

try:
    from kanban_status_bridge import KANBAN_STATUS_BRIDGE
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.kanban_status_bridge import KANBAN_STATUS_BRIDGE

try:
    from governance_client import fetch_mandate_policy_content
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.governance_client import fetch_mandate_policy_content

try:
    import kanban_tracker
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api import kanban_tracker

from portfolio_store import PortfolioRuntimeStore, _process_alive

from orchestration.workflows.portfolio_recommendation import (
    run_portfolio_recommendation_pipeline_async,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(__file__).with_name("portfolio_catalog.json")

STAGE_DEPARTMENT = {
    "research": "research-department",
    # 2026-08-10: orchestration/workflows/portfolio_recommendation.py의 DEPARTMENTS에
    # quant를 research 바로 다음으로 배선했다(요청 시점 백테스트, 팀 합의). 이 dict가
    # 그 파일과 별도로 유지되는 자기 것이라 여기 추가하지 않으면 quant 단계의
    # department_started/completed/worker_* 이벤트가 department=None으로 떨어져
    # (아래 STAGE_DEPARTMENT.get(stage) 가드) 조용히 Kanban·Operator UI에서 사라지고,
    # handoff는 research -> trading으로 quant를 건너뛴 것처럼 보인다. 순서는
    # STAGE_ORDER가 이 dict의 삽입 순서를 그대로 쓰므로 반드시 research 다음에 둔다.
    "quant": "quant-backtest-department",
    "trading": "trading-department",
    "risk": "risk-management",
    "qa": "qa-department",
    "accounting": "accounting-portfolio-department",
    "ceo": "ceo-agent",
}
STAGE_ORDER = tuple(STAGE_DEPARTMENT)

EMBEDDED_WORKER_ENABLED = os.getenv("PORTFOLIO_RUNTIME_EMBEDDED_WORKER", "false").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}

class PortfolioRuntimeLeaseLost(RuntimeError):
    """Raised when a worker loses its durable queue lease."""

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# _process_alive 는 portfolio_store 가 정본이다(위 import). 여기 사본을 두면
# 같은 판정이 두 곳에 생기고, 실제로 그래서 한쪽만 고쳐진 채로 남아 있었다 —
# 테스트가 이 모듈의 이름만 patch 하면 store 쪽 사본은 그대로 살아 돈다.


def _correlation_hash(profile: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(profile), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
def _request_fingerprint(profile: Mapping[str, Any]) -> str:
    return _correlation_hash({key: value for key, value in profile.items() if key != "as_of"})


def _one_line(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() or "실행 결과 요약이 없습니다."


def _department_label(department: str | None) -> str:
    return {
        "research-department": "리서치본부",
        "quant-backtest-department": "퀀트/백테스트본부",
        "trading-department": "트레이딩본부",
        "risk-management": "리스크관리본부",
        "qa-department": "AI QA·감사",
        "accounting-portfolio-department": "회계·포트폴리오본부",
        "ceo-agent": "CEO 오피스",
    }.get(department or "", department or "부서")


def langsmith_observability() -> dict[str, Any]:
    """Expose only safe tracing configuration metadata to the operator UI."""

    tracing_enabled = os.getenv("LANGSMITH_TRACING", "").casefold() in {
        "1", "true", "yes", "on"
    }
    endpoint = os.getenv("LANGSMITH_ENDPOINT", "").strip()
    api_key_configured = bool(os.getenv("LANGSMITH_API_KEY", "").strip())
    project = os.getenv("LANGSMITH_PROJECT")
    parsed = urlsplit(endpoint)
    safe_endpoint = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
    return {
        "status": "READY" if tracing_enabled and api_key_configured and safe_endpoint else "DISABLED",
        "configured": bool(api_key_configured and safe_endpoint),
        "tracing_enabled": tracing_enabled,
        "endpoint": safe_endpoint,
        "project": project or None,
    }


def _event_message(
    *,
    kind: str,
    department: str | None,
    worker_id: str | None = None,
    role: str | None = None,
    summary: Any = None,
) -> str:
    """Turn internal TEST output into one dynamic operator-facing sentence."""
    department_name = _department_label(department)
    worker_name = " ".join(str(role or worker_id or "Worker").split())
    if kind == "worker_started":
        return f"{department_name}의 {worker_name}가 근거 검토를 시작했습니다."
    if kind == "worker_completed":
        clean = _one_line(summary)
        if clean.lower().startswith("test ") or clean.lower().startswith("worker graph"):
            return f"{department_name}의 {worker_name}가 검토를 완료했습니다."
        return f"{department_name}의 {worker_name} 검토 완료 — {clean}"
    return _one_line(summary or f"{department_name} 실행 이벤트")


def portfolio_data_mode() -> str:
    """Return the explicit data-plane mode, refusing production fixtures."""

    mode = os.getenv("PORTFOLIO_DATA_MODE", "production").strip().lower()
    if mode not in {"production", "test"}:
        raise RuntimeError("unsupported_portfolio_data_mode")
    if mode == "test" and os.getenv("APP_ENV", "production").strip().lower() not in {
        "local",
        "test",
    }:
        raise RuntimeError("portfolio_test_data_forbidden")
    return mode


def load_test_catalog() -> list[dict[str, Any]]:
    """Load the deterministic catalog used only in explicit local/test mode."""

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
        "kanban_task_id": None,
        "updated_at": _now(),
    }


class PortfolioRuntime:
    """Durable advisory projection; it is not a source of financial truth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, threading.Thread] = {}
        self._owner_token = uuid4().hex
        self._claim_workers: dict[str, str] = {}
        default_store_dir = os.getenv("PORTFOLIO_RUNTIME_STORE_DIR", "/tmp").strip() or "/tmp"
        store_path = os.getenv("PORTFOLIO_RUNTIME_STORE_PATH", "").strip()
        self._store = PortfolioRuntimeStore(store_path or str(Path(default_store_dir) / "hgfinance-portfolio.sqlite3"))
        self._store.requeue_nonterminal()
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._job: dict[str, Any] | None = self._store.latest()
        active_run_id = self._store.active_run_id()
        active_owner_pid = self._store.active_run_owner_pid()
        stale_job: dict[str, Any] | None = None
        recovery_profile: dict[str, Any] | None = None
        if not self._store.enabled:
            if self._job and self._job.get("status") in {"QUEUED", "RUNNING"}:
                stale_job = self._job
        elif active_run_id and not _process_alive(active_owner_pid):
            persisted = self._store.get(active_run_id)
            if persisted and persisted.get("status") in {"QUEUED", "RUNNING"}:
                stale_job = persisted
        if stale_job is not None:
            candidate = stale_job.get("_profile")
            if isinstance(candidate, Mapping):
                recovery_profile = dict(candidate)
                stale_job["status"] = "QUEUED"
                stale_job["phase"] = "실행 재시작 후 durable queue에서 재개 대기"
                stale_job["error"] = "portfolio_runtime_requeued_after_worker_restart"
                stale_job["active_workers"] = []
            else:
                stale_job["status"] = "ERROR"
                stale_job["phase"] = "실행 재시작 감지 — 안전 보류"
                stale_job["error"] = "portfolio_runtime_restarted_before_terminal_state"
            stale_job["updated_at"] = _now()
            self._store.save(stale_job)
            if self._job and self._job.get("run_id") == stale_job.get("run_id"):
                self._job = stale_job
        if (
            stale_job is not None
            and recovery_profile is not None
            and self._store.enabled
            and self._store.reserve_active_run(
                stale_job["run_id"],
                stale_job["updated_at"],
                os.getpid(),
                self._owner_token,
            )
        ):
            self._job = stale_job
            self._dispatch(stale_job["run_id"], recovery_profile)

    def _base_job(self, job_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": job_id,
            "workflow": "portfolio-recommendation-full",
            "status": "QUEUED",
            "phase": "사용자 프로필 검증 대기",
            "_profile": dict(profile),
            "started_at": None,
            "updated_at": _now(),
            "profile_user_id": str(profile.get("user_id", "")),
            "trace_id": str(profile.get("trace_id") or ""),
            "case_id": profile.get("case_id"),
            "mandate_id": profile.get("mandate_id"),
            "mandate_version_id": profile.get("mandate_version_id"),
            "policy_hash": profile.get("policy_hash"),
            "idempotency_key": profile.get("idempotency_key"),
            "request_fingerprint": _request_fingerprint(profile),
            "input_hash": _correlation_hash(profile),
            "active_workers": [],
            "departments": {code: _runtime_department(code) for code in STAGE_DEPARTMENT.values()},
            "active_handoff": None,
            "messages": [],
            "pipeline_events": [],
            "performance_metrics": [],
            "result": None,
            "approval": {"status": "PENDING", "binding": False, "approved_at": None, "comment": None},
            "error": None,
        }

    def start(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            idempotency_key = str(profile.get("idempotency_key") or "").strip()
            owner = str(profile.get("user_id") or "")
            fingerprint = _request_fingerprint(profile)
            if idempotency_key:
                existing = self._idempotency.get((owner, idempotency_key))
                if existing is not None:
                    existing_id, existing_fingerprint = existing
                    if existing_fingerprint != fingerprint:
                        raise RuntimeError("portfolio_recommendation_idempotency_conflict")
                    existing_job = self._job_for(existing_id)
                    if existing_job is not None:
                        return {
                            "run_id": existing_job["run_id"],
                            "status": existing_job["status"],
                            "workflow": existing_job["workflow"],
                            "idempotent_replay": True,
                        }
                persisted_job = self._store.find_by_idempotency(owner, idempotency_key)
                if persisted_job is not None:
                    if persisted_job.get("request_fingerprint") != fingerprint:
                        raise RuntimeError("portfolio_recommendation_idempotency_conflict")
                    return {
                        "run_id": persisted_job["run_id"],
                        "status": persisted_job["status"],
                        "workflow": persisted_job["workflow"],
                        "idempotent_replay": True,
                    }
            if (
                self._job
                and self._job.get("status") in {"QUEUED", "RUNNING"}
                and not self._store.enabled
            ):
                raise RuntimeError("portfolio_recommendation_already_running")
            job_id = f"portfolio-run-{uuid4().hex}"
            normalized_profile = dict(profile)
            normalized_profile.setdefault("as_of", _now())
            job = self._base_job(job_id, normalized_profile)
            self._job = job
            try:
                self._store.save(self._job)
                self._store.enqueue(job_id)
            except Exception as exc:
                self._job = None
                raise RuntimeError("portfolio_runtime_store_unavailable") from exc
            if idempotency_key:
                self._idempotency[(owner, idempotency_key)] = (job_id, fingerprint)
            self._dispatch(job_id, normalized_profile)
            return {
                "run_id": job_id,
                "status": "QUEUED",
                "workflow": self._job["workflow"],
                "idempotent_replay": False,
            }

    def _dispatch(self, job_id: str, profile: Mapping[str, Any]) -> None:
        if self._store.enabled:
            self._store.enqueue(job_id)
            if not EMBEDDED_WORKER_ENABLED:
                return
            task = threading.Thread(
                target=lambda: self.run_once(
                    worker_id=f"embedded-{os.getpid()}-{job_id}",
                ),
                daemon=True,
            )
        else:
            task = threading.Thread(
                target=lambda: asyncio.run(self._run(job_id, dict(profile))),
                daemon=True,
            )
        self._tasks[job_id] = task
        task.start()

    async def _run_with_heartbeat(
        self,
        job_id: str,
        profile: Mapping[str, Any],
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        interval = max(lease_seconds / 3.0, 1.0)
        stopped = asyncio.Event()

        async def keepalive() -> None:
            while not stopped.is_set():
                await asyncio.sleep(interval)
                if not self._store.heartbeat(job_id, worker_id, lease_seconds):
                    raise PortfolioRuntimeLeaseLost(job_id)

        work_task = asyncio.create_task(self._run(job_id, dict(profile)))
        heartbeat_task = asyncio.create_task(keepalive())
        try:
            done, _ = await asyncio.wait(
                {work_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_task.result()
            await work_task
        finally:
            stopped.set()
            for task in (work_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            for task in (work_task, heartbeat_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def run_once(self, worker_id: str | None = None, lease_seconds: float = 60.0) -> bool:
        """Claim one durable queue item and execute it synchronously."""
        if not self._store.enabled:
            return False
        worker = worker_id or f"portfolio-worker-{os.getpid()}"
        job = self._store.claim_next(worker, lease_seconds)
        if not isinstance(job, dict):
            return False
        run_id = str(job.get("run_id", ""))
        profile = job.get("_profile")
        if not run_id or not isinstance(profile, Mapping):
            self._store.release_claim(run_id, worker)
            return False
        self._claim_workers[run_id] = worker
        try:
            asyncio.run(
                self._run_with_heartbeat(
                    run_id,
                    profile,
                    worker,
                    lease_seconds,
                )
            )
        except PortfolioRuntimeLeaseLost:
            return False
        finally:
            self._claim_workers.pop(run_id, None)
            self._store.release_claim(run_id, worker)
        return True

    def _job_for(self, job_id: str) -> dict[str, Any] | None:
        if self._job and self._job.get("run_id") == job_id:
            return self._job
        persisted = self._store.get(job_id)
        if isinstance(persisted, dict):
            self._job = persisted
            return persisted
        return None
    def _save_job(self, job: Mapping[str, Any]) -> None:
        run_id = str(job.get("run_id", ""))
        worker_id = self._claim_workers.get(run_id)
        if worker_id and not self._store.is_claim_owner(run_id, worker_id):
            raise PortfolioRuntimeLeaseLost(run_id)
        self._store.save(job)

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

    def _on_kanban_task_id(self, job_id: str, department: str, task_id: str) -> None:
        """Callback invoked from a kanban_tracker daemon thread once the real Task id is known."""

        with self._lock:
            job = self._job_for(job_id)
            if job is None:
                return
            job["departments"][department]["kanban_task_id"] = task_id
            self._save_job(job)

    def _record_event(self, job_id: str, event: Mapping[str, Any]) -> None:
        """Apply one pipeline event to the process-local operator projection."""

        with self._lock:
            job = self._job_for(job_id)
            if job is None:
                return
            pipeline_event = event.get("pipeline_event")
            if isinstance(pipeline_event, Mapping):
                job.setdefault("pipeline_events", []).append(dict(pipeline_event))
                job["pipeline_events"] = job["pipeline_events"][-200:]

            kind = str(event.get("kind", ""))
            stage = str(event.get("stage", ""))
            department = STAGE_DEPARTMENT.get(stage)
            job["status"] = "RUNNING"
            job["started_at"] = job["started_at"] or _now()
            job["updated_at"] = _now()

            if kind == "department_started" and department:
                worker_ids = [str(item) for item in event.get("worker_ids", [])]
                row = job["departments"][department]
                row.update(
                    {
                        "status": "RUNNING",
                        "current_stage": stage,
                        "active_worker_ids": worker_ids,
                        "updated_at": _now(),
                    }
                )
                job["phase"] = f"{department} worker 실행 중"
                self._message(
                    job,
                    f"{_department_label(department)}의 독립 Worker {len(worker_ids)}개 그래프 실행을 시작합니다.",
                    kind="department_started",
                    department=department,
                )
                kanban_tracker.track_department_started(
                    job_id,
                    department,
                    f"{_department_label(department)} — {job.get('trace_id') or job_id}",
                    on_task_id=lambda task_id, department=department: self._on_kanban_task_id(
                        job_id, department, task_id
                    ),
                )
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
                job["active_workers"] = [
                    item
                    for item in job["active_workers"]
                    if item["worker_id"] != worker["worker_id"]
                ]
                job["active_workers"].append(worker)
                KANBAN_STATUS_BRIDGE.publish_task_event(
                    {
                        "event_id": f"{job_id}:worker_started:{worker['worker_id']}",
                        "department_code": department,
                        "agent_id": worker["worker_id"],
                        "worker_id": worker["worker_id"],
                        "role": worker["role"],
                        "status": "running",
                        "reason": "LangGraph Worker graph 실행 시작",
                    }
                )
                self._message(
                    job,
                    _event_message(
                        kind="worker_started",
                        department=department,
                        worker_id=worker["worker_id"],
                        role=worker["role"],
                    ),
                    kind="worker_started",
                    department=department,
                    worker_id=worker["worker_id"],
                )
                job["departments"][department]["active_worker_ids"] = [
                    item["worker_id"] for item in job["active_workers"]
                    if item["department_code"] == department
                ]
            elif kind == "worker_completed" and department:
                worker_id = str(event.get("worker_id", ""))
                summary = _one_line(event.get("summary"))
                performance = event.get("performance")
                if isinstance(performance, Mapping):
                    job["performance_metrics"].append(dict(performance))
                    job["performance_metrics"] = job["performance_metrics"][-100:]
                KANBAN_STATUS_BRIDGE.publish_task_event(
                    {
                        "event_id": f"{job_id}:worker_completed:{worker_id}",
                        "department_code": department,
                        "agent_id": worker_id,
                        "worker_id": worker_id,
                        "role": str(event.get("role", "")),
                        "status": "completed",
                        "reason": summary or "LangGraph Worker graph 실행 완료",
                    }
                )
                job["active_workers"] = [
                    item for item in job["active_workers"]
                    if item["worker_id"] != worker_id
                ]
                job["departments"][department]["active_worker_ids"] = [
                    item["worker_id"] for item in job["active_workers"]
                    if item["department_code"] == department
                ]
                self._message(
                    job,
                    _event_message(
                        kind="worker_completed",
                        department=department,
                        worker_id=worker_id,
                        role=str(event.get("role", "")),
                        summary=summary,
                    ),
                    kind="worker_summary",
                    department=department,
                    worker_id=worker_id,
                )
            elif kind == "department_skipped" and department:
                row = job["departments"][department]
                row.update(
                    {
                        "status": "SKIPPED",
                        "current_stage": None,
                        "active_worker_ids": [],
                        "last_message": _one_line(event.get("message")),
                        "updated_at": _now(),
                    }
                )
                self._message(
                    job,
                    f"{_department_label(department)}는 CEO task plan에 따라 이번 요청에서 호출하지 않습니다.",
                    kind="department_skipped",
                    department=department,
                )
            elif kind == "department_completed" and department:
                status = str(event.get("status", "DEGRADED"))
                row = job["departments"][department]
                row.update(
                    {
                        "status": (
                            "COMPLETED"
                            if status == "COMPLETED"
                            else "SKIPPED"
                            if status in {"SKIPPED", "NOT_REQUESTED", "NOT_APPLICABLE"}
                            else "DEGRADED"
                        ),
                        "current_stage": None,
                        "active_worker_ids": [],
                        "last_message": _one_line(event.get("message")),
                        "updated_at": _now(),
                    }
                )
                current_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
                if status != "SKIPPED" and current_index >= 0 and current_index + 1 < len(STAGE_ORDER):
                    self._handoff(
                        job,
                        stage,
                        STAGE_ORDER[current_index + 1],
                        str(event.get("message", "")),
                    )
                if status != "SKIPPED":
                    kanban_tracker.track_department_completed(
                        job_id, department, status, str(event.get("message", ""))
                    )
            elif kind == "department_blocked" and department:
                job["departments"][department].update(
                    {
                        "status": "BLOCKED",
                        "current_stage": None,
                        "active_worker_ids": [],
                        "updated_at": _now(),
                    }
                )
                self._message(
                    job,
                    str(event.get("message", "실행 입력이 준비되지 않아 안전하게 중단했습니다.")),
                    kind="department_blocked",
                    department=department,
                )
                kanban_tracker.track_department_blocked(
                    job_id, department, str(event.get("message", ""))
                )
            self._save_job(job)

    def _event(self, job_id: str, event: Mapping[str, Any]) -> None:
        # Kept as the stable callback name for existing callers.
        return self._record_event(job_id, event)
        with self._lock:
            job = self._job_for(job_id)
        if job is None:
            return
            pipeline_event = event.get("pipeline_event")
            if isinstance(pipeline_event, Mapping):
                job.setdefault("pipeline_events", []).append(dict(pipeline_event))
                job["pipeline_events"] = job["pipeline_events"][-200:]
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
                self._message(job, f"{_department_label(department)}의 독립 Worker {len(worker_ids)}개 그래프 실행을 시작합니다.", kind="department_started", department=department)
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
                KANBAN_STATUS_BRIDGE.publish_task_event(
                    {
                        "event_id": f"{job_id}:worker_started:{worker['worker_id']}",
                        "department_code": department,
                        "agent_id": worker["worker_id"],
                        "worker_id": worker["worker_id"],
                        "role": worker["role"],
                        "status": "running",
                        "reason": "LangGraph Worker graph 실행 시작",
                    }
                )
                self._message(
                    job,
                    _event_message(
                        kind="worker_started",
                        department=department,
                        worker_id=worker["worker_id"],
                        role=worker["role"],
                    ),
                    kind="worker_started",
                    department=department,
                    worker_id=worker["worker_id"],
                )
                job["departments"][department]["active_worker_ids"] = [item["worker_id"] for item in job["active_workers"] if item["department_code"] == department]
            elif kind == "worker_completed" and department:
                worker_id = str(event.get("worker_id", ""))
                summary = _one_line(event.get("summary"))
                KANBAN_STATUS_BRIDGE.publish_task_event(
                    {
                        "event_id": f"{job_id}:worker_completed:{worker_id}",
                        "department_code": department,
                        "agent_id": worker_id,
                        "worker_id": worker_id,
                        "role": str(event.get("role", "")),
                        "status": "completed",
                        "reason": summary or "LangGraph Worker graph 실행 완료",
                    }
                )
                job["active_workers"] = [item for item in job["active_workers"] if item["worker_id"] != worker_id]
                job["departments"][department]["active_worker_ids"] = [item["worker_id"] for item in job["active_workers"] if item["department_code"] == department]
                self._message(
                    job,
                    _event_message(
                        kind="worker_completed",
                        department=department,
                        worker_id=worker_id,
                        role=str(event.get("role", "")),
                        summary=summary,
                    ),
                    kind="worker_summary",
                    department=department,
                    worker_id=worker_id,
                )
            elif kind == "department_skipped" and department:
                row = job["departments"][department]
                row.update({
                    "status": "SKIPPED",
                    "current_stage": None,
                    "active_worker_ids": [],
                    "last_message": _one_line(event.get("message")),
                    "updated_at": _now(),
                })
                self._message(job, f"{_department_label(department)}는 CEO task plan에 따라 이번 요청에서 호출하지 않습니다.", kind="department_skipped", department=department)
            elif kind == "department_completed" and department:
                status = str(event.get("status", "DEGRADED"))
                row = job["departments"][department]
                row.update({"status": "COMPLETED" if status == "COMPLETED" else "SKIPPED" if status in {"SKIPPED", "NOT_REQUESTED", "NOT_APPLICABLE"} else "DEGRADED", "current_stage": None, "active_worker_ids": [], "last_message": _one_line(event.get("message")), "updated_at": _now()})
                current_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
                if status != "SKIPPED" and current_index >= 0 and current_index + 1 < len(STAGE_ORDER):
                    next_stage = STAGE_ORDER[current_index + 1]
                    self._handoff(job, stage, next_stage, str(event.get("message", "")))
            elif kind == "department_blocked" and department:
                job["departments"][department].update({"status": "BLOCKED", "current_stage": None, "active_worker_ids": [], "updated_at": _now()})
                self._message(job, str(event.get("message", "실행 입력이 준비되지 않아 안전하게 중단했습니다.")), kind="department_blocked", department=department)

    async def _run(self, job_id: str, profile: dict[str, Any]) -> None:
        try:
            # Respect explicit backend LangSmith configuration. The browser never
            # receives credentials; only safe observability metadata is projected.
            data_mode = portfolio_data_mode()
            # CEO task planner input: best-effort, never blocks the run (see
            # governance_client.fetch_mandate_policy_content docstring).
            mandate_id = str(profile.get("mandate_id") or "").strip()
            if mandate_id:
                profile["mandate_policy"] = await fetch_mandate_policy_content(mandate_id)
            if data_mode == "production":
                result = await run_portfolio_recommendation_pipeline_async(profile, event_callback=lambda event: self._event(job_id, event))
            else:
                result = await run_portfolio_recommendation_pipeline_async(
                    profile,
                    load_test_catalog(),
                    event_callback=lambda event: self._event(job_id, event),
                )
            data_context = result.get("data_context", {})
            live_source = data_context.get("source") == "CONTROL_DB"
            live_instruments = None
            live_universe_status = None
            if data_mode == "production":
                # A configured live DB must never fall back to the browser TEST
                # catalog. An absent or unavailable control DB therefore yields
                # an empty live universe and a HOLD result.
                live_instruments = (
                    data_context.get("market", {})
                    .get("instrument_universe", {})
                    .get("instruments", [])
                    if live_source
                    else []
                )
                live_universe_status = "LIVE" if live_source else "UNAVAILABLE"

            result = enrich_suitability_result(
                result,
                str(profile.get("universe_id", DEFAULT_UNIVERSE_ID)),
                include_stock=bool(profile.get("include_stock", True)),
                include_derivatives=bool(profile.get("include_derivatives", False)),
                live_instruments=live_instruments,
                live_universe_status=live_universe_status,
            )
            with self._lock:
                job = self._job_for(job_id)
                if job is None:
                    return
                job["active_workers"] = []
                job["active_handoff"] = None
                job["result"] = result
                job["result"]["user_approval"] = dict(job["approval"])
                job["status"] = str(result.get("pipeline_status", "DEGRADED"))
                job["phase"] = "포트폴리오 추천 결과 준비 완료" if job["status"] == "COMPLETED" else "안전 보류 — 추가 검토 필요"
                job["updated_at"] = _now()
                if job["status"] == "COMPLETED":
                    self._message(job, "추천 결과가 준비되었습니다. 주문·승인·원장 변경은 수행하지 않았습니다.", kind="run_completed")
                else:
                    self._message(job, "LangGraph 실행은 끝났지만 Worker 또는 Gate 검증이 안전 보류되어 추천 결과를 확정하지 않았습니다.", kind="run_degraded")
                self._save_job(job)
                self._store.release_active_run(job_id, self._owner_token)
        except Exception as exc:  # noqa: BLE001 - BFF boundary fails closed.
            with self._lock:
                job = self._job_for(job_id)
                if job is None:
                    return
                error_reason = f"LangGraph 실행 오류: {type(exc).__name__}"
                for worker in list(job.get("active_workers", [])):
                    worker_id = str(worker.get("worker_id", ""))
                    department = str(worker.get("department_code", ""))
                    if not worker_id or not department:
                        continue
                    KANBAN_STATUS_BRIDGE.publish_task_event(
                        {
                            "event_id": f"{job_id}:worker_error:{worker_id}",
                            "department_code": department,
                            "agent_id": worker_id,
                            "worker_id": worker_id,
                            "role": worker.get("role"),
                            "status": "error",
                            "reason": error_reason,
                        }
                    )
                    self._message(
                        job,
                        error_reason,
                        kind="worker_error",
                        department=department,
                        worker_id=worker_id,
                    )
                for row in job.get("departments", {}).values():
                    if isinstance(row, dict) and row.get("status") == "RUNNING":
                        row.update({"status": "ERROR", "active_worker_ids": [], "last_message": error_reason, "updated_at": _now()})
                job["active_workers"] = []
                job["active_handoff"] = None
                job["status"] = "ERROR"
                job["phase"] = "실행 오류 — 안전 보류"
                job["error"] = f"{type(exc).__name__}: {_one_line(exc)}"
                job["updated_at"] = _now()
                self._message(job, "LangGraph 실행이 실패해 추천 결과를 확정하지 않았습니다.", kind="run_error")
                self._save_job(job)
                self._store.release_active_run(job_id, self._owner_token)

    def decide(self, run_id: str, decision: str, comment: str | None = None) -> dict[str, Any] | None:
        """Record user approval of an advisory result, never an order approval."""

        with self._lock:
            job = self._job_for(run_id)
            if job is None:
                return None
            result = job.get("result")
            if not isinstance(result, dict):
                raise TypeError("portfolio_recommendation_result_not_ready")
        if decision == "APPROVE":
            suitability = result.get("suitability", {})
            if result.get("pipeline_status") != "COMPLETED" or suitability.get("status") != "MATCHED":
                raise RuntimeError("portfolio_recommendation_approval_blocked")
            if result.get("instrument_recommendations_status") != "COMPLETE":
                raise RuntimeError("portfolio_instrument_recommendations_incomplete")
            if decision not in {"APPROVE", "REJECT"}:
                raise RuntimeError("portfolio_recommendation_approval_invalid")
            approval = {
                "status": decision,
                "binding": False,
                "approved_at": _now(),
                "comment": _one_line(comment, 240) if comment else None,
            }
            job["approval"] = approval
            result["user_approval"] = dict(approval)
            job["phase"] = "사용자 승인 완료 · 주문 단계는 별도" if decision == "APPROVE" else "사용자 추천 거절 · 재분석 가능"
            job["updated_at"] = _now()
            self._message(
                job,
                "사용자가 추천 포트폴리오를 승인했습니다. 이 승인으로 주문은 제출되지 않습니다."
                if decision == "APPROVE"
                else "사용자가 추천 포트폴리오를 거절했습니다.",
                kind="user_approval",
            )
            self._store.save(job)
            return self._public_job(job)
    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(dict(job), default=str))
        value.pop("idempotency_key", None)
        value.pop("request_fingerprint", None)
        value.pop("_profile", None)
        return value
    @property
    def durable(self) -> bool:
        return self._store.enabled

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._store.enabled:
                persisted = self._store.latest()
                if isinstance(persisted, dict):
                    self._job = persisted
            if self._job is None:
                return {"status": "OFFLINE", "run": None}
            job = self._public_job(self._job)
            handoff = job.get("active_handoff")
            if handoff and float(handoff.get("expires_at", 0)) <= datetime.now(timezone.utc).timestamp():
                job["active_handoff"] = None
            return {"status": self._job.get("status", "OFFLINE"), "run": job}

    def get(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        run = snapshot.get("run")
        if isinstance(run, dict) and run.get("run_id") == run_id:
            return run
        persisted = self._store.get(run_id)
        return self._public_job(persisted) if isinstance(persisted, dict) else None


RUNTIME = PortfolioRuntime()


def runtime_snapshot() -> dict[str, Any]:
    snapshot = RUNTIME.snapshot()
    snapshot["observability"] = {"langsmith": langsmith_observability()}
    return snapshot
