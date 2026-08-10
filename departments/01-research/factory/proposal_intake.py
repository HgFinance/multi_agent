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
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts.factory_contracts import (  # noqa: E402
    CompetingExplanation, DataRequirement, ExperimentProposalV1,
    MethodologyLeadV1, PriorCheck,
)
from lead_intake import parse_blocks  # noqa: E402
from publish_gate import evaluate  # noqa: E402

MODULE_VERSION = "research-proposal-intake-v1"

PLANNER_KEYS = ("TITLE", "LEAD_IDS", "ECONOMIC_RATIONALE", "COUNTERPARTY",
                "EDGE_TYPE", "UNIVERSE_KEY", "LABEL", "BASELINE",
                "FALSIFICATION_TESTS", "DATA_TABLES", "MIN_HISTORY_DAYS",
                "SUGGESTED_PARAMS", "SOURCE_REPORTED_EFFECT", "TRIAL_BUDGET")
SKEPTIC_KEYS = ("TITLE", "COMPETING_EXPLANATION", "COMPETING_CODES", "VERDICT")

PLANNER_REQUIRED = ("TITLE", "LEAD_IDS", "ECONOMIC_RATIONALE", "COUNTERPARTY",
                    "EDGE_TYPE", "UNIVERSE_KEY")
SKEPTIC_PASS = "PROCEED"          # 회의론자가 본가설을 살려 둔 경우만


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


def _maybe_json(v: str) -> dict:
    """dict 로 읽히면 dict, 아니면 빈 dict. **문자열을 억지로 끼우지 않는다.**"""
    try:
        d = json.loads(str(v or "").strip() or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


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


def proposal_id_for(lead_ids, edge_type: str, universe_key: str) -> str:
    """같은 리드로 같은 엣지·유니버스를 또 기획하면 같은 기획안이다."""
    blob = json.dumps([sorted(str(x) for x in lead_ids),
                       edge_type.strip().lower(), universe_key.strip().lower()],
                      ensure_ascii=False, separators=(",", ":"))
    return "prop_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build(planner: dict, skeptic: dict, *, case_id: str,
          planner_run: str, skeptic_run: str,
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

    codes = []
    for c in _split(skeptic.get("COMPETING_CODES", "")):
        try:
            codes.append(CompetingExplanation(c.upper()))
        except ValueError:
            # 어휘 밖 코드는 조용히 버리지 않는다 - 게이트가 막을 수 있게 남긴다.
            raise ValueError(f"경쟁 설명 코드가 어휘 밖이다: {c}") from None

    lead_ids = _split(planner["LEAD_IDS"])
    edge = planner["EDGE_TYPE"].strip().lower()
    universe = planner["UNIVERSE_KEY"].strip()

    tables = _split(planner.get("DATA_TABLES", "")) or ("market_bars",)
    try:
        min_days = int(str(planner.get("MIN_HISTORY_DAYS", "")).strip() or 0)
    except ValueError:
        min_days = 0

    return ExperimentProposalV1(
        proposal_id=proposal_id_for(lead_ids, edge, universe),
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
        label=(planner.get("LABEL") or "forward_return").strip(),
        baseline=(planner.get("BASELINE") or "equal_weight_buy_and_hold").strip(),
        falsification_tests=_split(planner.get("FALSIFICATION_TESTS", "")),
        data_requirements=DataRequirement(tables=list(tables),
                                          min_history_days=min_days),
        suggested_params=_maybe_json(planner.get("SUGGESTED_PARAMS", "")),
        trial_budget=_trial_budget(planner.get("TRIAL_BUDGET", "")),
        prior_check=PriorCheck(),
        source_reported_effect=_maybe_json(
            planner.get("SOURCE_REPORTED_EFFECT", "")),
    )


def intake(planner_text: str, skeptic_text: str, *, case_id: str,
           planner_run: str, skeptic_run: str,
           leads: dict | None = None, past_outcomes: list | None = None,
           as_known_at: datetime | None = None) -> Intake:
    """기획자·회의론자 산출을 짝지어 조립하고 발행 게이트에 태운다."""
    out = Intake()
    skeptics = {(b.get("TITLE") or "").strip(): b
                for b in parse_blocks(skeptic_text, SKEPTIC_KEYS)}

    for p in parse_blocks(planner_text, PLANNER_KEYS):
        title = (p.get("TITLE") or "(제목 없음)").strip()
        missing = [k for k in PLANNER_REQUIRED if not (p.get(k) or "").strip()]
        if missing:
            out.rejected.append(Rejected(title, f"필수 항목 없음: {','.join(missing)}"))
            continue
        s = skeptics.get(title)
        if s is None:
            # 짝이 없으면 회의론자를 안 거친 것이다. 통과시키면 서명이 무의미해진다.
            out.rejected.append(Rejected(title, "회의론자 검토가 없다"))
            continue
        try:
            prop = build(p, s, case_id=case_id, planner_run=planner_run,
                         skeptic_run=skeptic_run, as_known_at=as_known_at)
        except Exception as e:          # 계약 위반·독립성 위반 모두 여기로
            out.rejected.append(Rejected(title, str(e)))
            continue
        gate = evaluate(prop, leads=leads or {}, past_outcomes=past_outcomes or [])
        out.proposals.append((prop, gate))
    return out


# ── DB ─────────────────────────────────────────────────────────────────────
def load_leads(conn, lead_ids) -> dict:
    """근거 리드를 **DB 에서** 읽는다. 에이전트가 말한 리드를 그대로 믿지 않는다."""
    if not lead_ids:
        return {}
    cur = conn.cursor()
    cur.execute("""
        select lead_id, case_id, scout_lens, source_type, as_known_at, refs,
               claimed_edge, stated_mechanism, inferred, market_context,
               stated_failure_mode, independent_mentions, testability, status,
               model_version, prompt_version
          from research.methodology_leads where lead_id = any(%s)
    """, (list(lead_ids),))
    cols = ("lead_id", "case_id", "scout_lens", "source_type", "as_known_at",
            "refs", "claimed_edge", "stated_mechanism", "inferred",
            "market_context", "stated_failure_mode", "independent_mentions",
            "testability", "status", "model_version", "prompt_version")
    out = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        if isinstance(d["refs"], str):
            d["refs"] = json.loads(d["refs"])
        out[d["lead_id"]] = MethodologyLeadV1.model_validate(d)
    return out


def load_past_outcomes(conn, edge_type: str, universe_key: str) -> list[dict]:
    """같은 계열의 지난 판정. 기각 교훈에 대응이 없으면 게이트가 막는다."""
    cur = conn.cursor()
    cur.execute("""
        select decision, lesson_codes, trial_family_id
          from research.experiment_outcomes
         where coalesce(edge_type,'') = %s and coalesce(universe_key,'') = %s
    """, (edge_type, universe_key))
    return [{"decision": r[0], "lesson_codes": list(r[1] or []),
             "trial_family_id": r[2]} for r in cur.fetchall()]


_SQL_INSERT = """
insert into research.experiment_proposals
  (proposal_id, case_id, as_known_at, lead_ids, economic_rationale,
   counterparty, competing_explanation, competing_explanation_codes,
   skeptic_sign, edge_type, universe_key, label, baseline,
   falsification_tests, data_requirements, suggested_params,
   trial_budget, prior_check, source_reported_effect,
   planner_prompt_version, skeptic_prompt_version, status)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PUBLISHED')
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
            json.dumps(p.suggested_params), p.trial_budget,
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
_PLANNER = """TITLE: 복권형 수익 회피
LEAD_IDS: lead_aaa
ECONOMIC_RATIONALE: 복권형 payoff 선호로 극단 상승 종목이 과대평가되고,
그 대가로 다음 달 수익이 낮다.
COUNTERPARTY: 분산이 덜 된 개인이 복권형 종목에 프리미엄을 지불한다.
EDGE_TYPE: mean_reversion
UNIVERSE_KEY: krx_all
FALSIFICATION_TESTS: 국면 분해, 비용 민감도
DATA_TABLES: market_bars
MIN_HISTORY_DAYS: 400
SOURCE_REPORTED_EFFECT: {"monthly_alpha_pct": -1.0, "market": "US"}

TITLE: 필수 항목 빠진 기획
LEAD_IDS: lead_bbb
ECONOMIC_RATIONALE: 뭔가 된다
"""

_SKEPTIC = """TITLE: 복권형 수익 회피
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
    check("데이터 요구", tuple(prop.data_requirements.tables) == ("market_bars",)
          and prop.data_requirements.min_history_days == 400)
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

    # 회의론자 검토가 없으면 짝이 안 맞는다
    r4 = intake(planner, "", case_id="c", planner_run="p", skeptic_run="s",
                leads=leads)
    check("검토 없으면 반려",
          any("회의론자 검토가 없다" in x.reason for x in r4.rejected))

    # 근거 리드가 DB 에 없으면 게이트가 막는다
    r5 = intake(planner, _SKEPTIC, case_id="c", planner_run="p",
                skeptic_run="s", leads={})
    check("없는 리드 차단",
          r5.proposals and not r5.proposals[0][1].ok)

    # 같은 리드·엣지·유니버스는 같은 기획안
    check("기획안 id 결정론",
          proposal_id_for(["a", "b"], "MOMENTUM", "krx_all")
          == proposal_id_for(["b", "a"], "momentum", "krx_all"))

    for f in fails:
        if f:
            print(f"  FAIL {f}")
    total = 12
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


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
