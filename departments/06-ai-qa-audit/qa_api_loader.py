"""Load the QA API under a domain-owned module name.

The domain is still executable as ``uvicorn app:app --app-dir api`` for
backward compatibility, but internal workers and tests must not import the
generic ``api.app`` name.  Risk and QA can run in the same Python process
without whichever module was imported first winning.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


_MODULE_NAME = "hgfinance_qa_audit.api_app"
_APP_PATH = Path(__file__).resolve().parent / "api" / "app.py"


def load_qa_api() -> ModuleType:
    """Return the one QA API module loaded from this checkout."""

    current = sys.modules.get(_MODULE_NAME)
    if current is not None:
        return current
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _APP_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ImportError(f"cannot load QA API from {_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module

