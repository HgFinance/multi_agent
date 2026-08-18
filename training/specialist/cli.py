"""CPU-safe dataset preparation CLI used by the QLoRA entrypoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contamination import check_contamination, require_clean
from .mixing import load_pool, mix_pools, write_jsonl
from .schema import load_jsonl


def _pool_args(parser: argparse.ArgumentParser, name: str, *, required: bool = True) -> None:
    parser.add_argument(f"--{name}-train", type=Path, required=required)
    parser.add_argument(f"--{name}-validation", type=Path, required=required)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare deterministic HgFinance specialist datasets without model downloads"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a Qwen messages JSONL file")
    validate.add_argument("dataset", type=Path)
    validate.add_argument("--name", default="dataset")

    contam = sub.add_parser("check-contamination", help="check data against held-out benchmarks")
    contam.add_argument("dataset", type=Path)
    contam.add_argument("benchmark_root", type=Path)

    mix = sub.add_parser("mix", help="mix Common and department pools deterministically")
    _pool_args(mix, "common")
    _pool_args(mix, "department")
    _pool_args(mix, "general", required=False)
    mix.add_argument("--output", type=Path, required=True)
    mix.add_argument("--metadata-output", type=Path, required=True)
    mix.add_argument("--benchmark-root", type=Path, required=True)
    mix.add_argument("--target-train-size", type=int, required=True)
    mix.add_argument("--common-ratio", type=float, default=0.25)
    mix.add_argument("--general-ratio", type=float, default=0.15)
    mix.add_argument("--department-ratio", type=float, default=0.60)
    mix.add_argument("--seed", type=int, default=66)
    mix.add_argument("--allow-replacement", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        records = load_jsonl(args.dataset, source_dataset=args.name)
        print(json.dumps({"status": "PASS", "count": len(records)}, indent=2))
        return 0
    if args.command == "check-contamination":
        records = load_jsonl(args.dataset, source_dataset="candidate")
        result = check_contamination(records, args.benchmark_root)
        require_clean(result)
        print(json.dumps(result, indent=2))
        return 0

    if (args.general_train is None) != (args.general_validation is None):
        raise SystemExit("--general-train and --general-validation must be supplied together")
    pools = {
        "common": load_pool("common", args.common_train, args.common_validation),
        "department": load_pool("department", args.department_train, args.department_validation),
    }
    ratios = {
        "common": args.common_ratio,
        "general": args.general_ratio,
        "department": args.department_ratio,
    }
    if args.general_train is not None:
        pools["general"] = load_pool("general", args.general_train, args.general_validation)
    else:
        ratios["general"] = 0.0
    records, metadata = mix_pools(
        pools,
        ratios=ratios,
        target_size=args.target_train_size,
        seed=args.seed,
        allow_replacement=args.allow_replacement,
        benchmark_root=args.benchmark_root,
    )
    write_jsonl(records, args.output)
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "output": str(args.output), "metadata": str(args.metadata_output)}, indent=2))
    return 0
