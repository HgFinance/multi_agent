"""Persistent file-backed research laboratory.

The lab is append-oriented and human-readable.  Summary files are derived
from the event log; they are never treated as a source of unverified facts.
Writes use a lock plus atomic replacement so a killed agent cannot leave a
half-written state file for the next session.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping

try:  # Linux production path; the fallback keeps the module portable in tests.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from models import (
    ExperimentPlan,
    ExperimentResult,
    Hypothesis,
    Objective,
    ResearchEvent,
    canonical_json,
    from_result_dict,
    stable_id,
    to_dict,
    utc_now,
)


SUMMARY_FILES = (
    "OBJECTIVE.md",
    "STATE.md",
    "KNOWLEDGE.md",
    "EXPERIMENT_LOG.md",
    "FAILURE_MEMORY.md",
    "RESOURCE_MAP.md",
)


class ResearchLabError(RuntimeError):
    pass


class ResearchLab:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.events_path = self.root / "events.jsonl"
        self.state_path = self.root / ".state.json"
        self.objective_path = self.root / "objective.json"
        self.plans_dir = self.root / "plans"
        self.hypotheses_dir = self.root / "hypotheses"
        self.results_dir = self.root / "results"
        self.agent_runs_dir = self.root / "agent-runs"
        self.lock_path = self.root / ".lab.lock"

    def initialize(self, objective: Objective, *, replace: bool = False) -> None:
        if self.objective_path.exists():
            existing = self.objective()
            if existing.goal != objective.goal:
                raise ResearchLabError("lab already has a different objective; use an explicit new lab")
            if replace:
                raise ResearchLabError("replace cannot reset an initialized lab; use an explicit new lab")
            return
        for path in (self.root, self.plans_dir, self.hypotheses_dir, self.results_dir, self.agent_runs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._write_json(self.objective_path, to_dict(objective))
        self._write_json(self.state_path, {
            "schema": "autonomous-research-state.v1",
            "cycle": 0,
            "active_plan_id": None,
            "best_plan_id": None,
            "last_action": "INITIALIZED",
            "uncertainties": ["The objective has not been observed against local data yet."],
            "updated_at": utc_now(),
        })
        self._write_markdown("OBJECTIVE.md", self._objective_markdown(objective))
        self._write_markdown("STATE.md", self._state_markdown(self.state()))
        for name, content in {
            "KNOWLEDGE.md": "# KNOWLEDGE\n\nNo measured findings yet.\n",
            "EXPERIMENT_LOG.md": "# EXPERIMENT LOG\n\nNo experiments recorded yet.\n",
            "FAILURE_MEMORY.md": "# FAILURE MEMORY\n\nNo failures recorded yet.\n",
            "RESOURCE_MAP.md": "# RESOURCE MAP\n\nResource discovery has not run yet.\n",
        }.items():
            if not (self.root / name).exists() or replace:
                self._write_markdown(name, content)

    def objective(self) -> Objective:
        payload = self._read_json(self.objective_path)
        return Objective(
            goal=str(payload["goal"]),
            universe=str(payload.get("universe", "unspecified")),
            horizon=str(payload.get("horizon", "unspecified")),
            constraints=tuple(payload.get("constraints", ())),
            created_at=str(payload.get("created_at") or utc_now()),
            version=str(payload.get("version") or "autonomous-quant-research.v1"),
        )

    def state(self) -> dict[str, Any]:
        return self._read_json(self.state_path) if self.state_path.exists() else {}

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchLabError(f"invalid event at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ResearchLabError(f"event at line {line_number} is not an object")
            result.append(value)
        return result

    def plans(self) -> list[dict[str, Any]]:
        return self._read_objects(self.plans_dir)

    def results(self) -> list[ExperimentResult]:
        result: list[ExperimentResult] = []
        for payload in self._read_objects(self.results_dir):
            result.append(from_result_dict(payload))
        return result

    def record_hypothesis(self, hypothesis: Hypothesis) -> None:
        self._ensure_initialized()
        self._write_json(self.hypotheses_dir / f"{hypothesis.hypothesis_id}.json", to_dict(hypothesis))
        self.append_event("HYPOTHESIS_CREATED", to_dict(hypothesis))

    def record_plan(self, plan: ExperimentPlan) -> None:
        self._ensure_initialized()
        self._write_json(self.plans_dir / f"{plan.plan_id}.json", to_dict(plan))
        self.append_event("PLAN_CREATED", to_dict(plan))
        self.update_state(active_plan_id=plan.plan_id, last_action="PLAN_CREATED")

    def record_agent_run(self, payload: Mapping[str, Any]) -> None:
        self._ensure_initialized()
        run_id = str(payload.get("run_id") or stable_id("agent", payload.get("plan_id"), utc_now()))
        self._write_json(self.agent_runs_dir / f"{run_id}.json", dict(payload))
        self.append_event("AGENT_RUN", {**dict(payload), "run_id": run_id})

    def record_result(self, result: ExperimentResult) -> None:
        self._ensure_initialized()
        result.validate()
        self._write_json(self.results_dir / f"{result.plan_id}.json", to_dict(result))
        self.append_event("EXPERIMENT_RESULT", to_dict(result))
        action = "RESULT_RECORDED"
        if result.leakage_detected:
            action = "REJECTED_LEAKAGE"
        elif result.status != "COMPLETED":
            action = "RESULT_BLOCKED"
        self.update_state(active_plan_id=None, last_action=action)
        self._refresh_summaries()

    def append_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self._ensure_initialized()
        event = ResearchEvent(event_type=event_type, payload=dict(payload))
        line = canonical_json(asdict(event)) + "\n"
        with self._locked():
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        self._refresh_summaries()

    def update_state(self, **updates: Any) -> dict[str, Any]:
        current = self.state()
        current.update(updates)
        current["updated_at"] = utc_now()
        self._write_json(self.state_path, current)
        self._write_markdown("STATE.md", self._state_markdown(current))
        return current

    def write_resource_map(self, resources: list[Mapping[str, Any]], *, repo_root: Path) -> None:
        payload = {
            "generated_at": utc_now(),
            "repo_root": str(repo_root),
            "resources": [dict(item) for item in resources],
        }
        self._write_json(self.root / "resource-map.json", payload)
        lines = ["# RESOURCE MAP", "", f"Generated: `{payload['generated_at']}`", f"Repository: `{repo_root}`", ""]
        for resource in resources:
            lines.append(f"- **{resource.get('kind', 'unknown')}** `{resource.get('path', '')}` — {resource.get('detail', '')}")
        self._write_markdown("RESOURCE_MAP.md", "\n".join(lines) + "\n")

    def _refresh_summaries(self) -> None:
        if not self.objective_path.exists():
            return
        events = self.events()
        self._write_markdown("EXPERIMENT_LOG.md", self._experiment_log_markdown(events))
        self._write_markdown("KNOWLEDGE.md", self._knowledge_markdown(events))
        self._write_markdown("FAILURE_MEMORY.md", self._failure_markdown(events))
        self._write_markdown("STATE.md", self._state_markdown(self.state()))

    def _ensure_initialized(self) -> None:
        if not self.objective_path.exists():
            raise ResearchLabError(f"lab is not initialized: {self.root}")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchLabError(f"missing lab file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ResearchLabError(f"invalid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise ResearchLabError(f"expected JSON object: {path}")
        return payload

    @classmethod
    def _read_objects(cls, directory: Path) -> list[dict[str, Any]]:
        if not directory.exists():
            return []
        result = []
        for path in sorted(directory.glob("*.json")):
            result.append(cls._read_json(path))
        return result

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ResearchLab._atomic_write(path, content)

    def _write_markdown(self, name: str, content: str) -> None:
        self._atomic_write(self.root / name, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _objective_markdown(objective: Objective) -> str:
        constraints = "\n".join(f"- {item}" for item in objective.constraints) or "- None recorded"
        return (
            "# OBJECTIVE\n\n"
            f"## Goal\n{objective.goal}\n\n"
            f"- Universe: `{objective.universe}`\n"
            f"- Horizon: `{objective.horizon}`\n"
            f"- Created: `{objective.created_at}`\n\n"
            "## Constraints\n" + constraints + "\n"
        )

    @staticmethod
    def _state_markdown(state: Mapping[str, Any]) -> str:
        uncertainties = state.get("uncertainties") or ["No uncertainty has been recorded."]
        lines = ["# STATE", "", f"- Cycle: `{state.get('cycle', 0)}`", f"- Active plan: `{state.get('active_plan_id') or 'none'}`", f"- Best plan: `{state.get('best_plan_id') or 'none'}`", f"- Last action: `{state.get('last_action', 'unknown')}`", "", "## Uncertainties", ""]
        lines.extend(f"- {item}" for item in uncertainties)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _experiment_log_markdown(events: list[Mapping[str, Any]]) -> str:
        lines = ["# EXPERIMENT LOG", ""]
        for event in events:
            if event.get("event_type") not in {"PLAN_CREATED", "EXPERIMENT_RESULT", "DECISION"}:
                continue
            payload = event.get("payload") or {}
            lines.append(f"## `{event.get('created_at', 'unknown')}` {event.get('event_type')}")
            for key in ("plan_id", "hypothesis_id", "status", "decision", "rationale", "failure_reason"):
                if payload.get(key) not in (None, "", []):
                    lines.append(f"- {key}: {payload[key]}")
            lines.append("")
        return "\n".join(lines) + ("\n" if lines[-1] != "" else "")

    @staticmethod
    def _knowledge_markdown(events: list[Mapping[str, Any]]) -> str:
        lines = ["# KNOWLEDGE", "", "Measured findings and decisions only. Unmeasured beliefs stay in hypotheses.", ""]
        for event in events:
            if event.get("event_type") != "KNOWLEDGE":
                continue
            payload = event.get("payload") or {}
            lines.append(f"- {payload.get('finding', 'unlabelled finding')} (evidence: `{payload.get('evidence_id', 'unknown')}`)")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _failure_markdown(events: list[Mapping[str, Any]]) -> str:
        lines = ["# FAILURE MEMORY", "", "Failures are retained to prevent repeating the same research.", ""]
        for event in events:
            if event.get("event_type") not in {"EXPERIMENT_RESULT", "FAILURE"}:
                continue
            payload = event.get("payload") or {}
            if event.get("event_type") == "EXPERIMENT_RESULT" and payload.get("status") == "COMPLETED" and not payload.get("failure_modes"):
                continue
            reason = payload.get("failure_reason") or ", ".join(payload.get("failure_modes") or ()) or "unspecified"
            lines.append(f"- plan `{payload.get('plan_id', 'unknown')}`: {reason}")
        return "\n".join(lines) + "\n"
