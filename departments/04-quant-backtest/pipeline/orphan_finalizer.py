"""판정 없는 종결 실험(고아)의 완주 - 환류 누락을 구조적으로 없앤다.

왜
  실행부(backtest_runner)는 COMPLETED 를 자기 트랜잭션으로 커밋하고, 판정과
  환류는 그 **뒤에** 오케스트레이터가 붙인다. 그 사이에서 죽으면(풀러
  statement_timeout·워커 재시작·구버전 경로) 실험은 COMPLETED 인 채 환류가
  영영 없다. 그리고 중복 가드는 그 실험을 "이미 있다" 며 재실행도 막으므로
  **어떤 경로도 다시 판정하지 않는다** - 2026-08-13 병목 카드가 이렇게 쌓인
  21건을 세었다(TESTING 정체 7건의 원인도 이것이었다).

무엇
  backtest_runner 의 좀비 회수(2026-08-13)와 같은 원칙을 판정에 적용한다:
  **판정이 없는 완료 실험을 판정하는 것은 재판정이 아니라 완주다.**
  판정이 이미 있는 실험은 불가침이다 - 재판정은 결과 조작 여지가 있다.

어떻게 - 저장된 것만 읽는다. 다시 돌리지 않는다:
  - 강건성: WALK_FORWARD 창별 지표 -> walk_forward.fragility_summary (같은 함수)
  - 관문:  release_gate.SQL_GATE_METRICS -> metrics_from_rows -> evaluate (같은 함수)
  - 시도 압력: 실험 행에 **각인된** trial_family_id/trial_number (재계산 금지 -
    시도 압력은 기록된 사실이어야 한다, experiment_orchestrator 의 계약과 동일)
  - 환류: factory_bridge.build_outcome + finalize (적재와 전이 한 트랜잭션)

  가설 상태는 **비종결(TESTING/RUNNING/PROPOSED/...)일 때만** 전이한다.
  이미 REJECTED/ARCHIVED 로 끝난 가설의 역사는 고쳐 쓰지 않는다 - 그 경우
  환류 행만 남기고 상태는 그대로 둔다(교훈은 Gate 0 에 닿아야 하지만
  워크플로 역사는 판정 재료가 아니다). 같은 이유로 종결된 가설의 실험이
  ROBUST 로 나와도 SUBMIT_TO_QA 로 적지 않는다 - 제출은 산 가설만 한다.

실행: python orphan_finalizer.py            자체 점검 (DB 없음)
      python orphan_finalizer.py --assert-zero   원장 검사 - 고아 있으면 exit 1 (F2P 검사)
      python orphan_finalizer.py --run           원장 고아 전부 완주
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FINALIZER_VERSION = "quant-orphan-finalizer-v1"

# 이 상태의 가설만 전이한다. 나머지(REJECTED/ARCHIVED/SUPPORTED/...)는 종결
# 역사이므로 환류만 적재하고 상태는 보존한다.
NON_TERMINAL = frozenset({"INTAKE", "PROPOSED", "PREREGISTERED", "TESTING",
                          "RUNNING", "DATASET_CERTIFIED"})

# 고아 술어. census(_feedback_gap)·soundness(정상종결)와 같은 anti-join 에
# **정착 유예 30분**을 더한 것이다. 유예가 없으면 방금 COMPLETED 된 실험을
# 라이브 판정이 붙기 전에 소탕이 낚아채 판정이 둘 생긴다 - 2026-08-13 실측:
# e820053a 가 소탕 GATE_HOLD(17:21:24) 와 라이브 REJECT(17:22:53) 를 둘 다
# 받았다. 고아는 정의상 며칠 방치된 것이라 유예 30분은 검출을 못 늦춘다.
_SQL_ORPHANS = """
    select e.experiment_id::text
      from quant.experiments e
      left join research.experiment_outcomes o
             on o.experiment_id = e.experiment_id::text
     where e.status = 'COMPLETED' and o.outcome_id is null
       and e.ended_at < now() - interval '30 minutes'
     order by e.ended_at
"""

_SQL_EXPERIMENT = """
    select e.hypothesis_id::text, e.trial_family_id, e.trial_number,
           h.status
      from quant.experiments e
      left join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
     where e.experiment_id = %s
"""

# 창별 강건성 재료. SUMMARY/빈 차원은 전기간 요약이라 창이 아니다.
_SQL_WINDOWS = """
    select dimensions->>'window', metric, value
      from quant.experiment_metrics
     where experiment_id = %s and split = 'WALK_FORWARD'
       and coalesce(dimensions->>'window', '') not in ('', 'SUMMARY')
"""

# 환류 oos_summary 재료. _finalize_with_feedback 과 같은 술어를 쓴다.
_SQL_SUMMARY_METRICS = """
    select metric, value from quant.experiment_metrics
     where experiment_id = %s
       and coalesce(dimensions->>'window','') in ('','SUMMARY')
       and dimensions->>'regime' is null
"""

_WINDOW_KEYS = ("total_return", "sharpe_rf0", "max_drawdown")


def windows_from_rows(rows) -> tuple[list[tuple[str, dict]], list[str]]:
    """(window, metric, value) 행 -> fragility_summary 가 먹는 창 목록.

    세 핵심 지표가 다 없는 창은 판정 재료가 아니다 - 조용히 넣으면
    KeyError 로 죽거나(과거 실측) 반쪽 창이 판정을 흔든다. 뺀 창은
    이름을 돌려줘서 호출부가 적게 한다(조용한 절단 금지).
    """
    by_win: dict[str, dict] = {}
    for win, metric, value in rows or ():
        if win is None:
            continue
        try:
            by_win.setdefault(str(win), {})[str(metric)] = float(value)
        except (TypeError, ValueError):
            continue
    wm, dropped = [], []
    for label in sorted(by_win):
        m = by_win[label]
        if all(k in m for k in _WINDOW_KEYS):
            wm.append((label, m))
        else:
            dropped.append(label)
    return wm, dropped


def oos_from_rows(rows) -> dict:
    """전기간 지표 행 -> 환류 oos_summary. 이름 사상은 관문이 소유한다."""
    from experiment_orchestrator import _OOS_KEYS
    from release_gate import STORED_ALIASES

    found: dict[str, float] = {}
    for r in rows or ():
        if isinstance(r, (list, tuple)) and len(r) == 2:
            try:
                found[str(r[0])] = float(r[1])
            except (TypeError, ValueError):
                continue
    for want, (stored, scale) in STORED_ALIASES.items():
        if want not in found and stored in found:
            found[want] = found[stored] * scale
    oos = {k: found[k] for k in _OOS_KEYS if k in found}
    for src, dst in (("bootstrap_ci_low", "ci_low"),
                     ("bootstrap_ci_high", "ci_high")):
        if src in oos:
            oos[dst] = oos.pop(src)
    return oos


def judge_from_stored(*, exp_id: str, hypothesis_id: str, hyp_status: str,
                      trial_family_id: str | None, trial_number: int | None,
                      window_rows, summary_rows, gate_rows) -> dict:
    """(순수 함수) 저장된 지표 -> 판정·환류 재료. DB 를 만지지 않는다.

    반환: {new_status, status_to_set, decision, outcome(dict), note}
    """
    from experiment_orchestrator import (
        TRIAL_BUDGET_DEFAULT,
        _STATUS_TO_DECISION,
        _gate_note,
        robustness_to_status,
    )
    from factory_bridge import build_outcome, lessons_from
    from release_gate import evaluate as gate_evaluate
    from release_gate import metrics_from_rows
    from walk_forward import fragility_summary

    wm, dropped = windows_from_rows(window_rows)
    _stats, _flags, verdict = fragility_summary(wm)

    trial_no = int(trial_number or 1)
    pressure = {
        "trial_family_id": str(trial_family_id or ""),
        "trial_number": trial_no,
        # 예산 초과는 각인된 순번으로 판단한다. 이 실험들이 돌던 시점의
        # 사실이고, 지금 계열을 재계산하면 소급 변경이 된다.
        "over_budget": trial_no > TRIAL_BUDGET_DEFAULT,
    }

    new_status = robustness_to_status(verdict, pressure)

    metrics = metrics_from_rows(gate_rows or [])
    gate = gate_evaluate(metrics, fragility=verdict, trial_pressure=pressure)
    # 못 잰 조항은 교훈 재료가 아니다 - 라이브 경로와 같은 필터.
    unmeasured = set(gate.unmeasured or ())
    gate_failed = [str(x) for x in (gate.failed or []) if x not in unmeasured]

    failed: list[str] = []
    if new_status == "REJECTED" and verdict:
        failed.append(f"fragility_{verdict.lower()}")
    for g in gate_failed:
        if g not in failed:
            failed.append(g)
    if pressure["over_budget"] and "trial_pressure" not in failed:
        # 예산 초과가 GATE_HOLD 의 이유라면 그 사실이 failed 에 있어야
        # 다음 기획안이 "왜 보류됐나" 를 읽는다(사유 없는 보류 금지).
        failed.append("trial_pressure")

    oos = oos_from_rows(summary_rows)
    lessons = lessons_from(failed_criteria=failed, regime_concerns=(),
                           fragility=verdict, oos_summary=oos)

    terminal = hyp_status not in NON_TERMINAL
    decision = _STATUS_TO_DECISION.get(new_status, "GATE_HOLD")
    note_bits = [f"사후 완주({FINALIZER_VERSION}): COMPLETED 인데 환류 0건 - "
                 f"저장된 지표로 판정 재구성(재실행 없음), 창 {len(wm)}개"]
    if dropped:
        note_bits.append(f"핵심 지표 미비로 제외한 창 {len(dropped)}개")
    if decision == "SUBMIT_TO_QA" and terminal:
        # 제출은 산 가설만 한다 - 종결된 가설의 지지 증거는 기록만 남긴다.
        decision = "GATE_HOLD"
        note_bits.append(f"지지 증거이나 가설 이미 {hyp_status} - 제출 없음")
        if not (failed or lessons):
            failed = ["hypothesis_terminal"]
    gn = _gate_note(gate, metrics)
    if gn:
        note_bits.append(gn)
    if terminal:
        note_bits.append(f"가설 상태 {hyp_status} 보존(역사 불변)")

    outcome = build_outcome(
        experiment_id=exp_id, hypothesis_id=hypothesis_id,
        trial_family_id=pressure["trial_family_id"],
        trial_number=trial_no, decision=decision,
        failed_criteria=failed, lesson_codes=lessons, oos_summary=oos,
        notes=" | ".join(note_bits)[:900])
    return {
        "new_status": new_status,
        "status_to_set": hyp_status if terminal else new_status,
        "decision": decision, "outcome": outcome,
        "verdict": verdict, "note": " | ".join(note_bits),
    }


def finalize_one(conn, exp_id: str) -> dict:
    """고아 하나를 완주한다. 판정이 붙어 있으면 손대지 않는다(불가침)."""
    from factory_bridge import finalize

    cur = conn.cursor()
    # 불가침 재확인 - 목록 조회와 완주 사이에 다른 경로가 판정을 붙였을 수 있다.
    cur.execute("select 1 from research.experiment_outcomes "
                "where experiment_id = %s", (exp_id,))
    if cur.fetchone():
        return {"experiment_id": exp_id, "skipped": "already_judged"}

    cur.execute(_SQL_EXPERIMENT, (exp_id,))
    row = cur.fetchone()
    if row is None:
        return {"experiment_id": exp_id, "skipped": "no_experiment_row"}
    hypothesis_id, fam, trial_no, hyp_status = row

    cur.execute(_SQL_WINDOWS, (exp_id,))
    window_rows = cur.fetchall()
    cur.execute(_SQL_SUMMARY_METRICS, (exp_id,))
    summary_rows = cur.fetchall()
    from release_gate import SQL_GATE_METRICS

    cur.execute(SQL_GATE_METRICS, (exp_id,))
    gate_rows = cur.fetchall()

    j = judge_from_stored(
        exp_id=exp_id, hypothesis_id=str(hypothesis_id),
        hyp_status=str(hyp_status or ""), trial_family_id=fam,
        trial_number=trial_no, window_rows=window_rows,
        summary_rows=summary_rows, gate_rows=gate_rows)

    oid = finalize(conn, hypothesis_id=str(hypothesis_id),
                   new_status=j["status_to_set"], outcome=j["outcome"])
    return {"experiment_id": exp_id, "outcome_id": oid,
            "decision": j["decision"], "verdict": j["verdict"],
            "hypothesis": str(hypothesis_id),
            "status_set": j["status_to_set"]}


def finalize_orphans(conn, *, limit: int | None = None) -> dict:
    """고아 전부 완주. 하나가 죽어도 나머지는 계속 간다(사유는 남긴다)."""
    cur = conn.cursor()
    cur.execute(_SQL_ORPHANS)
    ids = [r[0] for r in cur.fetchall()]
    if limit is not None:
        ids = ids[:limit]
    done, failed = [], []
    for eid in ids:
        try:
            done.append(finalize_one(conn, eid))
        except Exception as e:  # noqa: BLE001 - 한 건의 실패가 소탕을 멈추지 않는다
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            failed.append({"experiment_id": eid,
                           "error": f"{type(e).__name__}: {str(e)[:200]}"})
    return {"checked": len(ids), "finalized": done, "failed": failed}


# ---------------------------------------------------------------------------
# 자체 점검 (DB 없음)
# ---------------------------------------------------------------------------

def _wrows(labels, ret=0.1, sharpe=1.0, mdd=-0.1):
    out = []
    for w in labels:
        out += [(w, "total_return", ret), (w, "sharpe_rf0", sharpe),
                (w, "max_drawdown", mdd)]
    return out


def _check_no_windows_is_inconclusive_not_pass():
    """창이 없으면 판정 불가다 - 통과도 기각도 아니고, 그 사실이 교훈에 남는다."""
    j = judge_from_stored(exp_id="e1", hypothesis_id="h1",
                          hyp_status="ARCHIVED", trial_family_id=None,
                          trial_number=None, window_rows=[], summary_rows=[],
                          gate_rows=[])
    assert j["new_status"] == "INCONCLUSIVE", j
    assert j["decision"] == "GATE_HOLD", j
    assert "UNDERPOWERED_DATA" in j["outcome"]["lesson_codes"], j["outcome"]
    # 종결된 가설의 역사는 고쳐 쓰지 않는다
    assert j["status_to_set"] == "ARCHIVED", j


def _check_fragile_windows_reject_and_transition_live_hypothesis():
    """산 가설(TESTING)은 라이브 의미 그대로 - FRAGILE 이면 REJECTED 로 전이."""
    rows = (_wrows(["w1", "w2"], ret=0.1) +
            _wrows(["w3", "w4"], ret=-0.2, mdd=-0.6))
    j = judge_from_stored(exp_id="e2", hypothesis_id="h2",
                          hyp_status="TESTING", trial_family_id="fam_x",
                          trial_number=2, window_rows=rows, summary_rows=[],
                          gate_rows=[])
    assert j["verdict"] == "FRAGILE" and j["new_status"] == "REJECTED", j
    assert j["status_to_set"] == "REJECTED", j
    assert j["decision"] == "REJECT", j
    # 사유 없는 기각은 성립하지 않는다 - build_outcome 이 이미 강제하지만
    # 여기서도 본다(fragility 가 failed 로 들어가는 배선 확인)
    assert "fragility_fragile" in j["outcome"]["failed_criteria"], j["outcome"]


def _check_over_budget_robust_is_held_with_reason():
    """예산 초과 ROBUST 는 INCONCLUSIVE 보류 - 이유(trial_pressure)가 남는다."""
    j = judge_from_stored(exp_id="e3", hypothesis_id="h3",
                          hyp_status="TESTING", trial_family_id="fam_x",
                          trial_number=7, window_rows=_wrows(["a", "b", "c"]),
                          summary_rows=[], gate_rows=[])
    assert j["verdict"] == "ROBUST" and j["new_status"] == "INCONCLUSIVE", j
    assert j["decision"] == "GATE_HOLD", j
    assert "trial_pressure" in j["outcome"]["failed_criteria"], j["outcome"]


def _check_terminal_hypothesis_never_submitted():
    """종결된 가설의 ROBUST 는 제출이 아니라 기록이다."""
    j = judge_from_stored(exp_id="e4", hypothesis_id="h4",
                          hyp_status="REJECTED", trial_family_id="fam_y",
                          trial_number=1, window_rows=_wrows(["a", "b", "c"]),
                          summary_rows=[], gate_rows=[])
    assert j["new_status"] == "SUPPORTED", j          # 증거 판정은 그대로
    assert j["decision"] == "GATE_HOLD", j            # 제출은 하지 않는다
    assert j["status_to_set"] == "REJECTED", j        # 역사 보존
    assert "제출 없음" in j["outcome"]["notes"], j["outcome"]["notes"]


def _check_half_windows_are_dropped_not_guessed():
    """핵심 지표가 빠진 창은 재료가 아니다 - 세지 않고, 뺐다고 적는다."""
    rows = _wrows(["w1", "w2", "w3"]) + [("w4", "total_return", 0.5)]
    wm, dropped = windows_from_rows(rows)
    assert [l for l, _ in wm] == ["w1", "w2", "w3"], wm
    assert dropped == ["w4"], dropped


class _JudgedCur:
    """판정이 이미 붙은 실험을 돌려주는 가짜 커서."""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())

    def fetchone(self):
        return (1,) if "experiment_outcomes" in self.sql else None

    def fetchall(self):
        return []


def _check_judged_experiment_is_untouchable():
    """판정이 붙은 실험은 불가침이다 - 재판정은 결과 조작 여지다."""
    class _C:
        def cursor(self):
            return _JudgedCur()

    r = finalize_one(_C(), "e9")
    assert r == {"experiment_id": "e9", "skipped": "already_judged"}, r


def _check_orphan_predicate_matches_census():
    """고아 술어가 census(_feedback_gap)와 같은 anti-join 인지 문자 그대로 본다.

    둘이 갈라지면 여기서 없앤 것을 census 가 계속 센다 - 없앴는데 남는 카드.
    허용된 차이는 정착 유예 하나뿐이다(라이브 판정과의 경합 방지) - census 는
    전부 세고, 소탕은 30분 기다렸다 움직인다.
    """
    for frag in ("e.status = 'COMPLETED'",
                 "left join research.experiment_outcomes",
                 "o.experiment_id = e.experiment_id::text",
                 "o.outcome_id is null",
                 "e.ended_at < now() - interval '30 minutes'"):
        assert frag in " ".join(_SQL_ORPHANS.split()), frag


def _selfcheck() -> None:
    print(f"{FINALIZER_VERSION} 자체 점검 (DB 없음)")
    _check_no_windows_is_inconclusive_not_pass()
    print("  창 0개 -> 판정불가 보류      OK")
    _check_fragile_windows_reject_and_transition_live_hypothesis()
    print("  FRAGILE -> 기각+전이(산 가설) OK")
    _check_over_budget_robust_is_held_with_reason()
    print("  예산초과 ROBUST -> 사유 보류  OK")
    _check_terminal_hypothesis_never_submitted()
    print("  종결 가설은 제출 금지         OK")
    _check_half_windows_are_dropped_not_guessed()
    print("  반쪽 창은 제외+기록           OK")
    _check_judged_experiment_is_untouchable()
    print("  판정 붙은 실험 불가침         OK")
    _check_orphan_predicate_matches_census()
    print("  고아 술어 = census 술어       OK")
    print("자체 점검 전부 통과")


def _db_conn():
    import psycopg2

    # source_registry 는 리서치 수집기 곁에 산다 - 오케스트레이터와 같은 경로 규칙
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env

    return psycopg2.connect(load_project_env()["DATABASE_URL"],
                            connect_timeout=20)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--assert-zero" in sys.argv:
        # F2P 검사: 원장에 고아가 하나라도 있으면 빨강(exit 1).
        conn = _db_conn()
        try:
            cur = conn.cursor()
            for attempt in range(4):
                try:
                    cur.execute(_SQL_ORPHANS)
                    break
                except Exception:  # noqa: BLE001 - 풀러 타임아웃 재시도
                    conn.rollback()
                    if attempt == 3:
                        raise
                    time.sleep(1.5)
            ids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        print(f"고아(COMPLETED·환류 0건): {len(ids)}건")
        for i in ids:
            print(f"  {i}")
        raise SystemExit(1 if ids else 0)

    if "--run" in sys.argv:
        conn = _db_conn()
        try:
            r = finalize_orphans(conn)
        finally:
            conn.close()
        print(f"검사 {r['checked']}건 / 완주 {len(r['finalized'])}건 / "
              f"실패 {len(r['failed'])}건")
        for d in r["finalized"]:
            print(f"  완주 {d['experiment_id'][:8]} -> {d['decision']:12} "
                  f"(강건성 {d['verdict']}, 가설 {d['hypothesis'][:8]} "
                  f"-> {d['status_set']})")
        for f in r["failed"]:
            print(f"  실패 {f['experiment_id'][:8]}: {f['error']}")
        raise SystemExit(2 if r["failed"] else 0)

    _selfcheck()
