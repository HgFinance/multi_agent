#!/usr/bin/env python3
"""**막힘 신고가 어디로도 안 가는 것**을 재현하는 검사. (2026-08-14)

▶ 무엇을 재현하는가 (보드 실측, 추정 아님)
  `공장 개선: 관측된 병목 N건` 카드가 이틀 동안 **96장** 발행됐다. 매 주기
  2장(리서치·퀀트)씩, 내용은 사실상 같다. 그중 다수가 `blocked` 로 돌아왔고
  `block_kind=capability` 인 것만 리서치 7건이다. 그런데도 다음 주기가 같은
  카드를 또 걸었다 - 이 카드(t_03687bd5)를 붙들고 있는 동안 이미 다음 주기
  카드(t_2bbfa8d3)가 보드에 올라와 있었다.

  즉 에이전트는 **못 하겠다고 말했고, 아무도 듣지 않았다.**

▶ 결함은 둘이다. 둘 다 이 검사가 잡는다.
  D1 재발행 - `_issue_bottleneck_cards` 의 idempotency-key 가
     `factory-bottleneck-{owner}-{stamp}` 다. stamp 가 매 주기 바뀌므로 키가
     **절대 안 겹친다.** 같은 주기에 두 번 도는 것만 막고, 주기를 넘는 중복은
     하나도 못 막는다. 앞 카드가 `blocked` 로 서 있어도 새 카드가 나간다.
  D2 신고 유실 - `_capability_blocks` 는 `tasks.block_kind` 와
     `block_recurrences` 만 읽는다. 정작 **무엇이 없어서 못 했는지**는
     `task_events.payload.reason` 에 들어 있는데 아무도 안 읽는다. 그래서
     카드에는 "무엇이 없는지 한 줄로 적어라" 는 지시만 반복되고, 이미 열두 번
     적어 낸 답("정본 소스에 쓰기 권한이 없다")은 매번 버려진다.

실행: python probe_capability_recurrence.py
고치기 전 = 빨강(F2P), 고친 뒤 = 초록.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bottleneck_census as bc                                    # noqa: E402
import factory_autopilot as fa                                    # noqa: E402

# 보드에서 실제로 읽어 온 신고 원문(t_813944f2). 지어낸 문장이 아니다.
REAL_REASON = (
    "반복 capability 병목을 findings.md에 재현·분류했지만, 실제 해소에는 Quant "
    "canonical 실행면의 probe import-path와 proposal gate의 persisted "
    "planner/skeptic provenance 계약 수정 권한이 필요합니다. 이 프로필은 해당 "
    "read-only 저장소를 수정할 수 없으므로, 현재는 검증 가능한 진단 산출물까지만 "
    "완료했습니다.")


def _payload(reason: str, kind: str = "capability") -> str:
    return json.dumps({"reason": reason, "kind": kind, "recurrences": 1},
                      ensure_ascii=False)


def check_report_text_reaches_the_card() -> None:
    """D2. **신고 원문이 다음 주기 카드에 실린다.**

    같은 사유로 세 번 막혔으면 카드는 그 사유를 **글자로** 실어야 한다.
    제목만 싣고 "무엇이 없는지 적어라" 고 되물으면, 이미 세 번 적어 낸 답을
    세 번 버린 것이다.
    """
    rows = [(tid, "research-department", "공장 개선: 관측된 병목 16건",
             "capability", 1, _payload(REAL_REASON))
            for tid in ("t_813944f2", "t_6e4d2a91", "t_c93d7575")]

    saved = fa._board_rows
    try:
        fa._board_rows = lambda sql, params=(): rows
        got = bc._capability_blocks(None)
    finally:
        fa._board_rows = saved

    assert len(got) == 1, f"같은 부서·같은 종류는 한 줄로 묶여야 한다: {got}"
    b = got[0]
    text = b.evidence + " " + b.hint
    assert ("쓰기 권한" in text or "read-only" in text
            or "수정할 수 없" in text), (
        "신고 원문이 카드에 안 실린다 - 에이전트는 무엇이 없는지 이미 적어 냈는데 "
        "공장은 그 문장을 안 읽고 매 주기 같은 질문을 되풀이한다.\n"
        f"  지금 실리는 것: {b.evidence}\n  힌트: {b.hint[:80]}")
    print("  신고 원문이 카드에 실림    OK")


def check_repeated_report_is_counted() -> None:
    """D2-b. **같은 사유가 몇 번 되풀이됐는지 세어 말한다.**

    한 번 못 하겠다는 것과 열두 번 못 하겠다는 것은 다른 신호다. 세지 않으면
    구조적 수요가 일회성 투정으로 읽힌다.
    """
    rows = [(tid, "research-department", "공장 개선", "capability", 1,
             _payload(REAL_REASON))
            for tid in ("t_a", "t_b", "t_c")]
    rows.append(("t_d", "research-department", "공장 개선", "capability", 1,
                 _payload("전혀 다른 사유: 시세 DB 접속 정보가 없다")))

    saved = fa._board_rows
    try:
        fa._board_rows = lambda sql, params=(): rows
        got = bc._capability_blocks(None)
    finally:
        fa._board_rows = saved

    assert len(got) == 1, got
    ev = got[0].evidence
    assert "3" in ev, f"같은 사유 3회를 안 셌다: {ev}"
    print("  되풀이 횟수를 셈           OK")


def check_unresolved_card_is_not_reminted() -> None:
    """D1. **앞 카드가 아직 안 끝났으면 같은 카드를 또 걸지 않는다.**

    실측: 같은 제목의 개선 카드가 96장. 앞 카드가 `blocked`/`ready` 로 서 있는
    동안에도 다음 주기가 새 카드를 만들었다. 중복 카드는 같은 실험을 두 번
    사는 것과 같다 - `_create_card` 의 docstring 이 스스로 그렇게 적어 놓고도
    키에 stamp 를 넣어 그 성질을 스스로 무력화했다.
    """
    group = [bc.Bottleneck(
        kind="막힘: 역량 부족(재라우팅 후보)",
        key="cap:research-department:capability",
        cost=7.0, unit="건", evidence="research-department 가 카드 7건을 막았다",
        owner="research-department", hint="무엇이 없는지 적어라",
        ids=["t_f7c959c7"])]

    made: list[str] = []
    saved_create, saved_rows = fa._create_card, fa._board_rows
    saved_census = bc.census
    try:
        fa._create_card = lambda **kw: (made.append(kw["key"]), "t_new")[1]
        bc.census = lambda conn=None: list(group)
        scope = fa._scope_key(group)
        # 보드에는 **같은 범위의 아직 안 끝난 카드**가 이미 있다
        fa._board_rows = lambda sql, params=(): [
            ("t_813944f2", "blocked",
             f"factory-bottleneck-research-department-{scope}-20260813T05")]
        fa._issue_bottleneck_cards(stamp="20260814T09", dry_run=False)
    finally:
        fa._create_card, fa._board_rows = saved_create, saved_rows
        bc.census = saved_census

    assert not made, (
        "앞 카드가 안 끝났는데 같은 범위의 카드를 또 걸었다 - 이것이 96장을 만든 "
        f"고리다. 새로 건 키: {made}")
    print("  안 끝난 카드를 또 안 검     OK")


def check_resolved_card_is_reissued() -> None:
    """**반대 방향도 지킨다.** 앞 카드가 끝났는데 병목이 그대로면 다시 건다.

    억제가 지나치면 "겉만 덮은" 것이 조용해진다 - 그건 이 카드가 금지한 바로
    그 실패다. 끝난 카드는 억제 근거가 못 된다.
    """
    group = [bc.Bottleneck(
        kind="막힘: 역량 부족(재라우팅 후보)",
        key="cap:research-department:capability",
        cost=7.0, unit="건", evidence="아직 그대로다",
        owner="research-department", hint="적어라", ids=[])]

    made: list[str] = []
    saved_create, saved_rows = fa._create_card, fa._board_rows
    saved_census = bc.census
    try:
        fa._create_card = lambda **kw: (made.append(kw["key"]), "t_new")[1]
        bc.census = lambda conn=None: list(group)
        fa._board_rows = lambda sql, params=(): []      # 안 끝난 카드가 없다
        fa._issue_bottleneck_cards(stamp="20260814T09", dry_run=False)
    finally:
        fa._create_card, fa._board_rows = saved_create, saved_rows
        bc.census = saved_census

    assert len(made) == 1, f"병목이 남았는데 카드를 안 걸었다: {made}"
    print("  끝난 뒤엔 다시 검          OK")


def check_scope_changes_when_bottlenecks_change() -> None:
    """**범위가 달라지면 다른 카드다.** 억제가 새 병목까지 삼키면 안 된다."""
    a = [bc.Bottleneck(kind="k", key="cap:x", cost=1.0, unit="건", evidence="",
                       owner="research-department", hint="")]
    b = [bc.Bottleneck(kind="k", key="gate:fragility", cost=1.0, unit="건",
                       evidence="", owner="research-department", hint="")]
    assert fa._scope_key(a) != fa._scope_key(b), "다른 병목이 같은 범위로 묶였다"
    assert fa._scope_key(a) == fa._scope_key(list(a)), "같은 병목이 안 겹친다"
    print("  범위가 병목을 따라감       OK")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("막힘 신고 회수 검사 (보드·DB 없음)")
    fails = 0
    for fn in (check_report_text_reaches_the_card,
               check_repeated_report_is_counted,
               check_unresolved_card_is_not_reminted,
               check_resolved_card_is_reissued,
               check_scope_changes_when_bottlenecks_change):
        try:
            fn()
        except AssertionError as exc:
            fails += 1
            print(f"  !! {fn.__name__}\n     {exc}")
        except AttributeError as exc:
            fails += 1
            print(f"  !! {fn.__name__} - 아직 없는 손잡이: {exc}")
        except (ValueError, TypeError) as exc:
            # 신고 원문 칸을 아예 못 받는 상태도 빨강이다 - 죽지 말고 세고 넘어간다
            fails += 1
            print(f"  !! {fn.__name__} - 모양이 안 맞는다: "
                  f"{type(exc).__name__}: {exc}")
    print(f"\n{'통과' if not fails else str(fails) + '건 빨강'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
