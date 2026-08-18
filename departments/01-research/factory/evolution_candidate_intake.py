"""Persist outcome-conditioned AST children without fabricating new sources.

The literature scout and the formula breeder are deliberately different jobs.  A
scout may introduce a new source.  A breeder starts from an already persisted,
source-backed intraday lead and submits only a typed child equation plus its
economic contract.  The child reuses the parent's immutable source references,
while every formula, lineage, semantic, dimensional, and novelty check is run
again by :mod:`lead_intake` before a revision lead is written.

This module contains no model call and never grades expected alpha.  It is the
deterministic intake boundary used by the Hermes tool surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

_HERE = Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent / "contracts"
for _path in (_HERE, _CONTRACTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from factory_contracts import (  # noqa: E402
    LeadStatus,
    MethodologyLeadV1,
    Testability,
    lead_id_for,
)
import intraday_ast_contract as grammar  # noqa: E402
import lead_intake  # noqa: E402
from literature_derivation import (  # noqa: E402
    CROSS_DOMAIN_TRANSFER,
    DERIVATION_TRANSFORMS,
    MECHANISM_MUTATION,
)


MODULE_VERSION = "outcome-conditioned-evolution-intake-v2"
MAX_EVOLUTION_BATCH = 64

_PARENT_SQL = """
select lead_id, case_id, scout_lens, source_type, refs, ast_contract,
       claimed_edge, stated_mechanism, market_context, stated_failure_mode
  from research.methodology_leads
 where lead_id = %s
"""


def _json(value: Any, *, field: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be valid JSON") from exc


def _parent_mapping(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(
        ("lead_id", "case_id", "scout_lens", "source_type", "refs",
         "ast_contract", "claimed_edge", "stated_mechanism",
         "market_context", "stated_failure_mode"),
        row,
    ))


def _nonblank(candidate: dict[str, Any], key: str) -> Any:
    value = candidate.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{key} is required for an evolved formula")
    return value


def _substantive(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    normalized = text.upper().replace("-", "_").replace(" ", "_")
    if not text or any(marker in normalized for marker in (
            "REQUIRES_HERMES", "TODO", "TBD", "UNSPECIFIED", "PLACEHOLDER")):
        raise ValueError(f"{field} must contain a substantive economic statement")
    return text


def _derivation_contract(parent_contract: dict[str, Any], parent_expr: dict,
                         candidate: dict[str, Any]) -> tuple[str, Any,
                                                              list[str]]:
    """Carry the real source baseline forward and add controlled mutations."""
    parent_mode = str(parent_contract.get("derivation_mode") or "").upper()
    mode = (CROSS_DOMAIN_TRANSFER if parent_mode == CROSS_DOMAIN_TRANSFER
            else MECHANISM_MUTATION)
    baseline = parent_contract.get("source_baseline_expr")
    if baseline in (None, "") and mode == MECHANISM_MUTATION:
        # Older current-contract parents occasionally lack a separate public
        # baseline.  Their source-backed executable equation is the only honest
        # reconstructable baseline; it is never presented as a new citation.
        baseline = parent_expr

    requested = candidate.get("derivation_transforms")
    if requested is None:
        requested = candidate.get("evolution_operators") or ()
    requested = _json(requested, field="derivation_transforms")
    if isinstance(requested, str):
        requested = requested.split(",")
    inherited = parent_contract.get("derivation_transforms") or ()
    transforms = sorted({
        str(value).strip().upper()
        for value in tuple(inherited) + tuple(requested)
        if str(value).strip().upper() in DERIVATION_TRANSFORMS
    })
    if mode == CROSS_DOMAIN_TRANSFER:
        transforms = sorted(set(transforms) | {"MARKET_STRUCTURE_TRANSFER"})
    if not transforms:
        raise ValueError(
            "an evolved formula requires a controlled derivation transform")
    return mode, baseline, transforms


def build_evolved_lead(parent: dict[str, Any], candidate: dict[str, Any], *,
                       model_version: str, prompt_version: str,
                       as_known_at: datetime | None = None) -> dict[str, Any]:
    """Validate one AST child and return a persistence-ready lead mapping."""
    if not model_version.strip() or not prompt_version.strip():
        raise ValueError("model_version and prompt_version are required")
    parent_contract = _json(parent.get("ast_contract") or {},
                            field="parent ast_contract")
    if not isinstance(parent_contract, dict):
        raise ValueError("parent ast_contract must be a JSON object")
    if (parent_contract.get("research_lane") != "INTRADAY_EVENT"
            or parent_contract.get("formula_discovery_version")
            != "formula-discovery-v5"
            or parent_contract.get("formula_contract_complete") is not True
            or parent_contract.get("alpha_candidate_eligible") is not True):
        raise ValueError(
            "parent must be an eligible formula-discovery-v5 INTRADAY_EVENT lead")

    parent_expr = grammar.parse(parent_contract.get("candidate_signal_expr"))
    parent_window_contract = str(
        parent_contract.get("feature_window_contract_version") or
        grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
    grammar.validate_feature_window_contract(
        parent_expr, contract_version=parent_window_contract)
    child_expr = grammar.parse(_json(
        _nonblank(candidate, "candidate_signal_expr"),
        field="candidate_signal_expr"))
    if grammar.fingerprint(child_expr) == grammar.fingerprint(parent_expr):
        raise ValueError("evolution child exactly reuses its parent formula")

    semantic_plan = _json(_nonblank(candidate, "semantic_plan"),
                          field="semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("semantic_plan must be a JSON object")
    child_window_contract = str(
        candidate.get("feature_window_contract_version") or
        (grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
         if any(seconds is not None for _field, seconds in
                grammar.field_window_bindings_of(child_expr))
         else grammar.LEGACY_FEATURE_WINDOW_CONTRACT))
    child_expr = grammar.validate_feature_window_contract(
        child_expr, contract_version=child_window_contract)
    child_expr = grammar.validate_completed_second_candidate(
        child_expr, execution=semantic_plan.get("execution"))
    thesis = _json(_nonblank(candidate, "formula_thesis"),
                   field="formula_thesis")
    evolution_operators = _json(
        _nonblank(candidate, "evolution_operators"),
        field="evolution_operators")
    ablations = _json(_nonblank(candidate, "ablations"), field="ablations")
    expected_increment = _substantive(
        _nonblank(candidate, "expected_increment"), field="expected_increment")
    mechanism = _substantive(
        candidate.get("economic_mechanism")
        or candidate.get("mechanism")
        or parent.get("stated_mechanism")
        or "", field="economic_mechanism")
    novelty_rationale = _substantive(
        candidate.get("novelty_rationale") or expected_increment,
        field="novelty_rationale")
    if isinstance(thesis, dict):
        _substantive(thesis.get("identification"),
                     field="formula_thesis.identification")
    mode, source_baseline, derivation_transforms = _derivation_contract(
        parent_contract, parent_expr, candidate)

    normalized_evolution_operators = {
        str(value).strip().upper()
        for value in evolution_operators
        if str(value).strip()
    }
    if parent_window_contract != child_window_contract:
        if not (
                parent_window_contract ==
                grammar.LEGACY_FEATURE_WINDOW_CONTRACT
                and child_window_contract ==
                grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT):
            raise ValueError(
                "feature-window evolution only supports an auditable "
                "legacy-to-explicit migration")
        if ("PRIMITIVE_WINDOW_MIGRATION" not in
                normalized_evolution_operators
                or "PRIMITIVE_WINDOW_MIGRATION" not in
                derivation_transforms):
            raise ValueError(
                "legacy-to-explicit feature-window evolution requires "
                "PRIMITIVE_WINDOW_MIGRATION provenance")

    block = {
        "READINESS": "AST_READY",
        "RESEARCH_LANE": "INTRADAY_EVENT",
        "OBSERVABLES": sorted(grammar.fields_of(child_expr)),
        "CANDIDATE_SIGNAL_EXPR": child_expr,
        "FEATURE_WINDOW_CONTRACT_VERSION": child_window_contract,
        "SEMANTIC_PLAN": semantic_plan,
        "DERIVATION_MODE": mode,
        "SOURCE_BASELINE_EXPR": source_baseline,
        "DERIVATION_TRANSFORMS": derivation_transforms,
        "NOVELTY_RATIONALE": novelty_rationale,
        "PARENT_SIGNAL_EXPR": parent_expr,
        "EVOLUTION_OPERATORS": evolution_operators,
        "EXPECTED_INCREMENT": expected_increment,
        "ABLATIONS": ablations,
        "FORMULA_THESIS": thesis,
        "TESTABLE_WITH": str(candidate.get("testable_with") or mechanism),
        "LESSONS_ADDRESSED": candidate.get("lessons_addressed") or "",
    }
    ast_contract = lead_intake._readiness_metadata(block, mechanism)  # noqa: SLF001
    ast_contract["parent_feature_window_contract_version"] = \
        parent_window_contract

    refs = _json(parent.get("refs") or [], field="parent refs")
    if not isinstance(refs, list) or not refs:
        raise ValueError("parent has no reusable source references")
    now = as_known_at or datetime.now(timezone.utc)
    payload = {
        "lead_id": lead_id_for(refs),
        "case_id": f"evolution-{now:%Y%m%d}",
        "scout_lens": parent["scout_lens"],
        "source_type": parent["source_type"],
        "as_known_at": now,
        "refs": refs,
        "ast_contract": ast_contract,
        "claimed_edge": str(
            candidate.get("claimed_edge")
            or candidate.get("title")
            or f"Evolved child of {parent.get('claimed_edge') or parent['lead_id']}"
        ).strip(),
        "stated_mechanism": mechanism,
        # The cited source supports the parent mechanism; the exact child is an
        # explicitly inferred, outcome-conditioned hypothesis.
        "inferred": True,
        "market_context": str(
            candidate.get("market_context") or parent.get("market_context") or ""),
        "stated_failure_mode": str(
            candidate.get("failure_mode")
            or parent.get("stated_failure_mode") or ""),
        "independent_mentions": 1,
        "testability": Testability.RULE_EXPRESSIBLE,
        "status": LeadStatus.COMPLETE,
        "model_version": model_version.strip(),
        "prompt_version": prompt_version.strip(),
    }
    # Validate source identity, timestamps, enums, and required lead invariants
    # before the lower-level revision-aware upsert sees the payload.
    # JSON mode keeps nested source timestamps serialisable for
    # ``lead_intake.persist`` while PostgreSQL still accepts the ISO timestamp
    # for the top-level ``as_known_at`` column.
    return MethodologyLeadV1.model_validate(payload).model_dump(mode="json")


def submit(conn, *, parent_lead_id: str, candidates: list[dict[str, Any]] | str,
           model_version: str, prompt_version: str) -> dict[str, Any]:
    """Validate and persist a bounded population of children for one parent."""
    candidates = _json(candidates, field="candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a non-empty JSON array")
    if len(candidates) > MAX_EVOLUTION_BATCH:
        raise ValueError(
            f"evolution batch exceeds {MAX_EVOLUTION_BATCH} candidates")
    with conn.cursor() as cur:
        cur.execute(_PARENT_SQL, (str(parent_lead_id),))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown parent_lead_id: {parent_lead_id}")
    parent = _parent_mapping(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    fingerprints: set[str] = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            rejected.append({"index": str(index), "reason": "candidate must be an object"})
            continue
        try:
            lead = build_evolved_lead(
                parent, raw, model_version=model_version,
                prompt_version=prompt_version)
            fingerprint = str(lead["ast_contract"]["ast_fingerprint"])
            if fingerprint in fingerprints:
                raise ValueError("duplicate AST within evolution batch")
            fingerprints.add(fingerprint)
            accepted.append(lead)
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({
                "index": str(index),
                "title": str(raw.get("title") or "")[:120],
                "reason": str(exc),
            })

    new = duplicate = 0
    lead_ids: list[str] = []
    if accepted:
        new, duplicate, lead_ids = lead_intake.persist(
            conn, accepted, return_ids=True)
    return {
        "ok": bool(accepted),
        "module_version": MODULE_VERSION,
        "parent_lead_id": str(parent_lead_id),
        "submitted": len(candidates),
        "accepted": len(accepted),
        "new": new,
        "merged_as_mention": duplicate,
        "lead_ids": lead_ids,
        "rejected": rejected,
    }


__all__ = [
    "MAX_EVOLUTION_BATCH",
    "MODULE_VERSION",
    "build_evolved_lead",
    "submit",
]
