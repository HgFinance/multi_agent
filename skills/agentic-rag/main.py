#!/usr/bin/env python3
"""CLI entry point for the agentic-rag skill.

Usage:
    python3 main.py --persona compliance-policy-agent --query "Can we open a new
    long position in SYMBOL_A today?" [--as-of 2026-07-29]

    python3 main.py --persona evidence-qa-agent --query "SYMBOL_A Q2 2026 revenue
    grew 14.2% year-over-year" [--as-of 2026-07-29]

Requires OPENAI_API_KEY in the environment (loaded from the calling profile's .env
by Hermes, or from a local .env via python-dotenv when run standalone).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(SKILL_DIR.parent.parent / ".env")

from src.graph import run_compliance_check  # noqa: E402

PERSONA_CORPUS = {
    "compliance-policy-agent": SKILL_DIR / "corpus" / "compliance",
    "evidence-qa-agent": SKILL_DIR / "corpus" / "evidence",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic RAG baseline (retrieve->grade->generate->hallucination-check->retry)")
    parser.add_argument("--persona", default="compliance-policy-agent", choices=sorted(PERSONA_CORPUS))
    parser.add_argument("--query", required=True, help="The compliance question, proposed order, or claim under review to check")
    parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="Point-in-Time date, YYYY-MM-DD")
    args = parser.parse_args()

    corpus_dir = PERSONA_CORPUS[args.persona]
    if not corpus_dir.exists():
        print(json.dumps({"error": f"No corpus configured for persona '{args.persona}'"}), file=sys.stderr)
        return 1

    result = run_compliance_check(query=args.query, as_of=args.as_of, corpus_dir=corpus_dir, persona=args.persona)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
