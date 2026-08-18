#!/usr/bin/env python3

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib import request

DEFAULT_DATA = "benchmarks/quantization/internal50_v2_reasoning.json"
DEFAULT_URL = "http://127.0.0.1:8000/v1/chat/completions"


def sha256(path):
    return hashlib.sha256(
        Path(path).read_bytes()
    ).hexdigest()


def post_json(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")

    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(
            resp.read().decode("utf-8")
        )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    dataset = json.loads(
        Path(args.data).read_text(encoding="utf-8")
    )

    assert dataset["benchmark"] == (
        "HgFinance-Internal50-v2-EmployeeReasoning"
    )
    assert len(dataset["cases"]) == 50

    rows = []

    for idx, case in enumerate(dataset["cases"], 1):

        typ = case["scoring_type"]

        if typ == "numeric":
            output_contract = (
                "Return only the final numeric value. "
                "Do not include units or explanation."
            )

        elif typ == "choice":
            labels = ", ".join(case["allowed_labels"])

            output_contract = (
                "Return exactly one of these labels: "
                f"{labels}."
            )

        elif typ == "json_exact":
            output_contract = (
                "Return only one valid JSON object using exactly "
                "the keys requested in the task. "
                "Do not use markdown."
            )

        else:
            raise ValueError(typ)

        prompt = f"""Use only the supplied rules and data.

CONTEXT:
{case["context"]}

TASK:
{case["question"]}

OUTPUT CONTRACT:
{output_contract}
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
                    "content": prompt,
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
            start = time.perf_counter()

            try:
                result = post_json(
                    args.url,
                    payload,
                    args.timeout,
                )

                latency = time.perf_counter() - start

                choice = result["choices"][0]
                prediction = (
                    choice["message"]["content"] or ""
                )
                finish_reason = choice.get(
                    "finish_reason",
                    "",
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
                latency = time.perf_counter() - start
                prediction = ""
                finish_reason = "ERROR"
                prompt_tokens = 0
                completion_tokens = 0
                err = f"{type(exc).__name__}: {exc}"

        row = {
            **case,
            "prediction": prediction,
            "finish_reason": finish_reason,
            "latency_s": latency,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "error": err,
        }

        rows.append(row)

        print(
            f"[{idx:02d}/50] "
            f"{case['id']} "
            f"{case['category']} "
            f"{latency:.2f}s "
            f"{finish_reason}"
        )

    out = {
        "benchmark": dataset["benchmark"],
        "dataset_sha256": sha256(args.data),
        "model": args.model,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "n": len(rows),
        "results": rows,
    }

    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    dst.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nsaved:", dst)


if __name__ == "__main__":
    main()
