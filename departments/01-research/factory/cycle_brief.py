"""회차 브리핑 - 원장에서 에이전트가 알아야 할 것을 뽑아 프롬프트로 만든다.

담당: 재일 (리서치본부 RES)

▶ 이게 없어서 "자기 개선" 이 사람 손에 묶여 있었다
  2026-08-10~11 실측에서 스카우트는 실제로 나아졌다 - 1회차 단기반전이 기각되자
  2회차는 설계로 반박을 받았고, 3회차는 아예 계열을 갈아탔다. 그런데 **그 교훈을
  매번 사람이 프롬프트에 붙여넣었다.** `research.experiment_outcomes` 를 읽는
  코드는 발행 게이트(중복 차단)뿐이고 스카우트 프롬프트로 가는 경로가 없었다.

  그래서 지금 상태는 "에이전트가 스스로 개선" 이 아니라 "사람이 물려주면 개선"
  이다. 사람이 빠지면 같은 실수를 처음부터 다시 한다.

▶ 브리핑은 **지시가 아니라 사실**이다
  여기서 "이렇게 하라" 고 쓰지 않는다. 원장에 있는 사실만 옮긴다 - 무엇이 왜
  기각됐는지, 예산이 얼마 남았는지, 우리 데이터가 지금 무엇을 지원하는지.
  판단은 에이전트가 한다. 브리핑이 결론까지 쓰기 시작하면 그때부터 에이전트의
  산출은 우리 지시의 메아리이고, 그것을 근거로 실험하면 근거가 순환한다.

▶ **문턱을 브리핑으로 낮추지 않는다**
  회의론자가 계속 기각한다고 "이번엔 좀 통과시켜라" 를 브리핑에 넣으면 그것이
  바로 2026-08-11 회고에서 잡힌 그 사고다(프롬프트로 판정 기준을 조율). 브리핑은
  기각 **사유**를 전하지 기각 **비율**을 전하지 않는다.

자체 점검: python departments/01-research/factory/cycle_brief.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "04-quant-backtest" / "pipeline"))

from stock_universe import governed_stock_evidence_sql  # noqa: E402

MODULE_VERSION = "research-cycle-brief-v1"
_GOVERNED_LESSON_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

# 교훈 코드 -> 스카우트가 다음에 무엇을 피해야 하는지. **사실의 번역이지
# 지시가 아니다** - "이 계열을 하지 마라" 가 아니라 "이 계열은 이 데이터로
# 이런 이유에서 답이 안 나왔다" 를 전한다.
LESSON_MEANING = {
    "UNDERPOWERED_DATA": "표본이 그 설계를 못 받쳤다(창이 안 나옴) - "
                         "더 짧은 창이나 더 긴 이력이 필요하다",
    "OVERFIT_PBO": "선택 절차가 신뢰되지 않았다 - 변형을 줄이거나 표본을 늘려야 한다",
    "OVERFIT_DSR": "시도 횟수를 감안하면 유의하지 않았다",
    "BASELINE_NOT_BEATEN": "기준선(동일가중 매수보유)을 못 이겼다",
    "BEAR_FRAGILE": "하락 국면에서 무너졌다",
    "SINGLE_REGIME_ONLY": "한 국면에서만 작동했다",
    "COST_SENSITIVE": "거래비용을 반영하면 사라졌다",
    "LEAKAGE_SUSPECT": "누수가 의심돼 무효화됐다",
    "CAPACITY_DOUBT": "수용력이 의심된다",
    "LIVE_SLIPPAGE_BLOWN": "실운용 슬리피지가 가정을 벗어났다",
    "LIVE_MDD_EXCEEDED": "실운용 낙폭이 한도를 넘었다",
}


@dataclass
class Brief:
    lessons: list = field(default_factory=list)      # (family, [codes], 요약)
    exhausted: list = field(default_factory=list)    # 최근 돌린 질의(쿨다운)
    coverage: dict = field(default_factory=dict)     # 지금 데이터가 지원하는 것
    budget: dict = field(default_factory=dict)       # family 별 남은 시도
    known_leads: int = 0

    def as_prompt(self) -> str:
        """에이전트 프롬프트에 붙일 사실 묶음. **결론은 쓰지 않는다.**"""
        out: list[str] = ["[원장에서 읽은 사실 - 판단은 네가 한다]"]

        if self.coverage:
            out.append("\n우리가 지금 가진 데이터:")
            for k, v in sorted(self.coverage.items()):
                out.append(f"  - {k}: {v}")

        if self.lessons:
            out.append("\n지난 실험이 이렇게 끝났다:")
            for fam, codes, note in self.lessons:
                why = "; ".join(LESSON_MEANING.get(c, c) for c in codes)
                out.append(f"  - [{fam[:12]}] {why}")
                if note:
                    out.append(f"      {note[:160]}")
            out.append("  같은 계열을 다시 제안하려면 이 사유에 대응해야 접수된다"
                       "(Gate 0 가 DUPLICATE_UNADDRESSED 로 막는다).")

        if self.budget:
            out.append("\n계열별 PRIMARY 시도 예산:")
            for fam, b in sorted(self.budget.items()):
                out.append(f"  - [{fam[:12]}] {b['used']}/{b['budget']} 사용"
                           + (f" · {b['identity']}" if b.get("identity") else "")
                           + ("  **소진**" if b["used"] >= b["budget"] else ""))
            out.append(
                "  PRIMARY 예산은 top-level 가설 실행만 센다. sidecar 스크리닝은 "
                "이 예산을 쓰거나 승격 권한을 얻지 않지만 DSR·실패 기억의 "
                "다중검정 노출에는 남는다.")
            out.append(
                "  인트라데이 계열은 경제 좌표(event/context/direction/execution)와 "
                "AST 구조 모양으로 구분한다. 창·상수·threshold만 바꾸거나 이름만 "
                "바꾸는 것은 새 경제 계열이 아니다. 소진 계열을 피하려면 다른 "
                "미시구조 사건/상대방/실행 메커니즘과 구조적으로 다른 AST를 제안하라.")

        if self.exhausted:
            out.append("\n최근 돌린 질의(쿨다운 중 - 같은 것을 또 돌리면 낭비다):")
            for q in self.exhausted[:12]:
                out.append(f"  - {q}")

        if self.known_leads:
            out.append(f"\n이미 원장에 리드 {self.known_leads}건이 있다. "
                       f"같은 출처를 다시 가져오면 새 리드가 아니라 언급 수만 오른다.")
        return "\n".join(out)


# ── 원장 조회 ──────────────────────────────────────────────────────────────
def load_lessons(conn, *, limit: int = 12) -> list:
    """기각 교훈. **성공도 함께 읽는다** - 무엇이 통했는지도 배워야 한다."""
    cur = conn.cursor()
    cur.execute("""
        select o.trial_family_id, o.lesson_codes, coalesce(o.notes, ''),
               o.decision
          from research.v_current_experiment_outcomes o
          join quant.experiments e
            on e.experiment_id::text = o.experiment_id
          join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
          join quant.dataset_manifests m on m.dataset_id = e.dataset_id
         where """ + _GOVERNED_LESSON_EVIDENCE + """
         order by o.created_at desc limit %s
    """, (limit,))
    out = []
    for fam, codes, notes, decision in cur.fetchall():
        cs = [str(c) for c in (codes or [])]
        if not cs and decision not in ("SUBMIT_TO_QA",):
            continue
        out.append((str(fam or "-"), cs or [f"판정 {decision}"], notes))
    return out


def load_budget(conn) -> dict:
    """계열별 실행 횟수. **실행 기록에서 센다** - 종결 기록으로 세면 적게 센다."""
    cur = conn.cursor()
    cur.execute("""
        select e.trial_family_id, count(*),
               max(coalesce(h.expected_edge->>'type', '')),
               max(coalesce(h.expected_edge->'semantic_plan'->>'event', '')),
               max(coalesce(h.expected_edge->'semantic_plan'->>'direction', '')),
               max(coalesce(h.expected_edge->'semantic_plan'->>'execution', '')),
               max(coalesce(h.expected_edge->'semantic_plan'->>'horizon_seconds', ''))
          from quant.experiments e
          join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
         where e.trial_family_id is not null
           and (
               e.status in ('QUEUED', 'RUNNING', 'COMPLETED')
               or exists (select 1 from quant.backtest_runs run
                           where run.experiment_id = e.experiment_id)
               or exists (select 1 from quant.experiment_metrics metric
                           where metric.experiment_id = e.experiment_id)
               or exists (select 1 from research.experiment_outcomes outcome
                           where outcome.experiment_id = e.experiment_id::text)
               or exists (
                    select 1
                      from quant.intraday_experiment_rungs rung
                      join quant.intraday_session_accesses access
                        on access.experiment_rung_id = rung.experiment_rung_id
                     where rung.experiment_id = e.experiment_id)
           )
         group by 1 order by 2 desc limit 12
    """)
    out = {}
    for row in cur.fetchall():
        f, n, *coords = row
        identity = "/".join(str(value) for value in coords if value)
        out[str(f)] = {"used": int(n), "budget": 5, "identity": identity}
    return out


def load_recent_queries(conn, *, hours: int = 72) -> list:
    """쿨다운 중인 질의. 없으면 빈 목록 - **없는 것을 지어내지 않는다.**"""
    cur = conn.cursor()
    try:
        cur.execute("""
            select distinct claimed_edge from research.methodology_leads
             where as_known_at > %s order by 1 limit 20
        """, (datetime.now(timezone.utc) - timedelta(hours=hours),))
        return [str(r[0])[:70] for r in cur.fetchall()]
    except Exception:
        conn.rollback()
        return []


def load_coverage(market_conn) -> dict:
    """지금 데이터가 무엇을 지원하는지. 에이전트가 답 안 나올 가설을 고르지 않게."""
    if market_conn is None:
        return {}
    cur = market_conn.cursor()
    out = {}
    cur.execute("""select count(distinct bucket_time::date), min(bucket_time)::date,
                          max(bucket_time)::date, count(distinct instrument_id)
                     from market.market_bars where interval_code = '1D'""")
    d, s, e, n = cur.fetchone()
    if d:
        out["일봉"] = f"{d}거래일 {s}~{e}, {n}종목"
    # Raw event tables are hundreds of GB. Both count(distinct ...) and global
    # ORDER BY ... LIMIT 1 probes were observed to fan out over compressed
    # chunks. Read only Timescale's chunk catalog here. This is explicitly a
    # chunk range, not an observed-row claim; the bounded evaluator measures
    # exact sessions, instruments and lineage for each experiment.
    cur.execute("""
        select hypertable_name, min(range_start)::date, max(range_end)::date,
               count(*)
          from timescaledb_information.chunks
         where hypertable_schema = 'market'
           and hypertable_name in ('market_ticks', 'market_quotes')
         group by hypertable_name
    """)
    labels = {"market_ticks": "체결", "market_quotes": "호가"}
    for table, s, e, chunks in cur.fetchall():
        if s and e:
            out[labels[str(table)]] = (
                f"LIVE CHUNK_RANGE {s}~{e} ({int(chunks)} chunks); "
                "정확한 세션·종목 수는 실험별 bounded slice에서 계측"
            )
    return out


def build(conn, *, market_conn=None) -> Brief:
    b = Brief()
    b.lessons = load_lessons(conn)
    b.budget = load_budget(conn)
    b.exhausted = load_recent_queries(conn)
    b.coverage = load_coverage(market_conn)
    cur = conn.cursor()
    cur.execute("select count(*) from research.methodology_leads")
    b.known_leads = int(cur.fetchone()[0] or 0)
    return b


# ── 자체 점검 ──────────────────────────────────────────────────────────────
class _Cur:
    def __init__(self, seq): self._seq = list(seq); self._i = -1
    def execute(self, sql, p=None): self._i += 1
    def fetchall(self): return self._seq[self._i]
    def fetchone(self): return self._seq[self._i][0]


class _Conn:
    def __init__(self, seq): self._c = _Cur(seq)
    def cursor(self): return self._c
    def rollback(self): pass


def _selfcheck() -> int:
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    conn = _Conn([
        [("fam_abc123", ["UNDERPOWERED_DATA"], "창 0개", "GATE_HOLD")],  # lessons
        [("fam_abc123", 3, "order_flow_imbalance", "QUEUE_IMBALANCE",
          "FOLLOW", "TAKER", "30")],                                  # budget
        [("모멘텀 붕괴는 변동성으로 예측된다",)],                          # queries
        [(5,)],                                                          # leads
    ])
    b = build(conn)
    p = b.as_prompt()

    check("교훈이 뜻으로 번역됨", "표본이 그 설계를 못 받쳤다" in p)
    check("Gate 0 대응 요구 명시", "DUPLICATE_UNADDRESSED" in p)
    check("예산 노출", "3/5 사용" in p)
    check("계열 의미 좌표 노출", "QUEUE_IMBALANCE/FOLLOW/TAKER/30" in p)
    check("PRIMARY와 screening 압력 분리", "sidecar 스크리닝" in p
          and "다중검정 노출" in p)
    check("숫자 retune은 새 계열 아님", "창·상수·threshold" in p)
    check("쿨다운 노출", "모멘텀 붕괴" in p)
    check("기존 리드 수", "리드 5건" in p)
    check("판단은 에이전트 몫이라 명시", "판단은 네가 한다" in p)

    # ▶ **문턱을 낮추는 문구가 절대 들어가면 안 된다.** 브리핑이 판정 기준을
    #   조율하기 시작하면 회의론자는 게이트가 아니라 장식이 된다.
    banned = ["통과시켜", "좀 봐줘", "기각률", "이번엔", "완화"]
    check("문턱 조율 문구 없음", not any(w in p for w in banned))

    # 빈 원장에서도 죽지 않는다
    b2 = build(_Conn([[], [], [], [(0,)]]))
    check("빈 원장", "[원장에서 읽은 사실" in b2.as_prompt())

    # 커버리지 없으면 조용히 비운다(지어내지 않는다)
    check("커버리지 미측정", b2.coverage == {})

    market = _Conn([
        [(627, "2024-01-02", "2026-07-30", 3924)],
        [("market_ticks", "2026-06-25", "2026-08-15", 43),
         ("market_quotes", "2026-06-25", "2026-08-15", 45)],
    ])
    coverage = load_coverage(market)
    check("raw event chunk coverage", "CHUNK_RANGE 2026-06-25~2026-08-15" in coverage["체결"]
          and "bounded slice" in coverage["호가"])
    import inspect
    source = inspect.getsource(load_coverage)
    check("raw event full scan forbidden",
          "select count(distinct observed_at::date)" not in source)
    check("raw event catalog probe",
          "timescaledb_information.chunks" in source
          and "order by event_time" not in source
          and "order by observed_at" not in source)

    for f in fails:
        print(f"  FAIL {f}")
    print(f"cycle_brief 자체 점검: {15 - len(fails)}/15 통과")
    return 1 if fails else 0


def _cli(argv: list[str]) -> int:
    if "--show" not in argv:
        return _selfcheck()
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    mkt = None
    try:
        mkt = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    except Exception:
        pass
    try:
        print(build(conn, market_conn=mkt).as_prompt())
    finally:
        conn.close()
        if mkt:
            mkt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
