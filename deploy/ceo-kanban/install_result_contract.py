"""Install the canonical Kanban completion-result contract in Hermes.

Hermes owns the Kanban transaction. HgFinance only tightens the handoff
contract at that boundary: a summary-only completion must still persist a
canonical task result in the same completion transaction.
"""

from __future__ import annotations

from pathlib import Path

MARKER = "# hgfinance-canonical-result-v1"
DB_SOURCE_CANDIDATES = (
    Path("/opt/hermes/hermes_cli/kanban_db.py"),
    Path("/opt/hermes-agent/hermes_cli/kanban_db.py"),
    Path("/app/hermes_cli/kanban_db.py"),
)
TOOL_SOURCE_CANDIDATES = (
    Path("/opt/hermes/tools/kanban_tools.py"),
    Path("/opt/hermes-agent/tools/kanban_tools.py"),
    Path("/app/tools/kanban_tools.py"),
)
CLI_SOURCE_CANDIDATES = (
    Path("/opt/hermes/hermes_cli/kanban.py"),
    Path("/opt/hermes-agent/hermes_cli/kanban.py"),
    Path("/app/hermes_cli/kanban.py"),
)


def _find_source(candidates: tuple[Path, ...], *, label: str) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(f"Hermes {label} source not found; refusing unpatched image")


def _install_db(source: str) -> str:
    if MARKER in source:
        return source

    function_anchor = "\ndef complete_task(\n"
    now_anchor = "    now = int(time.time())\n"
    edit_anchor = "    handoff_summary = summary if summary is not None else result\n"
    missing = [
        anchor
        for anchor in (function_anchor, now_anchor, edit_anchor)
        if anchor not in source
    ]
    if missing:
        raise SystemExit(
            f"Hermes Kanban completion contract changed: missing {missing}"
        )

    helper = f'''\n\n{MARKER}\ndef _canonical_handoff_values(\n    result: Optional[str], summary: Optional[str]\n) -> tuple[Optional[str], Optional[str]]:\n    """Return one canonical answer body and its compact run handoff.\n\n    ``complete_task`` is frequently called with only ``summary`` by workers.\n    That is a valid handoff, not an empty result. Keep an explicit non-empty\n    result authoritative and use the other field only as the missing fallback.\n    """\n    if not result or not result.strip():\n        result = summary\n    if not summary or not summary.strip():\n        summary = result\n    return result, summary\n'''
    source = source.replace(function_anchor, helper + function_anchor, 1)

    complete_function_start = source.index("def complete_task(")
    now_position = source.index(now_anchor, complete_function_start)
    completion_call = (
        f"    {MARKER}\n"
        "    result, summary = _canonical_handoff_values(result, summary)\n"
    )
    source = source[:now_position] + completion_call + source[now_position:]

    edit_function_start = source.index("def edit_completed_task_result(")
    edit_position = source.index(edit_anchor, edit_function_start)
    edit_call = (
        f"    {MARKER}\n"
        "    result, summary = _canonical_handoff_values(result, summary)\n"
    )
    source = source[:edit_position] + edit_call + source[edit_position:]

    # Keep the read-side explanation aligned with the write contract. These
    # are documentation-only replacements; the DB patch above is the single
    # implementation of the fallback behavior.
    source = source.replace(
        "    The worker writes its handoff to ``task_runs.summary``\n"
        "    via ``complete_task(summary=...)``; ``tasks.result`` is left empty\n"
        "    unless the caller passes ``result=`` explicitly. Dashboards and CLI\n"
        '    "show" views need this value to surface what a worker actually did\n'
        "    — without it, ``tasks.result`` is NULL and the task looks like a\n"
        "    no-op even when the run completed.\n",
        "    ``tasks.result`` is the canonical answer body. A summary-only\n"
        "    completion is promoted to that field by ``complete_task``;\n"
        "    ``task_runs.summary`` remains the compact downstream handoff.\n",
        1,
    )
    return source


def _install_optional_surface(source: str, *, surface: str) -> str:
    """Remove stale summary-only wording from optional CLI/tool surfaces."""

    if surface == "tool":
        return source.replace(
            "provide at least one of: summary (preferred), result",
            "provide at least one of: result (canonical), summary",
        )
    if surface == "cli":
        source = source.replace(
            'help="Result summary"',
            'help="Canonical task result body"',
        )
        source = source.replace(
            "Structured handoff summary for downstream tasks. "
            "Falls back to --result if omitted.",
            "Compact handoff summary; --result remains the canonical answer body.",
        )
        source = source.replace(
            "Structured handoff summary. Falls back to --result if omitted.",
            "Compact handoff summary; --result remains the canonical answer body.",
        )
        source = source.replace(
            "        # Workers hand off via ``task_runs.summary``; ``tasks.result`` is left NULL unless the caller explicitly passed\n"
            "        # ``result=``. Surfacing the latest summary here keeps ``show`` from\n"
            "        # looking like a no-op when the worker actually did real work.\n",
            "        # ``tasks.result`` is the canonical worker answer; latest summary\n"
            "        # remains a compact compatibility field for older cards.\n",
        )
        return source
    raise ValueError(f"unknown Hermes surface: {surface}")


def _install_file(path: Path, installer, **kwargs: str) -> None:
    updated = installer(path.read_text(encoding="utf-8"), **kwargs)
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    db_path = _find_source(DB_SOURCE_CANDIDATES, label="Kanban DB")
    _install_file(db_path, _install_db)

    tool_path = _find_source(TOOL_SOURCE_CANDIDATES, label="Kanban tool")
    _install_file(tool_path, _install_optional_surface, surface="tool")

    cli_path = _find_source(CLI_SOURCE_CANDIDATES, label="Kanban CLI")
    _install_file(cli_path, _install_optional_surface, surface="cli")


if __name__ == "__main__":
    main()
