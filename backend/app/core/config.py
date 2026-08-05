"""Environment-backed settings for the standalone backend API."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Runtime configuration with safe local defaults."""

    app_name: str = "HgFinance Platform Backend"
    api_prefix: str = "/api/v1"
    langgraph_base_url: str = "http://localhost:8001"
    langgraph_health_path: str = "/health"
    langgraph_invoke_path: str = "/api/v1/agent/invoke"
    langgraph_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("BACKEND_APP_NAME", cls.model_fields["app_name"].default),
            api_prefix=os.getenv("BACKEND_API_PREFIX", cls.model_fields["api_prefix"].default),
            langgraph_base_url=os.getenv("LANGGRAPH_BASE_URL", cls.model_fields["langgraph_base_url"].default),
            langgraph_health_path=os.getenv("LANGGRAPH_HEALTH_PATH", cls.model_fields["langgraph_health_path"].default),
            langgraph_invoke_path=os.getenv("LANGGRAPH_INVOKE_PATH", cls.model_fields["langgraph_invoke_path"].default),
            langgraph_timeout_seconds=float(
                os.getenv(
                    "LANGGRAPH_TIMEOUT_SECONDS",
                    str(cls.model_fields["langgraph_timeout_seconds"].default),
                )
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable-enough settings snapshot per process."""

    return Settings.from_env()
