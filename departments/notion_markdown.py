"""Small deterministic Markdown-to-Notion block renderer.

This module only renders report text. It does not make or change a binding
Risk/QA decision. The original Markdown remains in the database property so
the exact source can still be replayed and audited.
"""

from __future__ import annotations

import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING_RE = re.compile(r"^\s*(#{1,3})\s+(.+?)\s*$")
_UNORDERED_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TODO_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")
_INLINE_RE = re.compile(
    r"\[([^\]]+)\]\((https?://[^)\s]+)\)"
    r"|\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`"
)
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _text_object(
    content: str,
    *,
    bold: bool = False,
    code: bool = False,
    url: str | None = None,
) -> dict[str, Any]:
    annotations = {
        "bold": bold,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": code,
        "color": "default",
    }
    text: dict[str, Any] = {"content": content}
    if url:
        text["link"] = {"url": url}
    return {"type": "text", "text": text, "annotations": annotations}


def _rich_text(value: Any) -> list[dict[str, Any]]:
    """Convert common inline Markdown to Notion rich-text objects."""

    text = "" if value is None else str(value)
    if not text:
        return [_text_object("")]

    result: list[dict[str, Any]] = []
    cursor = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > cursor:
            result.extend(_plain_text(text[cursor : match.start()]))
        label, url, bold_a, bold_b, code = match.groups()
        if label is not None:
            result.extend(_plain_text(label, url=url))
        elif bold_a is not None or bold_b is not None:
            result.extend(_plain_text(bold_a or bold_b or "", bold=True))
        else:
            result.extend(_plain_text(code or "", code=True))
        cursor = match.end()
    if cursor < len(text):
        result.extend(_plain_text(text[cursor:]))
    return result or [_text_object("")]


def _plain_text(
    value: str,
    *,
    bold: bool = False,
    code: bool = False,
    url: str | None = None,
) -> list[dict[str, Any]]:
    return [
        _text_object(value[i : i + 1900], bold=bold, code=code, url=url)
        for i in range(0, len(value), 1900)
    ] or [_text_object("")]


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    stripped = stripped.removeprefix("|")
    stripped = stripped.removesuffix("|")
    cells = re.split(r"(?<!\\)\|", stripped)
    return [cell.replace("\\|", "|").strip() for cell in cells]


def _table_block(rows: list[list[str]]) -> dict[str, Any]:
    width = max((len(row) for row in rows), default=1)
    padded = [row + [""] * (width - len(row)) for row in rows]
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {
                    "object": "block",
                    "type": "table_row",
                    "table_row": {"cells": [_rich_text(cell) for cell in row]},
                }
                for row in padded
            ],
        },
    }


def markdown_to_notion_blocks(markdown: str | None) -> list[dict[str, Any]]:
    """Render report Markdown into Notion blocks without an LLM."""

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text("\n".join(paragraph))},
                }
            )
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            language = fence.group(1).lower() or "plain text"
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": _plain_text("\n".join(code_lines)),
                        "language": language,
                    },
                }
            )
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            blocks.append(
                {
                    "object": "block",
                    "type": f"heading_{level}",
                    f"heading_{level}": {"rich_text": _rich_text(heading.group(2))},
                }
            )
            index += 1
            continue

        if index + 1 < len(lines) and _table_separator(lines[index + 1]):
            header = _table_cells(line)
            if header:
                flush_paragraph()
                rows = [header]
                index += 2
                while index < len(lines):
                    row = _table_cells(lines[index])
                    if row is None:
                        break
                    rows.append(row)
                    index += 1
                blocks.append(_table_block(rows))
                continue

        if stripped == "---" or stripped == "***":
            flush_paragraph()
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            index += 1
            continue

        todo = _TODO_RE.match(line)
        if todo:
            flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": _rich_text(todo.group(2)),
                        "checked": todo.group(1).lower() == "x",
                    },
                }
            )
            index += 1
            continue

        unordered = _UNORDERED_RE.match(line)
        if unordered:
            flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rich_text(unordered.group(1))},
                }
            )
            index += 1
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "numbered_list_item",
                    "numbered_list_item": {"rich_text": _rich_text(ordered.group(1))},
                }
            )
            index += 1
            continue

        if stripped.startswith("> "):
            flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": _rich_text(stripped[2:])},
                }
            )
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    return blocks


def _table_separator(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line))
