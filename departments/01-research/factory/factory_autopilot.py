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

필수: TITLE, LEAD_IDS, ECONOMIC_RATIONALE, COUNTERPARTY, EDGE_TYPE, UNIVERSE_KEY,
      COMPETING_EXPLANATION.
하나라도 비면 그 기획안은 접수 전에 반려된다.

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


_SQL_LEADS = """
select lead_id, scout_lens, claimed_edge, stated_mechanism, testability, status
  from research.methodology_leads
 where status in ('COMPLETE', 'PARTIAL')
 order by (status = 'COMPLETE') desc, created_at desc
 limit %s
"""


def _vocab_block() -> str:
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

    sources = ""
    try:
        from data_resolution import SOURCE_TABLES  # noqa: PLC0415
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
        runnable = ("\n  ▶ 지금 **실험까지 도는** EDGE_TYPE: "
                    f"{', '.join(impl)}\n"
                    "    나머지는 접수는 되지만 실험 단계에서 '전략 구현' 으로 "
                    "막힌다 - 구현된 것 중에서 고르는 편이 한 주기를 아낀다.")
    except Exception:  # noqa: BLE001 - 못 읽으면 적지 않는다(지어내지 않는다)
        pass
    try:
        from config_binding import EDGE_KEYS      # noqa: PLC0415

        params = ("\n  SUGGESTED_PARAMS 에 쓸 수 있는 키: "
                  f"{', '.join(sorted(EDGE_KEYS))}\n"
                  "    이 밖의 키는 실행면이 읽지 않아 거부된다.")
    except Exception:  # noqa: BLE001
        pass

    return ("\n[통제 어휘 - 밖의 값은 반려된다]\n"
            f"  EDGE_TYPE    : {', '.join(sorted(EDGE_VOCAB))}\n"
            f"  UNIVERSE_KEY : {', '.join(sorted(UNIVERSE_VOCAB))}\n"
            f"  COMPETING_CODES: {', '.join(c.value for c in CompetingExplanation)}"
            + sources + runnable + params)


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

    conn = _conn()
    try:
        brief = cycle_brief.build(conn)
        with conn.cursor() as cur:
            cur.execute(_SQL_LEADS, (12,))
            leads = cur.fetchall()
    finally:
        conn.close()      # WAL 을 남기지 않는다 - with 는 커밋만 하고 안 닫는다

    out = [brief.as_prompt()]
    if leads:
        out.append("\n쓸 수 있는 리드 (LEAD_IDS 에 이 id 만 쓴다):")
        for lid, lens, edge, mech, test, status in leads:
            out.append(f"  - {lid}  [{lens}/{status}] {str(edge)[:70]}")
            if mech:
                out.append(f"      메커니즘: {str(mech)[:110]}")
    else:
        out.append("\n**쓸 수 있는 리드가 없다.** 리드 없이 기획안을 내지 마라 - "
                   "LEAD_IDS 가 비면 접수 전에 반려된다.")
    out.append(_vocab_block())
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
        out.append("  위 사유를 읽고 **그 원인을 제거한 기획안**을 내라. "
                   "통제 어휘 밖의 값을 지어내지 말고, 필요한 개념이 어휘에 "
                   "없으면 가장 가까운 코드를 쓰고 그 한계를 본문에 적어라.")
    return "\n".join(out)


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
    finally:
        conn.close()

    if not pending:
        return "", 0

    out = ["[원장에서 읽은 사실 - 판단은 네가 한다]", "",
           f"실험을 기다리는 가설 {len(pending)}건:"]
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

    out.append("")
    out.append("실험은 experiment_orchestrator 가 돌린다 - 사전등록 지문이 "
               "박히고 창별 강건성·과적합 통계가 함께 나온다. 결과 수치를 보고 "
               "설정을 바꾸면 사전등록이 무의미해진다.")
    return "\n".join(out), len(pending)


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


def harvest(*, dry_run: bool = False) -> int:
    """완료된 기획자·회의론자 카드를 원장으로 들인다. **여기서 루프가 닫힌다.**

    ▶ 왜 호스트가 제출하나
      제출은 `proposal_intake` 가 리드를 원장에서 다시 읽어 대조하고 발행 게이트를
      돌리는 결정론 절차다. 에이전트에게 맡기면 그 절차가 에이전트의 성실성에
      의존하게 된다 - 서명을 스스로 찍는 것과 같다. 판단은 에이전트가, 접수는
      코드가 한다.

    반환: 새로 발행된 기획안 수.
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
        planner_text = _agent_output(planner_id)
        if not planner_text:
            print(f"  수확: {stamp} 기획자 산출이 비었다 - 회의론자를 걸지 않는다",
                  flush=True)
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
                          leads=leads)
            pub = r.publishable
            if dry_run:
                print(f"  [dry-run] {stamp}: 발행가능 {len(pub)}건 "
                      f"반려 {len(r.rejected)}건 미상리드 {unknown}")
                continue
            new, dup = (PI.persist(conn, pub) if pub else (0, 0))
            published += new
            print(f"  수확 {stamp}: 발행 {new} 중복 {dup} 반려 {len(r.rejected)}",
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
        with conn.cursor() as cur:
            cur.execute(_SQL_NEEDS_EXPERIMENT, (limit,))
            rows = cur.fetchall()
        if not rows:
            return 0
        if dry_run:
            print(f"  [dry-run] 발주 대상 {len(rows)}건: "
                  f"{', '.join(t for _, t in rows)[:120]}", flush=True)
            return 0
        sent = 0
        for hid, title in rows:
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
            title=f"공장 주기 [기획자]: 다음 실험 기획안 1건 {stamp}",
            body=(rb + "\n\n---\n" + PLANNER_FORMAT + "\n\n"
                  "[규칙]\n"
                  "- 이미 기각된 계열을 다시 내려면 그 교훈에 어떻게 대응하는지 "
                  "ECONOMIC_RATIONALE 에 적어라(안 적으면 Gate 0 가 "
                  "DUPLICATE_UNADDRESSED 로 막는다).\n"
                  "- 예산이 소진된 계열은 제안하지 마라.\n"
                  "- 쓸 수 있는 리드 목록에 없는 id 를 대지 마라 - 원장에서 다시 "
                  "읽어 대조하므로 막힌다.\n"
                  "- 근거가 부족하면 **기획안을 내지 말고 무엇이 부족한지 적어라.** "
                  "지어낸 가설은 원장을 오염시킨다.\n"
                  "- 산출은 위 `KEY: value` 줄로만. 문서로 쓰면 파싱이 0건이 되어 "
                  "아무것도 접수되지 않는다."),
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
            title=f"공장 주기: 대기 가설 {n_pending}건 실험·판정",
            body=(qb + "\n\n---\n"
                  "요청: 위 가설 중 **우선순위가 높은 것부터** 실험을 돌리고 "
                  "결과를 판정하라.\n"
                  "- 판정은 사전등록한 반증 기준으로만 한다. 결과를 보고 기준을 "
                  "바꾸면 그건 실험이 아니다.\n"
                  "- 기각이면 교훈을 환류에 적재하라 - 다음 기획안이 그것을 읽는다.\n"
                  "- 미측정 지표를 0 으로 채우지 마라. 없는 것은 없는 것이다."),
            assignee=QUANT_ASSIGNEE,
            key=f"factory-quant-{stamp}", dry_run=dry_run)
    elif not qb:
        print("  퀀트: 실험 대기 가설 0건 - 카드를 만들지 않는다"
              "(할 일이 없는데 부르면 없는 일을 지어낸다)", flush=True)

    return fails


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


def _selfcheck() -> int:
    import tempfile

    print(f"{MODULE_VERSION} 자체 점검 (카드·DB 없음)")
    with tempfile.TemporaryDirectory() as d:
        _check_rejection_feedback(Path(d))
    v = _vocab_block()
    assert "REGIME_ARTIFACT" not in v, "내가 지어낸 어휘가 아직 브리핑에 있다"
    for token in ("EDGE_TYPE", "UNIVERSE_KEY", "COMPETING_CODES", "DATA_TABLES"):
        assert token in v, f"통제 어휘에 {token} 이 빠졌다"
    print("  통제 어휘 출처            OK")
    print("자동 조종 2개 영역 통과. 실행은 --once / --loop")
    return 0


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
