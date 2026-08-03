"""Dispatch department employee Workers without crossing authority boundaries.

Each department head remains a Hermes profile.  This module loads the
department's employee Worker registry and returns non-binding context for the
head.  It never turns Worker output into an order, Risk/QA verdict, ledger
posting, permission grant, or production approval.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from departments.employee_worker_runtime import WorkerLLM


EMPLOYEE_MODULE_BY_DEPARTMENT = {
    "ceo": "departments/00-ceo-office/employee_workers.py",
    "hr": "departments/07-agent-workforce/employee_workers.py",
    "research": "departments/01-research/employee_workers.py",
    "trading": "departments/02-trading/employee_workers.py",
    "risk": "departments/03-risk/risk_employee_workers.py",
    "quant-backtest": "departments/04-quant-backtest/employee_workers.py",
    "accounting-portfolio": "departments/05-accounting-portfolio/employee_workers.py",
    "qa": "departments/06-ai-qa-audit/qa_employee_workers.py",
}


class EmployeeDispatchError(RuntimeError):
    """Raised when a department Worker registry cannot be loaded."""


def run_department_workers(
    repo_root: Path,
    department: str,
    payload: Mapping[str, Any],
    *,
    llm: WorkerLLM | None = None,
) -> dict[str, Any]:
    """Run the registered Workers for one department.

    The returned object is deliberately marked non-binding.  Registry errors
    are represented as ``DEGRADED`` so a Worker failure is observable without
    silently becoming an approval.
    """

    relative_path = EMPLOYEE_MODULE_BY_DEPARTMENT.get(department)
    if relative_path is None:
        raise EmployeeDispatchError(f"department_worker_registry_missing:{department}")

    module = _load_module(repo_root, department, relative_path)
    runner = getattr(module, "run_employee_workers", None)
    if not callable(runner):
        raise EmployeeDispatchError(f"employee_runner_missing:{department}")

    try:
        result = runner(payload, llm=llm)
    except TypeError as exc:
        # Keep compatibility with older Risk/QA registries whose runner did
        # not make ``llm`` keyword-only.
        if llm is None:
            result = runner(payload)
        else:
            raise EmployeeDispatchError(f"employee_runner_invalid:{department}") from exc
    except Exception as exc:  # noqa: BLE001 - employee boundary fails closed
        return {
            "department": department,
            "status": "DEGRADED",
            "binding": False,
            "error": type(exc).__name__,
            "error_message": str(exc)[:240],
            "executed": [],
            "failed": ["employee_registry"],
            "not_executed": [],
        }

    if not isinstance(result, Mapping):
        raise EmployeeDispatchError(f"employee_result_invalid:{department}")

    normalized = dict(result)
    normalized.setdefault("department", department)
    normalized.setdefault("status", "DEGRADED" if normalized.get("degraded") else "COMPLETED")
    normalized["binding"] = False
    return normalized


def _load_module(repo_root: Path, department: str, relative_path: str) -> Any:
    path = (repo_root / relative_path).resolve()
    if not path.is_file():
        raise EmployeeDispatchError(f"employee_registry_file_missing:{department}")

    module_name = f"hgfinance_employee_workers_{department.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise EmployeeDispatchError(f"employee_registry_import_invalid:{department}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001 - normalize import boundary
        raise EmployeeDispatchError(f"employee_registry_import_failed:{department}") from exc
    finally:
        sys.modules.pop(module_name, None)

