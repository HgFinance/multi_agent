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
