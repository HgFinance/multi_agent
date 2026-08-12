"""Run the synthetic Risk -> QA pipeline without production data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from departments.risk_qa_testkit import PipelineMode, WorkerRuntime, run_risk_qa_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute Risk and AI-QA synthetic TEST pipeline"
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in PipelineMode],
        default=PipelineMode.TEST.value,
        help="test runs synthetic end-to-end; production is intentionally OFF",
    )
    parser.add_argument(
        "--worker-runtime",
        choices=[runtime.value for runtime in WorkerRuntime],
        default=WorkerRuntime.DETERMINISTIC.value,
        help="deterministic stub or real local Ollama Worker; Head remains TEST stub",
    )
    args = parser.parse_args()
    result = run_risk_qa_pipeline(args.mode, worker_runtime=args.worker_runtime)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["pipeline_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
