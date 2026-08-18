"""Fail-closed validation and canonical hashing for specialist examples."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class DatasetValidationError(ValueError):
    """Raised when an example cannot safely enter a training mixture."""


_ALLOWED_ROLES = {"system", "user", "assistant"}


def normalize_text(value: str) -> str:
    """Normalize only for duplicate detection; never mutate training text."""

    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def message_sha256(messages: list[dict[str, str]]) -> str:
    return _json_hash(messages)


def normalized_message_sha256(messages: list[dict[str, str]]) -> str:
    normalized = [
        {"role": item["role"], "content": normalize_text(item["content"])}
        for item in messages
    ]
    return _json_hash(normalized)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _messages_from_record(raw: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = raw.get("messages")
    if messages is None:
        required = ("instruct", "input", "output")
        if not all(isinstance(raw.get(key), str) and raw[key].strip() for key in required):
            raise DatasetValidationError("expected non-empty instruct/input/output")
        messages = [
            {"role": "system", "content": raw["instruct"]},
            {"role": "user", "content": raw["input"]},
            {"role": "assistant", "content": raw["output"]},
        ]
    if not isinstance(messages, list) or not messages:
        raise DatasetValidationError("messages must be a non-empty list")

    result: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise DatasetValidationError(f"messages[{index}] must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in _ALLOWED_ROLES:
            raise DatasetValidationError(f"messages[{index}] has unsupported role: {role!r}")
        if not isinstance(content, str) or not content.strip():
            raise DatasetValidationError(f"messages[{index}] content must be non-empty text")
        result.append({"role": role, "content": content})

    roles = [message["role"] for message in result]
    if "system" not in roles or "user" not in roles:
        raise DatasetValidationError("messages must contain system and user messages")
    if roles[-1] != "assistant" or roles.count("assistant") != 1:
        raise DatasetValidationError("messages must end with exactly one assistant completion")
    return result


@dataclass(frozen=True)
class ValidatedExample:
    record: dict[str, Any]
    messages: list[dict[str, str]]
    source_dataset: str
    source_file: str
    source_row: int

    @property
    def record_sha256(self) -> str:
        return message_sha256(self.messages)

    @property
    def normalized_sha256(self) -> str:
        return normalized_message_sha256(self.messages)


def validate_record(
    raw: Mapping[str, Any], *, source_dataset: str, source_file: str, source_row: int
) -> ValidatedExample:
    if not isinstance(raw, Mapping):
        raise DatasetValidationError(f"{source_file}:{source_row}: record must be an object")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise DatasetValidationError(f"{source_file}:{source_row}: id must be non-empty text")
    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        raise DatasetValidationError(f"{source_file}:{source_row}: category must be non-empty text")
    themes = raw.get("behavior_themes", [])
    if not isinstance(themes, list) or not all(
        isinstance(theme, str) and theme.strip() for theme in themes
    ):
        raise DatasetValidationError(f"{source_file}:{source_row}: behavior_themes must be text list")
    messages = _messages_from_record(raw)
    normalized = dict(raw)
    normalized["messages"] = messages
    normalized["category"] = category.strip()
    normalized["behavior_themes"] = list(themes)
    return ValidatedExample(normalized, messages, source_dataset, source_file, source_row)


def load_jsonl(path: Path, *, source_dataset: str) -> list[ValidatedExample]:
    examples: list[ValidatedExample] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise DatasetValidationError(f"cannot open {path}: {exc}") from exc
    with handle:
        for row, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetValidationError(f"{path}:{row}: invalid JSON: {exc}") from exc
            examples.append(
                validate_record(
                    raw,
                    source_dataset=source_dataset,
                    source_file=str(path),
                    source_row=row,
                )
            )
    if not examples:
        raise DatasetValidationError(f"{path}: no examples")
    return examples


def message_texts(examples: Iterable[ValidatedExample]) -> Iterable[str]:
    for example in examples:
        for message in example.messages:
            yield message["content"]
