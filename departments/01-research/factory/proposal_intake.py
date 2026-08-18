"""기획자·회의론자 산출을 실험 기획안으로 적재한다 - 발행 게이트가 판정한다.

담당: 재일 (리서치본부 RES)
계약: departments/01-research/contracts/factory_contracts.py (ExperimentProposalV1)
게이트: departments/01-research/factory/publish_gate.py

▶ 리드는 기획안이 아니다
  `lead_intake` 가 방법론을 원장에 올렸지만, 리드는 "남이 이렇게 주장한다" 일 뿐
  우리가 무엇을 검증할지는 아직 없다. 그 사이를 메우는 것이 기획안이고,
  기획안이 없으면 퀀트는 사전 등록할 대상이 없다.

▶ **회의론자는 기획자와 같은 실행이면 안 된다**
  `skeptic_sign` 을 기획자가 스스로 채우면 제안자와 검증자가 같아진다. 여기서
  구조로 막는다 - 서명이 비었거나 기획자 실행 id 와 같으면 발행을 거부한다.
  경쟁 설명을 자기가 쓰고 자기가 서명하는 것은 반증이 아니라 형식이다.

▶ **회의론자가 기각하면 발행하지 않는다**
  경쟁 설명이 본가설을 이긴다고 판정했는데도 올리면 그 판정은 장식이다.

▶ 판정은 게이트가 한다
  이 모듈은 조립만 하고, 통과 여부는 `publish_gate.evaluate()` 가 정한다.
  근거 리드와 기각 이력은 **DB 에서 읽어** 넘긴다 - 에이전트가 말한 것을 그대로
  쓰면 없는 리드를 근거로 단 기획안이 통과한다.

자체 점검: python departments/01-research/factory/proposal_intake.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "04-quant-backtest" / "pipeline"))

from factory_contracts import (  # noqa: E402
    CompetingExplanation, DataRequirement, ExperimentProposalV1,
    MethodologyLeadV1, PriorCheck,
)
from intraday_ablation import (  # noqa: E402
    INTRADAY_SCREENING_COHORT_VERSION,
)
from lead_intake import clip_excerpt, parse_blocks  # noqa: E402
from publish_gate import evaluate  # noqa: E402
from stock_universe import governed_stock_evidence_sql  # noqa: E402

MODULE_VERSION = "research-proposal-intake-v3"
MAX_INTRADAY_COHORT = 8
CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT = "explicit-primitive-window-v2"
_GOVERNED_PAST_OUTCOME = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

PLANNER_KEYS = ("TITLE", "LEAD_IDS", "ECONOMIC_RATIONALE", "COUNTERPARTY",
                "EDGE_TYPE", "UNIVERSE_KEY", "LABEL", "BASELINE",
                "RESEARCH_LANE", "SEMANTIC_PLAN",
                "FALSIFICATION_TESTS", "DATA_TABLES", "MIN_HISTORY_DAYS",
                "SUGGESTED_PARAMS", "SOURCE_REPORTED_EFFECT", "TRIAL_BUDGET",
                # ▶ **여기 없으면 앞 필드가 오염된다** (2026-08-13 실측)
                #   `LESSONS_ADDRESSED` 가 이 목록에 빠져 있었다. `build()` 는
                #   `planner.get("LESSONS_ADDRESSED")` 로 읽는데 `parse_blocks`
                #   가 그 키를 만들지 않으니, 에이전트가 서식대로 쓰면 값이
                #   **앞 필드에 이어붙어** `trial_budget 이 숫자가 아니다:
                #   '5 LESSONS_ADDRESSED: BASELINE_NOT_BEATEN=…'` 로 죽었다.
                #   오늘 카드 문구를 "LESSONS_ADDRESSED 칸에 적어라" 로 고친
                #   순간 이 함정이 켜졌다 - 서식과 파서가 갈리면 고칠수록
                #   나빠진다. `_check_format_fields_are_parsed` 가 고정한다.
                "LESSONS_ADDRESSED",
                # 자기서명 경로에서 기획자가 직접 쓰는 반대 가설. 어휘에 없으면
                # parse_blocks 가 새 키로 안 잡고 **앞 필드 값에 이어붙여** 버린다
                # - 그러면 그 앞 필드의 뜻까지 바뀐다.
                "COMPETING_EXPLANATION", "COMPETING_CODES")
SKEPTIC_KEYS = ("TITLE", "COMPETING_EXPLANATION", "COMPETING_CODES", "VERDICT")

PLANNER_REQUIRED = ("TITLE", "LEAD_IDS", "ECONOMIC_RATIONALE", "COUNTERPARTY",
                    "EDGE_TYPE", "UNIVERSE_KEY")
SKEPTIC_PASS = "PROCEED"          # 회의론자가 본가설을 살려 둔 경우만


def _set_distance(left, right) -> float:
    """Jaccard distance with two empty descriptions treated as identical."""
    lhs, rhs = frozenset(left), frozenset(right)
    union = lhs | rhs
    return 0.0 if not union else 1.0 - len(lhs & rhs) / len(union)


def _semantic_tokens(value, prefix: str = "semantic") -> frozenset[str]:
    """Flatten a semantic plan without depending on JSON/list input order."""
    out: set[str] = set()

    def walk(current, path: str) -> None:
        if isinstance(current, dict):
            for key in sorted(current):
                walk(current[key], f"{path}.{key}")
            return
        if isinstance(current, (list, tuple, set, frozenset)):
            for item in current:
                walk(item, path)
            return
        out.add(f"{path}={json.dumps(current, ensure_ascii=False, sort_keys=True)}")

    walk(value, prefix)
    return frozenset(out)


def _intraday_novelty_signature(row: dict, grammar) -> dict:
    """Content-only structural and economic signature for cohort selection."""
    expr = grammar.parse(row["intraday_signal_expr"])
    return {
        "shape": grammar.shape_fingerprint(expr),
        "fields": frozenset(grammar.fields_of(expr)),
        "operators": frozenset(grammar.operators_of(expr)),
        "clocks": frozenset(
            str(value) for value in
            getattr(grammar, "temporal_windows_of", grammar.clocks_of)(expr)),
        "primitive_windows": frozenset(
            str(value) for value in
            getattr(grammar, "primitive_windows_of", lambda _expr: set())(expr)),
        "semantic": _semantic_tokens(row.get("semantic_plan") or {}),
    }


def _intraday_pairwise_novelty(left: dict, right: dict) -> tuple[float, float,
                                                                  float]:
    """Return combined, structural, and semantic distances in ``[0, 1]``.

    Shape/field/operator differences dominate numeric-window tuning. Economic
    semantics still separate formulas whose executable trees look alike but
    encode different events, states, or directions.
    """
    structural = (
        0.45 * float(left["shape"] != right["shape"])
        + 0.22 * _set_distance(left["fields"], right["fields"])
        + 0.13 * _set_distance(left["operators"], right["operators"])
        + 0.10 * _set_distance(left["clocks"], right["clocks"])
        + 0.10 * _set_distance(
            left["primitive_windows"], right["primitive_windows"])
    )
    semantic = _set_distance(left["semantic"], right["semantic"])
    return 0.75 * structural + 0.25 * semantic, structural, semantic


def _intraday_novelty_key(row: dict) -> tuple[str, str]:
    """Stable tie-breaker based on formula/semantics, never lead UUID or order."""
    return (
        str(row.get("ast_fingerprint") or ""),
        json.dumps(row.get("semantic_plan") or {}, ensure_ascii=False,
                   sort_keys=True, separators=(",", ":")),
    )


def _exact_semantic_plan_fingerprint(plan: dict) -> str:
    """Hash the complete canonical plan, including execution and horizon.

    ``alpha_semantics.fingerprint`` intentionally identifies an economic
    family and therefore omits the numeric horizon.  Cohort provenance needs a
    stricter identity: a 30-second FOLLOW contract must never cite a 600-second
    REVERT lead merely because both leads happen to compile to the same AST16.
    """
    from alpha_semantics import validate as validate_plan  # noqa: PLC0415

    canonical = validate_plan(plan)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_novel_intraday_sidecars(primary: dict, candidates: list[dict], *,
                                    limit: int, grammar) -> list[dict]:
    """Deterministic farthest-first max-min structural/semantic frontier.

    The primary is the first anchor. Each next sidecar maximizes its minimum
    pairwise novelty to every already selected formula. Stable content hashes
    resolve exact ties, making the result invariant to database row order and
    lead UUIDs while keeping numeric-window near-clones from crowding the four
    independent-candidate slots.
    """
    if limit <= 0:
        return []
    unique: dict[str, dict] = {}
    primary_fp = str(primary.get("ast_fingerprint") or "")
    for row in candidates:
        key = str(row.get("ast_fingerprint") or "")
        if not key or key == primary_fp:
            continue
        current = unique.get(key)
        if current is None or _intraday_novelty_key(row) < \
                _intraday_novelty_key(current):
            unique[key] = row
    pool = sorted(unique.values(), key=_intraday_novelty_key)
    anchors = [_intraday_novelty_signature(primary, grammar)]
    selected: list[dict] = []
    while pool and len(selected) < limit:
        scored = []
        for row in pool:
            signature = _intraday_novelty_signature(row, grammar)
            distances = [_intraday_pairwise_novelty(signature, anchor)
                         for anchor in anchors]
            minimums = tuple(min(values) for values in zip(*distances))
            scored.append((row, signature, minimums))
        row, signature, _ = min(
            scored,
            key=lambda item: (
                -item[2][0], -item[2][1], -item[2][2],
                _intraday_novelty_key(item[0]),
            ),
        )
        selected.append(row)
        anchors.append(signature)
        pool.remove(row)
    return selected


@dataclass
class Rejected:
    title: str
    reason: str


@dataclass
class Intake:
    proposals: list = field(default_factory=list)     # (ExperimentProposalV1, GateResult)
    rejected: list = field(default_factory=list)

    @property
    def publishable(self) -> list:
        return [p for p, g in self.proposals if g.ok]


def _split(v: str) -> tuple[str, ...]:
    """쉼표·세미콜론으로 나눈다. 빈 칸은 버린다."""
    parts = [x.strip() for x in str(v or "").replace(";", ",").split(",")]
    return tuple(p for p in parts if p)


def _lessons_addressed(v: str) -> dict:
    """`CODE=이번엔 무엇을 다르게` 목록 -> dict.

    ▶ 왜 필요한가 (2026-08-12 실측)
      Gate 0 는 "이 계열에서 이미 기각됐다 - 그 교훈에 대응이 없다" 로 재제안을
      막는다. 옳은 규칙인데, **기획자 서식에 대응을 적을 칸이 없었다.**
      에이전트는 대응을 했다 - ECONOMIC_RATIONALE 산문에:

        "이전 SINGLE_REGIME_ONLY 실패에 대응해 특정 국면 필터를 사용하지
         않고 krx_all 전체 기간을 포함하며…"

      게이트는 구조화된 필드를 보는데 그 필드가 서식에 없었으므로 못 읽었다.
      결과: **기각된 계열은 영원히 다시 제안할 수 없었다** - 배분자가 "찾은
      것을 밀어붙여라" 라고 말해도 접수에서 막혔다.

      쉼표로 나누되 값 안의 쉼표는 살린다 - "국면 필터를 빼고, 전 기간을 쓴다"
      같은 문장이 잘리면 대응이 반토막 난다.
    """
    out: dict[str, str] = {}
    text = str(v or "").strip()
    if not text:
        return out
    # `CODE=` 가 나오는 자리에서만 자른다(값 안의 쉼표·세미콜론은
    # 안 자른다). LLM이 내는 `A=..., B=...`·`A=...; B=...`를 둘 다
    # 받되, 다음 토큰이 통제 코드일 때만 경계로 본다.
    parts = re.split(r"[,;]\s*(?=[A-Z][A-Z0-9_]{2,}\s*=)", text)
    for p in parts:
        if "=" not in p:
            continue
        code, _, how = p.partition("=")
        code, how = code.strip().upper(), how.strip()
        if code and how:
            out[code] = how
    return out


def _maybe_json(v: str) -> dict:
    """dict 로 읽히면 dict, 아니면 빈 dict. **문자열을 억지로 끼우지 않는다.**"""
    try:
        d = json.loads(str(v or "").strip() or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def signal_expr_from_block(block: dict):
    """Return the executable AST regardless of its research clock."""
    params = _maybe_json(block.get("SUGGESTED_PARAMS", ""))
    return params.get("signal_expr") or params.get("intraday_signal_expr")


def _trial_budget(v) -> int:
    """시도 예산은 **숫자다.** 서술로 쓰면 다중검정 방어가 계산을 못 한다.

    'medium' 이나 '16개 조합을 사전등록' 같은 값이 실제로 들어왔다(2026-08-10).
    조합을 여러 개 돌리겠다는 말은 예산이 그만큼 필요하다는 뜻인데, 그걸 숫자로
    안 적으면 trial_family 가 분모를 못 세고 DSR 이 감가를 못 한다. 그래서
    조용히 기본값으로 때우지 않고 무엇이 문제인지 말하며 막는다.
    """
    s = str(v or "").strip()
    if not s:
        return 5
    if s.isdigit():
        return int(s)
    raise ValueError(
        f"trial_budget 이 숫자가 아니다: {s[:60]!r} - 시도 횟수는 다중검정의 "
        f"분모라서 서술로 쓸 수 없다. 변형을 N 개 돌릴 계획이면 N 을 적어라")


_CODE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _competing_codes(raw: str) -> tuple[list, str]:
    """`COMPETING_CODES` 값에서 코드만 걷어낸다. 반환: (코드들, 걷어내지 못한 나머지).

    ▶ 왜 필요한가 (2026-08-12 실측)
      `parse_blocks` 는 **모르는 줄을 앞 필드에 이어붙인다.** 산문 필드에서는
      그게 맞다 - 새 키로 잘못 잡으면 메커니즘 문장이 중간에서 끊긴다. 그런데
      `COMPETING_CODES` 는 **닫힌 어휘**라 이 관대함이 독이 된다. 에이전트가
      마지막 필드 뒤에 마무리 문장을 쓰자 그게 코드값에 흡수돼

        `COST_UNACCOUNTED 제출 형식에 맞춘 모멘텀 실험 기획안 1건을 작성했다. …`

      가 됐고, `경쟁 설명 코드가 어휘 밖이다` 로 반려됐다. **코드 자체는 어휘에
      있었다.** 멀쩡한 기획안이 마무리 문장 한 줄 때문에 버려진 것이다.

      그래서 코드 모양(`^[A-Z][A-Z0-9_]*$`)인 토큰만 코드로 보고 나머지는 산문으로
      돌린다. 코드가 하나도 없으면 그때는 진짜 어휘 문제이므로 부르는 쪽이 막는다.
    """
    codes, rest = [], []
    for chunk in _split(raw):
        for tok in chunk.split():
            up = tok.strip().upper()
            if not _CODE_TOKEN.match(up):
                rest.append(tok)
                continue
            try:
                c = CompetingExplanation(up)
            except ValueError:
                rest.append(tok)      # 코드 모양인데 어휘 밖 - 이건 진짜 오류다
                continue
            if c not in codes:
                codes.append(c)
    return codes, " ".join(rest)[:120]


def proposal_id_for(lead_ids, edge_type: str, universe_key: str, *,
                    material: dict | None = None) -> str:
    """Return the identity of one immutable material preregistration.

    A lead/edge/universe-only id made a corrected parameter contract collide
    with the already rejected row, so ``ON CONFLICT DO NOTHING`` silently ate
    the repair.  Material execution/design fields now distinguish a genuinely
    corrected proposal.  Prose rewrites and shared-replay sidecars do not: they
    must not manufacture fresh trials.
    """
    if material is None:
        # Preserve the legacy helper contract for callers that only need the
        # coarse family-style identity.  Newly built proposals always pass a
        # material specification below.
        blob = json.dumps(
            [sorted(str(x) for x in lead_ids), edge_type.strip().lower(),
             universe_key.strip().lower()],
            ensure_ascii=False, separators=(",", ":"))
        return "prop_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    material = dict(material)
    params = dict(material.get("suggested_params") or {})
    # Screening sidecars are compute amortisation only and never hold promotion
    # authority, so membership changes must not manufacture fresh trials.  The
    # cohort version is different: it binds the execution/screening contract.
    # A repaired proposal under a new contract must not collide with a rejected
    # legacy row through ``ON CONFLICT DO NOTHING``.
    params.pop("screening_population", None)
    material["suggested_params"] = params
    if isinstance(material.get("falsification_tests"), (list, tuple)):
        material["falsification_tests"] = sorted(
            str(value) for value in material["falsification_tests"])
    requirements = dict(material.get("data_requirements") or {})
    if isinstance(requirements.get("tables"), (list, tuple)):
        requirements["tables"] = sorted(str(value)
                                         for value in requirements["tables"])
    if requirements:
        material["data_requirements"] = requirements
    plan = dict(material.get("semantic_plan") or {})
    for key in ("context", "qualities"):
        if isinstance(plan.get(key), (list, tuple)):
            plan[key] = sorted(str(value) for value in plan[key])
    if plan:
        material["semantic_plan"] = plan
    blob = json.dumps([sorted(str(x) for x in lead_ids),
                       edge_type.strip().lower(), universe_key.strip().lower(),
                       material], ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return "prop_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _prior_check(past_outcomes, lessons_text: str) -> PriorCheck:
    """원장 이력 + 에이전트 대응 -> PriorCheck.

    **사실은 우리가 채우고 대응은 에이전트가 쓴다.** 둘을 섞으면 안 된다 -
    우리가 대응까지 지어내면 Gate 0 의 견제가 형식만 남는다.
    """
    rows = list(past_outcomes or [])
    primary_scopes = {"AST_EXACT", "AST_EXACT_PRIMARY", "FAMILY_PRIMARY"}
    # Broad edge/universe outcomes are useful failure memory, not evidence that
    # this executable equation spent a PRIMARY budget slot.  Legacy callers
    # without scope annotations retain their historical all-row behaviour.
    scoped = any(str(r.get("match_scope") or "") for r in rows)
    budget_rows = ([r for r in rows
                    if str(r.get("match_scope") or "") in primary_scopes]
                   if scoped else rows)
    rejecting = {"REJECT", "GATE_HOLD", "KILL"}
    codes: list[str] = []
    fam = ""
    for r in rows:
        if str(r.get("decision", "")).upper() in rejecting:
            for c in (r.get("lesson_codes") or []):
                if str(c) not in codes:
                    codes.append(str(c))
        fam = fam or str(r.get("trial_family_id") or "")
    # ▶ **에이전트가 쓴 것을 거르지 않는다.** 계약은 형식(통제 어휘·빈 대응
    #   금지)만 보고, "맞는 교훈에 대응했나" 는 Gate 0 이 본다. 여기서
    #   미리 걸러 내면 대응 맵이 비어 계약이 엉뚱한 사유로 죽는다(실측).
    #   층마다 볼 것을 정해 두고 겹치지 않는다.
    budget_family = next((str(r.get("trial_family_id") or "")
                          for r in budget_rows
                          if r.get("trial_family_id")), "")
    return PriorCheck(trial_family_id=budget_family or fam,
                      trials_used=len(budget_rows),
                      past_outcomes=tuple(codes),
                      lessons_addressed=_lessons_addressed(lessons_text))


def _canonical_universe_key(raw: str, research_lane: str) -> str:
    """Normalize the one known intraday cohort/version identity mix-up.

    ``intraday-screening-cohort-v4`` names the deterministic screening contract,
    not an execution universe.  Preserve every other value so the downstream
    controlled-vocabulary gate can continue to reject unknown universes rather
    than silently changing the experiment.
    """
    universe = raw.strip().lower()
    lane = research_lane.strip().upper()
    if (lane == "INTRADAY_EVENT"
            and universe == INTRADAY_SCREENING_COHORT_VERSION):
        return "krx_all"
    return universe


def build(planner: dict, skeptic: dict, *, case_id: str,
          planner_run: str, skeptic_run: str,
          past_outcomes: list | None = None,
          as_known_at: datetime | None = None) -> ExperimentProposalV1:
    """두 에이전트 산출을 기획안 하나로 조립한다. 계약이 나머지를 검증한다."""
    if not skeptic_run.strip():
        raise ValueError("skeptic_sign 이 비었다 - 서명 없는 반증은 반증이 아니다")
    if skeptic_run.strip() == planner_run.strip():
        # 같은 실행이 기획하고 서명하면 생성자·검증자 분리가 무너진다.
        raise ValueError("회의론자 서명이 기획자와 같은 실행이다 - 독립 검증이 아니다")

    verdict = (skeptic.get("VERDICT") or "").strip().upper()
    if verdict and verdict != SKEPTIC_PASS:
        raise ValueError(f"회의론자가 통과시키지 않았다({verdict}) - 발행하지 않는다")

    codes, stray = _competing_codes(skeptic.get("COMPETING_CODES", ""))
    if not codes:
        # 어휘 밖 코드는 조용히 버리지 않는다 - 게이트가 막을 수 있게 남긴다.
        raise ValueError(f"경쟁 설명 코드가 어휘 밖이다: {stray or '(비어 있음)'}")

    lead_ids = _split(planner["LEAD_IDS"])
    edge = planner["EDGE_TYPE"].strip().lower()
    research_lane = (planner.get("RESEARCH_LANE") or
                     "DAILY_CROSS_SECTIONAL").strip().upper()
    universe = _canonical_universe_key(planner["UNIVERSE_KEY"], research_lane)

    tables = _split(planner.get("DATA_TABLES", "")) or ("market_bars",)
    try:
        min_days = int(str(planner.get("MIN_HISTORY_DAYS", "")).strip() or 0)
    except ValueError:
        min_days = 0

    data_requirements = DataRequirement(tables=list(tables),
                                        min_history_days=min_days)
    suggested_params = _maybe_json(planner.get("SUGGESTED_PARAMS", ""))
    semantic_plan = _maybe_json(planner.get("SEMANTIC_PLAN", ""))
    trial_budget = _trial_budget(planner.get("TRIAL_BUDGET", ""))
    label = (planner.get("LABEL") or "forward_return").strip()
    baseline = (planner.get("BASELINE") or "equal_weight_buy_and_hold").strip()
    falsification_tests = _split(planner.get("FALSIFICATION_TESTS", ""))
    prior_check = _prior_check(
        past_outcomes, planner.get("LESSONS_ADDRESSED", ""))
    material = {
        "label": label,
        "baseline": baseline,
        "falsification_tests": list(falsification_tests),
        "research_lane": research_lane,
        "data_requirements": data_requirements.model_dump(mode="json"),
        "suggested_params": suggested_params,
        "semantic_plan": semantic_plan,
        "trial_budget": trial_budget,
        # Only the planner-authored repair belongs in identity.  Ledger-derived
        # family/trial history changes over time and would make an unchanged
        # proposal churn ids on every harvest.
        "lessons_addressed": dict(prior_check.lessons_addressed),
    }

    return ExperimentProposalV1(
        proposal_id=proposal_id_for(
            lead_ids, edge, universe, material=material),
        case_id=case_id,
        as_known_at=as_known_at or datetime.now(timezone.utc),
        lead_ids=lead_ids,
        economic_rationale=planner["ECONOMIC_RATIONALE"],
        counterparty=planner["COUNTERPARTY"],
        competing_explanation=skeptic.get("COMPETING_EXPLANATION", ""),
        competing_explanation_codes=tuple(codes),
        skeptic_sign=skeptic_run.strip(),
        edge_type=edge,
        universe_key=universe,
        label=label,
        baseline=baseline,
        falsification_tests=falsification_tests,
        data_requirements=data_requirements,
        suggested_params=suggested_params,
        research_lane=research_lane,
        semantic_plan=semantic_plan,
        trial_budget=trial_budget,
        # ▶ **이력은 원장에서, 대응은 에이전트에게서** (2026-08-12)
        #   몇 번 돌았는지·무엇으로 기각됐는지는 사실이라 우리가 채운다.
        #   그것에 어떻게 대응할지는 판단이라 에이전트가 쓴다. 예전엔 둘 다
        #   비워 두어 Gate 0 의 "교훈에 대응이 없다" 검사가 **발동할 수도,
        #   통과할 수도 없는** 상태였다.
        prior_check=prior_check,
        source_reported_effect=_maybe_json(
            planner.get("SOURCE_REPORTED_EFFECT", "")),
    )


def _attach_intraday_screening_cohort(
        proposal: ExperimentProposalV1,
        leads: dict[str, MethodologyLeadV1]) -> ExperimentProposalV1:
    """Turn linked typed leads into a provenance-checked shared-replay cohort.

    Only the formula copied into ``SUGGESTED_PARAMS`` remains a preregistered
    primary lead.  Other linked formulas are screening sidecars: they share the
    expensive raw-event replay but cannot be promoted from that evidence.  Their
    lead ids therefore stay unused and can later receive an independent
    confirmatory experiment.
    """
    if proposal.research_lane.value != "INTRADAY_EVENT":
        return proposal

    import intraday_ast_contract as intraday_grammar  # noqa: PLC0415
    from intraday_ast_contract import (  # noqa: PLC0415
        COMPLETED_SECOND_SCREENING_COHORT_VERSION,
        EXPLICIT_FEATURE_WINDOW_CONTRACT,
        LEGACY_FEATURE_WINDOW_CONTRACT,
        field_window_bindings_of,
        fingerprint,
        parse,
        unit_of,
        validate_completed_second_candidate,
        validate_feature_window_contract,
    )
    from intraday_ablation import (  # noqa: PLC0415
        generate as generate_ablations,
    )
    from formula_discovery import assess as assess_formula  # noqa: PLC0415
    from alpha_semantics import validate as validate_plan  # noqa: PLC0415

    if (INTRADAY_SCREENING_COHORT_VERSION
            != COMPLETED_SECOND_SCREENING_COHORT_VERSION):
        raise RuntimeError(
            "research intake and completed-second capability contracts disagree: "
            f"{INTRADAY_SCREENING_COHORT_VERSION!r} != "
            f"{COMPLETED_SECOND_SCREENING_COHORT_VERSION!r}")
    if (CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT !=
            EXPLICIT_FEATURE_WINDOW_CONTRACT):
        raise RuntimeError(
            "proposal intake current feature-window contract drifted from "
            "the deployed evaluator")
    params = dict(proposal.suggested_params or {})
    proposal_plan = validate_plan(dict(proposal.semantic_plan or {}))
    proposal_execution = str(
        params.get("execution") or proposal_plan["execution"]
    ).strip().upper()
    if proposal_execution != proposal_plan["execution"]:
        raise ValueError(
            "INTRADAY_EVENT proposal execution does not match its exact "
            "semantic_plan contract")
    try:
        proposal_horizon = int(
            params.get("horizon_seconds", proposal_plan["horizon_seconds"]))
    except (TypeError, ValueError):
        raise ValueError(
            "INTRADAY_EVENT proposal horizon_seconds must be an integer") \
            from None
    if proposal_horizon != proposal_plan["horizon_seconds"]:
        raise ValueError(
            "INTRADAY_EVENT proposal horizon_seconds does not match its exact "
            "semantic_plan contract")
    params["execution"] = proposal_execution
    params["horizon_seconds"] = proposal_horizon
    configured_entry = str(params.get("entry_policy") or "").strip().upper()
    configured_coefficient = str(
        params.get("coefficient_policy") or "").strip().upper()
    def feature_window_contract_for(expression: dict,
                                    declared: object = None) -> str:
        version = str(declared or "").strip()
        if not version:
            version = (
                EXPLICIT_FEATURE_WINDOW_CONTRACT
                if any(seconds is not None for _field, seconds in
                       field_window_bindings_of(expression))
                else LEGACY_FEATURE_WINDOW_CONTRACT)
        validate_feature_window_contract(
            expression, contract_version=version)
        return version

    primary_window_contract = feature_window_contract_for(
        params.get("intraday_signal_expr"),
        params.get("feature_window_contract_version"))
    if primary_window_contract != EXPLICIT_FEATURE_WINDOW_CONTRACT:
        raise ValueError(
            "current INTRADAY_EVENT proposals require "
            f"feature_window_contract_version="
            f"{EXPLICIT_FEATURE_WINDOW_CONTRACT!r}; legacy formulas must first "
            "be migrated into a new V2 child")
    primary = validate_feature_window_contract(
        params.get("intraday_signal_expr"),
        contract_version=primary_window_contract)
    primary = validate_completed_second_candidate(
        primary, execution=proposal_execution)
    params["feature_window_contract_version"] = primary_window_contract
    primary_fp = fingerprint(primary)
    contract_groups: dict[
        tuple[str, str, str, str, str, str], list[dict]] = {}
    rejected_primary: list[str] = []

    # The planner chooses the preregistered primary, but it is not a reliable
    # cohort assembler: a live run linked one valid current-contract lead even
    # though four more unused formulas were present in its own brief.  ``load_leads``
    # therefore supplies a bounded current-contract pool. Consider that pool here so
    # shared replay is deterministic rather than dependent on an LLM copying
    # 2--8 ids correctly.  Only the exact primary remains in ``lead_ids``;
    # every other formula is SCREENING_ONLY and remains unused for a later
    # independent confirmation.
    linked_lead_ids = frozenset(str(value) for value in proposal.lead_ids)
    for lead_id in sorted(str(value) for value in leads):
        lead = leads.get(str(lead_id))
        if lead is None:
            continue
        contract = dict(lead.ast_contract or {})
        if (contract.get("formula_discovery_version") != "formula-discovery-v5"
                or contract.get("feature_window_contract_version") !=
                EXPLICIT_FEATURE_WINDOW_CONTRACT
                or not contract.get("formula_contract_complete")
                or contract.get("alpha_candidate_eligible") is not True
                or contract.get("research_lane") != "INTRADAY_EVENT"):
            continue
        try:
            expr = parse(contract.get("candidate_signal_expr"))
            candidate_window_contract = feature_window_contract_for(
                expr, contract.get("feature_window_contract_version"))
        except (TypeError, ValueError):
            continue
        fp = fingerprint(expr)
        thesis = dict(contract.get("formula_thesis") or {})
        semantic_plan = dict(contract.get("semantic_plan") or {})
        try:
            semantic_plan = validate_plan(semantic_plan)
            validate_completed_second_candidate(
                expr, execution=semantic_plan.get("execution"))
            assess_formula(
                thesis, candidate=expr,
                semantic_plan=semantic_plan,
                grammar=intraday_grammar,
            )
        except (TypeError, ValueError) as exc:
            if fp == primary_fp:
                rejected_primary.append(f"{lead_id}: {exc}")
            continue
        entry_policy = str(thesis.get("decision_rule") or "").strip().upper()
        coefficient_policy = str(
            thesis.get("coefficient_policy") or "").strip().upper()
        raw_baseline = contract.get("source_baseline_expr")
        source_baseline = None
        source_baseline_fp = ""
        if raw_baseline not in (None, ""):
            try:
                source_baseline = parse(raw_baseline)
                source_baseline_fp = hashlib.sha256(json.dumps(
                    source_baseline, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
            except (TypeError, ValueError):
                # The baseline participates in durable candidate identity.  A
                # malformed value must not be silently discarded and merged
                # with an otherwise identical executable lead.
                continue
        contract_key = (
            fp,
            _exact_semantic_plan_fingerprint(semantic_plan),
            entry_policy,
            coefficient_policy,
            source_baseline_fp,
            candidate_window_contract,
        )
        contract_groups.setdefault(contract_key, []).append({
            "candidate_role": "LINKED_CANDIDATE",
            "source_lead_ids": [str(lead_id)],
            "title": str(lead.claimed_edge),
            "ast_fingerprint": fp,
            "intraday_signal_expr": expr,
            "semantic_plan": semantic_plan,
            "entry_policy": entry_policy,
            "coefficient_policy": coefficient_policy,
            "source_baseline_expr": source_baseline,
            "feature_window_contract_version": candidate_window_contract,
            "evolution_role": str(contract.get("evolution_role") or "SEED"),
            "evolution_operators": list(
                contract.get("evolution_operators") or ()),
            "parent_ast_fingerprint": str(
                contract.get("parent_ast_fingerprint") or ""),
            "parent_candidate_identity_fingerprint": str(
                contract.get("parent_candidate_identity_fingerprint") or ""),
            "screening_cohort_version": INTRADAY_SCREENING_COHORT_VERSION,
            "_parent_signal_expr": contract.get("parent_signal_expr"),
            "_parent_feature_window_contract_version": contract.get(
                "parent_feature_window_contract_version"),
        })

    # One AST16 can occupy only one cohort slot.  First merge provenance only
    # inside the *exact* executable contract (full semantic plan, entry rule,
    # and coefficient policy), then select one contract per AST deterministically.
    # Conflicting contracts remain unused so they can receive independent
    # experiments instead of being falsely cited by this proposal.
    grouped_rows: list[
        tuple[tuple[str, str, str, str, str, str], dict]] = []
    for contract_key, rows in contract_groups.items():
        canonical = min(
            rows,
            key=lambda row: (
                str(row.get("title") or ""),
                str(row.get("evolution_role") or ""),
                json.dumps(row.get("_parent_signal_expr"), sort_keys=True,
                           separators=(",", ":"), default=str),
                str(row.get(
                    "_parent_feature_window_contract_version") or ""),
                row["source_lead_ids"][0],
            ),
        ).copy()
        canonical["source_lead_ids"] = sorted({
            source_id
            for row in rows
            for source_id in row["source_lead_ids"]
        })
        grouped_rows.append((contract_key, canonical))

    primary_contracts = [
        (key, row) for key, row in grouped_rows
        if key[0] == primary_fp
        and key[1] == _exact_semantic_plan_fingerprint(proposal_plan)
        and (not configured_entry or key[2] == configured_entry)
        and (not configured_coefficient or key[3] == configured_coefficient)
        and key[5] == primary_window_contract
        and linked_lead_ids.intersection(row["source_lead_ids"])
    ]
    if not primary_contracts:
        if rejected_primary:
            raise ValueError(
                "INTRADAY_EVENT primary formula no longer passes the current "
                "formula influence audit: " + " | ".join(rejected_primary))
        raise ValueError(
            "INTRADAY_EVENT primary formula must exactly match one linked "
            "formula-discovery-v5 candidate contract (AST, semantic plan, "
            "horizon, execution, entry, and coefficient policy)")
    _, primary_row = min(
        primary_contracts,
        key=lambda item: (item[0], _intraday_novelty_key(item[1])))

    candidates: dict[str, dict] = {primary_fp: primary_row}
    by_ast: dict[
        str, list[tuple[tuple[str, str, str, str, str, str], dict]]] = {}
    for key, row in grouped_rows:
        if key[0] != primary_fp and key[5] == primary_window_contract:
            by_ast.setdefault(key[0], []).append((key, row))
    for fp, choices in by_ast.items():
        _, candidates[fp] = min(
            choices,
            key=lambda item: (
                0 if linked_lead_ids.intersection(
                    item[1]["source_lead_ids"]) else 1,
                item[0],
                _intraday_novelty_key(item[1]),
            ),
        )

    primary_lead_ids = tuple(candidates[primary_fp]["source_lead_ids"])
    linked_sidecars = [row for fp, row in candidates.items() if fp != primary_fp]

    # An explicit parent is a valuable within-replay ablation.  It receives
    # the child's semantic/execution contract and remains SCREENING_ONLY.
    known_fps = set(candidates)
    lineage_parents = []
    for child in list(candidates.values()):
        parent_raw = child.pop("_parent_signal_expr", None)
        declared_parent_window_contract = child.pop(
            "_parent_feature_window_contract_version", None)
        if parent_raw in (None, ""):
            continue
        try:
            parent = parse(parent_raw)
            parent_window_contract = feature_window_contract_for(
                parent, declared_parent_window_contract)
            child["parent_feature_window_contract_version"] = \
                parent_window_contract
            validate_completed_second_candidate(
                parent,
                execution=dict(child["semantic_plan"]).get("execution"),
            )
            parent_fp = fingerprint(parent)
            child_structure_only = child["coefficient_policy"] == "STRUCTURE_ONLY"
            if ((not child_structure_only and unit_of(parent) != "BPS")
                    or (child_structure_only and unit_of(parent) == "BOOL")
                    or parent_fp in known_fps):
                continue
            if parent_window_contract != child[
                    "feature_window_contract_version"]:
                # A legacy formula upgraded to explicit primitive windows is
                # preserved as lineage provenance, but cannot share one frozen
                # evaluator contract with its V2 child.
                continue
        except (TypeError, ValueError):
            continue
        known_fps.add(parent_fp)
        lineage_parents.append({
            "candidate_role": "LINEAGE_PARENT",
            "source_lead_ids": list(child["source_lead_ids"]),
            "title": f"parent of {child['title']}",
            "ast_fingerprint": parent_fp,
            "intraday_signal_expr": parent,
            "semantic_plan": dict(child["semantic_plan"]),
            "entry_policy": child["entry_policy"],
            "coefficient_policy": child["coefficient_policy"],
            "source_baseline_expr": child.get("source_baseline_expr"),
            "feature_window_contract_version": parent_window_contract,
            "evolution_role": "PARENT_ABLATION",
            "parent_of_ast_fingerprint": child["ast_fingerprint"],
            "screening_cohort_version": INTRADAY_SCREENING_COHORT_VERSION,
        })

    controls = []
    primary_source = candidates[primary_fp]
    for control in generate_ablations(primary)[:2]:
        if control["ast_fingerprint"] in known_fps:
            continue
        known_fps.add(control["ast_fingerprint"])
        controls.append({
            **control,
            "candidate_role": "STRUCTURAL_ABLATION",
            "source_lead_ids": list(primary_lead_ids),
            "title": f"structural control of {primary_source['title']}",
            "semantic_plan": dict(primary_source["semantic_plan"]),
            "entry_policy": primary_source["entry_policy"],
            "coefficient_policy": primary_source["coefficient_policy"],
            "source_baseline_expr": primary_source.get(
                "source_baseline_expr"),
            "feature_window_contract_version": primary_window_contract,
            "evolution_role": "EMPIRICAL_TERM_INFLUENCE",
            "screening_cohort_version": INTRADAY_SCREENING_COHORT_VERSION,
        })

    # Preserve broad exploration while keeping explicit evolutionary ancestry
    # executable. A selected child is never emitted without its declared parent
    # row; most importantly, an evolved primary reserves its parent before
    # novelty or ablation slots are filled.
    linked_ranked = _select_novel_intraday_sidecars(
        primary_source, linked_sidecars, limit=len(linked_sidecars),
        grammar=intraday_grammar)
    preferred = (linked_ranked[:4] + controls + linked_ranked[4:] +
                 sorted(lineage_parents, key=_intraday_novelty_key))
    pool = {str(row["ast_fingerprint"]): row for row in preferred}
    sidecars: list[dict] = []
    selected_fps = {primary_fp}
    capacity = MAX_INTRADAY_COHORT - 1

    def dependency_chain(row: dict, trail: frozenset[str]) \
            -> list[dict] | None:
        row_fp = str(row["ast_fingerprint"])
        if row_fp in selected_fps:
            return []
        if row_fp in trail:
            return None
        parent_fp = str(row.get("parent_ast_fingerprint") or "")
        chain: list[dict] = []
        if parent_fp and parent_fp not in selected_fps:
            parent_row = pool.get(parent_fp)
            if parent_row is None:
                return None
            inherited = dependency_chain(
                parent_row, trail | frozenset({row_fp}))
            if inherited is None:
                return None
            chain.extend(inherited)
        if row_fp not in {str(item["ast_fingerprint"]) for item in chain}:
            chain.append(row)
        return chain

    def select_with_parents(row: dict, *, required: bool = False) -> bool:
        chain = dependency_chain(row, frozenset())
        if chain is None or len(sidecars) + len(chain) > capacity:
            if required:
                raise ValueError(
                    "evolved primary requires an explicit parent contract "
                    "inside the frozen screening cohort")
            return False
        for item in chain:
            item_fp = str(item["ast_fingerprint"])
            if item_fp not in selected_fps:
                sidecars.append(item)
                selected_fps.add(item_fp)
        return True

    primary_parent_fp = str(
        primary_source.get("parent_ast_fingerprint") or "")
    cross_contract_migration = False
    if primary_parent_fp:
        primary_parent = pool.get(primary_parent_fp)
        if primary_parent is None:
            cross_contract_migration = (
                primary_source.get(
                    "parent_feature_window_contract_version") not in
                (None, "", primary_window_contract)
                and "PRIMITIVE_WINDOW_MIGRATION" in {
                    str(value).upper() for value in
                    primary_source.get("evolution_operators") or ()})
            if not cross_contract_migration:
                raise ValueError(
                    "evolved primary declares a parent formula that is absent "
                    "from its exact source-backed cohort")
        else:
            select_with_parents(primary_parent, required=True)
    for row in preferred:
        if len(sidecars) >= capacity:
            break
        select_with_parents(row)
    for row in sidecars:
        row.pop("_parent_signal_expr", None)
    params["screening_population"] = sidecars[:MAX_INTRADAY_COHORT - 1]
    params["screening_cohort_version"] = INTRADAY_SCREENING_COHORT_VERSION
    params["feature_window_contract_version"] = primary_window_contract
    params["entry_policy"] = primary_source["entry_policy"]
    params["coefficient_policy"] = primary_source["coefficient_policy"]
    params["source_baseline_expr"] = primary_source.get(
        "source_baseline_expr")
    params["parent_candidate_identity_fingerprint"] = primary_source.get(
        "parent_candidate_identity_fingerprint") or None
    if cross_contract_migration:
        params["migration_parent_ast_fingerprint"] = primary_parent_fp
        params["migration_parent_feature_window_contract_version"] = str(
            primary_source.get("parent_feature_window_contract_version") or
            LEGACY_FEATURE_WINDOW_CONTRACT)
    # A V1 parent cannot be replayed inside a frozen V2 feature cube.  Its exact
    # AST remains durable research provenance on the evolved methodology lead
    # (and normally as source_baseline_expr), but it must not masquerade as an
    # in-cohort runtime lineage edge: the runner correctly requires every such
    # parent to share the primary evaluator contract and be present in the
    # frozen screening population.
    params["parent_ast_fingerprint"] = (
        None if cross_contract_migration else primary_parent_fp or None)
    material = {
        "label": proposal.label,
        "baseline": proposal.baseline,
        "falsification_tests": list(proposal.falsification_tests),
        "research_lane": proposal.research_lane.value,
        "data_requirements": proposal.data_requirements.model_dump(mode="json"),
        "suggested_params": params,
        "semantic_plan": proposal.semantic_plan,
        "trial_budget": proposal.trial_budget,
        "lessons_addressed": dict(proposal.prior_check.lessons_addressed),
    }
    return proposal.model_copy(update={
        "proposal_id": proposal_id_for(
            primary_lead_ids, proposal.edge_type, proposal.universe_key,
            material=material),
        "lead_ids": primary_lead_ids,
        "suggested_params": params,
    })


def intake(planner_text: str, skeptic_text: str, *, case_id: str,
           planner_run: str, skeptic_run: str,
           leads: dict | None = None, past_outcomes: list | None = None,
           outcomes_for=None,
           as_known_at: datetime | None = None) -> Intake:
    """기획자·회의론자 산출을 짝지어 조립하고 발행 게이트에 태운다.

    ▶ `outcomes_for(block) -> list` - **기획안마다 자기 계열 이력을 읽는다**
      (2026-08-13). `past_outcomes` 하나를 모든 기획안에 똑같이 쓰면, 한 카드에
      좌표가 다른 기획안이 둘 있을 때 남의 계열 이력으로 판정하게 된다.
      그리고 실제로는 그것조차 안 넘어오고 있었다 - `harvest` 가 인자를
      빼먹어서 **모든 기획안이 "이 계열은 처음"으로 접수됐다.** 그 결과
      접수 계약("기각 이력이 있는데 대응이 비면 거부")이 한 번도 발동하지
      않았고, 승격 관문이 뒤늦게 같은 것을 잡아 영구 반려로 만들었다.
    """
    out = Intake()
    _sk_blocks = parse_blocks(skeptic_text, SKEPTIC_KEYS)
    skeptics = {(b.get("TITLE") or "").strip(): b for b in _sk_blocks}
    _pl_blocks = parse_blocks(planner_text, PLANNER_KEYS)
    # 짝짓기의 "모호하지 않음" 은 **접수 가능한 기획안** 사이에서 따진다.
    # 필수 항목이 빠져 어차피 반려될 블록을 경쟁자로 세면, 멀쩡한 1:1 이
    # 1:2 로 보여 짝짓기가 실패한다(자체점검이 잡았다).
    _pl_valid = [b for b in _pl_blocks
                 if not [k for k in PLANNER_REQUIRED if not (b.get(k) or "").strip()]]

    for p in _pl_blocks:
        title = (p.get("TITLE") or "(제목 없음)").strip()
        missing = [k for k in PLANNER_REQUIRED if not (p.get(k) or "").strip()]
        if missing:
            out.rejected.append(Rejected(title, f"필수 항목 없음: {','.join(missing)}"))
            continue
        s = _pair_skeptic(title, skeptics, _pl_valid, _sk_blocks)
        if s is None:
            # ▶ **왜 짝이 안 맞았는지 말해 준다** (2026-08-13 실측)
            #   에이전트가 MCP 로 12번 넘게 제출했고 매번 이 사유로 거부됐다.
            #   그런데 사유가 "없다" 뿐이라 **무엇이 없는지 알 수 없었다** -
            #   회의론자 텍스트는 냈는데 제목이 안 맞아 짝짓기가 실패한 것을
            #   모른 채 본문만 다시 써서 5번을 더 시도했다(t_f64fb6ce).
            #   양쪽 제목을 그대로 실어야 한 번에 고친다.
            seen = sorted(k for k in skeptics if k)
            out.rejected.append(Rejected(
                title,
                ("독립 worker의 회의론자 검토가 없다 - 반대 가설인 "
                 "COMPETING_EXPLANATION을 기획자가 적었어도 독립 검증이 아니므로 "
                 "접수하지 않는다")
                if not seen else
                (f"회의론자 블록은 {len(seen)}개 왔는데 이 기획안과 **제목이 "
                 f"맞지 않는다.** 기획자 제목={title!r} · 회의론자 제목={seen}. "
                 f"제목을 똑같이 맞추고 독립 worker 실행 ID를 제출하라")))
            continue
        # 이 기획안 **자기 계열**의 이력. 못 읽으면 조용히 0 으로 넘기지 않는다 -
        # 미측정과 "이력 없음" 을 섞는 순간 계약이 무력해진다(그게 이 사고였다).
        past = past_outcomes
        if outcomes_for is not None:
            try:
                past = outcomes_for(p)
            except Exception as e:      # noqa: BLE001
                out.rejected.append(Rejected(
                    title, f"계열 이력을 못 읽었다 - 지난 기각에 대응했는지 "
                           f"판정할 수 없으므로 접수하지 않는다: "
                           f"{type(e).__name__}: {str(e)[:120]}"))
                continue
        try:
            prop = build(p, s, case_id=case_id, planner_run=planner_run,
                         skeptic_run=skeptic_run, as_known_at=as_known_at,
                         past_outcomes=past)
            prop = _attach_intraday_screening_cohort(prop, leads or {})
        except Exception as e:          # 계약 위반·독립성 위반 모두 여기로
            out.rejected.append(Rejected(title, str(e)))
            continue
        gate = evaluate(prop, leads=leads or {}, past_outcomes=past or [])
        out.proposals.append((prop, gate))
    return out


# ── DB ─────────────────────────────────────────────────────────────────────
def load_leads(conn, lead_ids, *, _expand_current: bool = True) -> dict:
    """근거 리드를 **DB 에서** 읽는다. 에이전트가 말한 리드를 그대로 믿지 않는다."""
    if not lead_ids:
        return {}
    cur = conn.cursor()
    cur.execute("""
        select lead_id, case_id, scout_lens, source_type, as_known_at, refs, ast_contract,
               claimed_edge, stated_mechanism, inferred, market_context,
               stated_failure_mode, independent_mentions, testability, status,
               model_version, prompt_version
          from research.methodology_leads where lead_id = any(%s)
    """, (list(lead_ids),))
    cols = ("lead_id", "case_id", "scout_lens", "source_type", "as_known_at",
            "refs", "ast_contract", "claimed_edge", "stated_mechanism", "inferred",
            "market_context", "stated_failure_mode", "independent_mentions",
            "testability", "status", "model_version", "prompt_version")
    out = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        if isinstance(d["refs"], str):
            d["refs"] = json.loads(d["refs"])
        if isinstance(d["ast_contract"], str):
            d["ast_contract"] = json.loads(d["ast_contract"])
        # ▶ **원장에 이미 계약을 넘는 값이 들어가 있다** (2026-08-12 실측)
        #   `lead_intake.to_lead` 가 EXCERPT 없을 때 MECHANISM 으로 대체하는데
        #   길이 규율이 없어 500자를 넘겼고, 그게 그대로 적재됐다. 그 뒤로 이
        #   리드를 읽을 때마다 `MethodologyLeadV1` 검증이 터져 **수확 전체가
        #   죽었다** - 20260812T00 카드가 매 주기 같은 자리에서 반복 실패했다.
        #
        #   적재 쪽은 고쳤지만(clip_excerpt) 이미 들어간 행은 남는다. 원장 값을
        #   고쳐 쓰지 않고 **계약 경계에서 맞춘다** - 저장된 것은 그 수확이 실제로
        #   쓴 문자열이라 사실이고, 잘렸다는 표식이 붙으므로 읽는 쪽이 원문
        #   전체로 오해하지 않는다.
        for ref in (d.get("refs") or []):
            if isinstance(ref, dict) and ref.get("excerpt"):
                ref["excerpt"] = clip_excerpt(ref["excerpt"])
        out[d["lead_id"]] = MethodologyLeadV1.model_validate(d)

    # If the submitted primary is a live current-contract intraday formula,
    # supplement the LLM-selected ids with a bounded pool of other unused formulas. This
    # does not mark those leads used: _attach_intraday_screening_cohort keeps
    # only the primary id on the proposal and records the rest as sourced,
    # non-promotable shared-replay sidecars.
    wants_current_intraday = any(
        (lead.ast_contract or {}).get("formula_discovery_version")
        == "formula-discovery-v5"
        and (lead.ast_contract or {}).get("research_lane")
        == "INTRADAY_EVENT"
        and (lead.ast_contract or {}).get("feature_window_contract_version")
        == CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT
        for lead in out.values()
    )
    if _expand_current and wants_current_intraday:
        cur.execute("""
            select l.lead_id
              from research.methodology_leads l
             where l.status = 'COMPLETE'
               and l.testability = 'RULE_EXPRESSIBLE'
               and l.ast_contract->>'formula_discovery_version' =
                   'formula-discovery-v5'
               and l.ast_contract->>'research_lane' = 'INTRADAY_EVENT'
               and l.ast_contract->>'feature_window_contract_version' = %s
               and l.ast_contract->>'ast_readiness' = 'AST_READY'
               and coalesce(
                     (l.ast_contract->>'formula_contract_complete')::boolean,
                     false)
               and coalesce(
                     (l.ast_contract->>'alpha_candidate_eligible')::boolean,
                     false)
               and not (l.lead_id = any(%s))
               and not exists (
                     select 1 from research.experiment_proposals p
                      where l.lead_id = any(p.lead_ids)
                        and p.status in ('PUBLISHED','ACCEPTED'))
               and not exists (
                     select 1 from research.proposal_review_outcomes r
                      where r.verdict = 'STOP'
                        and l.lead_id = any(r.lead_ids))
             order by l.created_at desc, l.lead_id
             limit %s
        """, (CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT,
              list(out), MAX_INTRADAY_COHORT - 1))
        extra_ids = [row[0] for row in cur.fetchall()]
        if extra_ids:
            # Reuse the canonical row validator without recursively expanding
            # the pool again.
            extras = load_leads(conn, extra_ids, _expand_current=False)
            out.update(extras)
    return out


def _norm_title(s: str) -> str:
    """제목 비교용 정규화. 공백·대소문자·양끝 구두점만 없앤다."""
    t = re.sub(r"\s+", " ", str(s or "")).strip().casefold()
    return t.strip("\"'`“”‘’.,:;!?()[]{}<>-–— ")


def _pair_skeptic(title: str, skeptics: dict, planners: list, sk_blocks: list):
    """기획안에 맞는 회의론자 블록을 찾는다. 못 찾으면 None.

    ▶ **자유 텍스트 제목으로 조인하고 있었다** (2026-08-13 실측)
      에이전트가 MCP 로 기획안을 제출할 때마다 `회의론자 검토가 없다` 로
      거부됐다 - 4회, 5회, 3회. 원장에 이렇게 남아 있었다:

        "planner/skeptic 텍스트와 별도 run 식별자를 제공했지만 게이트가
         독립 회의론자 산출을 인식하지 않아 발행 0건"

      원인은 `skeptics.get(title)` 이 **정확 일치**만 보는 것이다. 회의론자가
      제목을 한 글자라도 다르게 쓰면(마침표·따옴표·띄어쓰기) 짝이 깨지고,
      그러면 대비책이 **기획자 블록에** COMPETING_EXPLANATION 을 요구한다.
      그런데 경쟁 설명을 쓰는 것은 원래 회의론자의 일이라 거기 있었다.
      **에이전트는 옳게 했고 조인이 깨진 것이다.**

      멀티에이전트 핸드오프를 자유 텍스트 키로 잇지 말라는 것이 문헌의
      권고다 - 타입 계약을 쓸 수 없는 자리에서는 최소한 **정규화하고,
      모호하지 않을 때는 위치로 잇는다.**

    순서: ① 정확 일치 ② 정규화 일치 ③ 양쪽 1건씩이면 그것끼리(모호하지 않다)
    """
    s = skeptics.get(title)
    if s is not None:
        return s
    want = _norm_title(title)
    for k, v in skeptics.items():
        if _norm_title(k) == want and want:
            return v
    # 기획안도 회의론도 하나뿐이면 짝은 그것뿐이다 - 제목이 달라도 모호하지 않다.
    # 여럿일 때는 하지 않는다. 잘못 짝지으면 **남의 반대 가설로 통과**시킨다.
    if len(planners) == 1 and len(sk_blocks) == 1:
        return sk_blocks[0]
    return None


def load_past_outcomes(conn, edge_type: str, universe_key: str,
                       *, label: str = "", baseline: str = "",
                       signal_expr: dict | None = None,
                       research_lane: str = "DAILY_CROSS_SECTIONAL") -> list[dict]:
    """같은 계열 또는 exact AST의 지난 판정.

    계열명은 LLM이 바꿀 수 있지만 실행된 수식의 지문은 바뀌지 않는다. 따라서
    edge/universe 이력과 exact ``signal_expr`` 이력을 합쳐 보여 준다. 같은 outcome은
    SQL의 OR 조건으로 한 번만 읽고, AST로 맞은 행에는 ``match_scope``를 각인한다.

    ▶ 여기가 `발주 0건` 의 실제 지점이었다 (2026-08-13 실측)
      두 가지가 겹쳐 있었다.

      ① 이 함수가 `research.experiment_outcomes.edge_type` 을 조회했는데
         **그 컬럼이 없다.** 부르면 무조건 예외다.
      ② 그런데 애초에 **아무도 부르지 않았다.** `factory_autopilot.harvest`
         가 `intake(..., leads=leads)` 만 넘기고 `past_outcomes` 를 안 줘서
         기본값 `[]` 로 들어갔다.

      그래서 기획안 `prior_check` 가 전부 `trials_used=0 · past_outcomes=[] ·
      trial_family_id=""` 였다. 접수 계약은 "기각 이력이 있는데 대응이 비면
      거부" 인데 이력이 0이니 통과시킨다. 그리고 **승격 관문은 진짜 계열
      이력을 보고** "이미 2회 실행하고 기각됐다 - 그 교훈에 대응이 없다" 며
      막는다. 기획자는 접수에서 "이 계열은 처음" 이라고 듣고 그대로 했는데
      승격에서 그 이유로 벌을 받았다. 오늘 11:23 에 위험관리 손잡이를 처음
      쓴 기획안(`prop_5682`)도 여기서 죽었다.

      못 읽으면 **예외를 그대로 올린다.** 조용히 `[]` 를 돌려주는 것이
      이 사고의 형태였다 - 미측정과 0을 섞으면 계약이 무력해진다.

    ▶ **계열 해시를 여기서 다시 계산하지 않는다** (2026-08-13, 배포하고 알았다)
      처음엔 `trial_family.family_ids_for` 로 계열 ID 를 만들어 조회했다.
      그런데 이 함수는 **두 컨테이너에서 돈다** - 호스트 수확기(퀀트 파이프라인이
      있음)와 MCP 도구 면(리서치만 있음). MCP 쪽에서 `ModuleNotFoundError:
      No module named 'trial_family'` 로 죽었고, fail-closed 설계라 그게
      **기획안을 반려시켰다** - 고치기 전보다 나빠진 것이다.

      해시 로직을 양쪽에 복제하는 것은 답이 아니다. 그건 "같은 판단을 두
      곳에서" 를 다시 만드는 일이고, 오늘 그 사고를 이미 한 번 고쳤다.
      대신 **원장에 이미 찍힌 것을 좌표로 조회한다** - 판정은 가설을 통해
      좌표에 매달려 있으므로 해시가 필요 없다.

      이 조회는 계열(edge·universe·label·baseline)보다 **넓다**(edge·universe).
      의도한 것이다 - 접수는 "무엇에 답해야 하는지" 를 넓게 보여 주고, 좁은
      계열 판정은 승격 관문이 한다. 덜 알려 주는 쪽이 더 나쁘다.

    ▶ **조인 대신 작은 조회 둘로 나눈다** (2026-08-13, 세 번째 시도에서)
      `quant.hypotheses.hypothesis_id` 는 uuid 이고 `experiment_outcomes` 쪽은
      text 다. 양쪽을 캐스팅해 조인했더니 `QueryCanceled: statement timeout`
      이 났다 - 표가 47행·14행인데도. 양변 캐스팅은 인덱스를 못 쓰게 하고,
      이 원장은 세션풀이 얇아(컨테이너 23개가 15슬롯을 문다) 조금만 무거워도
      끊긴다. 작은 조회 둘이 조인 하나보다 안전하다.
    """
    edge = str(edge_type or "").strip().lower()
    uni = str(universe_key or "").strip().lower()
    lane = str(research_lane or "DAILY_CROSS_SECTIONAL").strip().upper()
    if lane not in {"DAILY_CROSS_SECTIONAL", "INTRADAY_EVENT"}:
        raise ValueError(f"unknown research_lane {lane!r}")
    with conn.cursor() as cur:
        lane_predicate = (
            "upper(coalesce(expected_edge->>'research_lane','')) = 'INTRADAY_EVENT'"
            if lane == "INTRADAY_EVENT" else
            "upper(coalesce(expected_edge->>'research_lane',"
            "'DAILY_CROSS_SECTIONAL')) = 'DAILY_CROSS_SECTIONAL'"
        )
        cur.execute(f"""
            select hypothesis_id::text from quant.hypotheses
             where lower(coalesce(expected_edge->>'type','')) = %s
               and lower(coalesce(expected_edge->>'universe_key',
                                  expected_edge->>'universe','')) = %s
               and {lane_predicate}
        """, (edge, uni))
        hyp_ids = [r[0] for r in cur.fetchall()]
        exact_primary: dict[str, str] = {}
        if signal_expr is not None:
            # jsonb equality ignores object key order.  Invalid expressions are left
            # to the canonical AST gate; history lookup must not silently repair one.
            ast_key = ("intraday_signal_expr" if lane == "INTRADAY_EVENT"
                       else "signal_expr")
            cur.execute(f"""
                select e.experiment_id::text, e.trial_family_id
                  from quant.experiments e
                 where e.config->'{ast_key}' = %s::jsonb
                   and (
                       e.status in ('QUEUED', 'RUNNING', 'COMPLETED')
                       or exists (
                            select 1 from quant.backtest_runs run
                             where run.experiment_id = e.experiment_id)
                       or exists (
                            select 1 from quant.experiment_metrics metric
                             where metric.experiment_id = e.experiment_id)
                       or exists (
                            select 1 from research.experiment_outcomes outcome
                             where outcome.experiment_id = e.experiment_id::text)
                       or exists (
                            select 1
                              from quant.intraday_experiment_rungs rung
                              join quant.intraday_session_accesses access
                                on access.experiment_rung_id =
                                   rung.experiment_rung_id
                             where rung.experiment_id = e.experiment_id)
                   )
            """, (json.dumps(signal_expr, sort_keys=True),))
            exact_primary = {str(r[0]): str(r[1] or "")
                             for r in cur.fetchall()}
        experiment_ids = list(exact_primary)
        if not hyp_ids and not experiment_ids:
            return []
        cur.execute(f"""
            select o.decision, o.lesson_codes, o.trial_family_id,
                   o.experiment_id
              from research.v_current_experiment_outcomes o
              join quant.experiments e
                on e.experiment_id::text = o.experiment_id
              join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
              join quant.dataset_manifests m on m.dataset_id = e.dataset_id
             where (o.hypothesis_id = any(%s::text[])
                or o.experiment_id = any(%s::text[]))
               and {_GOVERNED_PAST_OUTCOME}
             order by o.decided_at desc
        """, (hyp_ids, experiment_ids))
        exact = set(experiment_ids)
        rows = [{"decision": r[0], "lesson_codes": list(r[1] or []),
                 "trial_family_id": r[2], "experiment_id": str(r[3]),
                 "match_scope": ("AST_EXACT_PRIMARY" if str(r[3]) in exact
                                 else "EDGE_UNIVERSE")}
                for r in cur.fetchall()]
        settled = {r["experiment_id"] for r in rows if
                   r["match_scope"] == "AST_EXACT_PRIMARY"}
        # PRIMARY attempts remain in resource pressure even when their result
        # is pending or ineligible for promotion evidence.  They carry no
        # performance lesson or breeding authority.
        rows.extend({
            "decision": "PRIMARY_ATTEMPT",
            "lesson_codes": [],
            "trial_family_id": exact_primary[experiment_id],
            "experiment_id": experiment_id,
            "match_scope": "AST_EXACT_PRIMARY",
            "promotion_authority": False,
            "statistical_pressure_only": True,
        } for experiment_id in experiment_ids if experiment_id not in settled)
        return rows


_SQL_INSERT = """
insert into research.experiment_proposals
  (proposal_id, case_id, as_known_at, lead_ids, economic_rationale,
   counterparty, competing_explanation, competing_explanation_codes,
   skeptic_sign, edge_type, universe_key, label, baseline,
   falsification_tests, data_requirements, suggested_params,
   research_lane, semantic_plan,
   trial_budget, prior_check, source_reported_effect,
   planner_prompt_version, skeptic_prompt_version, status)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PUBLISHED')
on conflict (proposal_id) do nothing
returning proposal_id
"""


def persist(conn, proposals) -> tuple[int, int]:
    """(신규, 중복). 게이트를 통과한 것만 넣는다."""
    cur = conn.cursor()
    new = dup = 0
    for p in proposals:
        cur.execute(_SQL_INSERT, (
            p.proposal_id, p.case_id, p.as_known_at, list(p.lead_ids),
            p.economic_rationale, p.counterparty, p.competing_explanation,
            [c.value for c in p.competing_explanation_codes], p.skeptic_sign,
            p.edge_type, p.universe_key, p.label, p.baseline,
            list(p.falsification_tests),
            json.dumps(p.data_requirements.model_dump(mode="json")),
            json.dumps(p.suggested_params), p.research_lane.value,
            json.dumps(p.semantic_plan), p.trial_budget,
            json.dumps(p.prior_check.model_dump(mode="json")),
            json.dumps(p.source_reported_effect),
            getattr(p, "_planner_prompt", ""), getattr(p, "_skeptic_prompt", "")))
        if cur.fetchone():
            new += 1
        else:
            dup += 1
    conn.commit()
    return new, dup


# ── 자체 점검 ──────────────────────────────────────────────────────────────
_PLANNER = """TITLE: 주문흐름 불균형 반전
LEAD_IDS: lead_aaa
ECONOMIC_RATIONALE: 긴급 매수 주문의 불균형을 유동성 공급자가 흡수한 뒤 가격 압력이 되돌아온다.
COUNTERPARTY: 즉시 체결을 위해 스프레드와 가격충격을 지불하는 긴급 주문자.
EDGE_TYPE: liquidity_shock_reversal
UNIVERSE_KEY: krx_all
FALSIFICATION_TESTS: 국면 분해, 비용 민감도
DATA_TABLES: market_bars, microstructure_features
MIN_HISTORY_DAYS: 58
SUGGESTED_PARAMS: {"horizon_days":2,"top_n":20,"signal_expr":{"op":"neg","arg":{"op":"ts_mean","field":"order_flow_imbalance","n":3}}}
SOURCE_REPORTED_EFFECT: {"monthly_alpha_pct": -1.0, "market": "US"}

TITLE: 필수 항목 빠진 기획
LEAD_IDS: lead_bbb
ECONOMIC_RATIONALE: 뭔가 된다
"""

_SKEPTIC = """TITLE: 주문흐름 불균형 반전
COMPETING_EXPLANATION: 소형·저유동성 종목에 몰려 유동성 프리미엄일 수 있다.
COMPETING_CODES: LIQUIDITY_PREMIUM, DATA_MINING
VERDICT: PROCEED
"""


def _selfcheck() -> int:
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    from publish_gate import _mk_lead

    # 계약이 lead_id 를 출처 해시로 강제한다(임의 ID 금지). 그래서 리드를 먼저
    # 만들고 그 id 를 기획안 쪽에 끼워 넣는다 - 반대로 하면 계약이 막는다.
    lead = _mk_lead()
    leads = {lead.lead_id: lead}
    planner = _PLANNER.replace("lead_aaa", lead.lead_id)

    r = intake(planner, _SKEPTIC, case_id="c-1", planner_run="run-plan",
               skeptic_run="run-skeptic", leads=leads)

    check("기획안 1건 조립", len(r.proposals) == 1)
    check("필수 항목 없으면 반려",
          any("필수 항목" in x.reason for x in r.rejected))
    prop, gate = r.proposals[0]
    check("발행 게이트 통과", gate.ok or fails.append(f"blockers={gate.blockers}") or False)
    check("경쟁 설명 코드 2개", len(prop.competing_explanation_codes) == 2)
    check("회의론자 서명", prop.skeptic_sign == "run-skeptic")
    # 계약이 tables 를 튜플로 정규화한다(불변 - 발행 뒤 바뀌면 안 된다).
    check("데이터 요구", tuple(prop.data_requirements.tables) ==
          ("market_bars", "microstructure_features")
          and prop.data_requirements.min_history_days == 58)
    check("소스 수치 분리 보관",
          prop.source_reported_effect.get("monthly_alpha_pct") == -1.0)

    # 독립성: 같은 실행이 기획하고 서명하면 막힌다
    r2 = intake(planner, _SKEPTIC, case_id="c", planner_run="same",
                skeptic_run="same", leads=leads)
    check("자기 서명 차단",
          not r2.proposals and any("독립" in x.reason for x in r2.rejected))

    # 회의론자가 기각하면 발행 안 함
    r3 = intake(planner, _SKEPTIC.replace("PROCEED", "REJECT"), case_id="c",
                planner_run="p", skeptic_run="s", leads=leads)
    check("회의론자 기각 존중",
          not r3.proposals and any("통과시키지" in x.reason for x in r3.rejected))

    # 회의론자도 없고 기획자가 반대 가설도 안 썼으면 반려한다
    r4 = intake(planner, "", case_id="c", planner_run="p", skeptic_run="s",
                leads=leads)
    check("반대 가설 없으면 반려",
          any("반대 가설" in x.reason for x in r4.rejected))

    # 자기서명은 독립 검토가 아니다. 기획자가 경쟁 설명을 직접 썼더라도
    # 별도 회의론자 블록과 실행 ID가 없으면 접수하지 않는다.
    #   `_PLANNER` 는 블록이 둘이라(뒤엣것은 일부러 불완전) 끝에 덧붙이면 그
    #   불완전한 블록에 들어간다 - 첫 블록만 잘라 쓴다.
    _first = planner.split("TITLE:")[1]
    self_signed = ("TITLE:" + _first
                   + "\nCOMPETING_EXPLANATION: 단기 반전이 아니라 유동성 공급"
                     " 보상일 수 있다\nCOMPETING_CODES: DATA_MINING\n")
    r4b = intake(self_signed, "", case_id="c", planner_run="p", skeptic_run="s",
                 leads=leads)
    check("자기서명 접수 차단", not r4b.proposals)
    check("독립 worker 요구를 설명",
          any("독립 worker" in x.reason for x in r4b.rejected))

    # 근거 리드가 DB 에 없으면 게이트가 막는다
    r5 = intake(planner, _SKEPTIC, case_id="c", planner_run="p",
                skeptic_run="s", leads={})
    check("없는 리드 차단",
          r5.proposals and not r5.proposals[0][1].ok)

    # 같은 리드·엣지·유니버스는 같은 기획안
    check("기획안 id 결정론",
          proposal_id_for(["a", "b"], "MOMENTUM", "krx_all")
          == proposal_id_for(["b", "a"], "momentum", "krx_all"))
    rejected_material = {
        "suggested_params": {"max_drawdown_stop": 0.35, "top_n": 20},
        "trial_budget": 5,
    }
    repaired_material = {
        "suggested_params": {"max_drawdown_stop": -0.35, "top_n": 20},
        "trial_budget": 5,
    }
    check("교정 사전등록은 새 id",
          proposal_id_for(["a"], "momentum", "krx_all",
                          material=rejected_material)
          != proposal_id_for(["a"], "momentum", "krx_all",
                             material=repaired_material))
    lesson_material = {
        **repaired_material,
        "lessons_addressed": {"OVERFIT_PBO": "새 상태 조건을 사전등록한다"},
    }
    check("교훈 대응 교정은 새 id",
          proposal_id_for(["a"], "momentum", "krx_all",
                          material=repaired_material)
          != proposal_id_for(["a"], "momentum", "krx_all",
                             material=lesson_material))
    sidecar_a = {**repaired_material,
                 "suggested_params": {
                     **repaired_material["suggested_params"],
                     "screening_population": [{"ast_fingerprint": "a"}],
                     "screening_cohort_version": "cohort-a"}}
    same_contract_sidecar = {**repaired_material,
                 "suggested_params": {
                     **repaired_material["suggested_params"],
                     "screening_population": [{"ast_fingerprint": "b"}],
                     "screening_cohort_version": "cohort-a"}}
    check("sidecar 변화는 id 불변",
          proposal_id_for(["a"], "momentum", "krx_all", material=sidecar_a)
          == proposal_id_for(["a"], "momentum", "krx_all",
                             material=same_contract_sidecar))
    upgraded_contract = {**same_contract_sidecar,
                         "suggested_params": {
                             **same_contract_sidecar["suggested_params"],
                             "screening_cohort_version": "cohort-b"}}
    check("execution cohort version changes proposal id",
          proposal_id_for(["a"], "momentum", "krx_all", material=sidecar_a)
          != proposal_id_for(["a"], "momentum", "krx_all",
                             material=upgraded_contract))
    unordered_a = {
        **repaired_material,
        "falsification_tests": ["cost", "regime"],
        "data_requirements": {"tables": ["market_ticks", "market_quotes"]},
        "semantic_plan": {"context": ["OPEN", "TIGHT_SPREAD"],
                          "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"]},
    }
    unordered_b = {
        **repaired_material,
        "falsification_tests": ["regime", "cost"],
        "data_requirements": {"tables": ["market_quotes", "market_ticks"]},
        "semantic_plan": {"context": ["TIGHT_SPREAD", "OPEN"],
                          "qualities": ["STATE_CONDITIONAL", "PERSISTENCE"]},
    }
    check("집합 순서는 id 불변",
          proposal_id_for(["a"], "momentum", "krx_all", material=unordered_a)
          == proposal_id_for(["a"], "momentum", "krx_all", material=unordered_b))
    check("intraday cohort version is not a universe",
          _canonical_universe_key(INTRADAY_SCREENING_COHORT_VERSION,
                                  "INTRADAY_EVENT") == "krx_all"
          and _canonical_universe_key("unknown-intraday-universe",
                                      "INTRADAY_EVENT")
          == "unknown-intraday-universe"
          and _canonical_universe_key(INTRADAY_SCREENING_COHORT_VERSION,
                                      "DAILY_CROSS_SECTIONAL")
          == INTRADAY_SCREENING_COHORT_VERSION)

    # ▶ **접수가 계열 이력을 읽는다** (2026-08-13 실측 사고)
    #   `harvest` 가 `past_outcomes` 를 안 넘겨서 모든 기획안이 "이 계열은
    #   처음" 으로 접수됐고, 승격 관문만 진짜 이력을 봐서 영구 반려했다.
    #   기획자는 접수에서 들은 대로 했는데 승격에서 그 이유로 죽었다.
    seen = []

    def _rejected_family(block):
        seen.append(block)
        return [{"decision": "REJECTED", "lesson_codes": ["OVERFIT_DSR"]}]

    r6 = intake(planner, _SKEPTIC, case_id="c-6", planner_run="run-plan",
                skeptic_run="run-skeptic", leads=leads,
                outcomes_for=_rejected_family)
    check("접수가 기획안마다 계열 이력을 읽는다", bool(seen))
    # 좌표가 그대로 넘어가야 남의 계열 이력을 읽는 일이 없다
    check("좌표가 조회에 전달된다",
          any((b.get("EDGE_TYPE") or "").strip() == "liquidity_shock_reversal"
              and (b.get("UNIVERSE_KEY") or "").strip() == "krx_all"
              for b in seen))
    # 기각 이력이 있는데 대응이 없으면 **접수에서** 걸려야 한다(승격까지 가면
    # 늦다 - 그때는 기획자가 이미 다른 일을 하고 있고 카드도 닫혀 있다)
    check("기각 이력 + 무대응은 접수를 통과하지 못한다",
          bool(r6.rejected) or not any(g.ok for _, g in r6.proposals))

    # 이력을 못 읽으면 0 으로 넘기지 않는다 - 미측정과 "없음" 을 섞지 않는다
    def _boom(_b):
        raise RuntimeError("계열 조회 실패")

    r7 = intake(planner, _SKEPTIC, case_id="c-7", planner_run="run-plan",
                skeptic_run="run-skeptic", leads=leads, outcomes_for=_boom)
    check("이력을 못 읽으면 접수하지 않는다",
          not r7.proposals and any("못 읽었다" in x.reason for x in r7.rejected))

    # ▶ **제목이 조금 달라도 짝이 맞는다** (2026-08-13 실측 사고)
    #   에이전트가 MCP 로 12번 넘게 제출했고 매번 `회의론자 검토가 없다` 로
    #   거부됐다. 회의론자 텍스트는 냈는데 제목이 정확히 안 맞아 조인이
    #   깨진 것이었고, 사유가 "없다" 뿐이라 원인을 알 수 없었다.
    sk_dot = _SKEPTIC.replace("TITLE: 복권형 수익 회피",
                              'TITLE: "복권형 수익 회피".')
    r8 = intake(planner, sk_dot, case_id="c-8", planner_run="run-plan",
                skeptic_run="run-skeptic", leads=leads)
    check("제목 구두점·따옴표 차이로 안 깨진다", len(r8.proposals) == 1)
    check("정규화로 붙어도 독립 서명은 유지",
          bool(r8.proposals) and r8.proposals[0][0].skeptic_sign == "run-skeptic")

    # 하나:하나면 제목이 아주 달라도 모호하지 않다
    sk_other = _SKEPTIC.replace("TITLE: 주문흐름 불균형 반전", "TITLE: 전혀 다른 제목")
    r9 = intake(planner, sk_other, case_id="c-9", planner_run="run-plan",
                skeptic_run="run-skeptic", leads=leads)
    check("1:1 이면 제목이 달라도 짝짓는다", len(r9.proposals) == 1)

    # **여럿일 때는 위치로 잇지 않는다** - 남의 반대 가설로 통과시키면 안 된다
    two_sk = sk_other + "\n" + _SKEPTIC.replace("TITLE: 주문흐름 불균형 반전",
                                                "TITLE: 또 다른 제목")
    r10 = intake(planner, two_sk, case_id="c-10", planner_run="run-plan",
                 skeptic_run="run-skeptic", leads=leads)
    check("모호하면 짝짓지 않는다", not r10.proposals)
    check("반려문이 양쪽 제목을 보여 준다",
          any("회의론자 제목" in x.reason for x in r10.rejected))

    for f in fails:
        if f:
            print(f"  FAIL {f}")
    total = 26
    print(f"proposal_intake 자체 점검: {total - len([x for x in fails if x])}/{total} 통과")
    return 1 if [x for x in fails if x] else 0


def _cli(argv: list[str]) -> int:
    """python proposal_intake.py --planner a.txt --skeptic b.txt \
           --planner-run r1 --skeptic-run r2 [--dry-run]"""
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    plan_path = opt("--planner")
    if not plan_path:
        return _selfcheck()

    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
    from source_registry import load_project_env

    planner_text = Path(plan_path).read_text(encoding="utf-8")
    skeptic_text = Path(opt("--skeptic")).read_text(encoding="utf-8")
    case_id = opt("--case", f"plan-{datetime.now(timezone.utc):%Y%m%d}")

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        # 근거 리드는 DB 에서 읽는다 - 에이전트가 댄 id 를 그대로 믿지 않는다.
        wanted = {i for b in parse_blocks(planner_text, PLANNER_KEYS)
                  for i in _split(b.get("LEAD_IDS", ""))}
        leads = load_leads(conn, sorted(wanted))
        missing = wanted - set(leads)
        if missing:
            print(f"  ! DB 에 없는 리드: {', '.join(sorted(missing))}")

        r = intake(planner_text, skeptic_text, case_id=case_id,
                   planner_run=opt("--planner-run", ""),
                   skeptic_run=opt("--skeptic-run", ""), leads=leads)
        # ▶ **어떤 지시 아래 서명했는지를 남긴다** (2026-08-11 회고)
        #   1·2회차에 회의론자가 전부 기각하자 프롬프트를 바꿔 3회차에 통과시켰다.
        #   서명자(skeptic_sign)만 남기면 그 조율이 원장에 안 잡히고, 그러면
        #   프롬프트를 갈아가며 통과할 때까지 돌릴 수 있다 - 게이트가 아니라 장식이다.
        pp, sp = opt("--planner-prompt", ""), opt("--skeptic-prompt", "")
        if not sp:
            print("  ! --skeptic-prompt 가 없다. 판정 근거를 못 남긴다")
        for prop, _g in r.proposals:
            object.__setattr__(prop, "_planner_prompt", pp) if hasattr(prop, "__slots__")                 else setattr(prop, "_planner_prompt", pp)
            setattr(prop, "_skeptic_prompt", sp)

        print(f"{MODULE_VERSION}: 조립 {len(r.proposals)} / 반려 {len(r.rejected)}")
        for x in r.rejected:
            print(f"  - {x.title[:44]}: {x.reason}")
        for p, g in r.proposals:
            mark = "통과" if g.ok else "차단"
            print(f"  [{mark}] {p.proposal_id}  {p.edge_type}/{p.universe_key}")
            for b in g.blockers:
                print(f"      X {b}")
            for w in g.warnings:
                print(f"      ! {w}")

        pub = r.publishable
        if "--dry-run" in argv or not pub:
            print(f"  (적재하지 않았다 - 발행 가능 {len(pub)}건)")
            return 0
        new, dup = persist(conn, pub)
        print(f"  적재: 신규 {new} / 중복 {dup}")
    finally:
        conn.close()
    return 0


def _check_trailing_prose_does_not_kill_codes():
    """**마무리 문장 한 줄 때문에 멀쩡한 기획안을 버리지 않는다.** (2026-08-12 실측)

    parse_blocks 가 형식 밖 줄을 앞 필드에 이어붙이므로 COMPETING_CODES 값에
    산문이 흡수된다. 코드가 살아 있으면 살린다.
    """
    got, stray = _competing_codes(
        "COST_UNACCOUNTED 제출 형식에 맞춘 모멘텀 실험 기획안 1건을 작성했다.")
    assert got == [CompetingExplanation.COST_UNACCOUNTED], got
    assert "제출" in stray, stray

    # 여러 코드 + 쉼표도 그대로 산다. 중복은 한 번만.
    got2, _ = _competing_codes("DATA_MINING, BETA_EXPOSURE, DATA_MINING")
    assert got2 == [CompetingExplanation.DATA_MINING,
                    CompetingExplanation.BETA_EXPOSURE], got2

    # **코드가 하나도 없으면 살리지 않는다** - 그때는 진짜 어휘 문제다.
    got3, stray3 = _competing_codes("REGIME_ARTIFACT 라고 생각한다")
    assert got3 == [], got3
    assert "REGIME_ARTIFACT" in stray3, stray3
    got4, _ = _competing_codes("")
    assert got4 == [], got4


def _check_stored_long_excerpt_does_not_kill_harvest():
    """**원장에 이미 들어간 긴 발췌가 수확을 죽이지 않는다.**

    2026-08-12: `to_lead` 가 EXCERPT 없을 때 MECHANISM 으로 대체하면서 500자를
    넘겼고 그대로 적재됐다. 그 뒤 `load_leads` 가 그 행을 읽을 때마다 계약 검증이
    터져 **그 배치 전체**가 버려졌다 - 같은 카드가 매 주기 같은 자리에서 죽었다.
    적재 쪽을 고쳐도 이미 들어간 행은 남으므로 읽는 경계에서도 맞춘다.
    """
    from factory_contracts import (  # noqa: PLC0415
        MAX_EXCERPT_CHARS, lead_id_for)

    ref = {"url": "https://example.org/p", "title": "t",
           "accessed_at": "2026-08-11T04:46:29Z", "excerpt": "사" * 900}
    d = {"case_id": "c1", "scout_lens": "ACADEMIC", "source_type": "PAPER",
         "as_known_at": "2026-08-11T04:46:29Z", "refs": [ref],
         "claimed_edge": "x", "stated_mechanism": "m", "inferred": False,
         "market_context": "", "stated_failure_mode": "",
         "independent_mentions": 1, "testability": "VAGUE", "status": "COMPLETE",
         "model_version": "m1", "prompt_version": "p1"}

    # 자르기 전에는 실제로 발췌 때문에 터진다 - 이 검사가 헛돌지 않게 확인한다
    try:
        MethodologyLeadV1.model_validate({**d, "lead_id": lead_id_for(d["refs"])})
        raise AssertionError("긴 발췌가 그냥 통과했다 - 계약 상한이 사라졌나")
    except Exception as e:  # noqa: BLE001
        assert "excerpt" in str(e), str(e)[:120]

    ref["excerpt"] = clip_excerpt(ref["excerpt"])
    assert len(ref["excerpt"]) <= MAX_EXCERPT_CHARS
    MethodologyLeadV1.model_validate({**d, "lead_id": lead_id_for(d["refs"])})


def _check_agent_can_answer_the_gate():
    """**게이트가 요구하는 대응을 에이전트가 적을 수 있어야 한다.** (2026-08-12)

    Gate 0 는 "이 계열에서 이미 기각됐다 - 그 교훈에 대응이 없다" 로 막았는데,
    기획자 서식에 대응을 적을 칸이 없었다. 에이전트는 산문에 적었고 게이트는
    구조화된 필드를 봤다. 결과: **기각된 계열은 영원히 재제안 불가** -
    배분자가 "찾은 것을 밀어붙여라" 해도 접수에서 죽었다.
    """
    # ① 서식에 칸이 있어야 한다
    import factory_autopilot as fa  # noqa: PLC0415

    assert "LESSONS_ADDRESSED:" in fa.PLANNER_FORMAT, \
        "기획자 서식에 대응을 적을 칸이 없다 - 게이트가 답할 수 없는 것을 묻는다"

    # ①-b **서식이 말하는 칸은 파서도 알아야 한다** (2026-08-13 실측)
    #   이 검사가 없어서 `LESSONS_ADDRESSED` 가 서식에는 있고 `PLANNER_KEYS`
    #   에는 없는 상태로 남아 있었다. `parse_blocks` 는 모르는 키를 **앞 필드에
    #   이어붙이므로**, 에이전트가 서식대로 쓰면 `TRIAL_BUDGET` 이 오염돼
    #   `trial_budget 이 숫자가 아니다: '5 LESSONS_ADDRESSED: …'` 로 죽는다.
    #   즉 **서식을 고칠수록 나빠지는** 상태였다. 한 칸을 놓치면 그 앞 칸까지
    #   같이 잃으므로 개별이 아니라 전수로 본다.
    fmt_fields = set(re.findall(r"^([A-Z][A-Z0-9_]{2,}):", fa.PLANNER_FORMAT,
                                re.M))
    unknown = sorted(f for f in fmt_fields if f not in PLANNER_KEYS)
    assert not unknown, (
        f"서식이 말하는 칸을 파서가 모른다: {unknown} - `parse_blocks` 가 "
        f"모르는 키를 앞 필드에 이어붙여 그 필드까지 망친다. "
        f"PLANNER_KEYS 에 넣어라")
    assert fmt_fields, "서식에서 칸을 하나도 못 찾았다 - 이 검사가 헛돈다"

    # ②-a 파서가 실제로 그 칸을 독립 필드로 잡는가(앞 필드가 안 망가지는가)
    _blk = parse_blocks(
        "TITLE: t\nTRIAL_BUDGET: 5\n"
        "LESSONS_ADDRESSED: BEAR_FRAGILE=낙폭 정지를 건다\n", PLANNER_KEYS)
    assert _blk and _blk[0].get("TRIAL_BUDGET", "").strip() == "5", \
        f"앞 필드가 오염됐다: {_blk}"
    assert "BEAR_FRAGILE" in _blk[0].get("LESSONS_ADDRESSED", ""), _blk

    # ② 그 칸을 파싱해야 한다. 값 안의 쉼표가 대응을 반토막 내면 안 된다
    got = _lessons_addressed(
        "SINGLE_REGIME_ONLY=국면 필터를 빼고, 전 기간을 쓴다, "
        "BEAR_FRAGILE=낙폭 정지를 -0.28 로 건다")
    assert set(got) == {"SINGLE_REGIME_ONLY", "BEAR_FRAGILE"}, got
    assert got["SINGLE_REGIME_ONLY"] == "국면 필터를 빼고, 전 기간을 쓴다", got
    got2 = _lessons_addressed(
        "BASELINE_NOT_BEATEN=공개 기준선을 OOS 비용 후 이긴다; "
        "UNDERPOWERED_DATA=워밍업을 6일로 고정해 4창을 확보한다")
    assert set(got2) == {"BASELINE_NOT_BEATEN", "UNDERPOWERED_DATA"}, got2
    repeated = parse_blocks(
        "TITLE: t\nLESSONS_ADDRESSED: BASELINE_NOT_BEATEN=기준선 대조\n"
        "LESSONS_ADDRESSED: UNDERPOWERED_DATA=4창 확보\n", PLANNER_KEYS)
    assert _lessons_addressed(repeated[0]["LESSONS_ADDRESSED"]) == {
        "BASELINE_NOT_BEATEN": "기준선 대조",
        "UNDERPOWERED_DATA": "4창 확보",
    }, repeated
    assert _lessons_addressed("") == {} and _lessons_addressed("아무말") == {}

    # ③ 이력은 원장에서 오고 대응은 에이전트에게서 온다
    past = [{"decision": "REJECT", "lesson_codes": ["SINGLE_REGIME_ONLY"],
             "trial_family_id": "fam_x"}]
    pc = _prior_check(past, "SINGLE_REGIME_ONLY=국면 필터를 뺀다")
    assert pc.trials_used == 1 and pc.past_outcomes == ("SINGLE_REGIME_ONLY",)
    assert pc.lessons_addressed == {"SINGLE_REGIME_ONLY": "국면 필터를 뺀다"}
    assert pc.trial_family_id == "fam_x"

    # ④ **에이전트가 쓴 것을 접수기가 거르지 않는다.** 층마다 볼 것이 다르다 -
    #    계약은 형식(어휘·빈 대응), Gate 0 은 실질(맞는 교훈인가). 여기서
    #    미리 걸렀더니 대응 맵이 비어 계약이 엉뚱한 사유로 죽었다(실측).
    pc2 = _prior_check(past, "OVERFIT_PBO=PBO 를 재려고 변형을 4개 낸다")
    assert pc2.lessons_addressed == {"OVERFIT_PBO": "PBO 를 재려고 변형을 4개 낸다"}

    # ⑤ 이력이 없으면 계약은 대응을 요구하지 않는다
    pc3 = _prior_check([], "")
    assert pc3.past_outcomes == () and pc3.trials_used == 0

    # ⑥ 대응이 비면 계약이 거부해야 한다 - 견제가 살아 있는지 확인
    try:
        PriorCheck(trial_family_id="f", trials_used=1,
                   past_outcomes=("SINGLE_REGIME_ONLY",), lessons_addressed={})
        raise AssertionError("기각 이력이 있는데 대응 없이 통과했다")
    except Exception as e:  # noqa: BLE001 - pydantic 은 ValidationError 로 감싼다
        assert "대응" in str(e), e
    print("  에이전트가 게이트에 답할 수 있다  OK")


def _check_lane_isolated_history_lookup():
    """Daily outcomes must not spend an intraday family's trial budget."""
    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            sql, params = self.calls[-1]
            if "from quant.hypotheses" in sql:
                assert "= 'INTRADAY_EVENT'" in sql, sql
                return []
            if "from quant.experiments" in sql:
                assert "config->'intraday_signal_expr'" in sql, sql
                assert len(params) == 1, params
                return []
            raise AssertionError(sql)

    class Conn:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

    rows = load_past_outcomes(
        Conn(), "order_flow_imbalance", "krx_all",
        signal_expr={"op": "field", "field": "queue_imbalance_l1"},
        research_lane="INTRADAY_EVENT")
    assert rows == []


def _check_intraday_screening_cohort_is_sourced_and_non_promoting():
    """Linked current-contract formulas become exact, non-consumed sidecars."""
    import inspect

    from factory_contracts import SourceRef, lead_id_for
    from publish_gate import check_intraday_screening_population

    assert "p.status in ('PUBLISHED','ACCEPTED')" in inspect.getsource(load_leads), \
        "REJECTED primary lead를 sidecar pool에서도 영구 소비하면 교정 재제안이 막힌다"
    assert ("proposal_review_outcomes" in inspect.getsource(load_leads)
            and "r.verdict = 'STOP'" in inspect.getsource(load_leads)), \
        "독립 스켑틱 STOP primary를 sidecar 후보로 다시 쓰면 안 된다"
    assert "feature_window_contract_version" in inspect.getsource(load_leads), \
        "legacy formula를 current V2 screening pool에 자동 부착한다"

    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "REVERT",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    raw_microprice = {"op": "field", "field": "microprice_offset_bps"}
    primary_expr = {"op": "rolling_mean", "seconds": 10,
                    "arg": raw_microprice}
    side_expr = {"op": "rolling_mean", "seconds": 30,
                 "arg": primary_expr}

    def make_lead(label: str, expr: dict) -> MethodologyLeadV1:
        ref = SourceRef(url=f"https://example.com/{label}", title=label,
                        accessed_at=now, excerpt="bounded excerpt")
        return MethodologyLeadV1(
            lead_id=lead_id_for([ref]), case_id="cohort-check",
            scout_lens="ACADEMIC", source_type="PAPER", as_known_at=now,
            refs=(ref,), claimed_edge=label, stated_mechanism="mechanism",
            ast_contract={
                "formula_discovery_version": "formula-discovery-v5",
                "feature_window_contract_version":
                    CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT,
                "formula_contract_complete": True,
                "alpha_candidate_eligible": True,
                "research_lane": "INTRADAY_EVENT",
                "candidate_signal_expr": expr, "semantic_plan": plan,
                "formula_thesis": {
                    "target": "TAKER_NET_PNL",
                    "functional_form": "MONOTONE",
                    "expected_sign": "STATE_DEPENDENT",
                    "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
                    "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
                    "terms": {"microprice_offset_bps": "PRESSURE"},
                    "identification": (
                        "Microprice displacement must predict positive net markout "
                        "after the preregistered execution cost hurdle."),
                },
                "evolution_role": "SEED",
            })

    primary_lead = make_lead("primary", primary_expr)
    side_lead = make_lead("side", side_expr)
    leads = {row.lead_id: row for row in (primary_lead, side_lead)}
    proposal = ExperimentProposalV1(
        proposal_id="before", case_id="cohort-check", as_known_at=now,
        # Reproduce the live failure mode: the planner linked only its primary,
        # while the intake loader supplied another unused current-contract formula.
        lead_ids=(primary_lead.lead_id,),
        economic_rationale="quote dislocation meets urgent liquidity demand",
        counterparty="urgent liquidity taker",
        competing_explanation="data mining",
        competing_explanation_codes=(CompetingExplanation.DATA_MINING,),
        skeptic_sign="independent-worker", edge_type="order_flow_imbalance",
        universe_key="krx_all", falsification_tests=("net <= 0",),
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"), min_history_days=60),
        suggested_params={
            "intraday_signal_expr": primary_expr, "horizon_seconds": 5,
            "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "feature_window_contract_version":
                CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT},
        research_lane="INTRADAY_EVENT", semantic_plan=plan)
    attached = _attach_intraday_screening_cohort(proposal, leads)
    assert attached.lead_ids == (primary_lead.lead_id,)
    population = attached.suggested_params["screening_population"]
    assert len(population) == 2
    assert population[0]["source_lead_ids"] == [side_lead.lead_id]
    assert population[1]["candidate_role"] == "STRUCTURAL_ABLATION"
    assert population[1]["ablation_operator"] == "REMOVE_TEMPORAL_TRANSFORM"
    assert population[1]["source_lead_ids"] == [primary_lead.lead_id]
    assert check_intraday_screening_population(attached, leads) == []

    corrupt = dict(population[0])
    corrupt["ast_fingerprint"] = "wrong"
    tampered = attached.model_copy(update={
        "suggested_params": {**attached.suggested_params,
                             "screening_population": [corrupt]}})
    assert any("fingerprint" in error for error in
               check_intraday_screening_population(tampered, leads))

    false_control = dict(population[1])
    false_control["ablation_operator"] = "FABRICATED_CONTROL"
    tampered_control = attached.model_copy(update={
        "suggested_params": {**attached.suggested_params,
                             "screening_population": [false_control]}})
    assert any("does not match" in error for error in
               check_intraday_screening_population(tampered_control, leads))


if __name__ == "__main__":
    if "--check" in sys.argv:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
        _check_stored_long_excerpt_does_not_kill_harvest()
        print("  저장된 긴 발췌가 수확을 안 죽인다  OK")
        _check_trailing_prose_does_not_kill_codes()
        print("  코드 뒤 산문이 기획안을 안 죽인다  OK")
        _check_agent_can_answer_the_gate()
        _check_lane_isolated_history_lookup()
        _check_intraday_screening_cohort_is_sourced_and_non_promoting()
        print("  공유 재생 cohort 출처·비승격 계약  OK")
        raise SystemExit(0)
    raise SystemExit(_cli(sys.argv[1:]))
