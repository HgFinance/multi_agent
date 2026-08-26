"""QA approval and benchmark evidence enters Evolution through one bridge."""

from __future__ import annotations

from pathlib import Path

from orchestration.evolution_skills import (
    PRODUCTION_GENERATION_MODEL,
    EvolutionSkillStore,
    Occurrence,
    build_resolution_report,
    detect_candidates,
    promote_proposal,
)
from orchestration.langsmith_feedback import (
    FeedbackLedger,
    TraceObservation,
    evaluate_observation,
)
from orchestration.qa_skill_evolution_bridge import process_qa_skill_feedback


def _artifact(ledger: FeedbackLedger, number: int) -> str:
    source_run = f"risk-run-{number}"
    assert ledger.enqueue(source_run, "First")
    assert ledger.claim() is not None
    result = evaluate_observation(
        TraceObservation(
            source_run_id=source_run,
            name="worker.risk",
            status="error",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": f"risk-request-{number}",
                "stage": "risk",
                "status": "DEGRADED",
                "error_count": 1,
                "raw_payloads_sent": False,
            },
        )
    )
    return ledger.complete(source_run, f"eval-{number}", result)


def _occurrences(store: EvolutionSkillStore) -> list[Occurrence]:
    fields = Occurrence.__dataclass_fields__
    return [
        Occurrence(**{key: value for key, value in row.items() if key in fields})
        for row in store.load_occurrences()
    ]


def test_only_first_approved_and_benchmark_passed_skill_findings_are_admitted(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    artifacts = [_artifact(ledger, number) for number in range(1, 4)]

    assert process_qa_skill_feedback(ledger, store)["occurrences_written"] == 0
    for artifact_id in artifacts:
        assert ledger.approve(
            artifact_id,
            "APPROVED",
            "discord:manager",
            "repeatable risk failure",
            improvement_type="SKILL_CREATE",
        )

    result = process_qa_skill_feedback(ledger, store)
    assert result == {
        "benchmark_passed": 3,
        "benchmark_failed": 0,
        "occurrences_written": 3,
    }
    assert process_qa_skill_feedback(ledger, store)["occurrences_written"] == 0

    candidates = detect_candidates(_occurrences(store), department="03-risk")
    assert len(candidates) == 1
    assert candidates[0].improvement_type == "SKILL_CREATE"
    assert set(candidates[0].source_artifact_ids) == set(artifacts)
    assert candidates[0].benchmark_ids == ("qa-evolution-admission-v1",)


def test_non_skill_classification_never_enters_evolution(tmp_path: Path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    artifact_id = _artifact(ledger, 1)
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "discord:manager",
        "implementation defect",
        improvement_type="CODE_FIX",
    )

    assert process_qa_skill_feedback(ledger, store) == {
        "benchmark_passed": 0,
        "benchmark_failed": 0,
        "occurrences_written": 0,
    }


def test_duplicate_traces_for_one_request_count_as_one_evidence(tmp_path: Path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    artifact_ids = []
    for number in range(1, 4):
        source_run = f"duplicate-risk-run-{number}"
        assert ledger.enqueue(source_run, "First")
        assert ledger.claim() is not None
        result = evaluate_observation(
            TraceObservation(
                source_run_id=source_run,
                name="worker.risk",
                status="error",
                started_at=None,
                ended_at=None,
                metadata={
                    "request_id": "same-user-request",
                    "stage": "risk",
                    "status": "DEGRADED",
                    "error_count": 1,
                    "raw_payloads_sent": False,
                },
            )
        )
        artifact_ids.append(ledger.complete(source_run, f"eval-{number}", result))

    assert len(set(artifact_ids)) == 1
    assert ledger.approve(
        artifact_ids[0],
        "APPROVED",
        "discord:manager",
        "one request emitted duplicate traces",
        improvement_type="SKILL_CREATE",
    )
    result = process_qa_skill_feedback(ledger, store)
    assert result["benchmark_passed"] == 1
    assert result["occurrences_written"] == 1
    assert len(_occurrences(store)) == 1
    assert detect_candidates(_occurrences(store), department="03-risk") == []


def test_unattributed_end_to_end_latency_cannot_become_skill_evidence(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    source_run = "root-latency-run"
    assert ledger.enqueue(source_run, "First")
    assert ledger.claim() is not None
    result = evaluate_observation(
        TraceObservation(
            source_run_id=source_run,
            name="workflow.ceo",
            status="completed",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "slow-root-request",
                "department": "ceo-ingress",
                "latency_scope": "end_to_end",
                "latency_ms": 70_000,
                "raw_payloads_sent": False,
            },
        ),
        latency_warn_ms=60_000,
    )
    artifact_id = ledger.complete(source_run, "eval-root-latency", result)
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "discord:manager",
        "slow root request",
        improvement_type="SKILL_CREATE",
    )

    assert process_qa_skill_feedback(ledger, store) == {
        "benchmark_passed": 0,
        "benchmark_failed": 1,
        "occurrences_written": 0,
    }


def test_unattributed_root_failure_cannot_become_department_skill(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    source_run = "unattributed-root-failure"
    assert ledger.enqueue(source_run, "First")
    assert ledger.claim() is not None
    result = evaluate_observation(
        TraceObservation(
            source_run_id=source_run,
            name="hgfinance.user-query",
            status="error",
            started_at=None,
            ended_at=None,
            metadata={
                "request_id": "failed-root-request",
                "department": "ceo-workflow",
                "trace_kind": "workflow_root",
                "latency_scope": "end_to_end",
                "status": "error",
                "raw_payloads_sent": False,
            },
        )
    )
    artifact_id = ledger.complete(source_run, "eval-root-failure", result)
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "discord:manager",
        "root failed without an attributable owner",
        improvement_type="SKILL_CREATE",
    )

    assert process_qa_skill_feedback(ledger, store) == {
        "benchmark_passed": 0,
        "benchmark_failed": 1,
        "occurrences_written": 0,
    }


def test_approved_qa_evidence_reaches_canonical_skill_activation(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    store = EvolutionSkillStore(tmp_path / "evolution")
    artifacts = [_artifact(ledger, number) for number in range(1, 4)]
    for artifact_id in artifacts:
        assert ledger.approve(
            artifact_id,
            "APPROVED",
            "discord:first-reviewer",
            "independent risk failures",
            improvement_type="SKILL_CREATE",
        )
    assert process_qa_skill_feedback(ledger, store)["occurrences_written"] == 3

    candidate = detect_candidates(_occurrences(store), department="03-risk")[0]
    state = store.create_proposal(
        candidate,
        lambda _prompt: (
            f"# {candidate.slug}\n\n"
            "## 왜 필요한가\n독립 실행에서 반복된 리스크 작업 실패를 점검한다.\n\n"
            "## 작업 순서\n근거 ID를 확인하고 실패 원인을 재현한 뒤 검증한다.\n\n"
            "## 하지 않을 것\n근거 없는 성공 판정이나 정책 우회를 하지 않는다.\n"
        ),
        model_metadata={
            "model_version": PRODUCTION_GENERATION_MODEL,
            "base_model": PRODUCTION_GENERATION_MODEL,
            "adapter_id": None,
        },
    )
    approved = store.approve(
        state["proposal_id"],
        approved_by="discord:second-reviewer",
        qa_verdict="PASS",
        reason="exact hashes and validation reviewed",
    )
    assert approved["status"] == "APPROVED"

    repository_root = tmp_path / "repository"
    registry_path = repository_root / "skills/evolution-registry.json"
    active = promote_proposal(
        store,
        state["proposal_id"],
        repository_root=repository_root,
        registry_path=registry_path,
    )
    assert active["status"] == "ACTIVE"
    assert (repository_root / "skills/evolved" / candidate.slug / "SKILL.md").is_file()
    report = build_resolution_report(store, state["proposal_id"])
    assert report["outcome_evidence"]["status"] == "ACTIVE_PENDING_FEEDBACK"
    assert set(report["problem_evidence"]["source_artifact_ids"]) == set(artifacts)
