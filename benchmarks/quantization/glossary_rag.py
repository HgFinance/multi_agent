#!/usr/bin/env python3
"""Deterministic arithmetic/structured-output glossary matching and injection."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SCOPES = {"financial_arithmetic", "structured_output"}
_STOP_ALIASES = {"a", "an", "and", "of", "or", "the", "to", "two", "group"}

# Version-pinned, answer-key-free additions used by the selective Hybrid
# evaluation.  These are definitions and routing context, not benchmark
# answers or case-specific rules.
SELECTIVE_V2_ENTRIES = (
    {
        "term": "ROA",
        "definition": (
            "Return on assets (ROA) is a profitability ratio. Unless the supplied "
            "context specifies another convention, use net income divided by the "
            "relevant average total assets and preserve the requested percentage "
            "or ratio scale."
        ),
        "scope": "financial_arithmetic",
        "aliases": ["return on assets", "return on asset"],
    },
    {
        "term": "fixed asset turnover",
        "definition": (
            "Fixed asset turnover relates revenue to the fixed-asset base. Use the "
            "denominator and period explicitly supplied by the context; do not "
            "replace average fixed assets with an ending balance without evidence."
        ),
        "scope": "financial_arithmetic",
        "aliases": ["fixed assets turnover", "asset turnover"],
    },
    {
        "term": "quick ratio",
        "definition": (
            "Quick ratio measures liquid assets relative to current liabilities. "
            "Use the liquid-asset components and the denominator stated in the "
            "context, and distinguish a numeric ratio from a requested yes/no "
            "interpretation."
        ),
        "scope": "financial_arithmetic",
        "aliases": ["acid test ratio", "acid-test ratio"],
    },
    {
        "term": "gross margin applicability",
        "definition": (
            "Gross margin is revenue less cost of goods sold divided by revenue. "
            "For a financial institution, do not assume ordinary cost of goods sold "
            "exists; state that the metric is not applicable unless the supplied "
            "evidence defines the required components."
        ),
        "scope": "financial_arithmetic",
        "aliases": ["gross margin relevance", "financial institution margin"],
    },
    {
        "term": "fiscal-year mapping",
        "definition": (
            "Fiscal-year mapping aligns each table value with the exact fiscal year "
            "or reporting date named in the question. Compare like periods only and "
            "do not infer a year from row order when labels are available."
        ),
        "scope": "financial_arithmetic",
        "aliases": ["fiscal year mapping", "reporting-period mapping"],
    },
)


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    definition: str
    scope: str
    unit: str | None = None
    aliases: tuple[str, ...] = ()


def load_glossary(path: Path) -> tuple[str, list[GlossaryEntry]]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("glossary must be a JSON array")
    entries = []
    for row in payload:
        if not isinstance(row, dict) or row.get("scope") not in ALLOWED_SCOPES:
            raise ValueError("glossary contains an out-of-scope entry")
        aliases = row.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias.strip() for alias in aliases):
            raise ValueError("glossary aliases must be a list of non-empty strings")
        entries.append(
            GlossaryEntry(
                row["term"], row["definition"], row["scope"], row.get("unit"), tuple(aliases)
            )
        )
    return hashlib.sha256(raw).hexdigest(), entries


def load_selective_v2_glossary(path: Path) -> tuple[str, list[GlossaryEntry]]:
    """Load the existing glossary plus generic v2 definitions.

    The digest covers both the source file and the immutable supplement so
    provenance changes whenever either knowledge source changes.
    """

    digest, entries = load_glossary(path)
    additions = [
        GlossaryEntry(
            row["term"], row["definition"], row["scope"], row.get("unit"), tuple(row.get("aliases", []))
        )
        for row in SELECTIVE_V2_ENTRIES
    ]
    supplement_bytes = json.dumps(
        SELECTIVE_V2_ENTRIES, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    combined_digest = hashlib.sha256(
        path.read_bytes() + b"\n-- selective-v2 supplement --\n" + supplement_bytes
    ).hexdigest()
    return combined_digest, [*entries, *additions]


def inject(
    prompt: str,
    entries: list[GlossaryEntry],
    *,
    query: str | None = None,
) -> tuple[str, list[str]]:
    """Inject entries matched against a focused query, not the whole corpus."""

    lowered = (query if query is not None else prompt).casefold()

    def matches(candidate: str) -> bool:
        candidate = candidate.strip()
        if not candidate:
            return False
        if candidate.casefold() in _STOP_ALIASES:
            return False
        # Avoid substring collisions such as matching ``G2`` inside ``Q2``.
        # Word boundaries are applied to ASCII terms while Korean terms retain
        # their normal Unicode word boundaries.
        if re.fullmatch(r"[\w .&()/-]+", candidate, flags=re.UNICODE):
            pattern = r"(?<![\w])" + re.escape(candidate.casefold()) + r"(?![\w])"
            return re.search(pattern, lowered) is not None
        return candidate.casefold() in lowered

    hits = [
        entry
        for entry in entries
        if any(matches(candidate) for candidate in (entry.term, *entry.aliases))
    ]
    lines = ["GLOSSARY (deterministic matches only):"]
    lines.extend(f"- {entry.term}: {entry.definition}" for entry in hits)
    return "\n".join(lines) + "\n\n" + prompt, [entry.term for entry in hits]
