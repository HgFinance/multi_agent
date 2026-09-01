"""Worker Registry manifest contract tests.

The production observer must not import department runtime modules.  This test
reads the runtime declarations statically to compare only their public
metadata (worker_id and trigger) with the versioned manifest.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from orchestration.contracts.worker_registry import (
    MANIFEST_RELATIVE_PATH,
    SCHEMA_VERSION,
    SUPPORTED_DEPARTMENTS,
    WorkerRegistryError,
    load_worker_registry,
)

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = {
    "research": ROOT / "departments/01-research/employee_workers.py",
    "trading": ROOT / "departments/02-trading/employee_workers.py",
    "risk": ROOT / "departments/03-risk/risk_employee_workers.py",
    "quant-backtest": ROOT / "departments/04-quant-backtest/employee_workers.py",
    "accounting-portfolio": ROOT / "departments/05-accounting-portfolio/employee_workers.py",
    "qa": ROOT / "departments/06-ai-qa-audit/qa_employee_workers.py",
}


def _literal(node: ast.AST, constants: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return ast.literal_eval(node)


def _declared_worker_metadata(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, Any] = {}
    worker_value: ast.AST | None = None
    for node in tree.body:
        assignments: list[tuple[str, ast.AST]] = []
        if isinstance(node, ast.Assign):
            assignments = [
                (target.id, node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments = [(node.target.id, node.value)] if node.value is not None else []
        for name, value in assignments:
            try:
                constants[name] = ast.literal_eval(value)
            except (ValueError, TypeError):
                pass
            if name == "WORKER_SPECS":
                worker_value = value

    if worker_value is None:
        raise AssertionError(f"WORKER_SPECS missing from {path}")
    if not isinstance(worker_value, (ast.Tuple, ast.List)):
        raise AssertionError(f"WORKER_SPECS must be a static sequence in {path}")

    result: list[tuple[str, str]] = []
    for item in worker_value.elts:
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Name):
            raise AssertionError(f"unsupported WorkerSpec declaration in {path}")
        if item.func.id != "WorkerSpec" or len(item.args) < 4:
            raise AssertionError(f"invalid WorkerSpec declaration in {path}")
        result.append(
            (
                str(_literal(item.args[0], constants)),
                str(_literal(item.args[3], constants)),
            )
        )
    return result


def test_manifest_matches_each_department_worker_specs() -> None:
    registry = load_worker_registry(ROOT)
    actual = {
        department: [(item.worker_id, item.trigger) for item in registry if item.department == department]
        for department in SUPPORTED_DEPARTMENTS
    }
    expected = {
        department: _declared_worker_metadata(path)
        for department, path in RUNTIME_MODULES.items()
    }
    assert actual == expected


def test_manifest_has_the_six_known_departments_even_when_trading_has_no_specs() -> None:
    registry = load_worker_registry(ROOT)
    assert set(SUPPORTED_DEPARTMENTS) == set(RUNTIME_MODULES)
    assert all(item.department in SUPPORTED_DEPARTMENTS for item in registry)
    assert not any(item.department == "trading" for item in registry)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "hgfinance.worker-registry.v2", "workers": []},
        {"schema_version": SCHEMA_VERSION, "workers": [{"department": "research"}]},
    ],
)
def test_invalid_manifest_schema_fails_closed(tmp_path: Path, payload: dict[str, Any]) -> None:
    manifest = tmp_path / MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkerRegistryError):
        load_worker_registry(tmp_path)



def test_observer_and_image_do_not_couple_to_department_runtime_modules() -> None:
    observer_source = (
        ROOT / "departments/07-agent-workforce/scorecard/observability.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(observer_source)
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "orchestration.employee_dispatch"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_worker_specs"
        for node in ast.walk(tree)
    )

    dockerfile = (
        ROOT / "departments/07-agent-workforce/Dockerfile"
    ).read_text(encoding="utf-8")
    assert "COPY orchestration/contracts/worker_registry.py" in dockerfile
    assert "COPY orchestration/contracts/worker_registry.v1.json" in dockerfile
    for department_path in (
        "departments/01-research",
        "departments/02-trading",
        "departments/03-risk",
        "departments/04-quant-backtest",
        "departments/05-accounting-portfolio",
        "departments/06-ai-qa-audit",
    ):
        assert f"COPY {department_path}" not in dockerfile


def test_workforce_image_packages_api_import_roots_and_worker_loader() -> None:
    dockerfile = (
        ROOT / "departments/07-agent-workforce/Dockerfile"
    ).read_text(encoding="utf-8")
    for runtime_path in (
        "api",
        "lifecycle",
        "improvements",
        "scorecard",
        "roster",
        "workforce_events",
        "planning",
        "hiring",
        "performance",
    ):
        assert (
            f"COPY departments/07-agent-workforce/{runtime_path} ./{runtime_path}"
            in dockerfile
        )
    assert (
        "COPY departments/07-agent-workforce/workforce_api_loader.py "
        "./workforce_api_loader.py"
        in dockerfile
    )


def test_workforce_image_packages_hr_review_bridge_dependencies() -> None:
    dockerfile = (
        ROOT / "departments/07-agent-workforce/Dockerfile"
    ).read_text(encoding="utf-8")
    for module in (
        "langsmith_egress.py",
        "langsmith_queries.py",
        "langsmith_feedback.py",
        "hr_langfuse_feedback.py",
    ):
        assert f"COPY orchestration/{module} /app/orchestration/{module}" in dockerfile
