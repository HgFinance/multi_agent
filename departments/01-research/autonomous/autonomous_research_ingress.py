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

from lab import ResearchLab, ResearchLabError
from models import Objective, to_dict, utc_now


REQUEST_SCHEMA = "autonomous-research-request.v1"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
# Intake and request manifests are the small, non-secret IPC surface shared by
# the BFF and Strategy Hermes. The lab's experiment/state artifacts remain
# private to the research worker; these manifests must be readable across the
# BFF(root) -> Hermes(uid 1000) boundary.
SHARED_MANIFEST_MODE = 0o644
SHARED_ERROR_MODE = 0o644


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


def _optional_text(value: object, name: str, *, maximum: int) -> str | None:
    """Validate optional correlation metadata without inventing a value."""

    if value is None or not str(value).strip():
        return None
    return _text(value, name, maximum=maximum)


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
        # Discord delivery coordinates are carried into the lab as immutable
        # ingress metadata. They are not research inputs and are never exposed
        # to Hermes as an authority surface.
        "source_message_id": _optional_text(
            payload.get("source_message_id"), "source_message_id", maximum=512
        ),
        "discord_channel_id": _optional_text(
            payload.get("discord_channel_id"), "discord_channel_id", maximum=128
        ),
        "discord_message_id": _optional_text(
            payload.get("discord_message_id"), "discord_message_id", maximum=128
        ),
        "discord_guild_id": _optional_text(
            payload.get("discord_guild_id"), "discord_guild_id", maximum=128
        ),
        "discord_thread_id": _optional_text(
            payload.get("discord_thread_id"), "discord_thread_id", maximum=128
        ),
        # Control-plane correlation only. This points to a blocked,
        # tracking-only Kanban root; it is never a Hermes execution parent.
        "kanban_root_task_id": _optional_text(
            payload.get("kanban_root_task_id"), "kanban_root_task_id", maximum=128
        ),
    }


def request_fingerprint(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        payload.get(key)
        if key != "constraints"
        else tuple(payload.get(key) or ())
        for key in (
            "request_id", "goal", "universe", "horizon", "constraints", "actor_id", "source",
            "source_message_id", "discord_channel_id", "discord_message_id",
            "discord_guild_id", "discord_thread_id",
        )
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
    def _write_json(
        path: Path,
        payload: Mapping[str, Any],
        *,
        mode: int | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            # Set the final contract before the file becomes visible through
            # os.replace(). Applying chmod after rename leaves a small race in
            # which Hermes can observe the mkstemp 0600 mode while polling.
            if mode is not None:
                os.fchmod(fd, mode)
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
            # BFF and Strategy Hermes intentionally run as different users
            # while sharing this small file-backed IPC directory. Make the
            # directory consumable by either side and manifests readable by
            # the Hermes uid; the lab's private state remains mode 0600.
            self.intake_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.intake_dir.chmod(0o777)
            except OSError:
                pass
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
            self._write_json(intake_path, normalized, mode=SHARED_MANIFEST_MODE)
        return normalized, True

    def pending_ids(self) -> tuple[str, ...]:
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        return tuple(path.stem for path in sorted(self.intake_dir.glob("*.json")))

    def bind_kanban_root(self, request_id: str, task_id: str) -> dict[str, Any]:
        """Persist a tracking root without changing the request identity."""

        request_id = _safe_request_id(request_id)
        task_id = _text(task_id, "kanban_root_task_id", maximum=128)
        with self._locked():
            path = self.lab_path(request_id) / "request.json"
            if not path.exists():
                path = self.intake_dir / f"{request_id}.json"
            if not path.exists():
                raise FileNotFoundError(request_id)
            payload = self._read_json(path)
            existing = str(payload.get("kanban_root_task_id") or "").strip()
            if existing and existing != task_id:
                raise ResearchRequestConflict(
                    "request_id is already bound to a different kanban_root_task_id"
                )
            payload["kanban_root_task_id"] = task_id
            # Tracking-root updates also replace the file atomically. Keep
            # the shared manifest readable after that replacement.
            self._write_json(path, payload, mode=SHARED_MANIFEST_MODE)
            return normalize_request(payload)

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
            self._write_json(
                lab_path / "request.json",
                payload,
                mode=SHARED_MANIFEST_MODE,
            )
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
            mode=SHARED_ERROR_MODE,
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
        plan_paths = tuple((lab_path / "plans").glob("*.json")) if lab_path.exists() else ()
        plan_count = len(plan_paths)
        if lab_path.exists():
            try:
                events = ResearchLab(lab_path).events()
            except (ResearchLabError, OSError, ValueError):
                events = []
            registered_plan_ids = {
                str((event.get("payload") or {}).get("plan_id") or "")
                for event in events
                if event.get("event_type") == "PLAN_CREATED"
                and str((event.get("payload") or {}).get("plan_id") or "").strip()
            }
            if registered_plan_ids:
                plan_count = len(registered_plan_ids)
        result_count = len(tuple((lab_path / "results").glob("*.json"))) if lab_path.exists() else 0
        latest_result: dict[str, Any] | None = None
        result_paths = sorted((lab_path / "results").glob("*.json")) if lab_path.exists() else []
        if result_paths:
            try:
                latest_result = self._read_json(result_paths[-1])
            except (OSError, json.JSONDecodeError, ValueError):
                latest_result = None
        if candidate.exists():
            status = "CANDIDATE"
        elif error is not None:
            status = "BLOCKED"
        elif str((latest_result or {}).get("status") or "").upper() in {"BLOCKED", "FAILED"}:
            status = "BLOCKED"
        elif str((latest_result or {}).get("status") or "").upper() == "COMPLETED":
            status = "COMPLETED"
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
            # Control-plane correlation only.  The tracking root is not the
            # research execution parent; Strategy Hermes remains the sole
            # owner of the research cycle.
            "kanban_root_task_id": request.get("kanban_root_task_id"),
            "candidate_available": candidate.exists(),
            "updated_at": state.get("updated_at") or request["created_at"],
            "actor_id": request["actor_id"],
            "error": (error.get("error") if error else None)
            or ((latest_result or {}).get("failure_reason") if latest_result else None),
        }


def looks_like_strategy_research(text: str) -> bool:
    """Conservative UI classifier for routing strategy-research chat turns."""

    value = str(text or "").casefold()
    # ``백테스트해줘`` is itself a research action in Korean. The old
    # classifier treated 백테스트 as a noun and required a second verb such
    # as 생성/연구, so an explicit "전략 ... 백테스트" could fall through to
    # the CEO/order lane. Keep the boundary conservative: a strategy/alpha/
    # signal noun must still be paired with a research action.
    nouns = r"(?:전략|알파|시그널|트레이딩\s*전략|strategy|alpha|signal|quant)"
    actions = r"(?:생성|만들|개발|연구|검증|발굴|찾아|설계|백테스트|테스트|시뮬레이션|평가|generate|create|build|develop|research|validate|discover|find|design|backtest)"
    return bool(re.search(rf"{nouns}.*{actions}|{actions}.*{nouns}", value, re.IGNORECASE))


__all__ = [
    "REQUEST_SCHEMA",
    "ResearchIntake",
    "ResearchRequestConflict",
    "looks_like_strategy_research",
    "normalize_request",
]
