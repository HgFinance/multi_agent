"""Repository-wide deterministic test environment.

Tests that use the portfolio BFF must opt into its unsigned fixture identity;
production defaults to verified Supabase JWT and never infers fixture mode from
pytest itself.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PORTFOLIO_AUTH_MODE", "fixture")
os.environ.setdefault("PORTFOLIO_AUTH_REQUIRED", "false")
os.environ.setdefault("PORTFOLIO_DATA_MODE", "test")
os.environ.setdefault(
    "PORTFOLIO_RUNTIME_STORE_PATH",
    os.path.join(
        tempfile.gettempdir(), f"hgfinance-portfolio-pytest-{os.getpid()}.sqlite3"
    ),
)
