"""Investment-head Agent ask routers.

부서가 소유한 Hermes Profile을 명시적인 Router에 고정한다. 요청 본문으로
department를 받지 않으며, Agent 응답은 수치·주문·원장 상태의 authoritative
source가 아니다.
"""

from __future__ import annotations

from fastapi import APIRouter

try:  # ``python apps/api/main.py``와 package import를 모두 지원한다.
    import hermes_cli
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api import hermes_cli


router = APIRouter(tags=["investment-department-agents"])


def _ask(department: str, config: str, query: hermes_cli.AgentAsk) -> dict:
    return hermes_cli.ask(department=department, config=config, query=query.query)


@router.post("/research/agent/ask", operation_id="research_agent_ask")
def research_agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """리서치본부 Hermes Head에 질의한다. 자료 수집·주문·원장 변경은 수행하지 않는다."""

    return _ask("research-department", "departments/01-research/hermes/config.yaml", req)


@router.post("/risk/agent/ask", operation_id="risk_agent_ask")
def risk_agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """리스크관리본부에 비구속적 리스크 질의를 전달한다."""

    return _ask("risk-management", "departments/03-risk/hermes/config.yaml", req)


@router.post("/quant/agent/ask", operation_id="quant_agent_ask")
def quant_agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """퀀트·백테스트본부에 분석 질의를 전달한다. Production 승격은 하지 않는다."""

    return _ask("quant-backtest-department", "departments/04-quant-backtest/hermes/config.yaml", req)


@router.post("/qa/agent/ask", operation_id="qa_agent_ask")
def qa_agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """AI QA·감사본부에 근거·계약 검증 질의를 전달한다."""

    return _ask("qa-department", "departments/06-ai-qa-audit/hermes/config.yaml", req)


__all__ = ["router"]
