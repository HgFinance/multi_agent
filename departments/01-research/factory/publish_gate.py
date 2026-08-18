#!/usr/bin/env python3
"""발행 게이트 - 기획안이 퀀트로 나가기 전 마지막 결정론 검사.

담당: 재일 (리서치본부 RES)
계약: contracts/factory_contracts.py
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 6.1절 9단계(Publish Gate)
      docs/02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 10절 Quality Gate

▶ 이 게이트가 판정하는 것과 하지 않는 것
  판정한다: **답해야 할 질문에 답을 적었는가.** 반대편 주체, 경쟁 설명 코드,
            반증 검사, 회의론자 서명, 기각 이력 대응 - 전부 있는지 없는지다.
  하지 않는다: **그 답이 그럴듯한가.** 그건 실험이 판정한다. 여기서 의미 품질을
            판정하려 들면 LLM 이 LLM 을 심사하는 구조가 되고, 그러면 게이트가
            결정론이 아니게 된다.

▶ 왜 결정론이어야 하나
  이 게이트는 24시간 상주 루프의 출구다. 사람이 매번 보지 않는다. 판정이 모델
  기분에 따라 흔들리면 어떤 날은 통과하고 어떤 날은 막히는 기획안이 생기고,
  그러면 게이트가 있으나 마나다.

▶ 중복 실험 방지가 여기 있는 이유
  Gate 0(퀀트)이 같은 검사를 다시 한다. 두 번 하는 것이 낭비처럼 보이지만 아니다 -
  여기서 막으면 **기획안을 만드는 비용**을 아끼고, Gate 0 은 다른 경로로 들어온
  가설까지 막는다. 앞의 것은 절약, 뒤의 것은 방어다.

자체 점검: python departments/01-research/factory/publish_gate.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))

from factory_contracts import (  # noqa: E402
    CompetingExplanation,
    ExperimentProposalV1,
    LessonCode,
    MethodologyLeadV1,
    ResearchLane,
    Testability,
)
from alpha_ast_surface import (  # noqa: E402
    MICRO_FIELDS,
    fields_of as ast_fields_of,
    parse as parse_ast,
)

MODULE_VERSION = "research-publish-gate-v1"

# 경제적 근거가 이 표현만으로 이뤄지면 그것은 근거가 아니라 관찰이다.
# **의미 판정이 아니라 어휘 검사다** - "과거에 잘 됐다" 는 누가 잃어주는지 말하지 않는다.
_BACKTEST_ONLY_HINTS = (
    "과거에 잘", "백테스트에서 좋", "수익률이 높았", "성과가 좋았",
    "historically worked", "backtest showed", "good returns",
)

# 리드가 이 상태면 기획안의 근거로 쓰지 않는다.
_UNUSABLE_TESTABILITY = frozenset({Testability.UNUSABLE})


@dataclass
class GateResult:
    """통과 여부와 **무엇이 왜 막혔는지**. 이유 없는 거부는 재작성을 못 시킨다."""

    ok: bool = True
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def block(self, code: str, why: str) -> None:
        self.ok = False
        self.blockers.append(f"{code}: {why}")

    def warn(self, why: str) -> None:
        self.warnings.append(why)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blockers": list(self.blockers),
                "warnings": list(self.warnings)}


def check_rationale_names_a_loser(proposal: ExperimentProposalV1) -> tuple[bool, str]:
    """경제적 근거가 **반대편**을 지목하는가.

    counterparty 필드는 계약이 이미 non-blank 를 강제한다. 여기서는 근거 서술이
    성과 서술만으로 이뤄졌는지 본다 - 어휘 검사이지 의미 판정이 아니다.
    """
    text = f"{proposal.economic_rationale}".strip()
    hit = [h for h in _BACKTEST_ONLY_HINTS if h in text.lower() or h in text]
    if hit and len(text) < 60:
        return False, (
            f"경제적 근거가 성과 서술에 가깝다({hit[0]!r}) - 누가 반대편에서 "
            f"잃어주는지, 어떤 제약 때문에 엣지가 지속되는지를 적는다")
    return True, ""


def check_leads(proposal: ExperimentProposalV1,
                leads: dict[str, MethodologyLeadV1]) -> list[str]:
    """근거 리드가 실재하고 쓸 수 있는 상태인가."""
    out = []
    for lid in proposal.lead_ids:
        lead = leads.get(lid)
        if lead is None:
            out.append(f"근거 리드 {lid} 를 찾을 수 없다 - 끊어진 참조는 계보가 아니다")
            continue
        if lead.testability in _UNUSABLE_TESTABILITY:
            out.append(
                f"리드 {lid} 는 testability={lead.testability.value} 다 - "
                f"규칙으로 서술할 수 없는 주장을 기획안 근거로 쓰지 않는다")
        if (lead.ast_contract or {}).get("primary_data_plane") != "MICROSTRUCTURE":
            out.append(
                f"리드 {lid} 는 미시구조 우선 계약으로 검증되지 않았다 - "
                "짧은 호가·체결 표본을 일봉 대리변수로 바꾸지 않는다")
        if (lead.ast_contract or {}).get("alpha_candidate_eligible") is not True:
            mode = (lead.ast_contract or {}).get("derivation_mode") or "UNCLASSIFIED"
            out.append(
                f"리드 {lid} 는 공개 방법론 대조군이다(derivation_mode={mode}) - "
                "직접 복제나 창 조정은 알파 후보로 발행하지 않고, 메커니즘을 "
                "변형한 별도 AST 리드가 필요하다")
        if (proposal.research_lane == ResearchLane.INTRADAY_EVENT
                and ((lead.ast_contract or {}).get("formula_discovery_version")
                     != "formula-discovery-v5"
                     or not (lead.ast_contract or {}).get(
                         "formula_contract_complete"))):
            out.append(
                f"lead {lid} lacks the directional formula-discovery-v5 contract; "
                "resubmit a signed-pressure structure or identified BPS equation "
                "with an executable cost hurdle")
        elif proposal.research_lane == ResearchLane.INTRADAY_EVENT:
            contract = lead.ast_contract or {}
            try:
                from contracts import intraday_ast_contract as intraday_grammar
                import formula_discovery
                formula_discovery.assess(
                    contract.get("formula_thesis"),
                    candidate=contract.get("candidate_signal_expr"),
                    semantic_plan=contract.get("semantic_plan") or {},
                    grammar=intraday_grammar,
                )
            except (TypeError, ValueError) as exc:
                out.append(
                    f"lead {lid} fails the current formula influence audit: {exc}")
        if not lead.refs:
            out.append(f"리드 {lid} 에 출처가 없다")
    return out


def check_microstructure_primary(proposal: ExperimentProposalV1) -> list[str]:
    """Signal discovery is microstructure-first; daily data only executes/scores it."""
    out = []
    if proposal.research_lane == ResearchLane.INTRADAY_EVENT:
        # Intraday proposals intentionally use a different, seconds-based grammar.
        # Treating it as the daily ``signal_expr`` silently rejects the very raw-event
        # lane this gate is meant to protect.
        from intraday_ast_contract import fields_of, parse, unit_of

        raw = (proposal.suggested_params or {}).get("intraday_signal_expr")
        if not isinstance(raw, dict):
            return [
                "SUGGESTED_PARAMS.intraday_signal_expr is missing - an "
                "INTRADAY_EVENT proposal must preregister a seconds-based AST"
            ]
        try:
            parsed = parse(raw)
            fields = fields_of(parsed)
        except (TypeError, ValueError) as exc:
            return [f"intraday_signal_expr is not an executable AST: {exc}"]
        if not fields:
            out.append("intraday_signal_expr has no raw quote/trade field")
        output = str((proposal.semantic_plan or {}).get("output") or "").upper()
        if output in {"TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
            coefficient_policy = str((proposal.suggested_params or {}).get(
                "coefficient_policy") or "PREREGISTERED_NO_OOS_FIT").upper()
            if coefficient_policy not in {
                    "FIXED_FROM_SOURCE", "PREREGISTERED_NO_OOS_FIT",
                    "STRUCTURE_ONLY"}:
                out.append("intraday proposal has unsupported coefficient_policy")
            if coefficient_policy != "STRUCTURE_ONLY" and unit_of(parsed) != "BPS":
                out.append(
                    "fixed/preregistered net-PnL AST must predict markout in BPS")
            if coefficient_policy == "STRUCTURE_ONLY" and unit_of(parsed) == "BOOL":
                out.append("STRUCTURE_ONLY intraday AST must emit a numeric score")
            if str((proposal.suggested_params or {}).get(
                    "entry_policy") or "").upper() != \
                    "PREDICTED_MARKOUT_CLEARS_COST":
                out.append(
                    "net-PnL intraday proposal requires entry_policy="
                    "PREDICTED_MARKOUT_CLEARS_COST so predicted edge must clear "
                    "the live spread and round-trip charges")
        # ExperimentProposalV1 already requires both raw quote and trade tables.
        # microstructure_features is deliberately optional: derived features are
        # computed causally inside each bounded experiment slice.
        return out

    raw = (proposal.suggested_params or {}).get("signal_expr")
    if not isinstance(raw, dict):
        return [
            "SUGGESTED_PARAMS.signal_expr 가 없다 - 새 전략은 호가·체결 AST로 "
            "사전등록하고 일봉은 실행가격·벤치마크·레짐 보조로만 쓴다"]
    try:
        fields = ast_fields_of(parse_ast(raw))
    except (TypeError, ValueError) as exc:
        return [f"signal_expr가 실행 가능한 AST가 아니다: {exc}"]
    micro = sorted(fields & set(MICRO_FIELDS))
    if not micro:
        out.append(
            "signal_expr에 호가·체결 미시구조 필드가 없다 - close/notional/returns "
            "단독 신호는 이 공장의 핵심 탐색 대상으로 발행하지 않는다")
    tables = set(proposal.data_requirements.tables)
    if "microstructure_features" not in tables:
        out.append(
            "DATA_TABLES에 microstructure_features가 없다 - 짧은 표본도 직접 "
            "검증하며 market_bars는 체결·벤치마크 보조로 함께 둔다")
    return out


def check_intraday_screening_population(
        proposal: ExperimentProposalV1,
        leads: dict[str, MethodologyLeadV1]) -> list[str]:
    """Verify shared-replay sidecars against exact sourced v5 formulas."""
    if proposal.research_lane != ResearchLane.INTRADAY_EVENT:
        return []
    population = (proposal.suggested_params or {}).get(
        "screening_population") or []
    if not isinstance(population, list):
        return ["screening_population must be a JSON list"]
    if len(population) > 7:
        return ["screening_population exceeds the bounded seven-sidecar limit"]

    from alpha_semantics import validate as validate_plan
    from contracts import intraday_ast_contract as intraday_grammar
    from intraday_ast_contract import fingerprint, parse, unit_of
    from intraday_ablation import generate as generate_ablations
    import formula_discovery

    out: list[str] = []
    try:
        primary_fp = fingerprint(parse(
            (proposal.suggested_params or {}).get("intraday_signal_expr")))
    except (TypeError, ValueError):
        return []  # the primary formula gate reports the precise parse error
    seen = {primary_fp}
    for index, row in enumerate(population):
        prefix = f"screening_population[{index}]"
        if not isinstance(row, dict):
            out.append(f"{prefix} must be an object")
            continue
        try:
            expr = parse(row.get("intraday_signal_expr"))
            fp = fingerprint(expr)
            plan = validate_plan(row.get("semantic_plan") or {})
        except (TypeError, ValueError) as exc:
            out.append(f"{prefix} is not executable: {exc}")
            continue
        if fp in seen:
            out.append(f"{prefix} duplicates the primary or another sidecar")
        seen.add(fp)
        if str(row.get("ast_fingerprint") or "") != fp:
            out.append(f"{prefix}.ast_fingerprint does not match its AST")
        output = str(plan.get("output") or "").upper()
        if output in {"TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
            coefficient_policy = str(row.get("coefficient_policy") or "").upper()
            if coefficient_policy not in {
                    "FIXED_FROM_SOURCE", "PREREGISTERED_NO_OOS_FIT",
                    "STRUCTURE_ONLY"}:
                out.append(f"{prefix} has unsupported coefficient_policy")
            elif coefficient_policy != "STRUCTURE_ONLY" and unit_of(expr) != "BPS":
                out.append(f"{prefix} fixed net-PnL AST must output BPS")
            elif coefficient_policy == "STRUCTURE_ONLY" and unit_of(expr) == "BOOL":
                out.append(f"{prefix} STRUCTURE_ONLY AST must output a number")
            if str(row.get("entry_policy") or "").upper() != \
                    "PREDICTED_MARKOUT_CLEARS_COST":
                out.append(f"{prefix} lacks the executable cost hurdle")

        source_ids = [str(value) for value in row.get("source_lead_ids") or []]
        if not source_ids:
            out.append(f"{prefix} has no source_lead_ids")
            continue
        role = str(row.get("candidate_role") or "")
        sourced = False
        for lead_id in source_ids:
            lead = leads.get(lead_id)
            if lead is None:
                out.append(f"{prefix} source lead {lead_id} is unavailable")
                continue
            contract = lead.ast_contract or {}
            if (contract.get("formula_discovery_version") !=
                    "formula-discovery-v5"):
                out.append(f"{prefix} source lead {lead_id} is not v5")
                continue
            if (not contract.get("formula_contract_complete")
                    or contract.get("research_lane") != "INTRADAY_EVENT"):
                out.append(f"{prefix} source lead {lead_id} is not contract-complete")
                continue
            try:
                source_plan = validate_plan(contract.get("semantic_plan") or {})
                source_policy = str((contract.get("formula_thesis") or {}).get(
                    "decision_rule") or "")
                source_coefficient_policy = str(
                    (contract.get("formula_thesis") or {}).get(
                        "coefficient_policy") or "")
                formula_discovery.assess(
                    contract.get("formula_thesis"),
                    candidate=contract.get("candidate_signal_expr"),
                    semantic_plan=source_plan,
                    grammar=intraday_grammar,
                )
                if role == "LINKED_CANDIDATE":
                    source_expr = parse(contract.get("candidate_signal_expr"))
                    match = (contract.get("alpha_candidate_eligible") is True
                             and fingerprint(source_expr) == fp
                             and source_plan == plan
                             and source_policy == str(
                                 row.get("entry_policy") or "")
                             and source_coefficient_policy == str(
                                 row.get("coefficient_policy") or ""))
                elif role == "LINEAGE_PARENT":
                    source_expr = parse(contract.get("parent_signal_expr"))
                    match = (fingerprint(source_expr) == fp
                             and source_plan == plan
                             and source_policy == str(
                                 row.get("entry_policy") or "")
                             and source_coefficient_policy == str(
                                 row.get("coefficient_policy") or ""))
                elif role == "STRUCTURAL_ABLATION":
                    source_expr = parse(contract.get("candidate_signal_expr"))
                    source_fp = fingerprint(source_expr)
                    expected = {
                        candidate["ast_fingerprint"]: candidate
                        for candidate in generate_ablations(source_expr)
                    }.get(fp)
                    match = (
                        contract.get("alpha_candidate_eligible") is True
                        and source_fp == primary_fp
                        and row.get("ablation_of_ast_fingerprint") == source_fp
                        and expected is not None
                        and row.get("ablation_operator") == expected.get(
                            "ablation_operator")
                        and row.get("ablation_path") == expected.get(
                            "ablation_path")
                        and row.get("ablation_version") == expected.get(
                            "ablation_version")
                        and source_plan == plan
                        and source_policy == str(row.get("entry_policy") or "")
                        and source_coefficient_policy == str(
                            row.get("coefficient_policy") or ""))
                else:
                    out.append(f"{prefix} has unknown candidate_role={role!r}")
                    break
            except (TypeError, ValueError):
                match = False
            sourced = sourced or match
        if not sourced:
            out.append(f"{prefix} does not match its cited lead contract")
    return out


def check_prior_art(proposal: ExperimentProposalV1,
                    past_outcomes: list[dict]) -> list[str]:
    """같은 시도 계열의 기각 이력에 **대응이 있는가.**

    past_outcomes: [{outcome_id, decision, lesson_codes: [...]}, ...]
    같은 trial_family 에서 이미 기각된 교훈마다 대응이 있어야 한다. 없으면 그
    기획안은 회사가 이미 산 실험을 다시 사자는 제안이다.
    """
    out = []
    has_formula = bool(
        (proposal.suggested_params or {}).get("signal_expr") is not None
        or (proposal.suggested_params or {}).get("intraday_signal_expr") is not None
    )
    # A broad EDGE_TYPE/universe label is a prior, not formula identity.  Treating
    # every formula under e.g. order_flow_imbalance as one hard budget made a
    # never-tested cross-scale AST inherit five unrelated trials and die before
    # Quant could assign its formula-shaped trial family.  Exact AST history stays
    # a hard blocker; broad-family history is surfaced as a warning in evaluate().
    hard_outcomes = ([o for o in past_outcomes
                      if str(o.get("match_scope") or "") in
                      {"AST_EXACT", "AST_EXACT_PRIMARY", "FAMILY_PRIMARY"}]
                     if has_formula else list(past_outcomes))
    rejected = [o for o in hard_outcomes
                if str(o.get("decision")) in ("REJECT", "KILLED", "GATE_HOLD", "DEMOTED")]
    if not rejected:
        return out

    needed: set[str] = set()
    for o in rejected:
        for c in (o.get("lesson_codes") or []):
            needed.add(str(c))
    answered = set(proposal.prior_check.lessons_addressed)
    missing = sorted(needed - answered)
    if missing:
        out.append(
            f"같은 계열의 기각 교훈에 대응이 없다: {missing} - "
            f"대응 없는 재도전은 회사가 이미 산 실험을 다시 사는 것이다")

    # 예산은 Gate 0 이 최종 판정하지만, 여기서 미리 알려 기획 비용을 아낀다
    used = len([o for o in hard_outcomes
                if str(o.get("decision")) != "BLOCKED"])
    if used >= proposal.trial_budget:
        out.append(
            f"시도 예산 소진: 이 계열에서 이미 {used}회 시도했다"
            f"(예산 {proposal.trial_budget}) - 증액은 CEO 결정이 필요하다")
    return out


def evaluate(proposal: ExperimentProposalV1, *,
             leads: dict[str, MethodologyLeadV1] | None = None,
             past_outcomes: list[dict] | None = None) -> GateResult:
    """발행 게이트 판정. **결정론이다** - 같은 입력에 항상 같은 답."""
    r = GateResult()

    # ① 계약이 이미 강제하는 것들(반대편·경쟁설명·서명·반증·데이터)은 여기 오기 전에
    #    ExperimentProposalV1 생성에서 걸린다. 여기서는 계약이 못 보는 것만 본다.
    ok, why = check_rationale_names_a_loser(proposal)
    if not ok:
        r.block("RATIONALE_IS_PERFORMANCE_ONLY", why)

    # ② 경쟁 설명 코드가 실제 어휘인지(계약이 enum 으로 막지만 dict 경로 방어)
    for c in proposal.competing_explanation_codes:
        if c not in CompetingExplanation:
            r.block("UNKNOWN_COMPETING_CODE", f"경쟁 설명 코드가 어휘 밖이다: {c}")

    # ③ 근거 리드
    for why in check_leads(proposal, leads or {}):
        r.block("LEAD_UNUSABLE", why)

    # ④ 탐색 데이터 우선순위. 짧은 표본은 불확실성으로 판정하며 일봉으로
    #    대체하지 않는다.
    for why in check_microstructure_primary(proposal):
        r.block("MICROSTRUCTURE_PRIMARY_REQUIRED", why)

    for why in check_intraday_screening_population(proposal, leads or {}):
        r.block("SCREENING_POPULATION_INVALID", why)

    # ④ 기각 이력 대응 (중복 실험 방지)
    for why in check_prior_art(proposal, past_outcomes or []):
        code = "OVER_BUDGET" if "예산 소진" in why else "DUPLICATE_UNADDRESSED"
        r.block(code, why)

    if ((proposal.suggested_params or {}).get("signal_expr") is not None
            or (proposal.suggested_params or {}).get(
                "intraday_signal_expr") is not None):
        broad_negative = [o for o in (past_outcomes or [])
                          if str(o.get("match_scope") or "") == "EDGE_UNIVERSE"
                          and str(o.get("decision") or "") in
                          ("REJECT", "KILLED", "GATE_HOLD", "DEMOTED")]
        broad_lessons = sorted({str(code) for outcome in broad_negative
                                for code in (outcome.get("lesson_codes") or [])})
        if broad_lessons:
            r.warn("같은 edge/universe의 부정 선행 결과(새 수식의 시도 예산에는 "
                   f"합산하지 않음): {broad_lessons}")

    # ⑤ 경고: 막지는 않지만 퀀트가 알아야 하는 것
    if not proposal.source_reported_effect:
        r.warn("소스가 보고한 수치가 없다 - 우리 결과와 대조할 기준선이 없다")
    if proposal.prior_check.trial_family_id == "":
        r.warn("trial_family_id 가 비었다 - Gate 0 이 계열을 다시 계산한다")
    return r


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _mk_lead(lead_id=None, testability=Testability.RULE_EXPRESSIBLE):
    from datetime import datetime, timezone

    from factory_contracts import ScoutLens, SourceRef, SourceType, lead_id_for
    refs = (SourceRef(url="https://arxiv.org/abs/1234.5678", title="Momentum crashes",
                      accessed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                      excerpt="We document..."),)
    return MethodologyLeadV1(
        lead_id=lead_id or lead_id_for(list(refs)), case_id="rc_1",
        scout_lens=ScoutLens.ACADEMIC, source_type=SourceType.PAPER,
        as_known_at=datetime(2026, 8, 10, tzinfo=timezone.utc), refs=refs,
        ast_contract={"ast_readiness": "AST_READY",
                      "primary_data_plane": "MICROSTRUCTURE",
                      "daily_data_role": "EXECUTION_BENCHMARK_REGIME_AUXILIARY",
                      "derivation_mode": "MECHANISM_MUTATION",
                      "alpha_candidate_eligible": True},
        claimed_edge="모멘텀 붕괴는 변동성으로 예측된다", testability=testability)


def _mk_proposal(**kw):
    from datetime import datetime, timezone

    from factory_contracts import DataRequirement, PriorCheck
    lead = _mk_lead()
    base = dict(
        proposal_id="prop_1", case_id="rc_1",
        as_known_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        lead_ids=(lead.lead_id,),
        economic_rationale="레버리지 청산 물량이 반대편에서 강제로 판다. 그 압력이 "
                           "가라앉으면 가격이 되돌아온다",
        counterparty="레버리지 청산 물량",
        competing_explanation="단순 베타 노출일 수 있다",
        competing_explanation_codes=(CompetingExplanation.BETA_EXPOSURE,),
        skeptic_sign="worker_run_42",
        edge_type="liquidity_shock_reversal", universe_key="krx_all",
        falsification_tests=("하락장 초과수익이 0 미만이면 기각",),
        data_requirements=DataRequirement(
            tables=("market_bars", "microstructure_features"), min_history_days=58),
        suggested_params={"signal_expr": {
            "op": "ts_mean", "field": "order_flow_imbalance", "n": 3}},
        prior_check=PriorCheck(),
    )
    base.update(kw)
    return ExperimentProposalV1(**base), {lead.lead_id: lead}


def _with_current_intraday_contract(proposal, leads):
    """Make self-check leads obey the same complete contract as production rows."""
    from contracts import intraday_ast_contract as grammar

    expr = proposal.suggested_params["intraday_signal_expr"]
    fields = sorted(grammar.fields_of(expr))
    operators = grammar.operators_of(expr)
    form = "INTERACTION" if operators & {"mul", "div"} else "MONOTONE"
    thesis = {
        "target": proposal.semantic_plan["output"],
        "functional_form": form,
        "expected_sign": "STATE_DEPENDENT",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {field: ("PRESSURE" if "imbalance" in field
                           or "microprice" in field else "VOLATILITY")
                  for field in fields},
        "identification": (
            "The preregistered markout equation must remain positive after costs."),
    }
    return {lid: lead.model_copy(update={"ast_contract": {
        **lead.ast_contract,
        "formula_discovery_version": "formula-discovery-v5",
        "formula_contract_complete": True,
        "candidate_signal_expr": expr,
        "semantic_plan": proposal.semantic_plan,
        "formula_thesis": thesis,
    }}) for lid, lead in leads.items()}


def _check_clean_proposal_passes():
    p, leads = _mk_proposal()
    r = evaluate(p, leads=leads)
    assert r.ok, r.as_dict()


def _check_performance_only_rationale_is_blocked():
    """**"과거에 잘 됐다"는 경제적 근거가 아니다** - 누가 잃어주는지 말하지 않는다."""
    p, leads = _mk_proposal(economic_rationale="과거에 잘 됐다")
    r = evaluate(p, leads=leads)
    assert not r.ok and any("RATIONALE_IS_PERFORMANCE_ONLY" in b for b in r.blockers), r.as_dict()


def _check_missing_lead_is_blocked():
    """끊어진 참조는 계보가 아니다."""
    p, _ = _mk_proposal()
    r = evaluate(p, leads={})
    assert not r.ok and any("찾을 수 없다" in b for b in r.blockers), r.as_dict()


def _check_unusable_lead_is_blocked():
    """규칙으로 못 쓰는 주장을 기획안 근거로 쓰지 않는다."""
    lead = _mk_lead(testability=Testability.UNUSABLE)
    p, _ = _mk_proposal(lead_ids=(lead.lead_id,))
    r = evaluate(p, leads={lead.lead_id: lead})
    assert not r.ok and any("testability=UNUSABLE" in b for b in r.blockers), r.as_dict()


def _check_public_baseline_control_is_blocked():
    """공개식을 그대로 재현한 것은 기준선이지 신규 알파 후보가 아니다."""
    lead = _mk_lead()
    lead = lead.model_copy(update={"ast_contract": {
        **lead.ast_contract,
        "derivation_mode": "DIRECT_REPLICATION",
        "alpha_candidate_eligible": False,
    }})
    p, _ = _mk_proposal(lead_ids=(lead.lead_id,))
    r = evaluate(p, leads={lead.lead_id: lead})
    assert not r.ok
    assert any("공개 방법론 대조군" in b for b in r.blockers), r.as_dict()


def _check_daily_only_signal_is_blocked():
    p, leads = _mk_proposal(
        data_requirements={"tables": ["market_bars"], "min_history_days": 58},
        suggested_params={"signal_expr": {
            "op": "ts_mean", "field": "returns", "n": 5}})
    r = evaluate(p, leads=leads)
    assert not r.ok
    assert any("MICROSTRUCTURE_PRIMARY_REQUIRED" in b for b in r.blockers), r.as_dict()


def _check_intraday_ast_uses_intraday_contract():
    """The raw-event lane must not be parsed as a daily signal_expr."""
    from factory_contracts import DataRequirement

    p, leads = _mk_proposal(
        research_lane=ResearchLane.INTRADAY_EVENT,
        semantic_plan={
            "event": "QUOTE_IMBALANCE", "output": "TAKER_NET_PNL",
            "context": ["ALL"], "direction": "FOLLOW",
            "execution": "TAKER", "qualities": ["PERSISTENCE"],
            "horizon_seconds": 1,
        },
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"), min_history_days=10),
        suggested_params={"intraday_signal_expr": {
            "op": "mul", "args": [
                {"op": "rolling_mean",
                 "arg": {"op": "field", "field": "queue_imbalance_l1"},
                 "seconds": 5},
                {"op": "field", "field": "realized_volatility_bps"}],
        }, "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST"},
    )
    leads = _with_current_intraday_contract(p, leads)
    r = evaluate(p, leads=leads)
    assert r.ok, r.as_dict()


def _check_passive_intraday_uses_canonical_target_and_cost_hurdle():
    """Passive formulas use the same output vocabulary at every boundary."""
    from factory_contracts import DataRequirement

    params = {"intraday_signal_expr": {
        "op": "rolling_mean", "seconds": 5,
        "arg": {"op": "field", "field": "microprice_offset_bps"}},
        "execution": "PASSIVE_FIFO_LOWER_BOUND"}
    p, leads = _mk_proposal(
        research_lane=ResearchLane.INTRADAY_EVENT,
        semantic_plan={
            "event": "MICROPRICE_DISLOCATION",
            "output": "PASSIVE_FILL_ADJUSTED_PNL",
            "context": ["ALL"], "direction": "FOLLOW",
            "execution": "PASSIVE_FIFO_LOWER_BOUND",
            "qualities": ["PERSISTENCE"], "horizon_seconds": 1,
        },
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"), min_history_days=10),
        suggested_params=params,
    )
    leads = _with_current_intraday_contract(p, leads)
    blocked = evaluate(p, leads=leads)
    assert not blocked.ok
    assert any("PREDICTED_MARKOUT_CLEARS_COST" in reason
               for reason in blocked.blockers), \
        blocked.as_dict()

    executable = p.model_copy(update={"suggested_params": {
        **params, "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST"}})
    accepted = evaluate(executable, leads=leads)
    assert accepted.ok, accepted.as_dict()


def _check_unaddressed_rejection_is_blocked():
    """**회사가 이미 산 실험을 다시 사지 않는다.**"""
    p, leads = _mk_proposal()
    past = [{"outcome_id": "out_1", "decision": "REJECT",
             "lesson_codes": ["BEAR_FRAGILE", "OVERFIT_PBO"],
             "match_scope": "AST_EXACT"}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok
    assert any("DUPLICATE_UNADDRESSED" in b for b in r.blockers), r.as_dict()
    assert "BEAR_FRAGILE" in r.blockers[0] and "OVERFIT_PBO" in r.blockers[0]


def _check_addressed_rejection_passes():
    """대응을 적으면 재도전은 정상이다 - 막는 것이 목적이 아니라 배우게 하는 것이다."""
    from factory_contracts import PriorCheck
    p, leads = _mk_proposal(prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=1, past_outcomes=("out_1",),
        lessons_addressed={"BEAR_FRAGILE": "하락장 표본을 2창에서 5창으로 늘린다",
                           "OVERFIT_PBO": "변형 수를 줄이고 사전에 파라미터를 고정한다"}))
    past = [{"outcome_id": "out_1", "decision": "REJECT",
             "lesson_codes": ["BEAR_FRAGILE", "OVERFIT_PBO"],
             "match_scope": "AST_EXACT"}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert r.ok, r.as_dict()


def _check_budget_exhaustion_is_blocked():
    from factory_contracts import PriorCheck
    p, leads = _mk_proposal(trial_budget=2, prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=2, past_outcomes=("o1", "o2"),
        lessons_addressed={"OVERFIT_PBO": "파라미터 고정"}))
    past = [{"outcome_id": "o1", "decision": "REJECT",
             "lesson_codes": ["OVERFIT_PBO"], "match_scope": "AST_EXACT"},
            {"outcome_id": "o2", "decision": "REVISE", "lesson_codes": [],
             "match_scope": "AST_EXACT"}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok and any("OVER_BUDGET" in b for b in r.blockers), r.as_dict()


def _check_broad_family_does_not_spend_new_formula_budget():
    """A new AST gets its own trial budget even under a familiar edge label."""
    p, leads = _mk_proposal(trial_budget=1)
    broad = [{"decision": "GATE_HOLD", "lesson_codes": ["BASELINE_NOT_BEATEN"],
              "match_scope": "EDGE_UNIVERSE"} for _ in range(5)]
    r = evaluate(p, leads=leads, past_outcomes=broad)
    assert r.ok, r.as_dict()
    assert any("새 수식의 시도 예산에는 합산하지 않음" in warning
               for warning in r.warnings), r.as_dict()


def _check_success_history_does_not_block():
    """성공한 이력은 재도전을 막지 않는다 - 막는 것은 기각 교훈 미대응이다."""
    p, leads = _mk_proposal()
    past = [{"outcome_id": "o1", "decision": "PROMOTED", "lesson_codes": []}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert r.ok, r.as_dict()


def _check_deterministic():
    """상주 루프의 출구다 - 같은 입력에 항상 같은 답이어야 한다."""
    p, leads = _mk_proposal()
    past = [{"outcome_id": "o1", "decision": "REJECT", "lesson_codes": ["COST_SENSITIVE"]}]
    a = evaluate(p, leads=leads, past_outcomes=past).as_dict()
    b = evaluate(p, leads=leads, past_outcomes=past).as_dict()
    assert a == b


def _check_warnings_do_not_block():
    """경고는 막지 않는다 - 막을 것과 알릴 것을 섞으면 게이트가 소음이 된다."""
    p, leads = _mk_proposal()
    r = evaluate(p, leads=leads)
    assert r.ok and r.warnings, r.as_dict()
    assert any("소스가 보고한 수치가 없다" in w for w in r.warnings)


def _check_lesson_vocabulary_is_shared():
    """게이트가 대조하는 교훈 코드는 계약의 통제 어휘와 같은 집합이어야 한다."""
    p, leads = _mk_proposal()
    past = [{"outcome_id": "o1", "decision": "REJECT",
             "lesson_codes": [c.value for c in LessonCode],
             "match_scope": "AST_EXACT"}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok
    for c in LessonCode:
        assert c.value in r.blockers[0], c


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    _check_passive_intraday_uses_canonical_target_and_cost_hurdle()
    print("  passive target/cost contract OK")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_clean_proposal_passes();             print("  정상 기획안 통과         OK")
    _check_performance_only_rationale_is_blocked(); print("  성과 서술만 = 거부      OK")
    _check_missing_lead_is_blocked();           print("  끊어진 리드 참조 거부    OK")
    _check_unusable_lead_is_blocked();          print("  UNUSABLE 리드 거부       OK")
    _check_public_baseline_control_is_blocked(); print("  공개식 대조군 발행 차단  OK")
    _check_daily_only_signal_is_blocked();      print("  일봉 단독 신호 거부      OK")
    _check_intraday_ast_uses_intraday_contract(); print("  인트라데이 AST 계약 분리  OK")
    _check_unaddressed_rejection_is_blocked();  print("  기각 교훈 미대응 거부    OK")
    _check_addressed_rejection_passes();        print("  대응하면 재도전 허용     OK")
    _check_budget_exhaustion_is_blocked();      print("  예산 소진 차단           OK")
    _check_broad_family_does_not_spend_new_formula_budget(); print("  새 수식 독립 예산        OK")
    _check_success_history_does_not_block();    print("  성공 이력은 안 막음      OK")
    _check_deterministic();                     print("  결정론                   OK")
    _check_warnings_do_not_block();             print("  경고 != 차단             OK")
    _check_lesson_vocabulary_is_shared();       print("  교훈 어휘 공유           OK")
    print("발행 게이트 13개 영역 통과.")
