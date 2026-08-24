"""Compatibility import for the archived variant comparison renderer."""

from __future__ import annotations

from . import variant_manifest
from ._archived_compat import load_archived

_ARCHIVED = load_archived(
    "render_variant_comparison",
    aliases={"variant_manifest": variant_manifest},
)
VARIANTS = _ARCHIVED.VARIANTS
render = _ARCHIVED.render
main = _ARCHIVED.main

__all__ = ("VARIANTS", "main", "render")
