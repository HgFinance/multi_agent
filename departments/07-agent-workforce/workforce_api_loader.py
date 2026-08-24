"""Load the Workforce API without using the process-wide ``app`` name."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


_MODULE_NAME = "hgfinance_workforce.api_app"
_APP_PATH = Path(__file__).resolve().parent / "api" / "app.py"


def load_workforce_api() -> ModuleType:
    """Return the one Workforce API module loaded from this checkout."""

    current = sys.modules.get(_MODULE_NAME)
    if current is not None:
        return current
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _APP_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ImportError(f"cannot load Workforce API from {_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module

