from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from orchestration.ceo_workflow_scope import build_root_body
from orchestration.langsmith_feedback import (
    FeedbackConfig,
    FeedbackLedger,
    LangSmithFeedbackService,
    TraceObservation,
    evaluation_run_id,
    evaluate_observation,
    observation_from_run,
)
from orchestration.langsmith_feedback import _aggregate_metric_window


class _Run:
    id = "run-1"
    name = "worker.qa"
    status = "success"
    start_time = datetime.now(timezone.utc)
    end_time = datetime.now(timezone.utc)
    extra = {
        "metadata": {
            "request_id": "discord:1",
            "root_id": "t_root",
            "department": "qa-department",
            "stage": "qa",
            "status": "COMPLETED",
            "latency_ms": 12,
            "eval_score": 0.95,
            "raw_payloads_sent": False,
            "prompt": "must never be copied",
            "api_key": "must never be copied",
        },
        "runtime": {"secret": "must never be copied"},
    }
    inputs = {"prompt": "must never be read"}
    outputs = {"answer": "must never be read"}


def test_observation_allowlists_metadata_and_never_reads_payload() -> None:
    observation = observation_from_run(_Run())

    assert observation.source_run_id == "run-1"
    assert observation.department == "qa-department"
    assert observation.metadata["raw_payloads_sent"] is False
    assert "prompt" not in observation.metadata
    assert "api_key" not in observation.metadata
    assert "secret" not in observation.metadata


def test_evaluator_passes_structured_success_without_model_content() -> None:
    result = evaluate_observation(observation_from_run(_Run()))

    assert result.decision == "OBSERVED_PASS"
    assert result.finding_codes == ()
    assert result.score == 0.95
    assert result.metadata["request_id"] == "discord:1"
    assert result.metadata["raw_payloads_sent"] is False


def test_evaluator_creates_bounded_improvement_findings() -> None:
    observation = TraceObservation(
        source_run_id="run-2",
        name="worker.risk",
        status="error",
        started_at=None,
        ended_at=None,
        metadata={
            "stage": "risk",
            "status": "DEGRADED",
            "error_count": 1,
            "latency_ms": 70_000,
            "raw_payloads_sent": False,
        },
    )
    result = evaluate_observation(observation, latency_warn_ms=60_000)

    assert result.decision == "IMPROVEMENT_CANDIDATE"
    assert "WORKER_OR_WORKFLOW_DEGRADED" in result.finding_codes
    assert "LATENCY_ABOVE_THRESHOLD" in result.finding_codes
    assert "CORRELATION_METADATA_MISSING" in result.finding_codes


def test_metrics_are_reduced_to_one_non_correlated_window_observation() -> None:
    first = _Run()
    second = _Run()
    second.id = "run-2"
    second.status = "error"
    second.extra = {
        "metadata": {
            "stage": "risk",
            "status": "error",
            "latency_ms": 90,
            "error_count": 1,
            "raw_payloads_sent": False,
        }
    }
    window = _aggregate_metric_window(
        [first, second],
        project_name="HgFinance-Metrics",
        window_start=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc),
    )

    assert window is not None
    assert window.source_run_id.startswith("metrics-window:HgFinance-Metrics:")
    result = evaluate_observation(window, source_project="HgFinance-Metrics")
    assert result.metadata["metric_count"] == 2
    assert "CORRELATION_METADATA_MISSING" not in result.finding_codes
    assert "WORKER_OR_WORKFLOW_DEGRADED" in result.finding_codes


def test_privacy_violation_is_review_required() -> None:
    observation = TraceObservation(
        source_run_id="run-3",
        name="worker.research",
        status="success",
        started_at=None,
        ended_at=None,
        metadata={"stage": "research", "raw_payloads_sent": True},
    )

    result = evaluate_observation(observation)

    assert result.decision == "REVIEW_REQUIRED"
    assert result.finding_codes == ("PRIVACY_PAYLOAD_PRESENT", "CORRELATION_METADATA_MISSING")


def test_ledger_is_idempotent_and_approval_creates_bounded_hint(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    assert ledger.enqueue("source-1", "First") is True
    assert ledger.enqueue("source-1", "First") is False
    job = ledger.claim()
    assert job is not None
    result = evaluate_observation(observation_from_run(_Run()))
    artifact_id = ledger.complete("source-1", "eval-1", result)

    assert ledger.pending(10)[0]["artifact_id"] == artifact_id
    assert ledger.approve(artifact_id, "APPROVED", "qa-user", "reviewed") is True
    assert ledger.approve(artifact_id, "APPROVED", "qa-user", "duplicate") is False
    assert ledger.approved_hints(None, limit=3, max_chars=1200) is None
    candidates = ledger.benchmark_candidates(10)
    assert candidates[0]["artifact_id"] == artifact_id
    assert candidates[0]["benchmark_status"] == "PENDING"
    assert ledger.update_benchmark(
        artifact_id,
        status="PASSED",
        benchmark_id="offline-v1",
        score=0.91,
        report_ref="sha256:report",
        result_summary="offline gate passed",
    ) is True
    hint = ledger.approved_hints(None, limit=3, max_chars=1200)
    assert hint is not None
    assert hint["items"][0]["source"] == "qa-approved-langsmith-feedback"
    assert "prompt" not in str(hint)


def test_active_hint_is_local_only_and_requires_passed_benchmark(tmp_path, monkeypatch) -> None:
    path = tmp_path / "feedback.sqlite3"
    ledger = FeedbackLedger(str(path))
    assert ledger.enqueue("source-active", "First") is True
    assert ledger.claim() is not None
    artifact_id = ledger.complete(
        "source-active",
        "eval-active",
        evaluate_observation(observation_from_run(_Run())),
    )
    assert ledger.approve(artifact_id, "APPROVED", "qa-user", "reviewed") is True

    monkeypatch.setenv("LANGSMITH_FEEDBACK_MODE", "active")
    monkeypatch.setenv("LANGSMITH_FEEDBACK_STATE_PATH", str(path))
    monkeypatch.setattr("orchestration.langsmith_feedback._HINT_CACHE", None)
    monkeypatch.setattr(
        "langsmith.Client",
        lambda **_: (_ for _ in ()).throw(AssertionError("active hint must not call LangSmith")),
    )

    from orchestration.langsmith_feedback import approved_feedback_hint

    assert approved_feedback_hint() is None
    assert ledger.update_benchmark(
        artifact_id,
        status="PASSED",
        benchmark_id="offline-active-v1",
        score=0.9,
    ) is True
    monkeypatch.setattr("orchestration.langsmith_feedback._HINT_CACHE", None)
    hint = approved_feedback_hint()
    assert hint is not None
    assert hint["items"][0]["source"] == "qa-approved-langsmith-feedback"


def test_ledger_cleanup_removes_expired_artifacts_and_decisions(tmp_path) -> None:
    path = tmp_path / "feedback.sqlite3"
    ledger = FeedbackLedger(str(path))
    ledger.enqueue("source-old", "First")
    assert ledger.claim() is not None
    artifact_id = ledger.complete("source-old", "eval-old", evaluate_observation(observation_from_run(_Run())))
    assert ledger.approve(artifact_id, "APPROVED", "qa-user", "reviewed") is True

    with sqlite3.connect(path) as db:
        db.execute("UPDATE langsmith_feedback_artifacts SET created_at='2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE langsmith_feedback_jobs SET updated_at='2000-01-01T00:00:00+00:00'")

    assert ledger.cleanup(1) == 2
    assert ledger.pending(10) == []
    assert ledger.approved_hints(None, limit=3, max_chars=1200) is None


def test_evaluation_run_id_is_stable_and_stale_jobs_are_reclaimable(tmp_path) -> None:
    assert evaluation_run_id("source-1", "HgFinance-Evals") == evaluation_run_id(
        "source-1", "HgFinance-Evals"
    )
    assert evaluation_run_id("source-1", "HgFinance-Evals") != evaluation_run_id(
        "source-2", "HgFinance-Evals"
    )

    path = tmp_path / "feedback.sqlite3"
    ledger = FeedbackLedger(str(path))
    assert ledger.enqueue("source-stale", "First") is True
    assert ledger.claim() is not None
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE langsmith_feedback_jobs SET updated_at='2000-01-01T00:00:00+00:00'"
        )
    reclaimed = ledger.claim()
    assert reclaimed is not None
    assert reclaimed["source_run_id"] == "source-stale"


def test_root_body_feedback_is_advisory_and_contract_is_unchanged() -> None:
    body = build_root_body(
        "삼성전자 분석",
        "discord:1",
        approved_feedback_hint={
            "items": [
                {
                    "department": "qa",
                    "decision": "IMPROVEMENT_CANDIDATE",
                    "finding_codes": ["LATENCY_ABOVE_THRESHOLD"],
                    "summaries": ["worker latency exceeded threshold"],
                }
            ]
        },
    )

    assert "QA-approved observability feedback" in body
    assert "raw prompt" not in body
    assert "workflow_mode=analysis" in body
    assert "qa_enabled=true" in body


def test_feedback_config_bounds_concurrency_inputs(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_FEEDBACK_POLL_SECONDS", "0")
    monkeypatch.setenv("LANGSMITH_FEEDBACK_BATCH_SIZE", "99999")
    monkeypatch.setenv("LANGSMITH_FEEDBACK_MAX_PENDING", "1")
    monkeypatch.setenv("LANGSMITH_FEEDBACK_METRICS_MAX_RUNS", "99999")

    config = FeedbackConfig.from_env()

    assert config.poll_seconds == 5.0
    assert config.batch_size == 100
    assert config.max_pending == 10
    assert config.metrics_window_seconds == 300
    assert config.metrics_max_runs == 100


def test_service_evaluates_allowlisted_snapshot_without_reading_run_payload(tmp_path, monkeypatch) -> None:
    class _Client:
        def __init__(self, **kwargs):
            self.read_called = False
            self.list_calls = []

        def list_runs(self, **kwargs):
            self.list_calls.append(kwargs)
            if kwargs.get("project_name") == "First":
                return iter([_Run()])
            return iter([])

        def read_run(self, *_args, **_kwargs):
            self.read_called = True
            raise AssertionError("raw run read must not be used")

    fake_client = _Client()
    monkeypatch.setattr("langsmith.Client", lambda **kwargs: fake_client)
    monkeypatch.setattr("orchestration.langsmith_feedback.publish_evaluation", lambda result, project: "eval-1")
    config = FeedbackConfig(
        mode="shadow",
        workflow_project="First",
        metrics_project="HgFinance-Metrics",
        evals_project="HgFinance-Evals",
        state_path=str(tmp_path / "feedback.sqlite3"),
        poll_seconds=5,
        lookback_seconds=60,
        batch_size=10,
        max_pending=50,
        retention_days=30,
        latency_warn_ms=60_000,
        max_feedback_items=3,
        max_feedback_chars=1200,
        metrics_window_seconds=300,
        metrics_max_runs=500,
    )

    service = LangSmithFeedbackService(config=config)
    monkeypatch.setattr("orchestration.llm_observability.langsmith_enabled", lambda: True)
    result = service.run_once()

    assert result["completed"] == 1
    assert fake_client.read_called is False
    root_call = fake_client.list_calls[0]
    assert "end_time" in root_call
    assert "start_time" not in root_call
    assert "gt(end_time" in root_call["filter"]


def test_service_is_noop_when_langsmith_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("orchestration.llm_observability.langsmith_enabled", lambda: False)

    service = LangSmithFeedbackService(
        config=FeedbackConfig(
            mode="shadow",
            workflow_project="First",
            metrics_project="HgFinance-Metrics",
            evals_project="HgFinance-Evals",
            state_path=str(tmp_path / "feedback.sqlite3"),
            poll_seconds=5,
            lookback_seconds=60,
            batch_size=10,
            max_pending=50,
            retention_days=30,
            latency_warn_ms=60_000,
            max_feedback_items=3,
            max_feedback_chars=1200,
            metrics_window_seconds=300,
            metrics_max_runs=500,
        )
    )

    assert service.run_once() == {"discovered": 0, "completed": 0, "failed": 0, "dropped": 0}
