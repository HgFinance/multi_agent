"""Direct Strategy Hermes execution for one persistent research lab.

The process in this module is deliberately not a research planner. Hermes
owns the research turn: it reads the lab, chooses the next hypothesis and
plan, writes experiment code, runs the backtest, and records the result. The
caller only supplies the isolated lab boundary and records process telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import subprocess
import time
from typing import Mapping


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    plan_id: str | None
    status: str
    returncode: int | None
    output_path: str
    error: str | None = None
    duration_seconds: float = 0.0
    usage_path: str | None = None


class StrategyHermesAgent:
    """Run Hermes directly in one isolated strategy-research lab.

    ``plan`` is accepted only as a compatibility argument for old unit
    callers. The production strategy supervisor never supplies one: Hermes
    must decide and preregister its own next plan from the lab state.
    """

    def __init__(self, *, repo_root: Path, lab_root: Path, timeout_seconds: int = 1800) -> None:
        self.repo_root = repo_root.resolve()
        self.lab_root = lab_root.resolve()
        self.timeout_seconds = max(30, int(timeout_seconds))
        self.binary = os.getenv("AUTONOMOUS_RESEARCH_HERMES_BIN", "hermes")
        self.provider = os.getenv("AUTONOMOUS_RESEARCH_HERMES_PROVIDER", "openai-codex")
        self.model = os.getenv("AUTONOMOUS_RESEARCH_HERMES_MODEL", "gpt-5.6-luna")

    def run(self, plan: Mapping[str, object] | None = None) -> AgentRun:
        del plan  # Hermes, not the supervisor, owns plan selection.
        run_id = f"hermes-{int(time.time())}"
        output_path = self.lab_root / "agent-runs" / f"{run_id}.txt"
        usage_path = self.lab_root / "agent-runs" / f"{run_id}.usage.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = self._prompt()
        environment = os.environ.copy()
        environment["AUTONOMOUS_RESEARCH_LAB"] = str(self.lab_root)
        environment["AUTONOMOUS_RESEARCH_REPO_ROOT"] = str(self.repo_root)
        environment["QUANT_WORKSPACE"] = str(self.lab_root / "experiments")
        environment["HERMES_WRITE_SAFE_ROOT"] = str(self.lab_root)
        # Raw LS rows are scoped to the child process.  The persistent lab may
        # retain code, receipts and results, but never a downloaded data file.
        # TemporaryDirectory is deliberately outside the lab and is removed
        # even when Hermes times out or exits with an error.
        started = time.monotonic()
        temporary_parent = Path(os.getenv("STRATEGY_MARKET_DATA_PARENT", "/tmp"))
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="strategy-market-", dir=temporary_parent) as data_dir:
            environment["STRATEGY_MARKET_DATA_DIR"] = data_dir
            environment["LS_TOKEN_CACHE_DIR"] = str(Path(data_dir) / "token-cache")
            environment["LS_DATA_ACCESS_MODE"] = "readonly"
            environment["LS_ALLOWED_TR_CODES"] = (
                "t1665,t8410,t8411,t8412,t8451,t8452,t8453,"
                "t1441,t1444,t1452,t1463,t1466,t1481,t1482,t1489,t1492"
            )
            Path(environment["LS_TOKEN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
            try:
                completed = subprocess.run(
                    [
                        self.binary,
                        "--in",
                        str(self.lab_root),
                        "--skills",
                        "autonomous-quant-research",
                        "--provider",
                        self.provider,
                        "--model",
                        self.model,
                        "--reasoning",
                        "high",
                        "--usage-file",
                        str(usage_path),
                        "-z",
                        prompt,
                    ],
                    cwd=self.lab_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError:
                error = f"Hermes binary not found: {self.binary}"
                output_path.write_text(error + "\n", encoding="utf-8")
                return AgentRun(run_id, None, "FAILED", None, str(output_path), error, time.monotonic() - started, str(usage_path))
            except subprocess.TimeoutExpired as exc:
                output = (exc.stdout or "") + (exc.stderr or "")
                output_path.write_text(output + "\nagent timeout\n", encoding="utf-8")
                return AgentRun(run_id, None, "TIMED_OUT", None, str(output_path), "Hermes timed out", time.monotonic() - started, str(usage_path))

        output = (completed.stdout or "") + (completed.stderr or "")
        output_path.write_text(output, encoding="utf-8")
        status = "COMPLETED" if completed.returncode == 0 else "FAILED"
        return AgentRun(
            run_id,
            None,
            status,
            completed.returncode,
            str(output_path),
            None if completed.returncode == 0 else "Hermes returned a non-zero exit code",
            time.monotonic() - started,
            str(usage_path),
        )

    def _prompt(self) -> str:
        return f"""You are Strategy Hermes, the autonomous quant researcher and owner of this persistent research lab.

You are a dedicated strategy-generation agent. Do not delegate the work to the existing research-hermes, quant-hermes, CEO, Kanban, factory, order, broker, or OMS paths. Those agents are separate departments. Use only the repository's read-only source files, local research data/tools explicitly available to you, and this lab's writable workspace.

Research lab: {self.lab_root}
Repository for inspection only: {self.repo_root}
Writable experiment workspace: {self.lab_root / 'experiments'}
Ephemeral raw-market-data root (deleted after this Hermes turn): $STRATEGY_MARKET_DATA_DIR

For market data, use only the repository module
{self.repo_root}/departments/01-research/autonomous/ls_market_data.py. It is a
read-only allow-list for LS chart/investor/ranking TRs t1665, t8410, t8411,
t8412, t8451, t8452, t8453, t1441, t1444, t1452, t1463, t1466, t1481, t1482,
t1489 and t1492. Query only the symbols/date range or ranking snapshot needed
for the current experiment. Prefer the returned rows in memory; if a dataframe library needs a
file, use its temporary-data helper and write only below $STRATEGY_MARKET_DATA_DIR.
Never read or write quant-data, the legacy discovery cache, market/research
databases, collector backfill tables, or any other persistent raw-data path.
Do not print raw rows or credentials. Persist only code, experiment artifacts,
the non-sensitive DataReceipt (TR, range, row count, hash), and result/lineage
metadata in the lab. The temporary root is destroyed when this turn exits.

Read these files first:
{self.lab_root}/OBJECTIVE.md
{self.lab_root}/STATE.md
{self.lab_root}/RESOURCE_MAP.md
{self.lab_root}/KNOWLEDGE.md
{self.lab_root}/FAILURE_MEMORY.md
{self.lab_root}/EXPERIMENT_LOG.md

You own the complete research turn. Inspect the objective and prior lineage, then:
1. Form competing hypotheses with explicit mechanisms and falsifiers, recording each as JSON under {self.lab_root}/hypotheses.
2. Select one information-gaining next step and preregister an experiment plan as JSON under {self.lab_root}/plans before measuring its result. The plan must contain plan_id, hypothesis_id, objective, method, data_requirements, splits (development, validation, out-of-sample and forward-or-paper observation unless a concrete limitation is recorded), cost_model, integer seed, signature, and a non-empty preregistration_hash.
3. Write all strategy/backtest code and generated artifacts under {self.lab_root}/experiments. Never modify the repository or shared department code.
4. Run the actual experiment. Include point-in-time data boundaries, delayed execution, fees/slippage/turnover, out-of-sample evaluation, parameter sensitivity, time/regime/asset slices, and adversarial failure analysis. Missing evidence is unknown, never zero.
5. Write {self.lab_root}/results/<plan_id>.json with plan_id, preregistration_hash, status (COMPLETED, FAILED, or BLOCKED), cost_included, oos_evaluated, leakage_detected, named boolean robustness checks, measured metrics, artifacts, failure_modes, limitations, and failure_reason when required.

Continue from existing state rather than deleting or rewriting lineage. Keep one focused research turn, leave a reproducible artifact trail, and stop only after writing the result or a concrete BLOCKED/FAILED artifact. A candidate is evidence-gated only; it never authorizes an order or deployment."""


# Compatibility name for existing tests and callers. New runtime wiring uses
# the explicit StrategyHermesAgent name.
HermesResearchAgent = StrategyHermesAgent


__all__ = ["AgentRun", "HermesResearchAgent", "StrategyHermesAgent"]
