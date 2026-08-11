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


def research_brief() -> str:
    """리서치본부가 다음 기획안을 낼 때 볼 사실. 결론은 없다."""
    import cycle_brief                             # noqa: PLC0415

    conn = _conn()
    try:
        brief = cycle_brief.build(conn)
    finally:
        conn.close()      # WAL 을 남기지 않는다 - with 는 커밋만 하고 안 닫는다
    return brief.as_prompt()


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


def cycle(*, dry_run: bool = False) -> int:
    """공장 한 주기. 실패한 부서 수를 돌려준다(0이면 정상)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H")
    print(f"[{stamp}] 공장 자동 조종 - 브리핑을 만들어 부서에 건다", flush=True)
    fails = 0

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
        _create_card(
            title="공장 주기: 다음 실험 기획안 1건",
            body=(rb + "\n\n---\n"
                  "요청: 위 사실만 근거로 **다음에 실험할 기획안 1건**을 내라.\n"
                  "- 이미 기각된 계열을 다시 내려면 그 교훈에 어떻게 대응하는지 "
                  "본문에 적어라(안 적으면 Gate 0 가 DUPLICATE_UNADDRESSED 로 막는다).\n"
                  "- 예산이 소진된 계열은 제안하지 마라.\n"
                  "- 반대편(누가 손해를 보는가)과 경쟁 설명을 반드시 적어라 - "
                  "없으면 발행 게이트에서 막힌다.\n"
                  "- 근거가 부족하면 **기획안을 내지 말고 무엇이 부족한지 적어라.** "
                  "지어낸 가설은 원장을 오염시킨다."),
            assignee=RESEARCH_ASSIGNEE,
            key=f"factory-research-{stamp}", dry_run=dry_run)

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="전략 공장 자동 조종")
    ap.add_argument("--once", action="store_true", help="한 주기만 돌고 끝낸다")
    ap.add_argument("--loop", action="store_true", help="주기마다 계속 돈다")
    ap.add_argument("--interval-min", type=int, default=240,
                    help="--loop 주기(분). 기본 4시간")
    ap.add_argument("--dry-run", action="store_true",
                    help="카드를 만들지 않고 무엇을 걸지만 보여준다")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not (a.once or a.loop):
        ap.error("--once 또는 --loop 중 하나를 지정하라")

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
