from __future__ import annotations

import subprocess
from types import SimpleNamespace

from apps.api import hermes_boundary


def test_unblock_accepts_durable_ready_state_after_cli_timeout(monkeypatch):
    def time_out(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=["hermes"], timeout=8)

    observed: dict[str, object] = {}

    def show(task_id: str, *, timeout: float | None = None):
        observed.update(task_id=task_id, timeout=timeout)
        return {"id": task_id, "status": "ready"}

    monkeypatch.setattr(hermes_boundary.subprocess, "run", time_out)
    monkeypatch.setattr(hermes_boundary, "show_kanban_task", show)
    monkeypatch.setenv("KANBAN_CLI_TIMEOUT_SECONDS", "8")

    assert hermes_boundary.unblock_kanban_task(task_id="t_timeout") is True
    assert observed == {"task_id": "t_timeout", "timeout": 8.0}


def test_unblock_rejects_timeout_when_task_remains_blocked(monkeypatch):
    monkeypatch.setattr(
        hermes_boundary.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["hermes"], timeout=8)
        ),
    )
    monkeypatch.setattr(
        hermes_boundary,
        "show_kanban_task",
        lambda task_id, *, timeout=None: {"id": task_id, "status": "blocked"},
    )

    assert hermes_boundary.unblock_kanban_task(task_id="t_blocked") is False


def test_unblock_returns_immediately_on_cli_success(monkeypatch):
    monkeypatch.setattr(
        hermes_boundary.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        hermes_boundary,
        "show_kanban_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("successful unblock must not require a readback")
        ),
    )

    assert hermes_boundary.unblock_kanban_task(task_id="t_success") is True
