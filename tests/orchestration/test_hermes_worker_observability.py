from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scripts.hermes_worker_observability import (
    publish_accounting_worker_trace,
    publish_department_worker_trace,
)


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
    assert metadata[0]["tool_calls"] == 7
    assert metadata[0]["llm_calls"] == 1
    assert metadata[0]["tool_error_count"] == 0
    assert metadata[0]["tool_latency_available"] is True
    assert metadata[0]["tool_timing_source"] == "hermes-log-duration"
    assert metadata[0]["telemetry_completeness"] == "runtime-and-boundary"
    assert metadata[1]["observation_unit"] == "model"
    assert metadata[1]["latency_ms"] == 2_200
    assert metadata[1]["latency_scope"] == "model_estimate"
    assert metadata[2]["latency_ms"] is None
    assert metadata[2]["latency_scope"] == "unavailable"
    assert metadata[3]["latency_ms"] == 700
    assert metadata[3]["latency_scope"] == "tool_observation"
    assert metadata[4]["latency_ms"] == 100
    assert metadata[4]["latency_scope"] == "tool_observation"
    assert all(item["raw_payloads_sent"] is False for item in metadata)
    assert all(run["inputs"]["task_id"] == "t_primary" for run in runs)
    assert all(run["inputs"]["workflow_root_task_id"] == "t_root" for run in runs)
    assert all(run["inputs"]["request_id"] == "t_root" for run in runs)
    assert all(run["inputs"]["task_body_present"] is True for run in runs)
    assert all(run["outputs"]["status"] == "completed" for run in runs)
    assert all(run["outputs"]["tool_calls"] == 7 for run in runs)
    assert all(run["outputs"]["tool_error_count"] == 0 for run in runs)
    assert all(run["outputs"]["raw_payloads_sent"] is False for run in runs)
    assert runs[1]["outputs"]["latency_ms"] == 2_200
    assert runs[2]["outputs"]["latency_ms"] is None


def test_zero_duration_tool_markers_are_not_reported_as_available_latency(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_qa.log").write_text(
        "┊ ⚡ kanban_show 0.0s\n"
        "┊ ⚡ kanban_co 0s\n"
        "Messages: 3 (1 user, 2 tool calls)\n",
        encoding="utf-8",
    )
    from scripts.hermes_worker_observability import worker_log_metrics

    metrics = worker_log_metrics(
        task_id="t_qa",
        env={"HERMES_KANBAN_HOME": str(kanban_home)},
    )
    assert metrics["tool_duration_total_ms"] == 0
    assert metrics["tool_latency_available"] is False
    assert metrics["tool_timing_source"] == "unavailable"


def test_worker_log_metrics_reads_terminal_and_file_tool_durations(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_hr.log").write_text(
        "┊ ⚡ kanban_sh 0.0s\n"
        "┊ 💻 $         python3 helper.py  2.3s\n"
        "┊ 📖 read      evidence.json L1-20  0.3s\n"
        "┊ 💻 $         sha256sum evidence.json  0.1s\n"
        "┊ ⚡ kanban_co 0.0s\n"
        "Messages: 7 (1 user, 5 tool calls)\n",
        encoding="utf-8",
    )

    from scripts.hermes_worker_observability import worker_log_metrics

    metrics = worker_log_metrics(
        task_id="t_hr",
        env={"HERMES_KANBAN_HOME": str(kanban_home)},
    )

    assert metrics["tool_names"] == [
        "kanban_sh",
        "kanban_co",
        "terminal",
        "read",
    ]
    assert metrics["tool_duration_total_ms"] == 2700
    assert metrics["tool_latency_available"] is True
    assert metrics["tool_timing_source"] == "hermes-log-duration"


def test_worker_log_metrics_counts_structured_tool_errors(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_qa.log").write_text(
        "Tool execute_code returned error\n"
        "returned_error=command blocked\n"
        "Messages: 4 (1 user, 2 tool calls)\n",
        encoding="utf-8",
    )

    from scripts.hermes_worker_observability import worker_log_metrics

    metrics = worker_log_metrics(
        task_id="t_qa",
        env={"HERMES_KANBAN_HOME": str(kanban_home)},
    )

    assert metrics["tool_error_count"] == 2


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


def test_qa_worker_trace_uses_the_same_task_correlated_redacted_contract(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "qa-department"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n provider: openai-codex\n default: gpt-5.6-luna\n",
        encoding="utf-8",
    )
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_qa.log").write_text(
        "┊ ⚡ kanban_show 0.1s\n"
        "┊ ⚡ kanban_co 0.1s\n"
        "Messages: 4 (1 user, 2 tool calls)\n",
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

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_qa",
            task_body="workflow_root_task_id=t_root",
            task_status="done",
            run_id="42",
            return_code=0,
            started_ms=1_000,
            ended_ms=4_000,
            argv=["-p", "qa-department", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    runs = payload["post"]
    assert [run["name"] for run in runs] == [
        "hgfinance.qa.worker",
        "hgfinance.qa.llm",
        "hgfinance.qa.tool.kanban_show",
        "hgfinance.qa.tool.kanban_co",
    ]
    assert all(run["extra"]["metadata"]["department"] == "qa" for run in runs)
    assert all(run["extra"]["metadata"]["task_id"] == "t_qa" for run in runs)
    assert all(run["extra"]["metadata"]["llm_calls"] == 1 for run in runs)
    assert all(run["inputs"]["workflow_root_task_id"] == "t_root" for run in runs)
    assert all(run["inputs"]["request_id"] == "t_root" for run in runs)
    assert all(run["outputs"]["tool_calls"] == 2 for run in runs)
    assert all(run["outputs"]["raw_payloads_sent"] is False for run in runs)


def test_failed_worker_trace_marks_langsmith_run_as_failed_without_raw_error(
    tmp_path: Path,
):
    profile_dir = tmp_path / "profiles" / "qa-department"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n provider: openai-codex\n default: gpt-5.6-luna\n",
        encoding="utf-8",
    )
    kanban_home = tmp_path / "shared-kanban"
    (kanban_home / "kanban" / "logs").mkdir(parents=True)
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_failed",
            task_body="workflow_root_task_id=t_root",
            task_status="timed_out",
            run_id="43",
            return_code=-15,
            started_ms=1_000,
            ended_ms=4_000,
            argv=["-p", "qa-department", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    assert {run["error"] for run in payload["post"]} == {"kanban_timed_out"}
    assert all(run["extra"]["metadata"]["raw_payloads_sent"] is False for run in payload["post"])
