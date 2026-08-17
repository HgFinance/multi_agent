#!/usr/bin/env python3

import argparse
import json
import math
import re
from pathlib import Path


def norm(s):
    s = str(s).strip().upper()
    s = re.sub(r"\s+", " ", s)
    return s


def extract_number(text):
    s = str(text).replace(",", "")
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)

    if not nums:
        return None

    try:
        return float(nums[-1])
    except ValueError:
        return None


def extract_json(text):
    text = str(text).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    return None


def choice_match(prediction, expected, allowed):
    p = norm(prediction)
    e = norm(expected)

    # Direct expected label.
    if p == e:
        return True

    # Allow lightweight prose around exactly one supplied label.
    hits = []

    for label in allowed:
        label_n = norm(label)

        pattern = r"(?<![A-Z0-9_])" + re.escape(label_n) + r"(?![A-Z0-9_])"

        if re.search(pattern, p):
            hits.append(label_n)

    hits = list(dict.fromkeys(hits))

    return len(hits) == 1 and hits[0] == e


def json_equal(expected, actual):
    if type(expected) is not type(actual):
        # int/float compatibility
        if (
            isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
        ):
            return math.isclose(
                float(expected),
                float(actual),
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
        return False

    if isinstance(expected, dict):
        return (
            set(expected.keys()) == set(actual.keys())
            and all(
                json_equal(expected[k], actual[k])
                for k in expected
            )
        )

    if isinstance(expected, list):
        return (
            len(expected) == len(actual)
            and all(
                json_equal(e, a)
                for e, a in zip(expected, actual)
            )
        )

    if isinstance(expected, float):
        return math.isclose(
            expected,
            actual,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )

    return expected == actual


def score_one(row):
    typ = row["scoring_type"]
    expected = row["expected"]
    prediction = row.get("prediction", "")

    if row.get("error"):
        return False, "request_error"

    if typ == "numeric":
        got = extract_number(prediction)

        if got is None:
            return False, "numeric_missing"

        rel = row.get("rel_tol", 1e-4)
        abs_ = row.get("abs_tol", 1e-6)

        passed = math.isclose(
            got,
            float(expected),
            rel_tol=rel,
            abs_tol=abs_,
        )

        return passed, "numeric"

    if typ == "choice":
        passed = choice_match(
            prediction,
            expected,
            row["allowed_labels"],
        )
        return passed, "choice"

    if typ == "json_exact":
        actual = extract_json(prediction)

        if actual is None:
            return False, "invalid_json"

        return json_equal(expected, actual), "json_exact"

    raise ValueError(f"unknown scoring_type: {typ}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    x = json.loads(
        Path(args.input).read_text(encoding="utf-8")
    )

    rows = []

    for row in x["results"]:
        passed, metric = score_one(row)

        out = dict(row)
        out["passed"] = passed
        out["metric"] = metric
        out["score"] = 1.0 if passed else 0.0

        rows.append(out)

    n = len(rows)
    passed_n = sum(r["passed"] for r in rows)

    critical_rows = [
        r for r in rows if r["critical"]
    ]

    critical_failed = [
        r["id"]
        for r in critical_rows
        if not r["passed"]
    ]

    categories = {}

    for category in sorted({r["category"] for r in rows}):
        sub = [
            r for r in rows
            if r["category"] == category
        ]

        ok = sum(r["passed"] for r in sub)

        categories[category] = {
            "n": len(sub),
            "passed": ok,
            "accuracy": ok / len(sub),
        }

    latencies = [
        r["latency_s"]
        for r in rows
        if not r.get("error")
    ]

    result = {
        "benchmark": x["benchmark"],
        "dataset_sha256": x["dataset_sha256"],
        "model": x["model"],
        "n": n,
        "passed": passed_n,
        "accuracy": passed_n / n,
        "critical_n": len(critical_rows),
        "critical_failed_n": len(critical_failed),
        "critical_failed_ids": critical_failed,
        "error_n": sum(bool(r.get("error")) for r in rows),
        "avg_latency_s": (
            sum(latencies) / len(latencies)
            if latencies else None
        ),
        "categories": categories,
        "results": rows,
    }

    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=== INTERNAL50 V2 SCORE ===")
    print("model     :", x["model"])
    print("score     :", f"{passed_n}/{n}")
    print("accuracy  :", f"{passed_n/n*100:.1f}%")
    print("critical failures:", len(critical_failed))
    print("errors    :", result["error_n"])

    print("\n=== CATEGORY ===")
    for name, s in categories.items():
        print(
            f"{name:36s} "
            f'{s["passed"]:2d}/{s["n"]:2d} '
            f'{s["accuracy"]*100:6.1f}%'
        )

    if critical_failed:
        print("\n=== CRITICAL FAIL IDS ===")
        for cid in critical_failed:
            print(cid)


if __name__ == "__main__":
    main()
