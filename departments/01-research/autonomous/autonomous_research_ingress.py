"""Durable ingress for user-owned autonomous research labs.

The web API writes only a small request manifest.  The autonomous worker
materializes the manifest into an isolated lab and owns every research write
after that point.  This boundary deliberately has no Kanban, database, order,
or legacy factory dependency.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, Mapping

from lab import ResearchLab
from models import Objective, to_dict, utc_now


REQUEST_SCHEMA = "autonomous-research-request.v1"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class ResearchRequestConflict(ValueError):
    """The idempotency key is already bound to another research request."""


def _safe_request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must be 8-128 safe identifier characters")
    return request_id


def _text(value: object, name: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    if len(result) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return result


def _constraints(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("constraints must be a list of strings")
    if len(value) > 20:
        raise ValueError("constraints may contain at most 20 items")
    result: list[str] = []
    for item in value:
        text = _text(item, "constraint", maximum=500)
        if text not in result:
            result.append(text)
    return result


def normalize_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    request_id = _safe_request_id(payload.get("request_id"))
    return {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "goal": _text(payload.get("goal"), "goal", maximum=4000),
        "universe": _text(payload.get("universe") or "unspecified", "universe", maximum=500),
        "horizon": _text(payload.get("horizon") or "unspecified", "horizon", maximum=500),
        "constraints": _constraints(payload.get("constraints")),
        "actor_id": _text(payload.get("actor_id") or "anonymous", "actor_id", maximum=256),
        "source": _text(payload.get("source") or "web", "source", maximum=64),
        "created_at": _text(payload.get("created_at") or utc_now(), "created_at", maximum=64),
    }


def request_fingerprint(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        payload.get(key)
        if key != "constraints"
        else tuple(payload.get(key) or ())
        for key in ("request_id", "goal", "universe", "horizon", "constraints", "actor_id", "source")
    )


class ResearchIntake:
    """Atomic request manifest store shared by BFF and the autonomous worker."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.intake_dir = self.root / "intake"
        self.labs_dir = self.root / "labs"
        self.errors_dir = self.root / "errors"
        self.lock_path = self.root / ".intake.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected JSON object: {path}")
        return payload

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def lab_path(self, request_id: str) -> Path:
        return self.labs_dir / _safe_request_id(request_id)

    def submit(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = normalize_request(payload)
        request_id = normalized["request_id"]
        intake_path = self.intake_dir / f"{request_id}.json"
        lab_path = self.lab_path(request_id)
        with self._locked():
            existing_path = (
                lab_path / "request.json"
                if (lab_path / "request.json").exists()
                else intake_path
                if intake_path.exists()
                else None
            )
            if existing_path is not None:
                existing = normalize_request(self._read_json(existing_path))
                if request_fingerprint(existing) != request_fingerprint(normalized):
                    raise ResearchRequestConflict(
                        "request_id is already bound to a different research request"
                    )
                return existing, False
            self._write_json(intake_path, normalized)
        return normalized, True

    def pending_ids(self) -> tuple[str, ...]:
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        return tuple(path.stem for path in sorted(self.intake_dir.glob("*.json")))

    def materialize(self, request_id: str, *, repo_root: Path) -> Path:
        request_id = _safe_request_id(request_id)
        intake_path = self.intake_dir / f"{request_id}.json"
        lab_path = self.lab_path(request_id)
        with self._locked():
            if not intake_path.exists():
                return lab_path
            payload = normalize_request(self._read_json(intake_path))
            lab = ResearchLab(lab_path)
            lab.initialize(
                Objective(
                    goal=payload["goal"],
                    universe=payload["universe"],
                    horizon=payload["horizon"],
                    constraints=tuple(payload["constraints"]),
                )
            )
            self._write_json(lab_path / "request.json", payload)
            lab.write_resource_map([], repo_root=repo_root)
            # The lab is initialized before the marker is removed.  A worker crash
            # in between is therefore safe: the next worker sees the marker and
            # resumes the idempotent initialization.
            if intake_path.exists():
                intake_path.unlink()
            self.clear_error(request_id)
        return lab_path

    def record_error(self, request_id: str, *, phase: str, error: str) -> None:
        """Persist the current worker failure without replacing the lab history."""

        request_id = _safe_request_id(request_id)
        self._write_json(
            self.errors_dir / f"{request_id}.json",
            {"request_id": request_id, "phase": phase, "error": str(error), "updated_at": utc_now()},
        )

    def clear_error(self, request_id: str) -> None:
        error_path = self.errors_dir / f"{_safe_request_id(request_id)}.json"
        try:
            error_path.unlink()
        except FileNotFoundError:
            pass

    def status(self, request_id: str) -> dict[str, Any] | None:
        request_id = _safe_request_id(request_id)
        lab_path = self.lab_path(request_id)
        request_path = lab_path / "request.json"
        intake_path = self.intake_dir / f"{request_id}.json"
        if request_path.exists():
            request = normalize_request(self._read_json(request_path))
        elif intake_path.exists():
            request = normalize_request(self._read_json(intake_path))
        else:
            return None

        state_path = lab_path / ".state.json"
        state = self._read_json(state_path) if state_path.exists() else {}
        candidate = lab_path / "candidate.json"
        error_path = self.errors_dir / f"{request_id}.json"
        error = self._read_json(error_path) if error_path.exists() else None
        plan_count = len(tuple((lab_path / "plans").glob("*.json"))) if lab_path.exists() else 0
        result_count = len(tuple((lab_path / "results").glob("*.json"))) if lab_path.exists() else 0
        if candidate.exists():
            status = "CANDIDATE"
        elif error is not None:
            status = "BLOCKED"
        elif not (lab_path / "objective.json").exists():
            status = "QUEUED"
        else:
            status = "RESEARCHING"
        return {
            "schema": "autonomous-research-status.v1",
            "request_id": request_id,
            "lab_id": request_id,
            "goal": request["goal"],
            "universe": request["universe"],
            "horizon": request["horizon"],
            "status": status,
            "cycle": int(state.get("cycle", 0) or 0),
            "last_action": state.get("last_action"),
            "active_plan_id": state.get("active_plan_id"),
            "plan_count": plan_count,
            "result_count": result_count,
            "candidate_available": candidate.exists(),
            "updated_at": state.get("updated_at") or request["created_at"],
            "actor_id": request["actor_id"],
            "error": error.get("error") if error else None,
        }


def looks_like_strategy_research(text: str) -> bool:
    """Conservative UI classifier for routing strategy-research chat turns."""

    value = str(text or "").casefold()
    nouns = r"(?:전략|알파|시그널|백테스트|트레이딩\s*전략|quant|backtest)"
    verbs = r"(?:생성|만들|개발|연구|검증|발굴|찾아|설계|generate|create|build|develop|research|validate|discover|find|design)"
    return bool(re.search(rf"{nouns}.*{verbs}|{verbs}.*{nouns}", value, re.IGNORECASE))


__all__ = [
    "REQUEST_SCHEMA",
    "ResearchIntake",
    "ResearchRequestConflict",
    "looks_like_strategy_research",
    "normalize_request",
]
