"""Install the HgFinance primary-task guard into the Hermes image at build time."""

from __future__ import annotations

from pathlib import Path

MARKER = "# hgfinance-primary-idempotency-v1"
SOURCE_CANDIDATES = (
    Path("/opt/hermes/tools/kanban_tools.py"),
    Path("/opt/hermes/hermes-agent/tools/kanban_tools.py"),
    Path("/app/tools/kanban_tools.py"),
)


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
        "            _primary_identity = scoped_primary_identity(body, assignee)\n"
        "            if _primary_identity is None:\n"
        "                if requires_scoped_primary_contract(body, assignee):\n"
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


def main() -> None:
    path = _find_source()
    updated = _install(path.read_text(encoding="utf-8"))
    path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
