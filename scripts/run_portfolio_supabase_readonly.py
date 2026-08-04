"""Run the portfolio pipeline from Supabase read-only data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run portfolio recommendation with Supabase read-only PIT inputs"
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
    return parser


def _load_profile(profile_json: str | None, profile_file: Path | None) -> dict:
    if (profile_json is not None) == (profile_file is not None):
        raise SystemExit("provide exactly one of --profile-json or --profile-file")
    raw = profile_json
    if profile_file is not None:
        try:
            raw = profile_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"cannot read --profile-file: {exc}") from exc
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


async def main_async(argv: list[str] | None = None) -> int:
    # Keep tracing disabled and load local configuration only for an actual CLI run.
    # Importing this module in a test or another process must not mutate its environment
    # or initialize the workflow runtime.
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:
        pass

    from orchestration.workflows.portfolio_recommendation import (
        run_portfolio_recommendation_pipeline_async,
    )
    args = _parser().parse_args(argv)
    profile = _load_profile(args.profile_json, args.profile_file)
    if args.as_of:
        profile["as_of"] = args.as_of

    result = await run_portfolio_recommendation_pipeline_async(profile)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("pipeline_status") == "COMPLETED" else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
