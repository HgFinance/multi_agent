#!/usr/bin/env python3

import argparse
import json
import time
from pathlib import Path
from urllib import request, error

DEFAULT_DATA = "benchmarks/quantization/internal50_v1.json"
DEFAULT_URL = "http://127.0.0.1:8000/v1/chat/completions"

OUTPUT_RULES = {
    "numeric": (
        "Return ONLY the final numeric value. "
        "Do not include explanation, units, or extra text."
    ),
    "exact": (
        "Return ONLY the exact final answer token/value requested. "
        "Do not include explanation or extra text."
    ),
    "json_schema": (
        "Return ONLY one valid JSON object. "
        "Do not wrap it in markdown and do not add commentary."
    ),
    "json_semantic": (
        "Return ONLY one valid JSON object. "
        "Do not wrap it in markdown and do not add commentary."
    ),
    "contains_all": (
        "Return ONLY the requested values. "
        "Do not add unrelated information."
    ),
}


def post_json(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")

    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--max-tokens", type=int, default=384)
    ap.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    dataset = json.loads(
        Path(args.data).read_text(encoding="utf-8")
    )

    cases = dataset["cases"]

    assert dataset["benchmark"] == "HgFinance-Internal50-v1"
    assert dataset["status"] == "FROZEN_PRE_INFERENCE"
    assert len(cases) == 50

    rows = []

    for idx, case in enumerate(cases, 1):

        scoring = case["scoring_type"]

        output_rule = OUTPUT_RULES.get(
            scoring,
            "Return only the final answer."
        )

        user = f"""Use ONLY the supplied HgFinance context and rules.

Do not invent unavailable facts.
Do not override deterministic decisions stated in the context.
If the context requires fail-closed behavior, follow it.

CONTEXT:
{case["context"]}

TASK:
{case["user_prompt"]}

OUTPUT CONTRACT:
{output_rule}
"""

        payload = {
            "model": args.model,
            "messages": [
                {
                    "role": "system",
                    "content": case["system_prompt"],
                },
                {
                    "role": "user",
                    "content": user,
                },
            ],
            "temperature": 0,
            "max_tokens": args.max_tokens,
            "stream": False,
        }

        if args.dry_run:
            prediction = ""
            finish_reason = "DRY_RUN"
            latency = 0.0
            prompt_tokens = 0
            completion_tokens = 0
            err = None

        else:
            started = time.perf_counter()

            try:
                result = post_json(
                    args.url,
                    payload,
                    args.timeout,
                )

                latency = time.perf_counter() - started

                choice = result["choices"][0]

                prediction = (
                    choice["message"]["content"] or ""
                )

                finish_reason = choice.get(
                    "finish_reason",
                    ""
                )

                usage = result.get("usage", {})

                prompt_tokens = usage.get(
                    "prompt_tokens",
                    0,
                )

                completion_tokens = usage.get(
                    "completion_tokens",
                    0,
                )

                err = None

            except Exception as exc:
                latency = time.perf_counter() - started
                prediction = ""
                finish_reason = "ERROR"
                prompt_tokens = 0
                completion_tokens = 0
                err = f"{type(exc).__name__}: {exc}"

        row = {
            "id": case["id"],
            "category": case["category"],
            "critical": case["critical"],
            "scoring_type": case["scoring_type"],
            "expected": case["expected"],
            "prediction": prediction,
            "finish_reason": finish_reason,
            "latency_s": latency,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "error": err,
            "selected_from": case["selected_from"],
            "candidate_source": case["candidate_source"],
        }

        rows.append(row)

        print(
            f"[{idx:02d}/50] "
            f"{case['id']} "
            f"{case['category']} "
            f"{latency:.2f}s "
            f"{finish_reason}"
        )

    output = {
        "benchmark": "HgFinance-Internal50-v1",
        "dataset_sha256": (
            "368d1b0be88c2b13864a8e9cd3fd269aa781f877392a7cb563840fd99583dfef"
        ),
        "model": args.model,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "n": len(rows),
        "results": rows,
    }

    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    dst.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\nsaved:", dst)


if __name__ == "__main__":
    main()
