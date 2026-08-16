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


def build(rows: list[dict]) -> IntradayMemory:
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
        item = {**row, "plan": plan, "semantic_fingerprint": fingerprint(plan),
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
    )


def render(memory: IntradayMemory, *, limit: int = 6) -> str:
    if not memory.experiments:
        return "\n[INTRADAY AST 경험 메모리] 아직 완주한 event-time 실험이 없다."
    lines = [
        "", "[INTRADAY AST 경험 메모리 - 원장에서 매 주기 재계산]",
        f"  실험 {memory.experiments} / 의미 계열 {memory.semantic_families} / "
        f"수식 shape {memory.formula_shapes}",
        "  숫자 horizon만 바꾼 것은 새 아이디어가 아니다. 아래 실패는 영구 금지가 아니라 "
        "새 메커니즘·상태조건·실행모형이 필요한 비대칭 veto다.",
    ]
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
        "  다음 후보는 (a) 빈 의미 cell 탐색, (b) 실패 shape의 한 가지 메커니즘 편집, "
        "(c) 서로 다른 성공/실패 조각 재결합 중 하나를 명시하고 원 수식을 그대로 복제하지 않는다.",
    ])
    return "\n".join(lines)
