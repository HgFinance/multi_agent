"""Explicit adapter workflow contracts with lazy public imports.

The CEO Kanban supervisor is intentionally a small policy process. Importing
this package must not pull in the paper pipeline and its employee worker
runtime, because that runtime has the optional/heavy LangGraph dependency.
The public names remain available through module-level ``__getattr__`` so
existing ``from orchestration.adapters import ...`` callers keep working.
"""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "CeoAdapterError": (".ceo", "CeoAdapterError"),
    "LunaCeoAdapter": (".ceo", "LunaCeoAdapter"),
    "HermesSmokeAdapter": (".paper_e2e", "HermesSmokeAdapter"),
    "HermesSmokeError": (".paper_e2e", "HermesSmokeError"),
    "build_paper_e2e_handlers": (".paper_e2e", "build_paper_e2e_handlers"),
    "PaperPipelineAdapter": (".paper_pipeline", "PaperPipelineAdapter"),
    "build_paper_handlers": (".paper_pipeline", "build_paper_handlers"),
    "build_test_handlers": (".test_pipeline", "build_test_handlers"),
}


def __getattr__(name: str) -> Any:
    """Resolve an adapter only when a caller actually requests it."""

    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


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
