#!/usr/bin/env python3
"""승격 다리 - 홀드아웃을 통과한 확증을 **원장에 기록하고 배포 준비까지** 한다.

담당: 재일 (퀀트·백테스트본부 QNT)

▶ 왜 이게 없었나 (2026-08-25 실측)
  `strategy.evaluations`·`promotion_decisions`·`deployments` 는 **스키마만 있고
  쓰는 코드가 0곳**이었다. 등록된 유일한 전략은 E2E 픽스처
  (`fixture://paper-legacy-e2e/v1`)였고 `approved_by_decision_id` 는 null 이다.
  실제 배포는 `~/mlpipe-paper/run_paper.sh` 가 컨테이너를 띄우는 별도 경로였다.

  즉 **배포는 되는데 왜 배포했는지가 안 남았다.** 이 모듈은 그 자리를 메운다.

▶ 무엇을 자동화하고 무엇을 안 하나
  자동: 확증 결과 → `strategy.strategies`/`versions`/`evaluations`/
        `promotion_decisions` 기록, 배포 명령 생성.
  **안 함: 실제 `docker run`.** 돈이 걸린 행위는 사람이 방아쇠를 당긴다.
  `--deploy` 를 명시해야만 기동하고, 기본값은 명령만 출력한다.

▶ 무엇을 승격의 근거로 삼나
  홀드아웃 판정 하나뿐이다. 탐색 성적은 근거가 아니다 - 탐색은 p-해킹을
  허용한 구간이므로 거기서 좋았다는 사실은 아무것도 보장하지 않는다.
  사전등록 지문(`prereg_sha256`)이 확증 카드에 박힌 것과 같아야 한다.

사용:
    python3 promotion.py --self-check
    python3 promotion.py --list                # 승격 대기 확증
    python3 promotion.py --promote r0007 --dry-run
    python3 promotion.py --promote r0007
    python3 promotion.py --promote r0007 --deploy    # 컨테이너까지 기동
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import research_log as rlog                                    # noqa: E402

MODULE_VERSION = "promotion-bridge-v1"
OWNER_DEPARTMENT = "quant-backtest-department"
RUNNER_SCRIPT = os.getenv("PAPER_RUNNER", str(Path.home() / "mlpipe-paper"
                                              / "run_paper.sh"))


def _now():
    return datetime.now(timezone.utc)


def _connect():
    """control DB. RLS 때문에 svc_quant 롤을 신어야 quant 스키마가 보인다."""
    import psycopg2
    dsn = os.environ.get("QUANT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("QUANT_DATABASE_URL 이 없다")
    conn = psycopg2.connect(dsn, connect_timeout=20)
    with conn.cursor() as cur:
        cur.execute("set role svc_quant")
    return conn


# ── 승격 자격 판정 ──────────────────────────────────────────────────────────
def eligibility(entry, *, committee_decision_id: str = ""
                ) -> tuple[bool, list[str]]:
    """승격해도 되는가. **잰 것만 근거로 삼는다.**"""
    why: list[str] = []
    if entry.kind != "confirm":
        why.append("NOT_A_CONFIRMATION")
    if entry.status != "DONE":
        why.append("NOT_DONE")
    res = entry.confirm_result or {}
    if not res:
        why.append("NO_CONFIRM_RESULT")
    elif not bool(res.get("pass")):
        why.append("HOLDOUT_NOT_PASSED")
    # 사전등록 지문이 후보 사양과 일치해야 한다 - 홀드아웃을 본 뒤 사양을
    # 고쳤으면 그 판정은 확증이 아니다.
    cand = entry.candidate or {}
    if not cand:
        why.append("NO_CANDIDATE_SPEC")
    elif entry.prereg_sha256 and \
            rlog.prereg_fingerprint(cand) != entry.prereg_sha256:
        why.append("PREREG_FINGERPRINT_MISMATCH")
    # 홀드아웃을 실제로 썼는가. 확증인데 개발 세션만 썼으면 확증이 아니다.
    used = [str(x) for x in (entry.sessions_used or [])]
    if not any(u >= rlog.HOLDOUT_FROM for u in used):
        why.append("HOLDOUT_NOT_USED")
    # ▶ **다리는 스스로 승인하지 않는다.** `promotion_decisions` 는 위원회
    #   결정을 NOT NULL FK 로 요구한다 - 설계가 그렇다. 여기서 결정을
    #   만들어 넣으면 관문이 장식이 된다.
    if not str(committee_decision_id or "").strip():
        why.append("NO_COMMITTEE_DECISION")
    return (not why), why


# ── 원장 기록 ───────────────────────────────────────────────────────────────
_SQL_STRATEGY = """
insert into strategy.strategies
  (strategy_id, strategy_code, name, family, directionality, status,
   owner_department, current_version)
values (%s,%s,%s,%s,%s,'PAPER',%s,1)
on conflict (strategy_code) do update set updated_at = now()
returning strategy_id
"""

_SQL_VERSION = """
insert into strategy.versions
  (strategy_version_id, strategy_id, version, code_version, artifact_path,
   artifact_hash, config, signal_schema, target_portfolio_schema,
   capability_profile_id, deployment_state, effective_from)
values (%s,%s,1,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,'PAPER', now())
returning strategy_version_id
"""

# 기존 능력 프로필을 재사용한다. 새로 만들면 실행면이 감당 못 하는 능력을
# 스스로 부여하는 셈이라, 있는 것 중에서 고른다.
_SQL_CAPABILITY = """
select capability_profile_id from strategy.capability_profiles
 order by created_at limit 1
"""

_SQL_EVALUATION = """
insert into strategy.evaluations
  (evaluation_id, strategy_version_id, environment, window_start,
   window_end, metrics, decision)
values (%s,%s,'PAPER',%s,%s,%s::jsonb,%s)
returning evaluation_id
"""

_SQL_DECISION = """
insert into strategy.promotion_decisions
  (promotion_decision_id, strategy_version_id, from_stage, to_stage,
   evaluation_id, committee_decision_id, decision, conditions, decided_at)
values (%s,%s,'RESEARCH','PAPER',%s,%s,'APPROVE',%s::jsonb, now())
returning promotion_decision_id
"""


def promote(entry_id: str, *, dry_run: bool = False,
            deploy: bool = False,
            committee_decision_id: str = "") -> dict:
    entries = rlog.latest_by_id()
    entry = entries.get(entry_id)
    if entry is None:
        return {"ok": False, "why": ["UNKNOWN_ENTRY"]}

    ok, why = eligibility(
        entry, committee_decision_id=committee_decision_id)
    if not ok and not (dry_run and why == ["NO_COMMITTEE_DECISION"]):
        return {"ok": False, "why": why}

    cand = entry.candidate or {}
    res = entry.confirm_result or {}
    code = str(cand.get("name") or entry_id).strip().upper().replace(" ", "_")[:40]
    holdout = sorted(str(x) for x in (entry.sessions_used or [])
                     if str(x) >= rlog.HOLDOUT_FROM)

    plan = {
        "entry": entry_id,
        "strategy_code": code,
        "claim": cand.get("claim"),
        "script": cand.get("script"),
        "prereg_sha256": entry.prereg_sha256,
        "holdout_sessions": holdout,
        "holdout_result": res,
        "deploy_command": f"bash {RUNNER_SCRIPT} <SESSION>",
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}

    sid = str(uuid.uuid4())
    vid = str(uuid.uuid4())
    eid = str(uuid.uuid4())
    did = str(uuid.uuid4())
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_STRATEGY, (
                sid, code, str(cand.get("claim") or code)[:120],
                "research-bench", str(cand.get("side") or "LONG").upper(),
                OWNER_DEPARTMENT))
            sid = cur.fetchone()[0]
            cur.execute(_SQL_CAPABILITY)
            row = cur.fetchone()
            if row is None:
                raise SystemExit("strategy.capability_profiles 가 비었다 - "
                                 "실행면 능력 프로필을 먼저 등록해라")
            cap_id = row[0]
            cur.execute(_SQL_VERSION, (
                vid, sid, f"{MODULE_VERSION}:{entry_id}",
                f"research://bench/{entry_id}", entry.prereg_sha256 or entry_id,
                json.dumps({"execution_mode": "PAPER", "live_orders": False,
                            "params": cand.get("params") or {},
                            "script": cand.get("script")}, ensure_ascii=False),
                json.dumps({"schema": "research-bench-signal.v1"}),
                json.dumps({"schema": "research-bench-portfolio.v1"}),
                cap_id))
            vid = cur.fetchone()[0]
            cur.execute(_SQL_EVALUATION, (
                eid, vid, holdout[0] if holdout else None,
                holdout[-1] if holdout else None,
                json.dumps(res, ensure_ascii=False), "PASS"))
            eid = cur.fetchone()[0]
            cur.execute(_SQL_DECISION, (
                did, vid, eid, committee_decision_id,
                json.dumps({"prereg_sha256": entry.prereg_sha256,
                            "research_entry": entry_id,
                            "holdout_sessions": holdout,
                            "decided_by": MODULE_VERSION},
                           ensure_ascii=False)))
            did = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    plan.update({"strategy_id": sid, "strategy_version_id": vid,
                 "evaluation_id": eid, "promotion_decision_id": did})

    if deploy:
        import subprocess
        session = _now().strftime("%Y-%m-%d")
        r = subprocess.run(["bash", RUNNER_SCRIPT, session],
                           capture_output=True, text=True, timeout=120)
        plan["deploy_rc"] = r.returncode
        plan["deploy_out"] = ((r.stdout or "") + (r.stderr or ""))[-400:]
    else:
        # **돈이 걸린 행위는 사람이 방아쇠를 당긴다.** 명령만 낸다.
        plan["deploy_note"] = ("배포는 실행하지 않았다. 돌리려면 "
                               f"`bash {RUNNER_SCRIPT} <SESSION>`")
    return {"ok": True, "plan": plan}


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    fails = 0

    def ok(name, cond):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    E = rlog.Entry
    good_cand = {"name": "spike_fade_v3", "script": "research/scripts/r7.py",
                 "claim": "마감 전 호가 소멸 후 익일 개장 약세",
                 "params": {"h": 60}}
    fp = rlog.prereg_fingerprint(good_cand)

    base = dict(id="r0007", ts="t", question="q", status="DONE", kind="confirm",
                candidate=good_cand, prereg_sha256=fp,
                confirm_result={"pass": True, "net_bps": 12.3},
                sessions_used=["2026-08-10", "2026-08-11"])

    _CD = "11111111-1111-4111-8111-111111111111"
    ok("정상 확증은 승격 자격",
       eligibility(E(**base), committee_decision_id=_CD)[0])
    ok("위원회 결정이 없으면 거부(자기 승인 금지)",
       "NO_COMMITTEE_DECISION" in eligibility(E(**base))[1])

    bad = dict(base); bad["confirm_result"] = {"pass": False}
    ok("홀드아웃 실패는 거부",
       "HOLDOUT_NOT_PASSED" in eligibility(E(**bad), committee_decision_id=_CD)[1])

    bad = dict(base); bad["sessions_used"] = ["2026-07-01"]
    ok("홀드아웃을 안 썼으면 거부",
       "HOLDOUT_NOT_USED" in eligibility(E(**bad), committee_decision_id=_CD)[1])

    bad = dict(base); bad["candidate"] = {**good_cand, "params": {"h": 999}}
    ok("사양이 바뀌었으면 거부(사전등록 위반)",
       "PREREG_FINGERPRINT_MISMATCH" in eligibility(E(**bad), committee_decision_id=_CD)[1])

    bad = dict(base); bad["kind"] = "measure"
    ok("탐색 성적만으로는 승격 불가",
       "NOT_A_CONFIRMATION" in eligibility(E(**bad), committee_decision_id=_CD)[1])

    bad = dict(base); bad["candidate"] = {}
    ok("후보 사양이 없으면 거부",
       "NO_CANDIDATE_SPEC" in eligibility(E(**bad), committee_decision_id=_CD)[1])

    ok("같은 사양은 같은 지문",
       rlog.prereg_fingerprint(dict(reversed(list(good_cand.items()))))== fp)
    ok("사양이 바뀌면 지문도 바뀐다",
       rlog.prereg_fingerprint({**good_cand, "params": {"h": 61}}) != fp)

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--self-check", action="store_true")
    m.add_argument("--list", action="store_true")
    m.add_argument("--promote", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--committee", default="", dest="committee",
                   help="governance.committee_decisions 의 결정 id "
                        "(승격에 필수 - 다리는 스스로 승인하지 않는다)")
    p.add_argument("--deploy", action="store_true",
                   help="컨테이너까지 기동한다(기본은 명령만 출력)")
    a = p.parse_args(argv)

    if a.self_check:
        return _selfcheck()
    if a.list:
        rows = rlog.confirmed_passing()
        print(f"승격 대기 확증 {len(rows)}건")
        for e in rows:
            good, why = eligibility(e, committee_decision_id="(확인필요)")
            print(f"  {e.id} {'자격O' if good else '자격X ' + ','.join(why)}"
                  f"  {str((e.candidate or {}).get('claim'))[:60]}")
        return 0

    r = promote(a.promote, dry_run=a.dry_run, deploy=a.deploy,
                committee_decision_id=a.committee)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
