"""Explicit adapters between workflow contracts and department runtimes."""

from .ceo import CeoAdapterError, LunaCeoAdapter
from .paper_e2e import (
    HermesSmokeAdapter,
    HermesSmokeError,
    build_paper_e2e_handlers,
)
from .paper_pipeline import PaperPipelineAdapter, build_paper_handlers
from .test_pipeline import build_test_handlers

__all__ = [
    "CeoAdapterError",
    "HermesSmokeAdapter",
    "HermesSmokeError",
    "LunaCeoAdapter",
    "PaperPipelineAdapter",
    "build_paper_e2e_handlers",
    "build_paper_handlers",
    "build_test_handlers",
]
