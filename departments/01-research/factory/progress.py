"""공장 전진 - **"돌릴수록 똑똑해지는가" 를 잰다.**

담당: 재일 (리서치본부 RSH / 퀀트·백테스트본부 QNT 공용)

▶ 왜 (2026-08-13, 문헌 + 실측)
  안쪽 고리에는 목적함수가 있다 - 관문 8조항이 "이 전략이 좋은가" 를 판정한다.
  **바깥 고리에는 없었다.** 공장 개선을 이끄는 신호가 `병목 개수` 뿐인데,
  병목이 0이 돼도 좋은 전략이 나온다는 보장이 없다. 건전한 채로 아무것도
  못 찾는 공장이 가능하다. 즉 **자기개선을 주장하는데 그걸 재는 자가 없었다.**

  Bloom·Jones·Van Reenen(AER 2020)이 왜 이게 치명적인지 말한다: 연구 노력은
  급증하는데 생산성은 급락한다 - 무어의 법칙 한 번 배가에 필요한 연구자가
  1970년대 초의 **18배**다. **"돌릴수록 똑똑해진다" 는 기본값이 아니라 중력을
  거스르는 것이다.** 기본값은 반대다. 재지 않으면 거스르고 있는지 알 수 없다.

▶ 무엇을 재나 - ANNECS (Enhanced POET, Wang et al. 2020)
  개방형 시스템의 전진을 재는 도메인 무관 지표. **누적된, 새롭고 실제로 풀린
  도전의 수**다. 두 관문을 통과해야 계수된다:
    ① 최소 기준 - 지금까지의 모든 시도 대비 새로워야 한다(너무 쉽지도
       어렵지도 않게). 재탕은 0점이다.
    ② 결국 풀려야 한다 - **불가능한 것을 만들어 낸 데는 점수를 주지 않는다.**

  우리 번역:
    도전 = 계열(trial_family)          풀림 = 판정까지 감(experiment_outcomes)
    너무 어려움 = 실행조차 못 함(NOT_RUNNABLE) -> 0점
    재탕 = 이미 개척된 계열            -> 0점

  이 곡선이 평평해지면 공장은 돌고 있어도 **제자리**다.

▶ 붕괴 신호 - MAD (Alemohammad et al., Self-Consuming Generative Models Go MAD)
  자기 산출만 먹고 도는 루프는 분포의 꼬리가 깎여 나간다. 결정적인 것:
  **고정된 실물 데이터는 붕괴를 늦출 뿐 막지 못한다** - 매 세대 새 실물
  정보가 들어와야 한다. 우리 공장에서 그 유입구가 스카우트(신규 문헌)다.
  그래서 스카우트는 편의 기능이 아니라 **붕괴 방지 장치**다.

  ▶ **처음 재 보고 내 가정이 틀린 것을 확인했다** (2026-08-13)
    `momentum` 계열 판정 4건이 전부 같은 교훈 코드로 끝나는 것을 보고 "꼬리가
    깎이는 중" 이라고 의심했는데, 전체 분포를 재니 정규화 엔트로피 **0.94**
    였다(상위 코드가 7·6·6·6 으로 거의 고르다). 붕괴 중이 아니다.
    계열 안에서 같은 코드가 반복되는 것과 **공장 전체의 다양성이 죽는 것은
    다른 사건**이다 - 지표가 그 둘을 갈라 줬다. 이 줄을 남겨 둔다: 재기 전의
    직감이 틀렸다는 기록이 없으면 다음에 또 같은 직감으로 엉뚱한 데를 고친다.

▶ 이 모듈은 판정만 한다. 못 재면 빈 값이다 - 재지 못한 것을 "전진 중" 으로
  적으면 그게 제일 나쁘다(미측정 != 0).

자체 점검: python departments/01-research/factory/progress.py
"""

from __future__ import annotations

import hashlib
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "04-quant-backtest" / "pipeline"))

from stock_universe import governed_stock_evidence_sql  # noqa: E402

MODULE_VERSION = "factory-progress-v1"
_GOVERNED_PROGRESS_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

# ── 판정자 버전 ──────────────────────────────────────────────────────────────
# ▶ **측정기를 고쳐서 성적을 올리는 길을 막는다** (2026-08-13, DGM 실측)
#   Darwin Gödel Machine(Sakana, ICLR 2026)에서 자기수정 에이전트가 실제로
#   **부정행위를 검사하는 프로세스 자체를 수정**했다 - 환각 탐지 함수가 찾는
#   표식을 로깅에서 지워 감시자를 눈멀게 했다. 논문의 문장이 정확하다:
#   *"수정 루틴 자체가 가변이 되는 순간 안전 제약도 다른 코드와 똑같은 진화
#   압력을 받는다."*
#
#   우리는 에이전트에게 "실행면을 진화시켜도 된다" 고 허락해 놨고, 오늘
#   바깥 고리에 목적함수를 만들었다. 즉 **ANNECS 를 올리는 가장 싼 방법이
#   이 파일을 고치는 것**이 됐다. 어제까지는 해킹할 목적함수가 없어서 이
#   위험이 없었다.
#
#   막는 방법은 "고치지 마라" 가 아니다 - 고쳐도 되고 고쳐야 한다. 대신
#   **판정자가 바뀌면 성적표에 그 사실이 같이 찍히게** 한다. 실험이
#   `input_hash` 로 재료 변경을 드러내는 것과 같은 장치다. 판정자 버전이
#   다른 두 전진 수치는 비교 대상이 아니고, 곡선이 오른 게 아니라 **자가
#   불연속**이다.
_JUDGE_FILES = ("progress.py", "soundness.py", "attribution.py")


def judge_version() -> str:
    """이 성적을 낸 **판정자들의 해시.** 못 읽으면 빈 문자열(지어내지 않는다)."""
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    seen = 0
    for name in _JUDGE_FILES:
        p = here / name
        try:
            h.update(p.read_bytes())
            seen += 1
        except OSError:
            continue
    return f"judge_{h.hexdigest()[:12]}({seen}/{len(_JUDGE_FILES)})" if seen else ""

# 신선도 창. 이 안에 새 리드가 0이면 공장은 자기 산출만 먹고 도는 중이다.
FRESH_DAYS = 7
# 이 창 안에 개척이 0이면 전진이 멈춘 것으로 본다.
PIONEER_WINDOW_DAYS = 3
# 교훈 분포가 이보다 집중되면 꼬리가 깎이는 중으로 본다(정규화 엔트로피).
COLLAPSE_ENTROPY = 0.6


@dataclass
class Progress:
    """공장이 나아가고 있는가. **전부 원장에서 세어 나온 값이다.**"""

    pioneered: int = 0          # ANNECS - 판정까지 간 서로 다른 계열 수(누적)
    recent_pioneered: int = 0   # 최근 창에서 새로 개척한 수
    attempts: int = 0           # 총 실험 시도
    wasted: int = 0             # 실행조차 못 한 계열(불가능한 도전 - 0점)
    fresh_leads: int = 0        # 최근 창에 들어온 새 외부 정보
    lesson_entropy: float | None = None   # 교훈 분포의 정규화 엔트로피
    lesson_modes: list = field(default_factory=list)
    stations: list = field(default_factory=list)   # 공정별 수율
    judge: str = ""             # 이 성적을 낸 판정자 해시
    measured: list = field(default_factory=list)   # 실제로 잰 항목

    @property
    def yield_per_attempt(self) -> float | None:
        """시도당 개척. **BJVR 이 말한 연구 생산성이다** - 떨어지면 비싸지는 중."""
        if not self.attempts:
            return None
        return self.pioneered / self.attempts

    @property
    def advancing(self) -> bool:
        """최근 창에서 실제로 새 땅을 밟았나."""
        return self.recent_pioneered > 0

    @property
    def starving(self) -> bool:
        """외부 정보가 끊겼나. **고정 데이터는 붕괴를 늦출 뿐이다.**"""
        return "fresh_leads" in self.measured and self.fresh_leads == 0

    @property
    def collapsing(self) -> bool:
        """교훈이 몇 개 모드로 수렴하는가 - 꼬리 소실의 초기 신호."""
        return (self.lesson_entropy is not None
                and self.lesson_entropy < COLLAPSE_ENTROPY)

    def verdict(self) -> dict:
        return {"advancing": self.advancing,
                "starving": self.starving,
                "collapsing": self.collapsing,
                "pioneered": self.pioneered,
                "recent_pioneered": self.recent_pioneered,
                "attempts": self.attempts,
                "wasted": self.wasted,
                "yield_per_attempt": self.yield_per_attempt,
                "lesson_entropy": self.lesson_entropy,
                "measured": sorted(self.measured)}


def normalized_entropy(counts) -> float | None:
    """분포가 얼마나 퍼져 있나. 1=고르게, 0=한 곳으로. 못 재면 None.

    **한 종류뿐이면 0 이다.** 이것을 1(완벽히 고름)로 읽으면 붕괴가 최고
    건강으로 보인다 - 자기점검이 그 경우를 고정한다.
    """
    vals = [float(c) for c in (counts or []) if c and c > 0]
    if len(vals) < 2:
        return 0.0 if vals else None
    total = sum(vals)
    h = -sum((v / total) * math.log(v / total) for v in vals)
    return h / math.log(len(vals))


def annecs(families_with_verdict, families_never_ran) -> tuple[int, int]:
    """(개척 수, 낭비 수). **불가능한 도전에는 점수를 주지 않는다.**

    ANNECS 의 두 관문을 그대로 옮긴 것이다 - 새로워야 하고(집합이므로 재탕은
    자동으로 안 세어진다), 결국 풀려야 한다(판정까지 가야 한다).
    실행조차 못 한 계열은 개척이 아니라 낭비다.
    """
    solved = {f for f in (families_with_verdict or ()) if f}
    never = {f for f in (families_never_ran or ()) if f} - solved
    return len(solved), len(never)


# ── 공정별 수율 ──────────────────────────────────────────────────────────────
# ▶ **각 공정의 품질을 따로 잰다** (López de Prado, 메타전략 패러다임)
#   "성공한 모든 퀀트 회사는 메타전략 패러다임을 적용한다 - 조립라인의 과업을
#   하위 과업으로 나누고 **각각의 품질을 독립적으로 측정·감시**한다."
#   그가 제시한 생산 사슬은 데이터 큐레이터 -> 피처 분석가 -> 전략가 ->
#   백테스트 전문가 -> 배포팀 -> 포트폴리오 감독이다. 우리 부서 편제가 거의
#   1:1 인데 **피처 분석가가 비어 있다** - 원시 봉에서 곧장 템플릿 8종으로
#   건너뛴다. 그래서 어휘가 8개에 잠겼다.
#
#   끝(관문)과 전체(전진)만 재면 **어디서 새는지 모른다.** 오늘 `발주 0건`
#   을 네 층 파고 내려간 것이 그래서였다 - 공정별 수율이 있었으면 한 줄로
#   보였을 것이다.

# 공정 이름 -> (들어온 것을 세는 SQL, 그중 다음 공정까지 간 것을 세는 SQL).
# ▶ **두 수는 반드시 포개져야 한다** (2026-08-13, 처음 재고 바로 드러났다)
#   처음엔 단계별 총량을 나열하고 이웃끼리 나눴다. 그랬더니 접수 222% ·
#   발주 205% 가 나왔다 - `접수` 는 기획안에서 온 가설만 세는데 `발주` 는
#   배분자가 만든 가설까지 세서, **분모에 없는 것을 분자가 셌다.**
#   222% 짜리 수율은 계기 전체의 신뢰를 깎는다. 그래서 각 공정을
#   "같은 대상의 부분집합" 으로 다시 정의한다 - 구조상 100% 를 넘을 수 없다.
STATION_SQL = (
    ("수집", "리드", """
        select count(*) from research.methodology_leads""", """
        select count(*) from research.methodology_leads l
         where exists (select 1 from research.experiment_proposals p
                        where l.lead_id = any(p.lead_ids)
                          and p.status in ('PUBLISHED','ACCEPTED'))"""),
    ("기획", "기획안", """
        select count(*) from research.experiment_proposals""", """
        select count(*) from research.experiment_proposals p
         where exists (select 1 from quant.hypotheses h
                        where h.proposal_id = p.proposal_id)"""),
    ("접수", "가설", """
        select count(*) from quant.hypotheses""", """
        select count(*) from quant.hypotheses h
         where exists (select 1 from quant.experiment_jobs j
                        where j.hypothesis_id = h.hypothesis_id)"""),
    ("발주", "주문가설", """
        select count(distinct hypothesis_id) from quant.experiment_jobs""", """
        select count(distinct j.hypothesis_id) from quant.experiment_jobs j
         where exists (select 1 from quant.experiments e
                        where e.hypothesis_id = j.hypothesis_id)"""),
    # `experiment_outcomes.experiment_id` 는 text, `experiments.experiment_id`
    # 는 uuid 다. 캐스팅 없이 붙이면 `operator does not exist: text = uuid` 로
    # 죽는데, 예외를 삼키는 자리에 있으면 **그 조항이 영영 안 재어진다**
    # (실측: `soundness.proper_completion` 이 같은 조인으로 조용히 죽어 있었다).
    ("실행", "실험", """
        select count(*) from quant.experiments""", """
        select count(*) from quant.experiments e
         where exists (select 1 from research.experiment_outcomes o
                        where o.experiment_id = e.experiment_id::text)"""),
)


@dataclass(frozen=True)
class Station:
    """공정 하나의 수율. `into` 가 들어온 양, `out` 이 나간 양."""

    name: str
    unit: str
    into: int
    out: int

    @property
    def nested(self) -> bool:
        """두 수가 포개지는가. **안 포개지면 수율이 아니다.**

        `out > into` 는 분모에 없는 것을 분자가 셌다는 뜻이다 - 실측으로
        222% 가 나왔다. 그런 값을 수율이라고 내보내면 계기 전체를 못 믿는다.
        """
        return self.out <= self.into

    @property
    def yield_pct(self) -> float | None:
        """수율. 잴 대상이 없거나 안 포개지면 **None**(0%도 222%도 아니다)."""
        if not self.into or not self.nested:
            return None
        return 100.0 * self.out / self.into

    @property
    def dead(self) -> bool:
        """들어왔는데 하나도 안 나간 공정. **여기가 끊긴 자리다.**"""
        return self.into > 0 and self.out == 0 and self.nested


def funnel(pairs: dict) -> list[Station]:
    """{공정: (들어옴, 나감)} -> 공정 목록. **안 센 공정은 만들지 않는다.**

    안 센 것과 0건인 것을 섞지 않는다 - 아직 세지도 않은 단계를 0 으로 읽으면
    앞 공정이 "끊겼다" 로 뜬다(자체점검이 실제로 그 사고를 잡았다).
    """
    units = {n: u for n, u, _, _ in STATION_SQL}
    out = []
    for name, _u, _s1, _s2 in STATION_SQL:
        v = (pairs or {}).get(name)
        if not v:
            continue
        into, got = int(v[0] or 0), int(v[1] or 0)
        out.append(Station(name, units.get(name, ""), into, got))
    return out


def weakest(stations) -> Station | None:
    """가장 많이 새는 공정. **끊긴 곳(수율 0)이 있으면 그게 먼저다.**"""
    live = [s for s in (stations or []) if s.yield_pct is not None]
    if not live:
        return None
    dead = [s for s in live if s.dead]
    return min(dead or live, key=lambda s: (s.yield_pct, s.name))


# ── 원장 조회 ────────────────────────────────────────────────────────────────

def measure(conn, *, fresh_days: int = FRESH_DAYS,
            window_days: int = PIONEER_WINDOW_DAYS) -> Progress:
    """원장에서 전진을 잰다. **못 잰 항목은 `measured` 에 안 들어간다.**"""
    p = Progress()
    cur = conn.cursor()

    try:
        cur.execute("""select distinct o.trial_family_id
                         from research.v_current_experiment_outcomes o
                         join quant.experiments e
                           on e.experiment_id::text = o.experiment_id
                         join quant.hypotheses h
                           on h.hypothesis_id = e.hypothesis_id
                         join quant.dataset_manifests m
                           on m.dataset_id = e.dataset_id
                        where o.trial_family_id is not null
                          and """ + _GOVERNED_PROGRESS_EVIDENCE)
        solved = [r[0] for r in cur.fetchall()]
        cur.execute("""
            select distinct h.hypothesis_id::text
              from quant.hypotheses h
             where not exists (select 1 from quant.experiments e
                                where e.hypothesis_id = h.hypothesis_id)""")
        never = [r[0] for r in cur.fetchall()]
        p.pioneered, p.wasted = annecs(solved, never)
        p.measured.append("pioneered")
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        cur.execute("""
            select count(distinct o.trial_family_id)
              from research.v_current_experiment_outcomes o
              join quant.experiments e
                on e.experiment_id::text = o.experiment_id
              join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
              join quant.dataset_manifests m on m.dataset_id = e.dataset_id
             where o.trial_family_id is not null
               and o.decided_at > now() - make_interval(days => %s)
               and """ + _GOVERNED_PROGRESS_EVIDENCE,
                    (int(window_days),))
        p.recent_pioneered = int(cur.fetchone()[0] or 0)
        p.measured.append("recent_pioneered")
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        cur.execute("select count(*) from quant.experiments")
        p.attempts = int(cur.fetchone()[0] or 0)
        p.measured.append("attempts")
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        cur.execute("""
            select count(*) from research.methodology_leads
             where created_at > now() - make_interval(days => %s)""",
                    (int(fresh_days),))
        p.fresh_leads = int(cur.fetchone()[0] or 0)
        p.measured.append("fresh_leads")
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        cur.execute("""select unnest(o.lesson_codes), count(*)
                         from research.v_current_experiment_outcomes o
                         join quant.experiments e
                           on e.experiment_id::text = o.experiment_id
                         join quant.hypotheses h
                           on h.hypothesis_id = e.hypothesis_id
                         join quant.dataset_manifests m
                           on m.dataset_id = e.dataset_id
                        where """ + _GOVERNED_PROGRESS_EVIDENCE + """
                        group by 1 order by 2 desc""")
        rows = cur.fetchall()
        if rows:
            p.lesson_entropy = normalized_entropy([r[1] for r in rows])
            p.lesson_modes = [(r[0], int(r[1])) for r in rows[:4]]
            p.measured.append("lesson_entropy")
    except Exception:  # noqa: BLE001
        conn.rollback()

    # 공정별 수율. 못 센 공정은 빼고 잰다(0 으로 채우지 않는다).
    pairs: dict = {}
    for name, _unit, sql_in, sql_out in STATION_SQL:
        try:
            cur.execute(sql_in)
            a = int(cur.fetchone()[0] or 0)
            cur.execute(sql_out)
            b = int(cur.fetchone()[0] or 0)
            pairs[name] = (a, b)
        except Exception:  # noqa: BLE001
            conn.rollback()
    if pairs:
        p.stations = funnel(pairs)
        p.measured.append("stations")

    p.judge = judge_version()
    return p


def brief_block(p: Progress) -> str:
    """브리핑에 싣는 전진 판정. **못 쟀으면 빈 문자열**(지어내지 않는다)."""
    if not p.measured:
        return ""
    out = ["\n[공장 전진 - 돌릴수록 나아지고 있는가]"]
    if "pioneered" in p.measured:
        y = p.yield_per_attempt
        out.append(f"  개척한 계열 {p.pioneered}개 / 시도 {p.attempts}회"
                   + (f" (시도당 {y:.2f})" if y is not None else "")
                   + (f" · 실행조차 못 한 가설 {p.wasted}건은 **0점**"
                      if p.wasted else ""))
    if "recent_pioneered" in p.measured:
        out.append(f"  최근 {PIONEER_WINDOW_DAYS}일 새 개척: {p.recent_pioneered}건"
                   + ("" if p.advancing else "  ← **전진 정지**"))
    if p.starving:
        out.append(f"  ▶ **외부 정보가 끊겼다** - 최근 {FRESH_DAYS}일 새 리드 0건. "
                   "자기 산출만 먹고 도는 루프는 분포의 꼬리가 깎여 나간다. "
                   "**고정된 과거 데이터는 붕괴를 늦출 뿐 막지 못한다** - "
                   "스카우트가 편의 기능이 아니라 붕괴 방지 장치인 이유다.")
    elif "fresh_leads" in p.measured:
        out.append(f"  최근 {FRESH_DAYS}일 새 리드: {p.fresh_leads}건")
    if p.lesson_entropy is not None:
        mode = ", ".join(f"{c}:{n}" for c, n in p.lesson_modes)
        out.append(f"  교훈 다양성 {p.lesson_entropy:.2f}"
                   + ("  ← **몇 개 모드로 수렴 중**" if p.collapsing else "")
                   + f"  (상위: {mode})")
    if p.stations:
        parts = []
        for s in p.stations:
            y = s.yield_pct
            parts.append(f"{s.name} {s.into}"
                         + (f"→{y:.0f}%" if y is not None else "→-"))
        out.append("  공정 수율: " + " · ".join(parts))
        w = weakest(p.stations)
        if w is not None:
            out.append(f"  ▶ 가장 새는 공정: **{w.name}** "
                       + (f"({w.into}건 들어와 하나도 안 나갔다)" if w.dead
                          else f"(수율 {w.yield_pct:.0f}%)")
                       + ". 끝과 전체만 재면 어디서 새는지 모른다 - "
                       "공정마다 따로 재는 이유다(메타전략 패러다임).")
    out.append("  ▶ 이 숫자가 안 오르면 공장은 **돌고 있어도 제자리**다. "
               "연구 생산성은 가만 두면 떨어진다(무어의 법칙 한 번 배가에 "
               "1970년대의 18배 연구자가 든다) - 오르는 것은 거스르는 것이다.")
    if p.judge:
        out.append(f"  ▶ 판정자 {p.judge}. **이 값을 올리려고 판정자를 고치면 "
                   "해시가 바뀌어 곡선이 끊긴다** - 오른 게 아니라 자가 "
                   "불연속이 된다. 측정기는 고쳐도 되지만 고친 사실이 "
                   "성적표에 같이 찍힌다.")
    return "\n".join(out)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_impossible_challenges_earn_nothing():
    """**불가능한 도전을 만들어 낸 데는 점수를 안 준다.** (ANNECS 관문 ②)

    이게 없으면 공장이 못 도는 가설을 대량 생산해 "개척" 숫자를 부풀릴 수
    있다. 실측으로 실행조차 못 한 가설이 이미 여럿이었다(어휘 밖·파라미터
    불일치) - 그것들을 세면 전진 지표가 거짓말을 한다.
    """
    got, wasted = annecs(["fam_a", "fam_b"], ["h1", "h2", "h3"])
    assert got == 2 and wasted == 3, (got, wasted)
    # 재탕은 자동으로 안 세어진다(집합)
    assert annecs(["fam_a", "fam_a", "fam_a"], [])[0] == 1
    # 빈 값을 개척으로 세지 않는다
    assert annecs([None, "", "fam_a"], [])[0] == 1
    assert annecs([], []) == (0, 0)


def _check_single_mode_is_zero_not_one():
    """**한 종류로 수렴한 것을 "완벽히 고르다" 로 읽지 않는다.**

    정규화 엔트로피는 log(k) 로 나누는데 k=1 이면 0/0 이다. 여기서 1 을
    돌려주면 **붕괴가 최고 건강으로 보인다** - 부호가 뒤집힌 지표는 없느니만
    못하다.
    """
    assert normalized_entropy([10]) == 0.0
    assert normalized_entropy([]) is None
    e_even = normalized_entropy([5, 5, 5, 5])
    e_skew = normalized_entropy([50, 1, 1, 1])
    assert e_even is not None and e_even > 0.99, e_even
    assert e_skew is not None and e_skew < 0.5, e_skew
    assert e_skew < e_even


def _check_unmeasured_is_not_progress():
    """**못 쟀으면 "전진 중" 이라고 말하지 않는다.**

    빈 Progress 는 `advancing=False` 여야 한다. 기본값이 True 면 DB 가 죽은
    날 공장이 자기를 건강하다고 보고한다 - 오늘 하루 고친 유형이 이것이다.
    """
    p = Progress()
    assert p.advancing is False
    assert p.yield_per_attempt is None
    assert brief_block(p) == "", "못 쟀는데 판정을 적었다"
    # 굶주림은 **쟀을 때만** 말한다. 안 재고 0건이라고 하면 거짓이다.
    assert p.starving is False, "안 쟀는데 굶주렸다고 했다"
    p2 = Progress(measured=["fresh_leads"], fresh_leads=0)
    assert p2.starving is True


def _check_productivity_falls_when_attempts_outrun_discovery():
    """**시도만 늘고 개척이 안 늘면 생산성이 떨어진 것이다.** (BJVR)

    이 지표가 없으면 "실험 34건 돌렸다" 를 성과로 읽게 된다. 실측 격자에서
    총 시도 34회에 찬 칸은 9개였다 - 시도당 0.26 이다.
    """
    early = Progress(pioneered=9, attempts=34, measured=["pioneered"])
    later = Progress(pioneered=10, attempts=80, measured=["pioneered"])
    assert early.yield_per_attempt > later.yield_per_attempt
    assert abs(early.yield_per_attempt - 9 / 34) < 1e-9
    body = brief_block(early)
    assert "시도당 0.26" in body, body


def _check_starving_says_why_fixed_data_is_not_enough():
    """**"리드가 없다" 로 끝내지 않는다.** 왜 치명적인지가 같이 가야 한다.

    고정된 과거 데이터로 계속 돌리면 되지 않느냐는 것이 자연스러운 반응이다.
    문헌의 답은 아니다 - 고정 실물 데이터는 붕괴를 **늦출 뿐 막지 못한다.**
    그 한 줄이 없으면 스카우트가 다시 뒷순위로 밀린다(실측으로 밀렸었다).
    """
    p = Progress(measured=["fresh_leads", "pioneered"], fresh_leads=0,
                 pioneered=3, attempts=10)
    body = brief_block(p)
    assert "외부 정보가 끊겼다" in body
    assert "늦출 뿐" in body, "왜 치명적인지가 빠졌다"
    # 리드가 있으면 경보를 울리지 않는다(늑대가 없는데 외치지 않는다)
    ok = Progress(measured=["fresh_leads"], fresh_leads=4)
    assert "외부 정보가 끊겼다" not in brief_block(ok)


def _check_progress_and_soundness_answer_different_questions():
    """**건전성과 전진은 다른 질문이다.** 하나로 합치면 둘 다 흐려진다.

    건전(soundness) = 지금 앞으로 갈 수 있나(liveness).
    전진(progress)  = 실제로 나아갔나(open-endedness).
    건전한데 제자리인 공장이 가능하고, 그게 제일 위험한 상태다 - 아무 경보도
    안 울리기 때문이다.
    """
    stuck = Progress(measured=["pioneered", "recent_pioneered"],
                     pioneered=9, attempts=34, recent_pioneered=0)
    assert not stuck.advancing
    assert "전진 정지" in brief_block(stuck)
    moving = Progress(measured=["recent_pioneered"], recent_pioneered=2)
    assert moving.advancing and "전진 정지" not in brief_block(moving)


def _check_weakest_station_is_the_dead_one():
    """**끊긴 공정이 있으면 그게 1순위다.** 수율이 낮은 것보다 먼저다.

    (López de Prado) "각 하위 공정의 품질을 독립적으로 측정·감시한다."
    끝과 전체만 재면 어디서 새는지 모른다 - 오늘 `발주 0건` 을 네 층 파고
    내려간 것이 그래서였고, 공정별 수율이 있었으면 한 줄로 보였을 것이다.
    """
    st = funnel({"수집": (20, 11), "기획": (11, 9), "접수": (8, 0)})
    names = {s.name: s for s in st}
    assert abs(names["수집"].yield_pct - 55.0) < 1e-9, names["수집"].yield_pct
    assert names["접수"].dead, "8건 들어와 0건 나갔는데 안 끊겼다고 한다"
    w = weakest(st)
    assert w is not None and w.name == "접수", w

    # 끊긴 데가 없으면 가장 수율 낮은 곳
    st2 = funnel({"수집": (100, 90), "기획": (90, 9)})
    assert weakest(st2).name == "기획", weakest(st2)

    # **안 센 공정은 만들지 않는다.** 0 으로 읽으면 아직 세지도 않은 단계
    # 때문에 앞 공정이 "끊겼다" 로 뜬다(이 검사가 실제로 그 사고를 잡았다).
    st4 = funnel({"수집": (100, 90)})
    assert [s.name for s in st4] == ["수집"], [s.name for s in st4]

    # **포개지지 않으면 수율이 아니다.** 실측으로 접수 222% 가 나왔다 -
    # 분모에 없는 것을 분자가 셌기 때문이다. 그런 값은 내보내지 않는다.
    bad = Station("접수", "가설", 9, 20)
    assert not bad.nested and bad.yield_pct is None
    assert not bad.dead
    assert weakest([bad]) is None, "222% 짜리를 병목으로 골랐다"

    # **들어온 게 없으면 0% 가 아니라 미측정이다.**
    st3 = funnel({"수집": (0, 0)})
    assert st3[0].yield_pct is None and not st3[0].dead
    assert weakest(st3) is None
    assert funnel({}) == [] and weakest([]) is None

    collection_out = STATION_SQL[0][3]
    assert "p.status in ('PUBLISHED','ACCEPTED')" in collection_out, \
        "Gate 0 REJECTED 리드를 수집→기획 전환으로 세면 재제안과 수율이 모두 틀린다"
    assert weakest(None) is None


def _check_judge_version_moves_when_the_judge_moves():
    """**측정기를 고치면 성적표에 찍힌다.** (2026-08-13, DGM 실측 대응)

    Darwin Gödel Machine 에서 자기수정 에이전트가 **부정행위 검사 코드를
    수정해 감시자를 눈멀게 했다.** 우리는 "실행면을 진화시켜도 된다" 고
    허락해 놨고 오늘 바깥 고리에 목적함수를 만들었다 - 그 값을 올리는 가장
    싼 방법이 이 파일을 고치는 것이 됐다.

    막는 방법은 금지가 아니라 **각인**이다. 판정자가 바뀌면 해시가 바뀌고,
    해시가 다른 두 성적은 비교 대상이 아니다.
    """
    v = judge_version()
    assert v.startswith("judge_"), v
    assert "/3)" in v, f"판정자 파일 수가 안 맞는다: {v}"
    assert judge_version() == v, "같은 코드인데 해시가 흔들린다"

    # 성적표에 실제로 실린다 - 안 실리면 각인이 아니다
    p = Progress(measured=["pioneered"], pioneered=1, attempts=2, judge=v)
    body = brief_block(p)
    assert v in body and "곡선이 끊긴다" in body, body
    # 못 읽었으면 지어내지 않는다
    assert "판정자" not in brief_block(Progress(measured=["pioneered"]))


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_weakest_station_is_the_dead_one()
    print("  끊긴 공정이 1순위          OK")
    _check_judge_version_moves_when_the_judge_moves()
    print("  판정자 버전이 각인된다     OK")
    _check_impossible_challenges_earn_nothing()
    print("  불가능한 도전은 0점        OK")
    _check_single_mode_is_zero_not_one()
    print("  한 모드 수렴 = 엔트로피 0  OK")
    _check_unmeasured_is_not_progress()
    print("  못 쟀으면 전진 아님        OK")
    _check_productivity_falls_when_attempts_outrun_discovery()
    print("  시도당 수확을 센다         OK")
    _check_starving_says_why_fixed_data_is_not_enough()
    print("  굶주림에 이유가 붙는다     OK")
    _check_progress_and_soundness_answer_different_questions()
    print("  건전 != 전진               OK")
    print("공장 전진 8개 영역 통과.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_selfcheck())
