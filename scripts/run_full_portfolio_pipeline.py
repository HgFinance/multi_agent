"""Run the complete async TEST portfolio recommendation pipeline."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.workflows.portfolio_recommendation import (  # noqa: E402
    run_portfolio_recommendation_pipeline_async,
)


async def main_async() -> int:
    as_of = "2026-08-04T00:00:00+00:00"
    result = await run_portfolio_recommendation_pipeline_async(
        {
            "user_id": "user-test-full-pipeline",
            "mindset": "RISK_SEEKING",
            "experience": "INTERMEDIATE",
            "investment_horizon_years": 5,
            "max_drawdown_pct": "0.25",
            "liquidity_need": "MEDIUM",
            "as_of": as_of,
        },
        [
            {
                "portfolio_id": "balanced-core",
                "name": "Balanced Core",
                "risk_band": "MEDIUM",
                "minimum_experience": "BEGINNER",
                "minimum_horizon_years": 3,
                "max_drawdown_pct": "0.15",
                "max_exit_days": 14,
                "target_allocations": {"GLOBAL_EQUITY": "0.60", "SHORT_TERM_BOND": "0.40"},
                "evidence_refs": ["research:portfolio-catalog:v1"],
                "as_of": as_of,
            },
            {
                "portfolio_id": "aggressive-growth",
                "name": "Aggressive Growth",
                "risk_band": "HIGH",
                "minimum_experience": "EXPERIENCED",
                "minimum_horizon_years": 7,
                "max_drawdown_pct": "0.35",
                "max_exit_days": 30,
                "target_allocations": {"GLOBAL_EQUITY": "0.80", "EMERGING_MARKETS": "0.20"},
                "evidence_refs": ["research:portfolio-catalog:v1"],
                "as_of": as_of,
            },
        ],
    )
    print(
        json.dumps(
            {
                "workflow": result["workflow"],
                "pipeline_status": result["pipeline_status"],
                "safe_action": result["safe_action"],
                "recommendation_ids": [
                    item["portfolio_id"] for item in result["suitability"]["recommendations"]
                ],
                "risk_verdict": result["risk_gate"]["verdict"],
                "qa_decision": result["qa_gate"]["decision"],
                "departments": {
                    stage: {
                        "status": report["status"],
                        "executed": report["executed"],
                        "failed": report["failed"],
                        "fan_out": report["fan_out"],
                        "fan_in": report["fan_in"],
                    }
                    for stage, report in result["department_reports"].items()
                },
                "production_enabled": result["production_enabled"],
                "external_writes": result["external_writes"],
                "manual_review_required": result["manual_review_required"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["pipeline_status"] == "COMPLETED" else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
