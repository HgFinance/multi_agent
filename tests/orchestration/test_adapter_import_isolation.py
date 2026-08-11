"""Import-boundary tests for the lightweight CEO supervisor process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_ceo_supervisor_import_does_not_eagerly_load_paper_runtime() -> None:
    probe = """
import sys
import orchestration.adapters.ceo_supervisor

assert "orchestration.adapters.paper_pipeline" not in sys.modules
assert "orchestration.employee_dispatch" not in sys.modules
assert "departments.employee_worker_runtime" not in sys.modules
assert "langgraph" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_existing_paper_pipeline_exports_remain_lazy_and_available() -> None:
    probe = """
import sys
import orchestration.adapters as adapters

assert "orchestration.adapters.paper_pipeline" not in sys.modules
from orchestration.adapters import PaperPipelineAdapter, build_paper_handlers

assert adapters.PaperPipelineAdapter is PaperPipelineAdapter
assert callable(build_paper_handlers)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
