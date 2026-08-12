"""Run the available Jaeil Research/Quant acceptance checks in priority order.

This runner is intentionally honest: contract self-checks can pass while a
runtime/API/Worker gate remains DOCUMENTED or BLOCKED. It never promotes a
Research or Quant component to Production.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


P0_CHECKS = (
    ("RQF-01 research_v2", "departments/01-research/contracts/research_v2.py"),
    ("RQF-01 v1_adapter", "departments/01-research/contracts/research_v1_to_v2.py"),
    ("RQF-02 pit_manifest", "departments/01-research/evidence/pit_manifest.py"),
    ("RQF-WEB-01 web_search_contract", "departments/01-research/evidence/web_search.py"),
    ("RQF-09 quant_contract", "departments/04-quant-backtest/contracts/quant_v2.py"),
    ("RQF-08 experiment_boundary", "departments/04-quant-backtest/pipeline/experiment_orchestrator.py"),
)


def _run(path: str) -> dict[str, Any]:
    command = [sys.executable, path]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "error_class": "TimeoutExpired"}
    return {
        "status": "PASS" if completed.returncode == 0 else "FAILED",
        "return_code": completed.returncode,
        "output_tail": completed.stdout[-600:].strip(),
        "error_tail": completed.stderr[-600:].strip(),
    }


def run_checks() -> dict[str, Any]:
    p0 = []
    for name, path in P0_CHECKS:
        result = _run(path)
        p0.append({"id": name, "path": path, **result})

    p1 = [
        {
            "id": "RQF-10..12",
            "status": "DOCUMENTED_ONLY",
            "reason": "Quant API/Job restart, Robustness/Trial Ledger and Held-out Skill gate need runtime evidence.",
        },
        {
            "id": "RQF-WEB-04",
            "status": "DOCUMENTED_ONLY",
            "reason": "Read-only Playwright MCP requires isolated browser runtime evidence.",
        },
    ]
    p2 = [
        {
            "id": "RQF-13",
            "status": "NOT_RUN",
            "reason": "Forecast/Ensemble remains a sandbox spike and is not connected to Risk/QA or Production.",
        }
    ]
    return {
        "production_enabled": False,
        "p0": p0,
        "p1": p1,
        "p2": p2,
        "p3": {
            "status": "NOT_DEFINED",
            "reason": "TEAM_JAEIL_RESEARCH_QUANT_GUIDE has no P3 priority item; RQF-3 is a phase, not P3.",
        },
        "p0_all_pass": all(item["status"] == "PASS" for item in p0),
    }


def main() -> int:
    report = run_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["p0_all_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
