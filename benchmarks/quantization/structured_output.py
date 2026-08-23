"""Generic structured-output contracts for the quantization benchmark.

The schema is supplied by the application contract or inferred from the
request's explicit output contract.  No expected answer and no benchmark ID
are used.  Invalid or semantically unverified output is returned as an error;
there is intentionally no answer-producing fallback here.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator


class StructuredOutputError(ValueError):
    """Raised when structured output cannot be trusted."""


@dataclass(frozen=True)
class StructuredValidation:
    value: Any | None
    valid: bool
    error: str | None = None


def extract_json(raw: str) -> Any:
    """Decode one JSON value, allowing only surrounding markdown fences."""

    text = str(raw).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"invalid JSON: {exc.msg}") from exc


def _walk_finite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise StructuredOutputError(f"non-finite number at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _walk_finite(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_finite(child, f"{path}[{index}]")


def validate_json(raw: str, schema: dict[str, Any]) -> StructuredValidation:
    """Perform syntax, finite-number, type, enum, and extra-key validation."""

    try:
        value = extract_json(raw)
        _walk_finite(value)
        validator = Draft202012Validator(schema)
        error = next(validator.iter_errors(value), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            location = f" at {path}" if path else ""
            raise StructuredOutputError(f"schema violation{location}: {error.message}")
        return StructuredValidation(value=value, valid=True)
    except StructuredOutputError as exc:
        return StructuredValidation(value=None, valid=False, error=str(exc))


def _property_schema(description: str) -> dict[str, Any]:
    quoted = re.findall(r'"([^"\n]+)"', description)
    lowered = description.casefold()
    if quoted and ("one of" in lowered or "or" in lowered or "allowed" in lowered):
        return {"type": "string", "enum": quoted}
    if "boolean" in lowered:
        return {"type": "boolean"}
    if "integer" in lowered:
        return {"type": "integer"}
    if "number" in lowered or "numeric" in lowered:
        schema: dict[str, Any] = {"type": ["number", "null"] if "null" in lowered else "number"}
        return schema
    return {"type": "string"}


def infer_schema_from_contract(text: str) -> dict[str, Any] | None:
    """Infer a strict object schema only from explicit key/type instructions."""

    if "return json" not in text.casefold():
        return None
    properties: dict[str, Any] = {}
    for line in text.splitlines():
        match = re.match(r"\s*-\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+?)\s*$", line)
        if match:
            properties[match.group(1)] = _property_schema(match.group(2))
    if not properties:
        return None
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def vllm_response_format(schema: dict[str, Any], name: str = "structured_response") -> dict[str, Any]:
    """Build the OpenAI-compatible vLLM guided JSON request field."""

    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def retry_instruction(schema: dict[str, Any], error: str) -> str:
    return (
        "Your previous structured response failed validation.\n"
        f"Validation error: {error}\n"
        "Re-evaluate only the supplied context and return one JSON object "
        "that satisfies this schema exactly. Do not invent missing values.\n"
        f"SCHEMA:\n{json.dumps(schema, ensure_ascii=False, sort_keys=True)}"
    )
