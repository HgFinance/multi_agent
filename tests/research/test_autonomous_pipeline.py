from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

from director import ResearchDirector
from hermes_agent import HermesResearchAgent
from lab import ResearchLab
from models import ExperimentResult, Objective
from result import decision_for, parse_result
import runner
import artifact_validator
import hermes_agent


def _result(plan_id: str, **overrides: object) -> ExperimentResult:
    values = {
        "plan_id": plan_id,
        "status": "COMPLETED",
        "cost_included": True,
        "oos_evaluated": True,
        "leakage_detected": False,
        "robustness": {"cost_stress": True, "regime": True},
        "metrics": {"oos_return": 0.12},
        "artifacts": ("experiments/example.json",),
    }
    values.update(overrides)
    result = ExperimentResult(**values)
    result.validate()
    return result


def test_lab_initializes_human_and_machine_state(tmp_path: Path) -> None:
    lab = ResearchLab(tmp_path / "lab")
    lab.initialize(Objective(goal="Find a reproducible signal", universe="stocks"))

    assert (tmp_path / "lab" / "objective.json").exists()
    assert (tmp_path / "lab" / "events.jsonl").exists() is False
    for name in ("OBJECTIVE.md", "STATE.md", "KNOWLEDGE.md", "EXPERIMENT_LOG.md", "FAILURE_MEMORY.md", "RESOURCE_MAP.md"):
        assert (tmp_path / "lab" / name).exists()


def test_result_gates_reject_incomplete_evidence() -> None:
    with pytest.raises(ValueError, match="robustness"):
        parse_result({
            "plan_id": "plan-1",
            "status": "COMPLETED",
            "cost_included": True,
            "oos_evaluated": True,
            "leakage_detected": False,
            "robustness": {},
            "metrics": {"return": 1.0},
        })

    assert decision_for(_result("plan-1", leakage_detected=True))[0] == "REJECT"
    assert decision_for(_result("plan-1", cost_included=False))[0] == "PAUSE"
    assert decision_for(_result("plan-1", oos_evaluated=False))[0] == "PAUSE"
    assert decision_for(_result("plan-1", robustness={"regime": False}))[0] == "PIVOT"
    assert decision_for(_result("plan-1"))[0] == "CANDIDATE"


def test_result_contract_rejects_non_finite_and_non_boolean_values() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        parse_result({
            "plan_id": "plan-1",
            "status": "COMPLETED",
            "cost_included": True,
            "oos_evaluated": True,
            "leakage_detected": False,
            "robustness": {"regime": True},
            "metrics": {"return": float("nan")},
        })

    with pytest.raises(ValueError, match="boolean"):
        parse_result({
            "plan_id": "plan-1",
            "status": "COMPLETED",
            "cost_included": True,
            "oos_evaluated": True,
            "leakage_detected": False,
            "robustness": {"regime": "passed"},
            "metrics": {"return": 1.0},
        })


def test_director_pivots_after_repeated_signature() -> None:
    objective = Objective(goal="Test", universe="stocks")
    plans = [
        {"plan_id": f"plan-{index}", "signature": {"representation": "event-study"}}
        for index in range(3)
    ]
    director = ResearchDirector(objective, [], plans, [_result("plan-0"), _result("plan-1"), _result("plan-2")])
    assert director.next_action() == "PIVOT"


def test_hermes_missing_binary_is_recordable_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_RESEARCH_HERMES_BIN", "/definitely/missing/hermes")
    lab_root = tmp_path / "lab"
    agent = HermesResearchAgent(repo_root=tmp_path, lab_root=lab_root)
    run = agent.run({"plan_id": "plan-1"})

    assert run.status == "FAILED"
    assert Path(run.output_path).read_text(encoding="utf-8").startswith("Hermes binary not found")


def test_strategy_hermes_runs_directly_with_codex_high_and_no_plan_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        temporary_root = Path(kwargs["env"]["STRATEGY_MARKET_DATA_DIR"])
        temporary_root.mkdir(parents=True, exist_ok=True)
        (temporary_root / "raw.json").write_text("temporary", encoding="utf-8")
        return type("Completed", (), {"stdout": "ok", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(hermes_agent.subprocess, "run", fake_run)
    agent = hermes_agent.StrategyHermesAgent(repo_root=tmp_path, lab_root=tmp_path / "lab")

    run = agent.run({"plan_id": "ignored-compatibility-input"})
    command = captured["command"]
    assert run.status == "COMPLETED"
    assert isinstance(command, list)
    assert "--provider" in command and command[command.index("--provider") + 1] == "openai-codex"
    assert "--model" in command and command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert "--reasoning" in command and command[command.index("--reasoning") + 1] == "high"
    assert "--in" in command and command[command.index("--in") + 1] == str((tmp_path / "lab").resolve())
    assert "quant-hermes" in command[-1]
    assert "research-hermes" in command[-1]
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["QUANT_WORKSPACE"] == str((tmp_path / "lab" / "experiments").resolve())
    temporary_root = Path(environment["STRATEGY_MARKET_DATA_DIR"])
    assert str(temporary_root).startswith("/tmp/")
    assert not temporary_root.exists()
    assert environment["LS_DATA_ACCESS_MODE"] == "readonly"
    assert environment["LS_ALLOWED_TR_CODES"].split(",") == [
        "t1665", "t8410", "t8411", "t8412", "t8451", "t8452", "t8453",
        "t1441", "t1444", "t1452", "t1463", "t1466", "t1481", "t1482",
        "t1489", "t1492",
    ]


def test_artifact_validator_accepts_hermes_compact_plan_shapes(tmp_path: Path) -> None:
    lab = ResearchLab(tmp_path / "lab")
    lab.initialize(Objective(goal="Test direct Hermes artifacts", universe="stocks"))
    (lab.hypotheses_dir / "h1.json").write_text(json.dumps({
        "hypothesis_id": "h1",
        "statement": "A testable effect exists.",
        "mechanism": "A measurable mechanism.",
        "expected_behavior": "It survives costs.",
        "falsifiers": ["No effect"],
        "dimensions": ["costs", "regimes"],
    }), encoding="utf-8")
    (lab.plans_dir / "p1.json").write_text(json.dumps({
        "plan_id": "p1",
        "hypothesis_id": "h1",
        "objective": "Measure the effect.",
        "method": {"primary": "backtest"},
        "data_requirements": {"source": "local"},
        "splits": {"development": "historical"},
        "cost_model": {"fees_bps": 5},
        "seed": 7,
        "signature": "compact-signature-v1",
        "preregistration_hash": "hash-p1",
        "status": "PREREGISTERED",
    }), encoding="utf-8")
    (lab.results_dir / "p1.json").write_text(json.dumps({
        "plan_id": "p1",
        "preregistration_hash": "hash-p1",
        "status": "BLOCKED",
        "cost_included": True,
        "oos_evaluated": False,
        "leakage_detected": False,
        "robustness": {"data_integrity": True},
        "metrics": {},
        "failure_reason": "No suitable data.",
    }), encoding="utf-8")

    decisions = artifact_validator.sync_agent_artifacts(lab)

    assert decisions[0]["decision"] == "PAUSE"
    assert lab.plans()[0]["signature"] == {"signature": "compact-signature-v1"}


def test_runner_requires_registered_plan_and_publishes_candidate(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    init_args = runner.build_parser().parse_args([
        "init", "--lab-root", str(lab_root), "--repo-root", str(tmp_path), "--goal", "Test objective",
    ])
    runner.init_lab(init_args)
    cycle_args = runner.build_parser().parse_args([
        "cycle", "--lab-root", str(lab_root), "--repo-root", str(tmp_path), "--max-agent-attempts", "1",
    ])
    cycle = runner.run_cycle(cycle_args)
    plan_id = cycle["plan_id"]
    lab = ResearchLab(lab_root)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"plan_id": "not-registered"}), encoding="utf-8")
    with pytest.raises(ValueError, match="registered experiment plan"):
        runner.ingest_result(lab, unknown)

    result_path = lab_root / "results" / f"{plan_id}.json"
    result_path.write_text(json.dumps({
        "plan_id": plan_id,
        "preregistration_hash": lab.plans()[0]["preregistration_hash"],
        "status": "COMPLETED",
        "cost_included": True,
        "oos_evaluated": True,
        "leakage_detected": False,
        "robustness": {"cost_stress": True, "regime": True},
        "metrics": {"oos_return": 0.1},
        "artifacts": ["experiments/test.json"],
    }), encoding="utf-8")
    ingested = runner.ingest_result(lab, result_path)

    assert ingested["decision"] == "CANDIDATE"
    assert (lab_root / "candidate.json").exists()
    assert lab.state()["active_plan_id"] is None


def test_runner_rejects_result_for_mutated_registration(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    init_args = runner.build_parser().parse_args([
        "init", "--lab-root", str(lab_root), "--repo-root", str(tmp_path), "--goal", "Test objective",
    ])
    runner.init_lab(init_args)
    cycle_args = runner.build_parser().parse_args([
        "cycle", "--lab-root", str(lab_root), "--repo-root", str(tmp_path),
    ])
    cycle = runner.run_cycle(cycle_args)
    lab = ResearchLab(lab_root)
    result_path = lab_root / "results" / f"{cycle['plan_id']}.json"
    result_path.write_text(json.dumps({
        "plan_id": cycle["plan_id"],
        "preregistration_hash": "mutated-registration",
        "status": "COMPLETED",
        "cost_included": True,
        "oos_evaluated": True,
        "leakage_detected": False,
        "robustness": {"regime": True},
        "metrics": {"oos_return": 0.1},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="preregistration_hash"):
        runner.ingest_result(lab, result_path)


def test_runner_agent_cycle_records_adapter_failure_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTONOMOUS_RESEARCH_HERMES_BIN", "/definitely/missing/hermes")
    lab_root = tmp_path / "lab"
    init_args = runner.build_parser().parse_args([
        "init", "--lab-root", str(lab_root), "--repo-root", str(tmp_path), "--goal", "Test objective",
    ])
    runner.init_lab(init_args)
    cycle_args = runner.build_parser().parse_args([
        "cycle", "--agent", "--lab-root", str(lab_root), "--repo-root", str(tmp_path),
        "--max-agent-attempts", "1",
    ])

    cycle = runner.run_cycle(cycle_args)

    assert cycle["status"] == "CYCLE_COMPLETED"
    assert cycle["agent"]["status"] == "FAILED"
    assert cycle["result_available"] is False


def test_worker_materializes_a_request_and_advances_its_lab_once(tmp_path: Path) -> None:
    lab_root = tmp_path / "research"
    from autonomous_research_ingress import ResearchIntake

    ResearchIntake(lab_root).submit({
        "request_id": "worker-01",
        "goal": "Find a robust short-horizon strategy",
        "universe": "historical test data",
        "horizon": "1-5 days",
        "constraints": ["No live orders"],
        "actor_id": "user-a",
        "source": "test",
    })

    args = runner.build_parser().parse_args([
        "worker", "--lab-root", str(lab_root), "--repo-root", str(tmp_path),
    ])
    snapshot = runner.run_worker(args)
    status = ResearchIntake(lab_root).status("worker-01")

    assert snapshot["status"] == "WORKER_CYCLE_COMPLETED"
    assert status is not None
    assert status["status"] == "RESEARCHING"
    assert status["cycle"] == 1
    assert status["plan_count"] == 1
    assert not (lab_root / "intake" / "worker-01.json").exists()
