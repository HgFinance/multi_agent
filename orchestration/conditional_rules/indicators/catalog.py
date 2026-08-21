"""Catalog functions suitable for an MCP/API facade."""

from __future__ import annotations

from typing import Any

from .registry import DEFAULT_REGISTRY


def list_supported_indicators(*, source: str | None = None) -> list[dict[str, Any]]:
    return [definition.metadata() for definition in DEFAULT_REGISTRY.list(source=source)]


def get_indicator_definition(name: str) -> dict[str, Any]:
    definition = DEFAULT_REGISTRY.get(name)
    if definition is None:
        raise KeyError(f"unsupported indicator: {name}")
    return definition.metadata()
