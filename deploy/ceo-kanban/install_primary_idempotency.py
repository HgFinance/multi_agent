"""Install the HgFinance primary-task guard into the Hermes image at build time."""

from __future__ import annotations

from pathlib import Path

MARKER = "# hgfinance-primary-idempotency-v1"
SOURCE_CANDIDATES = (
    Path("/opt/hermes/tools/kanban_tools.py"),
    Path("/opt/hermes/hermes-agent/tools/kanban_tools.py"),
    Path("/app/tools/kanban_tools.py"),
)
CLI_SOURCE_CANDIDATES = (
    Path("/opt/hermes/hermes_cli/kanban.py"),
    Path("/opt/hermes-agent/hermes_cli/kanban.py"),
    Path("/app/hermes_cli/kanban.py"),
)
CLI_MARKER = "# hgfinance-primary-idempotency-cli-v1"


def _find_source() -> Path:
    for path in SOURCE_CANDIDATES:
        if path.is_file():
            return path
    raise SystemExit(
        "Hermes Kanban tool source not found; refusing unpatched CEO image"
    )


def _install(source: str) -> str:
    if MARKER in source:
        return source
    required = ("def _handle_create", "new_tid = kb.create_task(", "idempotency_key")
    missing = [item for item in required if item not in source]
    if missing:
        raise SystemExit(f"Hermes Kanban tool contract changed: missing {missing}")

    import_line = (
        "\n"
        "# hgfinance-primary-idempotency-v1\n"
        "from orchestration.primary_task_idempotency import (\n"
        "    find_existing_scoped_primary,\n"
        "    is_analysis_primary_eligible,\n"
        "    reject_invalid_primary_create,\n"
        "    requires_scoped_primary_contract,\n"
        "    scoped_primary_create_lock,\n"
        "    scoped_primary_identity,\n"
        ")\n"
    )
    if "from __future__ import annotations" in source:
        source = source.replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations" + import_line,
            1,
        )
    else:
        source = import_line.lstrip("\n") + source

    start = source.index("            new_tid = kb.create_task(\n")
    end = source.index("            new_task = kb.get_task", start)
    create_block = source[start:end].rstrip("\n")
    indented_create_block = "\n".join(
        (f"    {line}" if line else line) for line in create_block.splitlines()
    )
    else_create_block = "\n".join(
        (f"            {line}" if line else line) for line in create_block.splitlines()
    )
    replacement = (
            "            _primary_rejection = reject_invalid_primary_create(\n"
            "                body, assignee, idempotency_key=idempotency_key\n"
            "            )\n"
            "            if _primary_rejection:\n"
            "                raise ValueError(_primary_rejection)\n"
            "            _primary_identity = scoped_primary_identity(\n"
        "                body, assignee, idempotency_key=idempotency_key\n"
        "            )\n"
        "            if (\n"
        "                _primary_identity is not None\n"
        "                and not is_analysis_primary_eligible(_primary_identity[1])\n"
        "            ):\n"
        "                raise ValueError(\n"
        "                    'CEO primary task assignee is not analysis-primary eligible'\n"
        "                )\n"
        "            elif _primary_identity is None:\n"
        "                if requires_scoped_primary_contract(\n"
        "                    body, assignee, idempotency_key=idempotency_key\n"
        "                ):\n"
        "                    raise ValueError(\n"
        "                        'CEO primary task requires workflow_root_task_id and workflow_role=primary'\n"
        "                    )\n"
        f"{indented_create_block}\n"
        "            else:\n"
        "                with scoped_primary_create_lock():\n"
        "                    _existing_primary_id = find_existing_scoped_primary(\n"
        "                        kb.list_tasks(\n"
        "                            conn, assignee=str(assignee), include_archived=False\n"
        "                        ),\n"
        "                        root_task_id=_primary_identity[0],\n"
        "                        assignee=_primary_identity[1],\n"
        "                    )\n"
        "                    if _existing_primary_id:\n"
        "                        new_tid = _existing_primary_id\n"
        "                    else:\n"
        f"{else_create_block}\n"
    )
    source = source[:start] + replacement + source[end:]
    return source


def _find_cli_source() -> Path:
    for path in CLI_SOURCE_CANDIDATES:
        if path.is_file():
            return path
    raise SystemExit(
        "Hermes Kanban CLI source not found; refusing unguarded CEO create path"
    )


def _install_cli(source: str) -> str:
    if CLI_MARKER in source:
        return source
    required = (
        "def _cmd_create",
        "with kb.connect_closing() as conn:",
        'idempotency_key=getattr(args, "idempotency_key", None)',
    )
    missing = [item for item in required if item not in source]
    if missing:
        raise SystemExit(f"Hermes Kanban CLI contract changed: missing {missing}")

    import_line = (
        f"{CLI_MARKER}\n"
        "from orchestration.primary_task_idempotency import "
        "reject_invalid_primary_create\n"
    )
    anchor = "from hermes_cli import kanban_swarm as ks\n"
    if anchor not in source:
        raise SystemExit("Hermes Kanban CLI import contract changed")
    source = source.replace(anchor, anchor + import_line, 1)

    function_start = source.index("def _cmd_create")
    guard_anchor = source.index("    with kb.connect_closing() as conn:\n", function_start)
    guard = (
        "    _primary_rejection = reject_invalid_primary_create(\n"
        '        getattr(args, "body", None),\n'
        '        getattr(args, "assignee", None),\n'
        '        getattr(args, "idempotency_key", None),\n'
        "    )\n"
        "    if _primary_rejection:\n"
        '        print(f"kanban: {_primary_rejection}", file=sys.stderr)\n'
        "        return 2\n"
    )
    return source[:guard_anchor] + guard + source[guard_anchor:]


def main() -> None:
    tool_path = _find_source()
    tool_updated = _install(tool_path.read_text(encoding="utf-8"))
    tool_path.write_text(tool_updated, encoding="utf-8")

    cli_path = _find_cli_source()
    cli_updated = _install_cli(cli_path.read_text(encoding="utf-8"))
    cli_path.write_text(cli_updated, encoding="utf-8")


if __name__ == "__main__":
    main()
