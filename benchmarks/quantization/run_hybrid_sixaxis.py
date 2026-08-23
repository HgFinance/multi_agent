#!/usr/bin/env python3
"""Run the existing sixth-axis Hybrid pipeline with selective routing.

This is intentionally not a new benchmark axis.  It is the Hybrid column's
implementation: arithmetic uses an LLM-written ``EXPR:`` plus safe AST
evaluation, accounting/FinanceBench may receive query-scoped glossary RAG,
structured output uses a generic control envelope plus guided JSON validation,
and all other requests remain on the AWQ text path.

There are no answer-key, case-ID, or deterministic answer fallbacks.  The
control envelope is unwrapped only after schema validation; its fields are not
added to the frozen benchmark answer schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from unittest import mock
from pathlib import Path
from typing import Any

from benchmarks.quantization.glossary_rag import (
    inject,
    load_glossary,
    load_selective_v2_glossary,
)
from benchmarks.quantization.run_hybrid_generic import (
    ENDPOINT,
    EXTERNAL_DATA,
    GLOSSARY_DATA,
    INTERNAL_DATA,
    TIMEOUT_SECONDS,
    _base_prompt,
    _run_numeric,
    _run_structured,
    _structured_messages,
    _text_messages,
    call_model,
    sha256,
)
from benchmarks.quantization import run_hybrid_generic as generic_pipeline
from benchmarks.quantization.structured_output import (
    control_envelope_schema,
    infer_schema_from_contract,
    retry_instruction,
    unwrap_control_envelope,
    validate_json,
    vllm_response_format,
)
from benchmarks.quantization.safe_expression import ExpressionError, evaluate_response, format_value


def _glossary_prompt(
    prompt: str,
    glossary: tuple[str, list[Any]] | None,
    *,
    query: str,
    version: str = "bok-800-arithmetic-glossary-v1",
) -> tuple[str, dict[str, Any]]:
    if glossary is None:
        return prompt, {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    digest, entries = glossary
    injected, terms = inject(prompt, entries, query=query)
    return injected, {
        "version": version,
        "sha256": digest,
        "matched_terms": terms,
        "hit": bool(terms),
    }


def _structured_envelope_messages(case: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    messages = _structured_messages(case, prompt)
    messages[0] = {
        "role": "system",
            "content": messages[0]["content"]
        + (
            " Use this internal control envelope exactly: status must be SUCCESS, "
            "INSUFFICIENT_DATA, or INVALID; put the requested answer in result; "
            "use missing_params for unavailable inputs; never invent a result. "
            "Even for INSUFFICIENT_DATA, populate result with the exact requested "
            "application object when its schema defines a status/value response; "
            "the envelope result must not be null unless no valid application object "
            "can be produced."
        ),
    }
    return messages


def _run_structured_envelope(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Validate a generic envelope, then return the frozen exact result only."""

    result_schema = infer_schema_from_contract(case["context"])
    if result_schema is None:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "structured_schema_missing",
            "schema": None,
            "control_envelope": None,
            "error": "no explicit JSON schema contract was supplied",
        }

    envelope_schema = control_envelope_schema(result_schema)
    response_format = vllm_response_format(envelope_schema, name="hybrid_control_envelope")
    messages = _structured_envelope_messages(case, prompt)
    attempts: list[dict[str, Any]] = []
    accepted: dict[str, Any] | None = None
    accepted_result: Any | None = None

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
                "schema": result_schema,
                "envelope_schema": envelope_schema,
                "control_envelope": None,
                "attempts": attempts,
                "error": str(exc),
            }

        envelope_validation = validate_json(response["content"], envelope_schema)
        result_validation = unwrap_control_envelope(response["content"], result_schema)
        envelope_value = None
        if envelope_validation.valid:
            envelope_value = envelope_validation.value
        attempt = {
            **response,
            "phase": phase,
            "envelope_valid": envelope_validation.valid,
            "result_valid": result_validation.valid,
            "validation_error": result_validation.error or envelope_validation.error,
            "control_envelope": envelope_value,
        }
        attempts.append(attempt)

        if envelope_validation.valid and result_validation.valid:
            accepted = envelope_value
            accepted_result = result_validation.value

            audit_messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": (
                        "Audit the envelope against the supplied context. Correct only "
                        "unsupported or contradictory values. If data is missing, use "
                        "INSUFFICIENT_DATA and list missing_params. Return the same "
                        "envelope schema only; never add keys or invent values."
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
                audit_envelope = validate_json(audited["content"], envelope_schema)
                audit_result = unwrap_control_envelope(audited["content"], result_schema)
                attempts.append({
                    **audited,
                    "phase": "semantic_audit",
                    "envelope_valid": audit_envelope.valid,
                    "result_valid": audit_result.valid,
                    "validation_error": audit_result.error or audit_envelope.error,
                    "control_envelope": audit_envelope.value if audit_envelope.valid else None,
                })
                if audit_envelope.valid and audit_result.valid:
                    accepted = audit_envelope.value
                    accepted_result = audit_result.value
                    return {
                        "prediction": json.dumps(accepted_result, ensure_ascii=False, separators=(",", ":")),
                        "raw_prediction": response["content"],
                        "final_raw_prediction": audited["content"],
                        "final_source": "guided_json_envelope_semantic_audit",
                        "schema": result_schema,
                        "envelope_schema": envelope_schema,
                        "control_envelope": accepted,
                        "attempts": attempts,
                        "error": None,
                    }
            except RuntimeError as exc:
                attempts.append({"phase": "semantic_audit", "error": str(exc)})

            return {
                "prediction": json.dumps(accepted_result, ensure_ascii=False, separators=(",", ":")),
                "raw_prediction": response["content"],
                "final_source": "guided_json_envelope_validated",
                "schema": result_schema,
                "envelope_schema": envelope_schema,
                "control_envelope": accepted,
                "attempts": attempts,
                "error": None,
            }

        messages = [
            *messages,
            {"role": "assistant", "content": response["content"]},
            {
                "role": "user",
                "content": retry_instruction(
                    envelope_schema,
                    result_validation.error or envelope_validation.error or "invalid envelope",
                ),
            },
        ]

    return {
        "prediction": "",
        "raw_prediction": attempts[-1].get("content", "") if attempts else "",
        "final_source": "structured_validation_error",
        "schema": result_schema,
        "envelope_schema": envelope_schema,
        "control_envelope": attempts[-1].get("control_envelope") if attempts else None,
        "attempts": attempts,
        "error": attempts[-1].get("validation_error", "structured envelope validation failed") if attempts else "no response",
    }


_STAGE_SUFFIXES = {
    "unit_normalization": (
        " Before writing EXPR, normalize every unit before doing arithmetic. "
        "Convert percent notation p% to p/100 (for example 0.015% becomes "
        "0.015/100, never 0.015). Expand billion, million, and thousand into "
        "their numeric scale, and make numerator and denominator use the same "
        "unit. Keep the requested result unit explicit in the expression."
    ),
    "domain_formula": (
        " Apply a supplied domain formula only when its terms are present. "
        "For break-even sale price, use sale_price*(1-sell_fee_rate) = "
        "buy_price*(1+buy_fee_rate). For a target position, use additional "
        "shares = floor(((target_weight-current_weight)*NAV)/share_price); "
        "the result unit is shares, not currency. Do not invent inputs."
    ),
    "fifo_fewshot": (
        " For FIFO inventory, consume the oldest lot first and then the next "
        "lot only for any remaining quantity. Generic example: lots 5 shares "
        "at 10 and 7 shares at 12, sell 8 at 15 with fee 2; cost basis is "
        "5*10 + 3*12 and realized PnL is 8*15 - (5*10 + 3*12) - 2. "
        "Use this as a method example, not as a benchmark answer."
    ),
}


_NUMERIC_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "Pure arithmetic expression; no EXPR prefix, variables, or prose.",
        },
        "answer_type": {
            "type": "string",
            "enum": ["scalar", "percentage", "ratio", "currency", "shares", "count"],
        },
        "result_unit": {"type": "string"},
        "scale": {
            "type": "string",
            "description": "Source/result scale such as 1, 1e3, 1e6, 1e9, or percent-points.",
        },
        "formula_name": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": [
        "expression",
        "answer_type",
        "result_unit",
        "scale",
        "formula_name",
        "explanation",
    ],
    "additionalProperties": False,
}


def _numeric_metadata_messages(case: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    system = (
        case.get("system_prompt") or "You are a general financial QA model."
    ) + (
        " For an explicit numeric calculation, return only the supplied JSON "
        "calculation contract. Write a pure arithmetic expression using numeric "
        "literals and + - * / ** only. Normalize percent and thousand/million/billion "
        "scales before writing the expression. Do not use variables or invent inputs. "
        "The expression is evaluated separately by a safe AST calculator; the other "
        "fields are semantic metadata, not a place to put a benchmark answer."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def _run_numeric_with_metadata(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Use guided metadata plus an LLM expression, then calculate safely.

    This is generic: no case IDs, gold answers, or deterministic answer
    replacements are consulted. A valid model-produced expression is the only
    input to the AST evaluator.
    """

    messages = _numeric_metadata_messages(case, prompt)
    response_format = vllm_response_format(_NUMERIC_METADATA_SCHEMA, name="numeric_calculation_contract")
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
                "final_source": "numeric_metadata_request_error",
                "numeric_metadata": None,
                "attempts": attempts,
                "error": str(exc),
            }

        validation = validate_json(response["content"], _NUMERIC_METADATA_SCHEMA)
        attempt = {**response, "phase": phase, "valid": validation.valid, "validation_error": validation.error}
        attempts.append(attempt)
        if not validation.valid:
            messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": retry_instruction(
                        _NUMERIC_METADATA_SCHEMA,
                        validation.error or "invalid numeric calculation contract",
                    ),
                },
            ]
            continue

        metadata = validation.value
        expression = str(metadata["expression"]).strip()
        if expression.upper().startswith("EXPR:"):
            expression = expression.split(":", 1)[1].strip()
        try:
            evaluated = evaluate_response(f"EXPR: {expression}")
        except ExpressionError as exc:
            attempts[-1]["evaluation_error"] = str(exc)
            messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": (
                        "The expression is not safe or valid. Return the same JSON "
                        "schema with a pure numeric expression using only literals "
                        "and + - * / **; do not answer from memory."
                    ),
                },
            ]
            continue

        return {
            "prediction": format_value(evaluated.value),
            "raw_prediction": response["content"],
            "draft_prediction": response["content"],
            "final_source": "guided_numeric_metadata_ast",
            "expression": evaluated.expression,
            "calculator_value": evaluated.value,
            "numeric_metadata": {**metadata, "expression": evaluated.expression},
            "attempts": attempts,
            "error": None,
        }

    return {
        "prediction": attempts[-1].get("content", "") if attempts else "",
        "raw_prediction": attempts[-1].get("content", "") if attempts else "",
        "final_source": "numeric_metadata_validation_error",
        "numeric_metadata": None,
        "attempts": attempts,
        "error": attempts[-1].get("validation_error", "numeric metadata validation failed") if attempts else "no response",
    }


def _stage_numeric(
    *,
    stage: str,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Run the same EXPR/AST path with cumulative generic prompt treatments."""

    if stage == "selective_unit_scale":
        return _run_numeric_with_metadata(
            case=case, prompt=prompt, url=url, model=model, timeout=timeout
        )

    suffix = ""
    if stage in {"unit_normalization", "domain_formula", "fifo_fewshot", "structured_envelope"}:
        suffix += _STAGE_SUFFIXES["unit_normalization"]
    if stage in {"domain_formula", "fifo_fewshot", "structured_envelope"}:
        suffix += _STAGE_SUFFIXES["domain_formula"]
    if stage in {"fifo_fewshot", "structured_envelope"} and (
        "fifo" in prompt.casefold() or "fifo" in case.get("context", "").casefold()
    ):
        suffix += _STAGE_SUFFIXES["fifo_fewshot"]
    if not suffix:
        return _run_numeric(case=case, prompt=prompt, url=url, model=model, timeout=timeout)

    original = generic_pipeline._numeric_messages

    def messages_with_stage(stage_case: dict[str, Any], stage_prompt: str) -> list[dict[str, str]]:
        messages = original(stage_case, stage_prompt)
        messages[0] = {**messages[0], "content": messages[0]["content"] + suffix}
        return messages

    # _run_numeric resolves _numeric_messages in its defining module. Patch
    # only that function for this invocation; no global runtime state remains.
    with mock.patch.object(generic_pipeline, "_numeric_messages", messages_with_stage):
        return _run_numeric(case=case, prompt=prompt, url=url, model=model, timeout=timeout)


def _run_text(*, case: dict[str, Any], prompt: str, url: str, model: str, timeout: float) -> dict[str, Any]:
    try:
        response = call_model(url=url, model=model, messages=_text_messages(case, prompt), timeout=timeout)
        return {
            "prediction": response["content"],
            "raw_prediction": response["content"],
            "final_source": "awq_text_passthrough",
            "route": "text_passthrough",
            "attempts": [response],
            "error": None,
        }
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "request_error",
            "route": "text_passthrough",
            "attempts": [],
            "error": str(exc),
        }


def _run_internal_case(
    *,
    case: dict[str, Any],
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
    stage: str,
) -> dict[str, Any]:
    context = case["context"]
    question = case["question"]
    scoring_type = case.get("scoring_type")
    if scoring_type == "numeric":
        contract = "Return one EXPR line for the requested numeric result."
    elif scoring_type == "choice":
        labels = ", ".join(case.get("allowed_labels", []))
        contract = f"Return exactly one allowed label ({labels}) and no explanation."
    elif scoring_type == "json_exact":
        contract = "Return only the JSON object requested in CONTEXT."
    else:
        contract = "Answer concisely using only the context."

    glossary_meta = {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    if case.get("category") == "accounting_reasoning":
        context, glossary_meta = _glossary_prompt(
            context,
            glossary,
            query=question,
            version=(
                "bok-800-arithmetic-glossary-v2-selective"
                if stage == "selective_unit_scale"
                else "bok-800-arithmetic-glossary-v1"
            ),
        )
    prompt = _base_prompt(context, question, contract)
    started = time.perf_counter()

    if scoring_type == "numeric":
        outcome = _stage_numeric(
            stage=stage, case=case, prompt=prompt, url=url, model=arithmetic_model, timeout=timeout
        )
        route = "expr_ast"
    elif scoring_type == "json_exact":
        if stage == "structured_envelope":
            outcome = _run_structured_envelope(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
            route = "guided_json_envelope"
        else:
            outcome = _run_structured(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
            route = "guided_json_schema"
    else:
        outcome = _run_text(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
        route = "text_passthrough"

    result = dict(case)
    result.update(outcome)
    result.update({
        "pipeline_variant": "AWQ+HybridPipeline",
        "base_model": base_model,
        "arithmetic_model": arithmetic_model,
        "ab_stage": stage,
        "source_dataset": "internal",
        "route": route,
        "glossary_version": glossary_meta["version"],
        "glossary_sha256": glossary_meta["sha256"],
        "matched_terms": glossary_meta["matched_terms"],
        "glossary_hit": glossary_meta["hit"],
        "injected_content_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "latency_s": round(time.perf_counter() - started, 4),
    })
    return result


def _run_external_selective(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    stage: str,
) -> dict[str, Any]:
    """Route all external questions; use the arithmetic adapter only for math."""

    route_schema = {
        "type": "object",
        "properties": {"task_type": {"type": "string", "enum": ["CALCULATION", "TEXT"]}},
        "required": ["task_type"],
        "additionalProperties": False,
    }
    route_prompt = (
        f"{prompt}\n\nClassify the task before answering. Use CALCULATION only when "
        "the question explicitly asks to compute a numeric amount, ratio, "
        "percentage, rate, or formula result. Use TEXT for selection, "
        "comparison, explanation, yes/no, or list questions. Return only the "
        "JSON route object."
    )
    try:
        routed = call_model(
            url=url,
            model=base_model,
            messages=_text_messages(case, route_prompt),
            response_format=vllm_response_format(route_schema, name="hybrid_task_route"),
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
            "route": "financebench_route_error",
            "router": None,
            "attempts": [],
            "error": str(exc),
        }

    if task_type == "CALCULATION":
        outcome = _stage_numeric(
            stage=stage, case=case, prompt=prompt, url=url, model=arithmetic_model, timeout=timeout
        )
        outcome.update({"route": "financebench_calculation", "router": routed})
        return outcome

    outcome = _run_text(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
    outcome.update({"route": "financebench_text", "router": routed})
    return outcome


def _run_external_case(
    *,
    case: dict[str, Any],
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
    stage: str,
) -> dict[str, Any]:
    """Keep FinQA/TAT-QA untouched; scope RAG/EXPR routing to FinanceBench."""

    context = case["context"]
    glossary_meta = {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    if case.get("source") == "FinanceBench":
        context, glossary_meta = _glossary_prompt(
            context,
            glossary,
            query=case["question"],
            version=(
                "bok-800-arithmetic-glossary-v2-selective"
                if stage == "selective_unit_scale"
                else "bok-800-arithmetic-glossary-v1"
            ),
        )
    prompt = _base_prompt(context, case["question"], "Answer concisely from the supplied evidence.")
    started = time.perf_counter()

    if case.get("source") == "FinanceBench":
        outcome = _run_external_selective(
            case=case,
            prompt=prompt,
            url=url,
            base_model=base_model,
            arithmetic_model=arithmetic_model,
            timeout=timeout,
            stage=stage,
        )
        route = outcome.get("route") or "financebench_error"
    else:
        outcome = _run_external_selective(
            case=case,
            prompt=prompt,
            url=url,
            base_model=base_model,
            arithmetic_model=arithmetic_model,
            timeout=timeout,
            stage=stage,
        )
        route = outcome.get("route") or "external_route_error"

    result = dict(case)
    result.update(outcome)
    result.update({
        "pipeline_variant": "AWQ+HybridPipeline",
        "base_model": base_model,
        "arithmetic_model": arithmetic_model,
        "ab_stage": stage,
        "source_dataset": "external",
        "route": route,
        "glossary_version": glossary_meta["version"],
        "glossary_sha256": glossary_meta["sha256"],
        "matched_terms": glossary_meta["matched_terms"],
        "glossary_hit": glossary_meta["hit"],
        "injected_content_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "latency_s": round(time.perf_counter() - started, 4),
    })
    return result


def run_internal(args: argparse.Namespace, glossary: tuple[str, list[Any]] | None) -> dict[str, Any]:
    dataset = json.loads(args.internal.read_text(encoding="utf-8"))
    rows = []
    for index, case in enumerate(dataset["cases"], start=1):
        print(f"internal {index}/{len(dataset['cases'])}: {case['id']}", file=sys.stderr)
        rows.append(
            _run_internal_case(
                case=case,
                url=args.url,
                base_model=args.base_model,
                arithmetic_model=args.arithmetic_model,
                timeout=args.timeout,
                glossary=glossary,
                stage=args.stage,
            )
        )
    return {
        "benchmark": dataset["benchmark"],
        "dataset_sha256": sha256(args.internal),
        "model": args.base_model,
        "base_model": args.base_model,
        "arithmetic_model": args.arithmetic_model,
        "ab_stage": args.stage,
        "pipeline_variant": "AWQ+HybridPipeline",
        "policy": "selective_expr_ast_accounting_glossary_guided_json_envelope_awq_passthrough_no_fallback",
        "results": rows,
    }


def run_external(args: argparse.Namespace, glossary: tuple[str, list[Any]] | None) -> dict[str, Any]:
    dataset = json.loads(args.external.read_text(encoding="utf-8"))
    rows = []
    for index, case in enumerate(dataset["cases"], start=1):
        print(f"external {index}/{len(dataset['cases'])}: {case['id']}", file=sys.stderr)
        row = _run_external_case(
            case=case,
            url=args.url,
            base_model=args.base_model,
            arithmetic_model=args.arithmetic_model,
            timeout=args.timeout,
            glossary=glossary,
            stage=args.stage,
        )
        rows.append({
            "id": case["id"],
            "source": case["source"],
            "question": case["question"],
            "gold": case["gold_answer"],
            **{k: row.get(k) for k in (
                "prediction", "raw_prediction", "final_source", "expression", "calculator_value", "numeric_metadata",
                "route", "router", "finish_reason", "latency_s", "prompt_tokens", "completion_tokens",
                "error", "glossary_version", "glossary_sha256", "matched_terms", "glossary_hit",
                "injected_content_sha256", "attempts",
            )},
        })
    return {
        "benchmark": dataset.get("benchmark", "HgFinance-External50-v1"),
        "seed": dataset.get("seed"),
        "dataset_sha256": sha256(args.external),
        "model": args.base_model,
        "base_model": args.base_model,
        "arithmetic_model": args.arithmetic_model,
        "ab_stage": args.stage,
        "pipeline_variant": "AWQ+HybridPipeline",
        "policy": "FinanceBench_only_expr_ast_and_glossary; FinQA_TATQA_AWQ_text_passthrough; no_fallback",
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", dest="base_model_alias", help="compatibility alias for --base-model")
    parser.add_argument("--base-model")
    parser.add_argument("--arithmetic-model", required=True)
    parser.add_argument(
        "--stage",
        choices=("expr_ast", "unit_normalization", "domain_formula", "fifo_fewshot", "structured_envelope", "selective_unit_scale"),
        default="expr_ast",
    )
    parser.add_argument("--url", default=ENDPOINT)
    parser.add_argument("--internal", type=Path, default=INTERNAL_DATA)
    parser.add_argument("--external", type=Path, default=EXTERNAL_DATA)
    parser.add_argument("--glossary", type=Path, default=GLOSSARY_DATA)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    parser.add_argument("--only", choices=("all", "internal", "external"), default="all")
    args = parser.parse_args()
    args.base_model = args.base_model or args.base_model_alias
    if not args.base_model:
        parser.error("--base-model is required")

    glossary = (
        load_selective_v2_glossary(args.glossary)
        if args.stage == "selective_unit_scale"
        else load_glossary(args.glossary)
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    internal = run_internal(args, glossary) if args.only in {"all", "internal"} else None
    external = run_external(args, glossary) if args.only in {"all", "external"} else None
    if internal is not None:
        (args.output_dir / "internal50_raw.json").write_text(json.dumps(internal, ensure_ascii=False, indent=2), encoding="utf-8")
    if external is not None:
        (args.output_dir / "external50_raw.json").write_text(json.dumps(external, ensure_ascii=False, indent=2), encoding="utf-8")

    provenance = {
        "schema_version": "aws-hybrid-provenance.v2",
        "variant": "AWQ+HybridPipeline",
        "model": args.base_model,
        "base_model": args.base_model,
        "arithmetic_model": args.arithmetic_model,
        "ab_stage": args.stage,
        "endpoint": args.url,
        "datasets": {
            "internal50_v2_sha256": internal["dataset_sha256"] if internal else None,
            "external50_v1_sha256": external["dataset_sha256"] if external else None,
        },
        "glossary": {
            "path": str(args.glossary),
            "sha256": glossary[0],
            "scope": "internal accounting and FinanceBench only",
            "query_scoped": True,
            "version": "bok-800-arithmetic-glossary-v2-selective" if args.stage == "selective_unit_scale" else "bok-800-arithmetic-glossary-v1",
        },
        "arithmetic": {
            "llm_output": (
                "guided JSON {expression, answer_type, result_unit, scale, formula_name, explanation}"
                if args.stage == "selective_unit_scale"
                else "EXPR:<pure arithmetic expression>"
            ),
            "execution": "safe AST; numeric literals and + - * / ** only",
            "answer_rules": "none",
            "deterministic_answer_fallback": False,
        },
        "structured_output": {
            "control_envelope": ["status", "result", "expression", "missing_params", "reason"],
            "guided_json": True,
            "schema_validation": True,
            "semantic_audit": True,
            "benchmark_result_unwrapped_to_frozen_schema": True,
            "deterministic_answer_fallback": False,
            "unsupported_schema_behavior": "HOLD/error",
        },
        "external_routing": {
            "FinanceBench": "query-scoped glossary plus LLM calculation/text routing",
            "FinQA": "AWQ text passthrough",
            "TAT-QA": "AWQ text passthrough",
        },
    }
    (args.output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
