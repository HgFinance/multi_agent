#!/usr/bin/env python3
"""Run the generic Hybrid pipeline without answer-key or case-ID rules.

The pipeline has three reusable mechanisms:

* numeric contracts ask the LLM for ``EXPR: ...`` and execute only that
  expression in :mod:`safe_expression`;
* structured contracts use an application/request schema with vLLM guided
  JSON, JSON Schema validation, and one LLM semantic-audit retry;
* accounting/FinanceBench prompts may receive deterministic glossary RAG.

There is no deterministic answer fallback.  A failed expression or an
unverified structured response remains an error for scoring and diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from benchmarks.quantization.glossary_rag import inject, load_glossary
from benchmarks.quantization.safe_expression import (
    ExpressionError,
    evaluate_response,
    format_value,
)
from benchmarks.quantization.structured_output import (
    infer_schema_from_contract,
    retry_instruction,
    validate_json,
    vllm_response_format,
)


ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
INTERNAL_DATA = Path("benchmarks/quantization/internal50_v2_reasoning.json")
EXTERNAL_DATA = Path("benchmarks/quantization/external50_v1.json")
GLOSSARY_DATA = Path("benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json")
TIMEOUT_SECONDS = 180
MAX_TOKENS = 384


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_model(
    *,
    url: str,
    model: str,
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    started = time.perf_counter()
    try:
        result = post_json(url, payload, timeout)
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"model request failed: {type(exc).__name__}: {exc}") from exc
    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = result.get("usage", {})
    return {
        "content": str(content or ""),
        "finish_reason": choice.get("finish_reason"),
        "latency_s": round(time.perf_counter() - started, 4),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def _glossary_prompt(
    prompt: str,
    glossary: tuple[str, list[Any]] | None,
    *,
    query: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if glossary is None:
        return prompt, {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    digest, entries = glossary
    injected, terms = inject(prompt, entries, query=query)
    return injected, {
        "version": "bok-800-arithmetic-glossary-v1",
        "sha256": digest,
        "matched_terms": terms,
        "hit": bool(terms),
    }


def _base_prompt(context: str, question: str, contract: str) -> str:
    return (
        "Use only the supplied context. Do not use a gold answer or outside facts.\n\n"
        f"CONTEXT:\n{context}\n\nQUESTION:\n{question}\n\nOUTPUT CONTRACT:\n{contract}"
    )


def _numeric_messages(case: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    system = (
        case.get("system_prompt")
        or "You are a general financial QA model."
    ) + (
        " For a numeric calculation, translate the supplied facts into one "
        "pure arithmetic expression. Return exactly EXPR: followed by the "
        "expression and nothing else. Use explicit decimal factors such as "
        "0.18 / 100 for 0.18%. Convert all source units before calculating, "
        "and when the requested result is a percentage return percentage "
        "points (multiply a ratio by 100). Never use variable names, "
        "functions, modulo, or prose."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def _text_messages(case: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    system = case.get("system_prompt") or "You are a general financial QA model."
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def _structured_messages(case: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    system = case.get("system_prompt") or "You are a general financial QA model."
    system += (
        " Return only JSON that satisfies the supplied response schema. "
        "Do not add markdown, commentary, or unrequested keys."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def _run_numeric(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    messages = _numeric_messages(case, prompt)
    attempts = []
    for attempt in range(2):
        if attempt:
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "The previous response was not a safe expression. "
                        "Retry with exactly one line in the form EXPR: "
                        "<numeric expression>; do not answer from memory."
                    ),
                },
            ]
        try:
            response = call_model(url=url, model=model, messages=messages, timeout=timeout)
            attempts.append(response)
            evaluated = evaluate_response(response["content"])
            # Generic semantic/unit audit. The second model call may correct
            # the expression, but the server never invents a replacement
            # expression or answer.
            audit_messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": (
                        "Audit this expression against the supplied context. "
                        "Check every unit conversion, percentage-to-points "
                        "conversion, sign, and operation. Return the corrected "
                        "expression only as EXPR: <expression>; if it is correct, "
                        "repeat it exactly."
                    ),
                },
            ]
            try:
                audited = call_model(url=url, model=model, messages=audit_messages, timeout=timeout)
                attempts.append({**audited, "phase": "semantic_audit"})
                audited_value = evaluate_response(audited["content"])
                return {
                    "prediction": format_value(audited_value.value),
                    "raw_prediction": audited["content"],
                    "draft_prediction": response["content"],
                    "final_source": "llm_expression_ast_semantic_audit",
                    "expression": audited_value.expression,
                    "calculator_value": audited_value.value,
                    "attempts": attempts,
                    "error": None,
                }
            except (ExpressionError, RuntimeError) as audit_error:
                attempts[-1]["audit_error"] = str(audit_error)
                return {
                    "prediction": format_value(evaluated.value),
                    "raw_prediction": response["content"],
                    "draft_prediction": response["content"],
                    "final_source": "llm_expression_ast",
                    "expression": evaluated.expression,
                    "calculator_value": evaluated.value,
                    "attempts": attempts,
                    "audit_error": str(audit_error),
                    "error": None,
                }
        except (ExpressionError, RuntimeError) as exc:
            if not attempts:
                attempts.append({"content": "", "error": str(exc)})
            else:
                attempts[-1]["error"] = str(exc)
            if attempt == 1:
                return {
                    "prediction": "",
                    "raw_prediction": attempts[-1].get("content", ""),
                    "final_source": "expression_error",
                    "expression": None,
                    "calculator_value": None,
                    "attempts": attempts,
                    "error": str(exc),
                }
    raise AssertionError("unreachable")


def _run_structured(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    schema = infer_schema_from_contract(case["context"])
    if schema is None:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "structured_schema_missing",
            "schema": None,
            "semantic_audit": None,
            "error": "no explicit JSON schema contract was supplied",
        }
    response_format = vllm_response_format(schema)
    messages = _structured_messages(case, prompt)
    attempts: list[dict[str, Any]] = []
    for phase in ("initial", "retry"):
        try:
            response = call_model(
                url=url,
                model=model,
                messages=messages,
                response_format=response_format,
                timeout=timeout,
            )
        except RuntimeError as exc:
            return {
                "prediction": "",
                "raw_prediction": "",
                "final_source": "structured_request_error",
                "schema": schema,
                "semantic_audit": None,
                "attempts": attempts,
                "error": str(exc),
            }
        validation = validate_json(response["content"], schema)
        attempt = {**response, "phase": phase, "valid": validation.valid, "validation_error": validation.error}
        attempts.append(attempt)
        if validation.valid:
            # A second LLM pass is a generic context audit, not an answer-key
            # fallback. It receives the same schema and can only emit JSON.
            audit_messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": (
                        "Audit the candidate JSON against the supplied context. "
                        "Correct it only if the context contradicts it; do not "
                        "invent missing values. Preserve exact enum values and "
                        "the requested key types. Return the final JSON only."
                    ),
                },
            ]
            try:
                audited = call_model(
                    url=url,
                    model=model,
                    messages=audit_messages,
                    response_format=response_format,
                    timeout=timeout,
                )
                audit_validation = validate_json(audited["content"], schema)
                attempts.append({
                    **audited,
                    "phase": "semantic_audit",
                    "valid": audit_validation.valid,
                    "validation_error": audit_validation.error,
                })
                if audit_validation.valid:
                    return {
                        "prediction": audited["content"],
                        "raw_prediction": response["content"],
                        "final_source": "guided_json_semantic_audit",
                        "schema": schema,
                        "semantic_audit": True,
                        "attempts": attempts,
                        "error": None,
                    }
            except RuntimeError as exc:
                attempts.append({"phase": "semantic_audit", "error": str(exc)})
            return {
                "prediction": response["content"],
                "raw_prediction": response["content"],
                "final_source": "guided_json_schema_validated",
                "schema": schema,
                "semantic_audit": False,
                "attempts": attempts,
                "error": None,
            }
        messages = [
            *messages,
            {"role": "assistant", "content": response["content"]},
            {"role": "user", "content": retry_instruction(schema, validation.error or "invalid response")},
        ]
    return {
        "prediction": attempts[-1].get("content", "") if attempts else "",
        "raw_prediction": attempts[-1].get("content", "") if attempts else "",
        "final_source": "structured_validation_error",
        "schema": schema,
        "semantic_audit": False,
        "attempts": attempts,
        "error": attempts[-1].get("validation_error", "structured validation failed") if attempts else "no response",
    }


def _run_case(
    *,
    case: dict[str, Any],
    source: str,
    url: str,
    model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
) -> dict[str, Any]:
    scoring_type = case.get("scoring_type")
    if source == "internal":
        context = case["context"]
        question = case["question"]
        if scoring_type == "numeric":
            contract = "Return one EXPR line for the requested numeric result."
        elif scoring_type == "choice":
            labels = ", ".join(case.get("allowed_labels", []))
            contract = f"Return exactly one allowed label ({labels}) and no explanation."
        elif scoring_type == "json_exact":
            contract = "Return only the JSON object requested in CONTEXT."
        else:
            contract = "Answer concisely using only the context."
    else:
        context = case["context"]
        question = case["question"]
        contract = "Answer concisely from the supplied evidence."

    glossary_meta = {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    use_glossary = source == "external" and case.get("source") == "FinanceBench"
    if source == "internal" and case.get("category") == "accounting_reasoning":
        use_glossary = True
    if use_glossary:
        context, glossary_meta = _glossary_prompt(context, glossary, query=question)
    prompt = _base_prompt(context, question, contract)

    started = time.perf_counter()
    if source == "internal" and scoring_type == "numeric":
        outcome = _run_numeric(case=case, prompt=prompt, url=url, model=model, timeout=timeout)
    elif source == "internal" and scoring_type == "json_exact":
        outcome = _run_structured(case=case, prompt=prompt, url=url, model=model, timeout=timeout)
    elif source == "internal":
        try:
            response = call_model(
                url=url,
                model=model,
                messages=_text_messages(case, prompt),
                timeout=timeout,
            )
            outcome = {
                "prediction": response["content"],
                "raw_prediction": response["content"],
                "final_source": "llm_text",
                "route": "text",
                "attempts": [response],
                "error": None,
            }
        except RuntimeError as exc:
            outcome = {
                "prediction": "",
                "raw_prediction": "",
                "final_source": "request_error",
                "route": "text",
                "attempts": [],
                "error": str(exc),
            }
    else:
        outcome = _run_external(
            case=case,
            prompt=prompt,
            url=url,
            model=model,
            timeout=timeout,
        )

    result = dict(case)
    result.update(outcome)
    result.update({
        "pipeline_variant": "AWQ+HybridGenericPipeline",
        "source_dataset": source,
        "glossary_version": glossary_meta["version"],
        "glossary_sha256": glossary_meta["sha256"],
        "matched_terms": glossary_meta["matched_terms"],
        "glossary_hit": glossary_meta["hit"],
        "latency_s": round(time.perf_counter() - started, 4),
    })
    return result


def _run_external(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Use an LLM router so text questions are not forced into arithmetic."""

    route_schema = {
        "type": "object",
        "properties": {"task_type": {"type": "string", "enum": ["CALCULATION", "TEXT"]}},
        "required": ["task_type"],
        "additionalProperties": False,
    }
    route_prompt = (
        f"{prompt}\n\nClassify the requested task before answering. "
        "Use CALCULATION only when the question explicitly asks to compute "
        "a numeric amount, ratio, percentage, or formula result. Use TEXT for "
        "selection, comparison, explanation, yes/no, or list questions. "
        "Return only the JSON route object."
    )
    try:
        routed = call_model(
            url=url,
            model=model,
            messages=_text_messages(case, route_prompt),
            response_format=vllm_response_format(route_schema, name="task_route"),
            timeout=timeout,
        )
        route_validation = validate_json(routed["content"], route_schema)
        if not route_validation.valid:
            raise RuntimeError(route_validation.error or "invalid task route")
        task_type = route_validation.value["task_type"]
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "route_error",
            "route": None,
            "attempts": [],
            "error": str(exc),
        }

    if task_type == "CALCULATION":
        outcome = _run_numeric(case=case, prompt=prompt, url=url, model=model, timeout=timeout)
        outcome["route"] = "calculation"
        outcome["router"] = routed
        return outcome

    try:
        response = call_model(url=url, model=model, messages=_text_messages(case, prompt), timeout=timeout)
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "request_error",
            "route": "text",
            "router": routed,
            "attempts": [],
            "error": str(exc),
        }
    return {
        "prediction": response["content"],
        "raw_prediction": response["content"],
        "final_source": "llm_text",
        "route": "text",
        "router": routed,
        "attempts": [response],
        "error": None,
    }


def run_internal(args: argparse.Namespace, glossary: tuple[str, list[Any]] | None) -> dict[str, Any]:
    dataset = json.loads(args.internal.read_text(encoding="utf-8"))
    rows = []
    for index, case in enumerate(dataset["cases"], start=1):
        print(f"internal {index}/{len(dataset['cases'])}: {case['id']}", file=sys.stderr)
        rows.append(_run_case(case=case, source="internal", url=args.url, model=args.model, timeout=args.timeout, glossary=glossary))
    return {
        "benchmark": dataset["benchmark"],
        "dataset_sha256": sha256(args.internal),
        "model": args.model,
        "pipeline_variant": "AWQ+HybridGenericPipeline",
        "policy": "generic_expression_ast_guided_json_semantic_audit_accounting_rag_no_fallback",
        "results": rows,
    }


def run_external(args: argparse.Namespace, glossary: tuple[str, list[Any]] | None) -> dict[str, Any]:
    dataset = json.loads(args.external.read_text(encoding="utf-8"))
    rows = []
    for index, case in enumerate(dataset["cases"], start=1):
        print(f"external {index}/{len(dataset['cases'])}: {case['id']}", file=sys.stderr)
        row = _run_case(case=case, source="external", url=args.url, model=args.model, timeout=args.timeout, glossary=glossary)
        rows.append({
            "id": case["id"],
            "source": case["source"],
            "question": case["question"],
            "gold": case["gold_answer"],
            **{k: row.get(k) for k in (
                "prediction", "raw_prediction", "final_source", "expression", "calculator_value",
                "route", "router",
                "finish_reason", "latency_s", "prompt_tokens", "completion_tokens", "error",
                "glossary_version", "glossary_sha256", "matched_terms", "glossary_hit", "attempts",
            )},
        })
    return {
        "benchmark": dataset.get("benchmark", "HgFinance-External50-v1"),
        "seed": dataset.get("seed"),
        "dataset_sha256": sha256(args.external),
        "model": args.model,
        "pipeline_variant": "AWQ+HybridGenericPipeline",
        "policy": "generic_expression_ast_guided_json_semantic_audit_accounting_rag_no_fallback",
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--url", default=ENDPOINT)
    parser.add_argument("--internal", type=Path, default=INTERNAL_DATA)
    parser.add_argument("--external", type=Path, default=EXTERNAL_DATA)
    parser.add_argument("--glossary", type=Path, default=GLOSSARY_DATA)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    parser.add_argument("--only", choices=("all", "internal", "external"), default="all")
    args = parser.parse_args()

    glossary = load_glossary(args.glossary)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    internal = run_internal(args, glossary) if args.only in {"all", "internal"} else None
    external = run_external(args, glossary) if args.only in {"all", "external"} else None
    if internal is not None:
        (args.output_dir / "internal50_raw.json").write_text(json.dumps(internal, ensure_ascii=False, indent=2), encoding="utf-8")
    if external is not None:
        (args.output_dir / "external50_raw.json").write_text(json.dumps(external, ensure_ascii=False, indent=2), encoding="utf-8")
    provenance = {
        "schema_version": "aws-hybrid-generic-provenance.v1",
        "variant": "AWQ+HybridGenericPipeline",
        "model": args.model,
        "endpoint": args.url,
        "datasets": {
            "internal50_v2_sha256": internal["dataset_sha256"] if internal else None,
            "external50_v1_sha256": external["dataset_sha256"] if external else None,
        },
        "glossary": {
            "sha256": glossary[0],
            "scope": "FinanceBench and internal accounting context only",
        },
        "no_deterministic_answer_fallback": True,
        "structured_output": {
            "guided_json": True,
            "json_schema_validation": True,
            "semantic_audit_retry": True,
            "unsupported_schema": "HOLD/error",
        },
        "arithmetic": {
            "llm_output": "EXPR:<pure arithmetic expression>",
            "execution": "safe AST; numeric literals and + - * / ** only",
            "answer_rules": "none",
        },
    }
    (args.output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
