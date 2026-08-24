"""Compatibility import for the archived variant admission helper."""

from __future__ import annotations

from ._archived_compat import load_archived

_ARCHIVED = load_archived("variant_manifest")
ADAPTER_BASE_REQUIRED = _ARCHIVED.ADAPTER_BASE_REQUIRED
FROZEN_DATASETS = _ARCHIVED.FROZEN_DATASETS
PRIMARY_EXTERNAL_METRIC = _ARCHIVED.PRIMARY_EXTERNAL_METRIC
REQUIRED_CHECKS = _ARCHIVED.REQUIRED_CHECKS
adapter_compatibility = _ARCHIVED.adapter_compatibility
admit_manifest = _ARCHIVED.admit_manifest
external_gate = _ARCHIVED.external_gate
load_manifest = _ARCHIVED.load_manifest
sha256_file = _ARCHIVED.sha256_file

__all__ = (
    "ADAPTER_BASE_REQUIRED",
    "FROZEN_DATASETS",
    "PRIMARY_EXTERNAL_METRIC",
    "REQUIRED_CHECKS",
    "adapter_compatibility",
    "admit_manifest",
    "external_gate",
    "load_manifest",
    "sha256_file",
)
