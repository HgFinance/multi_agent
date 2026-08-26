from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from orchestration.ceo_workflow_scope import (
    approved_feedback_section_from_root,
    build_root_body,
)
from orchestration.langsmith_feedback import (
    FeedbackConfig,
    FeedbackLedger,
    LangSmithFeedbackService,
    TraceObservation,
    attribute_workflow_bottleneck,
    evaluation_run_id,
    evaluate_observation,
    observation_from_run,
)
from orchestration.langsmith_feedback import _aggregate_metric_window
from orchestration.semantic_qa import evaluate_answer, evaluate_prompt_answer
from orchestration.qa_feedback_benchmarks import run_pending_feedback_benchmarks


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


def _actionable_result(
    source_run_id: str,
    *,
    request_id: str,
    department: str = "qa-department",
):
    return evaluate_observation(
        TraceObservation(
            source_run_id=source_run_id,
            name=f"worker.{department}",
            status="error",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": request_id,
                "department": department,
                "stage": department,
                "status": "DEGRADED",
                "error_count": 1,
                "raw_payloads_sent": False,
            },
        )
    )


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


def test_root_latency_is_attributed_to_longest_primary_kanban_task(tmp_path) -> None:
    database_path = tmp_path / "kanban.db"
    with sqlite3.connect(database_path) as database:
        database.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                body TEXT,
                assignee TEXT,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                idempotency_key TEXT
            )
            """
        )
        database.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ("t_root", "request", "ceo-agent", 1, 1, 31, "request-1"),
                (
                    "t_trading",
                    "workflow_root_task_id=t_root\nworkflow_role=primary",
                    "trading-department",
                    35,
                    36,
                    97,
                    "t_root:primary:trading-department",
                ),
                (
                    "t_research",
                    "workflow_root_task_id=t_root\nworkflow_role=primary",
                    "research-department",
                    35,
                    36,
                    48,
                    "t_root:primary:research-department",
                ),
            ),
        )

    attributed = attribute_workflow_bottleneck(
        TraceObservation(
            source_run_id="run-root",
            name="hgfinance.user-query",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "request-1",
                "stage": "ceo-ingress",
                "trace_kind": "workflow_root",
                "latency_scope": "end_to_end",
                "latency_ms": 98_590,
            },
        ),
        kanban_db_path=str(database_path),
    )

    assert attributed.department == "trading-department"
    assert attributed.metadata["primary_bottleneck_duration_ms"] == 61_000
    assert attributed.metadata["joint_improvement_targets"] == "ceo-workflow / observability"
    assert attributed.metadata["observation_point"] == "ceo-ingress"
    assert attributed.metadata["latency_attribution_status"] == "MEASURED"


def test_semantic_answer_contract_is_redacted_and_evaluated() -> None:
    quality = evaluate_answer(
        "2026-08-24 기준 결론입니다. 근거는 t1234 이며 미확인 항목은 없음.",
    )
    assert quality.verdict == "PASS"
    assert quality.score == 1.0
    assert quality.as_metadata()["raw_payloads_sent"] is False

    observation = TraceObservation(
        source_run_id="run-semantic",
        name="hgfinance.user-query",
        status="completed",
        started_at=None,
        ended_at=None,
        metadata={
            "request_id": "req-1",
            "root_id": "root-1",
            "task_id": "root-1",
            "stage": "ceo-terminal",
            "status": "completed",
            **quality.as_metadata(),
        },
    )
    result = evaluate_observation(observation)
    assert result.decision == "OBSERVED_PASS"
    assert result.metadata["semantic_qa_score"] == 1.0
    assert result.metadata["semantic_qa_verdict"] == "PASS"


def test_semantic_failure_becomes_qa_review_signal_without_answer_text() -> None:
    quality = evaluate_answer("짧은 답")
    assert quality.verdict == "FAIL"
    assert "ANSWER_BODY_MISSING" in quality.finding_codes

    result = evaluate_observation(
        TraceObservation(
            source_run_id="run-semantic-fail",
            name="hgfinance.user-query",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "req-1",
                "root_id": "root-1",
                "task_id": "root-1",
                "stage": "ceo-terminal",
                "status": "completed",
                **quality.as_metadata(),
            },
        )
    )
    assert result.decision == "IMPROVEMENT_CANDIDATE"
    assert "SEMANTIC_QA_FAILED" in result.finding_codes
    assert "짧은 답" not in str(result.metadata)


def test_prompt_answer_relevance_is_local_and_bounded() -> None:
    quality = evaluate_prompt_answer(
        "삼성전자 2026년 2분기 실적과 근거를 알려줘",
        "삼성전자 2026년 2분기 실적은 t1234 근거로 확인되며 기준일은 2026-08-24입니다. 미확인 없음.",
    )

    assert quality.relevance == 1.0
    assert quality.verdict == "PASS"
    assert "semantic_qa_relevance" in quality.as_metadata()


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
    result = _actionable_result(
        "source-1", request_id="discord:actionable-1", department="risk-management"
    )
    artifact_id = ledger.complete("source-1", "eval-1", result)

    assert ledger.pending(10)[0]["artifact_id"] == artifact_id
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "qa-user",
        "reviewed",
        improvement_type="PROMPT_POLICY",
    ) is True
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "qa-user",
        "duplicate",
        improvement_type="PROMPT_POLICY",
    ) is False
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
    hint = ledger.approved_hints("risk", limit=3, max_chars=1200)
    assert hint is not None
    assert hint["items"][0]["department"] == "risk"
    assert hint["items"][0]["source"] == "qa-approved-langsmith-feedback"
    assert "prompt" not in str(hint)


def test_privacy_safe_runner_executes_registered_code_fix_suite(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    artifact_id = ledger.complete(
        "source-latency",
        "eval-latency",
        _actionable_result(
            "source-latency",
            request_id="request-latency",
            department="ceo-ingress",
        ),
    )
    # The helper finding is degradation; add latency through a normal observed
    # result so the registered attribution suite is selected.
    with ledger._connect() as db:
        db.execute(
            "UPDATE langsmith_feedback_artifacts SET finding_codes=? WHERE artifact_id=?",
            ('["LATENCY_ABOVE_THRESHOLD"]', artifact_id),
        )
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "discord:manager",
        "latency attribution fix",
        improvement_type="CODE_FIX",
    )
    assert run_pending_feedback_benchmarks(ledger) == {
        "passed": 1,
        "failed": 0,
        "skipped": 0,
    }
    candidate = ledger.approved_hints(None, limit=3, max_chars=1200)
    assert candidate is not None
    assert "LATENCY_ABOVE_THRESHOLD" in candidate["items"][0]["finding_codes"]


def test_pass_or_no_action_cannot_enter_approved_feedback(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    assert ledger.enqueue("source-pass", "First")
    assert ledger.claim() is not None
    pass_artifact = ledger.complete(
        "source-pass", "eval-pass", evaluate_observation(observation_from_run(_Run()))
    )
    assert not ledger.approve(
        pass_artifact,
        "APPROVED",
        "qa-user",
        "nothing to improve",
        improvement_type="PROMPT_POLICY",
    )

    assert ledger.enqueue("source-no-action", "First")
    assert ledger.claim() is not None
    actionable_artifact = ledger.complete(
        "source-no-action",
        "eval-no-action",
        _actionable_result("source-no-action", request_id="discord:no-action"),
    )
    assert not ledger.approve(
        actionable_artifact,
        "APPROVED",
        "qa-user",
        "no action classification",
    )


def test_ledger_merges_same_request_department_and_finding(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    common_metadata = {
        "request_id": "discord:semantic-one",
        "department": "ceo-ingress",
        "latency_scope": "end_to_end",
        "raw_payloads_sent": False,
    }
    first = evaluate_observation(
        TraceObservation(
            source_run_id="source-semantic-1",
            name="worker.ceo",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={**common_metadata, "latency_ms": 70_000},
        ),
        latency_warn_ms=60_000,
    )
    second = evaluate_observation(
        TraceObservation(
            source_run_id="source-semantic-2",
            name="worker.ceo",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={**common_metadata, "latency_ms": 80_000},
        ),
        latency_warn_ms=60_000,
    )
    for source in ("source-semantic-1", "source-semantic-2"):
        assert ledger.enqueue(source, "First") is True

    first_id = ledger.complete("source-semantic-1", "eval-1", first)
    second_id = ledger.complete("source-semantic-2", "eval-2", second)

    assert second_id == first_id
    with sqlite3.connect(ledger.path) as db:
        assert db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifacts"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifact_sources WHERE artifact_id=?",
            (first_id,),
        ).fetchone()[0] == 2
    assert ledger.claim_discord_delivery(first_id) is True
    assert ledger.claim_discord_delivery(second_id) is False


def test_concurrent_semantic_completions_merge_without_lock_failures(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))

    def complete(index: int) -> str:
        source = f"source-concurrent-{index}"
        assert ledger.enqueue(source, "First") is True
        result = evaluate_observation(
            TraceObservation(
                source_run_id=source,
                name="worker.ceo",
                status="completed",
                started_at=None,
                ended_at=None,
                metadata={
                    "request_id": "discord:concurrent-one",
                    "department": "ceo-ingress",
                    "latency_scope": "end_to_end",
                    "latency_ms": 70_000,
                    "raw_payloads_sent": False,
                },
            ),
            latency_warn_ms=60_000,
        )
        return ledger.complete(source, f"eval-{index}", result)

    with ThreadPoolExecutor(max_workers=8) as pool:
        artifact_ids = list(pool.map(complete, range(24)))

    assert len(set(artifact_ids)) == 1
    with sqlite3.connect(ledger.path) as db:
        assert db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifact_sources"
        ).fetchone()[0] == 24


def test_active_hint_is_local_only_and_requires_passed_benchmark(tmp_path, monkeypatch) -> None:
    path = tmp_path / "feedback.sqlite3"
    ledger = FeedbackLedger(str(path))
    assert ledger.enqueue("source-active", "First") is True
    assert ledger.claim() is not None
    artifact_id = ledger.complete(
        "source-active",
        "eval-active",
        _actionable_result("source-active", request_id="discord:active"),
    )
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "qa-user",
        "reviewed",
        improvement_type="PROMPT_POLICY",
    ) is True

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
    artifact_id = ledger.complete(
        "source-old",
        "eval-old",
        _actionable_result("source-old", request_id="discord:old"),
    )
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "qa-user",
        "reviewed",
        improvement_type="PROMPT_POLICY",
    ) is True

    with sqlite3.connect(path) as db:
        db.execute("UPDATE langsmith_feedback_artifacts SET created_at='2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE langsmith_feedback_jobs SET updated_at='2000-01-01T00:00:00+00:00'")

    assert ledger.cleanup(1) == 2
    assert ledger.pending(10) == []
    assert ledger.approved_hints(None, limit=3, max_chars=1200) is None


def test_unanswered_artifact_expires_without_becoming_rejected(tmp_path) -> None:
    path = tmp_path / "feedback.sqlite3"
    ledger = FeedbackLedger(str(path))
    ledger.enqueue("source-unanswered", "First")
    assert ledger.claim() is not None
    ledger.complete(
        "source-unanswered",
        "eval-unanswered",
        evaluate_observation(observation_from_run(_Run())),
    )

    assert len(ledger.pending(10)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM langsmith_feedback_decisions").fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='langsmith_feedback_reviews'"
        ).fetchone() == (0,)
        db.execute("UPDATE langsmith_feedback_artifacts SET created_at='2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE langsmith_feedback_jobs SET updated_at='2000-01-01T00:00:00+00:00'")

    assert ledger.cleanup(1) == 2
    assert ledger.pending(10) == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM langsmith_feedback_decisions").fetchone() == (0,)


def test_metric_windows_share_one_six_hour_incident_artifact(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    artifact_ids = []
    for number, window_start in enumerate(
        ("2026-08-26T00:00:00+00:00", "2026-08-26T00:05:00+00:00"),
        start=1,
    ):
        source_run = f"metrics-window-{number}"
        assert ledger.enqueue(source_run, "HgFinance-Metrics")
        assert ledger.claim() is not None
        result = evaluate_observation(
            TraceObservation(
                source_run_id=source_run,
                name="metrics.window",
                status="degraded",
                started_at=window_start,
                ended_at=window_start,
                metadata={
                    "source": "metrics-window",
                    "stage": "metrics-window",
                    "department": "metrics",
                    "status": "degraded",
                    "error_count": 1,
                    "window_start": window_start,
                    "raw_payloads_sent": False,
                },
            ),
            source_project="HgFinance-Metrics",
        )
        artifact_ids.append(ledger.complete(source_run, f"eval-metrics-{number}", result))

    assert len(set(artifact_ids)) == 1
    with sqlite3.connect(ledger.path) as db:
        assert db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifact_sources"
        ).fetchone()[0] == 2


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

    assert "LATENCY_ABOVE_THRESHOLD" in approved_feedback_section_from_root(body, "qa-department")
    assert approved_feedback_section_from_root(body, "risk-management") == ""


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
    class _Paginator:
        def __init__(self, rows):
            self.rows = rows

        def __aiter__(self):
            self._iterator = iter(self.rows)
            return self

        async def __anext__(self):
            try:
                return next(self._iterator)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class _Runs:
        def __init__(self, owner):
            self.owner = owner

        async def query(self, **kwargs):
            self.owner.query_calls.append(kwargs)
            if kwargs.get("project_ids") == ["project-First"]:
                return _Paginator([_Run()])
            return _Paginator([])

    class _Client:
        def __init__(self, **kwargs):
            self.read_called = False
            self.query_calls = []
            self.runs = _Runs(self)

        async def aread_project(self, *, project_name):
            return SimpleNamespace(id=f"project-{project_name}")

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
    root_call = fake_client.query_calls[0]
    assert root_call["project_ids"] == ["project-First"]
    assert "max_start_time" in root_call
    assert "min_start_time" in root_call
    assert "gt(end_time" in root_call["filter"]
    assert "page_size" in root_call


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
