#!/usr/bin/env python3
"""Build and verify the immutable factory runtime contract.

The factory autopilot and experiment worker intentionally share one image.  A
manual ``docker cp`` hotfix can nevertheless make their writable container
layers diverge.  This contract seals the code that turns proposals into ASTs
and executes them, then lets Docker health checks detect any post-build drift.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


SCHEMA = "factory-runtime-contract.v1"
DEFAULT_ROOT = Path("/app")
DEFAULT_MANIFEST = DEFAULT_ROOT / ".factory-runtime-contract.json"

CRITICAL_FILES = (
    "departments/01-research/contracts/alpha_semantics.py",
    "departments/01-research/contracts/intraday_ast_contract.py",
    "departments/01-research/factory/lead_intake.py",
    "departments/01-research/factory/formula_discovery.py",
    "departments/01-research/factory/literature_derivation.py",
    "departments/01-research/factory/proposal_intake.py",
    "departments/01-research/factory/factory_autopilot.py",
    "departments/01-research/factory/intraday_experience.py",
    "departments/04-quant-backtest/pipeline/alpha_ast.py",
    "departments/04-quant-backtest/pipeline/config_binding.py",
    "departments/04-quant-backtest/pipeline/factory_bridge.py",
    "departments/04-quant-backtest/pipeline/backtest_runner.py",
    "departments/04-quant-backtest/pipeline/db_writer.py",
    "departments/04-quant-backtest/pipeline/experiment_orchestrator.py",
    "departments/04-quant-backtest/pipeline/experiment_worker.py",
    "departments/04-quant-backtest/pipeline/intraday_alpha_ast.py",
    "departments/04-quant-backtest/pipeline/intraday_candidate.py",
    "departments/04-quant-backtest/pipeline/intraday_experiment_runner.py",
    "departments/04-quant-backtest/pipeline/intraday_microstructure.py",
    "departments/04-quant-backtest/pipeline/job_queue.py",
    "departments/04-quant-backtest/pipeline/strategy_lifecycle.py",
    "departments/04-quant-backtest/pipeline/trial_family.py",
)

# These fields are the current executable microstructure contract.  They are
# deliberately checked independently of the manifest, so rebuilding an old
# tree cannot silently bless the exact failure observed on 2026-08-15.
REQUIRED_AST_FIELDS = frozenset(
    {
        "traded_value",
        "traded_volume",
        "ofi_close",
        "ofi_open",
        "ofi_intraday_std",
        "close_vs_vwap",
        "spread_close_ratio",
        "depth_imbalance_l1",
        "depth_imbalance_l10",
        "depth_imbalance_slope",
        "size_weighted_ofi",
        "book_depth_notional_l1",
        "book_depth_notional_l10",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_alpha_ast(root: Path):
    path = root / "departments/04-quant-backtest/pipeline/alpha_ast.py"
    spec = importlib.util.spec_from_file_location("factory_contract_alpha_ast", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load alpha_ast from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_manifest(root: Path) -> dict:
    missing = [name for name in CRITICAL_FILES if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"factory runtime files missing: {missing}")

    alpha_ast = _load_alpha_ast(root)
    fields = tuple(sorted(alpha_ast.FIELDS))
    absent = sorted(REQUIRED_AST_FIELDS.difference(fields))
    if absent:
        raise RuntimeError(f"required AST fields missing: {absent}")

    return {
        "schema": SCHEMA,
        "alpha_ast_module_version": alpha_ast.MODULE_VERSION,
        "alpha_ast_fields": fields,
        "files": {name: _sha256(root / name) for name in CRITICAL_FILES},
    }


def verify_manifest(root: Path, manifest: dict) -> dict:
    if manifest.get("schema") != SCHEMA:
        raise RuntimeError(f"unsupported factory runtime contract: {manifest.get('schema')!r}")

    current = build_manifest(root)
    drift = {
        name: {"expected": expected, "actual": current["files"].get(name)}
        for name, expected in manifest.get("files", {}).items()
        if current["files"].get(name) != expected
    }
    if set(manifest.get("files", {})) != set(current["files"]):
        drift["<file-set>"] = {
            "expected": sorted(manifest.get("files", {})),
            "actual": sorted(current["files"]),
        }
    expected_fields = tuple(manifest.get("alpha_ast_fields", ()))
    if expected_fields != current["alpha_ast_fields"]:
        drift["<alpha-ast-fields>"] = {
            "expected": expected_fields,
            "actual": current["alpha_ast_fields"],
        }
    if drift:
        raise RuntimeError("factory runtime drift: " + json.dumps(drift, sort_keys=True))
    return current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("write", "check"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    if args.mode == "write":
        manifest = build_manifest(args.root)
        args.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        manifest = verify_manifest(args.root, manifest)

    print(
        json.dumps(
            {
                "status": "ok",
                "schema": manifest["schema"],
                "files": len(manifest["files"]),
                "alpha_ast_fields": len(manifest["alpha_ast_fields"]),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
