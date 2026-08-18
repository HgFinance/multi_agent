"""Read-only exact and near-duplicate checks against held-out benchmarks."""

from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .schema import DatasetValidationError, ValidatedExample, normalize_text


def _records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        if path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        value = json.loads(line)
                        if isinstance(value, dict):
                            yield value
            return
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, dict))
    elif isinstance(value, dict):
        for key in ("items", "examples", "data", "questions", "cases", "records", "rows"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                yield from (item for item in candidate if isinstance(item, dict))
                return
        yield value


def _texts(record: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("instruction", "instruct", "input", "question", "query", "prompt", "context"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    messages = record.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") in {"system", "user"}:
                if isinstance(message.get("content"), str):
                    texts.append(message["content"])
    return [normalize_text(value) for value in texts if normalize_text(value)]


def _benchmark_texts(root: Path) -> list[tuple[str, str, str]]:
    output: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        for index, record in enumerate(_records(path)):
            for text in _texts(record):
                output.append((str(path), str(record.get("id", index)), text))
    return output


def check_contamination(
    examples: Iterable[ValidatedExample],
    benchmark_root: Path,
    *,
    near_threshold: float = 0.90,
    minimum_length: int = 40,
) -> dict[str, Any]:
    benchmark = _benchmark_texts(benchmark_root)
    by_hash: dict[str, list[tuple[str, str]]] = {}
    for path, identifier, text in benchmark:
        by_hash.setdefault(hashlib.sha256(text.encode("utf-8")).hexdigest(), []).append(
            (path, identifier)
        )

    exact: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
    for example in examples:
        # System prompts are shared policy text, not an example identity. Restrict
        # candidate matching to user/question content to avoid quadratic noise.
        candidate_texts = sorted(
            {
                normalize_text(message["content"])
                for message in example.messages
                if message["role"] == "user" and normalize_text(message["content"])
            }
        )
        for candidate in candidate_texts:
            candidate_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            for path, identifier in by_hash.get(candidate_hash, []):
                exact.append(
                    {"id": example.record["id"], "benchmark_path": path, "benchmark_id": identifier}
                )
            if len(candidate) < minimum_length:
                continue
            candidate_tokens = set(candidate.split())
            for path, identifier, reference in benchmark:
                if abs(len(candidate) - len(reference)) / max(len(candidate), len(reference)) > 0.25:
                    continue
                reference_tokens = set(reference.split())
                if not reference_tokens or len(candidate_tokens & reference_tokens) / min(
                    len(candidate_tokens), len(reference_tokens)
                ) < 0.65:
                    continue
                score = SequenceMatcher(None, candidate, reference).ratio()
                if score >= near_threshold and candidate != reference:
                    near.append(
                        {
                            "id": example.record["id"],
                            "benchmark_path": path,
                            "benchmark_id": identifier,
                            "similarity": round(score, 6),
                        }
                    )
    return {
        "status": "BLOCKED" if exact or near else "PASS",
        "benchmark_root": str(benchmark_root),
        "benchmark_text_count": len(benchmark),
        "exact": exact,
        "near": near,
        "exact_count": len(exact),
        "near_count": len(near),
    }


def require_clean(result: dict[str, Any]) -> None:
    if result.get("status") != "PASS":
        raise DatasetValidationError(
            "held-out benchmark contamination detected: "
            f"exact={result.get('exact_count', 0)} near={result.get('near_count', 0)}"
        )
