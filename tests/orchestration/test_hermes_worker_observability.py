from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import scripts.hermes_worker_observability as worker_observability
from scripts.hermes_worker_observability import (
    publish_accounting_worker_trace,
    publish_department_worker_trace,
    publish_discord_worker_trace,
)


class _Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_langsmith_unique_trace_quota_fails_open_and_stops_retries() -> None:
    quota_error = HTTPError(
        "https://langsmith.invalid/runs/batch",
        429,
        "rate limited",
        {},
        io.BytesIO(b'{"error":"Monthly unique traces usage limit exceeded"}'),
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
    }

    worker_observability._LANGSMITH_USAGE_LIMITED = False
    try:
        with patch(
            "scripts.hermes_worker_observability.urllib.request.urlopen",
            side_effect=quota_error,
        ) as open_url:
            assert not worker_observability._post_batch(env=env, runs=[{"id": "run"}])
            open_url.assert_called_once()

        assert worker_observability._LANGSMITH_USAGE_LIMITED is True
        with patch(
            "scripts.hermes_worker_observability.urllib.request.urlopen"
        ) as open_url:
            assert not worker_observability._post_batch(env=env, runs=[{"id": "run-2"}])
            open_url.assert_not_called()
    finally:
        worker_observability._LANGSMITH_USAGE_LIMITED = False


def test_explicit_publisher_can_run_with_automatic_tracing_disabled() -> None:
    assert worker_observability._enabled(
        {
            "LANGSMITH_TRACING": "false",
            "HGFINANCE_LANGSMITH_PUBLISH_ENABLED": "true",
            "LANGSMITH_API_KEY": "test-key",
        }
    )


def test_egress_circuit_breaker_blocks_direct_batch_publisher() -> None:
    assert not worker_observability._enabled(
        {
            "LANGSMITH_TRACING": "true",
            "HGFINANCE_LANGSMITH_PUBLISH_ENABLED": "true",
            "HGFINANCE_LANGSMITH_EGRESS_ENABLED": "false",
            "LANGSMITH_API_KEY": "test-key",
        }
    )


def test_worker_trace_defaults_to_aggregate_tool_children() -> None:
    assert worker_observability._tool_trace_mode({}) == "aggregate"
    assert worker_observability._tool_trace_mode(
        {"LANGSMITH_TOOL_TRACE_MODE": "invalid"}
    ) == "aggregate"


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
        "LANGSMITH_TOOL_TRACE_MODE": "full",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
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
    assert {item["stage"] for item in metadata} == {"accounting-portfolio"}
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


def test_worker_trace_aggregates_tool_children_when_configured(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_research.log").write_text(
        "┊ ⚡ news_search 0.7s\n"
        "┊ ⚡ read_url 0.4s\n"
        "Messages: 5 (1 user, 2 tool calls)\n",
        encoding="utf-8",
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TOOL_TRACE_MODE": "aggregate",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_research",
            task_body="workflow_root_task_id=t_root",
            task_status="done",
            run_id="41",
            return_code=0,
            started_ms=1_000,
            ended_ms=3_000,
            argv=["-p", "research-department", "chat"],
            env=env,
        )

    runs = json.loads(open_url.call_args.args[0].data.decode("utf-8"))["post"]
    assert [run["extra"]["metadata"]["observation_unit"] for run in runs] == [
        "worker",
        "model",
    ]
    assert runs[0]["extra"]["metadata"]["tool_trace_mode"] == "aggregate"
    assert runs[0]["extra"]["metadata"]["tool_trace_published_count"] == 0
    assert runs[0]["extra"]["metadata"]["tool_calls"] == 2


def test_worker_trace_keeps_request_id_separate_from_kanban_root(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_primary.log").write_text(
        "┊ ⚡ kanban_co 0.1s\nMessages: 2 (1 user, 1 tool calls)\n",
        encoding="utf-8",
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TOOL_TRACE_MODE": "full",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_primary",
            task_body=(
                "workflow_root_task_id=t_root\n"
                "request_id=research-fast-e2e-test"
            ),
            task_status="done",
            run_id="41",
            return_code=0,
            started_ms=1_000,
            ended_ms=2_000,
            argv=["-p", "research-department", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    metadata = payload["post"][0]["extra"]["metadata"]
    assert metadata["root_id"] == "t_root"
    assert metadata["workflow_root_task_id"] == "t_root"
    assert metadata["request_id"] == "research-fast-e2e-test"


def test_ceo_synthesis_uses_the_same_redacted_worker_trace_contract(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "ceo-agent"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n provider: openai-codex\n default: gpt-5.6-luna\n",
        encoding="utf-8",
    )
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_synthesis.log").write_text(
        "┊ ⚡ kanban_co 0.1s\n"
        "Messages: 3 (1 user, 1 tool call)\n",
        encoding="utf-8",
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TOOL_TRACE_MODE": "full",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_synthesis",
            task_body=(
                "workflow_root_task_id=t_root\n"
                "request_id=research-e2e-test\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
            ),
            task_status="done",
            run_id="45",
            return_code=0,
            started_ms=1_000,
            ended_ms=2_000,
            argv=["-p", "ceo-agent", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    assert [run["name"] for run in payload["post"]] == [
        "hgfinance.ceo.worker",
        "hgfinance.ceo.llm",
        "hgfinance.ceo.tool.kanban_co",
    ]
    assert all(run["extra"]["metadata"]["department"] == "ceo" for run in payload["post"])
    assert all(run["extra"]["metadata"]["request_id"] == "research-e2e-test" for run in payload["post"])
    assert all(run["extra"]["metadata"]["raw_payloads_sent"] is False for run in payload["post"])


def test_risk_blocked_worker_is_business_block_not_langsmith_error(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_risk_blocked.log").write_text(
        "Messages: 2 (1 user, 1 tool calls)\n", encoding="utf-8"
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TOOL_TRACE_MODE": "full",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_risk_blocked",
            task_body="workflow_root_task_id=t_root",
            task_status="blocked",
            run_id="42",
            return_code=1,
            started_ms=1_000,
            ended_ms=2_000,
            argv=["-p", "risk-management", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    runs = payload["post"]
    assert runs
    assert all(run["extra"]["metadata"]["status"] == "blocked" for run in runs)
    assert all("error" not in run for run in runs)


def test_risk_normal_worker_trace_can_be_sampled_without_network_io(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_risk_normal.log").write_text(
        "Messages: 2 (1 user, 1 tool call)\n", encoding="utf-8"
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_RISK_TRACE_SAMPLE_RATE": "0",
        "LANGSMITH_RISK_TRACE_SLOW_MS": "45000",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen"
    ) as open_url:
        assert not publish_department_worker_trace(
            task_id="t_risk_normal",
            task_body="workflow_root_task_id=t_root",
            task_status="done",
            run_id="43",
            return_code=0,
            started_ms=1_000,
            ended_ms=2_000,
            argv=["-p", "risk-management", "chat"],
            env=env,
        )

    open_url.assert_not_called()


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


def test_worker_log_metrics_counts_mcp_connection_failures(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_research.log").write_text(
        "┊ ⚡ mcp__rese 005930 193.0s [MCP call failed: MCPError: Connection closed]\n"
        "┊ ⚡ mcp__rese 삼성전자 14.8s [MCP call failed: MCPError: Connection closed]\n"
        "Messages: 6 (1 user, 2 tool calls)\n",
        encoding="utf-8",
    )

    from scripts.hermes_worker_observability import worker_log_metrics

    metrics = worker_log_metrics(
        task_id="t_research",
        env={"HERMES_KANBAN_HOME": str(kanban_home)},
    )

    assert metrics["tool_error_count"] == 2


def test_worker_log_metrics_counts_generic_tool_errors(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_research.log").write_text(
        "┊ ⚡ mcp__rese 005930 0.1s [Error executing tool dart_search_disclosures: unavailable]\n",
        encoding="utf-8",
    )

    from scripts.hermes_worker_observability import worker_log_metrics

    metrics = worker_log_metrics(
        task_id="t_research",
        env={"HERMES_KANBAN_HOME": str(kanban_home)},
    )

    assert metrics["tool_error_count"] == 1


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


def test_discord_worker_trace_uses_redacted_gateway_boundary() -> None:
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_discord_worker_trace(
            message_id="discord-message-1",
            profile="hr-department",
            status="completed",
            started_ms=1_000,
            ended_ms=4_000,
            session_id="session-1",
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    assert len(payload["post"]) == 1
    run = payload["post"][0]
    metadata = run["extra"]["metadata"]
    assert run["name"] == "hgfinance.hr.discord"
    assert run["session_name"] == "First"
    assert metadata["department"] == "hr"
    assert metadata["profile"] == "hr-department"
    assert metadata["request_id"] == "discord:discord-message-1"
    assert metadata["session_id"] == "session-1"
    assert metadata["latency_ms"] == 3_000
    assert metadata["raw_payloads_sent"] is False
    assert run["inputs"]["raw_payloads_sent"] is False
    assert "prompt" not in json.dumps(payload).casefold()
    assert "answer" not in json.dumps(payload).casefold()


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
        "LANGSMITH_TOOL_TRACE_MODE": "full",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(kanban_home),
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_qa",
            task_body=(
                "workflow_root_task_id=t_root\n"
                "langsmith_trace_run_id=00000000-0000-0000-0000-000000000001\n"
                "langsmith_trace_context="
                "20260827T051452450099Z00000000-0000-0000-0000-000000000001"
            ),
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
    assert all(
        run["trace_id"] == "00000000-0000-0000-0000-000000000001"
        for run in runs
    )
    assert runs[0]["parent_run_id"] == "00000000-0000-0000-0000-000000000001"
    assert runs[1]["parent_run_id"] == runs[0]["id"]
    assert runs[0]["dotted_order"].startswith(
        "20260827T051452450099Z00000000-0000-0000-0000-000000000001."
    )
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


def test_worker_trace_records_workflow_mode_and_fast_advisory_budget(tmp_path: Path):
    kanban_home = tmp_path / "shared-kanban"
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "t_fast.log").write_text(
        "Reasoning\nMessages: 4 (1 user, 1 tool call)\n",
        encoding="utf-8",
    )
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://langsmith.invalid",
        "LANGSMITH_PROJECT": "First",
        "LANGSMITH_TRACE_SAMPLE_RATE": "1",
        "HERMES_KANBAN_HOME": str(kanban_home),
        "HGFINANCE_FAST_ADVISORY_MAX_TURNS": "8",
    }

    with patch(
        "scripts.hermes_worker_observability.urllib.request.urlopen",
        return_value=_Response(),
    ) as open_url:
        assert publish_department_worker_trace(
            task_id="t_fast",
            task_body=(
                "workflow_root_task_id=t_root\n"
                "workflow_mode=analysis\n"
                "analysis_mode=fast_advisory\n"
            ),
            task_status="done",
            run_id="44",
            return_code=0,
            started_ms=1_000,
            ended_ms=4_000,
            argv=["-p", "research-department", "chat"],
            env=env,
        )

    payload = json.loads(open_url.call_args.args[0].data.decode("utf-8"))
    metadata = payload["post"][0]["extra"]["metadata"]
    assert metadata["workflow_mode"] == "analysis"
    assert metadata["stage"] == "research"
    assert metadata["analysis_mode"] == "fast_advisory"
    assert metadata["configured_max_turns"] == 8
    assert metadata["actual_turns"] == 1
