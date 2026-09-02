from __future__ import annotations

from benchmarks.multi_agent_smoke_replay import (
    MODES,
    REPETITIONS,
    SCENARIOS,
    Status,
    format_report,
    run_benchmark,
)


def test_offline_smoke_has_30_runs_and_required_contracts() -> None:
    metrics = run_benchmark()
    assert len(metrics) == len(MODES) * len(SCENARIOS) * REPETITIONS
    assert all(item.forbidden_side_effect_count == 0 for item in metrics)
    assert all(item.output_contract_valid for item in metrics)
    assert all(item.temporary_artifact_cleanup for item in metrics)
    assert all(item.status == Status.SUCCESS for item in metrics if item.scenario != "isolated_failure")
    assert all(item.status == Status.FAILED_CLOSED for item in metrics if item.scenario == "isolated_failure")


def test_multi_failure_isolated_and_single_failure_closed() -> None:
    metrics = run_benchmark()
    multi = [item for item in metrics if item.mode == "multi_mode" and item.scenario == "isolated_failure"]
    single = [item for item in metrics if item.mode == "single_mode" and item.scenario == "isolated_failure"]
    assert all(item.isolated_failure for item in multi)
    assert all(not item.isolated_failure for item in single)
    assert all(item.fake_tool_calls == 1 for item in multi)
    assert all(item.fake_tool_calls == 1 for item in single)


def test_report_explicitly_does_not_report_p95_or_real_cost() -> None:
    report = format_report(run_benchmark())
    assert "offline deterministic smoke benchmark, n=5; p95 미산출" in report
    assert "NOT_MEASURED: offline fake replay" in report
    assert "p95" not in report.splitlines()[-1]
