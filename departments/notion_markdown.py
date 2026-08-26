"""Small deterministic Markdown<->Notion block renderer.

This module only renders report text. It does not make or change a binding
Risk/QA decision. The original Markdown remains in the database property so
the exact source can still be replayed and audited.

``notion_blocks_to_markdown`` is the read-side inverse used by the BFF to show
an already published report. It lives next to the writer on purpose: the two
share one block-type vocabulary, and splitting them lets the pair drift until
a block the writer emits renders as an empty line on the screen.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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


# ─────────────────────────── Notion blocks -> Markdown ───────────────────────


def _inline_markdown(rich_text: Any) -> str:
    """Rebuild inline Markdown from one Notion rich-text array.

    Annotation order matters: code wins over bold because Notion keeps both
    flags on a fenced inline span and ``**`code`**`` does not round-trip.
    """

    if not isinstance(rich_text, Sequence) or isinstance(
        rich_text, (str, bytes, bytearray)
    ):
        return ""

    parts: list[str] = []
    for item in rich_text:
        if not isinstance(item, Mapping):
            continue
        content = str(
            (item.get("text") or {}).get("content")
            if isinstance(item.get("text"), Mapping)
            else item.get("plain_text") or ""
        )
        if not content:
            content = str(item.get("plain_text") or "")
        if not content:
            continue

        annotations = item.get("annotations")
        annotations = annotations if isinstance(annotations, Mapping) else {}
        if annotations.get("code"):
            content = f"`{content}`"
        else:
            if annotations.get("bold"):
                content = f"**{content}**"
            if annotations.get("italic"):
                content = f"*{content}*"
            if annotations.get("strikethrough"):
                content = f"~~{content}~~"

        text = item.get("text")
        link = text.get("link") if isinstance(text, Mapping) else None
        url = ""
        if isinstance(link, Mapping):
            url = str(link.get("url") or "")
        elif not url:
            url = str(item.get("href") or "")
        if url:
            content = f"[{content}]({url})"

        parts.append(content)
    return "".join(parts)


def _block_children(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Nested rows/items, however the caller chose to attach them.

    The Notion API returns children under ``{type}.children`` when they were
    written inline, but a paginated read attaches them as a plain ``children``
    key. Accept both so a table read back from the API is not silently blank.
    """

    block_type = str(block.get("type") or "")
    payload = block.get(block_type)
    for candidate in (
        payload.get("children") if isinstance(payload, Mapping) else None,
        block.get("children"),
    ):
        if isinstance(candidate, Sequence) and not isinstance(
            candidate, (str, bytes, bytearray)
        ):
            return [row for row in candidate if isinstance(row, Mapping)]
    return []


def _table_markdown(block: Mapping[str, Any]) -> str:
    rows: list[list[str]] = []
    for row in _block_children(block):
        if str(row.get("type") or "") != "table_row":
            continue
        payload = row.get("table_row")
        cells = payload.get("cells") if isinstance(payload, Mapping) else None
        if not isinstance(cells, Sequence) or isinstance(
            cells, (str, bytes, bytearray)
        ):
            continue
        rows.append([_inline_markdown(cell).replace("|", "\\|") for cell in cells])

    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def notion_blocks_to_markdown(blocks: Any) -> str:
    """Render Notion blocks back into the Markdown this module would emit.

    Unknown block types degrade to their plain text rather than vanishing -
    a report that renders as a blank modal is worse than one with a plain
    paragraph where a callout used to be.
    """

    if not isinstance(blocks, Sequence) or isinstance(
        blocks, (str, bytes, bytearray)
    ):
        return ""

    _LIST_TYPES = {"bulleted_list_item", "numbered_list_item", "to_do"}

    lines: list[str] = []
    numbered = 0
    previous_type = ""

    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        payload = block.get(block_type)
        payload = payload if isinstance(payload, Mapping) else {}
        rich_text = payload.get("rich_text")

        if block_type != "numbered_list_item":
            numbered = 0
        # Consecutive list items stay glued into one list; every other
        # transition opens a new block and needs the blank line that makes it
        # one in Markdown.
        glued = block_type in _LIST_TYPES and previous_type in _LIST_TYPES
        if not glued and lines and lines[-1].strip():
            lines.append("")
        previous_type = block_type

        if block_type == "divider":
            lines.append("---")
            continue
        if block_type == "table":
            table = _table_markdown(block)
            if table:
                lines.extend(table.split("\n"))
            continue
        if block_type == "code":
            language = str(payload.get("language") or "").strip()
            language = "" if language in {"plain text", "plaintext"} else language
            lines.append(f"```{language}")
            lines.extend(_inline_markdown(rich_text).split("\n"))
            lines.append("```")
            continue

        text = _inline_markdown(rich_text)

        if block_type in {"heading_1", "heading_2", "heading_3"}:
            lines.append(f"{'#' * int(block_type.rsplit('_', 1)[1])} {text}")
        elif block_type == "bulleted_list_item":
            lines.append(f"- {text}")
        elif block_type == "numbered_list_item":
            numbered += 1
            lines.append(f"{numbered}. {text}")
        elif block_type == "to_do":
            lines.append(f"- [{'x' if payload.get('checked') else ' '}] {text}")
        elif block_type == "quote":
            lines.append(f"> {text}")
        else:
            # paragraph and anything this reader does not model yet.
            lines.append(text)

    return "\n".join(lines).strip()


if __name__ == "__main__":
    # 네트워크 없이 도는 자체 점검. 정변환과 역변환이 같은 블록 어휘를 쓰는지만
    # 본다 - 둘이 갈라지면 발행은 되는데 화면이 빈다.
    source = "\n".join(
        (
            "# CEO Final Synthesis",
            "",
            "- Root task: `t_cc8081c5`",
            "- Selected departments: research, risk",
            "",
            "## Result",
            "",
            "**Bold** and `code` and [link](https://example.com/a).",
            "",
            "1. first",
            "2. second",
            "",
            "> quoted line",
            "",
            "| A | B |",
            "| --- | --- |",
            "| 1 | 2 |",
            "",
            "---",
            "",
            "```json",
            '{"a": 1}',
            "```",
        )
    )

    blocks = markdown_to_notion_blocks(source)
    emitted = {str(block.get("type")) for block in blocks}
    assert emitted == {
        "heading_1",
        "heading_2",
        "bulleted_list_item",
        "numbered_list_item",
        "paragraph",
        "quote",
        "table",
        "divider",
        "code",
    }, emitted

    rendered = notion_blocks_to_markdown(blocks)
    assert rendered == source, f"round trip drifted:\n{rendered}"
    # 한 번 더 돌려도 같아야 한다 - 빈 줄 규칙이 매번 한 줄씩 늘면 안 된다.
    assert notion_blocks_to_markdown(markdown_to_notion_blocks(rendered)) == rendered

    # 이 리더가 모르는 블록도 사라지지 않고 최소한 글자는 남는다.
    unknown = notion_blocks_to_markdown(
        [{"type": "callout", "callout": {"rich_text": [{"plain_text": "주의"}]}}]
    )
    assert unknown == "주의", unknown

    # 표 행은 자식 블록이라 별도 조회로 붙는다 - 두 가지 부착 방식 모두 읽는다.
    rows = [
        {
            "type": "table_row",
            "table_row": {"cells": [[{"plain_text": "a"}], [{"plain_text": "b"}]]},
        }
    ]
    inline = notion_blocks_to_markdown([{"type": "table", "table": {"children": rows}}])
    attached = notion_blocks_to_markdown([{"type": "table", "table": {}, "children": rows}])
    assert inline == attached == "| a | b |\n| --- | --- |", inline

    assert notion_blocks_to_markdown([]) == ""
    assert notion_blocks_to_markdown(None) == ""
    assert notion_blocks_to_markdown("not a block list") == ""

    print("notion_markdown 자체 점검 통과")
