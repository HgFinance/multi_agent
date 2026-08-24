"""Export QA-approved redacted feedback candidates for an offline benchmark.

This command is intentionally read-only. It never calls LangSmith, starts a
model, changes a prompt/router, or marks a candidate as passed. The offline
benchmark runner must use artifact_id as its idempotency key and report only
the bounded gate result through the QA benchmark endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from orchestration.langsmith_feedback import FEEDBACK_SCHEMA, FeedbackLedger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-path",
        default=os.getenv(
            "LANGSMITH_FEEDBACK_STATE_PATH",
            "/var/lib/portfolio/langsmith-feedback.sqlite3",
        ),
    )
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    ledger = FeedbackLedger(args.state_path)
    payload = {
        "schema_version": f"{FEEDBACK_SCHEMA}.benchmark-candidate.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": ledger.benchmark_candidates(max(1, min(args.limit, 100))),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
