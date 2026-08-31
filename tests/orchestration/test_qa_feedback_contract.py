from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from orchestration.langsmith_feedback import (
    EvaluationResult,
    FeedbackLedger,
    TraceObservation,
    evaluate_observation,
)
from orchestration.qa_discord_feedback import format_qa_feedback_request, parse_qa_feedback_command
from orchestration.qa_feedback_contract import (
    human_approver_user_id,
    is_human_approver,
    qa_approver_is_allowed,
)


def test_qa_approval_identity_is_human_and_allowlisted() -> None:
    actor = "discord:382384727245455360"
    assert human_approver_user_id(actor) == "382384727245455360"
    assert is_human_approver(actor)
    assert qa_approver_is_allowed(
        actor,
        env={
            "QA_APPROVER_ALLOWLIST_REQUIRED": "true",
            "QA_DISCORD_APPROVER_USER_IDS": "382384727245455360",
        },
    )
    assert not qa_approver_is_allowed(
        "codex:qa-bottleneck-review",
        env={
            "QA_APPROVER_ALLOWLIST_REQUIRED": "true",
            "QA_DISCORD_APPROVER_USER_IDS": "382384727245455360",
        },
    )
    assert not qa_approver_is_allowed(
        "discord:999999999999999999",
        env={
            "QA_APPROVER_ALLOWLIST_REQUIRED": "true",
            "QA_DISCORD_APPROVER_USER_IDS": "382384727245455360",
        },
    )


def _observation(*, source_run_id: str, ended_at: str | None = None) -> TraceObservation:
    return TraceObservation(
        source_run_id=source_run_id,
        name="worker.research",
        status="error",
        started_at=ended_at,
        ended_at=ended_at,
        metadata={
            "workflow_role": "primary",
            "stage": "research",
            "department": "research",
            "status": "degraded",
            "error_count": 1,
            "raw_payloads_sent": False,
        },
    )


def test_single_latency_signal_is_review_worthy_performance_event() -> None:
    result = evaluate_observation(
        TraceObservation(
            source_run_id="latency-1",
            name="worker.research",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "request-1",
                "stage": "research",
                "latency_ms": 70_000,
                "raw_payloads_sent": False,
            },
        ),
        latency_warn_ms=60_000,
    )

    assert result.decision == "REVIEW_WORTHY"
    assert result.metadata["review_class"] == "PERFORMANCE_EVENT"


def test_human_review_queue_excludes_passes_and_prioritizes_required_work(
    tmp_path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback-queue.sqlite3"))

    def complete(source: str, observation: TraceObservation) -> str:
        assert ledger.enqueue(source, "First")
        assert ledger.claim() is not None
        result = evaluate_observation(observation)
        return ledger.complete(source, f"eval-{source}", result)

    complete(
        "pass-first",
        TraceObservation(
            source_run_id="pass-first",
            name="worker.research",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "request-pass",
                "stage": "research",
                "raw_payloads_sent": False,
            },
        ),
    )
    quality_id = complete(
        "quality-second",
        TraceObservation(
            source_run_id="quality-second",
            name="worker.research",
            status="error",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "request-quality",
                "stage": "research",
                "status": "error",
                "error_count": 1,
                "raw_payloads_sent": False,
            },
        ),
    )
    required_id = complete(
        "required-third",
        TraceObservation(
            source_run_id="required-third",
            name="worker.research",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "request-required",
                "stage": "research",
                "raw_payloads_sent": True,
            },
        ),
    )

    pending = ledger.pending(10)
    assert all(item["decision"] != "OBSERVED_PASS" for item in pending)
    assert [item["artifact_id"] for item in pending[:2]] == [
        required_id,
        quality_id,
    ]
    assert pending[0]["decision"] == "REVIEW_REQUIRED"
    assert all(
        item["decision"] != "OBSERVED_PASS"
        for item in ledger.pending_discord_reviews(10)
    )


def test_discord_queue_does_not_starve_on_non_discord_d5_candidates(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "discord-queue.sqlite3"))

    for number in range(30):
        source = f"d5-{number}"
        assert ledger.enqueue(source, "First")
        assert ledger.claim() is not None
        result = EvaluationResult(
            source_run_id=source,
            department="qa",
            workflow_role="qa",
            decision="IMPROVEMENT_CANDIDATE",
            score=None,
            finding_codes=("D5_ROUTING_MISMATCH",),
            summaries=("verified D5 candidate",),
            metadata={
                "request_id": source,
                "stage": "qa",
                "raw_payloads_sent": False,
                "improvement_candidate": True,
                "candidate_type": "D5_ROUTING_MISMATCH",
            },
        )
        ledger.complete(source, f"eval-{source}", result)

    review_source = "discord-review"
    assert ledger.enqueue(review_source, "First")
    assert ledger.claim() is not None
    review_id = ledger.complete(
        review_source,
        "eval-discord-review",
        evaluate_observation(
            TraceObservation(
                source_run_id=review_source,
                name="worker.risk",
                status="error",
                started_at=None,
                ended_at=None,
                metadata={
                    "request_id": review_source,
                    "stage": "risk",
                    "status": "error",
                    "error_count": 1,
                    "raw_payloads_sent": False,
                },
            )
        ),
    )

    pending = ledger.pending_discord_reviews(10)
    assert pending
    assert pending[0]["artifact_id"] == review_id


def test_missing_correlation_is_aggregated_and_card_shows_sample_count(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    ended_at = datetime(2026, 8, 30, 12, 15, tzinfo=timezone.utc).isoformat()
    artifact_ids: list[str] = []

    for number in range(1, 4):
        source = f"uncorrelated-{number}"
        assert ledger.enqueue(source, "First")
        assert ledger.claim() is not None
        result = evaluate_observation(_observation(source_run_id=source, ended_at=ended_at))
        artifact_ids.append(ledger.complete(source, f"eval-{number}", result))

    assert len(set(artifact_ids)) == 1
    artifact = ledger.get_artifact(artifact_ids[0])
    assert artifact is not None
    assert artifact["decision"] == "REVIEW_WORTHY"
    assert artifact["metadata"]["sample_count"] == 3
    assert "CORRELATION_METADATA_MISSING" in artifact["finding_codes"]
    assert artifact["metadata"]["review_class"] == "QUALITY_OR_WORKFLOW_REVIEW"

    card = format_qa_feedback_request(
        artifact_id=artifact["artifact_id"],
        department=artifact["department"],
        decision=artifact["decision"],
        finding_codes=artifact["finding_codes"],
        summaries=artifact["summaries"],
        metadata=artifact["metadata"],
    )
    assert "동일 범주 집계 표본: 3건" in card
    assert "중복 카드 없음" in card


def test_uncorrelated_events_in_different_windows_do_not_merge(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    artifact_ids: list[str] = []
    for number, hour in enumerate((12, 14), start=1):
        source = f"uncorrelated-window-{number}"
        ended_at = datetime(2026, 8, 30, hour, 15, tzinfo=timezone.utc).isoformat()
        assert ledger.enqueue(source, "First")
        assert ledger.claim() is not None
        artifact_ids.append(
            ledger.complete(
                source,
                f"eval-window-{number}",
                evaluate_observation(_observation(source_run_id=source, ended_at=ended_at)),
            )
        )

    assert artifact_ids[0] != artifact_ids[1]


def test_closed_no_action_is_a_terminal_decision_not_approved_no_action(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    source = "close-no-action"
    assert ledger.enqueue(source, "First")
    assert ledger.claim() is not None
    artifact_id = ledger.complete(
        source,
        "eval-close-no-action",
        evaluate_observation(
            TraceObservation(
                source_run_id=source,
                name="worker.research",
                status="completed",
                started_at=None,
                ended_at=None,
                metadata={
                    "request_id": "request-close",
                    "stage": "research",
                    "raw_payloads_sent": False,
                },
            )
        ),
    )

    assert ledger.approve(
        artifact_id,
        "CLOSED_NO_ACTION",
        "discord:382384727245455360",
        "확인 결과 재현되지 않아 종료",
    )
    assert ledger.pending(10) == []
    with sqlite3.connect(ledger.path) as db:
        row = db.execute(
            "SELECT decision, improvement_type FROM langsmith_feedback_decisions WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
    assert row == ("CLOSED_NO_ACTION", "NO_ACTION")


def test_legacy_approved_no_action_rows_are_migrated_to_closed(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE langsmith_feedback_decisions (
                artifact_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL CHECK(decision IN ('APPROVED', 'REJECTED')),
                approved_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION',
                target_skill_slug TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO langsmith_feedback_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("feedback-legacy", "APPROVED", "qa:old", "확인", "NO_ACTION", None, "2026-08-29"),
        )

    ledger = FeedbackLedger(str(path))
    with sqlite3.connect(ledger.path) as db:
        row = db.execute(
            "SELECT decision, improvement_type FROM langsmith_feedback_decisions WHERE artifact_id=?",
            ("feedback-legacy",),
        ).fetchone()
    assert row == ("CLOSED_NO_ACTION", "NO_ACTION")


def test_legacy_automatic_candidates_are_migrated_to_review_worthy(tmp_path) -> None:
    path = tmp_path / "legacy-candidate.sqlite3"
    ledger = FeedbackLedger(str(path))
    with ledger._connect() as db:
        db.execute(
            """INSERT INTO langsmith_feedback_artifacts
            (artifact_id, source_run_id, eval_run_id, department, department_key,
             decision, score, finding_codes, summaries, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "feedback-automatic-candidate",
                "legacy-source",
                "legacy-eval",
                "research",
                "research",
                "IMPROVEMENT_CANDIDATE",
                None,
                '["LATENCY_ABOVE_THRESHOLD"]',
                '["slow"]',
                '{"source":"First"}',
                "2026-08-29T00:00:00+00:00",
            ),
        )

    FeedbackLedger(str(path))
    artifact = ledger.get_artifact("feedback-automatic-candidate")
    assert artifact is not None
    assert artifact["decision"] == "REVIEW_WORTHY"
    assert artifact["metadata"]["review_class"] == "PERFORMANCE_EVENT"


def test_legacy_uncorrelated_artifacts_are_merged_without_losing_sources(tmp_path) -> None:
    path = tmp_path / "legacy-duplicates.sqlite3"
    ledger = FeedbackLedger(str(path))
    with ledger._connect() as db:
        for number in range(1, 4):
            artifact_id = f"feedback-legacy-duplicate-{number}"
            source_id = f"legacy-duplicate-source-{number}"
            db.execute(
                """INSERT INTO langsmith_feedback_artifacts
                (artifact_id, source_run_id, eval_run_id, department, department_key,
                 decision, score, finding_codes, summaries, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    source_id,
                    f"eval-{number}",
                    "research",
                    "research",
                    "REVIEW_WORTHY",
                    None,
                    '["CORRELATION_METADATA_MISSING"]',
                    '["correlation missing"]',
                    '{"source_project":"First","workflow_role":"primary"}',
                    f"2026-08-29T00:0{number}:00+00:00",
                ),
            )
            db.execute(
                "INSERT INTO langsmith_feedback_artifact_sources VALUES (?, ?, ?)",
                (source_id, artifact_id, f"2026-08-29T00:0{number}:00+00:00"),
            )

    FeedbackLedger(str(path))
    with ledger._connect() as db:
        count = db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifacts WHERE finding_codes=?",
            ('["CORRELATION_METADATA_MISSING"]',),
        ).fetchone()[0]
        source_count = db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifact_sources WHERE artifact_id=?",
            ("feedback-legacy-duplicate-1",),
        ).fetchone()[0]
    assert count == 1
    assert source_count == 3


def test_discord_close_command_maps_to_terminal_no_action() -> None:
    command = parse_qa_feedback_command("종료 feedback-0123456789abcdef0123456789abcdef 재현되지 않음")

    assert command is not None
    assert command.decision == "CLOSED_NO_ACTION"
    assert command.improvement_type is None
