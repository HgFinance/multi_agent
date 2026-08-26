from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scripts.hermes_worker_observability import publish_accounting_worker_trace


class _Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_accounting_worker_trace_correlates_task_model_and_tools(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "accounting-portfolio-department"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n provider: openai-codex\n default: gpt-5.6-luna\n",
        encoding="utf-8",
    )
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_primary.log").write_text(
        "┊ ⚡ kanban_sh 0.0s\n"
        "┊ ⚡ skill 0.7s\n"
        "┊ ⚡ kanban_co 0.1s\n"
        "Messages: 9 (1 user, 7 tool calls)\n",
        encoding="utf-8",
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch("scripts.hermes_worker_observability.urllib.request.urlopen", return_value=_Response()) as open_url:
        assert publish_accounting_worker_trace(
            task_id="t_primary",
            task_body="workflow_root_task_id=t_root",
            task_status="done",
            run_id="41",
            return_code=0,
            started_ms=1_000,
            ended_ms=4_000,
            argv=["-p", "accounting-portfolio-department", "chat"],
            env=env,
        )

    request = open_url.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    runs = payload["post"]
    assert len(runs) == 5  # worker + model + three observed tools
    metadata = [run["extra"]["metadata"] for run in runs]
    assert {item["task_id"] for item in metadata} == {"t_primary"}
    assert {item["workflow_root_task_id"] for item in metadata} == {"t_root"}
    assert {item["model_name"] for item in metadata} == {"gpt-5.6-luna"}
    assert metadata[0]["provider"] == "openai-codex"
    assert metadata[0]["tool_names"] == ["kanban_sh", "skill", "kanban_co"]
    assert metadata[0]["tool_call_count"] == 7
    assert all(item["raw_payloads_sent"] is False for item in metadata)
    assert all(run["inputs"] == {} and run["outputs"] == {} for run in runs)


def test_accounting_worker_trace_is_fail_open_when_disabled():
    assert not publish_accounting_worker_trace(
        task_id="t_primary",
        task_body="workflow_root_task_id=t_root",
        task_status="done",
        run_id="41",
        return_code=0,
        started_ms=1_000,
        ended_ms=1_001,
        argv=["-p", "accounting-portfolio-department"],
        env={"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": ""},
    )
