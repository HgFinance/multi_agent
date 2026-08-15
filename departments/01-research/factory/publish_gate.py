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

from contracts.factory_contracts import (  # noqa: E402
    CompetingExplanation,
    ExperimentProposalV1,
    LessonCode,
    MethodologyLeadV1,
    Testability,
)
from contracts.alpha_ast_surface import (  # noqa: E402
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
        if not lead.refs:
            out.append(f"리드 {lid} 에 출처가 없다")
    return out


def check_microstructure_primary(proposal: ExperimentProposalV1) -> list[str]:
    """Signal discovery is microstructure-first; daily data only executes/scores it."""
    out = []
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


def check_prior_art(proposal: ExperimentProposalV1,
                    past_outcomes: list[dict]) -> list[str]:
    """같은 시도 계열의 기각 이력에 **대응이 있는가.**

    past_outcomes: [{outcome_id, decision, lesson_codes: [...]}, ...]
    같은 trial_family 에서 이미 기각된 교훈마다 대응이 있어야 한다. 없으면 그
    기획안은 회사가 이미 산 실험을 다시 사자는 제안이다.
    """
    out = []
    rejected = [o for o in past_outcomes
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
    used = len([o for o in past_outcomes if str(o.get("decision")) != "BLOCKED"])
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

    # ④ 기각 이력 대응 (중복 실험 방지)
    for why in check_prior_art(proposal, past_outcomes or []):
        code = "OVER_BUDGET" if "예산 소진" in why else "DUPLICATE_UNADDRESSED"
        r.block(code, why)

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

    from contracts.factory_contracts import ScoutLens, SourceRef, SourceType, lead_id_for
    refs = (SourceRef(url="https://arxiv.org/abs/1234.5678", title="Momentum crashes",
                      accessed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                      excerpt="We document..."),)
    return MethodologyLeadV1(
        lead_id=lead_id or lead_id_for(list(refs)), case_id="rc_1",
        scout_lens=ScoutLens.ACADEMIC, source_type=SourceType.PAPER,
        as_known_at=datetime(2026, 8, 10, tzinfo=timezone.utc), refs=refs,
        ast_contract={"ast_readiness": "AST_READY",
                      "primary_data_plane": "MICROSTRUCTURE",
                      "daily_data_role": "EXECUTION_BENCHMARK_REGIME_AUXILIARY"},
        claimed_edge="모멘텀 붕괴는 변동성으로 예측된다", testability=testability)


def _mk_proposal(**kw):
    from datetime import datetime, timezone

    from contracts.factory_contracts import DataRequirement, PriorCheck
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


def _check_daily_only_signal_is_blocked():
    p, leads = _mk_proposal(
        data_requirements={"tables": ["market_bars"], "min_history_days": 58},
        suggested_params={"signal_expr": {
            "op": "ts_mean", "field": "returns", "n": 5}})
    r = evaluate(p, leads=leads)
    assert not r.ok
    assert any("MICROSTRUCTURE_PRIMARY_REQUIRED" in b for b in r.blockers), r.as_dict()


def _check_unaddressed_rejection_is_blocked():
    """**회사가 이미 산 실험을 다시 사지 않는다.**"""
    p, leads = _mk_proposal()
    past = [{"outcome_id": "out_1", "decision": "REJECT",
             "lesson_codes": ["BEAR_FRAGILE", "OVERFIT_PBO"]}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok
    assert any("DUPLICATE_UNADDRESSED" in b for b in r.blockers), r.as_dict()
    assert "BEAR_FRAGILE" in r.blockers[0] and "OVERFIT_PBO" in r.blockers[0]


def _check_addressed_rejection_passes():
    """대응을 적으면 재도전은 정상이다 - 막는 것이 목적이 아니라 배우게 하는 것이다."""
    from contracts.factory_contracts import PriorCheck
    p, leads = _mk_proposal(prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=1, past_outcomes=("out_1",),
        lessons_addressed={"BEAR_FRAGILE": "하락장 표본을 2창에서 5창으로 늘린다",
                           "OVERFIT_PBO": "변형 수를 줄이고 사전에 파라미터를 고정한다"}))
    past = [{"outcome_id": "out_1", "decision": "REJECT",
             "lesson_codes": ["BEAR_FRAGILE", "OVERFIT_PBO"]}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert r.ok, r.as_dict()


def _check_budget_exhaustion_is_blocked():
    from contracts.factory_contracts import PriorCheck
    p, leads = _mk_proposal(trial_budget=2, prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=2, past_outcomes=("o1", "o2"),
        lessons_addressed={"OVERFIT_PBO": "파라미터 고정"}))
    past = [{"outcome_id": "o1", "decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]},
            {"outcome_id": "o2", "decision": "REVISE", "lesson_codes": []}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok and any("OVER_BUDGET" in b for b in r.blockers), r.as_dict()


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
             "lesson_codes": [c.value for c in LessonCode]}]
    r = evaluate(p, leads=leads, past_outcomes=past)
    assert not r.ok
    for c in LessonCode:
        assert c.value in r.blockers[0], c


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_clean_proposal_passes();             print("  정상 기획안 통과         OK")
    _check_performance_only_rationale_is_blocked(); print("  성과 서술만 = 거부      OK")
    _check_missing_lead_is_blocked();           print("  끊어진 리드 참조 거부    OK")
    _check_unusable_lead_is_blocked();          print("  UNUSABLE 리드 거부       OK")
    _check_daily_only_signal_is_blocked();      print("  일봉 단독 신호 거부      OK")
    _check_unaddressed_rejection_is_blocked();  print("  기각 교훈 미대응 거부    OK")
    _check_addressed_rejection_passes();        print("  대응하면 재도전 허용     OK")
    _check_budget_exhaustion_is_blocked();      print("  예산 소진 차단           OK")
    _check_success_history_does_not_block();    print("  성공 이력은 안 막음      OK")
    _check_deterministic();                     print("  결정론                   OK")
    _check_warnings_do_not_block();             print("  경고 != 차단             OK")
    _check_lesson_vocabulary_is_shared();       print("  교훈 어휘 공유           OK")
    print("발행 게이트 12개 영역 통과.")
