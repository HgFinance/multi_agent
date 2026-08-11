"""공장 다리 - 리서치 기획안 접수(Gate 0)와 실험 결과 환류.

담당: 재일 (퀀트·백테스트본부 QNT)
계약: departments/01-research/contracts/factory_contracts.py
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 7.1절 0단계·10단계, 7.6.2절

▶ 이 모듈이 루프를 닫는다
  지금까지 리서치 -> 퀀트는 `GET /seeds` 호출처가 0개라 끊겨 있었고, 퀀트 -> 리서치는
  코드가 아예 없었다. 그래서 실험 결과가 다음 가설에 영향을 주지 못했다 -
  **공장이 아니라 일방통행이었다.**

  여기서 두 방향을 모두 연결한다:
    접수  research.experiment_proposals -> Gate 0 -> quant.hypotheses
    환류  실험 판정 -> research.experiment_outcomes -> 다음 Gate 0 가 조회

▶ **환류 적재와 상태 전이는 한 트랜잭션이다**
  "적재가 종결의 전제 조건" 을 주석으로 적어 두면 언젠가 지켜지지 않는다. 그래서
  `finalize()` 가 둘을 함께 커밋한다 - 환류가 실패하면 상태도 안 바뀐다. 조용히
  종결되고 교훈만 사라지는 경로를 구조로 없앤다.

▶ Gate 0 는 결정론이다
  어휘 사상·예산·기각 이력 대조 셋 다 코드가 판정한다. 리서치의 발행 게이트가 같은
  검사를 이미 하지만 두 번 하는 것이 낭비가 아니다 - 앞의 것은 기획 비용을 아끼고
  뒤의 것은 **다른 경로로 들어온 가설까지** 막는다(퀀트 자체 생성, 수동 등록).

자체 점검: python departments/04-quant-backtest/pipeline/factory_bridge.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data_resolution import SOURCE_TABLES
from strategy_templates import EDGE_VOCAB, NOT_IMPLEMENTED
from trial_family import UNIVERSE_VOCAB, family_id, hypothesis_view, pressure

MODULE_VERSION = "quant-factory-bridge-v1"

# 기각으로 보는 판정. 이것들의 교훈에는 대응이 있어야 재도전할 수 있다.
REJECTING = frozenset({"REJECT", "GATE_HOLD", "KILLED", "DEMOTED"})

# 예산을 세지 않는 판정 - 실험이 시작조차 못 한 것은 시도가 아니다.
NOT_A_TRIAL = frozenset({"BLOCKED"})

# ── 전략 구조 어휘 ─────────────────────────────────────────────────────────
# ▶ **어휘가 없으면 개념이 조용히 사라진다** (2026-08-11 회고)
#   기획안 초안은 롱숏이었다(하위 분위 롱 / 상위 분위 숏). 실행면이 읽는 키가
#   horizon_days·top_n 뿐이라고 알려 주자, 기획자는 숏 다리를 **표현할 자리가
#   없어서 개념째 지웠다.** 그런데 원장에는 그냥 "momentum 가설" 로 남았다 -
#   "우리 엔진이 숏을 못 해 롱온리로 깎인 모멘텀" 이 아니라.
#   그리고 그것이 결과를 정했을 수 있다: 상승장 벤치마크 +69.55% 를 롱온리
#   선별로 이기는 것은 사실상 불가능하다. **깎인 채로 실험하고 "안 된다" 고
#   기록한 셈이다.** 어휘 밖 구조는 깎지 말고 반려한다.
STRUCTURE_VOCAB = frozenset({"long_only"})
STRUCTURE_NOT_IMPLEMENTED = {
    "long_short": "실행면이 숏 다리를 지원하지 않는다 - 롱온리로 깎아 돌리면 "
                  "그 성적은 이 가설의 증거가 아니다",
    "market_neutral": "베타 중립화 미구현",
    "pairs": "페어 구성·헤지비 미구현",
}

# walk-forward 창이 최소 몇 개는 나와야 강건성을 판정할 수 있다. 이보다 적으면
# 실험을 돌려도 INCONCLUSIVE 로 끝난다 - 그건 접수에서 막아야 할 낭비다.
MIN_WF_WINDOWS = 4


@dataclass
class Gate0Result:
    """접수 판정. **거부 사유는 기계 판독 가능한 코드**여야 리서치가 대응한다."""

    ok: bool = True
    codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    trial_family_id: str = ""
    trial_number: int = 1
    trials_used: int = 0
    # 막지는 않지만 사람이 봐야 하는 것. 경고를 거부로 올리면 게이트가
    # "의심스러우면 차단"이 되고, 그건 신규 가설을 영영 못 사게 만든다.
    warnings: list[str] = field(default_factory=list)

    def reject(self, code: str, why: str) -> None:
        self.ok = False
        self.codes.append(code)
        self.reasons.append(why)

    def warn(self, why: str) -> None:
        self.warnings.append(why)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "codes": list(self.codes),
                "reasons": list(self.reasons),
                "trial_family_id": self.trial_family_id,
                "trial_number": self.trial_number,
                "trials_used": self.trials_used,
                "warnings": list(self.warnings)}


def _hyp_view(proposal: dict) -> dict:
    """기획안 -> trial_family 가 읽는 모양. Family 키는 튜닝값을 안 본다.

    ▶ 기본값을 **여기서 정하지 않는다**(2026-08-11). 예전엔 label/baseline 기본값이
      이 함수에 박혀 있었고 실행면은 그 값을 몰라서, 같은 기획안이 접수와 실행에서
      서로 다른 Family 를 받았다 - 세는 장부와 각인하는 장부가 갈렸다.
    """
    return hypothesis_view(edge_type=proposal.get("edge_type"),
                           universe_key=proposal.get("universe_key"),
                           label=proposal.get("label"),
                           baseline=proposal.get("baseline"))


def _horizon_of(proposal: dict) -> int:
    """기획안이 쓰겠다는 형성·보유 창(거래일). 못 읽으면 0(검사 생략)."""
    sp = proposal.get("suggested_params") or {}
    for k in ("horizon_days", "holding_horizon", "lookback_days"):
        v = sp.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return 0


def gate0(proposal: dict, *, trials_used: int = 0,
          past_outcomes: list[dict] | None = None,
          available_days: int = 0) -> Gate0Result:
    """접수 검사. **순수 함수** - DB 없이 자체 점검이 돈다.

    ▶ **시도 계수와 교훈 조회의 출처를 나눈다.**
      trials_used   = quant.experiments 의 실행 기록(호출부가 센다)
      past_outcomes = research.experiment_outcomes 의 종결 기록(교훈 대조용)

      한 곳에서 세면 안 된다. 실행됐지만 아직 종결되지 않은 실험이 있으면 환류
      기록만 세는 쪽이 적게 세고, 그러면 **예산을 넘겨서 접수된다.** DSR 감가도
      실행 횟수를 보므로 계수 기준은 실행 기록이다.

    past_outcomes: 같은 Family 의 [{decision, lesson_codes:[...]}] 목록.
    """
    r = Gate0Result()
    past = list(past_outcomes or [])

    # ① 통제 어휘 사상. 실행면에 없는 유형을 접수하면 실행 단계에서 죽는다 -
    #    접수는 실행 가능성의 약속이어야 한다.
    edge = str(proposal.get("edge_type") or "").strip().lower()
    if edge not in EDGE_VOCAB:
        why = NOT_IMPLEMENTED.get(edge)
        r.reject("UNMAPPED_VOCAB",
                 f"edge_type={edge!r} 을 실행면 어휘로 사상할 수 없다 - "
                 + (f"미구현: {why}" if why else f"사용 가능: {sorted(EDGE_VOCAB)}"))
    universe = str(proposal.get("universe_key") or "").strip().lower()
    if universe not in UNIVERSE_VOCAB:
        r.reject("UNMAPPED_VOCAB",
                 f"universe_key={universe!r} 가 통제 어휘 밖이다 - "
                 f"사용 가능: {sorted(UNIVERSE_VOCAB)}. 자유 서술은 같은 컨셉을 "
                 f"여러 Family 로 흩어 다중검정 가드를 무력화한다")

    # ①-b 원천 어휘. **접수는 실행 가능성의 약속**인데 여기서 안 보면 가설이
    #     등록된 뒤 실행 단계에서 죽는다(2026-08-10 실측: 기획자가 DATA_TABLES 에
    #     `derived_returns`·`cost_scenarios` 같은 **파생 산출물**을 적었다.
    #     그건 우리가 계산할 것이지 리서치가 요구할 원천이 아니다).
    req = proposal.get("data_requirements") or {}
    tables = req.get("tables") if isinstance(req, dict) else None
    for t in (tables or []):
        # 설명이 붙어 오는 경우가 흔하다("market_bars 일봉 OHLCV"). 첫 토큰만 본다.
        name = str(t).strip().split()[0] if str(t).strip() else ""
        if name not in SOURCE_TABLES:
            r.reject("UNMAPPED_SOURCE",
                     f"data_requirements.tables 의 {name!r} 가 원천 어휘 밖이다 - "
                     f"사용 가능: {sorted(SOURCE_TABLES)}. 파생 지표(수익률·베타·"
                     f"유동성 분위·비용 시나리오)는 실행면이 원천에서 계산한다")

    # ①-c 전략 구조. 어휘 밖이면 **깎지 말고 반려한다.**
    structure = str(proposal.get("strategy_structure") or "long_only").strip().lower()
    if structure not in STRUCTURE_VOCAB:
        why = STRUCTURE_NOT_IMPLEMENTED.get(structure)
        r.reject("UNMAPPED_STRUCTURE",
                 f"strategy_structure={structure!r} 를 실행면이 표현할 수 없다 - "
                 + (why if why else f"사용 가능: {sorted(STRUCTURE_VOCAB)}")
                 + ". 표현 못 하는 구조를 빼고 돌리면 등록한 가설과 다른 실험이 된다")

    # ①-d 설계 실현 가능성. **돌리기 전에 알 수 있는 산수는 접수에서 한다.**
    #   126일 형성 창이 634거래일 표본에 안 들어간다는 것은 백테스트를 다 돌린 뒤
    #   walk-forward 에서야 창 0개로 드러났다(2026-08-11 실측). 실험 한 번을
    #   통째로 버린 셈이다 - 어휘·예산은 접수에서 보면서 이건 안 봤다.
    horizon = _horizon_of(proposal)
    avail = int(available_days or 0)
    if horizon and avail:
        windows = (avail - horizon) // max(horizon, 1)
        if windows < MIN_WF_WINDOWS:
            r.reject("UNDERPOWERED_DESIGN",
                     f"형성·보유 {horizon}일을 표본 {avail}거래일에 태우면 "
                     f"walk-forward 창이 {max(windows, 0)}개다(최소 {MIN_WF_WINDOWS}) - "
                     f"돌려도 강건성을 못 재고 INCONCLUSIVE 로 끝난다. "
                     f"창을 줄이거나 더 긴 이력을 요구하라")

    # ② 시도 압력. 어휘가 안 잡히면 Family 도 못 만든다.
    fam = family_id(_hyp_view(proposal)) if r.ok else ""
    budget = int(proposal.get("trial_budget") or 5)
    used = max(0, int(trials_used))
    p = pressure(fam, [{"trial_family_id": fam} for _ in range(used)], budget=budget)
    r.trial_family_id = p["trial_family_id"]
    r.trial_number = int(p["trial_number"])
    r.trials_used = int(p["trials_used"])
    if p.get("over_budget"):
        r.reject("OVER_BUDGET",
                 f"이 계열에서 이미 {r.trials_used}회 시도했다(예산 {budget}) - "
                 f"증액은 CEO 결정이 필요하다")

    # ③ 기각 이력 대응. **회사가 이미 산 실험을 다시 사지 않는다.**
    #
    # ▶ 전제: "이미 샀어야" 이 규칙이 성립한다 (2026-08-11 재설계)
    #   환류의 목적은 **같은 실험을 두 번 사지 않는 것**이다. 그런데 이 검사가
    #   `trials_used` 를 보지 않아서, **이 계열에서 한 번도 실행된 적이 없는데도**
    #   교훈이 있다는 이유로 막았다(실측: `기존 실행 0, 환류 1` 인데
    #   DUPLICATE_UNADDRESSED). 그건 중복 방지가 아니라 **신규 차단**이고,
    #   아직 아무도 해보지 않은 가설이 영영 실험되지 못하는 교착이 된다.
    #   가설은 실험해서 경험을 쌓아야 하고, 막는 것은 그 경험이 생긴 뒤다.
    needed: set[str] = set()
    for o in past:
        if str(o.get("decision")) in REJECTING:
            needed.update(str(c) for c in (o.get("lesson_codes") or []))
    prior = proposal.get("prior_check") or {}
    answered = set((prior.get("lessons_addressed") or {}).keys())
    missing = sorted(needed - answered)
    if missing and r.trials_used > 0:
        r.reject("DUPLICATE_UNADDRESSED",
                 f"이 계열에서 이미 {r.trials_used}회 실행하고 기각됐다 - "
                 f"그 교훈에 대응이 없다: {missing}")
    elif missing:
        # 실행 0인데 기각 교훈이 있다 = 원장이 어긋났다는 신호다(다른 계열의
        # 결과가 섞였거나 정리가 덜 됐다). **막지 않고 알린다** - 여기서 막으면
        # 데이터 불일치가 곧 신규 실험 금지가 된다.
        r.warn(
            f"이 계열의 실행 기록은 0인데 기각 교훈이 있다: {missing}. "
            "원장 정합성을 확인하되, 아직 실행된 적이 없으므로 접수는 막지 않는다."
        )
    return r


def to_hypothesis_row(proposal: dict, gate: Gate0Result, *,
                      created_by: str = "factory-bridge") -> dict:
    """기획안 -> quant.hypotheses INSERT 페이로드.

    사전등록 시점의 근거를 **복사해 둔다** - 기획안이 나중에 수정돼도 이 실험이
    무엇을 등록했는지는 변하면 안 된다(사전등록의 의미가 그것이다).
    """
    edge = {"type": proposal.get("edge_type"),
            "universe_key": proposal.get("universe_key"),
            **{k: v for k, v in (proposal.get("suggested_params") or {}).items()}}
    return {
        "title": f"[{proposal.get('edge_type')}] {proposal.get('universe_key')}",
        "rationale": proposal.get("economic_rationale") or "",
        "expected_edge": edge,
        "falsification_criteria": list(proposal.get("falsification_tests") or []),
        "required_data_products": proposal.get("data_requirements") or {},
        "status": "PROPOSED",
        "created_by": created_by,
        # ── 계보 (2026-08-10 신설 컬럼) ──
        "proposal_id": proposal.get("proposal_id"),
        "lead_ids": list(proposal.get("lead_ids") or []),
        "research_packet_ids": list(proposal.get("research_packet_ids") or []),
        "claim_ids": list(proposal.get("claim_ids") or []),
        "economic_rationale": proposal.get("economic_rationale"),
        "counterparty": proposal.get("counterparty"),
        "competing_explanation": proposal.get("competing_explanation"),
        "competing_explanation_codes": list(
            proposal.get("competing_explanation_codes") or []),
        "skeptic_sign": proposal.get("skeptic_sign"),
        "source_reported_effect": proposal.get("source_reported_effect") or {},
        "trial_family_id": gate.trial_family_id,
        "trial_number": gate.trial_number,
    }


def outcome_id_for(experiment_id: str, decision: str) -> str:
    """같은 실험의 같은 판정은 한 번만 적재된다(재실행 멱등)."""
    blob = f"{experiment_id}|{decision}"
    return "out_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_outcome(*, experiment_id: str, hypothesis_id: str, trial_family_id: str,
                  trial_number: int, decision: str,
                  failed_criteria=(), lesson_codes=(), oos_summary=None,
                  regime_concerns=(), notes: str = "",
                  proposal_id: str = "") -> dict:
    """환류 행. **미측정 지표는 키를 넣지 않는다** - 0 으로 채우면 관문이 통과로 읽는다."""
    if decision in REJECTING and not (failed_criteria or lesson_codes):
        raise ValueError(
            f"{decision} 인데 사유가 없다 - 사유 없는 기각은 다음 기획안이 "
            f"대조할 것이 없다(환류가 성립하지 않는다)")
    clean = {k: v for k, v in (oos_summary or {}).items() if v is not None}
    return {
        "outcome_id": outcome_id_for(experiment_id, decision),
        "experiment_id": experiment_id, "hypothesis_id": hypothesis_id,
        "trial_family_id": trial_family_id, "trial_number": int(trial_number),
        "decision": decision, "proposal_id": proposal_id,
        "decided_at": datetime.now(timezone.utc),
        "failed_criteria": list(failed_criteria), "oos_summary": clean,
        "regime_concerns": list(regime_concerns), "lesson_codes": list(lesson_codes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# DB 경로 (자체 점검은 conn 주입으로 DB 없이 돈다)
# ---------------------------------------------------------------------------

_SQL_PUBLISHED = """
    select proposal_id, edge_type, universe_key, label, baseline,
           economic_rationale, counterparty, competing_explanation,
           competing_explanation_codes, skeptic_sign, lead_ids,
           falsification_tests, data_requirements, suggested_params,
           trial_budget, prior_check, source_reported_effect,
           research_packet_ids, claim_ids
      from research.experiment_proposals
     where status = 'PUBLISHED'
     order by as_known_at
     limit %s
"""

_SQL_FAMILY_OUTCOMES = """
    select decision, lesson_codes
      from research.experiment_outcomes
     where trial_family_id = %s
     order by decided_at desc
"""

_SQL_INSERT_OUTCOME = """
    insert into research.experiment_outcomes
      (outcome_id, experiment_id, hypothesis_id, trial_family_id, trial_number,
       decision, decided_at, proposal_id, failed_criteria, oos_summary,
       regime_concerns, lesson_codes, notes)
    values (%(outcome_id)s, %(experiment_id)s, %(hypothesis_id)s,
            %(trial_family_id)s, %(trial_number)s, %(decision)s, %(decided_at)s,
            %(proposal_id)s, %(failed_criteria)s, %(oos_summary)s,
            %(regime_concerns)s, %(lesson_codes)s, %(notes)s)
    on conflict (outcome_id) do nothing
"""


def fetch_published_proposals(conn, limit: int = 20) -> list[dict]:
    cols = ("proposal_id edge_type universe_key label baseline economic_rationale "
            "counterparty competing_explanation competing_explanation_codes "
            "skeptic_sign lead_ids falsification_tests data_requirements "
            "suggested_params trial_budget prior_check source_reported_effect "
            "research_packet_ids claim_ids").split()
    with conn.cursor() as cur:
        cur.execute(_SQL_PUBLISHED, (limit,))
        return [dict(zip(cols, row)) for row in cur.fetchall()]


_SQL_FAMILY_TRIALS = "select count(*) from quant.experiments where trial_family_id = %s"


def count_family_trials(conn, trial_family_id: str) -> int:
    """이 Family 에서 **실제로 실행된** 실험 수. 예산과 DSR 이 같은 값을 본다."""
    if not trial_family_id:
        return 0
    with conn.cursor() as cur:
        cur.execute(_SQL_FAMILY_TRIALS, (trial_family_id,))
        return int(cur.fetchone()[0])


def lessons_from(*, failed_criteria=(), regime_concerns=(),
                 fragility: str = "", oos_summary=None) -> list[str]:
    """판정 재료 -> 통제 어휘 교훈. **결정론 기본값이다.**

    LLM 워커(QNT-04)가 이 위에 서술을 얹을 수 있지만, 얹지 않아도 루프는 돈다 -
    에이전트가 없으면 교훈이 안 남는 구조면 **환류가 에이전트 가용성에 묶인다.**
    """
    out: list[str] = []
    f = {str(x).lower() for x in failed_criteria}
    if any("pbo" in x for x in f):
        out.append("OVERFIT_PBO")
    if any("deflated_sharpe" in x for x in f):
        out.append("OVERFIT_DSR")
    if any("excess_return" in x or "information_ratio" in x for x in f):
        out.append("BASELINE_NOT_BEATEN")
    if any("turnover" in x for x in f):
        out.append("COST_SENSITIVE")
    if any("drawdown" in x for x in f):
        out.append("BEAR_FRAGILE")
    if str(fragility).upper() == "FRAGILE":
        out.append("SINGLE_REGIME_ONLY")
    # ▶ **관문을 못 거쳐도 지표가 말하는 것은 남긴다** (2026-08-10 실측)
    #   walk-forward 창이 0개라 종결했더니 교훈이 UNDERPOWERED_DATA 하나였다.
    #   그런데 그 실험은 기준선에 58.83%p 뒤졌다 - 표본이 모자란 것과 별개로
    #   **이미 아는 사실**이고, 안 남기면 다음 기획안이 같은 사양을 또 낸다.
    #   failed_criteria 는 릴리스 관문이 만드는데, 관문에 도달 못 하면 비어 있다.
    oos = {k: v for k, v in (oos_summary or {}).items() if v is not None}
    if oos.get("excess_return_pct", 0) < 0 or oos.get("information_ratio", 0) < 0:
        out.append("BASELINE_NOT_BEATEN")
    if (oos.get("max_drawdown_pct") or 0) < -30 or (oos.get("max_drawdown") or 0) < -0.30:
        out.append("BEAR_FRAGILE")

    if str(fragility).upper() == "INSUFFICIENT":
        # 창이 0개라 강건성을 재지 못했다. "전략이 나쁘다" 가 아니라 **표본이
        # 사양을 못 받친다** 는 뜻이고, 리서치가 다음 기획에서 창을 줄이거나
        # 더 긴 이력을 요구해야 할 신호다.
        out.append("UNDERPOWERED_DATA")
    joined = " ".join(str(c) for c in regime_concerns)
    if "하락장" in joined:
        out.append("BEAR_FRAGILE")
    if "가로질러" in joined or "국면이 1개" in joined:
        out.append("SINGLE_REGIME_ONLY")
    seen, uniq = set(), []          # 순서 보존 중복 제거 - 대조가 지저분해진다
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def fetch_family_outcomes(conn, trial_family_id: str) -> list[dict]:
    if not trial_family_id:
        return []
    with conn.cursor() as cur:
        cur.execute(_SQL_FAMILY_OUTCOMES, (trial_family_id,))
        return [{"decision": d, "lesson_codes": list(lc or [])}
                for d, lc in cur.fetchall()]


_SQL_ALREADY_PROMOTED = (
    "select proposal_id from quant.hypotheses "
    " where proposal_id = any(%s) and proposal_id is not null")

_SQL_INSERT_HYPOTHESIS = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id,
   proposal_id, lead_ids, research_packet_ids, claim_ids,
   economic_rationale, counterparty, competing_explanation,
   competing_explanation_codes, skeptic_sign, source_reported_effect)
values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
returning hypothesis_id
"""


@dataclass(frozen=True)
class Promotion:
    """기획안 하나의 Gate 0 결과. 거부도 결과다 - 조용히 사라지지 않는다."""

    proposal_id: str
    accepted: bool
    hypothesis_id: str = ""
    gate: dict = field(default_factory=dict)

    @property
    def why(self) -> str:
        g = self.gate
        return "; ".join(g.get("reasons") or []) or ",".join(g.get("codes") or [])


def promote_published(conn, *, limit: int = 20, created_by: str = "factory-bridge",
                      dry_run: bool = False) -> list[Promotion]:
    """PUBLISHED 기획안을 Gate 0 에 태워 **가설로 승격한다.**

    ▶ 왜 이 함수가 필요했나 (2026-08-12)
      `gate0` 을 부르는 곳이 E2E 하네스(factory_e2e.py)뿐이었다. 즉 **기획안이
      가설이 되는 운영 경로가 없었다.** 리서치 에이전트가 기획안을 내고 접수까지
      통과해도 `quant.hypotheses` 는 그대로였고, 그래서 공장 자동 조종의 퀀트
      브리핑은 언제나 "실험 대기 가설 0건"이었다. 테스트에서만 도는 루프였다.

    ▶ 왜 결정론이 여기서 도는가
      Gate 0 은 판단이 아니라 **검사**다(어휘 대조·다중검정 예산·계열 압력).
      에이전트에게 맡기면 같은 기획안이 부를 때마다 다른 판정을 받는다.
      마스터플랜의 "결정론이 사실을 모으고 에이전트는 판단만" 이 그 뜻이다.

    ▶ 멱등
      이미 승격된 proposal_id 는 건너뛴다. 두 번 태우면 같은 기획안이 계열
      예산을 두 번 먹고, 다중검정 방어가 스스로 무너진다.
    """
    proposals = fetch_published_proposals(conn, limit=limit)
    if not proposals:
        return []
    ids = [p["proposal_id"] for p in proposals]
    with conn.cursor() as cur:
        cur.execute(_SQL_ALREADY_PROMOTED, (ids,))
        done = {r[0] for r in cur.fetchall()}

    out: list[Promotion] = []
    for p in proposals:
        if p["proposal_id"] in done:
            continue
        # 계열 예산·과거 교훈은 **원장에서 읽는다.** 인자로 받으면 호출부마다
        # 다른 수를 넘겨 같은 기획안이 다른 판정을 받는다.
        probe = gate0(p, trials_used=0, past_outcomes=[])
        fam = probe.trial_family_id
        gate = gate0(p, trials_used=count_family_trials(conn, fam),
                     past_outcomes=fetch_family_outcomes(conn, fam))
        if not gate.ok or dry_run:
            out.append(Promotion(p["proposal_id"], gate.ok, gate=gate.as_dict()))
            continue
        row = to_hypothesis_row(p, gate, created_by=created_by)
        with conn.cursor() as cur:
            cur.execute(_SQL_INSERT_HYPOTHESIS, (
                row["title"], row["rationale"], json.dumps(row["expected_edge"]),
                json.dumps(row["falsification_criteria"]),
                json.dumps(row["required_data_products"]), row["created_by"],
                row["proposal_id"], row["lead_ids"], row["research_packet_ids"],
                row["claim_ids"], row["economic_rationale"], row["counterparty"],
                row["competing_explanation"], row["competing_explanation_codes"],
                row["skeptic_sign"], json.dumps(row["source_reported_effect"])))
            hid = str(cur.fetchone()[0])
        conn.commit()
        out.append(Promotion(p["proposal_id"], True, hypothesis_id=hid,
                             gate=gate.as_dict()))
    return out


class _PromoConn:
    """승격 검증용 가짜 연결. 실행된 SQL 을 순서대로 들고 있는다."""

    def __init__(self, published, already=(), family_trials=0, outcomes=()):
        self.published, self.already = published, list(already)
        self.family_trials, self.outcomes = family_trials, list(outcomes)
        self.inserted: list[tuple] = []
        self.commits = 0

    def cursor(self):
        return _PromoCursor(self)

    def commit(self):
        self.commits += 1


class _PromoCursor:
    def __init__(self, conn):
        self.c, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "from research.experiment_proposals" in s:
            self._rows = [tuple(p[k] for k in (
                "proposal_id edge_type universe_key label baseline "
                "economic_rationale counterparty competing_explanation "
                "competing_explanation_codes skeptic_sign lead_ids "
                "falsification_tests data_requirements suggested_params "
                "trial_budget prior_check source_reported_effect "
                "research_packet_ids claim_ids").split())
                for p in self.c.published]
        elif "select proposal_id from quant.hypotheses" in s:
            self._rows = [(x,) for x in self.c.already]
        elif "count(*) from quant.experiments" in s:
            self._rows = [(self.c.family_trials,)]
        elif "insert into quant.hypotheses" in s:
            self.c.inserted.append(params)
            self._rows = [("hyp-" + str(len(self.c.inserted)),)]
        else:
            self._rows = list(self.c.outcomes)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _check_promotion_is_idempotent_and_records_rejection():
    """기획안 -> 가설 승격이 **한 번만** 일어나고, 거부가 조용히 사라지지 않는가.

    ▶ 이 경로가 없어서 공장이 안 돌았다 (2026-08-12)
      gate0 을 부르는 곳이 E2E 하네스뿐이라 기획안이 가설이 되지 못했다.
      접수는 통과하는데 quant.hypotheses 는 늘 비어 있었고, 퀀트 브리핑은
      언제나 "실험 대기 가설 0건"이었다.

    ▶ 두 번 태우면 다중검정 방어가 무너진다
      같은 기획안이 계열 예산을 두 번 먹는다. 그래서 멱등이 성능이 아니라
      **정합성** 문제다.
    """
    good = _prop(proposal_id="prop_ok")
    conn = _PromoConn([good])
    got = promote_published(conn, limit=5)
    assert len(got) == 1 and got[0].accepted, got
    assert len(conn.inserted) == 1 and conn.commits == 1
    # 계보가 실제로 실렸는가 - 참조가 아니라 복사여야 사전등록이 성립한다
    assert "prop_ok" in conn.inserted[0], conn.inserted[0]

    # 이미 승격된 것은 다시 안 태운다
    again = _PromoConn([good], already=["prop_ok"])
    assert promote_published(again, limit=5) == []
    assert again.inserted == [] and again.commits == 0

    # 거부는 **결과로 남는다** - ok=False 가 목록에 들어오고 INSERT 는 없다
    bad = _prop(proposal_id="prop_bad", edge_type="없는_엣지_유형")
    rej = _PromoConn([bad])
    res = promote_published(rej, limit=5)
    assert len(res) == 1 and not res[0].accepted, res
    assert rej.inserted == [], "거부된 기획안이 가설이 됐다"
    assert res[0].why, "거부 사유가 비었다 - 리서치가 대응할 수 없다"

    # dry_run 은 판정만 하고 쓰지 않는다
    dry = _PromoConn([good])
    assert promote_published(dry, limit=5, dry_run=True)[0].accepted
    assert dry.inserted == [] and dry.commits == 0
    print("  기획안->가설 승격 멱등   OK")


def finalize(conn, *, hypothesis_id: str, new_status: str, outcome: dict) -> str:
    """**환류 적재와 상태 전이를 한 트랜잭션으로 묶는다.**

    "적재가 종결의 전제 조건" 을 주석으로만 적어 두면 언젠가 지켜지지 않는다.
    여기서 환류 INSERT 가 실패하면 상태 UPDATE 도 롤백된다 - 조용히 종결되고
    교훈만 사라지는 경로가 구조적으로 없어진다.
    """
    payload = dict(outcome)
    payload["oos_summary"] = json.dumps(payload.get("oos_summary") or {})
    # 컨텍스트 매니저를 쓰지 않는다 - 호출부(오케스트레이터)의 가짜 커서와 관례가 같다
    cur = conn.cursor()
    cur.execute(_SQL_INSERT_OUTCOME, payload)
    cur.execute(
        "update quant.hypotheses set status = %s, status_changed_at = now() "
        "where hypothesis_id = %s", (new_status, hypothesis_id))
    conn.commit()
    return payload["outcome_id"]


# ---------------------------------------------------------------------------
# 자체 점검 (DB 없음)
# ---------------------------------------------------------------------------

def _prop(**kw):
    base = dict(proposal_id="prop_1", edge_type="mean_reversion",
                universe_key="krx_all", label="forward_return",
                baseline="equal_weight_buy_and_hold",
                economic_rationale="강제 청산자가 반대편에서 판다",
                counterparty="레버리지 청산 물량",
                competing_explanation="베타 노출일 수 있다",
                competing_explanation_codes=["BETA_EXPOSURE"],
                skeptic_sign="worker_run_42", lead_ids=["lead_a"],
                falsification_tests=["하락장 초과수익 < 0 이면 기각"],
                data_requirements={"tables": ["market_bars"], "min_history_days": 750},
                suggested_params={"lookback_days": 20}, trial_budget=5,
                prior_check={}, source_reported_effect={},
                research_packet_ids=[], claim_ids=[])
    base.update(kw)
    return base


def _check_clean_proposal_is_accepted():
    g = gate0(_prop())
    assert g.ok, g.as_dict()
    assert g.trial_family_id and g.trial_number == 1


def _check_unmapped_vocabulary_is_rejected():
    """**접수는 실행 가능성의 약속이다** - 실행면에 없는 유형을 받으면 나중에 죽는다."""
    g = gate0(_prop(edge_type="volatility_risk_premium"))
    assert not g.ok and "UNMAPPED_VOCAB" in g.codes
    assert "미구현" in g.reasons[0], g.reasons
    g2 = gate0(_prop(universe_key="내가 만든 유니버스"))
    assert not g2.ok and "UNMAPPED_VOCAB" in g2.codes
    assert "다중검정 가드" in g2.reasons[0]


def _check_budget_is_enforced():
    g = gate0(_prop(trial_budget=5,
                    prior_check={"lessons_addressed": {"OVERFIT_PBO": "파라미터 고정"}}),
              trials_used=5,
              past_outcomes=[{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}])
    assert not g.ok and "OVER_BUDGET" in g.codes, g.as_dict()
    assert g.trials_used == 5


def _check_trial_count_comes_from_executions_not_outcomes():
    """**실행됐지만 아직 종결 안 된 실험을 안 세면 예산을 넘겨 접수된다.**

    환류 기록이 1건인데 실행이 5건이면 계수 기준은 5여야 한다.
    """
    addressed = {"lessons_addressed": {"OVERFIT_PBO": "파라미터 고정"}}
    past = [{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}]
    over = gate0(_prop(trial_budget=5, prior_check=addressed),
                 trials_used=5, past_outcomes=past)
    assert over.trials_used == 5 and not over.ok, over.as_dict()
    # 반대로 실행이 0이면 환류가 있어도 예산은 남아 있다
    fresh = gate0(_prop(trial_budget=5, prior_check=addressed),
                  trials_used=0, past_outcomes=past)
    assert fresh.ok and fresh.trial_number == 1, fresh.as_dict()


def _check_unaddressed_lessons_block():
    """**회사가 이미 산 실험을 다시 사지 않는다.** 단, 산 적이 있어야 한다."""
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE", "COST_SENSITIVE"]}]
    g = gate0(_prop(), trials_used=1, past_outcomes=past)
    assert not g.ok and "DUPLICATE_UNADDRESSED" in g.codes
    assert "BEAR_FRAGILE" in g.reasons[-1] and "COST_SENSITIVE" in g.reasons[-1]


def _check_never_run_family_is_not_blocked_by_lessons():
    """**실행 0인 계열은 교훈이 있어도 막지 않는다** (2026-08-11 재설계).

    환류의 목적은 같은 실험을 두 번 사지 않는 것이다. 한 번도 실행되지 않은
    계열을 막으면 그건 중복 방지가 아니라 신규 차단이고, 아직 아무도 해보지
    않은 가설이 영영 실험되지 못하는 교착이 된다. 실측에서 실제로 그렇게 막혔다.
    """
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE"]}]
    g = gate0(_prop(), trials_used=0, past_outcomes=past)
    assert g.ok, g.as_dict()
    assert "DUPLICATE_UNADDRESSED" not in g.codes, g.as_dict()
    # 조용히 통과시키지는 않는다 - 원장이 어긋났다는 신호이므로 경고로 남긴다.
    assert g.warnings and "실행 기록은 0" in g.warnings[-1], g.as_dict()


def _check_addressed_lessons_pass():
    past = [{"decision": "REJECT", "lesson_codes": ["BEAR_FRAGILE"]}]
    g = gate0(_prop(prior_check={"lessons_addressed": {"BEAR_FRAGILE": "표본 확대"}}),
              trials_used=1, past_outcomes=past)
    assert g.ok, g.as_dict()
    assert g.trial_number == 2, g.as_dict()   # 두 번째 시도로 계수된다


def _check_success_history_does_not_block():
    past = [{"decision": "PROMOTED", "lesson_codes": []}]
    g = gate0(_prop(), past_outcomes=past)
    assert g.ok, g.as_dict()


def _check_lineage_is_copied_not_referenced():
    """사전등록 시점의 근거를 복사한다 - 기획안이 수정돼도 등록 내용은 안 변한다."""
    p = _prop()
    row = to_hypothesis_row(p, gate0(p))
    for k in ("proposal_id", "economic_rationale", "counterparty",
              "competing_explanation", "skeptic_sign"):
        assert row[k] == p[k if k != "proposal_id" else "proposal_id"], k
    assert row["competing_explanation_codes"] == ["BETA_EXPOSURE"]
    assert row["trial_family_id"] and row["trial_number"] == 1
    # 튜닝 파라미터는 expected_edge 안으로 들어간다(Family 는 이걸 안 본다)
    assert row["expected_edge"]["lookback_days"] == 20


def _check_outcome_requires_reason():
    """사유 없는 기각은 환류가 성립하지 않는다."""
    for d in ("REJECT", "KILLED", "GATE_HOLD", "DEMOTED"):
        try:
            build_outcome(experiment_id="e1", hypothesis_id="h1",
                          trial_family_id="fam", trial_number=1, decision=d)
        except ValueError as exc:
            assert "사유가 없다" in str(exc)
        else:
            raise AssertionError(f"{d} 무사유가 통과했다")
    # 성공 종결은 사유 없이도 적재된다 - 무엇이 통했는지도 배운다
    ok = build_outcome(experiment_id="e1", hypothesis_id="h1",
                       trial_family_id="fam", trial_number=1, decision="PROMOTED")
    assert ok["decision"] == "PROMOTED"


def _check_unmeasured_metrics_are_dropped_not_zeroed():
    """**미측정을 0 으로 채우면 관문이 통과로 읽는다.**"""
    o = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="fam",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"],
                      oos_summary={"pbo": 0.8, "deflated_sharpe": None,
                                   "information_ratio": None})
    assert o["oos_summary"] == {"pbo": 0.8}, o["oos_summary"]
    assert "deflated_sharpe" not in o["oos_summary"]


def _check_outcome_id_is_idempotent():
    a = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    b = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    assert a["outcome_id"] == b["outcome_id"]
    c = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REVISE")
    assert c["outcome_id"] != a["outcome_id"]


def _check_finalize_is_atomic():
    """**환류 실패 시 상태 전이도 안 된다** - 조용히 종결되는 경로를 없앤다."""

    class _Cur:
        def __init__(self, boom_on): self.boom_on, self.ran = boom_on, []
        def execute(self, sql, params=None):
            self.ran.append(sql.strip()[:20])
            if self.boom_on and self.boom_on in sql:
                raise RuntimeError("insert 실패")

    class _Conn:
        def __init__(self, boom_on=None):
            self.cur = _Cur(boom_on); self.committed = False
        def cursor(self): return self.cur
        def commit(self): self.committed = True

    o = build_outcome(experiment_id="e1", hypothesis_id="h1", trial_family_id="f",
                      trial_number=1, decision="REJECT", failed_criteria=["pbo"])
    ok = _Conn()
    finalize(ok, hypothesis_id="h1", new_status="REJECTED", outcome=o)
    assert ok.committed and len(ok.cur.ran) == 2

    boom = _Conn(boom_on="insert into research.experiment_outcomes")
    try:
        finalize(boom, hypothesis_id="h1", new_status="REJECTED", outcome=o)
    except RuntimeError:
        pass
    else:
        raise AssertionError("환류 실패가 통과했다")
    assert not boom.committed, "환류가 실패했는데 커밋됐다"
    assert len(boom.cur.ran) == 1, "환류 실패 후 상태 UPDATE 가 실행됐다"


def _check_lessons_mapping_is_deterministic_baseline():
    """**에이전트가 없어도 교훈이 남는다** - 환류가 에이전트 가용성에 묶이면 안 된다."""
    assert lessons_from(failed_criteria=["pbo", "min_deflated_sharpe"]) == [
        "OVERFIT_PBO", "OVERFIT_DSR"]
    b = lessons_from(failed_criteria=["max_turnover"], fragility="FRAGILE")
    assert "COST_SENSITIVE" in b and "SINGLE_REGIME_ONLY" in b, b
    assert lessons_from(regime_concerns=["하락장 평균 수익률 -32.1%"]) == ["BEAR_FRAGILE"]
    # 같은 교훈이 두 경로로 나와도 한 번만 (대조가 지저분해진다)
    assert lessons_from(failed_criteria=["max_drawdown_pct"],
                        regime_concerns=["하락장 손실"]) == ["BEAR_FRAGILE"]
    assert lessons_from() == []


def _check_gate0_is_deterministic():
    p = _prop()
    kw = dict(trials_used=1,
              past_outcomes=[{"decision": "REJECT", "lesson_codes": ["OVERFIT_PBO"]}])
    assert gate0(p, **kw).as_dict() == gate0(p, **kw).as_dict()


_SQL_REGISTER = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id, proposal_id, lead_ids,
   economic_rationale, counterparty, competing_explanation,
   competing_explanation_codes, skeptic_sign, source_reported_effect)
values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
        %s,%s,%s,%s,%s,%s,%s,%s)
-- 부분 유니크 인덱스(proposal_id is not null)를 쓰므로 같은 술어를 적어야
-- Postgres 가 그 인덱스를 추론한다. 빠뜨리면 제약이 있어도 못 찾는다.
on conflict (proposal_id) where proposal_id is not null do nothing
returning hypothesis_id
"""


def intake_published(conn, *, limit: int = 20, available_days: int = 0) -> list[dict]:
    """PUBLISHED 기획안을 Gate 0 에 태우고 통과분을 가설로 등록한다.

    ▶ 계수 출처를 섞지 않는다: 시도 횟수는 `quant.experiments`(실행 기록)에서,
      기각 교훈은 `research.experiment_outcomes`(대조 대상)에서 온다. 한 곳에서
      세면 미종결 실험을 놓쳐 예산을 넘긴 채 접수한다.
    """
    import json as _json

    out: list[dict] = []
    for prop in fetch_published_proposals(conn, limit=limit):
        fam = family_id(_hyp_view(prop))
        # ▶ 표본 일수를 접수에 넘긴다. 없으면 설계 검사가 조용히 생략되므로
        #   못 잰 경우 0 을 넘겨 검사를 건너뛰되, 잰 경우엔 반드시 쓴다.
        gate = gate0(prop,
                     trials_used=count_family_trials(conn, fam),
                     past_outcomes=fetch_family_outcomes(conn, fam),
                     available_days=available_days)
        rec = {"proposal_id": prop.get("proposal_id"), "gate": gate,
               "hypothesis_id": None}
        if gate.ok:
            row = to_hypothesis_row(prop, gate)
            cur = conn.cursor()
            cur.execute(_SQL_REGISTER, (
                row["title"], row["rationale"], _json.dumps(row["expected_edge"]),
                _json.dumps(row["falsification_criteria"]),
                _json.dumps(row["required_data_products"]), row["created_by"],
                row["proposal_id"], row["lead_ids"], row["economic_rationale"],
                row["counterparty"], row["competing_explanation"],
                row["competing_explanation_codes"], row["skeptic_sign"],
                _json.dumps(row["source_reported_effect"])))
            got = cur.fetchone()
            rec["hypothesis_id"] = str(got[0]) if got else None
        out.append(rec)
    conn.commit()
    return out


def _cli_intake() -> int:
    import psycopg2

    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    # 표본 일수는 시장 DB 에만 있다. 못 붙으면 0 -> 설계 검사는 건너뛰지만
    # 그 사실이 로그에 남는다(조용히 통과시키지 않는다).
    days = 0
    try:
        m = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
        c = m.cursor()
        c.execute("select count(distinct bucket_time::date) from market.market_bars"
                  " where interval_code = '1D'")
        days = int(c.fetchone()[0] or 0)
        m.close()
        print(f"  표본 {days}거래일 기준으로 설계 실현가능성을 검사한다")
    except Exception as e:
        print(f"  ! 표본 일수를 못 쟀다({type(e).__name__}) - 설계 검사 생략")
    try:
        for rec in intake_published(conn, available_days=days):
            g = rec["gate"]
            mark = "접수" if g.ok else "반려"
            print(f"  [{mark}] {rec['proposal_id']}  family={g.trial_family_id}"
                  f" trial#{g.trial_number}")
            for r in (g.reasons or []):
                print(f"      X {r}")
            if rec["hypothesis_id"]:
                print(f"      -> 가설 {rec['hypothesis_id']}")
            elif g.ok:
                print("      (이미 등록된 기획안 - 중복 접수 안 함)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--intake" in sys.argv:
        raise SystemExit(_cli_intake())

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_clean_proposal_is_accepted();        print("  정상 기획안 접수         OK")
    _check_unmapped_vocabulary_is_rejected();   print("  어휘 미사상 거부         OK")
    _check_budget_is_enforced();                print("  시도 예산 강제           OK")
    _check_trial_count_comes_from_executions_not_outcomes()
    print("  계수=실행기록(종결 아님)  OK")
    _check_unaddressed_lessons_block();         print("  기각 교훈 미대응 차단    OK")
    _check_never_run_family_is_not_blocked_by_lessons(); print("  실행 0 계열은 안 막음    OK")
    _check_addressed_lessons_pass();            print("  대응하면 재도전 허용     OK")
    _check_success_history_does_not_block();    print("  성공 이력은 안 막음      OK")
    _check_lineage_is_copied_not_referenced();  print("  계보 복사(참조 아님)     OK")
    _check_outcome_requires_reason();           print("  사유 없는 기각 거부      OK")
    _check_unmeasured_metrics_are_dropped_not_zeroed(); print("  미측정 != 0        OK")
    _check_outcome_id_is_idempotent();          print("  환류 멱등                OK")
    _check_finalize_is_atomic();                print("  환류+전이 원자성         OK")
    _check_lessons_mapping_is_deterministic_baseline()
    print("  교훈 사상(에이전트 무관)  OK")
    _check_gate0_is_deterministic();            print("  Gate 0 결정론            OK")
    _check_promotion_is_idempotent_and_records_rejection()
    print("공장 다리 14개 영역 통과. 루프가 닫혔다.")
