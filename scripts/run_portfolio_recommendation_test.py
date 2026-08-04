"""Run the synthetic user-profile to portfolio-list TEST pipeline."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from departments.risk_qa_testkit import run_portfolio_recommendation_pipeline  # noqa: E402


def main() -> int:
    as_of = datetime(2026, 8, 4, tzinfo=timezone.utc).isoformat()
    result = run_portfolio_recommendation_pipeline(
        {
            "user_id": "user-test-portfolio-001",
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
    suitability = result["suitability"]
    print(
        json.dumps(
            {
                "pipeline_status": result["pipeline_status"],
                "safe_action": result["safe_action"],
                "recommendation_ids": [item["portfolio_id"] for item in suitability["recommendations"]],
                "excluded": {
                    item["portfolio_id"]: item["reasons"] for item in suitability["exclusions"]
                },
                "risk_verdict": result["risk_qa"]["risk_gate"].get("verdict"),
                "qa_decision": result["risk_qa"]["qa_gate"].get("decision"),
                "manual_review_required": result["manual_review_required"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["pipeline_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
