#!/usr/bin/env python3
"""**중복 억제가 실제로는 안 듣는 것**을 재현하는 검사. (2026-08-14, t_2bbfa8d3)

▶ 앞 주기가 무엇을 고쳤나 (t_03687bd5)
  개선 카드 키에 `_scope_key` 범위 지문을 넣어, 같은 범위의 카드가 아직
  `ready/running/blocked` 면 새 카드를 안 걸게 했다. 46시간 동안 시간당 2장씩
  나가던 카드가 그 순간 멈춰야 했다.

▶ 그런데 안 멈췄다 (보드 실측, 추정 아님)
  지문이 붙은 뒤 **48분 만에** 퀀트 카드가 또 나갔다.

    10:12 UTC  t_8699a48f  quant  ...-71f357a631-20260814T10   병목 13건
    11:00 UTC  t_0759eb9f  quant  ...-4240f506a0-20260814T10   병목 14건

  두 카드의 **종류(kind) 집합은 완전히 같다** - 작업 되풀이 실패 / 건전성:
  완주가능 / 리드 사장 / 막힘:역량 부족 / 막힘:입력 대기 / 시세 미관측 /
  시세 정수배 갭. 달라진 것은 그 안의 **개체**뿐이다:

    작업 되풀이 실패 : job:BAD_TRANSITION…  ->  job:OSError Errno 12…
    건전성:완주가능  : sound:max_drawdown_stop:…  ->  sound:order_flow_imbalance:…:고아

▶ 왜 그런가 - 지문의 재료가 틀렸다
  `_scope_key` 는 `Bottleneck.key` 를 해시한다. 그런데 census 가 만드는 key 는
  **개체 수준**이다: `job:{sig}`(실패 사유 서명), `sound:{clause}:{level}`
  (위반 조항), `gate:{crit}`, `lesson:{code}`. 이 중 `job:{sig}` 는 실패
  순위표가 바뀔 때마다 바뀌고, 실패 순위표는 **매 주기 바뀐다.**

  그래서 지문은 매 주기 새 값이 되고, 억제는 **구조적으로 절대 못 건다.**
  반면 `kind` 는 census 가 쓰는 **고정 어휘**(11종)다 - 이쪽이 안정된 정체성이다.

▶ 앞 주기 검사가 왜 이걸 못 잡았나 (이게 핵심이다)
  `probe_capability_recurrence.py` 의 board 대역은 SQL 을 **통째로 무시**하고
  언제나 같은 행을 돌려준다:

      fa._board_rows = lambda sql, params=(): [("t_813944f2", "blocked", …)]

  실물에서 억제를 거는 것은 `where idempotency_key like ?` 의 **접두사
  일치**인데, 대역이 바로 그 필터를 지워 버렸다. 그러니 지문이 매번 달라져도
  검사는 초록이었다. 이 검사는 대역이 **접두사를 실제로 따진다** - 그것이
  이 검사가 앞 검사와 다른 점이다.

실행: python3 probe_kind_scope_stability.py
고치기 전 = 빨강(F2P), 고친 뒤 = 초록.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bottleneck_census as bc                                    # noqa: E402
import factory_autopilot as fa                                    # noqa: E402

OWNER = "quant-backtest-department"


def _b(kind: str, key: str) -> "bc.Bottleneck":
    return bc.Bottleneck(kind=kind, key=key, cost=1.0, unit="건",
                         evidence="보드에서 세었다", owner=OWNER, hint="열어라",
                         ids=["x"])


# 실측 두 장의 병목 묶음. 종류는 같고 개체만 다르다.
COMMON = [
    ("리드 사장", "lead:idle"),
    ("막힘: 역량 부족(재라우팅 후보)", f"cap:{OWNER}:capability"),
    ("막힘: 입력 대기(사람 확인 필요)", f"cap:{OWNER}:needs_input"),
    ("시세 미관측", "market:zero_volume"),
    ("시세 정수배 갭", "market:integer_gap"),
]
GROUP_A = [_b(k, v) for k, v in COMMON] + [
    _b("작업 되풀이 실패", "job:verdict=BAD_TRANSITION; backlog=RUNNING -> PREREGISTERED"),
    _b("건전성:완주가능", "sound:max_drawdown_stop:불명"),
]
GROUP_B = [_b(k, v) for k, v in COMMON] + [
    _b("작업 되풀이 실패", "job:OSError: [Errno 12] Cannot allocate memory"),
    _b("건전성:완주가능", "sound:order_flow_imbalance:구현:고아"),
]
# 진짜로 새 종류가 하나 늘어난 묶음 - 이건 반드시 새 카드가 나가야 한다
GROUP_C = list(GROUP_B) + [_b("관문 상습 조항", "gate:fragility")]

ALIVE = ("ready", "running", "blocked")


def _run_cycle(group, board, *, stamp: str) -> list[str]:
    """`_issue_bottleneck_cards` 를 한 주기 돌리고 **새로 건 키**를 돌려준다.

    board 대역은 실물처럼 **`like ?` 접두사를 실제로 따진다.** 이 한 줄이
    앞 검사와의 차이다.
    """
    made: list[str] = []

    def _rows(sql, params=()):
        assert "like" in sql.lower(), f"접두사 조회가 아니다: {sql}"
        prefix = str(params[0]).rstrip("%")
        return [r for r in board
                if r[2].startswith(prefix) and r[1] in ALIVE]

    saved = (fa._create_card, fa._board_rows, bc.census)
    try:
        fa._create_card = lambda **kw: (made.append(kw["key"]), "t_new")[1]
        fa._board_rows = _rows
        bc.census = lambda conn=None: list(group)
        fa._issue_bottleneck_cards(stamp=stamp, dry_run=False)
    finally:
        fa._create_card, fa._board_rows, bc.census = saved
    return made


def check_instance_churn_does_not_remint() -> None:
    """**개체가 갈려도 종류가 같으면 같은 카드다.** (이번에 고치는 것)

    실측 재현: 10시 카드가 `ready` 로 살아 있는데, 11시에 실패 서명 하나가
    갈렸다는 이유로 새 카드가 나갔다. 부서는 같은 질문을 두 번 받는다.
    """
    first = _run_cycle(GROUP_A, [], stamp="20260814T10")
    assert len(first) == 1, f"첫 카드가 안 나갔다: {first}"
    board = [("t_8699a48f", "ready", first[0])]

    again = _run_cycle(GROUP_B, board, stamp="20260814T11")
    assert not again, (
        "종류가 똑같은데 개체가 갈렸다는 이유로 카드를 또 걸었다 - 이것이 "
        "지문을 붙이고도 48분 만에 카드가 또 나간 이유다.\n"
        f"  살아 있던 카드: {first[0]}\n  또 건 카드    : {again[0]}")
    print("  개체가 갈려도 안 다시 검   OK")


def check_new_kind_still_mints() -> None:
    """**반대 방향.** 종류가 진짜로 늘면 새 카드가 나가야 한다.

    억제가 새 병목까지 삼키면 "겉만 덮은 것" 이 조용해진다 - 개선 카드가
    금지한 바로 그 실패다.
    """
    first = _run_cycle(GROUP_A, [], stamp="20260814T10")
    board = [("t_8699a48f", "ready", first[0])]

    again = _run_cycle(GROUP_C, board, stamp="20260814T11")
    assert len(again) == 1, (
        "새 종류(관문 상습 조항)가 생겼는데 카드를 안 걸었다 - 억제가 "
        f"과하다: {again}")
    print("  새 종류엔 새 카드          OK")


def check_finished_card_is_reissued() -> None:
    """**앞 카드가 끝났으면 다시 건다.** (고치기 전에도 초록 - 지켜야 할 쪽)"""
    first = _run_cycle(GROUP_A, [], stamp="20260814T10")
    board = [("t_8699a48f", "done", first[0])]
    again = _run_cycle(GROUP_A, board, stamp="20260814T11")
    assert len(again) == 1, f"끝난 카드가 억제 근거가 됐다: {again}"
    print("  끝난 뒤엔 다시 검          OK")


def check_same_group_is_suppressed() -> None:
    """**앞 주기가 세운 성질.** 완전히 같은 묶음은 억제된다 (양쪽 다 초록)."""
    first = _run_cycle(GROUP_A, [], stamp="20260814T10")
    board = [("t_8699a48f", "blocked", first[0])]
    again = _run_cycle(GROUP_A, board, stamp="20260814T11")
    assert not again, f"같은 묶음인데 또 걸었다 - 앞 주기 성질이 깨졌다: {again}"
    print("  같은 묶음은 억제됨         OK")


def check_other_department_is_not_suppressed() -> None:
    """**남의 부서 카드가 내 카드를 막으면 안 된다.** (양쪽 다 초록)"""
    first = _run_cycle(GROUP_A, [], stamp="20260814T10")
    mine = [bc.Bottleneck(kind=b.kind, key=b.key, cost=b.cost, unit=b.unit,
                          evidence=b.evidence, owner="research-department",
                          hint=b.hint, ids=b.ids) for b in GROUP_A]
    board = [("t_8699a48f", "ready", first[0])]
    again = _run_cycle(mine, board, stamp="20260814T11")
    assert len(again) == 1, f"남의 부서 카드가 내 카드를 막았다: {again}"
    print("  부서가 섞이지 않음         OK")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("범위 지문 안정성 검사 (보드·DB 없음)")
    fails = 0
    for fn in (check_instance_churn_does_not_remint,
               check_new_kind_still_mints,
               check_finished_card_is_reissued,
               check_same_group_is_suppressed,
               check_other_department_is_not_suppressed):
        try:
            fn()
        except AssertionError as exc:
            fails += 1
            print(f"  !! {fn.__name__}\n     {exc}")
        except AttributeError as exc:
            fails += 1
            print(f"  !! {fn.__name__} - 아직 없는 손잡이: {exc}")
    print(f"\n{'통과' if not fails else str(fails) + '건 빨강'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
