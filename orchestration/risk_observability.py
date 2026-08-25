"""Redacted, fail-open LangSmith spans shared by Risk and its projections."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator, Mapping
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

RISK_SPANS = frozenset(
    {
        "risk.advisory",
        "risk.mandate-load",
        "risk.portfolio-snapshot",
        "risk.market-snapshot",
        "risk.regime-classification",
        "risk.stop-calculation",
        "risk.take-profit-calculation",
        "risk.constraint-validation",
        "risk.discord-projection",
        "risk.notion-projection",
    }
)
_SAFE_KEYS = frozenset(
    {
        "task_id",
        "trace_id",
        "risk_plan_id",
        "mandate_version_id",
        "input_hash",
        "algorithm_version",
        "status",
        "stage",
        "target",
        "payload_hash",
        "duration_ms",
        "error",
    }
)


def _enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    } and bool(os.getenv("LANGSMITH_API_KEY", "").strip())


def _safe(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) in _SAFE_KEYS
        and (value is None or isinstance(value, (str, int, float, bool)))
    }


@lru_cache(maxsize=1)
def _client():
    from langsmith import Client

    return Client(hide_inputs=True, hide_outputs=True, hide_metadata=False)


@contextlib.contextmanager
def risk_span(name: str, metadata: Mapping[str, Any]) -> Iterator[Any]:
    """Emit one redacted span without allowing telemetry to affect execution."""

    if name not in RISK_SPANS:
        raise ValueError(f"unregistered Risk span: {name}")
    if not _enabled():
        yield None
        return
    started = time.perf_counter()
    try:
        from langsmith import trace

        context = trace(
            name,
            run_type="chain",
            inputs={},
            project_name=os.getenv("LANGSMITH_PROJECT", "").strip() or None,
            tags=["hgfinance", "risk", "redacted"],
            metadata=_safe(metadata),
            client=_client(),
        )
        run = context.__enter__()
    except Exception as exc:  # noqa: BLE001 - optional observer is fail-open
        logger.debug("Risk span unavailable: %s", type(exc).__name__)
        yield None
        return
    try:
        yield run
    except BaseException as exc:
        try:
            run.metadata.update(
                _safe(
                    {
                        "status": "error",
                        "error": type(exc).__name__,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    }
                )
            )
            context.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as observer_exc:  # noqa: BLE001
            logger.debug("Risk span error close failed: %s", type(observer_exc).__name__)
        raise
    else:
        try:
            completion = {
                "duration_ms": int((time.perf_counter() - started) * 1000)
            }
            if run.metadata.get("status") in {None, "", "running"}:
                completion["status"] = "success"
            run.metadata.update(_safe(completion))
            context.__exit__(None, None, None)
        except Exception as observer_exc:  # noqa: BLE001
            logger.debug(
                "Risk span success close failed: %s", type(observer_exc).__name__
            )


__all__ = ["RISK_SPANS", "risk_span"]
