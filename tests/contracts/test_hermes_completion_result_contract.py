"""Regression tests for the build-time Hermes completion contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional


def _installer_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "ceo-kanban"
        / "install_result_contract.py"
    )
    spec = importlib.util.spec_from_file_location("result_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HERMES_COMPLETION_FIXTURE = """
from typing import Optional

def complete_task(
    conn,
    task_id,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
):
    \"\"\"complete\"\"\"
    now = int(time.time())
    return result, summary, now

def edit_completed_task_result(conn, task_id, *, result: str,
                               summary: Optional[str] = None):
    handoff_summary = summary if summary is not None else result
    return result, handoff_summary
"""


def test_installer_patches_both_completion_paths_idempotently() -> None:
    installer = _installer_module()

    patched = installer._install_db(_HERMES_COMPLETION_FIXTURE)

    assert patched.count(installer.MARKER) == 3
    assert patched.index("_canonical_handoff_values(result, summary)") < patched.index(
        "now = int(time.time())"
    )
    assert patched.index("_canonical_handoff_values(result, summary)") < patched.index(
        "handoff_summary = summary"
    )
    assert installer._install_db(patched) == patched


def test_canonical_fallback_promotes_summary_to_result() -> None:
    installer = _installer_module()
    patched = installer._install_db(_HERMES_COMPLETION_FIXTURE)
    namespace = {"Optional": Optional}
    exec(patched, namespace)  # noqa: S102 - execute the generated test module.

    assert namespace["_canonical_handoff_values"](None, "answer") == (
        "answer",
        "answer",
    )
    assert namespace["_canonical_handoff_values"]("answer", "short") == (
        "answer",
        "short",
    )
