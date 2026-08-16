#!/usr/bin/env python3

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path


def normalize_text(value):
    s = str(value).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def extract_number(value):
    s = str(value).replace(",", "")

    nums = re.findall(
        r"[-+]?\d+(?:\.\d+)?",
        s,
    )

    if not nums:
        return None

    try:
        return float(nums[-1])
    except ValueError:
        return None


def parse_json_prediction(text):
    text = str(text).strip()

    # Direct JSON first.
    try:
        return json.loads(text)
    except Exception:
        pass

    # Defensive fallback for accidental surrounding text.
    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        try:
            return json.loads(
                text[start:end + 1]
            )
        except Exception:
            pass

    return None


def json_subset(expected, actual):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False

        for key, value in expected.items():
            if key not in actual:
                return False

            if not json_subset(
                value,
                actual[key],
            ):
                return False

        return True

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False

        if len(expected) != len(actual):
            return False

        return all(
            json_subset(e, a)
            for e, a in zip(expected, actual)
        )

    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False

        tolerance = max(
            1e-6,
            abs(float(expected)) * 1e-6,
        )

        return (
            abs(float(expected) - float(actual))
            <= tolerance
        )

    return expected == actual


def score_one(row):
    typ = row["scoring_type"]

    expected = row["expected"]
    pred = row["prediction"]

    if row.get("error"):
        return False, 0.0, "request_error"

    if typ == "numeric":
        got = extract_number(pred)

        if got is None:
            return False, 0.0, "numeric_missing"

        target = float(expected)

        tolerance = max(
            1e-6,
            abs(target) * 1e-4,
        )

        passed = abs(got - target) <= tolerance

        return (
            passed,
            1.0 if passed else 0.0,
            "numeric",
        )

    if typ == "exact":
        passed = (
            normalize_text(pred)
            == normalize_text(expected)
        )

        return (
            passed,
            1.0 if passed else 0.0,
            "exact",
        )

    if typ in {
        "json_schema",
        "json_semantic",
    }:
        actual = parse_json_prediction(pred)

        if actual is None:
            return False, 0.0, "invalid_json"

        passed = json_subset(
            expected,
            actual,
        )

        return (
            passed,
            1.0 if passed else 0.0,
            typ,
        )

    if typ == "contains_all":
        if isinstance(expected, list):
            text = normalize_text(pred)

            passed = all(
                normalize_text(item) in text
                for item in expected
            )
        else:
            passed = (
                normalize_text(expected)
                in normalize_text(pred)
            )

        return (
            passed,
            1.0 if passed else 0.0,
            "contains_all",
        )

    raise ValueError(
        f"unknown scoring_type: {typ}"
    )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("input")
    ap.add_argument("--output", required=True)

    args = ap.parse_args()

    x = json.loads(
        Path(args.input).read_text(
            encoding="utf-8"
        )
    )

    rows = []

    for row in x["results"]:
        passed, score, metric = score_one(row)

        out = dict(row)
        out["passed"] = passed
        out["score"] = score
        out["metric"] = metric

        rows.append(out)

    n = len(rows)

    passed_n = sum(
        bool(r["passed"])
        for r in rows
    )

    critical_rows = [
        r for r in rows
        if r["critical"]
    ]

    critical_failed = [
        r["id"]
        for r in critical_rows
        if not r["passed"]
    ]

    category = {}

    for name in sorted({
        r["category"]
        for r in rows
    }):
        sub = [
            r for r in rows
            if r["category"] == name
        ]

        ok = sum(
            bool(r["passed"])
            for r in sub
        )

        category[name] = {
            "n": len(sub),
            "passed": ok,
            "accuracy": ok / len(sub),
        }

    latencies = [
        r["latency_s"]
        for r in rows
        if not r.get("error")
    ]

    output = {
        "benchmark": x["benchmark"],
        "dataset_sha256": x["dataset_sha256"],
        "model": x["model"],
        "n": n,
        "passed": passed_n,
        "accuracy": passed_n / n,
        "mean_score": (
            sum(r["score"] for r in rows)
            / n
        ),
        "critical_n": len(critical_rows),
        "critical_failed_n": len(critical_failed),
        "critical_failed_ids": critical_failed,
        "error_n": sum(
            bool(r.get("error"))
            for r in rows
        ),
        "avg_latency_s": (
            sum(latencies) / len(latencies)
            if latencies else None
        ),
        "categories": category,
        "results": rows,
    }

    dst = Path(args.output)

    dst.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=== INTERNAL50 SCORE ===")
    print("model            :", x["model"])
    print("passed           :", f"{passed_n}/{n}")
    print("accuracy         :", f"{passed_n/n*100:.1f}%")
    print(
        "critical failures:",
        len(critical_failed),
    )
    print(
        "errors           :",
        output["error_n"],
    )

    print("\n=== CATEGORY ===")

    for name, stat in category.items():
        print(
            f"{name:42s} "
            f'{stat["passed"]:2d}/{stat["n"]:2d} '
            f'{stat["accuracy"]*100:6.1f}%'
        )

    if critical_failed:
        print("\n=== CRITICAL FAIL IDS ===")

        for cid in critical_failed:
            print(cid)

    print("\nsaved:", dst)


if __name__ == "__main__":
    main()
