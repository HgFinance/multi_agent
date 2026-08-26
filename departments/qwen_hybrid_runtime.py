"""Runtime middleware for the evaluated Qwen AWQ Hybrid Upgrade v1 pipeline.

The benchmark implementation lives under ``benchmarks/``.  Production workers
must not import benchmark runners, so this module carries only the generic,
answer-independent treatments that passed the paired A/B evaluation:

* selective finance/numeric routing;
* unit and scale instructions before model arithmetic;
* query-scoped deterministic glossary injection;
* strict JSON-schema validation and a bounded semantic-repair prompt.

It never contains expected answers and never replaces a model answer with a
deterministic finance answer.  Binding finance calculations remain in domain
engines rather than this LLM middleware.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

PIPELINE_VERSION = "awq-hybrid-upgrade-v1"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_GLOSSARY = (
    _REPO_ROOT
    / "benchmarks"
    / "quantization"
    / "knowledge"
    / "bok800_2026"
    / "glossary_rag_v1.json"
)

_NUMERIC_CUES = re.compile(
    r"(?:\d[\d,.]*\s*(?:%|퍼센트|bp|bps|원|만원|억원|천|만|백만|십억|"
    r"shares?|주)|계산|산출|비율|증가율|감소율|수익률|손익|pnl|notional|"
    r"exposure|margin|fifo|평균단가|손절가|익절가|수량\s*한도)",
    re.IGNORECASE,
)

_UNIT_SCALE_INSTRUCTION = """
HYBRID UPGRADE V1 — UNIT/SCALE CONTRACT:
- Normalize units before arithmetic. Convert p% to p/100; 0.015% means
  0.015/100, not 0.015. Distinguish a fraction, percent, percentage point and
  basis point. One basis point is 0.0001 as a fraction.
- Expand Korean/English scales (천/thousand, 만, 백만/million, 억,
  십억/billion) and put numerator and denominator on the same scale.
- Preserve the requested result unit. Never silently reinterpret currency as
  a ratio, exposure as notional, or shares as currency.
- If required values or their units are missing or ambiguous, report that
  uncertainty instead of inventing a number.
""".strip()


@dataclass(frozen=True)
class GlossaryHit:
    term: str
    definition: str


@dataclass(frozen=True)
class PreparedHybridRequest:
    system: str
    prompt: str
    model: str
    route: str
    matched_terms: tuple[str, ...]
    glossary_sha256: str | None
    unit_scale_applied: bool

    def metadata(self) -> dict[str, Any]:
        return {
            "pipeline_version": PIPELINE_VERSION,
            "route": self.route,
            "model": self.model,
            "matched_terms": list(self.matched_terms),
            "glossary_sha256": self.glossary_sha256,
            "unit_scale_applied": self.unit_scale_applied,
        }


def _enabled(config: Mapping[str, Any] | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    env = os.environ.get("WORKER_HYBRID_PIPELINE_ENABLED")
    if env is not None:
        return env.strip().casefold() in {"1", "true", "yes", "on"}
    return str(config.get("status", "")).casefold() == "enabled"


def _glossary_path(config: Mapping[str, Any]) -> Path:
    configured = os.environ.get("WORKER_GLOSSARY_PATH") or config.get("glossary_path")
    path = Path(str(configured)).expanduser() if configured else _DEFAULT_GLOSSARY
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


@lru_cache(maxsize=4)
def _load_glossary(path_text: str) -> tuple[str, tuple[dict[str, Any], ...]]:
    path = Path(path_text)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("glossary must be a JSON array")
    rows: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or "").strip()
        definition = str(row.get("definition") or "").strip()
        aliases = row.get("aliases") or []
        if term and definition and isinstance(aliases, list):
            rows.append(
                {
                    "term": term,
                    "definition": definition,
                    "aliases": tuple(str(value).strip() for value in aliases if str(value).strip()),
                }
            )
    return hashlib.sha256(raw).hexdigest(), tuple(rows)


def _contains_term(text: str, candidate: str) -> bool:
    candidate = candidate.strip().casefold()
    if len(candidate) < 2:
        return False
    lowered = text.casefold()
    if re.fullmatch(r"[a-z0-9 .&()/_-]+", candidate):
        return re.search(r"(?<![\w])" + re.escape(candidate) + r"(?![\w])", lowered) is not None
    return candidate in lowered


def glossary_hits(
    query: str, config: Mapping[str, Any], *, max_hits: int = 3
) -> tuple[str | None, tuple[GlossaryHit, ...]]:
    try:
        digest, entries = _load_glossary(str(_glossary_path(config)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, ()
    hits: list[GlossaryHit] = []
    for entry in entries:
        candidates = (entry["term"], *entry["aliases"])
        if any(_contains_term(query, candidate) for candidate in candidates):
            definition = re.sub(r"\s+", " ", entry["definition"]).strip()[:700]
            hits.append(GlossaryHit(entry["term"], definition))
            if len(hits) >= max_hits:
                break
    return digest, tuple(hits)


def prepare_request(
    *,
    system: str,
    prompt: str,
    base_model: str,
    config: Mapping[str, Any] | None,
    json_schema: Mapping[str, Any] | None = None,
) -> PreparedHybridRequest:
    """Select and apply generic Hybrid Upgrade treatments for one Qwen call."""

    if not _enabled(config):
        return PreparedHybridRequest(system, prompt, base_model, "base", (), None, False)

    assert isinstance(config, Mapping)
    joined = f"{system}\n{prompt}"
    numeric = bool(_NUMERIC_CUES.search(joined))
    model = base_model
    if numeric and str(config.get("numeric_adapter_model") or "").strip():
        model = str(config["numeric_adapter_model"]).strip()

    prepared_system = system
    if numeric:
        prepared_system = f"{prepared_system}\n\n{_UNIT_SCALE_INSTRUCTION}"

    glossary_digest: str | None = None
    hits: tuple[GlossaryHit, ...] = ()
    if bool(config.get("glossary_enabled", True)):
        glossary_digest, hits = glossary_hits(prompt, config)
    prepared_prompt = prompt
    if hits:
        context = "\n".join(f"- {hit.term}: {hit.definition}" for hit in hits)
        prepared_prompt = (
            "GLOSSARY — deterministic query matches only. Use only when relevant; "
            "the user/tool evidence remains authoritative.\n"
            f"{context}\n\n{prompt}"
        )

    if json_schema is not None:
        route = "guided_json_numeric" if numeric else "guided_json"
    elif numeric:
        route = "numeric_unit_scale"
    elif hits:
        route = "glossary"
    else:
        route = "base"
    return PreparedHybridRequest(
        prepared_system,
        prepared_prompt,
        model,
        route,
        tuple(hit.term for hit in hits),
        glossary_digest,
        numeric,
    )


def validate_structured_output(raw: str, schema: Mapping[str, Any]) -> str | None:
    """Return a compact validation error, or ``None`` when output is valid."""

    try:
        value = json.loads(str(raw).strip())
    except (TypeError, json.JSONDecodeError) as exc:
        return f"invalid JSON: {type(exc).__name__}"

    def finite(node: Any, path: str = "$") -> str | None:
        if isinstance(node, float) and not math.isfinite(node):
            return f"non-finite number at {path}"
        if isinstance(node, dict):
            for key, child in node.items():
                error = finite(child, f"{path}.{key}")
                if error:
                    return error
        if isinstance(node, list):
            for index, child in enumerate(node):
                error = finite(child, f"{path}[{index}]")
                if error:
                    return error
        return None

    error = finite(value)
    if error:
        return error
    try:
        from jsonschema import Draft202012Validator

        violation = next(Draft202012Validator(dict(schema)).iter_errors(value), None)
    except ModuleNotFoundError:  # vLLM guided decoding still constrains syntax
        violation = None
    if violation is None:
        return None
    location = ".".join(str(item) for item in violation.absolute_path)
    return f"schema violation{f' at {location}' if location else ''}: {violation.message}"[:800]


def validate_financial_semantics(raw: str) -> str | None:
    """Check common numeric relationships without supplying an answer.

    This validator only compares values already emitted by the model.  It does
    not parse the user's prose, choose a formula, or replace a bad value.  A
    domain engine remains authoritative; these generic relationships merely
    prevent syntactically valid JSON from silently contradicting itself.
    """

    try:
        value = json.loads(str(raw).strip())
    except (TypeError, json.JSONDecodeError):
        return None

    def number(row: Mapping[str, Any], key: str) -> Decimal | None:
        raw_value = row.get(key)
        if isinstance(raw_value, bool) or raw_value is None:
            return None
        try:
            result = Decimal(str(raw_value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

    def close(actual: Decimal, expected: Decimal) -> bool:
        tolerance = max(Decimal("0.000001"), abs(expected) * Decimal("0.000001"))
        return abs(actual - expected) <= tolerance

    def check(row: Mapping[str, Any], path: str) -> str | None:
        relationships = (
            (("price", "quantity", "notional"), lambda p, q: p * q, "price*quantity=notional"),
            (("entry_price", "quantity", "position_notional"), lambda p, q: p * q, "entry_price*quantity=position_notional"),
            (("entry_price", "stop_price", "quantity", "position_risk_amount"),
             lambda entry, stop, quantity: abs(entry - stop) * quantity,
             "abs(entry_price-stop_price)*quantity=position_risk_amount"),
            (("entry_price", "stop_price", "take_profit_price", "reward_risk_ratio"),
             lambda entry, stop, take_profit: abs(take_profit - entry) / abs(entry - stop),
             "reward/risk price distance=reward_risk_ratio"),
            (("old_value", "new_value", "decrease_rate_pct"),
             lambda old, new: (old - new) / old * Decimal(100),
             "(old_value-new_value)/old_value*100=decrease_rate_pct"),
            (("proceeds", "cost_basis", "fees", "pnl"),
             lambda proceeds, cost, fees: proceeds - cost - fees,
             "proceeds-cost_basis-fees=pnl"),
        )
        for fields, formula, label in relationships:
            numbers = [number(row, field) for field in fields]
            if any(item is None for item in numbers):
                continue
            *inputs, actual = numbers
            try:
                expected = formula(*inputs)
            except (ArithmeticError, InvalidOperation, ZeroDivisionError):
                return f"financial semantic input invalid at {path}: {label}"
            assert actual is not None
            if not close(actual, expected):
                return f"financial semantic mismatch at {path}: {label}"
        return None

    def walk(node: Any, path: str = "$") -> str | None:
        if isinstance(node, Mapping):
            error = check(node, path)
            if error:
                return error
            for key, child in node.items():
                error = walk(child, f"{path}.{key}")
                if error:
                    return error
        elif isinstance(node, list):
            for index, child in enumerate(node):
                error = walk(child, f"{path}[{index}]")
                if error:
                    return error
        return None

    return walk(value)


def validate_prompt_financial_alignment(prompt: str, raw: str) -> str | None:
    """Anchor unambiguous price/quantity fields to explicit prompt evidence.

    The check is deliberately narrow: it runs only when the prompt contains
    exactly one labelled KRW price and/or one share quantity and the JSON uses
    the canonical ``price``/``quantity`` fields.  Ambiguous multi-leg trades
    are left to their domain engine.  Like the relationship validator above,
    this rejects drift but never calculates or inserts an answer.
    """

    try:
        value = json.loads(str(raw).strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None

    number_text = r"([+-]?\d[\d,]*(?:\.\d+)?)"
    price_patterns = (
        re.compile(number_text + r"\s*\uc6d0\s*(?:\uc9dc\ub9ac|\uc778)?\s*(?:\uc8fc\uc2dd|\uc885\ubaa9|\uc790\uc0b0)"),
        re.compile(
            r"(?:price|priced|at)\s*(?:is|=|:)?\s*(?:krw\s*)?"
            + number_text,
            re.IGNORECASE,
        ),
    )
    quantity_patterns = (
        re.compile(number_text + r"\s*\uc8fc(?:\ub97c|\uc758|\s|$)"),
        re.compile(
            r"(?:quantity|qty|shares?)\s*(?:is|=|:)?\s*" + number_text,
            re.IGNORECASE,
        ),
    )

    def unique(patterns: tuple[re.Pattern[str], ...]) -> Decimal | None:
        matches: set[Decimal] = set()
        for pattern in patterns:
            for match in pattern.finditer(prompt):
                try:
                    matches.add(Decimal(match.group(1).replace(",", "")))
                except (InvalidOperation, ValueError):
                    continue
        return next(iter(matches)) if len(matches) == 1 else None

    for field, expected in (
        ("price", unique(price_patterns)),
        ("quantity", unique(quantity_patterns)),
    ):
        if expected is None or field not in value:
            continue
        raw_actual = value.get(field)
        if isinstance(raw_actual, bool):
            continue
        try:
            actual = Decimal(str(raw_actual))
        except (InvalidOperation, ValueError):
            continue
        if actual != expected:
            return f"financial source alignment mismatch: {field}"
    return None


def semantic_repair_prompt(original_prompt: str, raw: str, error: str) -> str:
    """Build one bounded repair turn without supplying or calculating an answer."""

    previous = (
        "Previous output omitted because it drifted from explicit source values."
        if error.startswith("financial source alignment mismatch")
        else f"Previous output:\n{str(raw)[:4000]}"
    )
    return (
        f"{original_prompt}\n\n"
        "Your previous JSON failed the application contract. Repair only syntax, "
        "types, field meanings, units and scale using the original evidence. Do not "
        "invent missing values and do not change a domain-engine decision. When the "
        "error is source alignment, copy the corresponding field from the single "
        "unambiguous labelled value in the original prompt before recomputing any "
        "dependent field.\n"
        f"Validation error: {error}\n{previous}"
    )


__all__ = [
    "PIPELINE_VERSION",
    "PreparedHybridRequest",
    "glossary_hits",
    "prepare_request",
    "semantic_repair_prompt",
    "validate_financial_semantics",
    "validate_prompt_financial_alignment",
    "validate_structured_output",
]
