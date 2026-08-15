"""Outcome-conditioned memory for the formulaic-alpha search loop.

The LLM does not own this memory.  Deterministic code derives it from executable ASTs
and canonical experiment outcomes, then gives the compact result to the scout and
planner.  This keeps "remembering" distinct from asking an agent to summarize itself.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

import alpha_ast_surface as ast  # noqa: E402

NEGATIVE_DECISIONS = frozenset({"REJECT", "GATE_HOLD", "KILLED", "DEMOTED"})


@dataclass(frozen=True)
class AstMemory:
    experiments: int
    unique_formulas: int
    unique_shapes: int
    duplicate_trials: int
    field_counts: dict[str, int]
    untested_micro_fields: tuple[str, ...]
    formula_history: tuple[dict, ...]
    public_baseline_controls: tuple[dict, ...]
    unused_novel_leads: tuple[dict, ...]
    unused_recycled_leads: tuple[dict, ...]


def _metric(summary: dict, key: str) -> float | None:
    try:
        value = summary.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError, AttributeError):
        return None


def build(experiments: list[dict], leads: list[dict]) -> AstMemory:
    """Build exact/near-duplicate and outcome memory from already loaded rows."""
    parsed_experiments: list[dict] = []
    fields = Counter()
    for row in experiments:
        try:
            expr = ast.parse(row.get("signal_expr"))
        except (TypeError, ValueError):
            continue
        item = dict(row)
        item.update(expr=expr, fingerprint=ast.fingerprint(expr),
                    shape_fingerprint=ast.shape_fingerprint(expr))
        parsed_experiments.append(item)
        fields.update(ast.fields_of(expr))

    by_fp: dict[str, list[dict]] = defaultdict(list)
    for row in parsed_experiments:
        by_fp[row["fingerprint"]].append(row)

    history = []
    for fp, rows in by_fp.items():
        lesson_codes = sorted({str(code) for row in rows
                               for code in (row.get("lesson_codes") or [])})
        negative = sum(str(row.get("decision") or "").upper() in NEGATIVE_DECISIONS
                       for row in rows)
        scored = sorted(
            rows,
            key=lambda row: (_metric(row.get("oos_summary") or {}, "signal_ic_t")
                             is not None,
                             _metric(row.get("oos_summary") or {}, "signal_ic_t")
                             or float("-inf")),
            reverse=True,
        )
        best = scored[0]
        summary = best.get("oos_summary") or {}
        history.append({
            "fingerprint": fp,
            "shape_fingerprint": best["shape_fingerprint"],
            "expr": best["expr"],
            "fields": sorted(ast.fields_of(best["expr"])),
            "trials": len(rows),
            "negative_trials": negative,
            "decisions": sorted({str(row.get("decision") or "UNDECIDED")
                                  for row in rows}),
            "lesson_codes": lesson_codes,
            "best_signal_ic": _metric(summary, "signal_ic"),
            "best_signal_ic_t": _metric(summary, "signal_ic_t"),
            "best_excess_return_pct": _metric(summary, "excess_return_pct"),
            "title": str(best.get("title") or "")[:80],
        })
    history.sort(key=lambda row: (
        row["best_signal_ic_t"] is not None,
        row["best_signal_ic_t"] or float("-inf"),
    ), reverse=True)

    tested = set(by_fp)
    tested_exprs = [row["expr"] for row in parsed_experiments]
    lead_groups: dict[str, dict] = {}
    baseline_groups: dict[str, dict] = {}
    for row in leads:
        if row.get("used"):
            continue
        eligible = row.get("alpha_candidate_eligible", True)
        if eligible is False:
            raw_baseline = row.get("source_baseline_expr") or row.get("signal_expr")
            try:
                baseline = ast.parse(raw_baseline)
            except (TypeError, ValueError):
                continue
            baseline_fp = ast.fingerprint(baseline)
            group = baseline_groups.setdefault(baseline_fp, {
                "fingerprint": baseline_fp,
                "shape_fingerprint": ast.shape_fingerprint(baseline),
                "expr": baseline,
                "lead_ids": [],
                "titles": [],
                "fields": sorted(ast.fields_of(baseline)),
            })
            group["lead_ids"].append(str(row.get("lead_id") or ""))
            group["titles"].append(str(row.get("title") or "")[:80])
            continue
        try:
            expr = ast.parse(row.get("signal_expr"))
        except (TypeError, ValueError):
            continue
        fp = ast.fingerprint(expr)
        group = lead_groups.setdefault(fp, {
            "fingerprint": fp,
            "shape_fingerprint": ast.shape_fingerprint(expr),
            "expr": expr,
            "lead_ids": [],
            "titles": [],
            "fields": sorted(ast.fields_of(expr)),
            "nearest_tested_similarity": 0.0,
        })
        group["lead_ids"].append(str(row.get("lead_id") or ""))
        group["titles"].append(str(row.get("title") or "")[:80])
        if tested_exprs:
            group["nearest_tested_similarity"] = max(
                ast.structural_similarity(expr, prior) for prior in tested_exprs)

    novel = tuple(group for fp, group in sorted(lead_groups.items()) if fp not in tested)
    recycled = tuple(group for fp, group in sorted(lead_groups.items()) if fp in tested)
    shapes = {row["shape_fingerprint"] for row in parsed_experiments}
    untested = tuple(field for field in ast.MICRO_FIELDS if not fields[field])
    return AstMemory(
        experiments=len(parsed_experiments),
        unique_formulas=len(by_fp),
        unique_shapes=len(shapes),
        duplicate_trials=max(0, len(parsed_experiments) - len(by_fp)),
        field_counts=dict(fields),
        untested_micro_fields=untested,
        formula_history=tuple(history),
        public_baseline_controls=tuple(
            group for _, group in sorted(baseline_groups.items())),
        unused_novel_leads=novel,
        unused_recycled_leads=recycled,
    )


def render(memory: AstMemory, *, max_history: int = 5) -> str:
    """Compact prompt block: facts and search directions, never invented conclusions."""
    lines = [
        "",
        "[AST 경험 메모리 - 원장의 수식·결과에서 매 주기 다시 계산]",
        f"  실험 {memory.experiments}건 · 고유 수식 {memory.unique_formulas}개 · "
        f"고유 구조 {memory.unique_shapes}개 · exact 중복 시도 {memory.duplicate_trials}건",
        "  ▶ 수식 이름이 아니라 지문으로 센다. 창만 바꾼 구조도 별도로 표시한다.",
    ]
    positive = [row for row in memory.formula_history
                if ((row["best_signal_ic_t"] or 0) > 0
                    or (row["best_excess_return_pct"] or 0) > 0)]
    if positive:
        lines.append("  [positive retrieval - 통계적 근접 신호, **검증 알파 아님**]")
        for row in positive[:max_history]:
            metrics = []
            if row["best_signal_ic"] is not None:
                metrics.append(f"IC {row['best_signal_ic']:+.4f}")
            if row["best_signal_ic_t"] is not None:
                metrics.append(f"t {row['best_signal_ic_t']:+.2f}")
            if row["best_excess_return_pct"] is not None:
                metrics.append(f"초과 {row['best_excess_return_pct']:+.2f}%p")
            lines.append(
                f"    {row['fingerprint']} fields={row['fields']} trials={row['trials']} "
                f"decision={row['decisions']} {' · '.join(metrics) or '성과 미측정'}")
    negative = [row for row in memory.formula_history
                if row["negative_trials"] and row["lesson_codes"]]
    if negative:
        lines.append("  [negative retrieval - 재사용 전에 반드시 제거할 실패 제약]")
        for row in negative[:max_history]:
            lines.append(
                f"    {row['fingerprint']} negative_trials={row['negative_trials']} "
                f"교훈={row['lesson_codes']}")
    if memory.public_baseline_controls:
        lines.append("  [공개 기준선 대조군 - 그대로 재제안 금지, 파생 출발점]")
        for row in memory.public_baseline_controls[:5]:
            lines.append(
                f"    {row['fingerprint']} leads={row['lead_ids']} fields={row['fields']}")
        lines.append(
            "  ▶ 창·상수만 바꾸지 말고 상태 조건, 메커니즘 상호작용, 실패모드 "
            "역전 또는 타분야 이전으로 별도 AST shape를 만든다.")
    if memory.unused_novel_leads:
        lines.append("  [미사용·exact 미실험 리드 - 먼저 검토]")
        for row in memory.unused_novel_leads[:5]:
            lines.append(
                f"    {row['fingerprint']} leads={row['lead_ids']} fields={row['fields']} "
                f"최근접 구조유사도={row['nearest_tested_similarity']:.2f}")
    if memory.unused_recycled_leads:
        lines.append("  [미사용 리드지만 수식은 이미 실험됨 - 독립 근거로 합치고 재실험 금지]")
        for row in memory.unused_recycled_leads[:5]:
            lines.append(f"    {row['fingerprint']} leads={row['lead_ids']}")
    if memory.untested_micro_fields:
        lines.extend([
            "  [문헌 검색 우선 표적 - 아직 어떤 AST 실험도 읽지 않은 미시구조 필드]",
            "    " + ", ".join(memory.untested_micro_fields),
            "  ▶ 이 필드와 **직접 대응하는 경제 메커니즘**을 먼저 검색하라. "
            "논문을 찾은 뒤 수식을 만들며, 빈 칸을 채우려고 무관한 근거를 끼우지 마라.",
        ])
    lines.extend([
        "  ▶ exact 지문이 과거 부정 종결과 같으면 이름·EDGE_TYPE을 바꿔 우회하지 마라.",
        "    재시도하려면 그 지문의 교훈 각각에 무엇이 달라졌는지 LESSONS_ADDRESSED에 적는다.",
        "  ▶ 구조 유사도는 검색 방향 신호일 뿐 성과 대리값이 아니다. 최종 판정은 PIT "
        "walk-forward·비용·다중검정 관문이 한다.",
    ])
    return "\n".join(lines)


def _selftest() -> None:
    ofi1 = {"op": "rank", "arg": {"op": "ts_mean",
                                     "field": "order_flow_imbalance", "n": 1}}
    ofi5 = {"op": "rank", "arg": {"op": "ts_mean",
                                     "field": "order_flow_imbalance", "n": 5}}
    spread = {"op": "rank", "arg": {"op": "ts_mean",
                                       "field": "spread_bps", "n": 5}}
    m = build([
        {"signal_expr": ofi1, "decision": "GATE_HOLD",
         "lesson_codes": ["UNDERPOWERED_DATA"],
         "oos_summary": {"signal_ic": .02, "signal_ic_t": 1.1}},
        {"signal_expr": ofi1, "decision": "GATE_HOLD", "lesson_codes": []},
    ], [
        {"lead_id": "l1", "signal_expr": ofi5, "used": False},
        {"lead_id": "l2", "signal_expr": ofi1, "used": False},
        {"lead_id": "l3", "signal_expr": spread, "used": True},
        {"lead_id": "l4", "signal_expr": spread, "used": False,
         "alpha_candidate_eligible": False, "source_baseline_expr": spread},
    ])
    assert (m.experiments, m.unique_formulas, m.duplicate_trials) == (2, 1, 1)
    assert len(m.unused_novel_leads) == 1 and len(m.unused_recycled_leads) == 1
    assert len(m.public_baseline_controls) == 1
    assert m.unused_novel_leads[0]["nearest_tested_similarity"] == 1.0
    assert "depth_imbalance" in m.untested_micro_fields
    assert "exact 중복 시도 1건" in render(m)


if __name__ == "__main__":
    _selftest()
    print("ast_experience: 6 areas passed")
