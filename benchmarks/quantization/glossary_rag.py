#!/usr/bin/env python3
"""Deterministic arithmetic/structured-output glossary matching and injection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SCOPES = {"financial_arithmetic", "structured_output"}


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    definition: str
    scope: str
    unit: str | None = None


def load_glossary(path: Path) -> tuple[str, list[GlossaryEntry]]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("glossary must be a JSON array")
    entries = []
    for row in payload:
        if not isinstance(row, dict) or row.get("scope") not in ALLOWED_SCOPES:
            raise ValueError("glossary contains an out-of-scope entry")
        entries.append(GlossaryEntry(row["term"], row["definition"], row["scope"], row.get("unit")))
    return hashlib.sha256(raw).hexdigest(), entries


def inject(prompt: str, entries: list[GlossaryEntry]) -> tuple[str, list[str]]:
    lowered = prompt.casefold()
    hits = [entry for entry in entries if entry.term.casefold() in lowered]
    lines = ["GLOSSARY (deterministic matches only):"]
    lines.extend(f"- {entry.term}: {entry.definition}" for entry in hits)
    return "\n".join(lines) + "\n\n" + prompt, [entry.term for entry in hits]
