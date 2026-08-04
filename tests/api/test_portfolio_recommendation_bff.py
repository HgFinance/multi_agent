"""BFF E2E contract for user suitability -> LangGraph -> advisory portfolio."""

from __future__ import annotations

import os
import time
import unittest

os.environ["DATABASE_URL"] = ""
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from fastapi.testclient import TestClient

from apps.api.main import app


class PortfolioRecommendationBffTest(unittest.TestCase):
    def test_beginner_profile_returns_non_binding_backend_recommendation(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-e2e-beginner",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "0.10",
                "liquidity_need": "MEDIUM",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]

        result = None
        runtime = None
        for _ in range(100):
            time.sleep(0.05)
            runtime = client.get("/ui/snapshot").json()["operations"]["runtime"]
            if runtime["run_id"] == run_id and runtime["result"] is not None:
                result = runtime["result"]
                break

        self.assertIsNotNone(result, runtime)
        self.assertEqual(result["pipeline_status"], "COMPLETED")
        self.assertFalse(result["production_enabled"])
        self.assertFalse(result["binding"])
        self.assertTrue(result["manual_review_required"])
        self.assertEqual(result["suitability"]["status"], "MATCHED")
        self.assertEqual(result["suitability"]["recommendations"][0]["portfolio_id"], "starter-safety")
        self.assertTrue(result["suitability"]["recommendations"][0]["target_allocations"])
        self.assertTrue(any(message["kind"] == "worker_summary" for message in runtime["messages"]))
        self.assertTrue(any(message["kind"] == "department_handoff" for message in runtime["messages"]))


if __name__ == "__main__":
    unittest.main()
