"""Explicit adapters between workflow contracts and department runtimes."""

from .paper_e2e import (
    HermesSmokeAdapter,
    HermesSmokeError,
    build_paper_e2e_handlers,
)

__all__ = [
    "HermesSmokeAdapter",
    "HermesSmokeError",
    "build_paper_e2e_handlers",
]

