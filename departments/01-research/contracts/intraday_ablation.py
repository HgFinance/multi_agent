"""Deterministic, unit-safe structural controls for intraday alpha ASTs.

The controls answer a deliberately narrow question: did an economic mechanism
inside the preregistered formula add value on the *same* event replay?  They are
screening evidence, never independently preregistered alpha candidates.
"""

from __future__ import annotations

from copy import deepcopy

try:  # package import from publish gate
    from . import intraday_ast_contract as grammar
except ImportError:  # direct self-check with this directory on sys.path
    import intraday_ast_contract as grammar


ABLATION_VERSION = "intraday-structural-ablation-v1"
# The cohort version is a cross-department execution contract, not merely a
# proposal-intake implementation detail.  Quant imports the same value and
# refuses stale populated cohorts before they consume a full-universe replay.
INTRADAY_SCREENING_COHORT_VERSION = "intraday-screening-cohort-v3"


def _is_zero(node: dict) -> bool:
    return "const" in node and float(node["const"]) == 0.0


def _local_replacements(node: dict) -> list[tuple[str, dict]]:
    """Return simpler local expressions in economic-diagnostic order."""
    op = node.get("op")
    if op == "where":
        out = [("REMOVE_STATE_GATE_KEEP_THEN", node["then"])]
        if not _is_zero(node["else"]):
            out.append(("REMOVE_STATE_GATE_KEEP_ELSE", node["else"]))
        return out
    if op == "mul":
        left, right = node["args"]
        left_unit, right_unit = grammar.unit_of(left), grammar.unit_of(right)
        if left_unit == grammar.RATIO and right_unit != grammar.RATIO:
            return [("REMOVE_RATIO_MODULATOR_LEFT", right)]
        if right_unit == grammar.RATIO and left_unit != grammar.RATIO:
            return [("REMOVE_RATIO_MODULATOR_RIGHT", left)]
        return []
    if op == "div" and grammar.unit_of(node["args"][1]) == grammar.RATIO:
        return [("REMOVE_RATIO_DENOMINATOR", node["args"][0])]
    if op in {"add", "min", "max"}:
        left, right = node["args"]
        return [("DROP_RIGHT_TERM", left), ("DROP_LEFT_TERM", right)]
    if op == "sub":
        left, right = node["args"]
        return [
            ("DROP_RIGHT_TERM", left),
            ("DROP_LEFT_TERM", {"op": "neg", "arg": right}),
        ]
    if op in {"lag", "rolling_mean", "rolling_std", "rolling_sum", "delta", "ewma"}:
        return [("REMOVE_TEMPORAL_TRANSFORM", node["arg"])]
    return []


def _children(node: dict) -> list[tuple[str, dict]]:
    if "arg" in node:
        return [("arg", node["arg"])]
    if "args" in node:
        return [(f"args.{index}", child)
                for index, child in enumerate(node["args"])]
    if node.get("op") == "where":
        return [(key, node[key]) for key in ("condition", "then", "else")]
    return []


def _replace(root: dict, path: tuple[str, ...], replacement: dict) -> dict:
    if not path:
        return deepcopy(replacement)
    out = deepcopy(root)
    target = out
    for token in path[:-1]:
        if token.startswith("args."):
            target = target["args"][int(token.split(".", 1)[1])]
        else:
            target = target[token]
    final = path[-1]
    if final.startswith("args."):
        target["args"][int(final.split(".", 1)[1])] = deepcopy(replacement)
    else:
        target[final] = deepcopy(replacement)
    return out


def generate(expr: dict) -> list[dict]:
    """Generate unique one-edit controls with the source unit preserved."""
    source = grammar.parse(expr)
    source_unit = grammar.unit_of(source)
    if source_unit == grammar.BOOL:
        return []
    source_fp = grammar.fingerprint(source)
    source_nodes = grammar.count_nodes(source)
    proposals: list[tuple[int, str, str, dict]] = []

    def walk(node: dict, path: tuple[str, ...]) -> None:
        for operator, replacement in _local_replacements(node):
            proposals.append((len(path), ".".join(path) or "ROOT",
                              operator, _replace(source, path, replacement)))
        for token, child in _children(node):
            walk(child, (*path, token))

    walk(source, ())
    out, seen = [], {source_fp}
    for _, path, operator, raw in sorted(
            proposals, key=lambda row: (row[0], row[2], row[1])):
        try:
            candidate = grammar.parse(raw)
            fp = grammar.fingerprint(candidate)
            if (grammar.unit_of(candidate) != source_unit
                    or grammar.count_nodes(candidate) >= source_nodes
                    or fp in seen):
                continue
        except (TypeError, ValueError):
            continue
        seen.add(fp)
        out.append({
            "intraday_signal_expr": candidate,
            "ast_fingerprint": fp,
            "ablation_operator": operator,
            "ablation_path": path,
            "ablation_of_ast_fingerprint": source_fp,
            "ablation_version": ABLATION_VERSION,
        })
    return out


if __name__ == "__main__":
    source = {
        "op": "where",
        "condition": {"op": "gt", "args": [
            {"op": "field", "field": "spread_bps"},
            {"const": 5, "unit": "BPS"},
        ]},
        "then": {"op": "mul", "args": [
            {"op": "field", "field": "microprice_offset_bps"},
            {"op": "field", "field": "queue_imbalance_l1"},
        ]},
        "else": {"const": 0, "unit": "BPS"},
    }
    controls = generate(source)
    assert controls and controls == generate(source)
    assert controls[0]["ablation_operator"] == "REMOVE_STATE_GATE_KEEP_THEN"
    assert all(grammar.unit_of(row["intraday_signal_expr"]) == grammar.BPS
               for row in controls)
    assert all(grammar.count_nodes(row["intraday_signal_expr"])
               < grammar.count_nodes(source) for row in controls)
    print(f"{ABLATION_VERSION} self-check OK ({len(controls)} controls)")
