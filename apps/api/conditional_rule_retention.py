"""Standalone maintenance worker for conditional PAPER rule history."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from orchestration.conditional_rules.retention import ConditionalRuleRetentionStore

LOG = logging.getLogger("conditional-rule-retention")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = ConditionalRuleRetentionStore.from_env()
    if args.healthcheck:
        store.check_ready()
        print("conditional-rule-retention ready")
        return 0
    interval = max(float(os.getenv("CONDITIONAL_RULE_RETENTION_INTERVAL_SECONDS", "86400")), 60.0)
    if args.once:
        print(json.dumps(store.run_once().__dict__, sort_keys=True))
        return 0
    while True:
        result = store.run_once()
        LOG.info("conditional rule retention cycle", extra=result.__dict__)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
