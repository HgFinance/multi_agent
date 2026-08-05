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
from apps.api.portfolio_universe import enrich_suitability_result


class PortfolioRecommendationBffTest(unittest.TestCase):
    def test_frontend_port_3003_is_allowed_by_bff_cors(self) -> None:
        response = TestClient(app).options(
            "/ui/snapshot",
            headers={
                "Origin": "http://localhost:3003",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3003")

    def test_asset_visibility_toggles_exclude_bonds(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-toggle-check",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "0.10",
                "investment_amount": "1000000",
                "currency": "KRW",
                "universe_id": "KOREA_GLOBAL_MIXED",
                "category": "PORTFOLIO_RECOMMENDATION",
                "include_stock": False,
                "include_derivatives": True,
                "query": "",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]
        result = None
        for _ in range(160):
            time.sleep(0.05)
            runtime = client.get(f"/ui/portfolio-recommendations/{run_id}").json()
            if runtime["result"] is not None:
                result = runtime["result"]
                break
        self.assertIsNotNone(result, runtime)
        assert result is not None
        self.assertEqual(result["asset_visibility"]["include_stock"], False)
        self.assertTrue(result["asset_visibility"]["include_derivatives"])
        self.assertTrue(all(item["asset_class"] in {"LEVERAGED_ETF", "SHORT_EXPOSURE", "DERIVATIVES_HEDGE"} for item in result["instrument_recommendations"]))
        self.assertFalse(any(item["asset_class"] == "SHORT_TERM_BOND" for item in result["instrument_recommendations"]))

    def test_domestic_universe_ignores_out_of_scope_global_allocation(self) -> None:
        result = enrich_suitability_result(
            {
                "safe_action": "NO_ACTION",
                "currency": "KRW",
                "suitability": {
                    "recommendations": [
                        {
                            "portfolio_id": "balanced-core",
                            "target_allocations": {"KOREA_EQUITY": "0.20", "GLOBAL_EQUITY": "0.50"},
                            "target_amounts": {"KOREA_EQUITY": "200000.00", "GLOBAL_EQUITY": "500000.00"},
                        }
                    ]
                },
            },
            "KOREA_EQUITY_WATCHLIST",
        )
        self.assertEqual(result["instrument_recommendations_status"], "COMPLETE")
        self.assertEqual(result["safe_action"], "NO_ACTION")
        self.assertEqual(result["unresolved_asset_classes"], [])
        self.assertTrue(all(item["asset_class"] == "KOREA_EQUITY" for item in result["instrument_recommendations"]))

    def test_both_asset_toggles_off_is_safe_hold(self) -> None:
        result = enrich_suitability_result(
            {
                "safe_action": "NO_ACTION",
                "currency": "KRW",
                "suitability": {
                    "recommendations": [
                        {
                            "portfolio_id": "starter-safety",
                            "target_allocations": {"KOREA_EQUITY": "1.00"},
                            "target_amounts": {"KOREA_EQUITY": "1000000.00"},
                        }
                    ]
                },
            },
            "KOREA_GLOBAL_MIXED",
            include_stock=False,
            include_derivatives=False,
        )
        self.assertEqual(result["instrument_recommendations"], [])
        self.assertEqual(result["instrument_recommendations_status"], "UNAVAILABLE")
        self.assertEqual(result["safe_action"], "HOLD")

    def test_universe_and_free_query_route_are_backend_owned(self) -> None:
        client = TestClient(app)
        universes = client.get("/ui/portfolio-universes")
        self.assertEqual(universes.status_code, 200)
        self.assertEqual(universes.json()["default_universe_id"], "KOREA_GLOBAL_MIXED")
        self.assertTrue(universes.json()["universes"])

        response = client.post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-e2e-free-query",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "0.10",
                "investment_amount": "1000000",
            "currency": "KRW",
            "universe_id": "KOREA_GLOBAL_MIXED",
            "category": "PORTFOLIO_RECOMMENDATION",
            "query": "국내 종목의 손실 위험과 근거를 설명해줘",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]
        result = None
        runtime = None
        for _ in range(160):
            time.sleep(0.05)
            runtime = client.get(f"/ui/portfolio-recommendations/{run_id}").json()
            if runtime["result"] is not None:
                result = runtime["result"]
                break
        self.assertIsNotNone(result, runtime)
        assert result is not None
        self.assertEqual(result["task_plan"]["category"], "PORTFOLIO_RECOMMENDATION")
        self.assertEqual(result["task_plan"]["requested_departments"], ["research", "risk", "qa", "ceo"])
        self.assertEqual(result["universe"]["universe_id"], "KOREA_GLOBAL_MIXED")
        self.assertTrue(result["instrument_recommendations"])
        self.assertTrue(result["suitability"]["recommendations"][0]["instrument_recommendations"])
        self.assertTrue(any(item["symbol"] == "005930" for item in result["instrument_recommendations"]))
        self.assertTrue(all(item["expected_return"] is None for item in result["instrument_recommendations"]))
        self.assertFalse(any("TEST async" in message["text"] for message in runtime["messages"]))
        self.assertEqual(runtime["departments"]["trading-department"]["status"], "SKIPPED")
        self.assertEqual(runtime["departments"]["research-department"]["last_message"], "research 부서가 2개 Worker 결과를 취합했습니다.")

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
                "investment_amount": "1000000",
                "currency": "KRW",
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
        self.assertEqual(result["suitability"]["currency"], "KRW")
        self.assertEqual(result["suitability"]["recommendations"][0]["target_amounts"]["KOREA_EQUITY"], "100000.00")
        self.assertTrue(any(message["kind"] == "worker_summary" for message in runtime["messages"]))
        self.assertTrue(any(message["kind"] == "department_handoff" for message in runtime["messages"]))

        approval = client.post(
            f"/ui/portfolio-recommendations/{run_id}/approval",
            json={"decision": "APPROVE"},
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        self.assertEqual(approval.json()["approval"]["status"], "APPROVE")
        self.assertFalse(approval.json()["approval"]["binding"])


if __name__ == "__main__":
    unittest.main()
