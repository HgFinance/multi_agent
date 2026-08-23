"""Run the portfolio pipeline from private control-database read-only data.

The filename is retained for operator compatibility; it no longer reads
``SUPABASE_DATABASE_URL`` or persistent ``research.documents`` rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run portfolio recommendation with control-database read-only PIT inputs"
    )
    parser.add_argument(
        "--profile-json",
        help="JSON object containing the user suitability profile; no credentials",
    )
    parser.add_argument(
        "--profile-file",
        type=Path,
        help="UTF-8 JSON file containing the user suitability profile",
    )
    parser.add_argument(
        "--as-of",
        help="Timezone-aware PIT cutoff; overrides profile.as_of when supplied",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="Run credential-free DNS/database/schema preflight without loading a profile",
    )
    parser.add_argument(
        "--replay-count",
        type=int,
        default=1,
        help="Repeat a completed PIT recommendation run for deterministic replay (1-3)",
    )
    return parser


def _load_profile(profile_json: str | None, profile_file: Path | None) -> dict:
    if (profile_json is not None) == (profile_file is not None):
        raise SystemExit("provide exactly one of --profile-json or --profile-file")
    raw = profile_json
    if profile_file is not None:
        try:
            raw = profile_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(
                f"cannot read --profile-file: {exc}; create the JSON file or use --profile-json"
            ) from exc
    try:
        profile = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        hint = ""
        if "\n" in (raw or "") and exc.lineno:
            hint = " Literal newlines are allowed between JSON fields, not inside quoted values."
        raise SystemExit(
            f"invalid profile JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.{hint}"
        ) from exc
    if not isinstance(profile, dict):
        raise SystemExit("profile JSON must decode to an object")
    return profile


def _load_readonly_adapter() -> Any:
    module_name = "portfolio_control_db_readonly_cli"
    path = ROOT / "departments/05-accounting-portfolio/portfolio/supabase_readonly.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit("control-database read-only adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.ControlDbReadOnlyAdapter()


def _replay_digest(result: dict[str, Any]) -> str:
    """Hash only deterministic recommendation and PIT provenance fields."""
    context = result.get("data_context", {})
    stable = {
        "suitability": result.get("suitability", {}),
        "risk_gate": result.get("risk_gate", {}),
        "qa_gate": result.get("qa_gate", {}),
        "data_context": {
            "source": context.get("source"),
            "as_of": context.get("as_of"),
            "quality_status": context.get("quality_status"),
            "reasons": context.get("reasons", []),
            "candidate_ids": [
                item.get("portfolio_id")
                for item in context.get("candidates", [])
                if isinstance(item, dict)
            ],
            "research_mode": context.get("research", {}).get("status"),
            "market_count": len(context.get("market", {}).get("snapshots", [])),
        },
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def main_async(argv: list[str] | None = None) -> int:
    """Run the CLI without leaking its `.env` into an embedding process."""
    previous_environment = dict(os.environ)
    try:
        return await _main_async_runtime(argv)
    finally:
        os.environ.clear()
        os.environ.update(previous_environment)


async def _main_async_runtime(argv: list[str] | None = None) -> int:
    # Keep tracing disabled and load local configuration only for an actual CLI run.
    # Importing this module in a test or another process must not mutate its environment
    # or initialize the workflow runtime.
    os.environ["LANGSMITH_TRACING"] = "false"

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:
        pass

    args = _parser().parse_args(argv)
    if args.diagnose_only:
        adapter = _load_readonly_adapter()
        diagnostics = await adapter.diagnose_connection()
        output: dict[str, object] = {
            "preflight": diagnostics.as_dict(),
            "external_writes": False,
        }
        snapshot_quality = "NOT_REQUESTED"
        if args.as_of:
            try:
                snapshot = await adapter.load_snapshot(as_of=args.as_of)
                context = snapshot.as_pipeline_context()
                output["snapshot"] = {
                    "source": context.get("source"),
                    "as_of": context.get("as_of"),
                    "quality_status": context.get("quality_status"),
                    "candidate_count": len(context.get("candidates", [])),
                    "research_mode": context.get("research", {}).get("status"),
                    "market_count": len(context.get("market", {}).get("snapshots", [])),
                    "reasons": context.get("reasons", []),
                    "data_diagnostics": context.get("data_diagnostics", {}),
                    "read_only": context.get("read_only"),
                    "external_writes": context.get("external_writes"),
                }
                snapshot_quality = str(context.get("quality_status", "FAIL"))
            except Exception as exc:  # noqa: BLE001 - sanitized CLI diagnostic.
                output["snapshot"] = {
                    "quality_status": "FAIL",
                    "reasons": [f"SNAPSHOT_PROBE_FAILED:{type(exc).__name__}"],
                }
                snapshot_quality = "FAIL"
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if diagnostics.status in {"PASS", "BYPASSED"} and snapshot_quality != "FAIL" else 2

    from orchestration.workflows.portfolio_recommendation import (
        run_portfolio_recommendation_pipeline_async,
    )
    profile = _load_profile(args.profile_json, args.profile_file)
    if not 1 <= args.replay_count <= 3:
        raise SystemExit("--replay-count must be between 1 and 3")
    if args.as_of:
        profile["as_of"] = args.as_of

    result = await run_portfolio_recommendation_pipeline_async(profile)
    if args.replay_count > 1:
        if result.get("pipeline_status") != "COMPLETED":
            result["replay"] = {
                "status": "SKIPPED_SAFE",
                "reason": "BASELINE_NOT_COMPLETED",
                "requested_runs": args.replay_count,
                "executed_runs": 1,
                "external_writes": False,
            }
        else:
            digests = [_replay_digest(result)]
            for _ in range(args.replay_count - 1):
                replay_result = await run_portfolio_recommendation_pipeline_async(profile)
                digests.append(_replay_digest(replay_result))
            deterministic = len(set(digests)) == 1
            result["replay"] = {
                "status": "PASS" if deterministic else "FAIL",
                "deterministic": deterministic,
                "requested_runs": args.replay_count,
                "executed_runs": len(digests),
                "digests": digests,
                "as_of": profile.get("as_of"),
                "external_writes": False,
            }
            if not deterministic:
                result["pipeline_status"] = "DEGRADED"
                result["safe_action"] = "HOLD"
                result["manual_review_required"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("pipeline_status") == "COMPLETED" else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
