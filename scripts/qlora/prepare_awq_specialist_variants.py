#!/usr/bin/env python3
"""Split the approved AWQ SFT data into non-overlapping specialist datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=Path, required=True)
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--output-root", type=Path, required=True)
    args = ap.parse_args()
    train = load_jsonl(args.train)
    validation = load_jsonl(args.validation)
    variants = {
        "arithmetic": "financial_arithmetic",
        "structured": "structured_output",
    }
    manifest = {
        "schema_version": "awq-specialist-splits.v1",
        "source": {
            "train": str(args.train),
            "train_sha256": sha256(args.train),
            "validation": str(args.validation),
            "validation_sha256": sha256(args.validation),
        },
        "frozen_benchmark_files_modified": False,
        "variants": {},
    }
    for name, category in variants.items():
        out = args.output_root / name
        train_rows = [row for row in train if row.get("category") == category]
        validation_rows = [row for row in validation if row.get("category") == category]
        train_hash = write_jsonl(out / "train.jsonl", train_rows)
        validation_hash = write_jsonl(out / "validation.jsonl", validation_rows)
        manifest["variants"][name] = {
            "category": category,
            "train_count": len(train_rows),
            "validation_count": len(validation_rows),
            "train_sha256": train_hash,
            "validation_sha256": validation_hash,
            "selection_rule": f"category == {category!r}; inherited contamination PASS from approved source manifest",
        }
    args.output_root.mkdir(parents=True, exist_ok=True)
    (args.output_root / "specialist_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
