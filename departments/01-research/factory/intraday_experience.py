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
                                *, limit: int = 8) -> tuple[tuple[dict, ...],
                                                          tuple[dict, ...], int]:
    """Select one diverse, contract-complete elite per economic niche.

    No backtest result is invented for an unevaluated lead. Quality here means
    research-contract quality; empirical quality belongs to the causal backtest.
    """
    tested_fps = {row["ast_fingerprint"] for row in tested}
    references = [(row["expr"], row["plan"]) for row in tested]
    candidates, recycled = [], []
    for lead in leads:
        if lead.get("used") or lead.get("alpha_candidate_eligible") is False:
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
        row.update(
            nearest_library_similarity=nearest,
            novelty_score=1.0 - nearest,
            lineage_complete=lineage_complete,
            source_distance=(None if source_similarity is None
                             else 1.0 - source_similarity),
        )
        row["research_quality"] = (
            0.45 * row["novelty_score"]
            + 0.20 * float(lineage_complete)
            + 0.20 * (row["source_distance"]
                      if row["source_distance"] is not None else 0.5)
            + 0.15 * (1.0 - max(0, row["complexity_nodes"] - 10)
                      / max(1, formula.MAX_NODES - 10))
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
    return tuple(elites[:limit]), tuple(recycled), len(candidates) + len(recycled)


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
        item = {**row, "plan": plan, "expr": formula.parse(expr),
                "ast_fingerprint": formula.fingerprint(expr),
                "semantic_fingerprint": fingerprint(plan),
                "shape_fingerprint": _shape_fp(expr), "fields": sorted(fields),
                "operators": sorted(operators), "clocks": sorted(clocks)}
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
        best = max(group, key=lambda row: (
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
            "operators": best["operators"], "clocks_seconds": best["clocks"],
            "trials": len(group),
            "decisions": sorted({str(row.get("decision") or "UNDECIDED") for row in group}),
            "lesson_codes": sorted({str(code) for row in group
                                    for code in (row.get("lesson_codes") or [])}),
            "best_net_bps": _number(summary, "mean_net_bps_per_opportunity"),
            "best_fill_rate": _number(summary, "fill_rate"),
            "best_sessions": _number(summary, "sessions"),
        })
    history.sort(key=lambda row: (row["best_net_bps"] is not None,
                                  row["best_net_bps"] or float("-inf")), reverse=True)
    lead_rows = list(leads or ())
    elites, recycled, candidate_population = _quality_diversity_frontier(
        lead_rows, parsed)
    tournaments = _lineage_tournaments(lead_rows, parsed)
    breeding = tuple(row for row in history
                     if (set(row["decisions"]) & {"SUBMIT_TO_QA", "PROMOTED"}
                         and row["best_net_bps"] is not None
                         and row["best_net_bps"] > 0))
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
            f"net={row['best_net_bps']}bps fill={row['best_fill_rate']} "
            f"sessions={row['best_sessions']} lessons={row['lesson_codes']}")
    lines.extend([
        f"  positive-associated components (causal claim 아님): {list(memory.positive_components)}",
        f"  negative-associated components (재사용 시 교훈 대응 필수): {list(memory.negative_components)}",
        f"  underexplored events: {list(memory.underexplored_events)}",
        f"  underexplored contexts: {list(memory.underexplored_contexts)}",
        f"  underexplored qualities: {list(memory.underexplored_qualities)}",
    ])
    lines.append(
        f"  [candidate population] unused={memory.candidate_population} "
        f"elite_shapes={memory.population_shapes} niches={len(memory.niche_elites)}")
    for row in memory.niche_elites:
        lines.append(
            f"    QD={row['research_quality']:.3f} novelty={row['novelty_score']:.3f} "
            f"niche={'/'.join(row['niche'])} fp={row['ast_fingerprint']} "
            f"lead={row.get('lead_id')} fields={row['fields']} ops={row['operators']} "
            f"nodes={row['complexity_nodes']} lineage={row['lineage_complete']} "
            f"competitors={row['niche_competitors']}")
    if memory.recycled_candidates:
        lines.append("  [recycled exact formulas - do not spend another trial]")
        for row in memory.recycled_candidates[:5]:
            lines.append(
                f"    fp={row['ast_fingerprint']} lead={row.get('lead_id')} "
                f"niche={'/'.join(row['niche'])}")
    lines.append(f"  [breeding parents] gate-surviving, positive net={len(memory.breeding_parents)}")
    for row in memory.breeding_parents[:5]:
        lines.append(
            f"    parent={row['ast_fingerprint']} net={row['best_net_bps']}bps "
            f"event={row['event']} context={row['context']} decisions={row['decisions']}")
    if memory.lineage_tournaments:
        lines.append("  [parent-child net-increment tournaments]")
        for row in memory.lineage_tournaments[:8]:
            lines.append(
                f"    {row['parent_ast_fingerprint']}->{row['child_ast_fingerprint']} "
                f"status={row['status']} parent_net={row['parent_net_bps']} "
                f"child_net={row['child_net_bps']} increment={row['net_increment_bps']} "
                f"decision={row['child_decision']}")
    lines.extend([
        "  [next generation protocol] Generate a population, not one favorite formula:",
        "    1) 4 exploration children in underfilled Event/Context/Quality niches;",
        "    2) 4 local children of admissible parents, each changing one economic coordinate;",
        "    3) 2 mechanism crossovers and 2 failure-mode inversions; target 12 drafts per cycle.",
        "    Every child declares PARENT_SIGNAL_EXPR, EVOLUTION_OPERATORS, EXPECTED_INCREMENT,",
        "    and ABLATIONS. Window/threshold-only edits are the same family, not novelty.",
        "    Submit contract-valid children together. The archive keeps one elite per niche;",
        "    causal backtests, costs, DSR/PBO and the trial ledger--not QD score--decide survival.",
        "    Only gate-surviving positive-net formulas and CHILD_SURVIVES tournament winners breed;",
        "    missing net metrics, underpowered samples and gate holds never become zero or a win.",
    ])
    return "\n".join(lines)
