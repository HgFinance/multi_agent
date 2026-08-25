"""Governed Evolution Skills lifecycle tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.evolution_skills import (
    PRODUCTION_GENERATION_MODEL,
    EvolutionSkillError,
    EvolutionSkillStore,
    Occurrence,
    active_registry_bindings,
    detect_candidates,
    inventory_skills,
    promote_proposal,
    record_trace_occurrences,
    retire_skill,
    validate_canonical_registry,
)


def _body(slug: str) -> str:
    return (
        f"# {slug}\n\n"
        "## 왜 필요한가\n서로 다른 실행에서 반복된 실패를 재현한다.\n\n"
        "## 작업 순서\n원인 로그를 확인하고 정본 검증 명령을 실행한다.\n\n"
        "## 하지 않을 것\n관측하지 않은 결과나 우회 절차를 만들지 않는다.\n"
    )


def _metadata(model: str = PRODUCTION_GENERATION_MODEL) -> dict[str, object]:
    return {
        "model_version": model,
        "base_model": model,
        "adapter_id": None,
    }


def _candidate(*, first_run: int = 1, active_version: int | None = None):
    rows = [
        Occurrence(
            kind="repeated quote timeout",
            detail=f"timeout in run {number}",
            run_id=f"run-{number}",
            department="01-research",
        )
        for number in range(first_run, first_run + 3)
    ]
    return detect_candidates(
        rows,
        department="01-research",
        active_versions={"repeated-quote-timeout": active_version} if active_version else None,
    )[0]


def _approved_proposal(store: EvolutionSkillStore, *, candidate=None) -> dict:
    candidate = candidate or _candidate()
    state = store.create_proposal(
        candidate,
        lambda _prompt: _body(candidate.slug),
        model_metadata=_metadata(),
    )
    assert state["status"] == "VALIDATED"
    return store.approve(
        state["proposal_id"],
        approved_by="qa-owner@example.com",
        qa_verdict="PASS",
    )


def test_occurrences_are_persistent_and_deduplicated_by_run(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path / "state")
    row = Occurrence(
        kind="tool timeout",
        detail="first",
        run_id="run-1",
        department="01-research",
    )

    assert store.append_occurrences([row, row]) == 1
    assert EvolutionSkillStore(tmp_path / "state").load_occurrences() == store.load_occurrences()
    assert len(store.load_occurrences()) == 1


def test_candidate_requires_three_distinct_unconsumed_runs() -> None:
    duplicate_runs = [
        Occurrence(kind="tool timeout", run_id="same", department="01-research")
        for _ in range(4)
    ]
    assert detect_candidates(duplicate_runs, department="01-research") == []

    candidate = _candidate(active_version=1)
    assert candidate.version == 2
    assert candidate.parent_version == 1
    assert len(candidate.runs) == 3

    assert (
        detect_candidates(
            [
                Occurrence(kind="repeated quote timeout", run_id=f"run-{number}")
                for number in range(1, 4)
            ],
            department="01-research",
            consumed_runs={"repeated-quote-timeout": {"run-1", "run-2", "run-3"}},
        )
        == []
    )


def test_generation_requires_governed_14b_and_deterministic_structure(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path)
    candidate = _candidate()

    with pytest.raises(EvolutionSkillError, match="14B"):
        store.create_proposal(
            candidate,
            lambda _prompt: _body(candidate.slug),
            model_metadata=_metadata("qwen2.5-8b-instruct"),
        )
    assert store.candidates_path.is_file()
    assert json.loads(store.candidates_path.read_text(encoding="utf-8").splitlines()[0])["status"] == "CANDIDATE"

    state = store.create_proposal(
        candidate,
        lambda _prompt: (
            "# incomplete\n\n설명만 길게 적었지만 필수 절과 정본 제목이 빠져 있어 "
            "결정론적 구조 검사를 통과하면 안 되는 제안서다. 이 문장은 최소 길이만 충족한다."
        ),
        model_metadata=_metadata(),
    )
    assert state["status"] == "PROPOSED"
    assert state["validation"]["ok"] is False


def test_approval_requires_validation_qa_pass_and_named_approver(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path)
    candidate = _candidate()
    state = store.create_proposal(
        candidate,
        lambda _prompt: _body(candidate.slug),
        model_metadata=_metadata(),
    )

    rejected = store.approve(state["proposal_id"], approved_by="qa", qa_verdict="FAIL")
    assert rejected["status"] == "REJECTED"

    second_store = EvolutionSkillStore(tmp_path / "second")
    second_state = second_store.create_proposal(
        candidate,
        lambda _prompt: _body(candidate.slug),
        model_metadata=_metadata(),
    )
    with pytest.raises(EvolutionSkillError, match="review requires"):
        second_store.approve(second_state["proposal_id"], approved_by="", qa_verdict="PASS")

    approved = second_store.approve(
        second_state["proposal_id"], approved_by="qa", qa_verdict="PASS"
    )
    assert approved["status"] == "APPROVED"


def test_promotion_registers_and_activates_without_runtime_writes(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    repo = tmp_path / "repo"
    registry = repo / "skills/evolution-registry.json"
    store = EvolutionSkillStore(state_root)
    approved = _approved_proposal(store)

    active_state = promote_proposal(
        store,
        approved["proposal_id"],
        repository_root=repo,
        registry_path=registry,
    )

    source = repo / "skills/evolved/repeated-quote-timeout/SKILL.md"
    provenance = source.with_name("provenance.json")
    assert active_state["status"] == "ACTIVE"
    assert active_state["regression_validation"]["ok"] is True
    assert source.is_file() and provenance.is_file()
    assert json.loads(provenance.read_text(encoding="utf-8"))["approved_by"] == "qa-owner@example.com"
    active, owners = active_registry_bindings(registry)
    assert active == {"repeated-quote-timeout"}
    assert owners["repeated-quote-timeout"] == {"research-department"}


def test_tampering_after_validation_blocks_promotion(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path / "state")
    approved = _approved_proposal(store)
    proposal_dir = store.proposal_dir(approved["proposal_id"])
    (proposal_dir / "SKILL.md").write_text(
        (proposal_dir / "SKILL.md").read_text(encoding="utf-8") + "\n변조\n",
        encoding="utf-8",
    )

    with pytest.raises(EvolutionSkillError, match="changed after validation"):
        promote_proposal(store, approved["proposal_id"], repository_root=tmp_path / "repo")


def test_project_owned_slug_cannot_be_overwritten(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path / "state")
    approved = _approved_proposal(store)
    repo = tmp_path / "repo"
    existing = repo / "skills/manual/repeated-quote-timeout"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("# protected\n", encoding="utf-8")

    with pytest.raises(EvolutionSkillError, match="collides"):
        promote_proposal(store, approved["proposal_id"], repository_root=repo)


def test_evolution_supersedes_old_version_and_retirement_preserves_source(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path / "state")
    repo = tmp_path / "repo"
    registry = repo / "skills/evolution-registry.json"

    first = _approved_proposal(store)
    promote_proposal(store, first["proposal_id"], repository_root=repo, registry_path=registry)

    second_candidate = _candidate(first_run=4, active_version=1)
    second = _approved_proposal(store, candidate=second_candidate)
    promote_proposal(store, second["proposal_id"], repository_root=repo, registry_path=registry)

    _, first_state = store.load_proposal(first["proposal_id"])
    _, second_state = store.load_proposal(second["proposal_id"])
    entry = json.loads(registry.read_text(encoding="utf-8"))["skills"]["repeated-quote-timeout"]
    assert first_state["status"] == "SUPERSEDED"
    assert second_state["status"] == "ACTIVE"
    assert entry["current_version"] == 2

    with pytest.raises(EvolutionSkillError, match="registered owner"):
        retire_skill(
            store,
            "repeated-quote-timeout",
            repository_root=repo,
            registry_path=registry,
            approved_by="wrong-owner",
            owner_profile="quant-backtest-department",
            owner_approved_no_replacement=True,
        )

    with pytest.raises(EvolutionSkillError, match="replacement"):
        retire_skill(
            store,
            "repeated-quote-timeout",
            repository_root=repo,
            registry_path=registry,
            approved_by="owner",
            owner_profile="research-department",
        )

    retired = retire_skill(
        store,
        "repeated-quote-timeout",
        repository_root=repo,
        registry_path=registry,
        approved_by="research-owner",
        owner_profile="research-department",
        owner_approved_no_replacement=True,
    )
    assert retired["status"] == "retired"
    assert (repo / "skills/evolved/repeated-quote-timeout/SKILL.md").is_file()
    assert active_registry_bindings(registry)[0] == frozenset()
    assert store.load_proposal(second["proposal_id"])[1]["status"] == "RETIRED"


def test_registry_regression_check_detects_canonical_drift(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path / "state")
    repo = tmp_path / "repo"
    registry = repo / "skills/evolution-registry.json"
    approved = _approved_proposal(store)
    promote_proposal(store, approved["proposal_id"], repository_root=repo, registry_path=registry)
    source = repo / "skills/evolved/repeated-quote-timeout/SKILL.md"
    source.write_text(source.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")

    result = validate_canonical_registry(repo, registry)
    assert result["ok"] is False
    assert any("content hash mismatch" in error for error in result["errors"])


def test_runtime_contract_activates_and_retires_without_module_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orchestration.skill_contract as contract

    store = EvolutionSkillStore(tmp_path / "state")
    repo = tmp_path / "repo"
    registry = repo / "skills/evolution-registry.json"
    monkeypatch.setattr(contract, "EVOLUTION_SKILL_REGISTRY", registry)
    approved = _approved_proposal(store)
    promote_proposal(store, approved["proposal_id"], repository_root=repo, registry_path=registry)

    assert contract.validate_skill_for_profile(
        "repeated-quote-timeout",
        "research-department",
        root=repo / "skills",
    ) == "repeated-quote-timeout"

    retire_skill(
        store,
        "repeated-quote-timeout",
        repository_root=repo,
        registry_path=registry,
        approved_by="owner",
        owner_profile="research-department",
        owner_approved_no_replacement=True,
    )
    with pytest.raises(contract.CanonicalSkillError, match="not active"):
        contract.resolve_canonical_skill("repeated-quote-timeout", root=repo / "skills")


def test_usage_count_cannot_drive_retirement() -> None:
    # The governed API intentionally has no usage-count argument or delete operation.
    import inspect

    signature = inspect.signature(retire_skill)
    assert "usage_count" not in signature.parameters
    assert "delete" not in {name.lower() for name in dir(EvolutionSkillStore)}


def test_trace_findings_feed_only_owned_departments(tmp_path: Path) -> None:
    store = EvolutionSkillStore(tmp_path)
    assert record_trace_occurrences(
        store,
        department="research",
        run_id="trace-1",
        finding_codes=("high_latency", "high_latency"),
        detail="redacted deterministic summary",
    ) == 1
    assert record_trace_occurrences(
        store,
        department="trading",
        run_id="trace-2",
        finding_codes=("order_failure",),
    ) == 0
    row = store.load_occurrences()[0]
    assert row["kind"] == "trace-high-latency"
    assert row["department"] == "01-research"


def test_inventory_distinguishes_sources_and_never_authorizes_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    project = repo / "skills/project-procedure"
    bundled_root = tmp_path / "runtime/skills"
    bundled = bundled_root / "vendor-procedure"
    custom = bundled_root / "operator-procedure"
    for directory in (project, bundled, custom):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("# source\n", encoding="utf-8")
    (bundled_root / ".bundled_manifest").write_text(
        "vendor-procedure:abc123\n", encoding="utf-8"
    )

    report = inventory_skills(
        [repo / "skills", bundled_root],
        repository_root=repo,
    )
    classifications = {
        (entry["name"], entry["classification"]) for entry in report["entries"]
    }
    assert ("project-procedure", "project-owned") in classifications
    assert ("vendor-procedure", "bundled") in classifications
    assert ("operator-procedure", "legacy-custom") in classifications
    assert {entry["removal_action"] for entry in report["entries"]} == {"preserve"}
