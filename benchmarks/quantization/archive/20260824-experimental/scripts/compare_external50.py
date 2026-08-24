#!/usr/bin/env python3
"""Print a read-only comparison table for two scored External-50 results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


METRICS = (
    ("n", "n"),
    ("passed", "passed"),
    ("pass_rate", "pass_rate"),
    ("mean_score", "mean_score"),
    ("avg_latency_s", "avg_latency_s"),
)


def load_result(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError(f"{path}: expected a result object with a results list")
    return payload


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def rows(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    output = [(label, payload.get(key)) for label, key in METRICS]
    sources = payload.get("sources")
    if isinstance(sources, dict):
        for source in sorted(sources):
            source_metrics = sources[source]
            if not isinstance(source_metrics, dict):
                continue
            for label, key in METRICS[1:]:
                output.append((f"{source}.{label}", source_metrics.get(key)))
    return output


def print_table(fp8: dict[str, Any], awq: dict[str, Any]) -> None:
    fp8_rows = dict(rows(fp8))
    awq_rows = dict(rows(awq))
    labels = list(fp8_rows)
    for label in awq_rows:
        if label not in fp8_rows:
            labels.append(label)
    table = [("metric", "FP8", "AWQ")]
    table.extend((label, format_value(fp8_rows.get(label)), format_value(awq_rows.get(label))) for label in labels)
    widths = [max(len(row[index]) for row in table) for index in range(3)]
    separator = "-+-".join("-" * width for width in widths)
    print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(table[0])))
    print(separator)
    for row in table[1:]:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a read-only table comparing FP8 and AWQ External-50 result files."
    )
    parser.add_argument("fp8_result", type=Path, help="FP8 final result JSON")
    parser.add_argument("awq_result", type=Path, help="AWQ final result JSON")
    args = parser.parse_args(argv)
    print_table(load_result(args.fp8_result), load_result(args.awq_result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
