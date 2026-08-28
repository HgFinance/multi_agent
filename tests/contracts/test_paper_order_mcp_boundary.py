from __future__ import annotations

import asyncio
import sqlite3
import sys
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from apps.api.conditional_rules import ConditionalRuleCandidate
from apps.api.paper_order_mcp import (
    BearerAuthMiddleware,
    UntrustedHermesOrderCandidate,
    _delegate_to_orchestrator,
    build_server,
    check_readiness,
    validate_api_key,
)
from orchestration.contracts.user_paper_order import TextEvidence

ROOT = Path(__file__).resolve().parents[2]
VALID_KEY = "paper-order-mcp-9f4e61d807a248e8a2b17f"


def test_text_evidence_schema_explains_instrument_normalization() -> None:
    description = TextEvidence.model_json_schema()["properties"]["normalized"][
        "description"
    ]
    assert "INSTRUMENT must exactly equal instrument_mention" in description


def _yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_mcp_key_validation_fails_closed() -> None:
    for value in (
        None,
        "",
        "too-short",
        "CHANGE_ME_MCP_TRADING_ORDER_API_KEY",
        "${MCP_TRADING_ORDER_API_KEY}",
        "a" * 32,
        "valid looking key with a space 1234567890",
        "한글비밀키" * 8,
    ):
        with pytest.raises(RuntimeError):
            validate_api_key(value)
    assert validate_api_key(VALID_KEY) == VALID_KEY


def test_http_boundary_rejects_missing_and_wrong_bearer() -> None:
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(downstream, api_key=VALID_KEY)

    async def request(headers: dict[str, str]) -> tuple[int, dict[str, str]]:
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
                "headers": [
                    (name.casefold().encode("ascii"), value.encode("ascii"))
                    for name, value in headers.items()
                ],
                "client": ("127.0.0.1", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        start = next(item for item in sent if item["type"] == "http.response.start")
        response_headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in start["headers"]
        }
        return start["status"], response_headers

    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": VALID_KEY},
        {"Authorization": f"Basic {VALID_KEY}"},
    ):
        status, response_headers = asyncio.run(request(headers))
        assert status == 401
        assert response_headers["www-authenticate"] == "Bearer"
    assert asyncio.run(request({"Authorization": f"Bearer {VALID_KEY}"}))[0] == 204


def test_server_exposes_command_tools_and_scoped_status_reader(monkeypatch) -> None:
    order_calls: list[dict] = []
    conditional_calls: list[dict] = []
    status_calls: list[dict] = []
    fake = ModuleType("apps.api.user_order_orchestrator")
    conditional_fake = ModuleType("apps.api.conditional_rule_orchestrator")

    async def orchestrate(**kwargs):
        order_calls.append(kwargs)
        return {"state": "INTERPRETED", "mode": "PAPER"}

    async def orchestrate_conditional(**kwargs):
        conditional_calls.append(kwargs)
        return {"state": "ACTIVE", "mode": "PAPER", "rule_active": True}

    fake.process_user_paper_order = orchestrate  # type: ignore[attr-defined]
    conditional_fake.process_user_conditional_paper_rule = (  # type: ignore[attr-defined]
        orchestrate_conditional
    )

    def read_status(**kwargs):
        status_calls.append(kwargs)
        return {
            "authority_verified": True,
            "workflow_state": "ACCOUNTING_PENDING",
            "final_answer": "권위 상태 확인",
        }

    conditional_fake.get_user_conditional_paper_rule_status = read_status  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apps.api.user_order_orchestrator", fake)
    monkeypatch.setitem(
        sys.modules, "apps.api.conditional_rule_orchestrator", conditional_fake
    )

    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == [
        "process_user_paper_order",
        "process_user_conditional_paper_rule",
        "get_user_conditional_paper_rule_status",
    ]
    schema = tools[0].inputSchema
    candidate_ref = schema["properties"]["interpretation"]["$ref"]
    candidate_schema = schema["$defs"][candidate_ref.rsplit("/", 1)[-1]]
    assert candidate_schema["type"] == "object"
    # The MCP transport must accept an untrusted object so malformed Hermes
    # output reaches the deterministic verifier and receives a durable outcome.
    # It does not confer authority: the strict candidate contract is applied by
    # the orchestrator before any PAPER OMS submission.
    assert candidate_schema["additionalProperties"] is True
    assert candidate_schema["properties"]["mode"]["const"] == "PAPER"
    assert candidate_schema["properties"]["binding"]["const"] is False
    assert candidate_schema["properties"]["decision"]["enum"] == [
        "EXECUTE",
        "CLARIFY",
        "NOT_ORDER",
    ]
    reason_items = candidate_schema["properties"]["reason_codes"]["items"]
    assert reason_items["$ref"].endswith("/OrderReasonCode")

    interpretation = {
        "schema_version": "user-paper-order-interpretation.v1",
        "mode": "PAPER",
        "binding": False,
        "raw_text_sha256": "0" * 64,
        "decision": "NOT_ORDER",
        "action": None,
        "instrument_mention": None,
        "side": None,
        "quantity": None,
        "order_type": None,
        "limit_price": None,
        "evidence": [],
        "reason_codes": ["QUESTION_OR_ADVICE"],
    }
    result = asyncio.run(
        server.call_tool(
            "process_user_paper_order",
            {
                "root_task_id": "root-1",
                "trading_task_id": "trade-1",
                "interpretation": interpretation,
            },
        )
    )
    assert result[1] == {"state": "INTERPRETED", "mode": "PAPER"}
    assert order_calls == [
        {
            "root_task_id": "root-1",
            "trading_task_id": "trade-1",
            "interpretation": interpretation,
        }
    ]

    conditional_candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "1D"},
            "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "2"},
        },
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
    }
    conditional_result = asyncio.run(
        server.call_tool(
            "process_user_conditional_paper_rule",
            {
                "root_task_id": "root-1",
                "trading_task_id": "trade-1",
                "candidate": conditional_candidate,
                "clarification_reason": None,
            },
        )
    )
    assert conditional_result[1]["rule_active"] is True
    assert conditional_calls == [
        {
            "root_task_id": "root-1",
            "trading_task_id": "trade-1",
            "candidate": ConditionalRuleCandidate.model_validate(conditional_candidate),
            "candidates": None,
            "clarification_reason": None,
        }
    ]

    status_result = asyncio.run(
        server.call_tool(
            "get_user_conditional_paper_rule_status",
            {"root_task_id": "root-1", "trading_task_id": "trade-1"},
        )
    )
    assert status_result[1]["authority_verified"] is True
    assert status_calls == [{"root_task_id": "root-1", "trading_task_id": "trade-1"}]


def test_transport_accepts_contradictory_candidate_for_durable_verifier(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    fake = ModuleType("apps.api.user_order_orchestrator")

    async def orchestrate(**kwargs):
        calls.append(kwargs)
        return {
            "decision": "CLARIFY",
            "request_state": "CLARIFICATION_REQUIRED",
            "reason_codes": ["INVALID_CANDIDATE_SCHEMA"],
        }

    fake.process_user_paper_order = orchestrate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apps.api.user_order_orchestrator", fake)
    server = build_server()
    contradictory = {
        "schema_version": "user-paper-order-interpretation.v1",
        "mode": "PAPER",
        "binding": False,
        "raw_text_sha256": "0" * 64,
        "decision": "CLARIFY",
        "action": "PLACE_ORDER",
        "instrument_mention": "삼성전자",
        "side": "BUY",
        "quantity": "2",
        "order_type": None,
        "limit_price": None,
        "evidence": [],
        "reason_codes": ["MISSING_OR_CONFLICTING_ORDER_TYPE"],
    }

    result = asyncio.run(
        server.call_tool(
            "process_user_paper_order",
            {
                "root_task_id": "t_root1",
                "trading_task_id": "t_trade1",
                "interpretation": contradictory,
            },
        )
    )

    assert result[1]["request_state"] == "CLARIFICATION_REQUIRED"
    assert calls == [
        {
            "root_task_id": "t_root1",
            "trading_task_id": "t_trade1",
            "interpretation": contradictory,
        }
    ]


def test_sync_order_orchestrator_runs_off_the_mcp_event_loop(monkeypatch) -> None:
    calls: list[dict] = []
    offloads: list[object] = []
    fake = ModuleType("apps.api.user_order_orchestrator")

    def orchestrate(**kwargs):
        calls.append(kwargs)
        return {"state": "COMPLETED", "mode": "PAPER"}

    async def to_thread(function, /, *args, **kwargs):
        offloads.append(function)
        return function(*args, **kwargs)

    fake.process_user_paper_order = orchestrate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apps.api.user_order_orchestrator", fake)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    interpretation = UntrustedHermesOrderCandidate.model_validate(
        {
            "schema_version": "user-paper-order-interpretation.v1",
            "mode": "PAPER",
            "binding": False,
            "raw_text_sha256": "0" * 64,
            "decision": "NOT_ORDER",
            "action": None,
            "instrument_mention": None,
            "side": None,
            "quantity": None,
            "order_type": None,
            "limit_price": None,
            "evidence": [],
            "reason_codes": ["QUESTION_OR_ADVICE"],
        }
    )

    result = asyncio.run(
        _delegate_to_orchestrator(
            root_task_id="t_root1",
            trading_task_id="t_trade1",
            interpretation=interpretation,
        )
    )

    assert result == {"state": "COMPLETED", "mode": "PAPER"}
    assert offloads == [orchestrate]
    assert calls[0]["root_task_id"] == "t_root1"


def test_readiness_checks_role_schema_kanban_and_trading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import psycopg2

    kanban_path = tmp_path / "kanban.db"
    sqlite3.connect(kanban_path).close()
    observed: list[object] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            observed.append(statement)

        def fetchone(self):
            return (
                "execution.user_order_requests",
                "execution.user_order_interpretations",
            )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("MCP_TRADING_ORDER_API_KEY", VALID_KEY)
    monkeypatch.setenv(
        "ORDER_ORCHESTRATOR_DATABASE_URL",
        "postgresql://redacted.invalid/control",
    )
    monkeypatch.setenv(
        "CONDITIONAL_RULE_DATABASE_URL",
        "postgresql://conditional.invalid/control",
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_path))
    monkeypatch.setenv("TRADING_API_URL", "http://trading-api:8000")
    monkeypatch.setattr(psycopg2, "connect", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout: observed.append((url, timeout)) or Response(),
    )

    check_readiness()

    assert observed[-1] == ("http://trading-api:8000/health/ready", 3)


def test_production_readiness_rejects_generic_database_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TRADING_ORDER_API_KEY", VALID_KEY)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://generic.invalid/control")
    monkeypatch.delenv("ORDER_ORCHESTRATOR_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="dedicated order orchestrator"):
        check_readiness()


def test_compose_keeps_authority_secrets_out_of_trading_hermes() -> None:
    root = _yaml("docker-compose.yml")
    service = root["services"]["paper-order-orchestrator-mcp"]
    environment = service["environment"]
    assert service["command"] == ["python", "-m", "apps.api.paper_order_mcp"]
    assert service["user"] == "1000:1000"
    assert service["expose"] == ["8046"]
    assert "ports" not in service
    assert service["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "apps.api.paper_order_mcp",
        "--healthcheck",
    ]
    assert service["depends_on"]["trading-api"] == {"condition": "service_healthy"}
    assert any(str(volume).endswith(":/opt/kanban") for volume in service["volumes"])
    for key in (
        "DATABASE_URL",
        "ORDER_ORCHESTRATOR_DATABASE_ROLE",
        "CONDITIONAL_RULE_DATABASE_URL",
        "CONDITIONAL_RULE_DATABASE_ROLE",
        "TRADING_API_URL",
        "TRADING_SERVICE_AUTH_SECRET",
        "TRADING_SERVICE_AUTH_ISSUER",
        "TRADING_SERVICE_AUTH_AUDIENCE",
        "MCP_TRADING_ORDER_API_KEY",
    ):
        assert key in environment

    trading_service = _yaml("departments/02-trading/compose.yaml")["services"][
        "trading-hermes"
    ]
    trading = trading_service["environment"]
    assert trading_service["depends_on"]["paper-order-orchestrator-mcp"] == {
        "condition": "service_healthy"
    }
    assert "MCP_TRADING_ORDER_API_KEY" in trading
    assert "DATABASE_URL" not in trading
    assert "CONTROL_DATABASE_URL" not in trading
    assert "TRADING_SERVICE_AUTH_SECRET" not in trading
    assert "TRADING_INTERNAL_SERVICE_AUTH_SECRET" not in trading

    dispatcher = root["services"]["kanban-dispatcher"]["environment"]
    assert "MCP_TRADING_ORDER_API_KEY" in dispatcher
    assert "TRADING_SERVICE_AUTH_SECRET" not in dispatcher
    assert root["services"]["kanban-dispatcher"]["depends_on"][
        "paper-order-orchestrator-mcp"
    ] == {"condition": "service_healthy"}


def test_risk_legal_mcp_is_available_to_dispatcher_spawned_risk_hermes() -> None:
    root = _yaml("docker-compose.yml")
    dispatcher = root["services"]["kanban-dispatcher"]

    assert "MCP_RISK_API_KEY" in dispatcher["environment"]
    assert dispatcher["depends_on"]["risk-mcp"] == {"condition": "service_healthy"}


def test_portfolio_image_is_readable_by_the_non_root_paper_mcp() -> None:
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")

    copy_index = dockerfile.index("COPY . .")
    permission_index = dockerfile.index("RUN chmod -R a+rX /app")
    final_import_check = dockerfile.index("RUN python -c 'import hermes_cli;")

    assert copy_index < permission_index < final_import_check


def test_trading_profile_and_prompts_pin_the_one_shot_paper_lane() -> None:
    config = _yaml("departments/02-trading/hermes/config.yaml")
    assert config["mcp_servers"]["user-paper-order"] == {
        "url": "http://paper-order-orchestrator-mcp:8046/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TRADING_ORDER_API_KEY}"},
        "enabled": True,
        "skip_preflight": True,
        "keepalive_interval": 60,
    }

    ceo = (ROOT / "departments/00-ceo-office/hermes/SOUL.md").read_text(
        encoding="utf-8"
    )
    trading = (ROOT / "departments/02-trading/hermes/SOUL.md").read_text(
        encoding="utf-8"
    )
    assert "hgfinance.user-paper-order-request.v1" in ceo
    assert "hgfinance.user-conditional-paper-rule.v1" in ceo
    assert "pre-created Trading primary" in ceo
    assert "Do not call `kanban_create`" in ceo
    assert "qa_required=false" in ceo
    assert "fail-closed Risk, QA, and approval gates" in ceo

    assert "hgfinance.user-paper-order-interpretation.v1" in trading
    assert "process_user_paper_order` exactly once" in trading
    assert "process_user_conditional_paper_rule` exactly once" in trading
    assert "get_user_conditional_paper_rule_status` exactly once" in trading
    for rejected in (
        "Questions/advice",
        "negation/prohibition",
        "conditional or hypothetical",
        "quoted examples",
        "LIVE/real-account",
        "multiple commands",
    ):
        assert rejected in trading
    assert "normal\nstrategy OrderIntent still requires" in trading
    assert "Mandatory terminal persistence for primary analysis" in trading
    assert "`result`: the complete Korean user-ready answer" in trading
    assert "`metadata.final_answer`: the same complete answer" in trading
    assert "A summary-only completion is invalid" in trading


def test_operator_docs_explain_the_internal_paper_only_boundary() -> None:
    runbook = (ROOT / "docs/02-engineering/HERMES_DOCKER_RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    baseline = (
        ROOT / "docs/02-engineering/LOCAL_COMPOSE_RUNTIME_BASELINE.md"
    ).read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for term in (
        "paper-order-orchestrator-mcp",
        "process_user_paper_order",
        "MCP_TRADING_ORDER_API_KEY",
        "LIVE 전환 경로는 없다",
        "ACCOUNTING_PENDING",
    ):
        assert term in runbook
    assert "GET /ui/paper-order-requests/{order_request_id}" in baseline
    assert "PAPER 전용 lane" in baseline
    assert "MCP_TRADING_ORDER_API_KEY=CHANGE_ME_MCP_TRADING_ORDER_API_KEY" in example
