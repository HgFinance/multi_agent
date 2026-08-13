"""BFF diagnostics for Governance API transport failures."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""
os.environ["PORTFOLIO_RUNTIME_STORE_PATH"] = os.path.join(
    tempfile.gettempdir(), f"hgfinance-governance-transport-tests-{os.getpid()}.sqlite3"
)
os.environ["PORTFOLIO_AUTH_REQUIRED"] = "false"

import apps.api.main as bff_main
# main.py deliberately imports this module by its unqualified runtime name.
# Use that same module instance so the exception handler and the test share
# one exception class.
import governance_client
from governance_client import GovernanceTransportError, governance_request


class _ConnectFailingAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, *args, **kwargs):
        request = httpx.Request("POST", "http://governance-api:8000/test")
        raise httpx.ConnectError("connection refused", request=request)


class GovernanceTransportErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_connect_failure_is_logged_and_classified(self) -> None:
        with (
            patch.object(governance_client, "GOVERNANCE_API_URL", "http://governance-api:8000"),
            patch.object(governance_client.httpx, "AsyncClient", _ConnectFailingAsyncClient),
            self.assertLogs("uvicorn.error", level="WARNING") as logs,
        ):
            with self.assertRaises(GovernanceTransportError) as raised:
                await governance_request(
                    "POST",
                    "/governance/v1/mandates/m1/change-requests",
                    body={"trace_id": "trace-123"},
                )

        error = raised.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.payload["error_code"], "GOVERNANCE_API_CONNECT_FAILED")
        self.assertEqual(error.payload["detail"], {"reason": "connect_error"})
        self.assertEqual(error.payload["trace_id"], "trace-123")
        self.assertTrue(any("connection refused" in item for item in logs.output))


class GovernanceTransportResponseTest(unittest.TestCase):
    def test_bff_returns_safe_structured_transport_error(self) -> None:
        transport_error = GovernanceTransportError(
            status_code=504,
            error_code="GOVERNANCE_API_TIMEOUT",
            message="거버넌스 서비스 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.",
            reason="timeout",
            trace_id="trace-456",
        )
        with (
            patch.object(bff_main, "PORTFOLIO_AUTH_REQUIRED", False),
            patch("apps.api.main._governance_request", new_callable=AsyncMock, side_effect=transport_error),
        ):
            response = TestClient(bff_main.app).post(
                "/ui/mandates/m1/change-requests",
                json={"trace_id": "trace-456"},
            )

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(
            response.json(),
            {
                "error_code": "GOVERNANCE_API_TIMEOUT",
                "message": "거버넌스 서비스 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.",
                "detail": {"reason": "timeout"},
                "trace_id": "trace-456",
            },
        )
