#!/usr/bin/env python3
"""Run the frozen HgFinance External-50-v1 inference contract.

This runner deliberately does not score predictions.  It only records the
raw inference fields consumed by the versioned scoring step.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
TEMPERATURE = 0
MAX_TOKENS = 384
TIMEOUT_SECONDS = 180
SYSTEM_PROMPT = (
    "You are a precise financial QA system. Answer only from the supplied evidence."
)
USER_PROMPT = """Use ONLY the supplied financial evidence to answer the question.

Rules:
1. Do not invent facts not contained in the evidence.
2. Perform calculations when required.
3. Keep the final answer concise.
4. Preserve the requested unit or scale.
5. If the evidence is insufficient, explicitly say so.

EVIDENCE:
{context}

QUESTION:
{question}"""
DEFAULT_DATASET = Path(__file__).with_name("external50_v1.json")

REQUIRED_RESULT_FIELDS = (
    "id",
    "source",
    "question",
    "gold",
    "prediction",
    "finish_reason",
    "latency_s",
    "prompt_tokens",
    "completion_tokens",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the frozen External-50-v1 vLLM inference contract."
    )
    parser.add_argument(
        "--model",
        help="vLLM model identifier sent in the request (required unless --dry-run).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"Frozen dataset path (default: {DEFAULT_DATASET}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON output path for raw predictions (required unless --dry-run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the dataset and print the fixed request contract without HTTP calls.",
    )
    return parser.parse_args(argv)


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("dataset must be an object containing a cases list")
    for index, case in enumerate(payload["cases"]):
        if not isinstance(case, dict):
            raise ValueError(f"dataset case {index} is not an object")
        for field in ("id", "source", "question", "context", "gold_answer"):
            if field not in case:
                raise ValueError(f"dataset case {index} is missing {field!r}")
    return payload


def build_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_PROMPT.format(
                context=case["context"], question=case["question"]
            ),
        },
    ]


def request_case(model: str, case: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": build_messages(case),
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with request.urlopen(http_request, timeout=TIMEOUT_SECONDS) as response:
            response_body = response.read()
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"inference failed for {case['id']}: {exc}") from exc
    latency_s = time.perf_counter() - started

    try:
        response_payload = json.loads(response_body)
        choice = response_payload["choices"][0]
        usage = response_payload.get("usage", {})
        prediction = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid vLLM response for {case['id']}") from exc

    return {
        "id": case["id"],
        "source": case["source"],
        "question": case["question"],
        "gold": case["gold_answer"],
        "prediction": prediction,
        "finish_reason": choice.get("finish_reason"),
        "latency_s": round(latency_s, 4),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def raw_result(model: str, dataset: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "benchmark": dataset.get("benchmark", "HgFinance-External50-v1"),
        "seed": dataset.get("seed"),
        "model": model,
        "n": len(results),
        "results": results,
    }


def validate_result_fields(results: list[dict[str, Any]]) -> None:
    for index, result in enumerate(results):
        missing = [field for field in REQUIRED_RESULT_FIELDS if field not in result]
        if missing:
            raise ValueError(f"result {index} is missing fields: {', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dataset={args.dataset}")
        print(f"cases={len(dataset['cases'])}")
        print(f"endpoint={ENDPOINT}")
        print(f"temperature={TEMPERATURE}")
        print(f"max_tokens={MAX_TOKENS}")
        print("stream=false")
        print(f"timeout={TIMEOUT_SECONDS}")
        print("http_requests=0")
        return 0

    if not args.model or not args.output:
        print("error: --model and --output are required unless --dry-run", file=sys.stderr)
        return 2

    results = []
    for index, case in enumerate(dataset["cases"], start=1):
        print(f"running {index}/{len(dataset['cases'])}: {case['id']}", file=sys.stderr)
        results.append(request_case(args.model, case))
    validate_result_fields(results)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(raw_result(args.model, dataset, results), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
