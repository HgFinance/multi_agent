import argparse
import json
import re
import statistics
from pathlib import Path


def normalize_text(text):
    text = str(text).lower()
    text = text.replace(",", "")
    text = text.replace("$", "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9.\-%() ]+", " ", text)
    return " ".join(text.split())


def accounting_negative_to_minus(text):
    """
    Convert accounting-style negatives:
      (1,581)      -> -1581
      $(1,161.33)  -> -1161.33
      €(2,088)m    -> -2088
    """
    text = str(text)

    pattern = re.compile(
        r"""
        [$€£]?
        \(
            \s*
            (\d[\d,]*(?:\.\d+)?)
            \s*
        \)
        """,
        re.VERBOSE,
    )

    return pattern.sub(
        lambda m: "-" + m.group(1).replace(",", ""),
        text,
    )


def extract_numbers(text):
    text = accounting_negative_to_minus(text)
    text = text.replace(",", "")

    out = []

    for m in re.finditer(
        r"(?<![\w])-?\d+(?:\.\d+)?\s*%?",
        text,
    ):
        raw = m.group(0).strip()
        is_percent = raw.endswith("%")
        raw_num = raw.rstrip("%").strip()

        try:
            value = float(raw_num)
        except ValueError:
            continue

        out.append({
            "value": value,
            "is_percent": is_percent,
            "raw": raw,
        })

    return out


def close(a, b):
    tol = max(
        abs(b) * 0.02,
        0.02,
    )
    return abs(a - b) <= tol


def numeric_equivalent(pred, gold):
    """
    Supports equivalent numerical forms:
      gold 0.9      <-> pred 90%
      gold 0.50195  <-> pred 50.20%
      gold -1581    <-> pred (1,581)
    """

    pnums = extract_numbers(pred)
    gnums = extract_numbers(gold)

    if not pnums or not gnums:
        return None

    hits = []

    for g in gnums:
        gv = g["value"]
        matched = False

        for p in pnums:
            pv = p["value"]

            candidates = [pv]

            if p["is_percent"]:
                candidates.append(pv / 100.0)

            raw_num = re.escape(
                p["raw"].rstrip("%").strip()
            )

            if re.search(
                rf"{raw_num}\s*(percent|percentage)",
                pred,
                flags=re.IGNORECASE,
            ):
                candidates.append(pv / 100.0)

            candidates.append(pv * 100.0)

            if any(
                close(candidate, gv)
                for candidate in candidates
            ):
                matched = True
                break

        hits.append(matched)

    return sum(hits) / len(hits)


def token_f1(pred, gold):
    p = normalize_text(pred).split()
    g = normalize_text(gold).split()

    if not p or not g:
        return 0.0

    pc = {}
    gc = {}

    for token in p:
        pc[token] = pc.get(token, 0) + 1

    for token in g:
        gc[token] = gc.get(token, 0) + 1

    common = sum(
        min(pc.get(token, 0), gc.get(token, 0))
        for token in set(pc) | set(gc)
    )

    if common == 0:
        return 0.0

    precision = common / len(p)
    recall = common / len(g)

    return (
        2 * precision * recall
        / (precision + recall)
    )


def score_case(row):
    source = row["source"]
    pred = row.get("prediction", "")
    gold = row["gold"]

    text_f1 = token_f1(pred, gold)
    num_score = numeric_equivalent(pred, gold)

    pred_norm = normalize_text(pred)
    gold_norm = normalize_text(gold)

    exact = pred_norm == gold_norm

    containment = (
        len(gold_norm) > 1
        and gold_norm in pred_norm
    )

    if source == "FinQA":
        if num_score is not None:
            score = num_score
            passed = score >= 1.0
            metric = "numeric_v2"
        else:
            score = max(
                text_f1,
                1.0 if exact else 0.0,
                1.0 if containment else 0.0,
            )
            passed = score >= 0.5
            metric = "text_v2"

        return {
            "score_v2": round(score, 4),
            "passed_v2": passed,
            "metric_v2": metric,
        }

    if source == "TAT-QA":
        candidates = [text_f1]

        if num_score is not None:
            candidates.append(num_score)

        if exact or containment:
            candidates.append(1.0)

        score = max(candidates)

        return {
            "score_v2": round(score, 4),
            "passed_v2": score >= 0.5,
            "metric_v2": "tatqa_hybrid_v2",
        }

    if source == "FinanceBench":
        candidates = [text_f1]

        if num_score is not None:
            candidates.append(num_score)

        if exact or containment:
            candidates.append(1.0)

        score = max(candidates)

        return {
            "score_v2": round(score, 4),
            "passed_v2": None,
            "metric_v2": "manual_required",
        }

    raise ValueError(source)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--raw",
        required=True,
    )

    parser.add_argument(
        "--out",
        required=True,
    )

    args = parser.parse_args()

    raw = json.load(
        open(
            args.raw,
            encoding="utf-8",
        )
    )

    results = []

    for row in raw["results"]:
        new = dict(row)
        new.update(score_case(row))
        results.append(new)

    auto_rows = [
        row
        for row in results
        if row["source"] in {
            "FinQA",
            "TAT-QA",
        }
    ]

    auto_passed = sum(
        bool(row["passed_v2"])
        for row in auto_rows
    )

    auto_mean = statistics.mean(
        row["score_v2"]
        for row in auto_rows
    )

    source_summary = {}

    for source in [
        "FinQA",
        "TAT-QA",
        "FinanceBench",
    ]:
        rows = [
            row
            for row in results
            if row["source"] == source
        ]

        if source == "FinanceBench":
            source_summary[source] = {
                "n": len(rows),
                "status": "manual_required",
                "diagnostic_mean": round(
                    statistics.mean(
                        row["score_v2"]
                        for row in rows
                    ),
                    4,
                ),
            }

        else:
            passed = sum(
                bool(row["passed_v2"])
                for row in rows
            )

            source_summary[source] = {
                "n": len(rows),
                "passed": passed,
                "pass_rate": round(
                    passed / len(rows),
                    4,
                ),
                "mean_score": round(
                    statistics.mean(
                        row["score_v2"]
                        for row in rows
                    ),
                    4,
                ),
            }

    output = {
        "scorer": "HgFinance-External50-Scorer-v2",
        "raw_file": args.raw,
        "model": raw["model"],
        "auto_scored_n": len(auto_rows),
        "auto_passed": auto_passed,
        "auto_pass_rate": round(
            auto_passed / len(auto_rows),
            4,
        ),
        "auto_mean_score": round(
            auto_mean,
            4,
        ),
        "financebench_status": "manual_required",
        "sources": source_summary,
        "results": results,
    }

    Path(args.out).write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 72)
    print("HgFinance External-50 Rescore v2")
    print("=" * 72)

    print(
        "model:",
        raw["model"],
    )

    for source, summary in source_summary.items():
        if source == "FinanceBench":
            print(
                f"{source:14s}",
                f"n={summary['n']}",
                "MANUAL REQUIRED",
                f"diagnostic={summary['diagnostic_mean']:.3f}",
            )
        else:
            print(
                f"{source:14s}",
                f"score={summary['mean_score']:.3f}",
                f"pass={summary['passed']}/{summary['n']}",
            )

    print()
    print(
        f"AUTO subtotal: "
        f"{auto_passed}/{len(auto_rows)} "
        f"({auto_passed/len(auto_rows)*100:.1f}%)"
    )

    print(
        f"AUTO mean: {auto_mean:.4f}"
    )

    print()
    print(
        "FinanceBench 15 cases require manual adjudication."
    )

    print(
        "Saved:",
        args.out,
    )


if __name__ == "__main__":
    main()
