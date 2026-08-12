"""Load and validate the versioned workflow manifests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .contracts import StepSpec, WorkflowContractError, WorkflowSpec

WORKFLOW_DIR = Path(__file__).resolve().parent
INDEX_PATH = WORKFLOW_DIR / "index.yaml"


class WorkflowManifestError(WorkflowContractError):
    """Raised when a manifest is missing or has the wrong shape."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowManifestError(f"{label}는 mapping이어야 합니다")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowManifestError(f"workflow manifest를 찾을 수 없습니다: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WorkflowManifestError(f"YAML parse 실패: {path}: {exc}") from exc
    return _mapping(value, str(path))


def _step_from_mapping(raw: Any, workflow_name: str) -> StepSpec:
    item = _mapping(raw, f"{workflow_name}.steps item")
    retry = _mapping(item.get("retry", {}), f"{workflow_name}/{item.get('id')}.retry")
    forbidden = item.get("forbidden_actions", ())
    if isinstance(forbidden, str) or not isinstance(forbidden, (list, tuple)):
        raise WorkflowManifestError(
            f"{workflow_name}/{item.get('id')}: forbidden_actions는 배열이어야 합니다"
        )
    return StepSpec(
        id=str(item.get("id", "")),
        sequence=int(item.get("sequence", 0)),
        department=str(item.get("department", "")),
        task=str(item.get("task", "")),
        input_contract=str(item.get("input_contract", "")),
        output_contract=str(item.get("output_contract", "")),
        timeout_seconds=int(item.get("timeout_seconds", 0)),
        max_attempts=int(retry.get("max_attempts", 0)),
        failure_action=str(item.get("failure_action", "")),
        owner=str(item.get("owner", "")),
        forbidden_actions=tuple(str(value) for value in forbidden),
    )


def _spec_from_mapping(raw: dict[str, Any], source: Path) -> WorkflowSpec:
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise WorkflowManifestError(f"{source}: steps는 배열이어야 합니다")
    spec = WorkflowSpec(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        kind=str(raw.get("kind", "")),
        description=str(raw.get("description", "")),
        steps=tuple(_step_from_mapping(item, str(raw.get("name", source.name))) for item in steps),
        boundary_rules=tuple(str(rule) for rule in raw.get("boundary_rules", ())),
        metadata={
            "source": str(source),
            "rules": raw.get("rules", {}),
            **_mapping(raw.get("metadata", {}), "metadata"),
        },
    )
    spec.validate()
    return spec


def load_workflows() -> dict[str, WorkflowSpec]:
    """Load every workflow named by ``index.yaml`` in deterministic order."""

    index = _load_yaml(INDEX_PATH)
    entries = index.get("workflows")
    if not isinstance(entries, dict) or not entries:
        raise WorkflowManifestError("index.yaml: workflows가 비어 있거나 mapping이 아닙니다")

    specs: dict[str, WorkflowSpec] = {}
    for name, relative_path in entries.items():
        if not isinstance(relative_path, str):
            raise WorkflowManifestError(f"{name}: manifest 경로는 문자열이어야 합니다")
        path = (WORKFLOW_DIR / relative_path).resolve()
        if WORKFLOW_DIR not in path.parents:
            raise WorkflowManifestError(f"{name}: workflow 디렉터리 밖의 경로입니다")
        spec = _spec_from_mapping(_load_yaml(path), path)
        if spec.name != name:
            raise WorkflowManifestError(
                f"index.yaml의 이름({name})과 manifest 이름({spec.name})이 다릅니다"
            )
        specs[name] = spec
    return specs


def load_workflow(name: str) -> WorkflowSpec:
    """Load one named workflow from the canonical workflow index."""

    try:
        return load_workflows()[name]
    except KeyError as exc:
        raise WorkflowManifestError(f"등록되지 않은 workflow: {name}") from exc
