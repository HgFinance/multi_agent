"""Central route registry for future API expansion."""

from fastapi import APIRouter

from backend.app.api.v1.endpoints.agent import router as agent_router
from backend.app.api.v1.endpoints.portfolio import router as portfolio_router

api_router = APIRouter()
api_router.include_router(portfolio_router)
api_router.include_router(agent_router)
