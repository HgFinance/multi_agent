"""Read-only loader for the versioned Worker Registry metadata contract.

This module deliberately does not import any department runtime module.  The
manifest is the boundary consumed by HR/observability and contains only the
metadata needed to identify a Worker and its trigger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = "hgfinance.worker-registry.v1"
MANIFEST_RELATIVE_PATH = Path("orchestration/contracts/worker_registry.v1.json")
SUPPORTED_DEPARTMENTS = (
    "research",
    "trading",
    "risk",
    "quant-backtest",
    "accounting-portfolio",
    "qa",
)
_SUPPORTED_DEPARTMENT_SET = frozenset(SUPPORTED_DEPARTMENTS)
_TOP_LEVEL_KEYS = frozenset({"schema_version", "workers"})
_WORKER_KEYS = frozenset({"department", "worker_id", "trigger"})


class WorkerRegistryError(RuntimeError):
    """The Worker Registry manifest is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class WorkerMetadata:
    """The metadata-only projection exposed to non-runtime consumers."""

    department: str
    worker_id: str
    trigger: str


def _invalid(detail: str) -> WorkerRegistryError:
    return WorkerRegistryError(f"worker_registry_invalid:{detail}")


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / MANIFEST_RELATIVE_PATH


def load_worker_registry(repo_root: Path) -> tuple[WorkerMetadata, ...]:
    """Load and validate the complete metadata manifest.

    Validation is intentionally strict.  A malformed or newer/unknown schema
    must stop the caller from presenting an incomplete Worker list as healthy.
    """

    path = _manifest_path(repo_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkerRegistryError(f"worker_registry_file_missing:{path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerRegistryError(
            f"worker_registry_file_unreadable:{path}:{type(exc).__name__}"
        ) from exc

    if not isinstance(raw, dict):
        raise _invalid("document_not_object")
    if set(raw) != _TOP_LEVEL_KEYS:
        raise _invalid("top_level_keys")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise _invalid(f"schema_version:{raw.get('schema_version')!r}")

    workers = raw.get("workers")
    if not isinstance(workers, list):
        raise _invalid("workers_not_array")

    result: list[WorkerMetadata] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(workers):
        if not isinstance(item, dict) or set(item) != _WORKER_KEYS:
            raise _invalid(f"worker_keys:{index}")
        department = item.get("department")
        worker_id = item.get("worker_id")
        trigger = item.get("trigger")
        if not all(isinstance(value, str) and value.strip() for value in (department, worker_id, trigger)):
            raise _invalid(f"worker_fields:{index}")
        department = department.strip()
        worker_id = worker_id.strip()
        trigger = trigger.strip()
        if department not in _SUPPORTED_DEPARTMENT_SET:
            raise _invalid(f"unknown_department:{department}")
        identity = (department, worker_id)
        if identity in seen:
            raise _invalid(f"duplicate_worker:{department}:{worker_id}")
        seen.add(identity)
        result.append(WorkerMetadata(department, worker_id, trigger))
    return tuple(result)


def workers_for_department(
    registry: tuple[WorkerMetadata, ...], department: str
) -> tuple[WorkerMetadata, ...]:
    """Return the already-validated metadata for one known department."""

    if department not in _SUPPORTED_DEPARTMENT_SET:
        raise ValueError(f"unknown_investment_department:{department}")
    return tuple(item for item in registry if item.department == department)


__all__ = [
    "MANIFEST_RELATIVE_PATH",
    "SCHEMA_VERSION",
    "SUPPORTED_DEPARTMENTS",
    "WorkerMetadata",
    "WorkerRegistryError",
    "load_worker_registry",
    "workers_for_department",
]
