#!/usr/bin/env python3
"""Label a bounded redacted QA sample and print overclassification metrics."""

from __future__ import annotations

import argparse
import json

from orchestration.langsmith_feedback import FeedbackLedger
from orchestration.qa_feedback_labeling import label_sample


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--sample-size", type=int, default=40)
    args = parser.parse_args()
    result = label_sample(
        FeedbackLedger(args.state_path), sample_size=args.sample_size
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
