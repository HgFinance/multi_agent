from __future__ import annotations

from datetime import datetime, timezone, timedelta

import time
import pytest

from eval_runner import (
    AppendOnlyViolation,
    CandidateSpec,
    EvalCase,
    EvalErrorCode,
    EvalMetric,
    EvalResult,
    EvalRun,
    EvalRunner,
    EvalSet,
    InMemoryEvalAuditRepository,
    MockToolRegistry,
)


def _eval_set(*, case_key: str = "case-1", expected: object = "ok") -> EvalSet:
    payload = {
        "eval_set_id": "00000000-0000-0000-0000-000000000001",
        "role_code": "qa-evaluator",
        "version": 1,
        "cases": [{"case_key": case_key, "expected": expected}],
    }
    return EvalSet(**payload, content_hash=EvalSet.compute_content_hash(payload))


def test_runner_owns_latency_protects_evidence_and_isolates_tools() -> None:
    seen_tools: list[MockToolRegistry] = []

    class Candidate:
        def run(self, case: EvalCase, *, tools: MockToolRegistry, memory) -> dict:
            seen_tools.append(tools)
            assert tools.call("fixture") == {"value": 1}
            return {
                "output": "ok",
                "latency_ms": 999999,
                "evidence": {"trace_id": "candidate-overwrite", "metric_version": "old"},
            }

    repository = InMemoryEvalAuditRepository()
    runner = EvalRunner(
        repository=repository,
        candidate_runner=Candidate(),
        mock_tools=MockToolRegistry({"fixture": {"value": 1}}),
    )
    first = runner.run(_eval_set())
    second = runner.run(_eval_set())

    assert first.status == second.status == "COMPLETED"
    assert repository.eval_sets[_eval_set().eval_set_id].identity == _eval_set().identity
    assert seen_tools[0] is not seen_tools[1]
    assert seen_tools[0].calls == seen_tools[1].calls == [{"tool": "fixture", "arguments": {}}]
    latency = next(row for row in repository.results if row.eval_run_id == first.eval_run_id and row.metric is EvalMetric.LATENCY_MS)
    assert latency.score is not None and latency.score < 999999
    assert latency.evidence["trace_id"] == first.trace_id
    assert latency.evidence["metric_version"] != "old"


def test_candidate_failure_status_fails_closed() -> None:
    class FailedCandidate:
        def run(self, case: EvalCase, *, tools, memory) -> dict:
            return {"status": "FAILED", "output": "ignored"}

    repository = InMemoryEvalAuditRepository()
    run = EvalRunner(repository=repository, candidate_runner=FailedCandidate()).run(_eval_set())

    rows = repository.results_for_run(run.eval_run_id)
    assert run.status == "FAILED"
    assert rows and all(not row.passed for row in rows)
    assert {row.error_code for row in rows} == {EvalErrorCode.CANDIDATE_FAILURE.value}

def test_denied_tool_has_no_registry_side_effect() -> None:
    seen: list[MockToolRegistry] = []

    class Candidate:
        def run(self, case: EvalCase, *, tools: MockToolRegistry, memory) -> dict:
            seen.append(tools)
            tools.call("forbidden")
            return {"output": "unreachable"}

    repository = InMemoryEvalAuditRepository()
    run = EvalRunner(
        repository=repository,
        candidate_runner=Candidate(),
        mock_tools=MockToolRegistry({"allowed": 1}, allowed_tools={"allowed"}),
    ).run(_eval_set())
    assert run.status == "FAILED"
    assert seen and seen[0].calls == []
    assert {row.error_code for row in repository.results} == {EvalErrorCode.TOOLCALL_DENIED.value}


def test_timeout_is_bounded_and_fail_closed() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = iter(
        [
            start,
            start,
            start + timedelta(seconds=2),
            start + timedelta(seconds=2),
            start + timedelta(seconds=2),
            start + timedelta(seconds=2),
        ]
    )

    class SlowClock:
        def __call__(self) -> datetime:
            return next(ticks)

    class Candidate:
        def run(self, case: EvalCase, *, tools, memory) -> dict:
            return {"output": "ok"}

    repository = InMemoryEvalAuditRepository()
    run = EvalRunner(
        repository=repository,
        candidate_runner=Candidate(),
        clock=SlowClock(),
        timeout_ms=100,
    ).run(_eval_set())
    rows = repository.results_for_run(run.eval_run_id)
    assert run.status == "FAILED"
    assert {row.error_code for row in rows} == {EvalErrorCode.TIMEOUT.value}
    assert rows[0].evidence["error_detail"]


def test_timeout_interrupts_a_blocked_candidate() -> None:
    class HangingCandidate:
        def run(self, case: EvalCase, *, tools, memory) -> dict:
            time.sleep(0.2)
            return {"output": "late"}

    repository = InMemoryEvalAuditRepository()
    run = EvalRunner(
        repository=repository,
        candidate_runner=HangingCandidate(),
        timeout_ms=20,
    ).run(_eval_set())
    assert run.status == "FAILED"
    assert {row.error_code for row in repository.results} == {EvalErrorCode.TIMEOUT.value}

def test_canonical_hash_and_malformed_case_errors_are_deterministic() -> None:
    payload = {
        "eval_set_id": "set-1",
        "role_code": "qa",
        "version": 1,
        "cases": [{"case_key": "case-1"}],
    }
    with pytest.raises(ValueError, match="content_hash"):
        EvalSet(**payload, content_hash="sha256:" + "0" * 64)

    bad_payload = {**payload, "cases": [{"unknown": True}]}
    with pytest.raises(ValueError):
        EvalSet(**bad_payload, content_hash=EvalSet.compute_content_hash(bad_payload))


def test_champion_comparison_exposes_all_metrics_and_rejects_mismatch() -> None:
    eval_set = _eval_set()

    class Candidate:
        def __init__(self, output: str):
            self.output = output

        def run(self, case: EvalCase, *, tools, memory) -> dict:
            return {"output": self.output, "risk_compliant": True}

    repository = InMemoryEvalAuditRepository()
    runner = EvalRunner(repository=repository)
    report = runner.evaluate(eval_set, Candidate("ok"), champion=Candidate("old"))
    assert report.comparison is not None
    assert report.comparison.status == "COMPARED"
    assert set(report.comparison.metrics) == {metric.value for metric in EvalMetric}
    assert repository.comparison_for_run(report.candidate_run.eval_run_id) == report.comparison
    different = _eval_set(case_key="different")
    mismatch = runner.compare_champion(report.candidate_run, eval_set, Candidate("old"), champion_eval_set=different)
    assert mismatch.status == "NOT_EXECUTED"
    assert mismatch.error_code == EvalErrorCode.EVAL_SET_MISMATCH.value


def test_append_only_replay_is_exactly_idempotent_and_conflicts() -> None:
    repository = InMemoryEvalAuditRepository()
    now = datetime.now(timezone.utc)
    run = EvalRun(
        eval_run_id="00000000-0000-0000-0000-000000000002",
        eval_set_id="00000000-0000-0000-0000-000000000001",
        eval_set_version=1,
        eval_set_hash="sha256:" + "1" * 64,
        candidate_id="candidate",
        candidate_profile_version="profile-v1",
        config={},
        status="QUEUED",
        trace_id="00000000-0000-0000-0000-000000000003",
        environment="SHADOW",
        mock_tool_manifest={},
        model_version="model-v1",
        adapter_version="adapter-v1",
        evidence_hash="sha256:" + "2" * 64,
        started_at=now,
        created_at=now,
    )
    repository.append_run(run)
    repository.append_run(run)
    assert len(repository.runs) == 1
    with pytest.raises(AppendOnlyViolation):
        repository.append_run(run.model_copy(update={"status": "RUNNING"}))

    result = EvalResult(
        eval_result_id="00000000-0000-0000-0000-000000000004",
        eval_run_id=run.eval_run_id,
        case_key="case-1",
        metric=EvalMetric.ACCURACY,
        score=1,
        passed=True,
        evidence={},
        created_at=now,
    )
    repository.append_result(result)
    repository.append_result(result)
    assert len(repository.results) == 1
    with pytest.raises(AppendOnlyViolation):
        repository.append_result(result.model_copy(update={"score": 0, "eval_result_id": "00000000-0000-0000-0000-000000000005"}))
