import argparse
import json
import re
import statistics
import time
from pathlib import Path

import requests

URL = "http://127.0.0.1:8000/v1/chat/completions"
DATA = "benchmarks/quantization/external50_v1.json"


# ============================================================
# Normalization
# ============================================================

def normalize_text(text):
    text = str(text).lower()

    text = text.replace(",", "")
    text = text.replace("$", "")
    text = text.replace("%", " percent ")

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9.\- ]+", " ", text)

    return " ".join(text.split())


def extract_numbers(text):
    text = str(text).replace(",", "")

    return [
        float(x)
        for x in re.findall(
            r"(?<![\w])-?\d+(?:\.\d+)?",
            text
        )
    ]


# ============================================================
# Text F1
# ============================================================

def token_f1(pred, gold):
    p = normalize_text(pred).split()
    g = normalize_text(gold).split()

    if not p or not g:
        return 0.0

    pc = {}
    gc = {}

    for x in p:
        pc[x] = pc.get(x, 0) + 1

    for x in g:
        gc[x] = gc.get(x, 0) + 1

    common = 0

    for token in set(pc) | set(gc):
        common += min(
            pc.get(token, 0),
            gc.get(token, 0)
        )

    if common == 0:
        return 0.0

    precision = common / len(p)
    recall = common / len(g)

    return (
        2 * precision * recall
        / (precision + recall)
    )


# ============================================================
# Numerical comparison
# ============================================================

def numeric_match(pred, gold):
    pred_nums = extract_numbers(pred)
    gold_nums = extract_numbers(gold)

    if not gold_nums or not pred_nums:
        return None

    # 모든 gold numeric value 중 하나라도 prediction에
    # tolerance 내 존재하는지 본다.
    hits = []

    for target in gold_nums:
        tolerance = max(
            abs(target) * 0.02,
            0.02
        )

        hit = any(
            abs(x - target) <= tolerance
            for x in pred_nums
        )

        hits.append(hit)

    return sum(hits) / len(hits)


# ============================================================
# Source-aware evaluator
# ============================================================

def evaluate(case, prediction):
    gold = case["gold_answer"]
    source = case["source"]

    num_score = numeric_match(
        prediction,
        gold
    )

    text_f1 = token_f1(
        prediction,
        gold
    )

    pred_norm = normalize_text(prediction)
    gold_norm = normalize_text(gold)

    containment = (
        len(gold_norm) > 1
        and gold_norm in pred_norm
    )

    exact = pred_norm == gold_norm


    # --------------------------------------------------------
    # FinQA
    #
    # 숫자 정답 비중이 높으므로 numeric match 우선.
    # yes/no 같은 textual answer면 text 평가.
    # --------------------------------------------------------

    if source == "FinQA":

        if num_score is not None:
            score = num_score
            passed = score >= 1.0
            metric = "numeric"

        else:
            score = max(
                text_f1,
                1.0 if containment else 0.0,
                1.0 if exact else 0.0,
            )

            passed = score >= 0.5
            metric = "text"


    # --------------------------------------------------------
    # TAT-QA
    #
    # multi-span / numerical / textual answer 모두 존재.
    # numeric + text 중 더 강한 신호 사용.
    # --------------------------------------------------------

    elif source == "TAT-QA":

        candidates = [text_f1]

        if num_score is not None:
            candidates.append(num_score)

        if containment:
            candidates.append(1.0)

        if exact:
            candidates.append(1.0)

        score = max(candidates)

        passed = score >= 0.5
        metric = "tatqa_hybrid"


    # --------------------------------------------------------
    # FinanceBench
    #
    # 답이 문장형일 수 있으므로 exact match보다
    # evidence-grounded answer overlap을 허용.
    # --------------------------------------------------------

    elif source == "FinanceBench":

        candidates = [text_f1]

        if num_score is not None:
            candidates.append(num_score)

        if containment:
            candidates.append(1.0)

        if exact:
            candidates.append(1.0)

        score = max(candidates)

        passed = score >= 0.5
        metric = "financebench_hybrid"

    else:
        raise ValueError(
            f"unknown source: {source}"
        )

    return {
        "score": round(score, 4),
        "passed": passed,
        "metric": metric,
        "text_f1": round(text_f1, 4),
        "numeric_score": (
            round(num_score, 4)
            if num_score is not None
            else None
        ),
    }


# ============================================================
# Inference
# ============================================================

def call_model(model, case):

    prompt = f"""Use ONLY the supplied financial evidence to answer the question.

Rules:
1. Do not invent facts not contained in the evidence.
2. Perform calculations when required.
3. Keep the final answer concise.
4. Preserve the requested unit or scale.
5. If the evidence is insufficient, explicitly say so.

EVIDENCE:
{case["context"]}

QUESTION:
{case["question"]}
"""

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise financial QA system. "
                    "Answer only from the supplied evidence."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0,
        "max_tokens": 384,
        "stream": False,
    }

    start = time.perf_counter()

    r = requests.post(
        URL,
        json=payload,
        timeout=180,
    )

    latency = time.perf_counter() - start

    r.raise_for_status()

    data = r.json()

    choice = data["choices"][0]
    usage = data.get("usage", {})

    return {
        "prediction": choice["message"]["content"],
        "finish_reason": choice.get(
            "finish_reason"
        ),
        "latency_s": round(
            latency,
            4
        ),
        "prompt_tokens": usage.get(
            "prompt_tokens"
        ),
        "completion_tokens": usage.get(
            "completion_tokens"
        ),
    }


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--model",
        required=True
    )

    parser.add_argument(
        "--out",
        required=True
    )

    args = parser.parse_args()

    dataset = json.load(
        open(
            DATA,
            encoding="utf-8"
        )
    )

    cases = dataset["cases"]

    if len(cases) != 50:
        raise RuntimeError(
            f"expected 50 cases, got {len(cases)}"
        )

    print("=" * 76)
    print("HgFinance External-50 Evaluation")
    print("benchmark :", dataset["benchmark"])
    print("model     :", args.model)
    print("cases     :", len(cases))
    print("=" * 76)

    results = []

    for i, case in enumerate(
        cases,
        1
    ):

        print(
            f"\n[{i:02d}/50] "
            f'{case["source"]:12s} '
            f'{case["id"]}'
        )

        try:

            response = call_model(
                args.model,
                case
            )

            evaluation = evaluate(
                case,
                response["prediction"]
            )

            row = {
                "id": case["id"],
                "source": case["source"],
                "question": case["question"],
                "gold": case["gold_answer"],
                **response,
                **evaluation,
            }

        except Exception as e:

            row = {
                "id": case["id"],
                "source": case["source"],
                "question": case["question"],
                "gold": case["gold_answer"],
                "prediction": "",
                "score": 0.0,
                "passed": False,
                "error": repr(e),
            }

        results.append(row)

        print(
            "PASS" if row["passed"] else "FAIL",
            f'score={row["score"]:.3f}',
            (
                f'latency={row["latency_s"]:.2f}s'
                if "latency_s" in row
                else ""
            )
        )

        if not row["passed"]:
            print(
                " GOLD:",
                row["gold"][:300]
            )

            print(
                " PRED:",
                row.get(
                    "prediction",
                    ""
                )[:500]
            )


    # ========================================================
    # Aggregate
    # ========================================================

    source_summary = {}

    for source in [
        "FinQA",
        "TAT-QA",
        "FinanceBench"
    ]:

        rows = [
            r for r in results
            if r["source"] == source
        ]

        source_summary[source] = {
            "n": len(rows),
            "passed": sum(
                r["passed"]
                for r in rows
            ),
            "pass_rate": round(
                sum(
                    r["passed"]
                    for r in rows
                ) / len(rows),
                4
            ),
            "mean_score": round(
                statistics.mean(
                    r["score"]
                    for r in rows
                ),
                4
            ),
            "avg_latency_s": round(
                statistics.mean(
                    r["latency_s"]
                    for r in rows
                    if "latency_s" in r
                ),
                4
            ),
        }


    passed = sum(
        r["passed"]
        for r in results
    )

    mean_score = statistics.mean(
        r["score"]
        for r in results
    )

    latencies = [
        r["latency_s"]
        for r in results
        if "latency_s" in r
    ]


    summary = {
        "benchmark": dataset["benchmark"],
        "seed": dataset["seed"],
        "model": args.model,
        "n": len(results),
        "passed": passed,
        "pass_rate": round(
            passed / len(results),
            4
        ),
        "mean_score": round(
            mean_score,
            4
        ),
        "avg_latency_s": round(
            statistics.mean(latencies),
            4
        ),
        "sources": source_summary,
        "results": results,
    }


    Path(args.out).write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


    print("\n" + "=" * 76)
    print("EXTERNAL-50 SUMMARY")
    print("=" * 76)

    print(
        f"PASS        = "
        f"{passed}/{len(results)} "
        f"({passed/len(results)*100:.1f}%)"
    )

    print(
        f"MEAN SCORE  = "
        f"{mean_score:.4f}"
    )

    print(
        f"AVG LATENCY = "
        f"{statistics.mean(latencies):.2f}s"
    )


    print("\nSOURCE BREAKDOWN")

    for source, s in source_summary.items():

        print(
            f"{source:14s} "
            f"score={s['mean_score']:.3f} "
            f"pass={s['passed']}/{s['n']} "
            f"latency={s['avg_latency_s']:.2f}s"
        )


    print(
        "\nSaved:",
        args.out
    )


if __name__ == "__main__":
    main()
