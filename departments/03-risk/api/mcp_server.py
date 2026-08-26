"""Authenticated, read-only legal LLM-Wiki boundary for Risk Hermes.

The server owns no retrieval or generation logic. It reuses the Risk query
classifier and ``query_legal_wiki`` so the Hermes and mandate entry paths have
one definition of legal routing and one LLM-Wiki implementation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

_RISK_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(_REPO_ROOT), str(_RISK_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from apps.security.mcp_bearer_auth import (  # noqa: E402
    BearerAuthMiddleware,
    validate_api_key,
)
from risk_mandate_workers import classify_compliance_query_mode  # noqa: E402
from tools.legal_wiki_tool import (  # noqa: E402
    LegalWikiAnswerFn,
    LegalWikiQueryInput,
    query_legal_wiki,
)

MCP_PORT = 8047
MCP_PATH = "/mcp"
SCHEMA_VERSION = "risk.hermes-legal-query.v1"
_LEGAL_MODES = frozenset({"LEGAL_QUERY", "MIXED_REVIEW"})


def execute_legal_query(
    question: str,
    as_of: str,
    *,
    answer_fn: LegalWikiAnswerFn | None = None,
) -> dict[str, Any]:
    """Guard and execute one legal query without duplicating the Wiki path."""

    normalized_question = str(question or "").strip()
    if not normalized_question or len(normalized_question) > 4000:
        raise ValueError("question must contain between 1 and 4000 characters")
    try:
        effective_date = date.fromisoformat(str(as_of).strip())
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date (YYYY-MM-DD)") from exc

    query_mode, routing_rationale = classify_compliance_query_mode(
        normalized_question
    )
    if query_mode not in _LEGAL_MODES:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "NOT_APPLICABLE",
            "query_mode": query_mode,
            "routing_rationale": routing_rationale,
            "llm_wiki_invoked": False,
            "escalate": False,
            "pages_visited": [],
        }

    result = query_legal_wiki(
        LegalWikiQueryInput(query=normalized_question, as_of=effective_date),
        answer_fn=answer_fn,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "query_mode": query_mode,
        "routing_rationale": routing_rationale,
        "llm_wiki_invoked": True,
        **result.model_dump(mode="json"),
    }


def build_server(*, host: str = "0.0.0.0", port: int = MCP_PORT):
    """Build the single-capability Risk legal MCP server."""

    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        name="hgfinance-risk-legal",
        instructions=(
            "Read-only Korean financial-law evidence tool for Risk Hermes. "
            "Use it only for statutes, regulations, cases, legal duties or "
            "possible legal breaches. Numeric market-risk and ordinary policy "
            "questions are not legal queries and must not invoke LLM-Wiki."
        ),
        host=host,
        port=port,
        streamable_http_path=MCP_PATH,
    )

    @server.tool(
        name="query_risk_legal_wiki",
        description=(
            "Retrieve and assess Korean financial-law evidence for a genuinely "
            "legal or regulatory question. Supply the point-in-time date as "
            "YYYY-MM-DD. Do not call this for VaR, exposure, volatility, position "
            "sizing or other ordinary numeric risk questions. The server applies "
            "a deterministic legal-scope guard before any LLM-Wiki/model call."
        ),
        structured_output=True,
    )
    async def query_risk_legal_wiki(question: str, as_of: str) -> dict[str, Any]:
        return await asyncio.to_thread(execute_legal_query, question, as_of)

    return server


def build_app(server: Any, *, api_key: str) -> BearerAuthMiddleware:
    """Protect the complete Streamable HTTP surface."""

    return BearerAuthMiddleware(
        server.streamable_http_app(),
        api_key=api_key,
        credential_name="MCP_RISK_API_KEY",
    )


def check_readiness() -> None:
    """Validate auth, imports and the frozen legal Wiki corpus."""

    validate_api_key(
        os.environ.get("MCP_RISK_API_KEY"),
        credential_name="MCP_RISK_API_KEY",
    )
    import numpy  # noqa: F401 - this import was the original runtime failure.

    wiki_dir = _RISK_DIR / "experiments" / "llm_wiki" / "data" / "wiki"
    if not wiki_dir.is_dir() or not any(wiki_dir.glob("*.md")):
        raise RuntimeError("legal LLM-Wiki corpus is not ready")


def main() -> None:
    """Run the internal-only Streamable HTTP MCP server."""

    if sys.argv[1:] == ["--healthcheck"]:
        check_readiness()
        return
    if sys.argv[1:]:
        raise SystemExit("usage: python api/mcp_server.py [--healthcheck]")

    import uvicorn

    api_key = validate_api_key(
        os.environ.get("MCP_RISK_API_KEY"),
        credential_name="MCP_RISK_API_KEY",
    )
    server = build_server()
    uvicorn.run(
        build_app(server, api_key=api_key),
        host="0.0.0.0",
        port=MCP_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "MCP_PATH",
    "MCP_PORT",
    "SCHEMA_VERSION",
    "build_app",
    "build_server",
    "check_readiness",
    "execute_legal_query",
]
