"""BFF E2E contract for user suitability -> LangGraph -> advisory portfolio."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = ""
os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = os.path.join(tempfile.gettempdir(), f"hgfinance-portfolio-tests-{os.getpid()}.sqlite3")
os.environ["PORTFOLIO_RUNTIME_EMBEDDED_WORKER"] = "true"
os.environ["PORTFOLIO_WORKER_RUNTIME"] = "deterministic_test"
os.environ["PORTFOLIO_REQUIRE_MANDATE_BINDING"] = "false"
os.environ["PORTFOLIO_GOVERNANCE_BINDING_ENABLED"] = "false"
os.environ["PORTFOLIO_AUTH_REQUIRED"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import apps.api.main as bff_main
from apps.api.main import app
from apps.api.portfolio_runtime import PortfolioRuntime
from apps.api.portfolio_schemas import PortfolioRecommendationResult
from apps.api.portfolio_universe import enrich_suitability_result

# apps.api.main freezes PORTFOLIO_AUTH_REQUIRED, PORTFOLIO_REQUIRE_MANDATE_BINDING and
# PORTFOLIO_GOVERNANCE_BINDING_ENABLED into module constants at import time, and the
# module is cached across test files within one pytest session. The os.environ lines
# above only take effect if this is the first file to import it; when another test file
# imports it first, this file's env vars are set too late. Patch the already-imported
# module's attributes directly so these defaults hold regardless of collection order
# (per-test `patch("apps.api.main.PORTFOLIO_AUTH_REQUIRED", True)` calls below still
# override this as expected).
bff_main.PORTFOLIO_AUTH_REQUIRED = False
bff_main.PORTFOLIO_REQUIRE_MANDATE_BINDING = False
bff_main.PORTFOLIO_GOVERNANCE_BINDING_ENABLED = False

# main.py imports portfolio_runtime *unqualified* via its own sys.path hack
# (`from portfolio_runtime import RUNTIME`), so `import apps.api.portfolio_runtime`
# here would be a distinct module object with its own EMBEDDED_WORKER_ENABLED copy -
# patching it would not affect the RUNTIME singleton main.py actually uses. Patch the
# real one via the bound method's own globals instead, so it's correct regardless of
# which sys.modules key the module ended up under.
bff_main.RUNTIME._dispatch.__func__.__globals__["EMBEDDED_WORKER_ENABLED"] = True


class PortfolioRecommendationBffTest(unittest.TestCase):
    @patch("apps.api.main._governance_request", new_callable=AsyncMock)
    def test_mandate_change_is_proxied_without_browser_domain_access(self, request: AsyncMock) -> None:
        request.return_value = {"stage": "AWAITING_REVIEW", "mandate_id": "m1"}
        response = TestClient(app).post(
            "/ui/mandates/m1/change-requests",
            json={"fund_id": "f1", "policy": {}, "objective_text": "보수적 운용"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stage"], "AWAITING_REVIEW")
        request.assert_awaited_once_with(
            "POST",
            "/governance/v1/mandates/m1/change-requests",
            body={"fund_id": "f1", "policy": {}, "objective_text": "보수적 운용"},
        )

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

    def test_health_ready_exposes_safe_dependency_projection(self) -> None:
        response = TestClient(app).get("/health/ready")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn(payload["status"], {"ready", "degraded"})
        self.assertFalse(payload["external_writes"])
        self.assertNotIn("DATABASE_URL", str(payload))
    def test_domestic_stock_projection_is_backend_owned(self) -> None:
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
                    "universe_id": "KOREA_EQUITY_WATCHLIST",
                "category": "PORTFOLIO_RECOMMENDATION",
                    "include_stock": True,
                    "include_derivatives": False,
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
        self.assertTrue(result["asset_visibility"]["include_stock"])
        self.assertFalse(result["asset_visibility"]["include_derivatives"])
        self.assertTrue(result["instrument_recommendations"])
        self.assertTrue(all(item["asset_class"] == "KOREA_EQUITY" for item in result["instrument_recommendations"]))

    def test_drawdown_ratio_contract_rejects_percent_points(self) -> None:
        response = TestClient(app).post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-invalid-drawdown",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "10",
                "investment_amount": "1000000",
                "currency": "KRW",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_drawdown_ratio_contract_rejects_zero(self) -> None:
        response = TestClient(app).post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-zero-drawdown",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "0",
                "investment_amount": "1000000",
                "currency": "KRW",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_mandate_version_requires_policy_hash_binding(self) -> None:
        payload = {
            "user_id": "bff-binding",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "mandate_version_id": "version-7",
        }
        response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "mandate_policy_binding_required")
    def test_idempotency_key_replays_same_advisory_run(self) -> None:
        client = TestClient(app)
        payload = {
            "user_id": "bff-idempotency",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
        }
        first = client.post(
            "/ui/portfolio-recommendations",
            json=payload,
            headers={"Idempotency-Key": "same-request"},
        )
        replay = client.post(
            "/ui/portfolio-recommendations",
            json=payload,
            headers={"Idempotency-Key": "same-request"},
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(first.json()["run_id"], replay.json()["run_id"])
        self.assertTrue(replay.json()["idempotent_replay"])
        for _ in range(160):
            if client.get(f"/ui/portfolio-recommendations/{first.json()['run_id']}").json()["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.05)
    def test_governance_binding_success_verifies_all_submitted_fields(self) -> None:
        payload = {
            "user_id": "governance-bound-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "mandate_id": "mandate-1",
            "case_id": None,
            "mandate_version_id": "version-1",
            "policy_hash": "hash-1",
        }
        canonical = {key: payload[key] for key in ("mandate_id", "case_id", "mandate_version_id", "policy_hash")}
        with (
            patch("apps.api.main.PORTFOLIO_GOVERNANCE_BINDING_ENABLED", True),
            patch("apps.api.main.GOVERNANCE_API_URL", "http://governance.test"),
            patch("apps.api.main._governance_request", new_callable=AsyncMock) as request,
            patch("apps.api.main.RUNTIME.start", return_value={"run_id": "binding-run", "status": "QUEUED", "workflow": "portfolio-recommendation-full", "idempotent_replay": False}) as start,
            patch(
                "apps.api.main.RUNTIME.get",
                return_value={
                    "trace_id": "trace-binding",
                    "case_id": None,
                    "mandate_version_id": "version-1",
                    "policy_hash": "hash-1",
                    "input_hash": "input-binding",
                },
            ),
        ):
            request.return_value = canonical
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        start.assert_called_once()
        request.assert_awaited_once_with(
            "GET",
            "/governance/v1/mandates/mandate-1/current",
        )

    def test_governance_binding_mismatch_rejects_before_runtime_start(self) -> None:
        payload = {
            "user_id": "governance-mismatch-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "mandate_id": "mandate-1",
            "case_id": "case-1",
            "mandate_version_id": "version-1",
            "policy_hash": "hash-1",
        }
        with (
            patch("apps.api.main.PORTFOLIO_GOVERNANCE_BINDING_ENABLED", True),
            patch("apps.api.main.GOVERNANCE_API_URL", "http://governance.test"),
            patch("apps.api.main._governance_request", new_callable=AsyncMock, return_value={**payload, "policy_hash": "wrong"}),
            patch("apps.api.main.RUNTIME.start") as start,
        ):
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "governance_mandate_binding_mismatch")
        start.assert_not_called()

    def test_governance_binding_unavailable_fails_closed(self) -> None:
        payload = {
            "user_id": "governance-unavailable-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "mandate_id": "mandate-1",
            "case_id": "case-1",
            "mandate_version_id": "version-1",
            "policy_hash": "hash-1",
        }
        with (
            patch("apps.api.main.PORTFOLIO_GOVERNANCE_BINDING_ENABLED", True),
            patch(
                "apps.api.main._governance_request",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=503, detail="governance_api_unavailable"),
            ),
            patch("apps.api.main.RUNTIME.start") as start,
        ):
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], "governance_binding_unavailable")
        start.assert_not_called()

    def test_governance_binding_malformed_response_fails_closed(self) -> None:
        payload = {
            "user_id": "governance-malformed-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "mandate_id": "mandate-1",
            "case_id": "case-1",
            "mandate_version_id": "version-1",
            "policy_hash": "hash-1",
        }
        with (
            patch("apps.api.main.PORTFOLIO_GOVERNANCE_BINDING_ENABLED", True),
            patch("apps.api.main.GOVERNANCE_API_URL", "http://governance.test"),
            patch("apps.api.main._governance_request", new_callable=AsyncMock, return_value={"binding": True}),
            patch("apps.api.main.RUNTIME.start") as start,
        ):
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "governance_mandate_binding_mismatch")
        start.assert_not_called()

    def test_runtime_store_rejects_second_active_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("PORTFOLIO_RUNTIME_STORE_PATH")
            os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = os.path.join(directory, "runtime.sqlite3")
            try:
                first = PortfolioRuntime()
                second = PortfolioRuntime()
                self.assertTrue(first._store.reserve_active_run("active-first", "2026-08-05T00:00:00+00:00", os.getpid()))
                queued = first._base_job("active-first", {"user_id": "owner"})
                first._store.save(queued)
                self.assertFalse(second._store.reserve_active_run("active-second", "2026-08-05T00:00:01+00:00", os.getpid()))
                first._store.release_active_run("active-first")
            finally:
                if previous is None:
                    os.environ.pop("PORTFOLIO_RUNTIME_STORE_PATH", None)
                else:
                    os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = previous
    def test_runtime_requeues_persisted_profile_after_worker_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("PORTFOLIO_RUNTIME_STORE_PATH")
            os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = os.path.join(directory, "runtime.sqlite3")
            try:
                seed = PortfolioRuntime()
                job = seed._base_job(
                    "restartable-run",
                    {
                        "user_id": "restart-owner",
                        "investment_amount": "1000000",
                        "max_drawdown_pct": "0.10",
                    },
                )
                seed._store.save(job)
                self.assertTrue(seed._store.reserve_active_run("restartable-run", job["updated_at"], 999999))
                with patch("apps.api.portfolio_runtime._process_alive", return_value=False), patch.object(
                    PortfolioRuntime,
                    "_dispatch",
                ) as dispatch:
                    recovered = PortfolioRuntime()
                restored = recovered.get("restartable-run")
                self.assertIsNotNone(restored)
                self.assertEqual(restored["status"], "QUEUED")
                self.assertEqual(restored["error"], "portfolio_runtime_requeued_after_worker_restart")
                dispatch.assert_called_once()
            finally:
                if previous is None:
                    os.environ.pop("PORTFOLIO_RUNTIME_STORE_PATH", None)
                else:
                    os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = previous

    def test_production_binding_mode_rejects_unversioned_analysis(self) -> None:
        payload = {
            "user_id": "binding-required-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
        }
        with patch("apps.api.main.PORTFOLIO_REQUIRE_MANDATE_BINDING", True):
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "mandate_version_binding_required")
    def test_auth_required_mode_rejects_missing_portfolio_identity(self) -> None:
        payload = {
            "user_id": "auth-required-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
        }
        with patch("apps.api.main.PORTFOLIO_AUTH_REQUIRED", True):
            response = TestClient(app).post("/ui/portfolio-recommendations", json=payload)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"], "portfolio_authentication_required")

    def test_auth_required_mode_allows_owner_bound_status(self) -> None:
        client = TestClient(app)
        payload = {
            "user_id": "auth-required-owner",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
        }
        with patch("apps.api.main.PORTFOLIO_AUTH_REQUIRED", True):
            response = client.post(
                "/ui/portfolio-recommendations",
                json=payload,
                headers={"X-User-Id": payload["user_id"]},
            )
            self.assertEqual(response.status_code, 202, response.text)
            run_id = response.json()["run_id"]
            status = client.get(
                f"/ui/portfolio-recommendations/{run_id}",
                headers={"X-User-Id": payload["user_id"]},
            )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["profile_user_id"], payload["user_id"])
        for _ in range(160):
            if client.get(
                f"/ui/portfolio-recommendations/{run_id}",
                headers={"X-User-Id": payload["user_id"]},
            ).json()["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.05)
    def test_run_status_rejects_mismatched_owner_header(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/ui/portfolio-recommendations",
            json={
                "user_id": "bff-owner",
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
        forbidden = client.get(
            f"/ui/portfolio-recommendations/{run_id}",
            headers={"X-User-Id": "different-owner"},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        for _ in range(160):
            if client.get(f"/ui/portfolio-recommendations/{run_id}").json()["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.05)

    def test_runtime_store_recovers_terminal_projection_after_new_instance(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.sqlite3")
            previous = os.environ.get("PORTFOLIO_RUNTIME_STORE_PATH")
            os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = path
            try:
                runtime = PortfolioRuntime()
                with runtime._lock:
                    older = runtime._base_job("older-run", {"user_id": "persisted-owner"})
                    older["status"] = "HOLD"
                    older["updated_at"] = "2026-08-04T00:00:00+00:00"
                    runtime._store.save(older)
                    profile = {"user_id": "persisted-owner", "idempotency_key": "recovered-key", "query": "same"}
                    job = runtime._base_job("persisted-run", profile)
                    job["status"] = "HOLD"
                    job["updated_at"] = "2026-08-05T00:00:00+00:00"
                    runtime._job = job
                    runtime._store.save(job)
                recovered = PortfolioRuntime()
                self.assertEqual(recovered.get("persisted-run")["status"], "HOLD")
                self.assertEqual(recovered.get("older-run")["run_id"], "older-run")
                self.assertEqual(
                    recovered._store.find_by_idempotency("persisted-owner", "recovered-key")["run_id"],
                    "persisted-run",
                )
                self.assertEqual(
                    recovered.start(profile)["idempotent_replay"],
                    True,
                )
            finally:
                if previous is None:
                    os.environ.pop("PORTFOLIO_RUNTIME_STORE_PATH", None)
                else:
                    os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = previous
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

    def test_live_universe_does_not_fallback_to_static_catalog(self) -> None:
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
            "KOREA_EQUITY_WATCHLIST",
            live_instruments=[],
            live_universe_status="UNAVAILABLE",
        )
        self.assertEqual(result["universe"]["status"], "UNAVAILABLE")
        self.assertEqual(result["instrument_recommendations"], [])
        self.assertEqual(result["instrument_recommendations_status"], "UNAVAILABLE")
        self.assertEqual(result["safe_action"], "HOLD")

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
            "KOREA_EQUITY_WATCHLIST",
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
        self.assertEqual(universes.json()["default_universe_id"], "KOREA_EQUITY_WATCHLIST")
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
                    "universe_id": "KOREA_EQUITY_WATCHLIST",
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
        # 2026-08-10 팀 합의로 포트폴리오 구성 요청은 요청 시점에 quant 를 거친다.
        # 단순 종목 질문(MARKET_RESEARCH)은 응답성 때문에 여전히 quant 를 부르지 않는다.
        self.assertEqual(
            result["task_plan"]["requested_departments"],
            ["research", "quant", "risk", "qa", "ceo"],
        )
        self.assertEqual(result["task_plan"]["workflow"], "portfolio-recommendation")
        self.assertTrue(result["task_plan"]["category_recognized"])
        self.assertEqual(result["universe"]["universe_id"], "KOREA_EQUITY_WATCHLIST")
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
        self.assertEqual(result["suitability"]["recommendations"][0]["target_amounts"]["KOREA_EQUITY"], "1000000.00")
        self.assertTrue(any(message["kind"] == "worker_summary" for message in runtime["messages"]))
        self.assertTrue(any(message["kind"] == "department_handoff" for message in runtime["messages"]))

        approval = client.post(
            f"/ui/portfolio-recommendations/{run_id}/approval",
            json={"decision": "APPROVE"},
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        self.assertEqual(approval.json()["approval"]["status"], "APPROVE")
        self.assertFalse(approval.json()["approval"]["binding"])

    def test_nested_result_contract_rejects_unknown_gate_fields(self) -> None:
        with self.assertRaises(ValidationError):
            PortfolioRecommendationResult.model_validate(
                {
                    "pipeline_status": "HOLD",
                    "workflow": "portfolio-recommendation-full",
                    "pipeline_version": "v1",
                    "trace_id": "trace-contract",
                    "safe_action": "HOLD",
                    "risk_gate": {
                        "status": "HOLD",
                        "verdict": "REJECT",
                        "safe_action": "HOLD",
                        "reason": "PIT data unavailable",
                        "data_quality": "FAIL",
                        "binding": False,
                        "unexpected": "must fail closed",
                    },
                }
            )

if __name__ == "__main__":
    unittest.main()
