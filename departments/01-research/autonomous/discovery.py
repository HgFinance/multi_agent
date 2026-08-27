"""Cheap, repeatable resource discovery for a research session."""

from __future__ import annotations

from pathlib import Path


EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "training_runs", "hgfinance-db-backups", ".hermes", ".codex",
}


def discover(repo_root: Path) -> list[dict[str, str]]:
    """Return a bounded resource map without reading data contents.

    The map is a routing aid, not evidence.  It deliberately reports paths and
    basic file classes so an agent can choose what to inspect next without
    pretending that the resource itself has already been validated.
    """

    root = repo_root.resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")
    found: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_dir():
            continue
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        kind = "other"
        detail = "available for inspection"
        if "data" in relative.parts or "dataset" in name or "market" in name:
            kind = "market-data-or-dataset"
        elif "strategy" in name or "experiment" in name or "research" in relative.parts:
            kind = "research-code"
        elif suffix in {".yaml", ".yml", ".json", ".toml", ".env"}:
            kind = "configuration"
        elif suffix in {".py", ".sql", ".sh"}:
            kind = "executable-or-schema"
        elif suffix in {".md", ".rst"}:
            kind = "research-note"
        found.append({"path": str(relative), "kind": kind, "detail": detail})
    # A resource map must remain useful even in a large repository.  Keep the
    # most relevant classes and cap each class; the agent can request a deeper
    # scan later when a question requires it.
    priority = {"market-data-or-dataset": 0, "research-code": 1, "configuration": 2, "research-note": 3, "executable-or-schema": 4, "other": 5}
    found.sort(key=lambda item: (priority.get(item["kind"], 99), item["path"]))
    selected: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for item in found:
        kind = item["kind"]
        if counts.get(kind, 0) >= 80:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        selected.append(item)
    return selected
