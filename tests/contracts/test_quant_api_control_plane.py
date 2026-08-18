"""Regression tests for the quant API write boundary."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "departments/04-quant-backtest/api/quant_api.py"


def _load_quant_api():
    spec = importlib.util.spec_from_file_location("quant_api_contract", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuantApiConnectionTests(unittest.TestCase):
    def test_http_control_plane_auth_is_fail_closed(self) -> None:
        api = _load_quant_api()
        client = TestClient(api.app)

        with patch.dict(
            os.environ, {"MCP_RESEARCH_API_KEY": "test-control-token"}
        ):
            missing = client.get("/jobs")
            wrong = client.get(
                "/jobs", headers={"Authorization": "Bearer wrong"}
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_RESEARCH_API_KEY", None)
            fail_closed = client.get("/jobs")
        self.assertEqual(fail_closed.status_code, 401)

    def test_authorized_job_submission_reaches_the_queue(self) -> None:
        api = _load_quant_api()
        client = TestClient(api.app)
        seen: dict[str, object] = {}

        class Connection:
            def close(self) -> None:
                seen["closed"] = True

        connection = Connection()

        def enqueue(conn, hypothesis_id: str, *, requested_by: str):
            seen.update(
                conn=conn,
                hypothesis_id=hypothesis_id,
                requested_by=requested_by,
            )
            return {"accepted": True, "job_id": "job-1", "status": "QUEUED"}

        with patch.dict(
            os.environ, {"MCP_RESEARCH_API_KEY": "test-control-token"}
        ), patch.dict(
            sys.modules, {"job_queue": types.SimpleNamespace(enqueue=enqueue)}
        ), patch.object(api, "get_conn", return_value=connection):
            response = client.post(
                "/jobs?hypothesis_id=hypothesis-1&requested_by=quant-hermes",
                headers={"Authorization": "Bearer test-control-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_id"], "job-1")
        self.assertIs(seen["conn"], connection)
        self.assertTrue(seen["closed"])

    def test_job_submission_stays_behind_api_connection_boundary(self) -> None:
        api = _load_quant_api()
        if not hasattr(api, "submit_job"):
            self.skipTest("FastAPI is not installed")

        seen: dict[str, object] = {}

        class Connection:
            def close(self) -> None:
                seen["closed"] = True

        connection = Connection()

        def enqueue(conn, hypothesis_id: str, *, requested_by: str):
            seen.update(
                conn=conn,
                hypothesis_id=hypothesis_id,
                requested_by=requested_by,
            )
            return {"accepted": True, "job_id": "job-1", "status": "QUEUED"}

        with patch.dict(
            sys.modules, {"job_queue": types.SimpleNamespace(enqueue=enqueue)}
        ), patch.object(api, "get_conn", return_value=connection):
            result = api.submit_job(
                hypothesis_id="hypothesis-1", requested_by="quant-hermes"
            )

        self.assertEqual(result["job_id"], "job-1")
        self.assertIs(seen["conn"], connection)
        self.assertEqual(seen["requested_by"], "quant-hermes")
        self.assertTrue(seen["closed"])

    def test_selected_database_url_uses_write_connection_helper(self) -> None:
        api = _load_quant_api()
        sentinel = object()
        seen: dict[str, object] = {}

        def connect(dsn: str, *, connect_timeout: int):
            seen.update(dsn=dsn, connect_timeout=connect_timeout)
            return sentinel

        modules = {
            "db_writer": types.SimpleNamespace(connect=connect),
            "source_registry": types.SimpleNamespace(
                load_project_env=lambda: {
                    "DATABASE_URL": "postgresql://selected-quant-role"
                }
            ),
        }
        with patch.dict(sys.modules, modules):
            self.assertIs(api.get_conn(), sentinel)

        self.assertEqual(
            seen,
            {"dsn": "postgresql://selected-quant-role", "connect_timeout": 10},
        )

    def test_seed_read_reuses_selected_api_connection(self) -> None:
        api = _load_quant_api()
        if not hasattr(api, "research_seeds"):
            self.skipTest("FastAPI is not installed")
        seen: dict[str, object] = {"closed": False}

        class Connection:
            def close(self) -> None:
                seen["closed"] = True

        connection = Connection()

        def bridge(*, conn, limit: int):
            seen.update(conn=conn, limit=limit)
            return {"ok": True, "seeds": []}

        with patch.dict(
            sys.modules,
            {"research_bridge": types.SimpleNamespace(bridge=bridge)},
        ), patch.object(api, "get_conn", return_value=connection):
            result = api.research_seeds(limit=7)

        self.assertTrue(result["ok"])
        self.assertIs(seen["conn"], connection)
        self.assertEqual(seen["limit"], 7)
        self.assertTrue(seen["closed"])

    def test_metric_response_preserves_split_and_dimensions(self) -> None:
        api = _load_quant_api()
        if not hasattr(api, "experiment_metrics"):
            self.skipTest("FastAPI is not installed")
        rows = [
            {
                "metric": "mean_net_bps_per_opportunity",
                "value": "2.5",
                "unit": "BPS",
                "split": "WALK_FORWARD",
                "dimensions": {"fold": 0},
                "cost_model_version": "krx-v1",
            },
            {
                "metric": "mean_net_bps_per_opportunity",
                "value": "-1.25",
                "unit": "BPS",
                "split": "WALK_FORWARD",
                "dimensions": {"fold": 1},
                "cost_model_version": "krx-v1",
            },
        ]

        with patch.object(api, "_query", return_value=rows):
            result = api.experiment_metrics("experiment-1")

        self.assertEqual(result["experiment_id"], "experiment-1")
        self.assertEqual(len(result["metrics"]), 2)
        self.assertEqual(
            [row["dimensions"] for row in result["metrics"]],
            [{"fold": 0}, {"fold": 1}],
        )
        self.assertEqual(
            [row["value"] for row in result["metrics"]], [2.5, -1.25]
        )


if __name__ == "__main__":
    unittest.main()
