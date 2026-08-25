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
import re
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
from benchmarks.quantization.bok800_wiki_rag import Bok800WikiIndex, load_bok800_wiki
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
from benchmarks.quantization.run_external50_v1 import build_messages as _official_external_messages
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


def _wiki_fallback_prompt(
    prompt: str,
    wiki: Bok800WikiIndex | None,
    *,
    query: str,
    glossary_hit: bool,
    url: str,
    model: str,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    if wiki is None or glossary_hit:
        return prompt, {
            "hit": False,
            "fallback_skipped": bool(glossary_hit),
            "sha256": wiki.digest if wiki is not None else None,
            "latency_ms": 0.0,
            "pages": [],
            "terms": [],
            "context_chars": 0,
            "planner": None,
            "candidate_hit": False,
            "candidates": [],
            "grade": None,
        }
    planner_schema = {
        "type": "object",
        "properties": {
            "applicable": {"type": "boolean"},
            "search_query": {"type": "string"},
        },
        "required": ["applicable", "search_query"],
        "additionalProperties": False,
    }
    planner_messages = [
        {
            "role": "system",
            "content": (
                "You route searches to the Bank of Korea Economic and Financial Terms 800 wiki. "
                "For a general economics, finance, accounting, ratio, rate, currency, banking, "
                "investment, or market concept, translate only its core terminology into a short "
                "Korean search query. Set applicable false for application-specific controls, stale "
                "data, or simple document value lookup with no concept to define. Do not answer the "
                "question. Return only the requested JSON."
            ),
        },
        {"role": "user", "content": query},
    ]
    try:
        planned = call_model(
            url=url,
            model=model,
            messages=planner_messages,
            response_format=vllm_response_format(planner_schema, name="bok800_wiki_search_route"),
            timeout=timeout,
        )
        validation = validate_json(planned["content"], planner_schema)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid BOK800 Wiki search route")
        plan = validation.value
        planned_query = str(plan["search_query"]).strip()
        applicable = bool(plan["applicable"])
        exact_term = bool(planned_query) and wiki.has_exact_term(planned_query)
        planner_meta = {
            "applicable": applicable,
            "search_query": planned_query,
            "exact_term": exact_term,
            "latency_s": planned.get("latency_s"),
            "prompt_tokens": planned.get("prompt_tokens"),
            "completion_tokens": planned.get("completion_tokens"),
            "error": None,
        }
    except RuntimeError as exc:
        return prompt, {
            "hit": False,
            "fallback_skipped": False,
            "sha256": wiki.digest,
            "latency_ms": 0.0,
            "pages": [],
            "terms": [],
            "context_chars": 0,
            "planner": {"error": str(exc)},
            "candidate_hit": False,
            "candidates": [],
            "grade": None,
        }
    # ``applicable`` is advisory only.  The small AWQ router can be overly
    # conservative for translated terminology, so a non-empty query proceeds
    # to deterministic retrieval and the separate relevance grader makes the
    # fail-closed injection decision.
    if not planned_query:
        return prompt, {
            "hit": False,
            "fallback_skipped": False,
            "sha256": wiki.digest,
            "latency_ms": 0.0,
            "pages": [],
            "terms": [],
            "context_chars": 0,
            "planner": planner_meta,
            "candidate_hit": False,
            "candidates": [],
            "grade": None,
        }
    injected, metadata = wiki.inject(prompt, query=planned_query, top_k=1, max_pages=3)
    metadata["fallback_skipped"] = False
    metadata["planner"] = planner_meta
    metadata["candidate_hit"] = metadata["hit"]
    metadata["candidates"] = list(metadata["pages"])
    metadata["grade"] = None
    if not metadata["hit"]:
        return prompt, metadata

    grade_schema = {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["relevant", "reason"],
        "additionalProperties": False,
    }
    candidate_context = "\n".join(
        f"{page['term']}: {wiki.pages[page['page_id']].definition[:600]}"
        for page in metadata["pages"]
    )
    grade_messages = [
        {
            "role": "system",
            "content": (
                "Judge retrieval relevance only. Return relevant=true only if the supplied BOK "
                "glossary definition directly clarifies a concept, formula, or accounting treatment "
                "needed to answer the question. Return false for a merely shared generic word, a "
                "company-specific fact lookup, an application control, or a definition that cannot "
                "help derive the requested answer. Do not answer the question."
            ),
        },
        {"role": "user", "content": f"QUESTION:\n{query}\n\nRETRIEVED BOK WIKI:\n{candidate_context}"},
    ]
    try:
        graded = call_model(
            url=url,
            model=model,
            messages=grade_messages,
            response_format=vllm_response_format(grade_schema, name="bok800_wiki_relevance"),
            timeout=timeout,
        )
        validation = validate_json(graded["content"], grade_schema)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid BOK800 Wiki relevance grade")
        metadata["grade"] = {
            **validation.value,
            "latency_s": graded.get("latency_s"),
            "prompt_tokens": graded.get("prompt_tokens"),
            "completion_tokens": graded.get("completion_tokens"),
            "error": None,
        }
    except RuntimeError as exc:
        metadata["grade"] = {"relevant": False, "reason": "grade_failed", "error": str(exc)}
    if not metadata["grade"]["relevant"]:
        metadata.update({"hit": False, "pages": [], "terms": [], "context_chars": 0})
        return prompt, metadata
    return injected, metadata


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


def _run_structured_consensus(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
    reasoned: bool = False,
    fewshot: bool = False,
) -> dict[str, Any]:
    """Generate and adjudicate structured JSON without an answer fallback.

    Two independent model passes are used so that the final adjudicator can
    resolve a semantic disagreement from the supplied context.  The
    adjudicator receives no benchmark ID, gold answer, or case-specific rule.
    If the adjudication request fails, a schema-valid model candidate may be
    retained diagnostically; no deterministic value is substituted.
    """

    result_schema = infer_schema_from_contract(case["context"])
    if result_schema is None:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "structured_consensus_schema_missing",
            "schema": None,
            "candidates": [],
            "adjudication": None,
            "error": "no explicit JSON schema contract was supplied",
        }

    candidate_schema = result_schema
    if reasoned:
        candidate_schema = {
            "type": "object",
            "properties": {
                "analysis": {
                    "type": "string",
                    "description": (
                        "Concise evidence and calculation plan grounded only in context; "
                        "do not mention hidden answers or benchmark IDs."
                    ),
                },
                "answer": result_schema,
            },
            "required": ["analysis", "answer"],
            "additionalProperties": False,
        }
    candidate_response_format = vllm_response_format(
        candidate_schema,
        name="hybrid_structured_reasoned_candidate" if reasoned else "hybrid_structured_consensus",
    )
    response_format = vllm_response_format(result_schema, name="hybrid_structured_consensus_final")
    base_messages = _structured_messages(case, prompt)
    fewshot_guidance = ""
    if fewshot:
        fewshot_guidance = (
            " General structured-task examples, not answers to this request: "
            "(1) if authoritative evidence says a claim's number differs, use "
            "supported=false with a concise canonical reason such as "
            "numeric_mismatch; (2) if a rule limits exposure below the requested "
            "amount, report the constrained action as RESIZE and calculate the "
            "remaining allowance; (3) if a timestamp exceeds a freshness limit, "
            "use REJECT and a short reason grounded in the stale snapshot concept. "
            "For arithmetic, multiply quantities by unit prices, subtract costs "
            "and fees in the stated order, and verify the result before emitting. "
            "Do not copy these examples as answers and do not use any hidden gold "
            "value."
        )
    candidate_messages = [
        [
            *base_messages,
            {
                "role": "system",
                "content": (
                    " Solve the task independently from the supplied context. "
                    "Check each field against the context and preserve exact "
                    "enum spelling. "
                    + (
                        "First write a concise evidence/calculation plan in the analysis "
                        "field, then put the final object in answer. For numeric fields, "
                        "show the complete equation and intermediate values. For a "
                        "short snake_case reason, reuse the shortest canonical terms "
                        "from the context instead of paraphrasing them. For a policy "
                        "action, distinguish an unrestricted approval from a limit that "
                        "requires resizing. "
                        if reasoned else "Return only the requested JSON object."
                    )
                    + fewshot_guidance
                    + ""
                ),
            },
        ],
        [
            *base_messages,
            {
                "role": "system",
                "content": (
                    " Independently recompute the answer before emitting JSON. "
                    "Pay special attention to condition names, signs, dates, "
                    "and the distinction between similar labels. Use no outside "
                    "facts. "
                    + (
                        "Record a concise evidence/calculation plan in analysis and "
                        "the final object in answer. Recompute every numeric field "
                        "step by step; ensure the answer agrees with the plan. Keep "
                        "short reason values grounded in the context's terminology "
                        "and mark a constrained action as a resize rather than an "
                        "unrestricted approval."
                        if reasoned else "Return only the requested JSON object."
                    )
                    + fewshot_guidance
                ),
            },
        ],
    ]

    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for index, messages in enumerate(candidate_messages, start=1):
        try:
            response = call_model(
                url=url,
                model=model,
                messages=messages,
                response_format=candidate_response_format,
                timeout=timeout,
            )
        except RuntimeError as exc:
            attempts.append({"phase": f"candidate_{index}", "error": str(exc)})
            continue

        validation = validate_json(response["content"], candidate_schema)
        attempt = {
            **response,
            "phase": f"candidate_{index}",
            "valid": validation.valid,
            "validation_error": validation.error,
        }
        attempts.append(attempt)
        if validation.valid:
            candidate_value = validation.value["answer"] if reasoned else validation.value
            candidates.append({
                "index": index,
                "content": response["content"],
                "value": candidate_value,
                "analysis": validation.value.get("analysis") if reasoned else None,
            })

    if not candidates:
        return {
            "prediction": "",
            "raw_prediction": attempts[-1].get("content", "") if attempts else "",
            "final_source": "structured_consensus_validation_error",
            "schema": result_schema,
            "candidates": [],
            "adjudication": None,
            "attempts": attempts,
            "error": "no schema-valid model candidate",
        }

    candidate_block = "\n\n".join(
        f"CANDIDATE {candidate['index']}:\n{candidate['content']}"
        for candidate in candidates
    )
    adjudication_messages = [
        {
            "role": "system",
            "content": (
                (case.get("system_prompt") or "You are a general financial QA model.")
                + " You are a semantic adjudicator. Resolve disagreements between "
                "model candidates using only the supplied context. Correct a field "
                "only when the context supports the correction. Do not use a gold "
                "answer, benchmark ID, hidden rule, or outside knowledge. Return "
                "only one JSON object satisfying the supplied schema. First audit "
                "numeric fields by recomputing the full equation from the context; "
                "never copy an unverified candidate number. For policy actions, "
                "distinguish unrestricted approval from a limit-induced resize. For "
                "short snake_case reason fields, use the most direct canonical "
                "terms present in the context and do not replace them with a long "
                "paraphrase."
                + fewshot_guidance
            ),
        },
        {
            "role": "user",
            "content": (
                f"{prompt}\n\n{candidate_block}\n\n"
                "Select or correct the candidate using the context, then return the "
                "final JSON object only."
            ),
        },
    ]

    try:
        adjudicated = call_model(
            url=url,
            model=model,
            messages=adjudication_messages,
            response_format=response_format,
            timeout=timeout,
        )
        adjudication_validation = validate_json(adjudicated["content"], result_schema)
        attempts.append({
            **adjudicated,
            "phase": "adjudication",
            "valid": adjudication_validation.valid,
            "validation_error": adjudication_validation.error,
        })
        if adjudication_validation.valid:
            return {
                "prediction": adjudicated["content"],
                "raw_prediction": candidates[0]["content"],
                "final_raw_prediction": adjudicated["content"],
                "final_source": "guided_json_consensus_adjudication",
                "schema": result_schema,
                "candidates": candidates,
                "adjudication": adjudicated["content"],
                "attempts": attempts,
                "error": None,
            }
    except RuntimeError as exc:
        attempts.append({"phase": "adjudication", "error": str(exc)})

    # This is a model-output retention path only. It never calculates or
    # replaces an answer and remains distinguishable in provenance.
    retained = candidates[0]
    return {
        "prediction": retained["content"],
        "raw_prediction": retained["content"],
        "final_source": "guided_json_consensus_candidate_retained",
        "schema": result_schema,
        "candidates": candidates,
        "adjudication": None,
        "attempts": attempts,
        "error": None,
    }


_STAGE_SUFFIXES = {
    "unit_normalization": (
        " Before writing EXPR, normalize every unit before doing arithmetic. "
        "Convert percent notation p% to p/100 (for example 0.015% becomes "
        "0.015/100, never 0.015). Expand billion, million, and thousand into "
        "their numeric scale, and make numerator and denominator use the same "
        "unit. Keep the requested result unit explicit in the expression. "
        "The percent sign itself is forbidden in EXPR. When the requested "
        "result is percentage points, multiply the final ratio by 100 after "
        "converting percentage inputs to fractions; use a pattern such as "
        "((p/100)*(r/100) + (q/100)*(s/100)) * 100 with the supplied numeric "
        "literals substituted."
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
            "enum": [
                "1",
                "fraction",
                "percent",
                "percent_points",
                "currency",
                "shares",
                "count",
                "none",
                "1e3",
                "1e6",
                "1e9",
            ],
            "description": "Canonical source/result scale; use 1e3, 1e6, or 1e9 rather than words.",
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

    if stage in {"selective_unit_scale", "structured_fewshot_consensus"}:
        return _run_numeric_with_metadata(
            case=case, prompt=prompt, url=url, model=model, timeout=timeout
        )

    suffix = ""
    if stage in {"unit_normalization", "domain_formula", "fifo_fewshot", "finance_typed_routing", "finance_direct_answer", "finance_scoped_split", "finance_scoped_split_strict", "finance_scoped_split_strict_direct", "finance_scoped_context_rag", "finance_scoped_evidence_plan", "structured_envelope", "structured_consensus", "structured_reasoned_consensus", "structured_grounded_consensus", "structured_adapter_consensus", "structured_fewshot_consensus"}:
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


def _run_choice(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Constrain a generic label task to the labels supplied by the contract.

    The allowed labels come from the request contract, never from the frozen
    answer.  The model still selects the label; there is no deterministic
    answer substitution when a request or validation fails.
    """

    labels = [str(label) for label in case.get("allowed_labels", [])]
    if not labels:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "guided_choice_schema_missing",
            "route": "guided_choice",
            "attempts": [],
            "error": "no allowed labels supplied by the request contract",
        }
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": labels}},
        "required": ["label"],
        "additionalProperties": False,
    }
    response_format = vllm_response_format(schema, name="hybrid_choice")
    messages = [
        *_text_messages(case, prompt),
        {
            "role": "user",
            "content": (
                "Return exactly one JSON object with the key `label`. Select the "
                "label that follows from the supplied context. Do not explain. "
                f"The allowed labels are: {', '.join(labels)}."
            ),
        },
    ]
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
                "final_source": "guided_choice_request_error",
                "route": "guided_choice",
                "attempts": attempts,
                "error": str(exc),
            }
        validation = validate_json(response["content"], schema)
        attempts.append({
            **response,
            "phase": phase,
            "valid": validation.valid,
            "validation_error": validation.error,
        })
        if validation.valid:
            audit_messages = [
                *messages,
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": (
                        "Audit the selected label against the supplied context. "
                        "Keep it only if the context supports it; otherwise select "
                        "the label that follows from the explicit rule and facts. "
                        "Do not use outside knowledge, hidden answers, or benchmark "
                        "identifiers. Return only the same JSON object."
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
                        "prediction": audit_validation.value["label"],
                        "raw_prediction": response["content"],
                        "final_raw_prediction": audited["content"],
                        "final_source": "guided_choice_semantic_audit",
                        "route": "guided_choice",
                        "attempts": attempts,
                        "schema": schema,
                        "error": None,
                    }
            except RuntimeError as exc:
                attempts.append({"phase": "semantic_audit", "error": str(exc)})
            return {
                "prediction": validation.value["label"],
                "raw_prediction": response["content"],
                "final_source": "guided_choice_enum",
                "route": "guided_choice",
                "attempts": attempts,
                "schema": schema,
                "error": None,
            }
        messages = [
            *messages,
            {"role": "assistant", "content": response["content"]},
            {"role": "user", "content": retry_instruction(schema, validation.error or "invalid choice")},
        ]
    return {
        "prediction": "",
        "raw_prediction": attempts[-1].get("content", "") if attempts else "",
        "final_source": "guided_choice_validation_error",
        "route": "guided_choice",
        "attempts": attempts,
        "schema": schema,
        "error": attempts[-1].get("validation_error", "choice validation failed") if attempts else "no response",
    }


def _run_internal_case(
    *,
    case: dict[str, Any],
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
    wiki: Bok800WikiIndex | None,
    stage: str,
) -> dict[str, Any]:
    internal_stage = "selective_unit_scale" if stage == "finqa_numeric_routing" else stage
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
    wiki_meta: dict[str, Any] = {
        "hit": False, "fallback_skipped": False, "sha256": None, "latency_ms": 0.0,
        "pages": [], "terms": [], "context_chars": 0, "planner": None,
        "candidate_hit": False, "candidates": [], "grade": None,
    }
    if case.get("category") == "accounting_reasoning":
        context, glossary_meta = _glossary_prompt(
            context,
            glossary,
            query=question,
            version=(
                "bok-800-arithmetic-glossary-v2-selective"
                if internal_stage in {"selective_unit_scale", "finance_typed_routing"}
                else "bok-800-arithmetic-glossary-v1"
            ),
        )
        context, wiki_meta = _wiki_fallback_prompt(
            context, wiki, query=question, glossary_hit=glossary_meta["hit"],
            url=url, model=base_model, timeout=timeout,
        )
    prompt = _base_prompt(context, question, contract)
    started = time.perf_counter()

    if scoring_type == "numeric":
        outcome = _stage_numeric(
            stage=internal_stage, case=case, prompt=prompt, url=url, model=arithmetic_model, timeout=timeout
        )
        route = "expr_ast"
    elif scoring_type == "json_exact":
        if internal_stage == "structured_envelope":
            outcome = _run_structured_envelope(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
            route = "guided_json_envelope"
        elif internal_stage == "structured_consensus":
            outcome = _run_structured_consensus(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
            route = "guided_json_consensus"
        elif internal_stage in {"structured_reasoned_consensus", "structured_grounded_consensus"}:
            outcome = _run_structured_consensus(
                case=case,
                prompt=prompt,
                url=url,
                model=base_model,
                timeout=timeout,
                reasoned=True,
            )
            route = "guided_json_reasoned_consensus"
        elif internal_stage == "structured_adapter_consensus":
            outcome = _run_structured_consensus(
                case=case,
                prompt=prompt,
                url=url,
                model=arithmetic_model,
                timeout=timeout,
                reasoned=True,
            )
            route = "guided_json_arithmetic_adapter_consensus"
        elif internal_stage == "structured_fewshot_consensus":
            outcome = _run_structured_consensus(
                case=case,
                prompt=prompt,
                url=url,
                model=base_model,
                timeout=timeout,
                reasoned=True,
                fewshot=True,
            )
            route = "guided_json_fewshot_consensus"
        else:
            outcome = _run_structured(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
            route = "guided_json_schema"
    elif scoring_type == "choice":
        outcome = _run_choice(case=case, prompt=prompt, url=url, model=base_model, timeout=timeout)
        route = "guided_choice"
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
        "wiki_hit": wiki_meta["hit"],
        "wiki_fallback_skipped": wiki_meta["fallback_skipped"],
        "wiki_sha256": wiki_meta["sha256"],
        "wiki_retrieval_latency_ms": wiki_meta["latency_ms"],
        "wiki_pages": wiki_meta["pages"],
        "wiki_terms": wiki_meta["terms"],
        "wiki_context_chars": wiki_meta["context_chars"],
        "wiki_planner": wiki_meta["planner"],
        "wiki_candidate_hit": wiki_meta["candidate_hit"],
        "wiki_candidates": wiki_meta["candidates"],
        "wiki_grade": wiki_meta["grade"],
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

    text_prompt = prompt
    if stage == "finance_direct_answer":
        text_prompt = (
            f"{prompt}\n\nAnswer contract: start with exactly one direct answer to the "
            "question. Then add exactly one short Evidence sentence quoting or "
            "paraphrasing only the supplied context. For yes/no questions begin "
            "with Yes or No; for list questions include the complete requested "
            "list; for applicability questions state relevant or not relevant "
            "directly. Do not add a contradictory alternative or a long analysis."
        )
    outcome = _run_text(case=case, prompt=text_prompt, url=url, model=base_model, timeout=timeout)
    outcome.update({"route": "financebench_text", "router": routed})
    return outcome


def _run_finqa_numeric_routing(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
) -> dict[str, Any]:
    """Use the arithmetic adapter only for explicit FinQA calculations."""

    route_schema = {
        "type": "object",
        "properties": {"task_type": {"type": "string", "enum": ["CALCULATION", "TEXT"]}},
        "required": ["task_type"],
        "additionalProperties": False,
    }
    route_prompt = (
        f"{prompt}\n\nClassify this FinQA question before answering. Return CALCULATION only "
        "when the question explicitly asks to compute one numeric amount, ratio, "
        "percentage, rate, difference, or formula result from the supplied table "
        "and context. Return TEXT for table selection, evidence, comparison, "
        "explanation, or any question that does not ask for one numeric result. "
        "When uncertain, return TEXT. Return only the JSON route object."
    )
    try:
        routed = call_model(
            url=url,
            model=base_model,
            messages=_text_messages(case, route_prompt),
            response_format=vllm_response_format(route_schema, name="finqa_numeric_route"),
            timeout=timeout,
        )
        validation = validate_json(routed["content"], route_schema)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid FinQA numeric route")
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "finqa_numeric_route_error",
            "route": "finqa_numeric_route_error",
            "router": None,
            "attempts": [],
            "error": str(exc),
        }

    if validation.value["task_type"] == "CALCULATION":
        outcome = _stage_numeric(
            stage="structured_fewshot_consensus",
            case=case,
            prompt=prompt,
            url=url,
            model=arithmetic_model,
            timeout=timeout,
        )
        outcome.update({"route": "finqa_numeric_adapter", "router": routed})
        return outcome

    outcome = _run_official_external_text(
        case=case,
        url=url,
        model=base_model,
        timeout=timeout,
    )
    outcome.update({"route": "finqa_text_passthrough", "router": routed})
    return outcome


def _run_external_typed_finance(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
) -> dict[str, Any]:
    """Route FinanceBench by question type without using gold answers."""

    route_schema = {
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "enum": [
                    "NUMERIC_SCALAR",
                    "BOOLEAN_COMPARISON",
                    "LIST_OR_SET",
                    "EVIDENCE_SELECTION",
                    "DEFINITION_RELEVANCE",
                    "TEXT",
                ],
            }
        },
        "required": ["task_type"],
        "additionalProperties": False,
    }
    route_prompt = (
        f"{prompt}\n\nClassify the question before answering. Use exactly one type: "
        "NUMERIC_SCALAR only for one explicit numeric amount, ratio, rate, "
        "percentage, or formula result; BOOLEAN_COMPARISON for yes/no or "
        "increase/decrease decisions; LIST_OR_SET for multiple named items or "
        "an empty set; EVIDENCE_SELECTION for selecting a segment, period, "
        "company, or supported fact; DEFINITION_RELEVANCE for whether a metric "
        "applies or what it means; TEXT for other textual questions. Return only "
        "the JSON route object."
    )
    try:
        routed = call_model(
            url=url,
            model=base_model,
            messages=_text_messages(case, route_prompt),
            response_format=vllm_response_format(route_schema, name="financebench_typed_route"),
            timeout=timeout,
        )
        validation = validate_json(routed["content"], route_schema)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid typed FinanceBench route")
        task_type = validation.value["task_type"]
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "typed_route_error",
            "route": "financebench_typed_route_error",
            "router": None,
            "attempts": [],
            "error": str(exc),
        }

    if task_type == "NUMERIC_SCALAR":
        outcome = _stage_numeric(
            stage="unit_normalization",
            case=case,
            prompt=prompt,
            url=url,
            model=arithmetic_model,
            timeout=timeout,
        )
        outcome.update({"route": "financebench_typed_numeric", "router": routed})
        return outcome

    answer_contract = {
        "BOOLEAN_COMPARISON": "Put the direct Yes/No or comparison answer first, followed by minimal evidence.",
        "LIST_OR_SET": "Return the complete requested list or explicitly state that the set is empty.",
        "EVIDENCE_SELECTION": "Name the selected segment, period, company, or fact first, followed by one evidence sentence.",
        "DEFINITION_RELEVANCE": "State whether the metric is applicable and why, using supplied evidence.",
        "TEXT": "Answer directly and concisely from the supplied evidence.",
    }.get(task_type, "Answer directly and concisely from the supplied evidence.")
    typed_prompt = (
        f"{prompt}\n\nANSWER FORMAT FOR {task_type}: {answer_contract} "
        "Do not omit requested values, periods, names, or evidence."
    )
    outcome = _run_text(case=case, prompt=typed_prompt, url=url, model=base_model, timeout=timeout)
    outcome.update({"route": f"financebench_typed_{task_type.casefold()}", "router": routed})
    return outcome


_FINANCE_EVIDENCE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "question_type": {
            "type": "string",
            "enum": ["comparison", "list", "boolean", "relevance", "evidence", "other"],
        },
        "relevant_periods": {"type": "array", "items": {"type": "string"}},
        "relevant_facts": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "answer_requirements": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "question_type",
        "relevant_periods",
        "relevant_facts",
        "missing_evidence",
        "answer_requirements",
    ],
    "additionalProperties": False,
}


def _run_finance_evidence_plan(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    base_model: str,
    timeout: float,
) -> dict[str, Any]:
    """Extract context-grounded evidence before a FinanceBench text answer."""

    plan_prompt = (
        f"{prompt}\n\nBefore answering, create only an evidence plan in the supplied "
        "JSON schema. Identify the exact periods, rows, names, and numerical facts "
        "needed by the question. Preserve fiscal-year labels exactly. Do not answer "
        "the question, use outside facts, or infer a missing value."
    )
    try:
        planned = call_model(
            url=url,
            model=base_model,
            messages=_text_messages(case, plan_prompt),
            response_format=vllm_response_format(_FINANCE_EVIDENCE_PLAN_SCHEMA, name="financebench_evidence_plan"),
            timeout=timeout,
        )
        validation = validate_json(planned["content"], _FINANCE_EVIDENCE_PLAN_SCHEMA)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid FinanceBench evidence plan")
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "finance_evidence_plan_error",
            "route": "financebench_evidence_plan_error",
            "evidence_plan": None,
            "attempts": [],
            "error": str(exc),
        }

    answer_prompt = (
        f"{prompt}\n\nMODEL-GENERATED EVIDENCE PLAN (not an answer):\n"
        f"{planned['content']}\n\n"
        "Use the evidence plan only as a checklist and verify every item against "
        "the supplied context. Answer the original question directly and concisely. "
        "Include all requested names, periods, list members, or yes/no conclusions. "
        "Do not mention the plan or add unsupported alternatives."
    )
    outcome = _run_text(case=case, prompt=answer_prompt, url=url, model=base_model, timeout=timeout)
    outcome.update({
        "route": "financebench_evidence_plan_text",
        "evidence_plan": validation.value,
        "planner": planned,
    })
    return outcome


_FINANCE_RETRIEVAL_STOPWORDS = {
    "what", "which", "who", "when", "where", "how", "why", "was", "were",
    "is", "are", "the", "a", "an", "of", "to", "for", "from", "in", "on",
    "and", "or", "by", "with", "this", "that", "these", "those", "does",
    "did", "do", "than", "most", "least", "highest", "lowest", "had",
    "have", "has", "give", "using", "based", "only", "among", "all",
}


def _retrieve_financebench_context(context: str, question: str) -> tuple[str, dict[str, Any]]:
    """Retrieve answer-free evidence lines from the FinanceBench source context.

    This is lexical document retrieval only: it never reads the gold answer,
    case ID, or a benchmark-specific answer map.  The original question is
    preserved; only source-document evidence is narrowed before the AWQ text
    model sees it.
    """

    lines = [line.strip() for line in context.splitlines() if line.strip()]
    if not lines:
        return context, {"retrieval_hit": False, "retrieved_line_count": 0, "retrieved_terms": []}

    query_terms = [
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9&'/-]{2,}", question)
        if token.casefold() not in _FINANCE_RETRIEVAL_STOPWORDS
    ]
    unique_terms = list(dict.fromkeys(query_terms))
    scored: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        lowered = line.casefold()
        score = sum(2 if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered) else 0 for term in unique_terms)
        if score:
            scored.append((score, index))

    if not scored:
        return context, {"retrieval_hit": False, "retrieved_line_count": len(lines), "retrieved_terms": []}

    selected: set[int] = set(range(min(12, len(lines))))
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))[:32]:
        selected.update(range(max(0, index - 1), min(len(lines), index + 2)))

    ordered = [lines[index] for index in sorted(selected)]
    retrieved = "\n".join(ordered)
    return retrieved, {
        "retrieval_hit": True,
        "retrieved_line_count": len(ordered),
        "retrieved_terms": unique_terms,
        "retrieved_content_sha256": hashlib.sha256(retrieved.encode("utf-8")).hexdigest(),
    }


def _run_external_scoped_split(
    *,
    case: dict[str, Any],
    prompt: str,
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
    strict: bool = False,
    direct: bool = False,
    retrieve_context: bool = False,
    evidence_plan: bool = False,
    numeric_stage: str = "unit_normalization",
) -> dict[str, Any]:
    """Separate numeric adapter use from glossary-RAG text use."""

    route_schema = {
        "type": "object",
        "properties": {"task_type": {"type": "string", "enum": ["CALCULATION", "TEXT"]}},
        "required": ["task_type"],
        "additionalProperties": False,
    }
    if strict:
        route_prompt = (
            f"{prompt}\n\nYou are a conservative routing classifier, not the answerer. "
            "Return CALCULATION only if the question explicitly asks the model to "
            "compute or derive one numeric result from supplied values, such as a "
            "percentage change, ratio, rate, difference, sum, or formula result. "
            "Return TEXT for every evidence-selection or retrieval task, including "
            "questions asking which company, segment, period, item, or metric had "
            "the highest/lowest/most/least value; questions asking who/what/which "
            "was selected; comparisons without an explicit requested calculation; "
            "yes/no, relevance, definition, list, and explanation questions. "
            "A numeric value appearing in the context does not make a task a "
            "calculation. When uncertain, return TEXT. Return only the JSON route "
            "object."
        )
    else:
        route_prompt = (
            f"{prompt}\n\nClassify the task before answering. Use CALCULATION only when "
            "the question explicitly asks for one numeric amount, ratio, percentage, "
            "rate, or formula result. Use TEXT for selection, comparison, explanation, "
            "yes/no, applicability, evidence, or list questions. Return only the JSON "
            "route object."
        )
    try:
        routed = call_model(
            url=url,
            model=base_model,
            messages=_text_messages(case, route_prompt),
            response_format=vllm_response_format(
                route_schema,
                name="financebench_scoped_strict_route" if strict else "financebench_scoped_route",
            ),
            timeout=timeout,
        )
        validation = validate_json(routed["content"], route_schema)
        if not validation.valid:
            raise RuntimeError(validation.error or "invalid scoped FinanceBench route")
        task_type = validation.value["task_type"]
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "scoped_route_error",
            "route": "financebench_scoped_strict_route_error" if strict else "financebench_scoped_route_error",
            "router": None,
            "glossary_applied_to": "none",
            "attempts": [],
            "error": str(exc),
        }

    if task_type == "CALCULATION":
        outcome = _stage_numeric(
            stage=numeric_stage,
            case=case,
            prompt=prompt,
            url=url,
            model=arithmetic_model,
            timeout=timeout,
        )
        outcome.update({
            "route": "financebench_scoped_strict_numeric_adapter" if strict else "financebench_scoped_numeric_adapter",
            "router": routed,
            "glossary_applied_to": "none",
        })
        return outcome

    text_prompt = prompt
    retrieval_meta = {"retrieval_hit": False, "retrieved_line_count": None, "retrieved_terms": []}
    if retrieve_context:
        retrieved_context, retrieval_meta = _retrieve_financebench_context(
            case["context"], case["question"]
        )
        text_prompt = _base_prompt(
            retrieved_context,
            case["question"],
            "Answer concisely from the retrieved FinanceBench evidence. Do not use outside facts.",
        )

    scoped_prompt, glossary_meta = _glossary_prompt(
        text_prompt,
        glossary,
        query=case["question"],
        version="bok-800-arithmetic-glossary-v1",
    )
    # A glossary miss must be a true no-op.  An empty retrieval header changes
    # the prompt distribution without adding evidence and can make a text
    # question look like a glossary task.
    if not glossary_meta["hit"]:
        scoped_prompt = prompt
    if strict and direct:
        scoped_prompt = (
            f"{scoped_prompt}\n\nAnswer-format guidance for evidence questions: start with the "
            "single selected company, segment, period, item, or fact requested by "
            "the question. Then give one short evidence sentence using only the "
            "supplied context. For highest/lowest/most/least questions, compare the "
            "relevant supplied values before selecting one. Never present two "
            "contradictory candidates and do not add outside facts."
        )
    if evidence_plan:
        outcome = _run_finance_evidence_plan(
            case=case,
            prompt=scoped_prompt,
            url=url,
            base_model=base_model,
            timeout=timeout,
        )
    else:
        outcome = _run_text(case=case, prompt=scoped_prompt, url=url, model=base_model, timeout=timeout)
    outcome.update({
        "route": (
            "financebench_scoped_evidence_plan_text"
            if evidence_plan
            else ("financebench_scoped_strict_text_rag" if strict else "financebench_scoped_text_rag")
        ),
        "router": routed,
        "glossary_applied_to": "text",
        "scoped_glossary_version": glossary_meta["version"],
        "scoped_glossary_sha256": glossary_meta["sha256"],
        "scoped_matched_terms": glossary_meta["matched_terms"],
        "scoped_glossary_hit": glossary_meta["hit"],
        **retrieval_meta,
    })
    return outcome


def _run_official_external_text(
    *,
    case: dict[str, Any],
    url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Use the frozen External-50 text contract for FinQA and TAT-QA."""

    try:
        response = call_model(
            url=url,
            model=model,
            messages=_official_external_messages(case),
            timeout=timeout,
        )
    except RuntimeError as exc:
        return {
            "prediction": "",
            "raw_prediction": "",
            "final_source": "official_external_request_error",
            "attempts": [],
            "error": str(exc),
        }
    return {
        "prediction": response["content"],
        "raw_prediction": response["content"],
        "final_source": "official_external_text_contract",
        "finish_reason": response.get("finish_reason"),
        "latency_s": response.get("latency_s"),
        "prompt_tokens": response.get("prompt_tokens"),
        "completion_tokens": response.get("completion_tokens"),
        "attempts": [response],
        "error": None,
    }


def _run_external_case(
    *,
    case: dict[str, Any],
    url: str,
    base_model: str,
    arithmetic_model: str,
    timeout: float,
    glossary: tuple[str, list[Any]] | None,
    wiki: Bok800WikiIndex | None,
    stage: str,
) -> dict[str, Any]:
    """Keep FinQA/TAT-QA untouched; scope RAG/EXPR routing to FinanceBench."""

    context = case["context"]
    glossary_meta = {"version": None, "sha256": None, "matched_terms": [], "hit": False}
    wiki_meta: dict[str, Any] = {
        "hit": False, "fallback_skipped": False, "sha256": None, "latency_ms": 0.0,
        "pages": [], "terms": [], "context_chars": 0, "planner": None,
        "candidate_hit": False, "candidates": [], "grade": None,
    }
    if case.get("source") == "FinanceBench" and stage not in {"finance_scoped_split", "finance_scoped_split_strict", "finance_scoped_split_strict_direct", "finance_scoped_context_rag", "finance_scoped_evidence_plan", "finance_selective_reasoning"}:
        context, glossary_meta = _glossary_prompt(
            context,
            glossary,
            query=case["question"],
            version=(
                "bok-800-arithmetic-glossary-v2-selective"
                if stage in {"selective_unit_scale", "finance_typed_routing"}
                else "bok-800-arithmetic-glossary-v1"
            ),
        )
        context, wiki_meta = _wiki_fallback_prompt(
            context, wiki, query=case["question"], glossary_hit=glossary_meta["hit"],
            url=url, model=base_model, timeout=timeout,
        )
    prompt = _base_prompt(context, case["question"], "Answer concisely from the supplied evidence.")
    started = time.perf_counter()

    if case.get("source") == "FinanceBench":
        if stage in {"finance_scoped_split", "finance_scoped_split_strict", "finance_scoped_split_strict_direct", "finance_scoped_context_rag", "finance_scoped_evidence_plan", "finance_selective_reasoning"}:
            outcome = _run_external_scoped_split(
                case=case,
                prompt=prompt,
                url=url,
                base_model=base_model,
                arithmetic_model=arithmetic_model,
                timeout=timeout,
                glossary=glossary,
                strict=stage in {"finance_scoped_split_strict", "finance_scoped_split_strict_direct", "finance_scoped_context_rag", "finance_scoped_evidence_plan", "finance_selective_reasoning"},
                direct=stage in {"finance_scoped_split_strict_direct", "finance_scoped_context_rag"},
                retrieve_context=stage in {"finance_scoped_context_rag", "finance_scoped_evidence_plan"},
                evidence_plan=stage in {"finance_scoped_evidence_plan", "finance_selective_reasoning"},
                numeric_stage="expr_ast" if stage == "finance_selective_reasoning" else "unit_normalization",
            )
            if outcome.get("glossary_applied_to") == "text":
                glossary_meta = {
                    "version": outcome.get("scoped_glossary_version"),
                    "sha256": outcome.get("scoped_glossary_sha256"),
                    "matched_terms": outcome.get("scoped_matched_terms", []),
                    "hit": outcome.get("scoped_glossary_hit", False),
                }
        elif stage == "finqa_numeric_routing":
            outcome = _run_external_selective(
                case=case,
                prompt=prompt,
                url=url,
                base_model=base_model,
                arithmetic_model=arithmetic_model,
                timeout=timeout,
                stage="structured_fewshot_consensus",
            )
        elif stage == "finance_typed_routing":
            outcome = _run_external_typed_finance(
                case=case,
                prompt=prompt,
                url=url,
                base_model=base_model,
                arithmetic_model=arithmetic_model,
                timeout=timeout,
            )
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
        route = outcome.get("route") or "financebench_error"
    elif case.get("source") == "FinQA" and stage == "finqa_numeric_routing":
        outcome = _run_finqa_numeric_routing(
            case=case,
            prompt=prompt,
            url=url,
            base_model=base_model,
            arithmetic_model=arithmetic_model,
            timeout=timeout,
        )
        route = outcome.get("route") or "finqa_numeric_error"
    else:
        # FinQA/TAT-QA are the external automatic gate. Preserve the frozen
        # runner's exact system/user prompt contract; FinanceBench-only
        # routing must not perturb these datasets.
        outcome = _run_official_external_text(
            case=case,
            url=url,
            model=base_model,
            timeout=timeout,
        )
        route = "official_external_text_contract"

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
        "wiki_hit": wiki_meta["hit"],
        "wiki_fallback_skipped": wiki_meta["fallback_skipped"],
        "wiki_sha256": wiki_meta["sha256"],
        "wiki_retrieval_latency_ms": wiki_meta["latency_ms"],
        "wiki_pages": wiki_meta["pages"],
        "wiki_terms": wiki_meta["terms"],
        "wiki_context_chars": wiki_meta["context_chars"],
        "wiki_planner": wiki_meta["planner"],
        "wiki_candidate_hit": wiki_meta["candidate_hit"],
        "wiki_candidates": wiki_meta["candidates"],
        "wiki_grade": wiki_meta["grade"],
        "injected_content_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "latency_s": round(time.perf_counter() - started, 4),
    })
    return result


def run_internal(
    args: argparse.Namespace,
    glossary: tuple[str, list[Any]] | None,
    wiki: Bok800WikiIndex | None,
) -> dict[str, Any]:
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
                wiki=wiki,
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
        "policy": (
            "selective_expr_ast_accounting_glossary_then_bok800_wiki_guided_json_awq_no_answer_fallback"
            if args.wiki_fallback
            else "selective_expr_ast_accounting_glossary_guided_json_envelope_awq_passthrough_no_fallback"
        ),
        "results": rows,
    }


def run_external(
    args: argparse.Namespace,
    glossary: tuple[str, list[Any]] | None,
    wiki: Bok800WikiIndex | None,
) -> dict[str, Any]:
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
            wiki=wiki,
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
                "wiki_hit", "wiki_fallback_skipped", "wiki_sha256", "wiki_retrieval_latency_ms",
                "wiki_pages", "wiki_terms", "wiki_context_chars",
                "wiki_planner",
                "wiki_candidate_hit", "wiki_candidates", "wiki_grade",
                "injected_content_sha256", "retrieval_hit", "retrieved_line_count",
                "retrieved_terms", "retrieved_content_sha256", "attempts",
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
        "policy": (
            "FinanceBench_only_expr_ast_glossary_then_bok800_wiki; FinQA_TATQA_AWQ_text_passthrough; no_answer_fallback"
            if args.wiki_fallback
            else "FinanceBench_only_expr_ast_and_glossary; FinQA_TATQA_AWQ_text_passthrough; no_fallback"
        ),
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", dest="base_model_alias", help="compatibility alias for --base-model")
    parser.add_argument("--base-model")
    parser.add_argument("--arithmetic-model", required=True)
    parser.add_argument(
        "--stage",
        choices=("expr_ast", "unit_normalization", "domain_formula", "fifo_fewshot", "finance_typed_routing", "finqa_numeric_routing", "finance_direct_answer", "finance_scoped_split", "finance_scoped_split_strict", "finance_scoped_split_strict_direct", "finance_scoped_context_rag", "finance_scoped_evidence_plan", "finance_selective_reasoning", "structured_envelope", "structured_consensus", "structured_reasoned_consensus", "structured_grounded_consensus", "structured_adapter_consensus", "structured_fewshot_consensus", "selective_unit_scale"),
        default="expr_ast",
    )
    parser.add_argument("--url", default=ENDPOINT)
    parser.add_argument("--internal", type=Path, default=INTERNAL_DATA)
    parser.add_argument("--external", type=Path, default=EXTERNAL_DATA)
    parser.add_argument("--glossary", type=Path, default=GLOSSARY_DATA)
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=Path("benchmarks/quantization/knowledge/bok800_2026"),
    )
    parser.add_argument("--wiki-fallback", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    parser.add_argument("--only", choices=("all", "internal", "external"), default="all")
    args = parser.parse_args()
    args.base_model = args.base_model or args.base_model_alias
    if not args.base_model:
        parser.error("--base-model is required")

    glossary = (
        load_selective_v2_glossary(args.glossary)
        if args.stage in {"selective_unit_scale", "finance_typed_routing"}
        else load_glossary(args.glossary)
    )
    wiki = load_bok800_wiki(args.wiki_root) if args.wiki_fallback else None
    args.output_dir.mkdir(parents=True, exist_ok=True)
    internal = run_internal(args, glossary, wiki) if args.only in {"all", "internal"} else None
    external = run_external(args, glossary, wiki) if args.only in {"all", "external"} else None
    if internal is not None:
        (args.output_dir / "internal50_raw.json").write_text(json.dumps(internal, ensure_ascii=False, indent=2), encoding="utf-8")
    if external is not None:
        (args.output_dir / "external50_raw.json").write_text(json.dumps(external, ensure_ascii=False, indent=2), encoding="utf-8")

    provenance = {
        "schema_version": "aws-hybrid-provenance.v2",
        "variant": "AWQ+HybridPipeline+WikiFallback" if args.wiki_fallback else "AWQ+HybridPipeline",
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
            "version": "bok-800-arithmetic-glossary-v2-selective" if args.stage in {"selective_unit_scale", "finance_typed_routing"} else "bok-800-arithmetic-glossary-v1",
        },
        "wiki_fallback": {
            "enabled": args.wiki_fallback,
            "path": str(args.wiki_root) if args.wiki_fallback else None,
            "sha256": wiki.digest if wiki is not None else None,
            "documents": len(wiki.pages) if wiki is not None else 0,
            "policy": "glossary exact match first; LLM query planner; BM25 top-1 seed; related-term traversal up to 3 pages; LLM relevance grade; inject accepted context only",
            "max_chars_per_page": 600,
        },
        "arithmetic": {
            "llm_output": (
                "guided JSON {expression, answer_type, result_unit, scale, formula_name, explanation}"
                if args.stage in {"selective_unit_scale", "finance_typed_routing"}
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
            "FinQA": "numeric-only routing" if args.stage == "finqa_numeric_routing" else "AWQ text passthrough",
            "TAT-QA": "AWQ text passthrough",
        },
    }
    (args.output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
