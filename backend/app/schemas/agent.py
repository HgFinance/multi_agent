"""HTTP contracts for the LangGraph adapter boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentHealthResponse(BaseModel):
    status: str
    upstream: str
    detail: str | None = None


class AgentInvokeResponse(BaseModel):
    status: str
    department: str
    result: dict[str, Any]
