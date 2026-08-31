from __future__ import annotations

from orchestration.langsmith_feedback import EvaluationResult, FeedbackLedger
from orchestration.qa_feedback_labeling import (
    LABEL_NO_ACTION,
    LABEL_REVIEW,
    adjudicate_redacted_artifact,
    label_sample,
)


def test_redacted_adjudication_does_not_treat_latency_as_skill_evolution() -> None:
    label, rationale = adjudicate_redacted_artifact(
        {
            "auto_decision": "REVIEW_WORTHY",
            "finding_codes": ["LATENCY_ABOVE_THRESHOLD"],
            "metadata": {
                "request_id": "request-1",
                "latency_ms": 70_000,
                "latency_threshold_ms": 60_000,
            },
        }
    )
    assert label == LABEL_REVIEW
    assert "지연" in rationale


def test_label_sample_is_separate_from_feedback_decisions(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    ledger.complete(
        "sample-pass",
        "eval-pass",
        EvaluationResult(
            source_run_id="sample-pass",
            department="research",
            workflow_role="worker",
            decision="OBSERVED_PASS",
            score=1.0,
            finding_codes=(),
            summaries=("pass",),
            metadata={"raw_payloads_sent": False},
        ),
    )
    ledger.complete(
        "sample-review",
        "eval-review",
        EvaluationResult(
            source_run_id="sample-review",
            department="research",
            workflow_role="worker",
            decision="REVIEW_WORTHY",
            score=0.2,
            finding_codes=("LATENCY_ABOVE_THRESHOLD",),
            summaries=("latency",),
            metadata={
                "request_id": "request-review",
                "latency_ms": 70_000,
                "latency_threshold_ms": 60_000,
                "raw_payloads_sent": False,
            },
        ),
    )

    result = label_sample(ledger, sample_size=30)
    assert result["sample_size"] == 2
    assert result["manual_labels"][LABEL_NO_ACTION] == 1
    assert result["manual_labels"][LABEL_REVIEW] == 1
    assert ledger.pending(10)
    assert ledger.manual_labels()
