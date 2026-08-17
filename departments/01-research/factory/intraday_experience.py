"""Outcome-conditioned semantic memory for event-time alpha search.

Only quant-accepted ASTs enter this view.  It does not claim that a successful
operator caused performance; it exposes tested cells, residual failures, and
underexplored semantic coordinates so the next agent can mutate deliberately.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from statistics import median
import sys

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from alpha_semantics import (CONTEXTS, INTRADAY_EVENTS, QUALITIES, fingerprint,
                             validate)  # noqa: E402
import intraday_ast_contract as formula  # noqa: E402


NEGATIVE = frozenset({"REJECT", "GATE_HOLD", "KILLED", "DEMOTED"})


@dataclass(frozen=True)
class IntradayMemory:
    experiments: int
    semantic_families: int
    formula_shapes: int
    history: tuple[dict, ...]
    underexplored_events: tuple[str, ...]
    underexplored_contexts: tuple[str, ...]
    underexplored_qualities: tuple[str, ...]
    positive_components: tuple[tuple[str, int], ...]
    negative_components: tuple[tuple[str, int], ...]
    candidate_population: int
    population_shapes: int
    niche_elites: tuple[dict, ...]
    recycled_candidates: tuple[dict, ...]
    lineage_tournaments: tuple[dict, ...]
    breeding_parents: tuple[dict, ...]
    empirical_term_influence: tuple[dict, ...]
    frequent_losing_subtrees: tuple[dict, ...]
    reusable_term_bank: tuple[dict, ...]
    generation_arm_audit: dict


def _walk(node, fields: set[str], operators: set[str], clocks: set[int]) -> None:
    if not isinstance(node, dict):
        return
    op = node.get("op")
    if op:
        operators.add(str(op))
    if op == "field" and node.get("field"):
        fields.add(str(node["field"]))
    if "seconds" in node:
        try:
            clocks.add(int(node["seconds"]))
        except (TypeError, ValueError):
            pass
    for key in ("arg", "condition", "then", "else"):
        _walk(node.get(key), fields, operators, clocks)
    for child in node.get("args") or ():
        _walk(child, fields, operators, clocks)


def _shape(node):
    if isinstance(node, list):
        return [_shape(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in sorted(node.items()):
        out[key] = "#" if key in {"seconds", "const"} else _shape(value)
    return out


def _shape_fp(expr: dict) -> str:
    payload = json.dumps(_shape(expr), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _subtree_shapes(expr: dict, *, min_nodes: int = 3) -> dict[str, dict]:
    """Canonical non-trivial subtree shapes, with clocks/constants masked.

    Field names remain visible because a rolling transform over signed OFI is
    economically different from the same syntax over unsigned activity.  Each
    shape is counted once per formula so repeated syntax inside one AST cannot
    manufacture frequency.
    """
    out: dict[str, dict] = {}

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        if formula.count_nodes(node) >= min_nodes:
            shaped = _shape(node)
            payload = json.dumps(shaped, sort_keys=True, separators=(",", ":"))
            key = hashlib.sha256(payload.encode()).hexdigest()[:16]
            out.setdefault(key, shaped)
        for name in ("arg", "condition", "then", "else"):
            walk(node.get(name))
        for child in node.get("args") or ():
            walk(child)

    walk(expr)
    return out


def _frequent_losing_subtrees(rows: list[dict], *, min_support: int = 2
                              ) -> tuple[dict, ...]:
    """Find repeated losing structures across independently evaluated formulas.

    This is a search prior, never a gate.  Screening-only sidecars and repeated
    trials of the same exact AST cannot create support.  Any independently
    positive-net occurrence prevents a shape from entering the losing list.
    """
    by_formula: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("evidence_tier") == "SCREENING_ONLY":
            continue
        by_formula[row["ast_fingerprint"]].append(row)

    stats: dict[str, dict] = {}
    for ast_fp, trials in by_formula.items():
        measured = any(
            _number(row.get("oos_summary") or {},
                    "mean_net_bps_per_opportunity") is not None
            or str(row.get("decision") or "").upper() in NEGATIVE | {
                "SUBMIT_TO_QA", "PROMOTED"}
            for row in trials)
        if not measured:
            continue
        positive = any(
            str(row.get("decision") or "").upper() in {
                "SUBMIT_TO_QA", "PROMOTED"}
            or (_number(row.get("oos_summary") or {},
                        "mean_net_bps_per_opportunity") or 0.0) > 0.0
            for row in trials)
        shapes = _subtree_shapes(trials[0]["expr"])
        for key, shape in shapes.items():
            item = stats.setdefault(key, {
                "subtree_fingerprint": key, "shape": shape,
                "support": 0, "losing_support": 0, "positive_support": 0,
                "formula_fingerprints": [],
            })
            item["support"] += 1
            item["positive_support" if positive else "losing_support"] += 1
            item["formula_fingerprints"].append(ast_fp)

    losing = [item for item in stats.values()
              if item["support"] >= min_support
              and item["losing_support"] >= min_support
              and item["positive_support"] == 0]
    for item in losing:
        item["shape_label"] = json.dumps(
            item["shape"], sort_keys=True, separators=(",", ":"))
    return tuple(sorted(losing, key=lambda item: (
        -item["losing_support"], item["subtree_fingerprint"])))


def _numeric_subtrees(expr: dict, *, min_nodes: int = 2,
                      max_nodes: int = 18) -> dict[str, dict]:
    """Return reusable, typed numeric subexpressions, excluding the full AST.

    Boolean predicates are not standalone signals, but their numeric children
    remain eligible.  Exact constants and clocks are retained: the term bank is
    executable material, while shape-level novelty is handled elsewhere.
    """
    root = formula.parse(expr)
    out: dict[str, dict] = {}

    def walk(node: dict, *, is_root: bool = False) -> None:
        try:
            parsed = formula.parse(node)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and not is_root:
            nodes = formula.count_nodes(parsed)
            if min_nodes <= nodes <= max_nodes:
                out.setdefault(formula.fingerprint(parsed), parsed)
        for name in ("arg", "condition", "then", "else"):
            child = node.get(name)
            if isinstance(child, dict):
                walk(child)
        for child in node.get("args") or ():
            if isinstance(child, dict):
                walk(child)

    walk(root, is_root=True)
    return out


def _reusable_term_bank(rows: list[dict], leads: list[dict]) -> tuple[dict, ...]:
    """Build an external AST-term dictionary without assigning causal credit.

    Support is counted once per exact whole formula.  Primary, screening, and
    untested proposal evidence stay separate so a term discovered during a
    shared replay cannot acquire promotion authority by appearing frequently.
    """
    stats: dict[str, dict] = {}

    def add(expr: dict, *, formula_fp: str, evidence: str,
            event: str = "UNKNOWN", lead_id: str = "") -> None:
        for term_fp, term in _numeric_subtrees(expr).items():
            item = stats.setdefault(term_fp, {
                "term_fingerprint": term_fp,
                "term_ast": term,
                "shape_fingerprint": formula.shape_fingerprint(term),
                "unit": formula.unit_of(term),
                "nodes": formula.count_nodes(term),
                "fields": sorted(formula.fields_of(term)),
                "formula_fingerprints": set(),
                "primary_positive_formulas": set(),
                "primary_negative_formulas": set(),
                "screening_positive_formulas": set(),
                "screening_negative_formulas": set(),
                "unresolved_formulas": set(),
                "proposal_formulas": set(),
                "lead_ids": set(),
                "events": set(),
            })
            item["formula_fingerprints"].add(formula_fp)
            item[f"{evidence}_formulas"].add(formula_fp)
            if lead_id:
                item["lead_ids"].add(lead_id)
            if event:
                item["events"].add(event)

    for row in rows:
        summary = row.get("oos_summary") or {}
        net = _number(summary, "mean_net_bps_per_opportunity")
        decision = str(row.get("decision") or "").upper()
        screening = row.get("evidence_tier") == "SCREENING_ONLY"
        positive = decision in {"SUBMIT_TO_QA", "PROMOTED"} or (
            net is not None and net > 0.0)
        measured = net is not None or decision in NEGATIVE | {
            "SUBMIT_TO_QA", "PROMOTED"}
        if screening:
            evidence = "screening_positive" if positive else "screening_negative"
        elif positive:
            evidence = "primary_positive"
        elif measured:
            evidence = "primary_negative"
        else:
            evidence = "unresolved"
        add(row["expr"], formula_fp=row["ast_fingerprint"], evidence=evidence,
            event=str((row.get("plan") or {}).get("event") or "UNKNOWN"))

    seen_leads: set[tuple[str, str]] = set()
    for lead in leads:
        try:
            expr = formula.parse(lead.get("intraday_signal_expr"))
            formula_fp = formula.fingerprint(expr)
            plan = validate(lead.get("semantic_plan") or {})
        except (TypeError, ValueError):
            continue
        lead_id = str(lead.get("lead_id") or "")
        if (formula_fp, lead_id) in seen_leads:
            continue
        seen_leads.add((formula_fp, lead_id))
        add(expr, formula_fp=formula_fp, evidence="proposal",
            event=plan["event"], lead_id=lead_id)

    bank = []
    for item in stats.values():
        positive = len(item["primary_positive_formulas"])
        negative = len(item["primary_negative_formulas"])
        screening_positive = len(item["screening_positive_formulas"])
        proposals = len(item["proposal_formulas"])
        if positive:
            status = "PRIMARY_POSITIVE_ASSOCIATION"
            search_action = "SET_LEVEL_REUSE_WITH_ABLATION"
        elif negative >= 2 and not screening_positive:
            status = "REPEATED_PRIMARY_LOSER"
            search_action = "EXPLICIT_FAILURE_INVERSION_ONLY"
        elif screening_positive:
            status = "SCREENING_POSITIVE_ONLY"
            search_action = "INDEPENDENT_PRIMARY_CONFIRMATION"
        elif proposals >= 2:
            status = "RECURRING_UNTESTED"
            search_action = "EVALUATE_OR_STOP_REPROPOSING"
        else:
            status = "UNTESTED_COMPONENT"
            search_action = "FRESH_ONLY_IF_MECHANISM_JUSTIFIED"
        bank.append({
            "term_fingerprint": item["term_fingerprint"],
            "shape_fingerprint": item["shape_fingerprint"],
            "term_ast": item["term_ast"],
            "term_label": json.dumps(
                item["term_ast"], sort_keys=True, separators=(",", ":")),
            "unit": item["unit"], "nodes": item["nodes"],
            "fields": item["fields"], "status": status,
            "search_action": search_action,
            "formula_support": len(item["formula_fingerprints"]),
            "primary_positive_support": positive,
            "primary_negative_support": negative,
            "screening_positive_support": screening_positive,
            "screening_negative_support": len(
                item["screening_negative_formulas"]),
            "proposal_support": proposals,
            "source_events": sorted(item["events"]),
            "source_lead_ids": sorted(item["lead_ids"]),
        })
    priority = {
        "PRIMARY_POSITIVE_ASSOCIATION": 0,
        "SCREENING_POSITIVE_ONLY": 1,
        "UNTESTED_COMPONENT": 2,
        "RECURRING_UNTESTED": 3,
        "REPEATED_PRIMARY_LOSER": 4,
    }
    return tuple(sorted(bank, key=lambda item: (
        priority[item["status"]], -item["formula_support"],
        item["nodes"], item["term_fingerprint"])))


def _number(summary: dict, key: str):
    try:
        value = summary.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError, AttributeError):
        return None


def _semantic_distance(left: dict, right: dict) -> float:
    """Distance in economic coordinates, independent of formula spelling."""
    scalar = ("event", "direction", "output", "execution")
    scalar_distance = sum(left[key] != right[key] for key in scalar) / len(scalar)
    set_distances = []
    for key in ("context", "qualities"):
        a, b = set(left[key]), set(right[key])
        set_distances.append(1.0 - len(a & b) / max(1, len(a | b)))
    return 0.6 * scalar_distance + 0.4 * sum(set_distances) / len(set_distances)


def _horizon_bucket(plan: dict) -> str:
    seconds = int(plan["horizon_seconds"])
    if seconds <= 5:
        return "1_5S"
    if seconds <= 30:
        return "6_30S"
    if seconds <= 300:
        return "31_300S"
    return "301_3600S"


def _niche(plan: dict) -> tuple[str, str, str, str, str]:
    """MAP-Elites behavior cell; tuning numbers do not define an idea family."""
    state = "+".join(plan["context"])
    return (plan["event"], state, plan["direction"], plan["execution"],
            _horizon_bucket(plan))


def _quality_diversity_frontier(leads: list[dict], tested: list[dict],
                                losing_subtree_fps: set[str] | None = None
                                ) -> tuple[tuple[dict, ...], tuple[dict, ...], int]:
    """Select one diverse, contract-complete elite per economic niche.

    No backtest result is invented for an unevaluated lead. Quality here means
    research-contract quality; empirical quality belongs to the causal backtest.
    """
    # Screening evidence informs novelty and breeding, but is deliberately not
    # independent confirmation.  Keep the corresponding lead eligible for a
    # later primary preregistration instead of falsely marking it recycled.
    losing_subtree_fps = set(losing_subtree_fps or ())
    tested_fps = {row["ast_fingerprint"] for row in tested
                  if row.get("evidence_tier") != "SCREENING_ONLY"}
    references = [(row["expr"], row["plan"]) for row in tested]
    candidates, recycled = [], []
    for lead in leads:
        if lead.get("used") or lead.get("alpha_candidate_eligible") is False:
            continue
        if lead.get("formula_discovery_version") not in (
                None, "", "formula-discovery-v5"):
            # Keep legacy outcomes as negative/positive history, but do not let a
            # dimensionally invalid v1 lead occupy a live evolutionary niche.
            continue
        try:
            expr = formula.parse(lead.get("intraday_signal_expr"))
            plan = validate(lead.get("semantic_plan") or {})
        except (TypeError, ValueError):
            continue
        fp = formula.fingerprint(expr)
        row = dict(lead)
        row.update(expr=expr, plan=plan, ast_fingerprint=fp,
                   shape_fingerprint=formula.shape_fingerprint(expr),
                   fields=sorted(formula.fields_of(expr)),
                   operators=sorted(formula.operators_of(expr)),
                   clocks_seconds=sorted(formula.clocks_of(expr)),
                   complexity_nodes=formula.count_nodes(expr), niche=_niche(plan))
        if fp in tested_fps:
            recycled.append(row)
            continue
        nearest = max((
            0.65 * formula.structural_similarity(expr, prior_expr)
            + 0.35 * (1.0 - _semantic_distance(plan, prior_plan))
            for prior_expr, prior_plan in references), default=0.0)
        source_similarity = _number(lead, "candidate_vs_source_similarity")
        lineage_complete = bool(
            lead.get("evolution_role") == "CHILD"
            and lead.get("evolution_operators")
            and lead.get("expected_increment")
            and lead.get("ablations"))
        formula_complete = bool(
            lead.get("formula_contract_complete")
            and lead.get("formula_discovery_version") == "formula-discovery-v5")
        losing_overlap = sorted(
            set(_subtree_shapes(expr)) & losing_subtree_fps)
        losing_penalty = min(0.12, 0.04 * len(losing_overlap))
        row.update(
            nearest_library_similarity=nearest,
            novelty_score=1.0 - nearest,
            lineage_complete=lineage_complete,
            formula_contract_complete=formula_complete,
            frequent_losing_subtree_overlap=losing_overlap,
            frequent_losing_subtree_penalty=losing_penalty,
            source_distance=(None if source_similarity is None
                             else 1.0 - source_similarity),
        )
        row["research_quality"] = (
            0.35 * row["novelty_score"]
            + 0.15 * float(lineage_complete)
            + 0.15 * (row["source_distance"]
                      if row["source_distance"] is not None else 0.5)
            + 0.15 * (1.0 - max(0, row["complexity_nodes"] - 10)
                      / max(1, formula.MAX_NODES - 10))
            + 0.20 * float(formula_complete)
            - losing_penalty
        )
        candidates.append(row)

    by_niche: dict[tuple, list[dict]] = defaultdict(list)
    for row in candidates:
        by_niche[row["niche"]].append(row)
    elites = []
    for niche, group in by_niche.items():
        winner = sorted(group, key=lambda row: (
            -row["research_quality"], row["complexity_nodes"],
            row["ast_fingerprint"]))[0]
        winner = dict(winner)
        winner["niche_competitors"] = len(group)
        elites.append(winner)
    elites.sort(key=lambda row: (-row["research_quality"], row["niche"]))
    return tuple(elites), tuple(recycled), len(candidates) + len(recycled)


def _lineage_tournaments(leads: list[dict], tested: list[dict]) -> tuple[dict, ...]:
    """Compare evaluated children with evaluated parents using net outcomes only."""
    tested_by_fp: dict[str, list[dict]] = defaultdict(list)
    for row in tested:
        tested_by_fp[row["ast_fingerprint"]].append(row)

    def best(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        return max(rows, key=lambda row: (
            _number(row.get("oos_summary") or {}, "mean_net_bps_per_opportunity")
            if _number(row.get("oos_summary") or {},
                       "mean_net_bps_per_opportunity") is not None
            else float("-inf")))

    tournaments = []
    seen = set()
    for lead in leads:
        parent_fp = str(lead.get("parent_ast_fingerprint") or "")
        if not parent_fp:
            continue
        try:
            child_fp = formula.fingerprint(lead.get("intraday_signal_expr"))
        except (TypeError, ValueError):
            continue
        key = (parent_fp, child_fp)
        if key in seen:
            continue
        seen.add(key)
        parent, child = best(tested_by_fp[parent_fp]), best(tested_by_fp[child_fp])
        parent_net = (_number(parent.get("oos_summary") or {},
                              "mean_net_bps_per_opportunity") if parent else None)
        child_net = (_number(child.get("oos_summary") or {},
                             "mean_net_bps_per_opportunity") if child else None)
        child_decision = str(child.get("decision") or "") if child else ""
        if child is None:
            status = "PENDING_CHILD_EVALUATION"
        elif parent is None:
            status = "NO_EVALUATED_PARENT"
        elif parent_net is None or child_net is None:
            status = "NO_COMPARABLE_NET_METRIC"
        elif child_net <= parent_net:
            status = "PARENT_WINS"
        elif child_decision not in {"SUBMIT_TO_QA", "PROMOTED"}:
            status = "INCREMENT_WITHHELD_BY_GATE"
        else:
            status = "CHILD_SURVIVES"
        tournaments.append({
            "parent_ast_fingerprint": parent_fp,
            "child_ast_fingerprint": child_fp,
            "lead_id": lead.get("lead_id"),
            "parent_net_bps": parent_net,
            "child_net_bps": child_net,
            "net_increment_bps": (child_net - parent_net
                                  if child_net is not None and parent_net is not None
                                  else None),
            "child_decision": child_decision or "UNEVALUATED",
            "status": status,
        })
    return tuple(sorted(tournaments, key=lambda row: (
        row["status"], row["parent_ast_fingerprint"], row["child_ast_fingerprint"])))


def _generation_arm_audit(leads: list[dict], tested: list[dict]) -> dict:
    """Describe fresh-seed versus lineage outcomes under explicit arm budgets.

    This is an observational process audit, not a promotion test.  Repeated
    primary trials are collapsed to one median net result per exact formula and
    screening-only sidecars are excluded.  A prospective 6/6 protocol below is
    what makes future comparisons budget-matched.
    """
    roles_by_formula: dict[str, set[str]] = defaultdict(set)
    for lead in leads:
        role = str(lead.get("evolution_role") or "SEED").upper()
        arm = {"SEED": "FRESH", "CHILD": "LINEAGE"}.get(role)
        if arm is None:
            continue
        try:
            formula_fp = formula.fingerprint(lead.get("intraday_signal_expr"))
        except (TypeError, ValueError):
            continue
        roles_by_formula[formula_fp].add(arm)

    trials_by_formula: dict[str, list[dict]] = defaultdict(list)
    for row in tested:
        if row.get("evidence_tier") == "SCREENING_ONLY":
            continue
        trials_by_formula[row["ast_fingerprint"]].append(row)

    summaries = {}
    for arm in ("FRESH", "LINEAGE"):
        proposed = sorted(fp for fp, roles in roles_by_formula.items()
                          if roles == {arm})
        observations = []
        for formula_fp in proposed:
            trials = trials_by_formula.get(formula_fp, [])
            nets = [value for row in trials
                    if (value := _number(
                        row.get("oos_summary") or {},
                        "mean_net_bps_per_opportunity")) is not None]
            decisions = {str(row.get("decision") or "").upper()
                         for row in trials}
            if trials:
                observations.append({
                    "ast_fingerprint": formula_fp,
                    "net_bps": median(nets) if nets else None,
                    "gate_survivor": bool(
                        decisions & {"SUBMIT_TO_QA", "PROMOTED"}),
                })
        measured = [row for row in observations if row["net_bps"] is not None]
        summaries[arm] = {
            "proposed_unique": len(proposed),
            "evaluated_unique": len(observations),
            "net_measured_unique": len(measured),
            "median_net_bps": (median(row["net_bps"] for row in measured)
                               if measured else None),
            "positive_net_rate": (
                sum(row["net_bps"] > 0 for row in measured) / len(measured)
                if measured else None),
            "gate_survivor_rate": (
                sum(row["gate_survivor"] for row in observations)
                / len(observations) if observations else None),
        }

    measured_counts = [summaries[arm]["net_measured_unique"]
                       for arm in ("FRESH", "LINEAGE")]
    if min(measured_counts) == 0:
        status = "NO_COMPARABLE_OUTCOMES"
    elif min(measured_counts) < 3:
        status = "TOO_FEW_PER_ARM"
    elif measured_counts[0] != measured_counts[1]:
        status = "UNBALANCED_OBSERVATIONAL"
    else:
        status = "BALANCED_OBSERVATIONAL"
    return {
        "status": status,
        "matched_net_sample_size": min(measured_counts),
        "arms": summaries,
        "ambiguous_role_formulas": sum(
            len(roles) != 1 for roles in roles_by_formula.values()),
        "promotion_authority": False,
    }


def build(rows: list[dict], leads: list[dict] | None = None) -> IntradayMemory:
    parsed = []
    positive_components = Counter()
    negative_components = Counter()
    used_events, used_contexts, used_qualities = set(), set(), set()
    for row in rows:
        try:
            plan = validate(row.get("semantic_plan") or {})
            expr = row.get("intraday_signal_expr")
            if not isinstance(expr, dict):
                continue
        except (TypeError, ValueError):
            continue
        fields, operators, clocks = set(), set(), set()
        _walk(expr, fields, operators, clocks)
        evidence_tier = str(row.get("evidence_tier") or "PRIMARY").upper()
        item = {**row, "plan": plan, "expr": formula.parse(expr),
                "ast_fingerprint": formula.fingerprint(expr),
                "semantic_fingerprint": fingerprint(plan),
                "shape_fingerprint": _shape_fp(expr), "fields": sorted(fields),
                "operators": sorted(operators), "clocks": sorted(clocks),
                "evidence_tier": evidence_tier}
        parsed.append(item)
        used_events.add(plan["event"])
        used_contexts.update(plan["context"])
        used_qualities.update(plan["qualities"])
        summary = row.get("oos_summary") or {}
        decision = str(row.get("decision") or "").upper()
        positive = (decision == "SUBMIT_TO_QA" or
                    (_number(summary, "mean_net_bps_per_opportunity") or 0) > 0)
        target = positive_components if positive else negative_components
        target.update(f"field:{value}" for value in fields)
        target.update(f"op:{value}" for value in operators)

    groups = defaultdict(list)
    for row in parsed:
        groups[(row["semantic_fingerprint"], row["shape_fingerprint"])].append(row)
    history = []
    for (semantic_fp, shape_fp), group in groups.items():
        independent = [row for row in group
                       if row["evidence_tier"] != "SCREENING_ONLY"]
        # Once an exact formula has an independent primary run, screening
        # evidence must never replace its promotion authority merely because the
        # selected sidecar happened to print a larger number.
        best_pool = independent or group
        best = max(best_pool, key=lambda row: (
            _number(row.get("oos_summary") or {}, "mean_net_bps_per_opportunity")
            if _number(row.get("oos_summary") or {}, "mean_net_bps_per_opportunity")
            is not None else float("-inf")))
        summary = best.get("oos_summary") or {}
        history.append({
            "ast_fingerprint": best["ast_fingerprint"],
            "semantic_fingerprint": semantic_fp, "shape_fingerprint": shape_fp,
            "event": best["plan"]["event"], "context": best["plan"]["context"],
            "qualities": best["plan"]["qualities"], "direction": best["plan"]["direction"],
            "execution": best["plan"]["execution"], "fields": best["fields"],
            "horizon_seconds": best["plan"]["horizon_seconds"],
            "niche": _niche(best["plan"]),
            "operators": best["operators"], "clocks_seconds": best["clocks"],
            "trials": len(group),
            "evidence_tiers": sorted({row["evidence_tier"] for row in group}),
            "best_evidence_tier": best["evidence_tier"],
            "decisions": sorted({str(row.get("decision") or "UNDECIDED") for row in group}),
            "lesson_codes": sorted({str(code) for row in group
                                    for code in (row.get("lesson_codes") or [])}),
            "best_gross_bps": _number(summary, "mean_mid_markout_bps"),
            "best_implementation_drag_bps": _number(
                summary, "mean_implementation_drag_bps"),
            "best_net_bps": _number(summary, "mean_net_bps_per_opportunity"),
            "best_fill_rate": _number(summary, "fill_rate"),
            "best_sessions": _number(summary, "sessions"),
            "score_calibration_status": str(
                (best.get("score_calibration") or {}).get("status") or
                "NOT_RECORDED"),
            "score_calibration_beta": _number(
                best.get("score_calibration") or {},
                "beta_bps_per_score_unit"),
            "score_calibration_observations": _number(
                best.get("score_calibration") or {}, "observations"),
        })
    history.sort(key=lambda row: (row["best_net_bps"] is not None,
                                  row["best_net_bps"] or float("-inf")), reverse=True)
    frequent_losers = _frequent_losing_subtrees(parsed)
    lead_rows = list(leads or ())
    term_bank = _reusable_term_bank(parsed, lead_rows)
    elites, recycled, candidate_population = _quality_diversity_frontier(
        lead_rows, parsed, {row["subtree_fingerprint"]
                            for row in frequent_losers})
    tournaments = _lineage_tournaments(lead_rows, parsed)
    arm_audit = _generation_arm_audit(lead_rows, parsed)
    # MAP-Elites needs stepping stones even before a globally profitable formula
    # exists.  The old archive bred only already-profitable gate survivors, so a
    # cold start with all-negative formulas had no parents and every cycle became
    # another unrelated one-shot idea.  Keep one measured elite per economic
    # niche, but label its permitted use honestly: cost-flipped gross predictors
    # may breed execution-aware children; negative-gross elites may only seed an
    # explicit failure-mode inversion.  Neither is promoted as alpha.
    by_tested_niche: dict[tuple, list[dict]] = defaultdict(list)
    for row in history:
        if row["best_net_bps"] is not None:
            by_tested_niche[row["niche"]].append(row)
    breeding_rows = []
    for niche, group in by_tested_niche.items():
        parent = max(group, key=lambda row: row["best_net_bps"])
        parent = dict(parent)
        if (parent["best_evidence_tier"] == "SCREENING_ONLY"
                and parent["best_net_bps"] > 0):
            parent["breeding_role"] = "SCREEN_SURVIVOR"
            parent["allowed_child_operators"] = [
                "STATE_CONDITION", "MECHANISM_INTERACTION",
                "CROSS_SCALE_DISAGREEMENT", "EXECUTION_AWARE"]
        elif (set(parent["decisions"]) & {"SUBMIT_TO_QA", "PROMOTED"}
                and parent["best_net_bps"] > 0):
            parent["breeding_role"] = "NET_SURVIVOR"
            parent["allowed_child_operators"] = [
                "STATE_CONDITION", "MECHANISM_INTERACTION",
                "CROSS_SCALE_DISAGREEMENT", "EXECUTION_AWARE"]
        elif ((parent["best_gross_bps"] or 0.0) > 0
              and parent["best_net_bps"] <= 0):
            parent["breeding_role"] = "COST_STEPPING_STONE"
            parent["allowed_child_operators"] = [
                "EXECUTION_AWARE", "STATE_CONDITION", "TARGET_CHANGE"]
        else:
            parent["breeding_role"] = "FAILURE_INVERSION_PARENT"
            parent["allowed_child_operators"] = ["FAILURE_MODE_INVERSION"]
        breeding_rows.append(parent)
    breeding = tuple(sorted(
        breeding_rows,
        key=lambda row: ({"NET_SURVIVOR": 0, "SCREEN_SURVIVOR": 1}.get(
                             row["breeding_role"], 2),
                         -row["best_net_bps"], row["niche"])))
    influence = []
    for row in parsed:
        measured = row.get("empirical_influence")
        if (row.get("candidate_role") != "STRUCTURAL_ABLATION"
                or not isinstance(measured, dict)):
            continue
        influence.append({
            "ablation_of_ast_fingerprint": str(
                measured.get("ablation_of_ast_fingerprint") or
                row.get("ablation_of_ast_fingerprint") or ""),
            "ablation_ast_fingerprint": row["ast_fingerprint"],
            "ablation_operator": str(measured.get("ablation_operator") or
                                      row.get("ablation_operator") or ""),
            "ablation_path": str(measured.get("ablation_path") or
                                  row.get("ablation_path") or ""),
            "net_increment_bps": _number(measured, "net_increment_bps"),
            "gross_increment_bps": _number(measured, "gross_increment_bps"),
            "implementation_drag_increment_bps": _number(
                measured, "implementation_drag_increment_bps"),
            "interpretation": str(measured.get("interpretation") or
                                  "NOT_MEASURED"),
        })
    influence.sort(key=lambda row: (
        row["net_increment_bps"] is not None,
        row["net_increment_bps"] or float("-inf")), reverse=True)
    return IntradayMemory(
        experiments=len(parsed), semantic_families=len({row["semantic_fingerprint"]
                                                        for row in parsed}),
        formula_shapes=len({row["shape_fingerprint"] for row in parsed}),
        history=tuple(history),
        underexplored_events=tuple(sorted(INTRADAY_EVENTS - used_events)),
        underexplored_contexts=tuple(sorted(CONTEXTS - used_contexts)),
        underexplored_qualities=tuple(sorted(QUALITIES - used_qualities)),
        positive_components=tuple(positive_components.most_common(8)),
        negative_components=tuple(negative_components.most_common(8)),
        candidate_population=candidate_population,
        population_shapes=len({row["shape_fingerprint"] for row in elites}),
        niche_elites=elites,
        recycled_candidates=recycled,
        lineage_tournaments=tournaments,
        breeding_parents=breeding,
        empirical_term_influence=tuple(influence),
        frequent_losing_subtrees=frequent_losers,
        reusable_term_bank=term_bank,
        generation_arm_audit=arm_audit,
    )


def render(memory: IntradayMemory, *, limit: int = 6) -> str:
    lines = [
        "", "[INTRADAY AST 경험 메모리 - 원장에서 매 주기 재계산]",
        f"  실험 {memory.experiments} / 의미 계열 {memory.semantic_families} / "
        f"수식 shape {memory.formula_shapes}",
        "  숫자 horizon만 바꾼 것은 새 아이디어가 아니다. 아래 실패는 영구 금지가 아니라 "
        "새 메커니즘·상태조건·실행모형이 필요한 비대칭 veto다.",
    ]
    if not memory.experiments:
        lines.append(
            "  아직 완주한 event-time 실험은 없다. 미평가 후보와 검증 결과를 혼동하지 않는다.")
    for row in memory.history[:limit]:
        lines.append(
            f"  {row['semantic_fingerprint']}/{row['shape_fingerprint']} "
            f"{row['event']} {row['direction']} context={row['context']} "
            f"quality={row['qualities']} fields={row['fields']} clocks={row['clocks_seconds']} "
            f"trials={row['trials']} decisions={row['decisions']} "
            f"evidence={row['evidence_tiers']} "
            f"gross={row['best_gross_bps']}bps "
            f"implementation_drag={row['best_implementation_drag_bps']}bps "
            f"net={row['best_net_bps']}bps fill={row['best_fill_rate']} "
            f"sessions={row['best_sessions']} "
            f"calibration={row['score_calibration_status']} "
            f"beta={row['score_calibration_beta']} "
            f"calibration_n={row['score_calibration_observations']} "
            f"lessons={row['lesson_codes']}")
    lines.extend([
        f"  positive-associated components (causal claim 아님): {list(memory.positive_components)}",
        f"  negative-associated components (재사용 시 교훈 대응 필수): {list(memory.negative_components)}",
        f"  underexplored events: {list(memory.underexplored_events)}",
        f"  underexplored contexts: {list(memory.underexplored_contexts)}",
        f"  underexplored qualities: {list(memory.underexplored_qualities)}",
    ])
    if memory.frequent_losing_subtrees:
        lines.append(
            "  [frequent losing subtrees - soft search prior, rejection rule 아님]")
        for row in memory.frequent_losing_subtrees[:6]:
            lines.append(
                f"    fp={row['subtree_fingerprint']} "
                f"losing/support={row['losing_support']}/{row['support']} "
                f"shape={row['shape_label'][:220]}")
        lines.append(
            "    그대로 반복하는 후보는 QD 점수에 작은 패널티를 받는다. 새 경제 "
            "메커니즘이 이 부분구조를 필요로 하면 제거한 실패모드를 명시하고 "
            "독립 실험으로 반증할 수 있다.")
    lines.append(
        f"  [candidate population] unused={memory.candidate_population} "
        f"elite_shapes={memory.population_shapes} niches={len(memory.niche_elites)}")
    for row in memory.niche_elites[:8]:
        lines.append(
            f"    QD={row['research_quality']:.3f} novelty={row['novelty_score']:.3f} "
            f"niche={'/'.join(row['niche'])} fp={row['ast_fingerprint']} "
            f"lead={row.get('lead_id')} fields={row['fields']} ops={row['operators']} "
            f"nodes={row['complexity_nodes']} lineage={row['lineage_complete']} "
            f"math_contract={row['formula_contract_complete']} "
            f"losing_subtrees={len(row['frequent_losing_subtree_overlap'])} "
            f"competitors={row['niche_competitors']}")
    if len(memory.niche_elites) > 8:
        lines.append(
            f"    ... {len(memory.niche_elites) - 8} additional niche elites remain "
            "in the deterministic archive (prompt display capped at 8)")
    if memory.recycled_candidates:
        lines.append("  [recycled exact formulas - do not spend another trial]")
        for row in memory.recycled_candidates[:5]:
            lines.append(
                f"    fp={row['ast_fingerprint']} lead={row.get('lead_id')} "
                f"niche={'/'.join(row['niche'])}")
    lines.append(
        f"  [breeding parents] measured niche elites={len(memory.breeding_parents)}; "
        "only NET_SURVIVOR is alpha-like; SCREEN_SURVIVOR still needs an "
        "independent primary run, and other roles are controlled stepping stones")
    for row in memory.breeding_parents[:5]:
        lines.append(
            f"    parent={row['ast_fingerprint']} role={row['breeding_role']} "
            f"gross={row['best_gross_bps']}bps net={row['best_net_bps']}bps "
            f"event={row['event']} context={row['context']} decisions={row['decisions']} "
            f"allowed_children={row['allowed_child_operators']}")
    if memory.lineage_tournaments:
        lines.append("  [parent-child net-increment tournaments]")
        for row in memory.lineage_tournaments[:8]:
            lines.append(
                f"    {row['parent_ast_fingerprint']}->{row['child_ast_fingerprint']} "
                f"status={row['status']} parent_net={row['parent_net_bps']} "
                f"child_net={row['child_net_bps']} increment={row['net_increment_bps']} "
                f"decision={row['child_decision']}")
    if memory.empirical_term_influence:
        lines.append(
            "  [same-replay empirical term influence - screening, causal claim 아님]")
        for row in memory.empirical_term_influence[:8]:
            lines.append(
                f"    parent={row['ablation_of_ast_fingerprint']} "
                f"control={row['ablation_ast_fingerprint']} "
                f"operator={row['ablation_operator']} path={row['ablation_path']} "
                f"primary-minus-control net={row['net_increment_bps']}bps "
                f"gross={row['gross_increment_bps']}bps "
                f"drag={row['implementation_drag_increment_bps']}bps "
                f"interpretation={row['interpretation']}")
    if memory.reusable_term_bank:
        lines.append(
            "  [typed reusable term bank - set-level search material, no causal credit]")
        candidates = [row for row in memory.reusable_term_bank
                      if row["status"] not in {
                          "RECURRING_UNTESTED", "REPEATED_PRIMARY_LOSER"}]
        # Prompt bandwidth is scarce.  Show a behaviorally broader frontier
        # instead of six clock variants of the same field family.
        display_candidates, seen_term_niches = [], set()
        for candidate in candidates:
            niche = (tuple(candidate["fields"]), candidate["unit"])
            if niche in seen_term_niches:
                continue
            seen_term_niches.add(niche)
            display_candidates.append(candidate)
            if len(display_candidates) == 6:
                break
        for row in display_candidates:
            lines.append(
                f"    term={row['term_fingerprint']} status={row['status']} "
                f"action={row['search_action']} "
                f"unit={row['unit']} nodes={row['nodes']} fields={row['fields']} "
                f"support={row['formula_support']} "
                f"primary+/-={row['primary_positive_support']}/"
                f"{row['primary_negative_support']} "
                f"screen+={row['screening_positive_support']} "
                f"proposals={row['proposal_support']} "
                f"ast={row['term_label'][:260]}")
        cautions = [row for row in memory.reusable_term_bank
                    if row["status"] in {
                        "RECURRING_UNTESTED", "REPEATED_PRIMARY_LOSER"}]
        if cautions:
            lines.append(
                "  [term saturation/avoidance queue - do not treat as preferred material]")
            for row in cautions[:4]:
                lines.append(
                    f"    term={row['term_fingerprint']} status={row['status']} "
                    f"action={row['search_action']} support={row['formula_support']} "
                    f"primary+/-={row['primary_positive_support']}/"
                    f"{row['primary_negative_support']} "
                    f"proposals={row['proposal_support']} "
                    f"ast={row['term_label'][:220]}")
        lines.append(
            "    Recombine compatible typed terms as a set, then state the joint economic "
            "mechanism and ablate the assembled set. Frequency is not individual term "
            "credit; REPEATED_PRIMARY_LOSER terms require an explicit failure inversion.")
    audit = memory.generation_arm_audit
    lines.append(
        f"  [fresh-vs-lineage process audit] status={audit['status']} "
        f"matched_n={audit['matched_net_sample_size']} "
        f"ambiguous={audit['ambiguous_role_formulas']} promotion_authority=false")
    for arm in ("FRESH", "LINEAGE"):
        row = audit["arms"][arm]
        lines.append(
            f"    {arm}: proposed={row['proposed_unique']} "
            f"evaluated={row['evaluated_unique']} measured={row['net_measured_unique']} "
            f"median_net={row['median_net_bps']} "
            f"positive_rate={row['positive_net_rate']} "
            f"gate_survivor_rate={row['gate_survivor_rate']}")
    lines.extend([
        "  [next generation protocol] Generate a population, not one favorite formula:",
        "    1) FRESH arm: 6 independent SEED drafts in underfilled economic niches; no parent;",
        "    2) LINEAGE arm: 2 local children, 2 mechanism crossovers, and 2 explicit",
        "       failure-mode inversions from admissible parents; target 12 drafts per cycle.",
        "    Keep the 6/6 LLM-call budget fixed so fresh sampling and evolution can be audited.",
        "    Never relabel the same exact AST across arms; ambiguous formulas are excluded.",
        "    Every child declares PARENT_SIGNAL_EXPR, EVOLUTION_OPERATORS, EXPECTED_INCREMENT,",
        "    ABLATIONS, and a typed FORMULA_THESIS connecting every field to an economic term.",
        "    Fresh SEED drafts declare no parent. Use the typed term bank as external material,",
        "    but select economically coherent term sets rather than trusting per-term scores.",
        "    Window/threshold-only edits are the same family, not novelty.",
        "    Submit contract-valid children together. The archive keeps one elite per niche;",
        "    causal backtests, costs, DSR/PBO and the trial ledger--not QD score--decide survival.",
        "    NET_SURVIVOR formulas may breed broadly. COST_STEPPING_STONE parents may only",
        "    breed execution/cost-aware children; FAILURE_INVERSION_PARENT may only breed an",
        "    explicit failure-mode inversion. These parent roles are search stepping stones,",
        "    never promotions. Missing net metrics never become zero or a win.",
        "    Use empirical term-influence point estimates diagnostically: retain mechanisms",
        "    whose primary-minus-control net increment is positive; simplify or replace",
        "    non-positive mechanisms. This same-replay screen is not causal proof and cannot",
        "    promote a formula without an independent primary experiment.",
    ])
    return "\n".join(lines)
