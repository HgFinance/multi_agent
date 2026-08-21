#!/usr/bin/env python3
"""Validate provenance, adapter compatibility, and fail-closed admission."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FROZEN_DATASETS = {
    "internal50_v2": "ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978",
}
REQUIRED_CHECKS = ("contamination", "license", "format", "duplicates", "purpose_fit")
ADAPTER_BASE_REQUIRED = ("base_model", "base_revision", "quantization")
PRIMARY_EXTERNAL_METRIC = "overall_accuracy"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapter_compatibility(manifest: dict[str, Any]) -> tuple[bool, str]:
    compatibility = manifest.get("adapter_compatibility")
    if not isinstance(compatibility, dict):
        return False, "adapter_compatibility is required"
    missing = [key for key in ADAPTER_BASE_REQUIRED if not compatibility.get(key)]
    if missing:
        return False, f"missing adapter compatibility fields: {', '.join(missing)}"
    expected = manifest.get("served_base")
    if not isinstance(expected, dict):
        return False, "served_base is required"
    for key in ADAPTER_BASE_REQUIRED:
        if compatibility.get(key) != expected.get(key):
            return False, f"adapter/base mismatch: {key}"
    return True, ""


def admit_manifest(manifest: dict[str, Any]) -> tuple[str, list[str]]:
    errors: list[str] = []
    if manifest.get("variant") not in {"awq-finetune", "awq-reasoning", "awq-rag"}:
        errors.append("unknown variant")
    if manifest.get("status") not in {"candidate", "validated", "hold"}:
        errors.append("invalid status")
    checks = manifest.get("checks")
    if not isinstance(checks, dict):
        errors.append("checks must be an object")
    else:
        for check in REQUIRED_CHECKS:
            if checks.get(check) is not True:
                errors.append(f"check failed: {check}")
    if not manifest.get("source_hashes"):
        errors.append("source_hashes must be non-empty")
    if manifest.get("held_out_overlap") is not False:
        errors.append("held_out_overlap must be false")
    if manifest.get("variant") == "awq-finetune":
        compatible, reason = adapter_compatibility(manifest)
        if not compatible:
            errors.append(reason)
    return ("HOLD", errors) if errors else ("ADMIT", [])


def external_gate(
    *, overall_accuracy: float | None, fp8_overall_accuracy: float,
    critical_failures: int, new_critical_regressions: int, request_errors: int,
) -> tuple[str, list[str]]:
    """Apply frozen External Overall gate; Auto Mean stays diagnostic."""
    errors: list[str] = []
    if overall_accuracy is None:
        errors.append(f"{PRIMARY_EXTERNAL_METRIC} is unavailable")
    elif overall_accuracy < fp8_overall_accuracy * 0.97:
        errors.append("external overall relative degradation exceeds 3%")
    if critical_failures > 0:
        errors.append("critical failures present")
    if new_critical_regressions > 0:
        errors.append("new critical regression")
    if request_errors > 0:
        errors.append("request errors present")
    return ("PASS", []) if not errors else ("HOLD", errors)


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    return payload


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    decision, errors = admit_manifest(load_manifest(args.manifest))
    print(json.dumps({"decision": decision, "errors": errors}, ensure_ascii=False))
    return 0 if decision == "ADMIT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
