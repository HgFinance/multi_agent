"""Load benchmark helpers retained in the historical experiment archive.

The active benchmark datasets and runners stay at the package root, while a
few legacy helpers were intentionally moved with the archived experiment.
This loader keeps the old import paths working without maintaining a second
copy of those helpers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ARCHIVE_SCRIPTS = (
    Path(__file__).resolve().parent / "archive" / "20260824-experimental" / "scripts"
)
_MISSING = object()


def load_archived(
    name: str, *, aliases: dict[str, ModuleType] | None = None
) -> ModuleType:
    """Load one archived helper and temporarily provide legacy imports."""

    path = _ARCHIVE_SCRIPTS / f"{name}.py"
    if not path.is_file():
        raise ModuleNotFoundError(f"archived benchmark helper is missing: {path}")

    spec = importlib.util.spec_from_file_location(f"_hgfinance_archived_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load archived benchmark helper: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = {alias: sys.modules.get(alias, _MISSING) for alias in (aliases or {})}
    try:
        for alias, value in (aliases or {}).items():
            sys.modules[alias] = value
        spec.loader.exec_module(module)
    finally:
        for alias, value in previous.items():
            if value is _MISSING:
                sys.modules.pop(alias, None)
            else:
                sys.modules[alias] = value
    return module
