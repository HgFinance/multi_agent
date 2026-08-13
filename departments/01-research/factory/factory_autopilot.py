"""전략 공장 자동 조종 - 리서치·퀀트 부서 에이전트를 일하게 만든다.

담당: 재일 (리서치본부 RES + 퀀트·백테스트본부 QNT)

▶ 무엇을 하나
  공장은 사람이 스크립트를 손으로 돌려야만 한 바퀴 돌았다. 이 파일은 그 손을
  없앤다. 주기마다:

    1. **결정론이 원장을 읽어** 브리핑을 만든다(지난 교훈·계열별 시도 압력·
       쿨다운·데이터 적용범위). 여기에 결론은 없다.
    2. 그 브리핑을 본문에 실어 **칸반 카드**를 만든다.
    3. 이미 도는 `kanban-dispatcher` 가 카드를 부서 Hermes 에이전트에게 돌린다.
    4. 에이전트가 판단(다음 가설은 무엇인가 / 이 결과를 어떻게 읽는가)을 한다.

▶ 왜 이 모양인가
  - **부서 컨테이너는 저장소도 psycopg2 도 없다**(실측). 그래서 브리핑은 DB 에
    닿는 호스트가 만들고, 컨테이너에는 **완성된 사실**만 건넨다. 이건 우회가
    아니라 이 저장소의 규칙 그대로다 - 사실은 결정론이 모으고, 에이전트는
    서술·판단만 한다(CLAUDE.md 개발원칙: LLM 은 관련성 판단·서술에만).
  - 카드로 넘기는 이유는 **감시 가능해서**다. 카드는 보드에 남아 상태·실패
    사유·소요가 보인다. 컨테이너 안에서 조용히 도는 크론은 그게 안 된다.

▶ 지어내지 않는다
  브리핑을 못 만들면 **카드를 만들지 않는다**. 사실 없이 "가설을 내라"고 시키면
  그 자리에서 나오는 것은 근거 없는 문장이고, 그게 공장에 들어가면 원장이
  오염된다. 조회 실패는 실패로 보고하고 그 주기는 건너뛴다.

실행:
    python departments/01-research/factory/factory_autopilot.py --once
    python departments/01-research/factory/factory_autopilot.py --once --dry-run
    python departments/01-research/factory/factory_autopilot.py --loop --interval-min 240
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_RESEARCH = _HERE.parent
_ROOT = _RESEARCH.parents[1]
for p in (str(_HERE), str(_RESEARCH), str(_RESEARCH / "collectors"),
          str(_ROOT / "departments" / "04-quant-backtest" / "pipeline")):
    if p not in sys.path:
        sys.path.insert(0, p)

MODULE_VERSION = "factory-autopilot-v1"

# 카드를 만들 때 쓰는 CLI 컨테이너. 어느 프로필이든 같은 보드를 본다
# (/opt/kanban 이 8개 컨테이너에 공유 마운트다).
KANBAN_CLI_CONTAINER = os.getenv("KANBAN_CLI_CONTAINER", "hedgefund-qa-hermes")

RESEARCH_ASSIGNEE = "research-department"
QUANT_ASSIGNEE = "quant-backtest-department"

# 컨테이너의 /opt/kanban 이 호스트의 이 경로다. 에이전트 산출물(첨부)을
# **호스트가 직접 읽는다** - 컨테이너에서 꺼내오는 왕복이 필요 없다.
ATTACH_ROOT = Path(os.getenv(
    "KANBAN_ATTACH_ROOT",
    str(Path.home() / ".hermes-shared-kanban" / "kanban" / "attachments")))
WORKSPACE_ROOT = Path(os.getenv(
    "KANBAN_WORKSPACE_ROOT",
    str(Path.home() / ".hermes-shared-kanban" / "kanban" / "workspaces")))

# ▶ **형식을 알려주지 않으면 발행이 0건이다** (2026-08-11 실측).
#   에이전트는 105초 동안 제대로 된 기획안을 썼는데 마크다운 산문이었고,
#   `parse_blocks` 가 한 블록도 못 잘라 `published=0, rejected=[], gate=[]` 이
#   나왔다. 막힌 게 아니라 **읽히지 않은** 것이다. 요구 형식은 계약이므로
#   요청과 함께 준다.
PLANNER_FORMAT = """\
[산출 형식 - 이대로 쓰지 않으면 한 글자도 접수되지 않는다]
아래 `KEY: value` 줄만 낸다. 설명·머리말·코드펜스를 붙이지 마라.
`TITLE:` 이 나올 때마다 새 기획안이다. 값이 길면 다음 줄에 이어 써도 된다.

**몇 건을 낼지는 네가 정한다.** 파서는 개수 제한이 없다 - 검정할 것이
여럿이면 여럿 내라. 특히 데이터셋이 넓어져 표본 부족 교훈이 해소된
계열은 한 주기에 함께 재제안하는 편이 낫다(한 건씩 내면 계열 수만큼
주기가 걸린다). 다만 **근거로 채우지 마라** - 리드가 없거나 메커니즘을
못 쓰겠으면 그 건은 내지 않는 것이 맞다. 수를 늘리려고 낸 기획안은
시도 예산만 태우고 계열 압력을 올린다.

TITLE: (한 줄 제목)
LEAD_IDS: (아래 '쓸 수 있는 리드' 의 lead_id 를 쉼표로. **목록에 없는 id 를 쓰면 막힌다**)
ECONOMIC_RATIONALE: (왜 이 초과수익이 존재할 수 있는가 - 메커니즘)
COUNTERPARTY: (누가 반대편에서 손해를 보는가. 이게 없으면 공짜 점심 주장이다)
EDGE_TYPE: (아래 통제 어휘 중 하나)
UNIVERSE_KEY: (아래 통제 어휘 중 하나)
LABEL: forward_return
BASELINE: equal_weight_buy_and_hold
FALSIFICATION_TESTS: (무엇이 나오면 기각인가. 쉼표로 나열)
DATA_TABLES: (필요한 원천 테이블. 파생지표는 실행면이 계산하므로 적지 마라)
MIN_HISTORY_DAYS: (정수)
SUGGESTED_PARAMS: {"horizon_days": 20, "top_n": 20}
SOURCE_REPORTED_EFFECT: {"sharpe": null}
TRIAL_BUDGET: 5
COMPETING_EXPLANATION: (이 결과를 알파 말고 무엇으로 설명할 수 있는가 - 구체적으로)
COMPETING_CODES: (아래 통제 어휘 중 해당하는 것을 쉼표로. 밖의 값은 반려된다)
LESSONS_ADDRESSED: (이 계열이 전에 기각됐다면 **교훈코드=이번엔 무엇을 다르게
      하는가** 를 쉼표로. 예: SINGLE_REGIME_ONLY=국면 필터를 빼고 전 기간을
      쓴다, BEAR_FRAGILE=낙폭 정지를 -0.28 로 건다.
      **비면 같은 계열 재제안이 Gate 0 에서 자동 반려된다.**
      기각 이력이 없는 새 계열이면 비워도 된다)

필수: TITLE, LEAD_IDS, ECONOMIC_RATIONALE, COUNTERPARTY, EDGE_TYPE, UNIVERSE_KEY,
      COMPETING_EXPLANATION.
하나라도 비면 그 기획안은 접수 전에 반려된다.

▶ **기각된 계열을 다시 내는 것은 좋은 일이다.** 찾은 것을 버리고 새로 찾는
  것보다 싸다. 다만 무엇을 바꿨는지 LESSONS_ADDRESSED 에 적어야 한다 -
  같은 설계를 다시 내면 같은 이유로 또 기각된다. 산문(ECONOMIC_RATIONALE)에만
  적으면 Gate 0 가 못 읽는다. **그 칸에 적어라.**

COMPETING_EXPLANATION 은 **결과를 보기 전에** 적는 것이다. 이걸 미리 안 적으면
나중에 어떤 결과가 나와도 설명이 붙는다 - 그 순간 실험은 검증이 아니라 서사가
된다. '과적합일 수 있다' 같은 일반론은 아무것도 막지 못하므로 구체적으로 써라."""

SKEPTIC_FORMAT = """\
[산출 형식 - 이대로 쓰지 않으면 한 글자도 접수되지 않는다]
TITLE: (검토 대상 기획안의 **제목을 그대로**. 다르면 짝을 못 찾아 반려된다)
COMPETING_EXPLANATION: (이 결과를 알파 말고 무엇으로 설명할 수 있는가)
COMPETING_CODES: (아래 통제 어휘 중 해당하는 것을 쉼표로. 밖의 값은 반려된다)
VERDICT: PROCEED 또는 STOP

VERDICT 가 PROCEED 가 아니면 그 기획안은 발행되지 않는다. 통과시키려고
PROCEED 를 쓰지 마라 - 회의론자가 통과 도장이면 서명이 무의미해진다."""


# ── 원장 조회 ────────────────────────────────────────────────────────────────

_SQL_PENDING = """
select h.hypothesis_id::text, h.title, h.status, h.expected_edge,
       h.created_at, h.proposal_id
  from quant.hypotheses h
  left join quant.experiments e on e.hypothesis_id = h.hypothesis_id
 where h.status in ('PROPOSED', 'PREREGISTERED', 'TESTING')
   and e.experiment_id is null
 order by h.created_at
 limit %s
"""

_SQL_FAMILY_PRESSURE = """
select trial_family_id, count(*), max(trial_number)
  from quant.experiments
 where trial_family_id is not null
 group by trial_family_id
 order by count(*) desc
 limit 12
"""


def _conn():
    from source_registry import load_project_env   # noqa: PLC0415

    import psycopg2                                # noqa: PLC0415

    return psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)


# ▶ **안 쓴 리드를 맨 앞에 둔다** (2026-08-12 실측)
#   예전 정렬은 `COMPLETE 먼저, 최신 먼저` 였다. 그래서 **이미 기획안으로
#   쓴 리드가 목록 맨 위**에 왔고, 에이전트가 그걸 먼저 보고 같은 계열을
#   재제안했다. Gate 0 는 "이 계열에서 이미 2회 기각됐다" 로 거부했고
#   발주가 0건이 되어 공장이 멈췄다.
#
#   그때 안 쓴 리드가 4건 있었다 - MAX 효과·오버나이트 수익 같은 **일봉으로
#   바로 검정 가능한** 고전 이상현상이었는데 목록 아래에 묻혀 있었다.
#   재료가 없어서가 아니라 **안 보여서** 멈춘 것이다.
_SQL_LEADS = """
select l.lead_id, l.scout_lens, l.claimed_edge, l.stated_mechanism,
       l.testability, l.status,
       exists (select 1 from research.experiment_proposals p
                where l.lead_id = any(p.lead_ids)) as used
  from research.methodology_leads l
 where l.status in ('COMPLETE', 'PARTIAL')
 order by used asc, (l.status = 'COMPLETE') desc, l.created_at desc
 limit %s
"""


# ▶ **스카우트 접근면을 카드에 싣는다** (2026-08-12, 재일 지시로 Agent-Reach 도입)
#   프로필에 렌즈가 4개 정의돼 있는데 실제 수집은 ACADEMIC 11 / PRACTITIONER 1 /
#   **COMMUNITY 0 / CROSSDOMAIN 0** 이었다. 뒤 둘이 노리는 포럼·영상에 닿을
#   도구가 없었기 때문이다. 그래서 리드가 전부 학술 논문이고 같은 우물만 팠다
#   (8/11 배치는 검정가능성이 통째로 VAGUE 로 떨어졌다).
#
#   도구를 깔아 두는 것만으로는 안 쓴다 - 오늘 `quant-py` 로 두 번 겪었다.
#   페르소나 4,400자에 묻히면 못 본다. **읽히는 자리에 명령을 적는다.**
#
#   ⚠ 리드 계약(`MethodologyLeadV1`)이 `refs.url` 을 요구한다. 되짚을 수 없는
#     출처는 접수되지 않으므로, 무엇을 읽었든 URL 을 남겨야 한다.
_SCOUT_SURFACE = """
[스카우트 접근면 - COMMUNITY·CROSSDOMAIN 렌즈가 쓸 것]
  agent-reach doctor                       # 지금 어떤 채널이 사는지
  curl -s https://r.jina.ai/<URL>          # 아무 웹페이지나 본문으로 읽는다
  yt-dlp --list-subs <youtube-url>         # 영상 자막 (강연·인터뷰)
  yt-dlp --write-auto-subs --skip-download -o - <url>
  reach-py -c "import feedparser; ..."     # RSS/Atom (arXiv q-fin 이 여기로 온다)
    ⚠ 시스템 `python3` 에는 feedparser 가 없다 - **`reach-py` 를 쓴다.**
      (2026-08-12 실측: python3 로 부른 에이전트가 ModuleNotFoundError 를 맞고
       uv run 으로 우회했다. 우회가 필요 없게 이름을 줬다.)
  agent-reach doctor 가 [!] 로 표시한 채널은 로그인 대기다 - 지어내지 말고 건너뛴다.

  실측(2026-08-12): arXiv q-fin.ST RSS 8건, Jina 로 임의 페이지 읽기, YouTube
  자막 추출 전부 확인됨. **안 된다고 적기 전에 위 명령을 실제로 돌려 본다.**

  ▶ 무엇을 읽든 `refs.url` 에 되짚을 수 있는 URL 을 남긴다 - 없으면 접수 거부다."""


def window_counts(dates: list, horizons: tuple = (5, 20, 60, 126, 252)) -> list:
    """형성창 길이별 walk-forward 창 개수. **실행면 함수를 그대로 돌린다.**

    ▶ 왜 (2026-08-12, 재일 지시)
      `667f0a45` 는 `walk-forward 창 0개 - 126일 형성 창이 634거래일 표본에 안
      들어간다` 로 죽었고, 에이전트가 `walk_forward_window_days` 로 고쳐 보려다
      **실행면이 그 키를 안 읽어** 거부됐다. 즉 **못 돌 이유는 알려주는데 어떻게
      해야 도는지는 안 알려줬다.** 그래서 측정이 0 이고 배우는 것도 0 이었다.

      창 개수는 판단이 아니라 산수다 - `make_windows` 가 반기로 묶고 시작
      인덱스가 웜업보다 작은 반기를 버린다. 그 함수를 여기서 그대로 돌려
      기획자에게 **고를 수 있는 값**을 준다. 표를 손으로 적지 않는 이유는
      오늘 여러 번 겪은 그대로다 - 손글씨 표는 실행면과 갈린다.
    """
    sys.path.insert(0, str(_ROOT / "departments" / "04-quant-backtest" / "pipeline"))
    from walk_forward import WARMUP_TRADING_DAYS, make_windows   # noqa: PLC0415

    out = []
    for h in horizons:
        # ▶ 실행면과 **같은 유도식**을 쓴다. 오케스트레이터가
        #   `max(WARMUP_TRADING_DAYS, template.min_history(config))` 로 잡으므로
        #   여기서도 그렇게 센다. 한때 `h + 10` 으로 어림했는데 그건 지어낸
        #   값이었다 - 브리핑에 실린 수는 실행면이 실제로 쓸 수와 같아야 한다.
        warmup = max(WARMUP_TRADING_DAYS, h + 1)   # 템플릿 min_history 는 대개 lookback+1
        try:
            out.append((h, len(make_windows(dates, warmup)), warmup))
        except Exception:        # noqa: BLE001 - 못 세면 적지 않는다(지어내지 않는다)
            return []
    return out


def _near_miss(conn, *, keep: int = 4) -> str:
    """**관문에 근접한 계열.** 여기 있는 것은 새 엣지보다 우선한다.

    ▶ 왜 (2026-08-12 실측)
      momentum 이 초과 +157.51%p · IR 1.26 · DSR 0.976 을 냈다. 다중검정을
      감안해도 우연이 아닌 성적이다 - **알파는 확인됐다.** 관문 8개 조항 중
      4개를 통과했고 막힌 건 낙폭(-50.52% vs 허용 -35%)이었다.

      그런데 환류에 남은 건 `fragility_fragile` 한 줄이라, 리서치는 그것을
      "이 엣지는 안 된다" 로 읽고 **다른 엣지를 설계했다.** 새 엣지도 위험관리가
      없기는 마찬가지라 같은 자리에서 같이 죽었다.

      찾은 알파를 버리고 새로 찾는 것보다, **찾은 알파에 위험관리를 붙이는
      쪽이 훨씬 싸다.** 그 판단을 하려면 근접했다는 사실을 알아야 한다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select trial_family_id, coalesce(notes, ''), lesson_codes
                  from research.experiment_outcomes
                 where notes like '관문%%통과%%'
                 order by decided_at desc limit 40""")
            rows = cur.fetchall() or []
    except Exception:      # noqa: BLE001 - 못 읽으면 적지 않는다(지어내지 않는다)
        return ""

    best: dict[str, tuple[int, str, list]] = {}
    for fam, note, lessons in rows:
        m = re.search(r"관문\s+(\d+)/(\d+)\s*통과", str(note))
        if not m:
            continue
        got = int(m.group(1))
        # 계열별 최고치만 남긴다 - 같은 계열의 나쁜 시도가 좋은 것을 가리면 안 된다
        if fam not in best or got > best[fam][0]:
            best[fam] = (got, str(note), list(lessons or []))

    # **절반 이상 통과한 것만** 올린다. 두세 조항 통과는 근접이 아니다.
    near = [(g, f, n, ls) for f, (g, n, ls) in best.items() if g >= 4]
    if not near:
        return ""
    near.sort(reverse=True)

    lines = ["\n[관문에 근접한 계열 - **새 엣지보다 이쪽이 먼저다**]"]
    for got, fam, note, lessons in near[:keep]:
        lines.append(f"  {str(fam)[:44]}")
        lines.append(f"    {note}")
        if lessons:
            lines.append(f"    교훈: {', '.join(str(x) for x in lessons)}")
    lines.append(
        "  ▶ 이 계열들은 **초과수익·정보비율·DSR 을 이미 통과했다** - 알파가\n"
        "    없어서 기각된 게 아니다. 남은 조항(대개 낙폭·강건성)을 겨냥해\n"
        "    같은 엣지에 위험관리를 붙인 기획안을 내라. 위 SUGGESTED_PARAMS 의\n"
        "    max_drawdown_stop / vol_target_annual 이 그것을 위한 손잡이다.\n"
        "    같은 계열 재시도는 trial 수를 올리므로(DSR 이 그만큼 조여진다)\n"
        "    아무 값이나 여러 번 던지지 말고 **근거를 갖고 한 번에 골라라.**")
    return "\n".join(lines)


def _dataset_news(conn) -> str:
    """지금 쓸 수 있는 데이터셋과 **그것이 무엇을 바꾸는가.**

    ▶ 왜 필요한가 (2026-08-12)
      Gate 0 는 "이 계열은 이미 기각됐다 - 교훈에 대응이 없다" 로 재제안을 막는다.
      옳은 규칙이다. 그런데 **표본이 넓어지면 그 교훈 자체가 해소된다** -
      `UNDERPOWERED_DATA`(표본 부족)·`SINGLE_REGIME_ONLY`(한 국면)는 데이터가
      길어지면 사라지는 종류다.

      오늘 데이터셋이 350종목·2년에서 3,924종목·10년으로 넓어졌는데
      **브리핑에 그 사실이 한 줄도 없었다.** 리서치는 재제안할 이유를 모르고,
      Gate 0 는 계속 막고, 공장은 새 표본을 못 써 본 채로 돈다.

      기각 사유를 돌려주는 것만으로는 부족하다 - **무엇이 달라졌는지**도 같이
      줘야 대응이 가능하다. 여기서 판단하지는 않는다(그건 기획자 몫이고),
      사실만 싣는다.
    """
    try:
        sys.path.insert(0, str(_ROOT / "departments" / "04-quant-backtest" / "pipeline"))
        from data_resolution import catalog          # noqa: PLC0415

        rows = catalog(conn)
    except Exception:  # noqa: BLE001 - 못 읽으면 적지 않는다(지어내지 않는다)
        return ""
    if not rows:
        return ""
    lines = ["\n[지금 쓸 수 있는 데이터셋 - 무엇으로 검정할 수 있는가]"]
    for r in rows:
        # ▶ **못 센 것을 0 으로 찍지 않는다** (2026-08-12). 유니버스가 안 걸린
        #   데이터셋이 `0종목` 으로 보여 "빈 데이터셋" 처럼 읽혔다 - 실제로는
        #   148,931행이 들어 있었다. 횡단면 순위를 매기려면 유니버스가 필요하니
        #   그 사실 자체가 기획에 필요한 정보다.
        syms = (f"{r['symbols']:>5,}종목" if r["symbols"] is not None
                else "유니버스없음")
        lines.append(f"  {r['dataset']:30} {','.join(r['sources'])[:34]:34} "
                     f"{r['span']:22} {syms:>9} {r['rows']:>10,}행")
    lines.append(
        "  ▶ **표본 부족으로 기각된 계열은 다시 제안할 수 있다.**\n"
        "    `UNDERPOWERED_DATA`·`SINGLE_REGIME_ONLY` 은 데이터가 길어지면 해소되는\n"
        "    교훈이다. 위 표에서 그때보다 넓어진 데이터셋이 있으면, 그 사실을\n"
        "    ECONOMIC_RATIONALE 에 적어 대응으로 삼아라 - Gate 0 는 대응이 있으면\n"
        "    통과시킨다. 넓어지지 않았는데 다시 내면 같은 이유로 또 막힌다.")
    return "\n".join(lines)


def _market_conn():
    """시세 DB. 없으면 None - 창 산수를 비운다(못 잰 것을 0 으로 적지 않는다)."""
    try:
        import os as _os                            # noqa: PLC0415

        import psycopg2                             # noqa: PLC0415
        dsn = _os.environ.get("TIMESCALE_DATABASE_URL")
        return psycopg2.connect(dsn, connect_timeout=10) if dsn else None
    except Exception:  # noqa: BLE001
        return None


def _window_math(market_conn) -> str:
    """창 산수 블록. 시세 DB 를 못 읽으면 **빈 문자열** - 없는 수를 지어내지 않는다."""
    if market_conn is None:
        return ""
    try:
        cur = market_conn.cursor()
        cur.execute("""
            select distinct bucket_time::date
              from market.market_bars
             where interval_code = '1D'
               and bucket_time::date between date '2024-01-02' and date '2026-07-30'
             order by 1
        """)
        dates = [r[0] for r in cur.fetchall()]
        if len(dates) < 60:
            return ""
        counts = window_counts(dates)
        if not counts:
            return ""
        tbl = " · ".join(f"{h}일→{n}창(웜업{w})" for h, n, w in counts)
        return ("\n[창 산수 - 형성창을 정하기 전에 본다]\n"
                f"  데이터셋 거래일 {len(dates)}일 ({dates[0]} ~ {dates[-1]})\n"
                f"  horizon_days 별 walk-forward 창: {tbl}\n"
                "  강건성 판정은 창이 3개 미만이면 성립하지 않는다. 창이 0개면\n"
                "  실험이 돌아도 `강건성을 재지 못했다` 로 끝난다.\n"
                "  **창이 3개 이상 나오는 horizon_days 를 골라라.** 더 긴 형성창이\n"
                "  꼭 필요하면 데이터셋을 늘려야 하는 일이므로 그렇게 적어라 -\n"
                "  돌려 보고 창이 없다고 하는 것보다 낫다.\n"
                "  (웜업은 전략이 선언한 min_history 를 따른다 - 2026-08-12 이전에는\n"
                "   30일 고정이라 긴 형성창 전략이 시그널을 못 냈다.)")
    except Exception:  # noqa: BLE001
        return ""


def _vocab_block(conn=None) -> str:
    """실행면이 표현할 수 있는 어휘. **여기 없는 값은 반려된다.**

    ▶ **목록을 손으로 적지 않는다** (2026-08-11 실측). 프롬프트에 코드 목록을
      직접 써넣었다가 실제 어휘에 없는 `REGIME_ARTIFACT` 를 시켜서, 제대로 쓴
      기획안이 `경쟁 설명 코드가 어휘 밖이다` 로 반려됐다. 에이전트가 시킨 대로
      했는데 막힌 것이다 - 어휘는 **정의된 곳에서 읽어온다.**
    """
    sys.path.insert(0, str(_ROOT / "departments" / "04-quant-backtest" / "pipeline"))
    from strategy_templates import EDGE_VOCAB      # noqa: PLC0415
    from trial_family import UNIVERSE_VOCAB        # noqa: PLC0415

    from contracts.factory_contracts import (      # noqa: PLC0415
        CompetingExplanation, SourceType)

    # ▶ **목록에 있다고 데이터셋이 있는 것은 아니다** (2026-08-12 실측)
    #   여기는 `SOURCE_TABLES` 전체를 나열했다. 그런데 실험이 사상하는 근거는
    #   `quant.dataset_manifests` 의 `source_versions` 이고, 지금 덮이는 것은
    #   `market_bars` 와 `microstructure_features` 뿐이다. 기획자는 목록에
    #   있으니 `market_quotes`·`market_ticks` 를 골랐고, 그 가설은 전부
    #   `사상할 데이터셋이 없다` 로 죽었다(breakout·momentum 실측).
    #   **못 돌 것을 권한 쪽이 문제다** - 어휘 때와 같은 사고다.
    sources = ""
    try:
        from data_resolution import SOURCE_TABLES  # noqa: PLC0415

        covered: set = set()
        if conn is not None:
            from data_resolution import manifest_index  # noqa: PLC0415

            for _n, _v, srcs in manifest_index(conn):
                covered |= set(srcs or {})
        rest = sorted(set(SOURCE_TABLES) - covered)
        if covered:
            sources = ("\n  DATA_TABLES  : " + ", ".join(sorted(covered))
                       + "\n    ▶ **위 목록만 실험까지 간다** - 데이터셋 매니페스트가 "
                         "덮는 원천이다.\n"
                       f"    아직 데이터셋이 없는 원천: {', '.join(rest)}\n"
                       "    이쪽을 적으면 접수는 되지만 실험 단계에서 "
                       "'사상할 데이터셋이 없다' 로 막힌다.")
        else:
            sources = f"\n  DATA_TABLES  : {', '.join(sorted(SOURCE_TABLES))}"
    except Exception:  # noqa: BLE001 - 어휘를 못 읽으면 적지 않는다(지어내지 않는다)
        pass
    _ = SourceType

    # ▶ **어휘에 있다고 돌 수 있는 것은 아니다** (2026-08-12 실측)
    #   EDGE_VOCAB 은 접수 게이트가 받는 이름이고, 실제로 백테스트가 도는 것은
    #   STRATEGY_CATALOG 에 구현이 등재된 것뿐이다. 둘이 갈린 채로 브리핑에
    #   EDGE_VOCAB 만 실었더니, 기획자가 `low_volatility` 로 기획안을 냈고
    #   접수·Gate 0 을 다 통과한 뒤 실험 단계에서 `'low_volatility' 전략 구현
    #   (STRATEGY_CATALOG 등재 조건)` 으로 죽었다. **못 돌 것을 권한 쪽이 문제다.**
    #
    #   ▷ 그 뒤 원인이 하나 더 있었다(같은 날): STRATEGY_CATALOG 자체가 손글씨
    #     표라 실행면(strategy_templates.TEMPLATES)에 8개가 구현된 뒤에도 2개만
    #     적혀 있었다. 즉 `low_volatility` 는 **구현이 있는데 반려된** 것이다.
    #     이제 STRATEGY_CATALOG 은 TEMPLATES 파생 뷰라 둘이 갈릴 수 없다.
    #     여기서 읽는 값도 자동으로 따라오므로 이 브리핑은 그대로 둔다.
    #
    #   마찬가지로 파라미터도 실행면(config_binding.EDGE_KEYS)이 읽는 것만 쓸 수
    #   있다. 밖의 키를 쓰면 "등록한 가설과 실행한 실험이 달라진다"며 거부되는데,
    #   기획자는 그 목록을 볼 길이 없었다(실측: signal_window_days 로 거부).
    runnable = params = ""
    try:
        from experiment_orchestrator import STRATEGY_CATALOG  # noqa: PLC0415

        impl = sorted(STRATEGY_CATALOG)
        runnable = (
            "\n  ▶ 지금 **실험까지 도는** EDGE_TYPE: "
            f"{', '.join(impl)}\n"
            "    나머지는 접수는 되지만 실험 단계에서 '전략 구현' 으로 막힌다.\n"
            "  ▶ **리드가 이 어휘에 안 맞으면 두 길이 있다** (2026-08-12 개방)\n"
            "    ① 가장 가까운 어휘로 사상하고 **무엇을 잃는지 본문에 적는다**\n"
            "    ② **실행면을 넓힌다** - 새 시그널 템플릿을 만들어도 된다\n"
            "       (experiment-factory §4 '실행면을 진화시킨다'). `code_version`\n"
            "       이 코드 해시를 물고 있어 옛 실험은 오염되지 않는다.\n"
            "    예전엔 여기에 '구현된 것 중에서 고르는 편이 한 주기를 아낀다'\n"
            "    고 적혀 있었다. 그때는 넓힐 수단이 없어서 맞는 말이었지만,\n"
            "    지금은 **어휘가 좁아서 좋은 리드를 버리는 쪽이 더 비싸다** -\n"
            "    실측으로 미사용 리드 2건(오버나이트 수익, MAX 효과)이 어휘에\n"
            "    대응이 없어 묶여 있었다.")
    except Exception:  # noqa: BLE001 - 못 읽으면 적지 않는다(지어내지 않는다)
        pass
    try:
        from config_binding import EDGE_KEYS      # noqa: PLC0415

        params = ("\n  SUGGESTED_PARAMS 에 쓸 수 있는 키: "
                  f"{', '.join(sorted(EDGE_KEYS))}\n"
                  "    이 밖의 키는 실행면이 읽지 않아 거부된다.\n"
                  "  ▶ 위험관리 손잡이(2026-08-12 개방). **엣지를 바꾸지 않고 낙폭을\n"
                  "    줄이는 길이다** - 지금까지 실행면은 완전투자 동일가중밖에\n"
                  "    표현하지 못했고, 그래서 알파가 있는 전략도 낙폭에서 죽었다.\n"
                  "      max_drawdown_stop  고점 대비 이 낙폭이면 전량 현금 (-0.25 = -25%)\n"
                  "      vol_target_annual  목표 연변동성 (0.15 = 15%). 실현변동성이\n"
                  "                         크면 그만큼 노출을 줄인다\n"
                  "      max_exposure       익스포저 상한 (1.0 = 완전투자, 상한이 1.0)\n"
                  "      vol_lookback_days  변동성 추정 창 (기본 60)\n"
                  "    안 적으면 꺼진 채로 돈다(예전과 동일). **값은 네가 정한다** -\n"
                  "    무엇을 얼마로 걸지가 이 실험의 가설이다.\n"
                  "  ▶ 집중도(top_n) 구조 진단 (2026-08-13 실측 - 기존 실험 로그\n"
                  "    재계산이지 새 실험이 아니다. 시도예산·DSR 관문은 그대로다)\n"
                  "      지금까지 전 실험이 관례 기본값 top-20 으로 돌았고, 그\n"
                  "      초집중이 TC 0.114(신호의 89%가 비중에 전달되지 않음)와\n"
                  "      TE 연 34.6% 의 주범이었다 - IR 미달의 범인은 신호가\n"
                  "      아니라 구조였다. N=200 동일가중은 TC 0.316(2.8배)이고\n"
                  "      top_n 상한은 300 까지 열려 있다. 순위(신호비례)가중은\n"
                  "      같은 N 에서 동일가중보다 일관 열위 - 폭이 결정한다.\n"
                  "      MAX 신호 IC -0.108(t -8.4, 페니주 필터 무영향, 반쪽\n"
                  "      분할 모두 유의) - KRX 문헌(KCI·JDQS)의 '알파는 숏다리,\n"
                  "      롱온리는 빼기(고MAX 배제)+광폭 보유' 와 같은 방향이다.\n"
                  "      top_n 을 관례에 맡기지 말고 가설이 직접 정하라.")
    except Exception:  # noqa: BLE001
        pass

    return ("\n[통제 어휘 - 밖의 값은 반려된다]\n"
            f"  EDGE_TYPE    : {', '.join(sorted(EDGE_VOCAB))}\n"
            f"  UNIVERSE_KEY : {', '.join(sorted(UNIVERSE_VOCAB))}\n"
            f"  COMPETING_CODES: {', '.join(c.value for c in CompetingExplanation)}"
            + sources + runnable + params)


_RE_REREGISTER = re.compile(r"\s*-?\s*(가설|부모)을?를?\s*다시 등록해야 한다"
                            r"(\([^)]*\))?")


def _level_of(why: str) -> str:
    """이 막힘이 어느 개입 수준인가. 못 가리면 `불명` - 지어내지 않는다.

    수준을 안 적으면 기획자가 "내가 고칠 것인가" 를 매번 다시 판단한다.
    적어 두면 자기 몫이 아닌 것은 넘기고 자기 몫만 집는다.
    """
    try:
        import attribution                        # noqa: PLC0415
        return attribution.attribute(why).level
    except Exception:  # noqa: BLE001
        return "불명"


def _progress_line(conn) -> str:
    """**돌릴수록 나아지고 있는가.** 못 재면 빈 문자열 - 지어내지 않는다."""
    try:
        import progress                           # noqa: PLC0415

        return progress.brief_block(progress.measure(conn))
    except Exception:  # noqa: BLE001
        return ""


def _pareto_line(conn) -> str:
    """**결함 파레토 + 테마별 π₀.** 못 재면 빈 문자열 - 지어내지 않는다.

    ▶ 왜 (2026-08-13, 수율 램프 문헌)
      반도체 수율 학습의 핵심 관행이 결함 파레토 상설 게시다 - 최빈 결함부터
      공정을 고친다. 우리 결함 = FRACAS 근본원인. 그리고 테마별 REJECT/시도
      비율은 GBH 의 π₀ 추정치다 - 버려지던 판정들이 다음 탐색의 사전확률이
      된다. KRX 복제 연구가 예언한 그림(수익성류 5% 생존)과 우리 실측이
      맞는지도 여기서 드러난다.
    """
    try:
        sys.path.insert(0, "/app/departments/04-quant-backtest/pipeline")
        from trial_family import theme_of          # noqa: PLC0415

        out = []
        with conn.cursor() as cur:
            cur.execute("""select root_cause, count(*)
                             from research.experiment_outcomes
                            where root_cause is not null
                            group by 1 order by 2 desc""")
            pareto = cur.fetchall()
            if pareto:
                out.append("\n[결함 파레토 - 최빈 결함부터 고친다 (수율 램프 관행)]")
                total = sum(n for _, n in pareto)
                for rc, n in pareto:
                    out.append(f"  {rc:<10} {n}건 ({100*n//total}%)")
                out.append("  ▶ 1위 결함을 없애는 개선이 같은 노력의 다른 어떤 "
                           "개선보다 크다 - 카드를 고를 때 이 표를 먼저 봐라.")
            cur.execute("""
                select coalesce(h.expected_edge->>'type',''), o.decision
                  from research.experiment_outcomes o
                  join quant.hypotheses h
                    on h.hypothesis_id::text = o.hypothesis_id""")
            agg: dict = {}
            for edge, dec in cur.fetchall():
                t = theme_of(edge)
                a = agg.setdefault(t, [0, 0])
                a[0] += 1
                if str(dec).upper() in ("REJECT", "GATE_HOLD", "KILLED"):
                    a[1] += 1
            if agg:
                out.append("[테마별 성적 - 기각율이 낮은 테마가 사전확률이 높다]")
                for t, (n, rej) in sorted(agg.items(), key=lambda kv: kv[1][1]/max(kv[1][0],1)):
                    out.append(f"  {t:<6} 시도 {n} · 기각 {rej} ({100*rej//max(n,1)}%)")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return ""


def _soundness_line(conn) -> str:
    """**공장이 지금 나아갈 수 있는 상태인가.** 판정 한 줄.

    ▶ 왜 맨 앞인가 (2026-08-12)
      오늘 주기가 `발주 0건` 으로 끝났는데 공장은 그 사실을 한 번도 말하지
      않았다. 카드마다 개별 병목은 있었지만 "지금 이 공장은 앞으로 못 간다"
      는 판정이 없었다 - 안전만 재고 진행(liveness)을 안 쟀기 때문이다.
      건전성 3조항이 그 판정을 정의해 놨으니 그것을 맨 앞에 건다.
    """
    try:
        import attribution as attr                # noqa: PLC0415
        import soundness as snd                   # noqa: PLC0415

        def _blocked(c, ids):
            return _structurally_blocked(c, ids) or {}

        violations, v = snd.report(conn, blocked_fn=_blocked)
    except Exception:  # noqa: BLE001 - 못 재면 판정하지 않는다
        return ""
    if v["sound"]:
        return ("\n[공장 건전성] **건전하다** - 완주가능·죽은전이·정상종결 "
                "3조항 위반 0건.")
    lines = [f"\n[공장 건전성] **건전하지 않다** - 위반 {v['total']}건 "
             f"({' · '.join(v['clauses_violated'])})"]
    by_level: dict[str, int] = {}
    for x in violations:
        lv = attr.attribute(x.why).level
        by_level[lv] = by_level.get(lv, 0) + 1
    lines.append("  수준별: " + " · ".join(f"{k} {n}건"
                                          for k, n in sorted(by_level.items())))
    lines.append("  ▶ 건전성은 **진행(liveness)** 판정이다. 개별 실험이 잘 돌아도 "
                 "여기가 빨간 동안은 공장이 앞으로 못 간다.")
    lines.append("    완주가능=종결에 닿을 수 있나 · 죽은전이=한 번도 안 돈 "
                 "활동이 있나 · 정상종결=끝났는데 남은 것이 있나")
    return "\n".join(lines)


def _split_blocked(blocked: dict, orphan_ids) -> tuple[dict, dict]:
    """막힌 가설을 **고칠 주체가 있는 것**과 **없는 것**으로 가른다.

    "다시 등록해라" 는 등록할 원본이 있을 때만 성립한다. 배분자 변형은
    `proposal_id` 가 없어서 리서치가 다시 낼 것이 없다 - 그 말을 그대로
    적으면 아무도 안 움직이고 계열이 통째로 멈춘다(실측 8일).
    """
    orphan = set(orphan_ids or ())
    own = {h: w for h, w in blocked.items() if h not in orphan}
    # 고아 줄에서는 "다시 등록해야 한다" 를 떼어 낸다. 머리말이 "등록할 원본이
    # 없다" 인데 줄마다 등록하라고 적혀 있으면 읽는 쪽이 어느 쪽을 믿을지
    # 모른다 - 엇갈린 신호가 8일을 끌었다. 사유(무엇이 안 읽히는가)는 남긴다.
    lost = {h: _RE_REREGISTER.sub("", w).strip(" -")
            for h, w in blocked.items() if h in orphan}
    return own, lost


REJECT_PATH = Path.home() / ".factory_autopilot_rejections"
REJECT_KEEP = 6      # 브리핑에 싣는 최근 반려 수 - 오래된 것까지 실으면 잡음이 된다


def record_rejections(rejected, *, stamp: str, path: Path | None = None) -> int:
    """반려를 다음 브리핑이 읽을 수 있게 남긴다. 실패해도 수확을 죽이지 않는다."""
    path = path or REJECT_PATH
    if not rejected:
        return 0
    try:
        with path.open("a", encoding="utf-8") as fh:
            for x in rejected:
                one = f"{stamp}\t{x.title[:70]}\t{x.reason[:160]}"
                fh.write(one.replace("\n", " ") + "\n")
    except OSError as e:
        print(f"      ⚠ 반려 기록 실패 - 다음 주기가 같은 실수를 반복할 수 있다: "
              f"{e}", flush=True)
        return 0
    return len(rejected)


def recent_rejections(*, keep: int = REJECT_KEEP, path: Path | None = None) -> list[str]:
    """최근 반려 몇 건. 못 읽으면 빈 목록 - 없는 교훈을 지어내지 않는다."""
    path = path or REJECT_PATH
    try:
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []
    return lines[-keep:]


def research_brief() -> str:
    """리서치본부가 다음 기획안을 낼 때 볼 사실. 결론은 없다.

    ▶ 리드 id 를 **실제로 싣는다** (2026-08-11). 예전엔 "원장에 리드 12건이 있다"
      고만 알려줬는데, `factory_submit_proposal` 은 LEAD_IDS 를 원장에서 다시 읽어
      대조하므로 id 를 모르면 인용할 수가 없다. 에이전트는 정직하게
      "근거 리드가 입력에 없어 문헌 근거로 가장하지 않는다"고 적고 멈췄다 -
      맞는 판단이었고, 못 준 쪽이 문제였다.
    """
    import cycle_brief                             # noqa: PLC0415

    # ▶ **연결은 브리핑을 다 만든 뒤에 닫는다** (2026-08-12 실측)
    #   여기서 리드만 읽고 바로 닫고 있었는데, 아래 네 블록이 **닫힌 연결로**
    #   조회하고 있었다. 각 블록은 예외를 삼키고 빈 문자열을 돌려주므로
    #   (`못 읽으면 적지 않는다` - 옳은 처리다) 아무 소리 없이 사라졌다:
    #
    #     _vocab_block      - 매니페스트가 덮는 표 목록이 죽음
    #     _dataset_news     - **통째로 죽음**. 데이터셋이 넓어진 사실이 안 감
    #     _near_miss        - 관문 근접 계열이 안 감
    #     막힌 가설 블록     - 774f3b75 가 8/4부터 막혀 있는데 리서치가 몰랐던 이유
    #
    #   "리서치에 보여 준다" 고 적어 놓은 블록이 정작 도달한 적이 없었다.
    #   WAL 걱정으로 일찍 닫은 것인데, 일찍 닫으면 안 닫은 것만 못하다.
    conn = _conn()
    try:
        brief = cycle_brief.build(conn)
        with conn.cursor() as cur:
            cur.execute(_SQL_LEADS, (12,))
            leads = cur.fetchall()
        return _compose_research_brief(conn, brief, leads)
    finally:
        conn.close()      # WAL 을 남기지 않는다 - with 는 커밋만 하고 안 닫는다


def _compose_research_brief(conn, brief, leads) -> str:
    """브리핑 본문. **연결이 살아 있는 동안** 조회 블록을 다 만든다."""
    out = [brief.as_prompt()]
    if leads:
        out.append("\n쓸 수 있는 리드 (LEAD_IDS 에 이 id 만 쓴다):")
        for row in leads:
            lid, lens, edge, mech, test, status = row[:6]
            used = bool(row[6]) if len(row) > 6 else False
            # **쓴 것과 안 쓴 것을 눈에 보이게 가른다.** 안 가르면 이미
            # 기각된 계열을 다시 내게 된다(2026-08-12 실측으로 공장이 멈췄다)
            tag = "이미 씀" if used else "**미사용**"
            out.append(f"  - {lid}  [{lens}/{status}] {tag} {str(edge)[:62]}")
            if mech:
                out.append(f"      메커니즘: {str(mech)[:110]}")
    else:
        out.append("\n**쓸 수 있는 리드가 없다.** 리드 없이 기획안을 내지 마라 - "
                   "LEAD_IDS 가 비면 접수 전에 반려된다.")
    out.append(_vocab_block(conn))
    # ▶ **공장이 나아갈 수 있는 상태인지 먼저 말한다.** 개별 병목보다 앞이다 -
    #   빨간 동안은 무엇을 기획해도 실험까지 안 간다(오늘 발주 0건).
    out.append(_soundness_line(conn))
    # ▶ **건전성 바로 뒤에 전진을 둔다** (2026-08-13). 둘은 다른 질문이다 -
    #   건전=지금 갈 수 있나, 전진=실제로 나아갔나. 건전한데 제자리인 공장이
    #   가능하고 그게 제일 위험하다(아무 경보도 안 울린다).
    out.append(_progress_line(conn))
    out.append(_pareto_line(conn))
    # ▶ **재료 부족을 맨 앞에 둔다.** 리드가 마르면 기획안을 내는 것 자체가
    #   낭비다 - 이미 기각된 계열을 다시 내게 된다(2026-08-12 실측으로
    #   그렇게 공장이 멈췄다). 무엇을 낼지 고르기 전에 낼 재료가 있는지부터.
    out.append(_lead_health(conn))
    # 근접 계열을 어휘 바로 뒤에 둔다 - 손잡이 목록을 읽은 직후에 "어디에
    # 쓸지" 가 나와야 한다. 멀리 떨어뜨리면 둘을 잇지 못한다.
    out.append(_near_miss(conn))
    out.append(_dataset_news(conn))
    # ▶ **막힌 가설을 리서치에 보여 준다** (2026-08-12)
    #   발주 게이트가 "못 돈다" 고 판정한 가설이 원장에 앉아 있는데, 그 사실이
    #   리서치에 안 갔다. 그래서 기획자는 자기 기획안이 왜 실험까지 못 가는지
    #   모른 채 다음 것을 냈고, 막힌 것은 계속 막혀 있었다(`774f3b75` 가
    #   8월 4일부터). **다시 기획하면 풀리는 것들이라 알려주면 된다.**
    #
    # ▶ **고아를 갈라 적는다** (2026-08-12, 같은 날 뒤늦게 발견)
    #   "다시 등록해라" 는 등록할 주체가 있을 때만 말이 된다. 배분자가 만든
    #   변형은 `proposal_id` 가 없어서 **리서치가 다시 낼 원본이 없다.**
    #   실측: `774f3b75`(8/4) 가 그렇고, 그 변형 `e2379857`·`deb25128` 이
    #   `observation_refs, universe` 를 물려받아 같이 막혔다. 배분자는 못 도는
    #   부모의 변형을 거부하므로(오늘 넣은 가드), 이대로 두면 그 계열은
    #   **주인 없이 영구 동결**된다 - 아무도 고칠 수 없는 상태가 제일 나쁘다.
    #   그래서 둘을 갈라 적고, 고아는 무엇을 고를지까지 말해 준다.
    try:
        with conn.cursor() as _c:
            _c.execute("select hypothesis_id::text, proposal_id "
                       "from quant.hypotheses "
                       "where status in ('PROPOSED','RUNNING')")
            _rows = _c.fetchall()
        _ids = [r[0] for r in _rows]
        _orphan = {r[0] for r in _rows if not r[1]}
        _blocked = _structurally_blocked(conn, _ids)
        if _blocked:
            _own, _lost = _split_blocked(_blocked, _orphan)
            out.append("")
            if _own:
                out.append("[막혀 있는 가설 - 다시 기획하면 풀린다]")
                for _h, _why in _own.items():
                    out.append(f"  {_h[:8]} [{_level_of(_why)}]: {_why[:150]}")
                out.append("  ▶ 내용이 나쁜 게 아니라 **계약이 안 맞아** 실험까지 "
                           "못 간다.")
                out.append("    같은 아이디어를 위 통제 어휘·허용 키로 다시 내면 "
                           "그대로 돈다.")
                out.append("    새로 내면 옛 가설은 대체된다 - 같은 것을 두 번 "
                           "세지 않는다.")
            if _lost:
                out.append("")
                out.append("[주인 없이 막힌 가설 - **다시 등록할 원본이 없다**]")
                for _h, _why in _lost.items():
                    out.append(f"  {_h[:8]} [{_level_of(_why)}]: {_why[:150]}")
                out.append("  ▶ 이것들은 배분자가 만든 변형이라 기획안이 없다. "
                           "'다시 등록해라' 가 성립하지 않는다 - 계약 검사가 "
                           "생기기 전에 태어나서 지금 기준으로는 처음부터 "
                           "실행 불가였다.")
                out.append("  ▶ 배분자는 못 도는 부모의 변형을 거부하므로 "
                           "**그 계열 전체가 멈춰 있다.** 둘 중 하나를 골라라:")
                out.append("    ① 같은 아이디어를 허용 키로 **새 기획안**으로 내라 "
                           "- 그러면 계열이 정상 혈통으로 다시 산다.")
                out.append("    ② 살릴 값이 없으면 **폐기 사유를 남기고 ARCHIVED** "
                           "로 내려라 - 그래야 배분자가 다음 계열로 넘어간다.")
                out.append("    고르지 않고 두는 것만 하지 마라. 그게 지금까지 "
                           "8일이었다.")
    except Exception:  # noqa: BLE001 - 못 세면 적지 않는다(지어내지 않는다)
        pass
    # 창 산수는 시세 DB 를 봐야 나온다. 실패하면 빈 문자열이라 브리핑이 조용히
    # 짧아질 뿐, 없는 수가 실리지는 않는다.
    _mk = _market_conn()
    try:
        out.append(_window_math(_mk))
    finally:
        if _mk:
            _mk.close()
    out.append(_SCOUT_SURFACE)
    # ▶ 지난 반려를 싣는다. 어휘를 나열하는 것만으로는 안 고쳐진다 - 8/11 에
    #   기획자가 REGIME_ARTIFACT 를 지어내 반려됐는데, 그 사실이 다음 브리핑에
    #   없어서 같은 코드로 또 냈다. 기각 사유가 돌아와야 파훼가 가능하다.
    rej = recent_rejections()
    if rej:
        out.append("\n[지난 반려 - 같은 이유로 또 내지 마라]")
        for line in rej:
            parts = line.split("\t")
            stamp, title, reason = (parts + ["", "", ""])[:3]
            out.append(f"  - ({stamp}) {title}\n      반려 사유: {reason}")
        out.append(
            "  위 사유를 읽고 **그 원인을 제거한 기획안**을 내라. "
            "통제 어휘 밖의 값을 지어내지 마라 - 어휘는 실행면에서 파생되므로 "
            "지어낸 값은 접수돼도 실행 단계에서 죽는다.\n"
            "  다만 **깎는 것이 기본값은 아니다.** 필요한 개념이 어휘에 없으면 "
            "가장 가까운 코드로 사상하고 잃은 것을 본문에 적어도 되고, 실행면을 "
            "넓혀 그 개념을 만들어도 된다. 어느 쪽이든 **무엇이 없어서 그랬는지 "
            "본문에 남겨라** - 조용히 깎으면 그 수요는 아무 데도 안 남고, 다음 "
            "주기의 너는 같은 것을 또 깎는다(실측: 기획안 11건 전부 어휘 안이라 "
            "어휘 반려가 한 번도 없었다. 안 막힌 게 아니라 아무도 안 물은 것이다).")
    return "\n".join(out)


def _widest_price_dataset(conn) -> tuple[str, int | None]:
    """지금 있는 **가장 긴 가격 데이터셋**과 그 거래일 수.

    배분자가 "표본을 넓히는 수" 를 낼 때 무엇으로 넓힐지 알아야 한다.
    못 읽으면 빈 값 - 없는 데이터셋을 지어내지 않는다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select m.name, m.version,
                       (select count(*) from quant.dataset_partitions p
                         where p.dataset_id = m.dataset_id) parts,
                       m.row_count
                  from quant.dataset_manifests m
                 where m.source_versions ? 'market_bars'
                 order by m.row_count desc limit 1""")
            got = cur.fetchone()
    except Exception:  # noqa: BLE001
        return "", None
    if not got:
        return "", None
    name, ver, parts, _rc = got
    # 파티션은 월 단위다. 월당 거래일 ~21일 - 어림수이고 그렇게 밝힌다.
    days = int(parts) * 21 if parts else None
    return f"{name}/{ver}", days


# 안 쓴 리드가 이보다 적으면 재료가 마른 것이다. 기획안 하나가 리드 하나를
# 쓰므로, 이 아래로 떨어지면 다음 주기에 낼 것이 없다.
LEADS_LOW = 4
# 이보다 오래된 리드만 있으면 시장이 바뀌었을 수 있다.
LEADS_STALE_DAYS = 2


def _lead_health(conn) -> str:
    """**재료가 남아 있나.** 마르면 스카우트가 먼저다.

    ▶ 왜 카드에 싣나 (2026-08-12 실측)
      공장이 통째로 멈춰 있었다. 사슬은 이랬다:

        스카우트 정지(08-11 이후) → 리드 12건 고갈 → 리서치가 **같은 계열을
        재제안** → Gate 0 가 "이미 2회 기각됐다" 로 거부 → 발주 0건

      `LESSONS_ADDRESSED` 채널을 열어도 안 풀린다 - 재제안이 아니라 **새 계열**
      이 필요한데 재료가 없었다. 그리고 자동 조종에는 **스카우트를 부르는 곳이
      아예 없었다.** 페르소나는 "스카우트를 파견하라" 고 하는데 카드는
      "기획안을 내라" 만 요구했다.

      우리가 스카우트를 강제하지 않는다 - **사실을 보여주고 판단은 에이전트가**
      한다. 재료가 있는데 억지로 스카우트하면 그것도 낭비다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select count(*),
                       count(*) filter (
                         where not exists (
                           select 1 from research.experiment_proposals p
                            where methodology_leads.lead_id = any(p.lead_ids))),
                       extract(epoch from (now() - max(created_at)))/86400.0
                  from research.methodology_leads""")
            total, unused, age = cur.fetchone()
    except Exception:  # noqa: BLE001 - 못 세면 안 적는다
        return ""
    total, unused = int(total or 0), int(unused or 0)
    age = float(age or 0)
    if unused >= LEADS_LOW and age < LEADS_STALE_DAYS:
        return ""                      # 재료가 넉넉하면 말하지 않는다

    why = []
    if unused < LEADS_LOW:
        why.append(f"안 쓴 리드가 {unused}건뿐이다(기준 {LEADS_LOW})")
    if age >= LEADS_STALE_DAYS:
        why.append(f"가장 최근 리드가 {age:.1f}일 전이다")
    return "\n".join([
        "",
        f"[재료 부족 - **기획 전에 스카우트가 먼저다**] 리드 {total}건 중 "
        f"안 쓴 것 {unused}건",
        "  " + " · ".join(why),
        "  ▶ 이 상태로 기획안을 내면 **이미 기각된 계열을 다시 내게 된다.**",
        "    실측(2026-08-12): 그렇게 낸 기획안 2건이 Gate 0 에서 "
        "'이미 2회 기각됐다 - 교훈에 대응이 없다' 로 거부됐고 발주가 0건이 됐다.",
        "  ▶ **네가 직접 훑어라.** `agent-reach doctor` 로 살아 있는 채널을 보고,",
        "    arXiv q-fin 은 feedparser, 임의 페이지는 `curl -s https://r.jina.ai/<URL>`,",
        "    발표·인터뷰는 `yt-dlp --write-auto-subs --skip-download`.",
        "    렌즈를 순서대로 돌리고 **어느 렌즈가 말랐는지 말해라** - 빈 것도 사실이다.",
        "  ▶ 재검증 가능한 URL 을 반드시 들고 와라. 다시 열 수 없는 리드는 접수에서 막힌다.",
    ])


def _unrunnable_falsifications(conn, *, keep: int = 4) -> str:
    """**네가 설계한 반증 중 기계가 못 돌리는 것.** 수요 순으로 모은다.

    ▶ 왜 카드에 싣나 (2026-08-12)
      계약은 "반증 없는 가설은 미완성" 이라며 강제한다. 에이전트는 성실히
      썼다 - 기획안 10건에 49개. 그중 절반은 기계가 돌릴 수 있게 됐지만
      **베타 중립화·유동성 층화·placebo·분위수 단조성은 실행면에 기계가 없다.**

      그걸 우리가 만들면 또 우리 코드가 두뇌가 된다. 에이전트는 이미 무엇이
      필요한지 알고 있다 - **자기가 그걸 설계했기 때문이다.** 우리가 할 일은
      "네 설계 중 이건 못 돌린다" 를 보여 주는 것까지다.

      수요를 세어 순서를 매긴다. 아홉 번 요청된 것과 한 번 요청된 것은
      만들 가치가 다르다.
    """
    try:
        sys.path.insert(0, str(_ROOT / "departments" / "04-quant-backtest"
                               / "pipeline"))
        import falsification as fx  # noqa: PLC0415

        with conn.cursor() as cur:
            cur.execute("""
                select falsification_criteria from quant.hypotheses
                 where falsification_criteria is not null
                   and created_at > now() - interval '30 days'""")
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - 못 세면 안 적는다
        return ""

    demand: dict[str, list] = {}
    unclassified: list[str] = []
    total = ran = 0
    for (crits,) in rows:
        for t in (crits or []):
            total += 1
            kind, runnable = fx.classify(str(t))
            if runnable:
                ran += 1
            elif kind == "unclassified":
                # **분류된 것과 섞지 않는다.** "기계가 없다" 와 "무슨 말인지
                # 못 알아들었다" 는 다른 문제이고 고칠 곳도 다르다.
                unclassified.append(str(t))
            else:
                demand.setdefault(kind, []).append(str(t))
    if not demand and not unclassified:
        return ""

    order = sorted(demand.items(), key=lambda kv: -len(kv[1]))
    named = sum(len(v) for v in demand.values())
    lines = [f"\n[네가 설계한 반증 {total}개 - 실행 {ran} · "
             f"기계 없음 {named} · 못 읽음 {len(unclassified)}]"]
    for kind, texts in order[:keep]:
        lines.append(f"  {kind} · {len(texts)}회 요청")
        lines.append(f"      예: {texts[0][:74]}")
    if unclassified:
        lines += [
            "",
            f"  못 읽은 것 {len(unclassified)}개 - **기계가 없는 것과 다른 문제다.**",
            f"      예: {unclassified[0][:74]}",
            "      측정 가능한 문장으로 다시 쓰거나(무엇이 얼마면 기각인가),",
            "      원장에 잘못 저장된 것일 수 있다 - 2026-08-12 에 `['note',",
            "      'fragility_rules']` 가 반증 기준으로 앉아 있었다(dict 를",
            "      목록 자리에 넣은 것). 그런 것이 보이면 그 자체가 결함이다.",
        ]
    lines += [
        "",
        "  ▶ **이건 실행면에 기계가 없다는 뜻이지 반증이 틀렸다는 뜻이 아니다.**",
        "    돌리지 못한 것은 판정에 `미실행` 으로 남는다 - 통과로 세지 않는다.",
        "  ▶ **네가 만들어라.** 러너·창 분할·비용 모델까지 고쳐도 된다",
        "    (experiment-factory §4 '실행면을 진화시킨다'). 요청이 많은 것부터.",
        "  ▶ 만들었으면 `falsification.py` 의 `_PATTERNS` 에 등록해 다음부터",
        "    자동으로 돌게 하고, `skill-authoring` 으로 스킬에 남겨라.",
        "    안 남기면 다음 주기의 네가 같은 자리를 다시 판다.",
    ]
    return "\n".join(lines)


def _allocator_block(conn) -> tuple[str, int]:
    """배분자가 고른 다음 수. 반환: (본문, 둘 수 있는 수의 개수).

    ▶ 왜 브리핑에 싣나 (2026-08-12)
      발주가 도착 순서라 **최고 전략(Sharpe 1.276)이 2년짜리 데이터로 돌고,
      최악 전략(Sharpe -0.958)이 10년짜리를 썼다.** 무엇이 유망한지 아무도
      안 봤기 때문이다. 배분자는 그 재료를 만들고, 고르는 것은 에이전트다.
    """
    try:
        import allocator  # noqa: PLC0415

        wider, days = _widest_price_dataset(conn)
        moves = allocator.plan(conn, wider_dataset=wider, wider_days=days)
    except Exception as exc:  # noqa: BLE001 - 못 세면 안 적는다
        return f"  (배분자를 못 돌렸다: {type(exc).__name__})", 0
    live = [m for m in moves if m.score > 0]
    if not live:
        return "", 0

    lines = ["[다음 수 - **목표까지의 거리 순**. 도착 순서가 아니다]"]
    for i, m in enumerate(live[:5], 1):
        what = m.dataset or ", ".join(f"{k}={v}" for k, v in m.config_delta.items())
        mark = " ⚠자멸" if m.self_defeating else ""
        lines.append(f"  {i}. [{m.score:.2f}]{mark} {m.family_id[:18]}  "
                     f"{m.kind}: {what}")
        lines.append(f"       {m.why[:150]}")
    lines += [
        "",
        "  ▶ **위에서 하나를 골라 그 계열로 실험을 걸어라.** 새 엣지를 찾기 전에",
        "    이미 찾은 것을 목표까지 밀어붙이는 쪽이 훨씬 싸다.",
        "  ▶ `⚠자멸` 은 두면 DSR 이 관문 아래로 떨어지는 수다. 같은 표본으로",
        "    한 번 더 돌리면 다중검정 기준선이 올라 그 계열을 스스로 죽인다.",
        "    **표본을 넓히는 수는 예외다** - 기준선 상승보다 z 가 더 오른다.",
        "  ▶ 점수는 근거이지 명령이 아니다. 다르게 판단하면 그 이유를 적어라.",
    ]
    return "\n".join(lines), len(live)


def quant_brief() -> tuple[str, int]:
    """퀀트본부가 볼 사실: 실험을 기다리는 가설과 계열별 압력.

    반환: (브리핑, 대기 가설 수). 대기가 0이면 카드를 만들지 않는다 -
    할 일이 없는데 부르면 에이전트가 없는 일을 지어낸다.
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_PENDING, (10,))
            pending = cur.fetchall()
            cur.execute(_SQL_FAMILY_PRESSURE)
            pressure = cur.fetchall()
        # **연결이 살아 있는 동안** 배분자를 부른다 - 닫고 부르면 조용히
        # 빈 값이 된다(오늘 브리핑 블록 네 개가 그렇게 죽어 있었다).
        moves_block, n_moves = _allocator_block(conn)
        unrun_block = _unrunnable_falsifications(conn)
    finally:
        conn.close()

    # ▶ **대기 가설이 0이어도 둘 수 있는 수가 있으면 카드를 만든다** (2026-08-12)
    #   예전엔 `if not pending: return "", 0` 이었다. 그런데 배분자가 보는 것은
    #   **이미 완주한 계열**이다 - 대기 가설이 없을 때가 오히려 "다음 수를 둘
    #   때" 다. 그대로 뒀으면 최고의 수를 둬야 하는 순간에 공장이 쉬었다.
    if not pending and not n_moves:
        return "", 0

    out = ["[원장에서 읽은 사실 - 판단은 네가 한다]", ""]
    if moves_block:
        out.append(moves_block)
        out.append("")
    out.append(f"실험을 기다리는 가설 {len(pending)}건:")
    for hid, title, status, edge, created, proposal in pending:
        edge_type = (edge or {}).get("type") if isinstance(edge, dict) else None
        out.append(f"  - {hid[:8]} [{status}] {str(title)[:60]}")
        out.append(f"      edge={edge_type} 제안={proposal} 등록={created:%Y-%m-%d}")

    if pressure:
        out.append("")
        out.append("계열별 누적 시도(다중검정 분모 - 12번째 시도의 Sharpe 는 "
                   "1번째와 다르다):")
        for fam, n, maxn in pressure:
            out.append(f"  - [{fam[:12]}] 실험 {n}회 (최근 시도번호 {maxn})")

    # ▶ **큐 적체를 보여 준다** (2026-08-12, 재일 지시)
    #   병렬도를 상수로 박지 않기로 했다. 대신 **판단 재료**를 준다 -
    #   지금 몇 건이 밀렸고 한 건에 얼마나 걸리는지. 병목이라고 보면
    #   에이전트가 `QUANT_EXPERIMENT_BATCH` 를 올려 병렬로 돌린다.
    try:
        with conn.cursor() as _c:
            _c.execute("""select
                  count(*) filter (where status='QUEUED'),
                  count(*) filter (where status='LEASED'),
                  (select round(avg(extract(epoch from (b.created_at - e.created_at))))
                     from quant.experiments e
                     join quant.backtest_runs b on b.experiment_id=e.experiment_id
                    where e.created_at > now() - interval '1 day')
                from quant.experiment_jobs""")
            q, l, avg = _c.fetchone()
        if (q or 0) + (l or 0) > 0:
            eta = int((q or 0) * (avg or 0) / 60) if avg else None
            out.append("")
            out.append(f"큐: 대기 {q}건 / 실행 {l}건"
                       + (f" · 최근 한 건 평균 {int(avg)}초" if avg else "")
                       + (f" · 직렬이면 약 {eta}분" if eta else ""))
    except Exception:  # noqa: BLE001 - 못 세면 적지 않는다(지어내지 않는다)
        pass

    if unrun_block:
        out.append(unrun_block)

    out.append("")
    out.append("실험은 experiment_orchestrator 가 돌린다 - 사전등록 지문이 "
               "박히고 창별 강건성·과적합 통계가 함께 나온다. 결과 수치를 보고 "
               "설정을 바꾸면 사전등록이 무의미해진다.")
    out.append(_EXEC_SURFACE)
    # 둘 수 있는 수도 할 일이다 - 대기 가설이 0이어도 카드를 만든다
    return "\n".join(out), len(pending) + n_moves


# ▶ **실행면을 카드에 싣는다** (2026-08-12 실측)
#   퀀트 카드 3장이 각각 11분씩 조사만 하고 전부 `NOT_RUNNABLE` 로 끝났다.
#   사유는 "백테스트 실행면·결과 원장이 노출되지 않았다" 였는데, 실행면은
#   그 자리에 있었다 - 에이전트가 자기 workspace 만 뒤졌다. 프로필 페르소나에
#   같은 안내가 있었지만 4,400자 안에 묻혔고, 카드 본문이 훨씬 구체적이라
#   그쪽을 따랐다. **읽히는 자리에 적어야 읽는다.**
#
#   시스템 python3 에는 pandas·psycopg2 가 없다. 에이전트가 그걸로 돌렸다가
#   `ModuleNotFoundError: httpx` 를 받고 "환경이 없다"고 보고한 실측이 있다.
_EXEC_SURFACE = """
[실행면 - 있는 것을 없다고 적지 않는다]
  저장소  /app/departments/04-quant-backtest
  파이썬  quant-py   (pandas·numpy·psycopg2 포함. system python3 에는 없다)

  cd /app/departments/04-quant-backtest
  quant-py pipeline/experiment_orchestrator.py --run --hypothesis <id>
  quant-py pipeline/pit_dataset.py --build --name <n> --version <v> --from <d> --to <d>
  quant-py pipeline/<모듈>.py            # 인자 없이 = 자체점검

  "실행면이 없다"고 쓰기 전에 위 경로를 실제로 열어 본다. 진짜로 없으면
  무엇이 없는지 정확히 적는다 - 그 구분이 이 카드의 값이다."""


# ── 카드 생성 ────────────────────────────────────────────────────────────────

def _create_card(*, title: str, body: str, assignee: str, key: str,
                 dry_run: bool) -> str | None:
    """칸반 카드 하나. **같은 주기에 두 번 돌아도 카드는 하나다**(idempotency-key).

    중복 카드는 같은 실험을 두 번 사는 것과 같다 - 공장의 존재 이유에 반한다.
    """
    argv = ["docker", "exec", "-u", "hermes", "-i", KANBAN_CLI_CONTAINER,
            "hermes", "kanban", "create", title,
            "--assignee", assignee,
            "--idempotency-key", key,
            "--created-by", MODULE_VERSION,
            "--body", body]
    if dry_run:
        print(f"  [dry-run] {assignee} <- {title}")
        print(f"            key={key} 본문 {len(body)}자")
        return None
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       timeout=120)
    if r.returncode != 0:
        # 카드가 안 만들어졌으면 **조용히 넘어가지 않는다** - 부서가 일하지
        # 않았다는 사실이 보여야 한다
        print(f"  !! 카드 생성 실패({assignee}): "
              f"{(r.stderr or r.stdout).strip()[:240]}", flush=True)
        return None
    out = (r.stdout or "").strip()
    print(f"  {assignee} <- {title}")
    print(f"      {out.splitlines()[0][:120] if out else '(응답 없음)'}")
    return out


def _board_rows(sql: str, params: tuple = ()) -> list[tuple]:
    """칸반 보드를 **컨테이너를 통해** 읽는다.

    호스트에서 직접 열면 WAL/-shm 매핑이 생겨 컨테이너 쪽 쓰기가 전부
    `disk I/O error` 로 죽는다(실측). 읽기 하나 때문에 부서가 멈춘다.
    """
    code = (
        "import sqlite3,json,sys\n"
        "c=sqlite3.connect('file:/opt/kanban/kanban.db?mode=ro',uri=True)\n"
        "try: print(json.dumps(c.execute(sys.argv[1], json.loads(sys.argv[2])).fetchall()))\n"
        "finally: c.close()\n")
    import json as _json
    r = subprocess.run(
        ["docker", "exec", "-u", "hermes", "-i", KANBAN_CLI_CONTAINER,
         "python3", "-c", code, sql, _json.dumps(list(params))],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"보드 조회 실패: {(r.stderr or r.stdout).strip()[:200]}")
    return [tuple(x) for x in _json.loads(r.stdout or "[]")]


def _agent_output(task_id: str) -> str:
    """그 카드에서 에이전트가 낸 것. 첨부 + 요약을 모두 본다.

    ▶ `result` 만 보면 안 된다 (2026-08-11 실측). 완료 카드 21장 전부
      `result_len=0` 이었고 산출물은 **첨부파일**로만 남아 있었다. 그래서
      "에이전트가 아무것도 안 했다"고 잘못 읽었다.
    """
    parts: list[str] = []
    # ▶ **작업공간도 본다** (2026-08-11 실측). 에이전트는 산출을 첨부가 아니라
    #   `workspaces/<task_id>/` 에 쓰기도 한다. 첨부만 읽었더니 수확이 118자짜리
    #   요약 한 줄만 집어 blocks 0개 -> `발행 0 반려 0` 이 나왔다. 어디에 쓰든
    #   에이전트가 낸 것은 낸 것이다.
    for folder in (ATTACH_ROOT / task_id, WORKSPACE_ROOT / task_id):
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if f.is_file() and f.suffix in {".md", ".txt", ".json"}:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
    rows = _board_rows("select coalesce(result,'') from tasks where id=?", (task_id,))
    if rows and rows[0][0]:
        parts.append(rows[0][0])
    # ▶ **task_runs.summary 가 네 번째 자리다** (2026-08-11 실측)
    #   T11~T14 네 주기 연속으로 "기획자 산출이 비었다"가 찍혔다. 카드는 done
    #   이고 에이전트는 14분씩 돌았는데 위 세 곳이 전부 비어 있었다. 실제로는
    #   완성된 기획안이 `task_runs.summary` 에 멀쩡히 들어 있었다 - 공장은
    #   일하고 있었고 **수확기가 엉뚱한 곳을 보고 있었다.**
    #
    #   카드가 재시도되면 run 이 여러 개 생긴다(74 crashed -> 75 completed).
    #   실패한 run 의 summary 는 없거나 쓸모없으므로 완료된 run 만 읽는다.
    #   여러 개면 전부 이어 붙인다 - 어느 시도의 산출인지는 접수 게이트가
    #   판정할 문제이지, 여기서 골라 버릴 일이 아니다.
    parts.extend(
        s for (s,) in _board_rows(
            "select coalesce(summary,'') from task_runs"
            " where task_id=? and status='done' order by rowid", (task_id,))
        if s.strip())
    return "\n\n".join(parts)


def _agent_claimed(task_id: str) -> dict:
    """에이전트가 **자기가 몇 건 냈다고 기록했나.** 못 읽으면 빈 dict.

    ▶ 왜 이게 필요한가 (2026-08-13 실측)
      `t_b2f39ed0` 이 30초 만에 done 으로 끝났고 수확기가 집은 것은 123자짜리
      요약 한 줄이었다. 로그는 "기획자 산출이 비었다 - 건너뛴다" 를 찍었다.
      **그런데 거짓이다.** `task_runs.metadata` 에 이렇게 남아 있었다:

        {"proposals": 1, "lead_ids": ["lead_2757a464…"],
         "dataset": "krx-basket-daily/v3", "edge_type": "illiquidity_premium"}

      에이전트는 기획안을 만들었고(그것도 어제 묶여 있던 MAX 효과 리드를
      집어 가장 가까운 어휘로 사상해서) **우리가 본문을 잃은 것**이다.

      본문 채널이 불안정하다 - 같은 자리(`task_runs.summary`)에 어떤 때는
      기획안 전문 2434자가 오고 어떤 때는 요약 123자가 온다. 그 불안정을
      당장 못 고치더라도, **"에이전트가 안 했다" 와 "우리가 잃었다" 는
      반드시 구분해야 한다.** 전자는 프롬프트 문제이고 후자는 배관 문제인데,
      섞이면 엉뚱한 데를 고친다(오늘 하루 그 유형만 여섯 번 나왔다).
    """
    try:
        rows = _board_rows(
            "select coalesce(metadata,'') from task_runs"
            " where task_id=? and status='done' order by rowid desc limit 1",
            (task_id,))
    except Exception:  # noqa: BLE001
        return {}
    if not rows or not rows[0][0]:
        return {}
    try:
        import json as _j                          # noqa: PLC0415

        v = _j.loads(rows[0][0])
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


DELIVERY_RULES = (
    "[납품 규약 - 2026-08-13 개정]\n"
    "- **납품은 `factory_submit_proposal` 도구 호출이다.** 호출이 성공해 원장에 "
    "꽂힌 것만 납품으로 센다. 카드 요약·본문 텍스트는 납품이 아니다 - 같은 "
    "자리에 전문 2,434자와 요약 123자가 번갈아 와서 본문을 잃은 실측이 있다.\n"
    "- 호출할 때 `planner_run` 에 **이 카드의 ID**(t_ 로 시작)를 그대로 넣어라. "
    "그래야 납품이 이 카드 몫으로 대조된다.\n"
    "- 반려를 받으면 사유가 도구 결과로 즉시 돌아온다 - 그 사유에 답해 **같은 "
    "카드 안에서** 다시 제출하라.\n"
    "- 제출하지 못했으면 왜인지를 metadata 에 구조화해 남겨라 - **기권도 1급 "
    "산출물이다.** 조용한 미제출이 제일 나쁘다.\n")


def _mcp_deliveries(planner_id: str, conn=None) -> int:
    """이 카드 몫으로 **원장에 실제 꽂힌** 기획안 수. 도구 호출 = 납품의 계측면.

    ▶ 왜 (2026-08-13, 문헌 셋이 독립적으로 같은 처방)
      12-Factor Agents F4·F5(도구 호출이 곧 납품, 실행 상태 = 업무 상태),
      Anthropic 멀티에이전트(산출은 저장소에 영속, 채널엔 참조만),
      SWE-agent ACI(제출 시점 검증 + 같은 턴 피드백).
      우리 실측: 텍스트 채널은 전문 2,434자/요약 123자가 번갈아 와 본문을
      잃었고, 에이전트는 이미 MCP 로 12회 제출하고 있었다. **납품 판정을
      텍스트에서 원장으로 옮기면 그 채널의 불안정이 무의미해진다.**

    대조 두 겹: ① `case_id = 'card-<카드ID>'` 정확 대조(MCP 가 찍는다)
              ② 카드 실행 시간창 안에 생긴 기획안(에이전트가 planner_run 을
                 다르게 넣은 경우의 안전망)

    못 재면 0 을 돌려준다. 미측정!=0 원칙의 예외가 아니다 - 이 함수의 실패는
    **멱등한 텍스트 폴백으로 떨어질 뿐**이고(proposal_id 가 리드·엣지·유니버스
    해시라 이중 발행이 없다), 실패 자체는 로그에 남는다.
    """
    own = conn is None
    try:
        if own:
            conn = _conn()
        with conn.cursor() as cur:
            cur.execute("select count(*) from research.experiment_proposals"
                        " where case_id = %s", (f"card-{planner_id}",))
            exact = int(cur.fetchone()[0] or 0)
        if exact:
            return exact
        rows = _board_rows(
            "select min(started_at), max(ended_at) from task_runs"
            " where task_id=? and status='done'", (planner_id,))
        if not rows or not rows[0] or not rows[0][0]:
            return 0
        t0 = int(rows[0][0])
        t1 = int(rows[0][1] or rows[0][0])
        with conn.cursor() as cur:
            cur.execute(
                "select count(*) from research.experiment_proposals"
                " where created_at between to_timestamp(%s) and to_timestamp(%s)",
                (t0 - 60, t1 + 180))
            return int(cur.fetchone()[0] or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"  (납품 대조 실패 - 텍스트 폴백으로 진행: "
              f"{type(exc).__name__}: {str(exc)[:90]})", flush=True)
        return 0
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def harvest(*, dry_run: bool = False) -> int:
    """완료된 기획자 카드의 납품을 원장으로 들인다. **여기서 루프가 닫힌다.**

    ▶ 납품의 정본은 도구 호출이다 (2026-08-13 개정)
      에이전트가 MCP `factory_submit_proposal` 로 직접 제출한 것이 원장에
      있으면 그것이 납품이고 텍스트는 안 읽는다. 텍스트 파싱 경로는 **폐지
      수순의 폴백**으로만 남긴다 - 도구 호출 납품이 0건이고 본문이 파싱될
      때만 돌며, 그 발행에는 레거시 표식이 붙는다.

    ▶ 왜 접수는 여전히 결정론 코드인가
      제출은 `proposal_intake` 가 리드를 원장에서 다시 읽어 대조하고 발행
      게이트를 돌리는 절차다. MCP 경로도 같은 `intake()` 를 거친다 - 판단은
      에이전트가, 접수는 코드가 한다.

    반환: 새로 확인·발행된 기획안 수.
    """
    import proposal_intake as PI                   # noqa: PLC0415

    pairs = _board_rows(
        "select id, title from tasks "
        " where status='done' and created_by like 'factory-autopilot%'"
        "   and title like '%[기획자]%' order by created_at desc limit 6")
    if not pairs:
        return 0

    # ▶ **한 번 처리한 카드는 다시 안 태운다** (2026-08-11 실측). 반려된 카드를
    #   매 주기 재수확하며 같은 사유를 반복 출력했다 - 에이전트 산출은 이미
    #   확정이라 결과가 바뀔 수 없는데 계속 돌았고, 로그가 지저분해져 **새 반려를
    #   못 알아보게** 됐다. 처리 기록은 파일 하나로 남긴다(보드에 쓰면 컨테이너
    #   쓰기와 경합한다).
    seen_path = Path.home() / ".factory_autopilot_harvested"
    try:
        seen = set(seen_path.read_text(encoding="utf-8").split())
    except OSError:
        seen = set()
    pairs = [(i, t) for i, t in pairs if i not in seen]
    if not pairs:
        return 0

    published = 0
    for planner_id, title in pairs:
        stamp = title.rsplit(" ", 1)[-1]

        # ▶ **정본 경로 먼저**: 도구 호출로 이미 원장에 꽂혔으면 그게 납품이다.
        #   텍스트가 비었든 요약뿐이든 상관없다 - 텍스트를 읽기 전에 본다
        #   (실측: 산출 123자였던 카드도 에이전트는 MCP 로 제출을 시도했었다).
        got = _mcp_deliveries(planner_id)
        if got:
            published += got
            print(f"  수확 {stamp}: **도구 호출 납품 {got}건 확인** - "
                  f"텍스트 수확 생략", flush=True)
            if not dry_run:
                with seen_path.open("a", encoding="utf-8") as fh:
                    fh.write(planner_id + "\n")
            continue

        planner_text = _agent_output(planner_id)
        if not planner_text:
            print(f"  수확: {stamp} 도구 호출 납품 0건 · 텍스트 산출도 비었다 - "
                  f"미납. 카드가 done 이어도 납품이 아니다", flush=True)
            continue

        # ▶ **회의론자 카드를 없앴다** (2026-08-11, 재일 결정).
        #   별도 실행으로 두면 주기가 두 배가 되고, 그 카드 하나가 막히면 공장이
        #   통째로 선다(실제로 버려진 카드 하나에 상태기계가 조용히 정지했다).
        #   경쟁 설명은 기획자가 COMPETING_EXPLANATION 에 직접 쓴다 - 사전등록
        #   시점에 반대 가설을 적어두는 것이 이 필드의 목적이고 그건 지켜진다.
        #   `proposal_intake` 가 그 서명에 `#self` 를 박으므로 원장에서 독립 검토와
        #   구분된다 - 승격 판정 때 같은 무게로 읽으면 안 된다.
        skeptic_text = ""
        if not planner_text:
            print(f"  수확: {stamp} 산출물이 비었다 - 건너뛴다", flush=True)
            continue

        # ▶ **"안 했다" 와 "잃었다" 를 구분한다** (2026-08-13 실측)
        #   에이전트가 `task_runs.metadata` 에 몇 건 냈는지 남긴다. 그 수와
        #   우리가 파싱한 블록 수가 다르면 그건 에이전트 잘못이 아니라 배관
        #   문제다. 로그가 "산출이 비었다" 로만 찍히면 프롬프트를 고치러 가는데,
        #   실제로는 본문 채널이 새고 있었다.
        _claimed = _agent_claimed(planner_id)
        _n_claimed = int(_claimed.get("proposals") or 0)
        _n_parsed = len(PI.parse_blocks(planner_text, PI.PLANNER_KEYS))
        if _n_claimed and _n_parsed < _n_claimed:
            _meta = {k: v for k, v in _claimed.items()
                     if k != "worker_session_id"}
            print(f"  !! 수확 유실 {stamp}: 에이전트는 {_n_claimed}건을 냈다고 "
                  f"기록했는데 도구 호출 납품 0건, 본문 파싱 {_n_parsed}건이다. "
                  f"**제출 호출이 원장에 닿지 않았다** - MCP 게이트 반려인지 "
                  f"연결 문제인지 gate 응답을 확인하라. "
                  f"metadata={_meta} · 집은 산출 {len(planner_text)}자",
                  flush=True)

        conn = _conn()
        try:
            wanted = {i for b in PI.parse_blocks(planner_text, PI.PLANNER_KEYS)
                      for i in PI._split(b.get("LEAD_IDS", ""))}
            leads = PI.load_leads(conn, sorted(wanted))
            unknown = sorted(wanted - set(leads))
            r = PI.intake(planner_text, skeptic_text,
                          case_id=f"auto-{stamp}",
                          # 회의론자가 없으므로 intake 가 자기서명(`#self`)으로
                          # 채운다. 여기서 가짜 서명을 만들어 넣지 않는다 -
                          # 그러면 원장에서 독립 검토와 구분이 안 된다.
                          planner_run=planner_id, skeptic_run=planner_id,
                          leads=leads,
                          # ▶ **계열 이력을 넘긴다** (2026-08-13). 이 인자가
                          #   빠져 있어서 모든 기획안이 "이 계열은 처음" 으로
                          #   접수됐고(`trials_used=0 · past_outcomes=[]`),
                          #   승격 관문만 진짜 이력을 봐서 "2회 기각됐는데
                          #   대응이 없다" 로 영구 반려했다. 기획자는 접수에서
                          #   들은 대로 했는데 승격에서 그 이유로 죽었다 -
                          #   `발주 0건` 의 실제 지점이었다.
                          outcomes_for=lambda b: PI.load_past_outcomes(
                              conn,
                              (b.get("EDGE_TYPE") or "").strip().lower(),
                              (b.get("UNIVERSE_KEY") or "").strip().lower(),
                              label=(b.get("LABEL") or "").strip(),
                              baseline=(b.get("BASELINE") or "").strip()))
            pub = r.publishable
            if dry_run:
                print(f"  [dry-run] {stamp}: 발행가능 {len(pub)}건 "
                      f"반려 {len(r.rejected)}건 미상리드 {unknown}")
                continue
            new, dup = (PI.persist(conn, pub) if pub else (0, 0))
            published += new
            print(f"  수확 {stamp}: 발행 {new} 중복 {dup} 반려 {len(r.rejected)}"
                  + (" (레거시 텍스트 경로 - 정본은 도구 호출 납품)"
                     if new else ""),
                  flush=True)
            for x in r.rejected:
                # 왜 안 들어갔는지 **반드시 남긴다** - 조용히 0건이면 다음 주기가
                # 같은 실수를 반복한다(실제로 published=0 을 며칠 몰랐다)
                print(f"      반려: {x.title[:40]} <- {x.reason[:90]}", flush=True)
            # ▶ 반려를 **다음 브리핑으로 되돌린다** (2026-08-11)
            #   사람에게 보이는 것만으로는 루프가 안 닫힌다. 8/11 실측:
            #   기획자가 어휘에 없는 COMPETING_CODES(REGIME_ARTIFACT)를 지어내
            #   반려됐는데, 다음 주기 브리핑은 그 사실을 안 실어서 **같은 코드로
            #   또 냈다.** 통제 어휘를 나열하는 것과 "네가 지난번 그것을 어겼다"고
            #   말해주는 것은 다른 신호다.
            record_rejections(r.rejected, stamp=stamp)
            if unknown:
                print(f"      원장에 없는 리드: {unknown}", flush=True)
            # 발행이든 반려든 **판정이 났으면** 처리 완료다. 반려는 다시 태워도
            # 같은 결과이므로, 고치려면 다음 주기가 새 카드를 낸다.
            with seen_path.open("a", encoding="utf-8") as fh:
                fh.write(planner_id + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 수확 실패({stamp}): {type(exc).__name__}: "
                  f"{str(exc)[:180]}", flush=True)
        finally:
            conn.close()
    return published


def _promote(*, dry_run: bool = False) -> int:
    """접수된 기획안을 Gate 0 에 태워 가설로 올린다. 반환: 실패 수(0=정상).

    퀀트 모듈(factory_bridge)이 게이트의 주인이다 - 여기서는 부르기만 한다.
    판정 규칙을 이쪽에 복사하면 언젠가 두 벌이 갈린다.
    """
    import factory_bridge as FB                    # noqa: PLC0415 - 경로 주입 뒤

    conn = _conn()
    try:
        got = FB.promote_published(conn, limit=20,
                                   created_by="factory-autopilot",
                                   dry_run=dry_run)
    finally:
        conn.close()      # WAL 을 남기지 않는다
    if not got:
        return 0
    ok = [p for p in got if p.accepted]
    tag = "[dry-run] " if dry_run else ""
    print(f"  {tag}승격: 가설 {len(ok)}건 / 게이트 거부 {len(got) - len(ok)}건",
          flush=True)
    for p in got:
        if p.accepted:
            print(f"      {p.proposal_id} -> {p.hypothesis_id or '(dry-run)'} "
                  f"계열 {p.gate.get('trial_family_id')} "
                  f"시도 {p.gate.get('trial_number')}", flush=True)
        else:
            # **거부도 결과다.** 조용히 사라지면 리서치가 대응할 수 없다.
            print(f"      게이트 거부: {p.proposal_id} <- {p.why[:110]}", flush=True)
            record_rejections(
                [SimpleNamespace(title=p.proposal_id, reason=p.why)],
                stamp="gate0")
    return 0


_SQL_NEEDS_EXPERIMENT = """
select h.hypothesis_id::text, left(h.title, 60)
  from quant.hypotheses h
 where h.status = 'PROPOSED'
   -- 이미 대기·실행 중인 주문이 있으면 다시 넣지 않는다. enqueue 도 막지만,
   -- 여기서 거르면 매 주기 "거부됨" 이 로그를 채우지 않는다.
   and not exists (select 1 from quant.experiment_jobs j
                    where j.hypothesis_id = h.hypothesis_id
                      and j.status in ('QUEUED','LEASED'))
 order by h.created_at
 limit %s
"""


def _structurally_blocked(conn, hypothesis_ids: list) -> dict:
    """발주해도 지금 조건에서는 못 도는 가설. 반환: {id: 사유}.

    **판정을 여기서 새로 만들지 않는다** - 실험 워커가 쓰는 것과 같은 게이트를
    부른다. 손으로 규칙을 적으면 실행면과 갈리고, 그건 오늘 하루 종일 고친 사고다.

    못 세면 빈 dict 를 돌려 **전부 발주한다** - 판정을 못 했다고 막으면 멀쩡한
    가설이 조용히 멈춘다(가설이 RUNNING 에 갇히던 것과 같은 유형).
    """
    if not hypothesis_ids:
        return {}
    try:
        sys.path.insert(0, str(_ROOT / "departments" / "04-quant-backtest" / "pipeline"))
        pass  # feasibility 는 아래에서 모양별로 직접 대조한다
        from data_resolution import manifest_index          # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}

    out: dict = {}
    try:
        datasets = {f"{n}/{v}" for n, v, _ in manifest_index(conn)}
        covered: set = set()
        for _n, _v, srcs in manifest_index(conn):
            covered |= set(srcs or {})
        # ▶ 유도본으로 대체 가능한 원시도 **덮인 것으로 본다** (2026-08-12)
        #   `resolve` 는 `market_quotes` 요구를 `microstructure_features` 로
        #   잇는다(DERIVED_FROM). 여기서 그걸 모르면 발주 게이트가 실제로는
        #   돌 수 있는 가설을 보류한다 - 같은 표를 봐야 판정이 일치한다.
        try:
            from data_resolution import DERIVED_FROM   # noqa: PLC0415
            for derived, raws in DERIVED_FROM.items():
                if derived in covered:
                    covered |= set(raws)
        except Exception:  # noqa: BLE001 - 못 읽으면 좁게 본다(막는 쪽이 안전)
            pass
        with conn.cursor() as cur:
            cur.execute(
                "select hypothesis_id::text, expected_edge, required_data_products "
                "from quant.hypotheses where hypothesis_id = any(%s::uuid[])",
                (list(hypothesis_ids),))
            rows = cur.fetchall()
        from experiment_orchestrator import STRATEGY_CATALOG  # noqa: PLC0415

        for hid, edge, req in rows:
            # ▶ `required_data_products` 는 **두 모양**이다 - 옛 가설은
            #   `["krx-basket-daily/v1"]`(데이터셋 이름 목록), 새 가설은
            #   `{"tables": [...], "min_history_days": N}`(원천 요구). 한 모양만
            #   가정하면 dict 의 **키 이름을 데이터셋 이름으로 읽는다** - 실제로
            #   `Dataset 'min_history_days' 구축` 이라는 엉뚱한 백로그가 나왔다.
            if isinstance(req, dict):
                missing = sorted(set(req.get("tables") or []) - covered)
                if missing:
                    out[hid] = (f"매니페스트가 안 덮는 원천: {', '.join(missing)} "
                                f"(덮는 것: {', '.join(sorted(covered))})")
                    continue
            elif isinstance(req, list) and req:
                missing_ds = sorted(set(map(str, req)) - datasets)
                if missing_ds:
                    out[hid] = f"없는 데이터셋: {', '.join(missing_ds)}"
                    continue

            # 데이터가 되면 남는 것은 전략 구현·어휘다. 이건 목록 대조라
            # `feasibility` 를 부르지 않고 같은 표를 직접 본다(모양 가정 없음).
            etype = str((edge or {}).get("type") or "").strip().lower()
            if not etype:
                out[hid] = "expected_edge.type 이 비었다"
                continue
            if etype not in STRATEGY_CATALOG:
                out[hid] = (f"'{etype}' 는 어휘에 없다 - "
                            f"사용 가능: {', '.join(sorted(STRATEGY_CATALOG))}")
                continue
            # ▶ 실행면이 안 읽는 파라미터도 **영원히 실패하는 부류**다 (2026-08-12)
            #   `config_binding.bind` 가 EDGE_KEYS 밖의 키를 거부한다. 오늘
            #   `expected_edge` 오염은 접수 단계에서 막았지만(관문 우회 차단),
            #   **그 전에 등록된 가설**은 이미 오염된 채로 남아 매 주기 같은
            #   자리에서 죽는다. 데이터·어휘만 보고 발주하면 이걸 못 거른다.
            try:
                from config_binding import EDGE_KEYS   # noqa: PLC0415
                bad = sorted(k for k in (edge or {}) if k not in EDGE_KEYS)
            except Exception:  # noqa: BLE001
                bad = []
            if bad:
                out[hid] = (f"실행면이 안 읽는 파라미터: {', '.join(bad)} - "
                            f"가설을 다시 등록해야 한다(재시도로는 안 풀린다)")
    except Exception:  # noqa: BLE001 - 못 세면 막지 않는다
        return {}
    return out


_SQL_RECENT_EXP_FAILURES = """
select substr(h.title, 1, 60), replace(j.failure_reason, chr(10), ' ')
  from quant.experiment_jobs j
  join quant.hypotheses h using (hypothesis_id)
 where j.status = 'FAILED'
   and coalesce(j.failure_reason, '') <> ''
   -- 종결 시각 컬럼은 `updated_at` 이다(finished_at 은 없다). 상태가 FAILED 로
   -- 바뀔 때 같이 갱신되므로 "언제 죽었나" 로 쓰기에 맞다.
   and j.updated_at >= now() - interval '24 hours'
 order by j.updated_at desc
 limit %s
"""

_SEEN_FAILURES = Path.home() / ".factory_autopilot_exp_failures"


def _feed_back_experiment_failures(conn, *, limit: int = 6) -> int:
    """실험 실패를 **다음 브리핑으로 되돌린다.** 반환: 새로 기록한 수.

    ▶ 왜 필요한가 (2026-08-12 실측)
      첫 실험 3건이 전부 실패했는데 사유가 하나같이 기획 단계에서 피할 수 있는
      것이었다: 구현 안 된 전략(`low_volatility`), 실행면이 안 읽는 파라미터
      (`signal_window_days`), 사상 안 되는 데이터셋. 그런데 그 사유는 퀀트 쪽
      원장에만 남고 리서치 브리핑에는 실리지 않았다 - **같은 기획안을 다시
      낼 수밖에 없는 구조였다.**

      브리핑에 통제 어휘를 싣는 것과 "네가 낸 것이 실험에서 이렇게 죽었다"고
      말해주는 것은 다른 신호다. 앞의 것은 규칙이고 뒤의 것은 결과다.
    """
    try:
        seen = set(_SEEN_FAILURES.read_text(encoding="utf-8").splitlines())
    except OSError:
        seen = set()
    with conn.cursor() as cur:
        cur.execute(_SQL_RECENT_EXP_FAILURES, (limit,))
        rows = cur.fetchall()
    fresh = [(t, r) for t, r in rows if f"{t}\t{r}"[:200] not in seen]
    if not fresh:
        return 0
    record_rejections(
        [SimpleNamespace(title=f"[실험 실패] {t}", reason=r) for t, r in fresh],
        stamp="experiment")
    try:
        with _SEEN_FAILURES.open("a", encoding="utf-8") as fh:
            for t, r in fresh:
                fh.write(f"{t}\t{r}"[:200] + "\n")
    except OSError as e:
        # 기록 실패가 발주를 죽이면 안 된다 - 다음 주기에 한 번 더 실릴 뿐이다
        print(f"      ⚠ 실험 실패 표식 기록 실패: {e}", flush=True)
    print(f"      실험 실패 {len(fresh)}건을 다음 브리핑으로 되돌린다", flush=True)
    return len(fresh)


_DATASET_MARK = Path.home() / ".factory_autopilot_dataset_day"
_MS_DATASET = "krx-microstructure-daily/v1"


def _refresh_datasets(*, dry_run: bool = False) -> int:
    """하루 한 번: 새 날을 피처로 접고 **데이터셋 파티션을 다시 굳힌다.**

    ▶ 왜 자동이어야 하나 (2026-08-12)
      마이크로구조 데이터셋은 손으로 한 번 만들었다. 그대로 두면 **오늘부터
      바로 낡는다** - 수집기는 매일 쌓는데 백테스트가 보는 스냅샷은 어제 것이다.
      그리고 낡았다는 사실이 아무 데도 안 드러난다(매니페스트는 여전히
      "있다" 고 말한다).

    ▶ 왜 주기마다가 아니라 하루 한 번인가
      자동 조종은 15분마다 돈다. 파티션을 그때마다 다시 굳히면 하루 96번
      같은 파일을 쓴다. 일별 피처는 하루 한 번이면 충분하고, 그 이상은
      디스크와 해시 재계산만 태운다.

    ▶ 실패해도 공장은 돈다
      데이터셋 갱신이 막혀도 가설·실험 주기는 이어져야 한다. 대신 조용히
      넘기지 않는다 - 사유를 찍고 다음 날 다시 시도한다(표식을 안 남긴다).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        if _DATASET_MARK.read_text(encoding="utf-8").strip() == today:
            return 0
    except OSError:
        pass

    quant = _ROOT / "departments" / "04-quant-backtest"
    steps = (
        ([sys.executable, "pipeline/microstructure_builder.py", "--build"]
         + (["--dry-run"] if dry_run else []), "피처 접기"),
        ([sys.executable, "pipeline/spec_dataset_builder.py", "--build", _MS_DATASET]
         + (["--dry-run"] if dry_run else []), "파티션 굳히기"),
    )
    for argv, label in steps:
        r = subprocess.run(argv, cwd=str(quant), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=3600)
        tail = "\n".join((r.stdout or "").strip().splitlines()[-3:])
        if r.returncode != 0:
            print(f"  !! 데이터셋 {label} 실패(exit={r.returncode}):\n{tail}\n"
                  f"     {(r.stderr or '').strip()[-200:]}", flush=True)
            return 1
        print(f"  데이터셋 {label}:\n{tail}", flush=True)

    if not dry_run:
        try:
            _DATASET_MARK.write_text(today, encoding="utf-8")
        except OSError as e:
            # 표식을 못 남기면 다음 주기에 한 번 더 돈다 - 멱등이라 안전하다
            print(f"      ⚠ 데이터셋 표식 기록 실패: {e}", flush=True)
    return 0


def _enact_top_move(conn, *, dry_run: bool = False) -> int:
    """배분자가 고른 **1순위 수를 공장이 직접 둔다.** 반환: 등록한 변형 수.

    ▶ 여기가 수동과 능동을 가르는 자리다 (2026-08-12)
      지금까지는 배분자가 아무리 좋은 수를 찾아도 **에이전트가 카드를 읽고
      움직여야** 일이 났다. 그래서 실험 자체는 2~3초인데 5건 완주에 4시간이
      걸렸다 - 나머지는 전부 사람(에이전트) 대기였다.

      "같은 엣지를 더 넓은 표본으로 다시 잰다" 는 **판단이 아니라 산수**다.
      엣지도 파라미터도 안 바꾼다. 그런 수는 기다릴 이유가 없다.

    ▶ **판단이 필요한 수는 여기서 안 한다**
      손잡이 값을 고르는 것(낙폭 정지를 -0.28 로 할 것인가)은 가설이다.
      그건 카드로 나가고 에이전트가 고른다. `register_variant` 가 그 경계를
      코드로 갖고 있고 자체 점검이 그것을 지킨다 - 공장이 사람 없이 가설을
      지어내기 시작하면 사전등록의 뜻이 사라진다.

    ▶ 한 주기에 하나만 둔다
      시도 1->2 에 DSR 기준선이 +0.520 뛴다. 좋아 보인다고 여러 개를 한꺼번에
      걸면 그 계열들을 동시에 깎는다.
    """
    try:
        import allocator  # noqa: PLC0415

        wider, days = _widest_price_dataset(conn)
        if not wider:
            return 0
        moves = allocator.plan(conn, wider_dataset=wider, wider_days=days)
    except Exception as exc:  # noqa: BLE001 - 못 세면 아무것도 안 둔다
        print(f"  배분자 실행 실패 - 자동 등록 생략: {type(exc).__name__}",
              flush=True)
        return 0

    for mv in moves:
        if mv.score <= 0 or mv.self_defeating or mv.kind != "표본 확대":
            continue
        if dry_run:
            print(f"  [dry-run] 배분자 자동 등록: {mv.family_id[:12]} "
                  f"-> {mv.dataset} (점수 {mv.score:.2f})", flush=True)
            return 1
        hid, why = allocator.register_variant(conn, mv)
        if hid:
            print(f"  배분자 자동 등록: {hid[:8]} <- {why}", flush=True)
            return 1
        # 못 둔 이유를 남긴다 - 조용히 넘어가면 왜 안 도는지 아무도 모른다
        print(f"  배분자 보류: {mv.family_id[:12]} - {why}", flush=True)
    return 0


def _dispatch_experiments(*, dry_run: bool = False, limit: int = 10) -> int:
    """PROPOSED 가설을 실험 큐에 넣는다. 반환: 실패 수(0=정상).

    ▶ 왜 결정론이 발주하는가
      백테스트는 실행이지 판단이 아니다. 에이전트가 돌리면 같은 가설이 부를
      때마다 다른 창·다른 비용가정을 쓴다. 퀀트 에이전트가 할 일은 나온 결과를
      해석하는 것이고, 실제로 그 카드는 "읽기만 하고 아무것도 쓰지 않았다"고
      정직하게 보고했다 - 시킬 수단이 없는 일을 시킨 쪽이 문제였다.
    """
    import job_queue                              # noqa: PLC0415 - 경로 주입 뒤

    conn = _conn()
    try:
        _feed_back_experiment_failures(conn)
        _enact_top_move(conn, dry_run=dry_run)
        with conn.cursor() as cur:
            cur.execute(_SQL_NEEDS_EXPERIMENT, (limit,))
            rows = cur.fetchall()
        if not rows:
            return 0
        if dry_run:
            print(f"  [dry-run] 발주 대상 {len(rows)}건: "
                  f"{', '.join(t for _, t in rows)[:120]}", flush=True)
            return 0
        # ▶ **못 돌 것을 발주하지 않는다** (2026-08-12)
        #   큐가 구조적으로 막힌 가설로 가득 차 매 주기 같은 5건만 재시도하고
        #   있었다(데이터셋 없음 3건, 어휘 밖 1건). 실패 사유는 정확했지만
        #   **다음 주기에도 똑같이 실패**하므로 공장이 제자리를 돈다.
        #
        #   이력으로 영구 제외하지 않는다 - 오늘 카탈로그를 고치자 어제 못 돌던
        #   `low_volatility` 가 돌았다. **매번 다시 판정**해서 조건이 바뀌면
        #   저절로 재개되게 한다. 판정은 실행면 함수 그대로 쓴다.
        blocked = _structurally_blocked(conn, [h for h, _ in rows])
        if blocked:
            print(f"  발주 보류 {len(blocked)}건 (조건이 바뀌면 저절로 재개):",
                  flush=True)
            for hid, why in blocked.items():
                print(f"      - {hid[:8]}: {why[:90]}", flush=True)
        sent = 0
        for hid, title in rows:
            if hid in blocked:
                continue
            r = job_queue.enqueue(conn, hid, requested_by="factory-autopilot")
            if r.get("accepted"):
                sent += 1
                print(f"      발주 {hid[:8]} <- {title}", flush=True)
            else:
                # 조용히 넘기지 않는다 - 왜 안 들어갔는지 남아야 추적된다
                print(f"      발주 거부 {hid[:8]}: {r.get('reason')}", flush=True)
        print(f"  발주: 실험 주문 {sent}건", flush=True)
    finally:
        conn.close()
    return 0


def cycle(*, dry_run: bool = False) -> int:
    """공장 한 주기. 실패한 부서 수를 돌려준다(0이면 정상)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H")
    print(f"[{stamp}] 공장 자동 조종 - 브리핑을 만들어 부서에 건다", flush=True)
    fails = 0

    # ── 0. 수확 먼저. 지난 주기 산출을 원장에 들이지 않고 새 카드를 걸면
    #      에이전트는 계속 일하는데 원장은 영영 비어 있다(실제로 그랬다).
    try:
        n = harvest(dry_run=dry_run)
        if n:
            print(f"  수확: 기획안 {n}건 발행 - 퀀트가 다음 주기에 집는다", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 수확 실패: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        fails += 1

    # ── 0.5 승격. **수확과 실험 사이가 끊겨 있었다** (2026-08-12)
    #      gate0 을 부르는 곳이 E2E 하네스뿐이라, 접수된 기획안이 가설이 되는
    #      운영 경로가 없었다. 에이전트는 계속 기획안을 내는데 quant.hypotheses
    #      는 늘 비어 있었고 퀀트 브리핑은 언제나 "실험 대기 가설 0건"이었다.
    #      루프가 도는 것처럼 보이면서 실제로는 여기서 끊겨 있었다.
    #
    #      Gate 0 은 판단이 아니라 검사(어휘 대조·다중검정 예산·계열 압력)라
    #      결정론이 돌린다. 에이전트에게 맡기면 같은 기획안이 부를 때마다 다른
    #      판정을 받는다.
    try:
        fails += _promote(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 승격 실패: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        fails += 1

    # ── 0.6 발주. **여기도 끊겨 있었다** (2026-08-12)
    #      job_queue.enqueue 를 부르는 곳이 없어서, 가설이 PROPOSED 로 서고도
    #      실험 큐에는 아무것도 안 들어갔다. 워커(experiment_worker --serve)는
    #      멀쩡히 있는데 물 주문이 없었다.
    #
    #      **실험 실행은 에이전트 일이 아니다.** 백테스트는 결정론이고,
    #      에이전트가 돌리면 같은 가설이 부를 때마다 다른 수를 쓴다. 퀀트
    #      카드가 하는 일은 결과에 대한 판단이지 실행이 아니다 - 실제로
    #      에이전트는 "읽기만 하고 아무것도 쓰지 않았다"고 정직하게 보고했다.
    try:
        fails += _dispatch_experiments(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 발주 실패: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        fails += 1

    # ── 0.7 데이터셋 갱신(하루 한 번). 새 날을 피처로 접고 파티션을 다시 굳힌다.
    #      **발주 뒤에 둔다** - 실험이 먼저 나가야 하고, 데이터셋 갱신이 오래
    #      걸려도 그 주기의 가설·실험이 밀리지 않는다.
    try:
        fails += _refresh_datasets(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 데이터셋 갱신 실패: {type(exc).__name__}: {str(exc)[:180]}",
              flush=True)
        fails += 1

    # ── 리서치: 다음 기획안 ──
    try:
        rb = research_brief()
    except Exception as exc:  # noqa: BLE001
        # 사실을 못 읽었으면 **카드를 만들지 않는다** - 근거 없이 가설을 내라고
        # 시키면 나오는 것은 문장이지 가설이 아니다
        print(f"  !! 리서치 브리핑 실패 - 카드 생략: {type(exc).__name__}: "
              f"{str(exc)[:180]}", flush=True)
        rb = ""
        fails += 1
    if rb:
        # ▶ **기획자와 회의론자를 다른 카드로 낸다** (2026-08-11).
        #   `proposal_intake` 는 두 산출의 실행 id 가 같으면 거부한다 - 자기가
        #   낸 가설을 자기가 검토하면 서명이 무의미하기 때문이다. 한 카드에서
        #   둘 다 쓰게 하면 그 규칙을 형식만 만족시키게 된다.
        _create_card(
            title=f"공장 주기 [기획자]: 다음 실험 기획안 {stamp}",
            body=(rb + "\n\n---\n" + PLANNER_FORMAT + "\n\n"
                  + DELIVERY_RULES + "\n"
                  "[규칙]\n"
                  # ▶ **칸 이름을 바로잡았다** (2026-08-13 실측)
                  #   여기가 오래 `ECONOMIC_RATIONALE 에 적어라` 였는데 접수는
                  #   `LESSONS_ADDRESSED:` 를 읽는다. 기획자는 시킨 대로 했다 -
                  #   `prop_5682` 는 ECONOMIC_RATIONALE 에 교훈 5개를 이름으로
                  #   짚어 대응했고(변동성 목표·낙폭 정지·장기 표본까지) 관문은
                  #   **"그 교훈에 대응이 없다"** 며 막았다. 지시와 파서가 다른
                  #   칸을 가리키면 에이전트가 아무리 잘해도 통과할 수 없다.
                  "- 이미 기각된 계열을 다시 내려면 그 교훈에 어떻게 대응하는지 "
                  "**`LESSONS_ADDRESSED:` 칸에** 적어라. 형식은 "
                  "`교훈코드=이번엔 무엇을 다르게 하는가` 이고 여러 개면 "
                  "쉼표로 잇는다. 예: `LESSONS_ADDRESSED: BEAR_FRAGILE=고점 "
                  "대비 -25%% 손실 정지를 사전 고정, OVERFIT_DSR=사전 지정 "
                  "파라미터 한 조합만 사용`\n"
                  "  메커니즘 설명은 ECONOMIC_RATIONALE 에 그대로 쓰되, "
                  "**대응 자체는 반드시 위 칸에도 적어라** - 계약은 그 칸만 "
                  "읽으므로 본문에만 있으면 '대응 없음' 으로 막힌다.\n"
                  "- 예산이 소진된 계열은 제안하지 마라.\n"
                  "- 쓸 수 있는 리드 목록에 없는 id 를 대지 마라 - 원장에서 다시 "
                  "읽어 대조하므로 막힌다.\n"
                  "- 근거가 부족하면 **기획안을 내지 말고 무엇이 부족한지 적어라.** "
                  "지어낸 가설은 원장을 오염시킨다.\n"
                  # ▶ 옛 문구 "산출은 위 KEY:value 줄로만(카드에)" 는 납품
                  #   규약과 정면 모순이었다 - 엇갈린 신호가 8일을 끌었던 그
                  #   버그 클래스라, 서식의 **용도**를 명시하는 문장으로 바꿨다.
                  "- 위 `KEY: value` 서식은 **`factory_submit_proposal` 의 "
                  "planner_text 인자에 넣는 형식**이다. 카드 본문에 붙이는 게 "
                  "아니라 도구 호출에 실어라 - 카드 텍스트는 납품으로 세지 "
                  "않는다."),
            assignee=RESEARCH_ASSIGNEE,
            key=f"factory-planner-{stamp}", dry_run=dry_run)

        # 회의론자 카드는 **여기서 만들지 않는다**. 기획자 산출이 아직 없는데
        # 걸면 검토할 대상이 없는 채로 돌아 빈 산출을 내거나, 없는 기획안을
        # 검토한 척하게 된다. 기획자가 끝난 것을 보고 수확기가 건다.

    # ── 퀀트: 대기 중인 가설 실험 ──
    try:
        qb, n_pending = quant_brief()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 퀀트 브리핑 실패 - 카드 생략: {type(exc).__name__}: "
              f"{str(exc)[:180]}", flush=True)
        qb, n_pending = "", 0
        fails += 1
    if n_pending:
        _create_card(
            title=f"공장 주기: 다음 수·대기 가설 {n_pending}건 실험·판정",
            body=(qb + "\n\n---\n"
                  "요청: **[다음 수] 목록이 있으면 거기서 먼저 고른다.** 그것이 "
                  "목표까지의 거리를 가장 크게 줄이는 수다. 대기 가설은 그 다음이다.\n"
                  "- 다음 수를 안 고르고 대기 가설을 먼저 돌린다면 그 이유를 적어라. "
                  "점수는 근거이지 명령이 아니지만, 근거를 무시하려면 근거가 필요하다.\n"
                  "요청(계속): 위 가설 중 **우선순위가 높은 것부터** 실험을 돌리고 "
                  "결과를 판정하라.\n"
                  "- 판정은 사전등록한 반증 기준으로만 한다. 결과를 보고 기준을 "
                  "바꾸면 그건 실험이 아니다.\n"
                  "- 기각이면 교훈을 환류에 적재하라 - 다음 기획안이 그것을 읽는다.\n"
                  "- 미측정 지표를 0 으로 채우지 마라. 없는 것은 없는 것이다.\n"
                  "\n"
                  "**결과를 해석했으면 반드시 남겨라.** 너는 이 카드가 끝나면 "
                  "죽고, 다음 주기의 너는 아무것도 기억하지 못한다. 원장에 "
                  "적힌 것만 남는다:\n"
                  "```\n"
                  "quant-py pipeline/research_note.py --experiment <실험id> \\\n"
                  "         --note \"왜 이렇게 나왔다고 보는가 · 무엇이 놀라웠나 · "
                  "다음엔 무엇을 먼저 확인할 것인가\"\n"
                  "```\n"
                  "- 결정론이 적은 사실(관문 거리·교훈코드)은 자동으로 남는다. "
                  "**네가 적을 것은 해석이다** - 사실을 다시 쓰지 마라.\n"
                  "- `추가 분석 필요` 같은 문장은 거부된다. 다음 사람에게 "
                  "0비트다.\n"
                  "- 이 노트는 다음 주기 브리핑에 실려 **네가 읽는다.** "
                  "지금 안 적으면 다음 주기의 네가 같은 자리에서 다시 생각한다."),
            assignee=QUANT_ASSIGNEE,
            key=f"factory-quant-{stamp}", dry_run=dry_run)
    elif not qb:
        print("  퀀트: 실험 대기 가설 0건 - 카드를 만들지 않는다"
              "(할 일이 없는데 부르면 없는 일을 지어낸다)", flush=True)

    fails += _issue_bottleneck_cards(stamp=stamp, dry_run=dry_run)
    return fails


def _issue_bottleneck_cards(*, stamp: str, dry_run: bool = False) -> int:
    """**공장이 자기 병목을 스스로 세어 카드로 건다.**

    ▶ 왜 (2026-08-12)
      지금까지 병목은 사람이 하나씩 만나서 코드에 박았다. 그래서 공장은
      **박아 준 병목만** 볼 수 있었고, 못 박아 준 것은 매 주기 같은 값을
      조용히 태웠다. 돌린다고 똑똑해지지 않는 구조였다.

      인구조사는 종류를 미리 정하지 않는다. 실패 사유를 서명으로 군집하므로
      **이름을 모르는 사고도** 다음 주기에 저절로 목록에 뜬다. 처음 돌렸을 때
      우리가 몰랐던 것 둘이 바로 나왔다 - TESTING 정체 7건(56가설·일)과
      환류 누락 21건. 둘 다 아무도 코딩해 넣지 않은 종류다.

      **없앤 것은 다음 주기에 저절로 사라진다.** 보고서가 아니라 상태다.
    """
    try:
        import bottleneck_census                      # noqa: PLC0415
        items = bottleneck_census.census()
    except Exception as exc:  # noqa: BLE001 - 못 세면 카드를 안 만든다(지어내지 않는다)
        print(f"  병목 인구조사 실패 - 카드 생략: {type(exc).__name__}: "
              f"{str(exc)[:140]}", flush=True)
        return 1
    if not items:
        print("  병목: 관측 0건 - 카드를 만들지 않는다", flush=True)
        return 0

    # 담당별로 가른다. **남의 부서 병목을 받으면 고칠 권한이 없어 되돌아온다.**
    by_owner: dict[str, list] = {}
    for b in items:
        by_owner.setdefault(b.owner, []).append(b)

    assignees = {"research-department": RESEARCH_ASSIGNEE,
                 "quant-backtest-department": QUANT_ASSIGNEE}
    made = 0
    for owner, group in sorted(by_owner.items(),
                               key=lambda kv: -sum(b.cost for b in kv[1])):
        who = assignees.get(owner)
        if not who:
            continue
        body = bottleneck_census.card_body(group, top=3)
        if not body:
            continue
        _create_card(
            title=f"공장 개선: 관측된 병목 {len(group)}건 ({stamp})",
            body=(body + "\n\n---\n"
                  "[규칙]\n"
                  # ▶ 완료 판정을 자기보고에서 검사 실행으로 (2026-08-13, 문헌:
                  #   SWE-bench Verified 의 F2P/P2P 이중 기준 + BSG-VA 실측
                  #   "수리 에이전트 긍정 판정의 46%가 버그를 판별하지 않는
                  #   검사였다" + TDD Guard 의 RED 단계)
                  "- **완료 주장은 검사 실행으로만 한다.** 순서: ① 병목을 "
                  "재현하는 **실패하는 검사**를 먼저 만들어 빨강임을 확인하고 "
                  "(F2P), ② 고친 뒤 그 검사가 초록이 되고, ③ 기존 자체점검 "
                  "전부(P2P)가 하나도 깨지지 않아야 완료다. 원래부터 통과하던 "
                  "검사로 완료를 주장하는 것은 자동 적발된다.\n"
                  "- **검사·게이트 코드를 고쳐서 초록을 만들지 마라.** 판정자가 "
                  "바뀌면 해시가 바뀌어 성적표에 찍힌다.\n"
                  "- 못 끝내겠으면 **기권 사유를 구조화해 metadata 에 남겨라** - "
                  "기권도 1급 산출물이고, 조용한 미완이 제일 나쁘다.\n"
                  "- **하나를 골라 끝까지 없애라.** 여러 개를 조금씩 건드리면 "
                  "다음 주기에 전부 그대로 남는다.\n"
                  "- 없애기 전에 층을 갈라라 - `wiring-audit`, 데이터면 "
                  "`dataset-engineering` 의 probe_dataset.py.\n"
                  "- **'없다'고 적기 전에 실제로 열어 봐라.** 오늘 하루 그 오진이 "
                  "열두 번 났고 열두 번 다 있었다.\n"
                  "- 러너·창 분할·비용 모델까지 고쳐도 된다. 다만 무엇을 왜 "
                  "바꾸는지 **먼저** 적고, 자체 점검을 같이 내라.\n"
                  "- 뚫었으면 `skill-authoring` 으로 스킬에 남겨라. 남기지 않으면 "
                  "다음 주기의 네가 같은 자리를 다시 판다."),
            assignee=who, key=f"factory-bottleneck-{owner}-{stamp}",
            dry_run=dry_run)
        made += 1
    print(f"  병목: {len(items)}건 관측 -> 개선 카드 {made}건", flush=True)
    return 0


def _check_rejection_feedback(tmp: Path) -> None:
    """반려가 **다음 브리핑으로 돌아오는가.** 8/11 에 끊겨 있던 고리다.

    통제 어휘를 나열하는 것과 "네가 지난번 그것을 어겼다"고 말하는 것은 다른
    신호다. 전자만 있으면 기획자는 같은 코드를 또 지어낸다(실측).
    """
    from types import SimpleNamespace

    path = tmp / "rejections.tsv"
    assert recent_rejections(path=path) == [], "없는 파일에서 교훈을 지어냈다"

    rej = [SimpleNamespace(title="다층 호가 불균형 이후 단기 가격 지속성 검증",
                           reason="경쟁 설명 코드가 어휘 밖이다: REGIME_ARTIFACT")]
    assert record_rejections(rej, stamp="20260811T09", path=path) == 1
    assert record_rejections([], stamp="20260811T10", path=path) == 0, \
        "반려가 없는데 기록했다"

    got = recent_rejections(path=path)
    assert len(got) == 1 and "REGIME_ARTIFACT" in got[0], got
    assert "\n" not in got[0], "여러 줄이 섞이면 다음 읽기가 어긋난다"

    # 최근 것만 싣는다 - 오래된 반려까지 다 실으면 브리핑이 잡음이 된다
    for i in range(REJECT_KEEP + 3):
        record_rejections([SimpleNamespace(title=f"t{i}", reason=f"r{i}")],
                          stamp="x", path=path)
    assert len(recent_rejections(path=path)) == REJECT_KEEP
    assert recent_rejections(path=path)[-1].endswith(f"r{REJECT_KEEP + 2}")

    # 기록 실패가 수확을 죽이면 안 된다 - 없는 디렉터리로 확인한다
    assert record_rejections(rej, stamp="x", path=tmp / "없는곳" / "r.tsv") == 0
    print("  반려 환류 고리            OK")


def _check_delivery_is_the_tool_call_not_text():
    """**납품 = 도구 호출.** 텍스트 채널이 무엇을 싣든 판정과 무관해야 한다.

    (2026-08-13, 문헌 3건 독립 수렴: 12-Factor F4·F5 / Anthropic 아티팩트 /
    SWE-agent ACI) 실측: 같은 자리에 전문 2,434자와 요약 123자가 번갈아 와서
    본문을 잃었고, 에이전트는 이미 MCP 로 12회 제출하고 있었다. 정본을
    원장 대조로 바꾼다.
    """
    # 카드가 규약을 싣는다 - 에이전트가 어디에 뭘 넣어야 하는지
    assert "factory_submit_proposal" in DELIVERY_RULES
    assert "planner_run" in DELIVERY_RULES and "카드의 ID" in DELIVERY_RULES
    assert "기권도 1급" in DELIVERY_RULES

    # **모순 신호가 없어야 한다** (2026-08-13 실측 - 내가 만들 뻔했다).
    # 납품 규약은 "카드 텍스트는 납품이 아니다" 인데 옛 규칙이 "산출은 카드에
    # KEY:value 로" 라고 말하면, 지시와 파서가 다른 칸을 가리켜 8일을 끌었던
    # 그 버그 클래스가 재현된다. 서식 문구는 반드시 도구 호출 용도로 읽혀야 한다.
    import inspect as _ins

    _card_src = _ins.getsource(run_cycle) if "run_cycle" in globals() else ""
    _whole = _ins.getsource(sys.modules[__name__])
    _i = _whole.find("공장 주기 [기획자]")
    _blk = _whole[_i:_i + 4000]
    assert "planner_text 인자에 넣는 형식" in _blk, \
        "카드 규칙이 서식의 용도(도구 호출 인자)를 말하지 않는다"
    assert "줄로만. 문서로 쓰면" not in _blk, \
        "옛 문구(카드에 KEY:value 로 내라)가 남아 납품 규약과 모순된다"

    class _Cur:
        def __init__(self, exact, window):
            self._e, self._w = exact, window

        def execute(self, sql, params=()):
            self._last = "case_id" in sql
        def fetchone(self):
            return (self._e,) if self._last else (self._w,)
        def __enter__(self):  return self
        def __exit__(self, *a):  return False

    class _Conn:
        def __init__(self, cur):  self._c = cur
        def cursor(self):  return self._c

    saved = globals()["_board_rows"]
    try:
        globals()["_board_rows"] = lambda sql, params=(): [(1000, 1100)]
        # ① case_id 각인이 있으면 그것으로 끝 - 시간창은 안 본다
        assert _mcp_deliveries("t_x", _Conn(_Cur(2, 99))) == 2
        # ② 각인이 없으면 실행 시간창으로 안전망
        assert _mcp_deliveries("t_x", _Conn(_Cur(0, 1))) == 1
        # ③ 실행 기록이 없으면 0 - 시간창을 지어내지 않는다
        globals()["_board_rows"] = lambda sql, params=(): [(None, None)]
        assert _mcp_deliveries("t_x", _Conn(_Cur(0, 7))) == 0

        # ④ 못 재면 0 = 멱등한 텍스트 폴백으로 (이중 발행은 proposal_id 해시가 막는다)
        class _Boom:
            def cursor(self):
                raise RuntimeError("원장 연결 실패")
        assert _mcp_deliveries("t_x", _Boom()) == 0
    finally:
        globals()["_board_rows"] = saved
    print("  납품 = 도구 호출          OK")


def _check_improvement_cards_carry_acceptance():
    """**개선 카드가 F2P/P2P 수용기준을 싣는다.** 완료는 자기보고가 아니다.

    (BSG-VA 실측: 수리 에이전트 긍정 판정의 46%가 버그를 판별하지 않는
    검사였다.) 카드 규칙에 ① 실패 검사 먼저(RED) ② 기존 점검 무회귀 ③ 기권의
    1급 지위, 셋이 다 실려야 에이전트의 첫 완주가 기계 판정 가능해진다.
    """
    import inspect

    src = inspect.getsource(_issue_bottleneck_cards)
    for needle in ("실패하는 검사", "P2P", "기권", "검사·게이트 코드를 고쳐서"):
        assert needle in src, f"개선 카드 규칙에서 {needle!r} 가 빠졌다"
    print("  개선 카드에 수용기준      OK")


def _check_lost_output_is_not_called_empty():
    """**"에이전트가 안 했다" 와 "우리가 잃었다" 를 구분한다.** (2026-08-13 실측)

    `t_b2f39ed0` 이 30초 만에 done 이 됐고 수확기가 집은 것은 123자 요약뿐이라
    로그가 "산출이 비었다" 를 찍었다. 그런데 `task_runs.metadata` 에는
    `{"proposals": 1, "lead_ids": ["lead_2757a464…"], …}` 가 남아 있었다 -
    에이전트는 어제 묶여 있던 MAX 효과 리드를 집어 기획안을 만들었고
    **본문 채널이 샜을 뿐이다.** 둘을 섞으면 프롬프트를 고치러 가게 된다.
    """
    saved = globals()["_board_rows"]
    try:
        globals()["_board_rows"] = lambda sql, params=(): (
            [('{"proposals": 1, "lead_ids": ["lead_x"], '
              '"edge_type": "illiquidity_premium"}',)]
            if "metadata" in sql else [])
        got = _agent_claimed("t_x")
        assert got.get("proposals") == 1, got
        assert got.get("edge_type") == "illiquidity_premium", got

        # 못 읽으면 지어내지 않는다 - 유실 경보가 헛울리면 아무도 안 본다
        globals()["_board_rows"] = lambda sql, params=(): []
        assert _agent_claimed("t_x") == {}
        globals()["_board_rows"] = lambda sql, params=(): [("깨진 json",)]
        assert _agent_claimed("t_x") == {}

        def _boom(*a, **k):
            raise RuntimeError("보드 조회 실패")

        globals()["_board_rows"] = _boom
        assert _agent_claimed("t_x") == {}
    finally:
        globals()["_board_rows"] = saved
    print("  잃은 것을 안 했다로 안 봄 OK")


def _check_progress_is_measured_not_assumed():
    """**전진도 재서만 말한다.** 그리고 건전성과 한 줄로 합치지 않는다.

    (2026-08-13) 바깥 고리에 목적함수가 없어서 `병목 개수` 가 그 자리를
    대신하고 있었다. 병목이 0이 돼도 좋은 전략이 나온다는 보장이 없다 -
    건전한 채로 제자리인 공장이 가능하고 그때는 아무 경보도 안 울린다.
    """
    class _Bad:
        def cursor(self):
            raise RuntimeError("DB 없음")

    assert _progress_line(_Bad()) == "", "못 쟀는데 전진을 적었다"

    import progress as pg

    # 두 판정이 서로 다른 질문에 답해야 한다
    stuck = pg.Progress(measured=["recent_pioneered", "pioneered"],
                        pioneered=9, attempts=34, recent_pioneered=0)
    assert "전진 정지" in pg.brief_block(stuck)
    assert not stuck.advancing
    print("  전진도 재서만 말함        OK")


def _check_soundness_verdict_is_measured_not_assumed():
    """**못 쟀으면 "건전하다" 고 적지 않는다.** (2026-08-12)

    오늘 공장이 `발주 0건` 인 채로 카드를 정상으로 내보냈다. 진행(liveness)을
    안 쟀기 때문이다. 그런데 못 재고서 건전하다고 적는 것은 더 나쁘다 -
    빨간 것을 초록으로 보고하는 셈이다. 그래서 실패하면 **빈 문자열**이다.
    """
    class _Bad:
        def cursor(self):
            raise RuntimeError("DB 없음")

    assert _soundness_line(_Bad()) == "", "못 쟀는데 판정을 적었다"

    # 수준을 적는다 - 안 적으면 기획자가 매번 "내 몫인가" 를 다시 판단한다
    assert _level_of("'short_term_reversal' 는 어휘에 없다") == "구현"
    assert _level_of("실행면이 안 읽는 파라미터: universe") == "가설"
    assert _level_of("듣도 보도 못한 사고") == "불명"
    print("  건전성은 재서만 말함      OK")


def _check_orphan_blocked_gets_a_different_instruction():
    """**고칠 주체가 없는 가설에 "다시 등록해라"고 말하지 않는다.** (2026-08-12)

    `774f3b75` 는 배분자 변형이라 `proposal_id` 가 없다. 리서치가 다시 낼
    원본이 없는데 카드는 "리서치가 부모를 다시 등록해야 한다" 고만 적었다.
    그 사이 배분자는 못 도는 부모의 변형을 거부하므로 계열이 통째로 멈춘다 -
    아무도 고칠 수 없는 상태가 제일 나쁘다. 8일이 그렇게 갔다.
    """
    # 원장 원문 그대로. 두 줄 다 "다시 등록해야 한다" 로 끝난다 - 고아 쪽에만
    # 그게 거짓이 된다.
    _tail = " - 가설을 다시 등록해야 한다(재시도로는 안 풀린다)"
    blocked = {
        "774f3b75": "실행면이 안 읽는 파라미터: observation_refs, universe" + _tail,
        "667f0a45": "실행면이 안 읽는 파라미터: signal_window_days" + _tail,
    }
    own, lost = _split_blocked(blocked, {"774f3b75"})
    assert set(own) == {"667f0a45"}, own
    assert set(lost) == {"774f3b75"}, lost
    # 머리말이 "등록할 원본이 없다" 인데 줄이 "등록해라" 면 어느 쪽을 믿나
    assert "다시 등록" not in lost["774f3b75"], lost["774f3b75"]
    assert "observation_refs" in lost["774f3b75"], "사유까지 지우면 안 된다"
    # 주인 있는 쪽은 그 지시가 그대로 맞다 - 건드리지 않는다
    assert "다시 등록해야 한다" in own["667f0a45"]

    # 고아가 없으면 고아 문단을 만들지 않는다(없는 문제를 지어내지 않는다)
    own2, lost2 = _split_blocked(blocked, set())
    assert not lost2 and len(own2) == 2

    # 전부 고아면 "다시 기획하면 풀린다" 문단이 아예 안 나와야 한다
    own3, lost3 = _split_blocked(blocked, set(blocked))
    assert not own3 and len(lost3) == 2
    print("  고아 가설은 다른 지시      OK")


def _selfcheck() -> int:
    import tempfile

    print(f"{MODULE_VERSION} 자체 점검 (카드·DB 없음)")
    with tempfile.TemporaryDirectory() as d:
        _check_rejection_feedback(Path(d))
    v = _vocab_block()
    assert "REGIME_ARTIFACT" not in v, "내가 지어낸 어휘가 아직 브리핑에 있다"
    for token in ("EDGE_TYPE", "UNIVERSE_KEY", "COMPETING_CODES", "DATA_TABLES"):
        assert token in v, f"통제 어휘에 {token} 이 빠졌다"
    # IR 구조 진단이 브리핑에 실려 있는가 - 진단이 원장·보고서에만 남으면
    # 기획자는 다음 주기에도 관례 top-20 으로 낸다(2026-08-13)
    assert "TC 0.114" in v and "top_n" in v, "IR 구조 진단이 브리핑에서 빠졌다"
    print("  통제 어휘 출처            OK")
    _check_dataset_refresh_is_daily_and_ordered()
    _check_near_miss_surfaces_the_winner()
    _check_brief_blocks_run_before_close()
    _check_bottleneck_cards_are_issued_every_cycle()
    _check_quant_card_survives_empty_queue()
    _check_lead_health_is_surfaced()
    _check_unused_leads_come_first()
    _check_factory_enacts_its_own_top_move()
    _check_orphan_blocked_gets_a_different_instruction()
    _check_soundness_verdict_is_measured_not_assumed()
    _check_progress_is_measured_not_assumed()
    _check_lost_output_is_not_called_empty()
    _check_delivery_is_the_tool_call_not_text()
    _check_improvement_cards_carry_acceptance()
    print("자동 조종 16개 영역 통과. 실행은 --once / --loop")
    return 0


class _RowsConn:
    """자체 점검용 최소 연결. 넘겨준 행을 한 번 돌려주고 만다."""

    def __init__(self, rows): self._rows = rows

    def cursor(self):
        rows = self._rows
        class _C:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def execute(self_, *a, **k): return None
            def fetchall(self_): return rows
        return _C()


def _check_near_miss_surfaces_the_winner():
    """**관문에 근접한 계열이 브리핑에 뜬다.** (2026-08-12 사고 원문)

    momentum 은 초과·IR·DSR 을 통과하고 낙폭에서 막혔는데, 환류에 남은 건
    `fragility_fragile` 뿐이라 리서치가 "엣지가 없다" 로 읽고 다른 엣지를
    설계했다. 근접했다는 사실이 브리핑에 없으면 그 오독이 반복된다.
    """
    note = ("관문 4/8 통과. 남은 조항: max_drawdown -50.52% (기준 -35.0%, "
            "15.52% 모자람); pbo 미측정; fragility; bootstrap_ci 하한 -0.0029")
    out = _near_miss(_RowsConn([
        ("momentum|krx_all|fwd_20d|equal_weight", note,
         ["BEAR_FRAGILE", "SINGLE_REGIME_ONLY"]),
        # 같은 계열의 나쁜 시도가 좋은 것을 가리면 안 된다
        ("momentum|krx_all|fwd_20d|equal_weight", "관문 1/8 통과. 남은 조항: …", []),
        # 절반 미만은 근접이 아니다 - 올리지 않는다
        ("breakout|krx_all|fwd_20d|equal_weight", "관문 2/8 통과. 남은 조항: …", []),
    ]))
    assert "momentum" in out, out
    assert "4/8" in out and "1/8" not in out, "계열 최고치가 아니라 최신을 골랐다"
    assert "breakout" not in out, "절반 미만인 계열을 근접으로 올렸다"
    assert "max_drawdown_stop" in out, "손잡이로 잇는 문장이 없다"
    assert "알파가" in out, "왜 이쪽이 먼저인지 설명이 없다"

    # 근접한 것이 없으면 **빈 문자열** - 없는 것을 지어내지 않는다
    assert _near_miss(_RowsConn([])) == ""
    assert _near_miss(_RowsConn([("f", "관문 2/8 통과.", [])])) == ""
    # 못 읽으면 조용히 비운다(브리핑 전체를 죽이지 않는다)
    class _Boom:
        def cursor(self): raise RuntimeError("DB 없음")
    assert _near_miss(_Boom()) == ""
    print("  근접 계열 노출            OK")


def _check_brief_blocks_run_before_close():
    """**브리핑 조회 블록이 살아 있는 연결에서 돈다.** (2026-08-12 사고 원문)

    `research_brief` 가 리드만 읽고 `finally: conn.close()` 로 닫은 뒤, 그
    아래 네 블록이 닫힌 연결로 조회하고 있었다. 블록마다 예외를 삼키므로
    (`못 읽으면 적지 않는다` - 그 자체는 옳다) **아무 소리 없이** 사라졌다.
    `_dataset_news` 는 통째로 죽어 있었고, "막힌 가설을 리서치에 보여 준다" 고
    적어 둔 블록은 도달한 적이 없었다.

    AST 로 본다 - 문자열 검색은 이 주석 자신에 걸린다.
    """
    import ast  # noqa: PLC0415

    def _use_after_close(src: str, fname: str) -> list:
        """`conn.close()` 뒤에 남은 `conn` 사용 줄. 없으면 빈 목록."""
        fn = next((n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == fname), None)
        assert fn is not None, f"{fname} 가 사라졌다"
        closes_at = None
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "conn"):
                closes_at = (node.lineno if closes_at is None
                             else min(closes_at, node.lineno))
        assert closes_at is not None, "conn.close() 가 사라졌다 - WAL 이 남는다"
        return sorted({n.lineno for n in ast.walk(fn)
                       if isinstance(n, ast.Name) and n.id == "conn"
                       and n.lineno > closes_at})

    # ▶ **검사가 자기 사고를 잡는지 먼저 확인한다.** 사고 원문 구조를 픽스처로
    #   넣는다 - 안 잡는 검사를 통과시키면 아무것도 안 지킨 것이다.
    accident = (
        "def research_brief():\n"
        "    conn = _conn()\n"
        "    try:\n"
        "        brief = cycle_brief.build(conn)\n"
        "    finally:\n"
        "        conn.close()\n"
        "    out.append(_vocab_block(conn))\n"      # <- 닫힌 연결
        "    out.append(_dataset_news(conn))\n"     # <- 닫힌 연결
        "    return out\n")
    assert _use_after_close(accident, "research_brief"), (
        "검사가 사고 원문을 못 잡는다 - 이 검사는 아무것도 지키지 않는다")

    used_after = _use_after_close(
        Path(__file__).read_text(encoding="utf-8"), "research_brief")
    assert not used_after, (
        f"conn.close() 뒤에서 conn 을 쓴다: {used_after}행 - "
        f"닫힌 연결 조회는 예외를 삼키는 블록에서 **조용히 빈 값**이 된다")
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    # ▶ 조회 블록들이 본문 조립부에 실제로 있어야 한다. 검사만 통과하고
    #   블록이 사라지면 아무것도 안 지킨 것이다.
    body = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_compose_research_brief"), None)
    assert body is not None, "_compose_research_brief 가 사라졌다"
    called = {n.func.id for n in ast.walk(body)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for need in ("_vocab_block", "_dataset_news", "_near_miss",
                 "_structurally_blocked"):
        assert need in called, f"{need} 가 브리핑 조립부에서 빠졌다"
    print("  브리핑 블록이 살아있는 연결에서 돎  OK")


def _check_bottleneck_cards_are_issued_every_cycle():
    """**공장이 자기 병목을 매 주기 카드로 내는가.** (2026-08-12)

    인구조사를 만들어 두고 주기에 안 걸면 아무 일도 안 일어난다 - 오늘
    하루 그 모양(만들어 놓고 안 부름)을 세 번 봤다. `evaluate_promotion` 은
    336줄이 한 번도 안 불렸고, `_check_dataset_comes_from_hypothesis` 는
    정의만 돼 있었다.
    """
    import ast  # noqa: PLC0415

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    cyc = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "cycle"), None)
    assert cyc is not None, "cycle 이 사라졌다"
    called = {n.func.id for n in ast.walk(cyc)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_issue_bottleneck_cards" in called, (
        "주기가 병목 카드를 안 낸다 - 인구조사가 돌아도 아무 일이 안 일어난다")

    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "_issue_bottleneck_cards"), None)
    assert fn is not None
    src = ast.get_source_segment(Path(__file__).read_text(encoding="utf-8"), fn) or ""
    # 담당별로 갈라야 한다 - 남의 부서 병목은 고칠 권한이 없어 되돌아온다
    assert "by_owner" in src, "병목을 담당별로 안 가른다"
    # 뚫은 것을 남기라는 지시가 있어야 다음 주기가 똑똑해진다
    assert "skill-authoring" in src, "뚫은 것을 스킬로 남기라는 지시가 없다"
    print("  병목 카드가 주기에 걸림   OK")


def _check_quant_card_survives_empty_queue():
    """**대기 가설이 0이어도 둘 수 있는 수가 있으면 카드를 만든다.** (2026-08-12)

    예전엔 `if not pending: return "", 0` 이었다. 그런데 배분자가 보는 것은
    이미 완주한 계열이라, 대기 가설이 없을 때가 오히려 다음 수를 둘 때다.
    그대로 뒀으면 최고의 수를 둬야 하는 순간에 공장이 쉬었다.
    """
    import ast  # noqa: PLC0415

    src = Path(__file__).read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "quant_brief"), None)
    assert fn is not None, "quant_brief 가 사라졌다"
    body = ast.get_source_segment(src, fn) or ""
    assert "not pending and not n_moves" in body, \
        "대기 가설만 보고 카드를 접는다 - 둘 수 있는 수가 있어도 공장이 쉰다"
    assert "_allocator_block" in body, "배분자를 안 부른다"
    # **연결이 살아 있을 때 불러야 한다** - 닫고 부르면 조용히 빈 값이 된다
    closes = body.index("conn.close()")
    assert body.index("_allocator_block(conn)") < closes, \
        "닫힌 연결로 배분자를 부른다 - 조용히 빈 값이 된다"
    print("  빈 큐에도 다음 수 카드   OK")


def _check_factory_enacts_its_own_top_move():
    """**공장이 1순위 수를 직접 둔다.** 에이전트를 기다리지 않는다. (2026-08-12)

    배분자가 좋은 수를 찾아도 카드로만 나가면 에이전트가 읽을 때까지 아무 일도
    안 난다 - 실험 2~3초짜리가 5건 완주에 4시간 걸린 이유다.

    다만 **판단이 필요한 수까지 자동으로 두면 안 된다.** 그 경계가 지켜지는지
    같이 본다 - 공장이 사람 없이 가설을 지어내면 사전등록이 무의미해진다.
    """
    import ast  # noqa: PLC0415

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    disp = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "_dispatch_experiments"), None)
    assert disp is not None, "_dispatch_experiments 가 사라졌다"
    called = {n.func.id for n in ast.walk(disp)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_enact_top_move" in called, \
        "발주가 배분자의 수를 안 둔다 - 좋은 수를 찾아도 아무 일도 안 난다"

    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_enact_top_move")
    body = ast.get_source_segment(src, fn) or ""
    # **결정론적인 수만** 둔다. 손잡이 값 고르기는 가설이라 에이전트 몫이다
    assert '"표본 확대"' in body, "결정론적인 수로 한정하지 않는다"
    assert "self_defeating" in body, "자멸적인 수를 거르지 않는다"
    assert "return 1" in body, "한 주기에 하나만 두는 제한이 없다"
    print("  공장이 1순위 수를 직접 둠 OK")


def _check_unused_leads_come_first():
    """**안 쓴 리드가 맨 앞이다.** (2026-08-12 실측 - 이걸로 공장이 멈췄다)

    예전 정렬은 `COMPLETE 먼저, 최신 먼저` 라 **이미 쓴 리드가 위**에 왔다.
    에이전트가 그걸 보고 같은 계열을 재제안했고 Gate 0 가 "이미 2회
    기각됐다" 로 거부해 발주가 0건이 됐다. 그때 안 쓴 리드 4건이 목록
    아래에 묻혀 있었다.
    """
    assert "order by used asc" in _SQL_LEADS, "안 쓴 리드를 앞에 안 놓는다"
    assert "experiment_proposals" in _SQL_LEADS, "사용 여부를 안 본다"

    # 표시가 갈려야 한다 - 안 갈리면 봐도 모른다
    import ast
    src = Path(__file__).read_text(encoding="utf-8")
    body = ast.get_source_segment(src, next(
        n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
        and n.name == "_compose_research_brief")) or ""
    assert "미사용" in body and "이미 씀" in body, "쓴 것/안 쓴 것 표시가 없다"
    # 옛 6-튜플 언팩이 남아 있으면 7번째 컬럼에서 터진다
    assert "for lid, lens, edge, mech, test, status in leads" not in body, \
        "옛 언팩이 남아 있다 - 새 컬럼에서 죽는다"
    print("  안 쓴 리드가 맨 앞         OK")


def _check_lead_health_is_surfaced():
    """**재료가 마르면 카드가 먼저 말한다.** (2026-08-12 실측)

    공장이 통째로 멈췄다: 스카우트 정지 → 리드 고갈 → 같은 계열 재제안 →
    Gate 0 거부 → 발주 0건. 자동 조종에 **스카우트를 부르는 곳이 없었고**
    카드는 "기획안을 내라" 만 요구했다.
    """
    class _Rows:
        def __init__(self, row): self._row = row
        def cursor(self):
            row = self._row
            class _C:
                def __enter__(self_): return self_
                def __exit__(self_, *a): return False
                def execute(self_, *a, **k): return None
                def fetchone(self_): return row
            return _C()

    # 실측 상태: 리드 12건 · 안 쓴 것 4건 · 최신 1.2일 전
    #   → 안 쓴 것이 기준(4) 이상이고 1.2일이면 아직 조용하다
    assert _lead_health(_Rows((12, 4, 1.2))) == ""

    # 안 쓴 것이 줄면 말한다
    low = _lead_health(_Rows((12, 1, 0.5)))
    assert "재료 부족" in low and "안 쓴 리드가 1건" in low, low
    assert "스카우트가 먼저" in low
    # **어떻게 훑는지**까지 알려줘야 한다 - 도구를 모르면 못 한다
    assert "agent-reach" in low and "r.jina.ai" in low, low

    # 오래되면 말한다
    stale = _lead_health(_Rows((12, 9, 3.4)))
    assert "3.4일 전" in stale, stale

    # 못 세면 안 적는다
    class _Boom:
        def cursor(self): raise RuntimeError("표 없음")
    assert _lead_health(_Boom()) == ""

    # **브리핑 조립부가 실제로 부르는가** - 안 부르면 검사가 장식이다
    import ast
    src = Path(__file__).read_text(encoding="utf-8")
    body = ast.get_source_segment(src, next(
        n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
        and n.name == "_compose_research_brief")) or ""
    assert "_lead_health(conn)" in body, "브리핑이 재료 부족을 안 싣는다"
    print("  재료 부족을 카드가 말함   OK")


def _check_dataset_refresh_is_daily_and_ordered():
    """데이터셋 갱신이 **하루 한 번**이고 **발주 뒤**인가.

    자동 조종은 15분마다 돈다. 파티션을 그때마다 굳히면 하루 96번 같은 파일을
    쓴다. 그리고 갱신이 발주보다 앞서면, 오래 걸리는 굳히기 때문에 그 주기의
    실험이 통째로 밀린다.
    """
    import inspect

    src = inspect.getsource(_refresh_datasets)
    assert "_DATASET_MARK.read_text" in src and "return 0" in src, \
        "하루 한 번 가드가 없다"
    # 실패하면 표식을 남기지 않아야 다음 날 다시 시도한다
    i_fail = src.index("데이터셋 {label} 실패")
    i_mark = src.index("_DATASET_MARK.write_text")
    assert i_fail < i_mark, "실패해도 표식을 남긴다 - 그러면 하루를 통째로 건너뛴다"

    cyc = inspect.getsource(cycle)
    assert cyc.index("_dispatch_experiments") < cyc.index("_refresh_datasets"), \
        "데이터셋 갱신이 발주보다 앞선다 - 그 주기의 실험이 밀린다"
    print("  데이터셋 갱신 주기·순서   OK")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="전략 공장 자동 조종")
    ap.add_argument("--check", action="store_true",
                    help="실행 없이 자체 점검만 한다")
    ap.add_argument("--once", action="store_true", help="한 주기만 돌고 끝낸다")
    ap.add_argument("--loop", action="store_true", help="주기마다 계속 돈다")
    ap.add_argument("--interval-min", type=int, default=240,
                    help="--loop 주기(분). 기본 4시간")
    ap.add_argument("--dry-run", action="store_true",
                    help="카드를 만들지 않고 무엇을 걸지만 보여준다")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if a.check:
        return _selfcheck()

    if not (a.once or a.loop):
        ap.error("--once / --loop / --check 중 하나를 지정하라")

    if a.once:
        return 1 if cycle(dry_run=a.dry_run) else 0

    interval = max(10, int(a.interval_min)) * 60
    print(f"{MODULE_VERSION} 반복 시작 - {a.interval_min}분마다", flush=True)
    while True:
        try:
            cycle(dry_run=a.dry_run)
        except KeyboardInterrupt:
            print("\n중단됨", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - 한 주기 실패가 운전을 멈추지 않는다
            print(f"  !! 주기 실패: {type(exc).__name__}: {str(exc)[:200]}",
                  flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
