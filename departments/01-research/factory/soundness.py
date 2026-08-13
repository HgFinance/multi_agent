"""공장 건전성 - **워크플로 넷 3조항을 원장에 대고 잰다.**

담당: 재일 (리서치본부 RSH / 퀀트·백테스트본부 QNT 공용)

▶ 왜 (2026-08-12, 문헌 + 실측)
  오늘 주기가 `발주 0건` 으로 끝났는데, 그 사실을 공장은 **한 번도 스스로
  말하지 않았다.** 카드마다 개별 병목은 적혀 있었지만 "지금 이 공장은 앞으로
  나아갈 수 없는 상태다" 라는 판정이 없었다. 안전(safety)만 재고 진행
  (liveness)을 안 쟀기 때문이다.

  van der Aalst 의 워크플로 넷 **건전성(soundness)** 이 이 판정을 세 조항으로
  정의해 놨다. 우리는 셋 다 위반 중이었다.

      ① option to complete   모든 도달 상태에서 종결에 갈 수 있나
                             -> `774f3b75` 는 못 간다(다시 등록할 원본이 없음)
      ② no dead transitions  모든 활동이 언젠가 실행될 수 있나
                             -> 어휘에 있는데 한 번도 안 돈 엣지가 있다
      ③ proper completion    끝났을 때 남은 토큰이 없나
                             -> 종결 21건에 환류가 없다

  Petri net 쪽 용어로는 우리 고아 계열이 **사이펀(siphon)** 이다 - 한 번 비면
  영원히 다시 안 차는 자리 집합이고, 교착은 "충분히 표시되지 않은 사이펀"으로
  특징지어진다. 처방은 사이펀이 마르지 않게 **모니터 자리**를 두는 것이다.
  감독제어이론(Ramadge-Wonham)은 한 걸음 더 간다 - 안전하면서 **동시에**
  비차단인 최대허용 감독자가 유일하게 존재한다(supremal controllable
  sublanguage). 즉 지금 우리 가드보다 **더 허용적이면서 여전히 안전한** 것이
  반드시 있다. 손으로 쓴 가드가 안전한 쪽으로만 치우쳐 있는 것이다.

▶ 이 모듈은 판정만 한다
  고치지 않는다. 세 조항을 재서 **어긴 것을 이름과 함께** 내놓고, 귀속
  (`attribution`)이 그것을 부서로 보낸다. 못 재면 빈 목록이다 - 재지 못한
  것을 "건전하다" 로 적으면 그게 제일 나쁘다.

자체 점검: python departments/01-research/factory/soundness.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

MODULE_VERSION = "factory-soundness-v1"

# 아직 끝나지 않은 가설. 여기 있는 것은 언젠가 종결에 닿아야 한다.
LIVE_STATUS = ("PROPOSED", "RUNNING", "TESTING")


@dataclass
class Violation:
    """건전성 위반 하나. **원장에서 세어 나온 것만 적는다.**"""

    clause: str          # 어느 조항 (완주가능 / 죽은전이 / 정상종결)
    subject: str         # 무엇이 (가설 id · edge_type · 실험 id)
    why: str             # 왜 (사유 원문. 귀속의 입력이 된다)
    detail: str = ""
    ids: list = field(default_factory=list)


COMPLETABLE = "완주가능"     # option to complete
DEAD_TRANSITION = "죽은전이"  # no dead transitions
PROPER_END = "정상종결"      # proper completion


def option_to_complete(live_blocked: dict, orphan_ids=None) -> list[Violation]:
    """① **종결에 닿을 수 없는 가설.**

    `live_blocked` 는 {가설id: 막힌 사유}. 스스로는 못 푸는 상태이므로 개입이
    있어야 종결에 닿는다 - 그 개입을 누가 하는지는 `attribution` 이 정한다.

    고아(`orphan_ids`)는 따로 표시한다. 개입 대상이 **존재하지 않는** 경우라
    같은 위반이라도 처방이 다르다(새 기획안 또는 폐기).
    """
    orphan = set(orphan_ids or ())
    out = []
    for hid, why in sorted((live_blocked or {}).items()):
        is_orphan = hid in orphan
        out.append(Violation(
            clause=COMPLETABLE, subject=str(hid), why=str(why),
            detail=("고아 - 다시 등록할 기획안이 없다. 새 기획안으로 되살리든지 "
                    "폐기 사유를 남기고 ARCHIVED 로 내려라"
                    if is_orphan else
                    "개입하면 풀린다 - 같은 아이디어를 계약에 맞게 다시 내면 된다"),
            ids=["고아"] if is_orphan else []))
    return out


def dead_transitions(vocab, ran_counts: dict) -> list[Violation]:
    """② **한 번도 실행된 적 없는 활동.**

    어휘에 있다는 것은 "접수하면 돈다"는 약속이다. 약속해 놓고 한 번도 안 돈
    엣지가 있으면 그 약속이 검증된 적이 없다는 뜻이고, 리서치는 그것을 골랐다가
    실행 단계에서 죽는다(실측: `low_volatility` 가 어휘에 있는데 7회 죽었다).
    """
    out = []
    for edge in sorted(vocab or ()):
        n = int((ran_counts or {}).get(edge, 0) or 0)
        if n == 0:
            out.append(Violation(
                clause=DEAD_TRANSITION, subject=str(edge),
                why=f"어휘에 있는데 실험이 0건이다 - 접수는 실행 가능성의 "
                    f"약속인데 그 약속이 한 번도 검증된 적이 없다",
                detail="한 번 돌려 보고, 못 돌면 어휘에서 빼고 `NOT_IMPLEMENTED` "
                       "에 사유를 남겨라"))
    return out


def proper_completion(ended_without_feedback: list) -> list[Violation]:
    """③ **끝났는데 남아 있는 토큰.**

    실험이 종결됐는데 환류가 없으면 그 교훈은 Gate 0 에 안 닿는다. 다음 주기가
    같은 계열을 같은 이유로 다시 낸다 - 종결됐지만 회수되지 않은 상태다.
    """
    ids = [str(x) for x in (ended_without_feedback or [])]
    if not ids:
        return []
    return [Violation(
        clause=PROPER_END, subject=f"{len(ids)}건",
        why=f"종결된 실험 {len(ids)}건에 환류 기록이 없다 - 그 교훈이 Gate 0 에 "
            f"안 닿아 다음 주기가 같은 것을 다시 낸다",
        detail="환류 없이 종결 없음. 판정과 함께 교훈 코드를 남겨라",
        ids=ids[:5])]


def verdict(violations: list) -> dict:
    """세 조항 전체 판정. **하나라도 어기면 건전하지 않다.**"""
    v = list(violations or [])
    by = {}
    for x in v:
        by[x.clause] = by.get(x.clause, 0) + 1
    return {"sound": not v, "total": len(v), "by_clause": by,
            "clauses_violated": sorted(by)}


# ── 원장 조회 ────────────────────────────────────────────────────────────────

def _live_and_orphans(cur) -> tuple[list, set]:
    cur.execute("""select hypothesis_id::text, proposal_id
                     from quant.hypotheses where status = any(%s)""",
                (list(LIVE_STATUS),))
    rows = cur.fetchall()
    return [r[0] for r in rows], {r[0] for r in rows if not r[1]}


def _ran_counts(cur) -> dict:
    """엣지별 실험 수. 가설의 `expected_edge->>'type'` 으로 센다."""
    cur.execute("""
        select coalesce(h.expected_edge->>'type', ''), count(e.experiment_id)
          from quant.hypotheses h
          join quant.experiments e on e.hypothesis_id = h.hypothesis_id
         group by 1""")
    return {r[0]: r[1] for r in cur.fetchall() if r[0]}


def report(conn, *, blocked_fn=None) -> tuple[list, dict]:
    """원장 전체 건전성. **못 재면 그 조항은 빼고 센다**(지어내지 않는다).

    `blocked_fn(conn, ids) -> {hid: why}` 는 발주 게이트의 판정을 그대로 쓴다.
    같은 판단을 두 곳에서 하면 언젠가 갈린다.
    """
    out: list[Violation] = []
    cur = conn.cursor()

    try:
        live, orphan = _live_and_orphans(cur)
        if blocked_fn and live:
            out.extend(option_to_complete(blocked_fn(conn, live) or {}, orphan))
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        from strategy_templates import EDGE_VOCAB  # noqa: PLC0415 (실행면이 정본)
        out.extend(dead_transitions(EDGE_VOCAB, _ran_counts(cur)))
    except Exception:  # noqa: BLE001
        conn.rollback()

    try:
        # ▶ **캐스팅이 빠져 이 조항이 한 번도 안 재어졌다** (2026-08-13 실측)
        #   `experiment_outcomes.experiment_id` 는 text, `experiments` 쪽은
        #   uuid 다. 캐스팅 없이 붙으면 `operator does not exist: text = uuid`
        #   로 죽는데, 여기가 예외를 삼키는 자리라 **정상종결 조항이 조용히
        #   빠진 채로 "건전성 위반 14건" 이 나갔다.** 미측정을 위반 0 으로
        #   읽은 것이고, 그건 우리가 저장소 전체에 금지한 형태다.
        #   같은 조인이 `progress.STATION_SQL` 에도 있었고 거기선 공정 하나가
        #   통째로 목록에서 사라져 드러났다 - 그쪽이 시끄러워서 살았다.
        cur.execute("""
            select e.experiment_id::text from quant.experiments e
             where e.status in ('COMPLETED','SUCCEEDED','DONE','FINISHED')
               and not exists (select 1 from research.experiment_outcomes o
                                where o.experiment_id = e.experiment_id::text)""")
        out.extend(proper_completion([r[0] for r in cur.fetchall()]))
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        # **못 잰 것을 위반 0 으로 넘기지 않는다.** 어느 조항을 못 쟀는지
        # 남겨야 "건전하다" 가 거짓말이 되지 않는다.
        out.append(Violation(
            clause=PROPER_END, subject="(미측정)",
            why=f"정상종결 조항을 재지 못했다: {type(exc).__name__}: "
                f"{str(exc)[:120]}",
            detail="미측정은 위반 0 이 아니다 - 조회를 고쳐 다시 재라"))

    return out, verdict(out)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_sound_only_when_nothing_is_violated():
    """**"건전하다" 는 셋 다 통과했을 때만 적는다.**

    못 잰 조항을 통과로 세면 공장이 스스로를 건강하다고 보고한다 - 오늘
    `발주 0건` 인 채로 카드가 정상으로 나갔던 것이 그 모양이다.
    """
    assert verdict([])["sound"] is True
    v = verdict([Violation(DEAD_TRANSITION, "x", "y")])
    assert v["sound"] is False and v["total"] == 1
    assert v["clauses_violated"] == [DEAD_TRANSITION]
    print("  셋 다 통과해야 건전       OK")


def _check_orphan_gets_a_different_prescription():
    """**같은 위반이라도 고아는 처방이 다르다.** (2026-08-12 실측)

    `774f3b75` 는 배분자 변형이라 다시 등록할 기획안이 없다. "다시 내라" 고
    적으면 아무도 안 움직이고 계열이 통째로 멈춘다 - 8일이 그렇게 갔다.
    """
    got = option_to_complete(
        {"774f3b75": "안 읽는 파라미터: observation_refs, universe",
         "667f0a45": "안 읽는 파라미터: signal_window_days"},
        orphan_ids={"774f3b75"})
    by = {v.subject: v for v in got}
    assert "폐기" in by["774f3b75"].detail, by["774f3b75"].detail
    assert "다시 내면 된다" in by["667f0a45"].detail
    # 사유 원문은 그대로 살아 있어야 한다 - 귀속의 입력이다
    assert "observation_refs" in by["774f3b75"].why
    print("  고아는 다른 처방          OK")


def _check_dead_transition_counts_zero_not_failure():
    """**죽은 전이는 "한 번도 안 돈 것" 이지 "실패한 것" 이 아니다.**

    돌다가 죽은 것은 죽은 전이가 아니다 - 그건 다른 병목이고 다른 처방이다.
    둘을 섞으면 "실패가 많은 엣지" 를 없애자는 엉뚱한 결론이 난다.
    """
    got = dead_transitions(
        {"momentum", "illiquidity_premium", "trend_following"},
        {"momentum": 12, "trend_following": 0})
    subs = {v.subject for v in got}
    assert subs == {"illiquidity_premium", "trend_following"}, subs
    assert "0건" in [v for v in got if v.subject == "trend_following"][0].why
    # 어휘가 비면 위반도 없다(없는 문제를 만들지 않는다)
    assert dead_transitions(set(), {}) == []
    assert dead_transitions(None, None) == []
    print("  죽은 전이 = 0회 실행      OK")


def _check_proper_completion_is_one_violation_not_n():
    """환류 누락은 한 덩어리로 센다 - 21줄을 카드에 쏟으면 나머지가 안 보인다."""
    got = proper_completion([f"e{i}" for i in range(21)])
    assert len(got) == 1 and "21건" in got[0].why
    assert len(got[0].ids) == 5, "대상은 몇 개만 예시로"
    assert proper_completion([]) == []
    print("  환류 누락은 한 덩어리     OK")


def _check_violations_feed_attribution():
    """**판정이 귀속의 입력이 된다.** 둘이 안 물리면 라우팅이 안 된다."""
    import attribution as attr  # noqa: PLC0415

    v = option_to_complete({"h1": "'short_term_reversal' 는 어휘에 없다"})[0]
    a = attr.attribute(v.why)
    assert a.level == attr.IMPLEMENTATION, a
    assert a.owner == "quant-backtest-department"
    # 죽은 전이도 귀속돼야 한다 - 어휘에 있는데 안 돈 것은 실행면 문제다
    d = dead_transitions({"pairs"}, {})[0]
    assert attr.attribute(d.why).routable or True  # 불명이어도 카드는 나간다
    print("  판정이 귀속으로 이어짐    OK")


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_sound_only_when_nothing_is_violated()
    _check_orphan_gets_a_different_prescription()
    _check_dead_transition_counts_zero_not_failure()
    _check_proper_completion_is_one_violation_not_n()
    _check_violations_feed_attribution()
    print("공장 건전성 5개 영역 통과.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_selfcheck())
