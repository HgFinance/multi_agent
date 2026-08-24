#!/usr/bin/env python3
"""Render five-variant quality, gate, and provenance tables."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .variant_manifest import external_gate
except ImportError:
    from variant_manifest import external_gate

VARIANTS = ("FP8", "AWQ", "AWQ+Finetune", "AWQ+Reasoning", "AWQ+RAG")


def _value(payload: dict[str, Any], *keys: str, default: Any = "HOLD") -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def render(paths: dict[str, Path], external_paths: dict[str, Path] | None = None) -> str:
    payloads = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in paths.items()}
    external = {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in (external_paths or {}).items()
    }
    rows = [("metric", *VARIANTS)]
    metrics = (
        ("Internal Quality", "accuracy"),
        ("Critical Failures", "critical_failed_n"),
        ("Request Errors", "error_n"),
        ("External Overall", "overall_accuracy"),
        ("External Auto Mean", "auto_mean_score"),
        ("Avg Latency (s)", "avg_latency_s"),
        ("Status", "status"),
    )
    for label, key in metrics:
        rows.append((label, *(_value(external.get(name, {}), key, default=_value(payloads.get(name, {}), key)) for name in VARIANTS)))
    rows.append(("Primary Gate", *(_gate(name, payloads, external) for name in VARIANTS)))
    rows.append(("Provenance", *(_provenance(payloads.get(name, {}), external.get(name, {})) for name in VARIANTS)))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(" | ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)) for row in rows)


def _gate(name: str, payloads: dict[str, dict[str, Any]], external: dict[str, dict[str, Any]]) -> str:
    if name in {"FP8", "AWQ"}:
        return "BASELINE"
    score = external.get(name, {}).get("overall_accuracy")
    fp8_score = external.get("FP8", {}).get("overall_accuracy")
    if fp8_score is None:
        return "HOLD: missing frozen Overall baseline"
    status, errors = external_gate(
        overall_accuracy=score, fp8_overall_accuracy=fp8_score,
        critical_failures=int(payloads.get(name, {}).get("critical_failed_n", 0) or 0),
        new_critical_regressions=int(payloads.get(name, {}).get("new_critical_regressions", 0) or 0),
        request_errors=int(payloads.get(name, {}).get("error_n", 0) or 0),
    )
    return status if not errors else f"{status}: {'; '.join(errors)}"


def _provenance(payload: dict[str, Any], external: dict[str, Any]) -> str:
    manifest = payload.get("manifest") or external.get("manifest")
    if not isinstance(manifest, dict):
        return "HOLD: missing manifest"
    return f"{manifest.get('name', 'unnamed')}@{manifest.get('version', 'unknown')}#{manifest.get('sha256', 'unknown')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", action="append", nargs=2, metavar=("VARIANT", "PATH"), required=True)
    parser.add_argument("--external", action="append", nargs=2, metavar=("VARIANT", "PATH"))
    args = parser.parse_args()
    print(render({variant: Path(path) for variant, path in args.result}, {variant: Path(path) for variant, path in (args.external or [])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
