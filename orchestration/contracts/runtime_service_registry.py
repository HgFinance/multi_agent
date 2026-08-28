"""Loader for deterministic runtime services outside the LLM Worker Registry.

The Workforce LLM registry intentionally excludes Trading's request-scoped
strategy workers and its always-on deterministic desk runner.  This separate
contract prevents a real service from appearing as ``NO_WORKERS_REGISTERED``
without pretending that it has token usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = "hgfinance.runtime-service-registry.v1"
MANIFEST_RELATIVE_PATH = Path("orchestration/contracts/runtime_service_registry.v1.json")
_TOP_LEVEL_KEYS = frozenset({"schema_version", "services"})
_SERVICE_KEYS = frozenset({"department", "service_id", "worker_id", "kind", "trigger"})
_SUPPORTED_KINDS = frozenset({"deterministic"})


class RuntimeServiceRegistryError(RuntimeError):
    """The deterministic runtime service manifest is invalid or unavailable."""


@dataclass(frozen=True)
class RuntimeServiceMetadata:
    department: str
    service_id: str
    worker_id: str
    kind: str
    trigger: str


def _invalid(detail: str) -> RuntimeServiceRegistryError:
    return RuntimeServiceRegistryError(f"runtime_service_registry_invalid:{detail}")


def load_runtime_service_registry(repo_root: Path) -> tuple[RuntimeServiceMetadata, ...]:
    path = repo_root / MANIFEST_RELATIVE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeServiceRegistryError(f"runtime_service_registry_file_missing:{path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeServiceRegistryError(
            f"runtime_service_registry_file_unreadable:{path}:{type(exc).__name__}"
        ) from exc

    if not isinstance(raw, dict) or set(raw) != _TOP_LEVEL_KEYS:
        raise _invalid("top_level_keys")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise _invalid(f"schema_version:{raw.get('schema_version')!r}")
    services = raw.get("services")
    if not isinstance(services, list):
        raise _invalid("services_not_array")

    result: list[RuntimeServiceMetadata] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(services):
        if not isinstance(item, dict) or set(item) != _SERVICE_KEYS:
            raise _invalid(f"service_keys:{index}")
        values = {key: item.get(key) for key in _SERVICE_KEYS}
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise _invalid(f"service_fields:{index}")
        metadata = RuntimeServiceMetadata(
            department=values["department"].strip(),
            service_id=values["service_id"].strip(),
            worker_id=values["worker_id"].strip(),
            kind=values["kind"].strip(),
            trigger=values["trigger"].strip(),
        )
        if metadata.kind not in _SUPPORTED_KINDS:
            raise _invalid(f"service_kind:{metadata.kind}")
        identity = (metadata.department, metadata.worker_id)
        if identity in seen:
            raise _invalid(f"duplicate_service:{metadata.department}:{metadata.worker_id}")
        seen.add(identity)
        result.append(metadata)
    return tuple(result)


def services_for_department(
    registry: tuple[RuntimeServiceMetadata, ...], department: str
) -> tuple[RuntimeServiceMetadata, ...]:
    return tuple(item for item in registry if item.department == department)


__all__ = [
    "MANIFEST_RELATIVE_PATH",
    "RuntimeServiceMetadata",
    "RuntimeServiceRegistryError",
    "SCHEMA_VERSION",
    "load_runtime_service_registry",
    "services_for_department",
]
