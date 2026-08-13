"""실패 귀속 - **무엇이 잘못됐는지 가른 뒤에 누구에게 보낼지 정한다.**

담당: 재일 (리서치본부 RSH / 퀀트·백테스트본부 QNT 공용)

▶ 왜 이 모듈이 필요한가 (2026-08-12, 문헌 + 실측)
  오늘 주기가 `발주 0건`으로 끝났다. 근원을 따라가니 막는 말이 전부 하나였다.

      발주 보류 4건   -> "리서치가 가설을 다시 등록해야 한다"
      배분자 보류     -> "리서치가 부모를 다시 등록해야 한다"
      Gate 0 반려     -> "가장 가까운 코드를 쓰고 한계를 적어라"

  그런데 막힌 것 셋은 `proposal_id` 가 없는 배분자 변형이라 **리서치가 다시
  낼 원본이 없다.** 고칠 주체를 지목하지 못하는 fail-closed 는 안전한 게
  아니라 그냥 정지다.

  SAGE(arXiv 2606.31478)가 같은 문제를 때린다: 자율 연구 에이전트가 실패를
  **"하나의 서술적 비평"** 으로 처리하면 지표·로그·설계 선택이 뭉개져서
  헛된 시행착오나 과도한 방향전환이 된다. 처방은 복구를 **구조적 인과
  진단**으로 바꾸는 것 - 여러 설명을 세우고, 각각을 따로 재고, 검증된
  근본원인을 **결정론적으로 개입 수준에 라우팅**한다. 그렇게 해서 지표를
  담은 산출이 42% -> 92% 가 됐다.

  MAST(arXiv 2503.13657)는 이 유형에 이름을 붙여 놨다. 우리가 오늘 잰 것이
  그대로 상위 빈도다 - FM-1.3 Step Repetition(15.7%, job 95개 재발행),
  FM-1.5 Unaware of Termination(12.4%, TESTING 8.2일), FM-2.2 Fail to Ask
  for Clarification(6.8%, 어휘에 없으면 묻지 않고 깎음). 그리고 같은 논문이
  프롬프트 개입을 실제로 시도해 +9.4% / +15.6% 를 얻고도 결론을 이렇게
  냈다 - *tactical approaches are insufficient*. 그래서 여기는 프롬프트가
  아니라 **배선**이다.

▶ 개입 수준 (SAGE 3층을 우리 부서 구조에 사상)
      가설  hypothesis      -> 리서치. 아이디어·어휘·키가 계약과 안 맞는다
      설계  experiment      -> 퀀트. 창·유니버스·데이터셋·중복이 안 맞는다
      구현  implementation  -> 퀀트. 실행면에 그 개념을 표현할 자리가 없다
      일시  transient       -> 아무도. 재시도가 맞는 종류다
      불명  unknown         -> 카드. **지어내지 않는다**

▶ 불명을 버리지 않는다
  규칙에 없는 사유는 `불명` 으로 남기고 카드에 싣는다. 그것이 다음 규칙이
  된다 - 미리 다 적어 넣을 수 있었으면 애초에 병목이 아니었다.

자체 점검: python departments/01-research/factory/attribution.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

MODULE_VERSION = "factory-attribution-v1"

HYPOTHESIS = "가설"
DESIGN = "설계"
IMPLEMENTATION = "구현"
TRANSIENT = "일시"
UNKNOWN = "불명"

# 어느 부서가 그 수준에서 실제로 손을 댈 수 있나. **권한이 없는 쪽에 보내면
# 카드가 되돌아온다** - 오늘 그렇게 8일이 갔다.
OWNER = {
    HYPOTHESIS: "research-department",
    DESIGN: "quant-backtest-department",
    IMPLEMENTATION: "quant-backtest-department",
    TRANSIENT: "quant-backtest-department",
    UNKNOWN: "quant-backtest-department",
}


@dataclass(frozen=True)
class Attribution:
    """실패 하나의 귀속. `level` 이 무엇을, `owner` 가 누가, `action` 이 어떻게."""

    level: str
    owner: str
    action: str
    evidence: str = ""

    @property
    def retryable(self) -> bool:
        """재시도로 풀리는 종류인가. **일시적인 것만 그렇다.**"""
        return self.level == TRANSIENT

    @property
    def routable(self) -> bool:
        """보낼 곳을 아는가. 모르면 카드가 사람·에이전트에게 판정을 맡긴다."""
        return self.level != UNKNOWN


# (정규식, 수준, 지시). **순서가 중요하다** - 위에서부터 처음 맞는 것을 쓴다.
# 좁은 규칙을 먼저, 넓은 규칙을 뒤에 둔다.
_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    # ── 일시 ────────────────────────────────────────────────────────────────
    (re.compile(r"연결이 없|connection|timeout|타임아웃|일시적"),
     TRANSIENT,
     "인프라가 돌아오면 저절로 풀린다. 손대지 말고 다음 주기에 다시 봐라"),

    # ── 구현: 실행면에 그 개념이 없다 ────────────────────────────────────────
    (re.compile(r"어휘에 없다|UNMAPPED_VOCAB"),
     IMPLEMENTATION,
     "실행면에 그 엣지를 표현할 자리가 없다. PITView 재료(closes·notionals·"
     "daily_returns)로 만들어지면 `strategy_templates.TEMPLATES` 에 템플릿을 "
     "더해라 - 어휘는 거기서 파생되므로 그 순간 Gate 0 이 열린다. 이미 있는 "
     "것과 같은 개념이면 그렇게 적고 그 어휘로 다시 내라. 재료가 모자라면 "
     "`NOT_IMPLEMENTED` 에 사유를 남겨라"),
    (re.compile(r"전략 구현|STRATEGY_CATALOG"),
     IMPLEMENTATION,
     "어휘에는 있는데 러너가 그 전략을 못 만든다. 카탈로그와 템플릿이 "
     "어긋난 것이니 실행면을 맞춰라"),
    (re.compile(r"BAD_TRANSITION|계약 순서"),
     IMPLEMENTATION,
     "상태 전이가 계약을 어겼다. 가설 내용이 아니라 상태기계 문제다"),
    # ▶ 이 규칙은 **`불명` 이 만들어 준 것이다** (2026-08-12)
    #   건전성을 처음 원장에 댔더니 죽은전이 2건(`illiquidity_premium`,
    #   `trend_following`)이 `불명` 으로 떨어졌다. 규칙에 없던 문장이라
    #   그게 맞다 - 그리고 모듈이 "판정했으면 규칙을 남겨라" 고 시킨 대로
    #   여기 남긴다. 다음 주기의 나는 이것을 다시 판정하지 않는다.
    (re.compile(r"실험이 0건|한 번도 검증된 적이 없다"),
     IMPLEMENTATION,
     "어휘에 있는데 한 번도 안 돈 엣지다. 접수는 실행 가능성의 약속인데 그 "
     "약속이 검증된 적이 없다 - 한 번 돌려 보고, 못 돌면 어휘에서 빼고 "
     "`NOT_IMPLEMENTED` 에 사유를 남겨라. 리서치가 그것을 골랐다가 실행 "
     "단계에서 죽는 것이 실측으로 있었다(`low_volatility` 7회)"),

    # ── 설계: 실험을 어떻게 돌릴지가 안 맞는다 ───────────────────────────────
    (re.compile(r"데이터셋이 없|사상할 데이터셋|커버리지"),
     DESIGN,
     "요구한 데이터 산출물에 맞는 데이터셋이 없다. 있는 데이터셋으로 범위를 "
     "좁히든지, 데이터셋을 만들어라(dataset-engineering)"),
    (re.compile(r"중복 실험"),
     DESIGN,
     "같은 input_hash 가 이미 있다. 같은 것을 또 돌리려는 것이므로 무엇을 "
     "바꿀지부터 정해라 - 기존 판정을 확인하고, 다르게 돌릴 것이 없으면 폐기"),
    (re.compile(r"창이 (0|부족)|walk.?forward 창"),
     DESIGN,
     "형성창 대비 표본이 짧아 창이 안 나온다. horizon_days 를 줄이거나 "
     "표본을 넓혀라"),

    # ── 가설: 기획안이 계약과 안 맞는다 ──────────────────────────────────────
    (re.compile(r"안 읽는 파라미터|파라미터 거부"),
     HYPOTHESIS,
     "기획안이 실행면에 없는 키를 썼다. 허용 키로 같은 아이디어를 다시 내라"),
    (re.compile(r"교훈에 대응이 없|LESSONS_ADDRESSED"),
     HYPOTHESIS,
     "같은 계열이 이미 기각됐는데 그 교훈에 대응이 없다. 무엇을 다르게 "
     "하는지 본문에 적어라"),
    (re.compile(r"리드|LEAD_IDS"),
     HYPOTHESIS,
     "근거 리드가 없다. 스카우트가 먼저다"),
)


def attribute(reason: str) -> Attribution:
    """실패 사유 한 줄 -> 귀속. **모르면 모른다고 한다.**"""
    text = str(reason or "").strip()
    if not text:
        return Attribution(UNKNOWN, OWNER[UNKNOWN],
                           "사유가 비어 있다. 무엇이 죽었는지 먼저 남겨라 - "
                           "사유 없는 실패는 판정할 수 없다", "")
    for pat, level, action in _RULES:
        m = pat.search(text)
        if m:
            return Attribution(level, OWNER[level], action, m.group(0))
    return Attribution(
        UNKNOWN, OWNER[UNKNOWN],
        "**규칙에 없는 사유다.** 어느 수준인지(가설/설계/구현/일시) 네가 "
        "판정하고, 판정했으면 `attribution._RULES` 에 규칙을 남겨라 - "
        "남기지 않으면 다음 주기의 네가 같은 것을 다시 판정한다", "")


def route(reasons) -> dict[str, list[str]]:
    """사유 여러 개 -> 수준별 묶음. 카드가 부서별로 갈라 실을 때 쓴다."""
    out: dict[str, list[str]] = {}
    for r in reasons or ():
        out.setdefault(attribute(r).level, []).append(str(r))
    return out


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_real_ledger_reasons_are_routed():
    """**오늘 원장에 실제로 있던 사유 전부가 갈린다.** (2026-08-12 원문)

    지어낸 문장이 아니라 `quant.experiment_jobs.failure_reason` 에서 그대로
    가져온 것들이다. 하나라도 `불명` 이면 라우팅이 뚫린 것이다.
    """
    cases = [
        ("verdict=NOT_RUNNABLE; backlog='short_term_reversal' 는 어휘에 없다 - "
         "사용 가능: ['breakout', ...]", IMPLEMENTATION),
        ("verdict=NOT_RUNNABLE; backlog='low_volatility' 전략 구현 "
         "(STRATEGY_CATALOG 등재 조건)", IMPLEMENTATION),
        ("verdict=NOT_RUNNABLE; backlog=사상할 데이터셋이 없다 - 리서치에 반려한다",
         DESIGN),
        ("verdict=NOT_RUNNABLE; backlog=시장 DB 연결이 없어 커버리지를 재지 못했다",
         TRANSIENT),
        ("RuntimeError: 중복 실험(c73f571d-48f6-4dc4-a5d2-08409de2703c) - "
         "기존 판정을 수동 확인할 것", DESIGN),
        ("RuntimeError: 가설 파라미터 거부: 실행면이 읽지 않는 파라미터가 "
         "있다: ['signal_window_days']", HYPOTHESIS),
        ("verdict=BAD_TRANSITION; backlog=RUNNING -> PREREGISTERED 는 계약 "
         "순서를 어긴다", IMPLEMENTATION),
        ("이 계열에서 이미 2회 실행하고 기각됐다 - 그 교훈에 대응이 없다: "
         "['OVERFIT_DSR']", HYPOTHESIS),
        # 건전성을 처음 원장에 댔을 때 `불명` 으로 떨어진 문장. 규칙으로
        # 승격됐으므로 이제 갈려야 한다 - 이 줄이 그 승격을 고정한다.
        ("어휘에 있는데 실험이 0건이다 - 접수는 실행 가능성의 약속인데 그 "
         "약속이 한 번도 검증된 적이 없다", IMPLEMENTATION),
    ]
    bad = [(r[:40], attribute(r).level, want) for r, want in cases
           if attribute(r).level != want]
    assert not bad, f"귀속이 틀렸다: {bad}"
    # 일시만 재시도로 풀린다
    assert attribute(cases[3][0]).retryable
    assert not attribute(cases[0][0]).retryable
    print("  원장 사유 9종이 갈림       OK")


def _check_unknown_is_not_invented():
    """**모르는 사유를 아무 데나 넣지 않는다.**

    `불명` 을 없애려고 넓은 정규식을 두면 엉뚱한 부서로 가고, 그 카드는
    권한이 없어 되돌아온다. 모르면 모른다고 적고 카드가 판정을 맡긴다.
    """
    a = attribute("듣도 보도 못한 새 사고: code 42 at /x/y.py")
    assert a.level == UNKNOWN, a
    assert not a.routable
    assert "규칙을 남겨라" in a.action, "다음 규칙이 되는 길을 안 알려준다"
    # 빈 사유도 지어내지 않는다
    assert attribute("").level == UNKNOWN
    assert attribute(None).level == UNKNOWN
    print("  모르는 것은 모른다고       OK")


def _check_every_level_has_a_real_owner():
    """**모든 수준에 실제로 손댈 수 있는 부서가 있다.**

    이게 오늘의 아킬레스건이었다 - "리서치가 다시 등록해라" 가 등록할 원본이
    없는 가설에도 붙었다. 고칠 주체를 못 대면 그건 라우팅이 아니라 정지다.
    """
    for level in (HYPOTHESIS, DESIGN, IMPLEMENTATION, TRANSIENT, UNKNOWN):
        assert OWNER.get(level), f"{level} 에 담당이 없다"
        a = attribute("x") if False else Attribution(level, OWNER[level], "a")
        assert a.owner in ("research-department", "quant-backtest-department")
    # 구현 문제를 리서치로 보내지 않는다 - 리서치는 러너를 못 고친다
    imp = attribute("'short_term_reversal' 는 어휘에 없다")
    assert imp.owner == "quant-backtest-department", imp
    # 가설 문제를 퀀트로 보내지 않는다 - 퀀트는 남의 기획안을 못 쓴다
    hyp = attribute("실행면이 안 읽는 파라미터: universe")
    assert hyp.owner == "research-department", hyp
    print("  수준마다 손댈 부서 있음   OK")


def _check_narrow_rules_win_over_broad():
    """좁은 규칙이 먼저다. 순서가 뒤집히면 전부 한 바구니로 간다."""
    # '어휘에 없다' 는 '전략 구현' 보다 먼저 잡혀야 한다(둘 다 실행면이지만
    # 지시가 다르다 - 하나는 템플릿 추가, 하나는 카탈로그 정합)
    a = attribute("'short_term_reversal' 는 어휘에 없다 - 전략 구현이 필요하다")
    assert "TEMPLATES" in a.action, a.action
    # 데이터셋(설계) 이 파라미터(가설) 보다 먼저 - 사유에 둘 다 있으면
    # 데이터셋이 없는 쪽이 먼저 풀려야 실험이 돈다
    b = attribute("사상할 데이터셋이 없다; 안 읽는 파라미터도 있다")
    assert b.level == DESIGN, b
    print("  좁은 규칙이 먼저 잡힘     OK")


def _check_route_groups_by_level():
    """부서별로 갈라 실을 수 있어야 카드가 만들어진다."""
    got = route(["'x' 는 어휘에 없다",
                 "사상할 데이터셋이 없다",
                 "안 읽는 파라미터: universe",
                 "듣도 보도 못한 것"])
    assert set(got) == {IMPLEMENTATION, DESIGN, HYPOTHESIS, UNKNOWN}, got
    assert len(got[IMPLEMENTATION]) == 1
    assert route([]) == {} and route(None) == {}
    print("  수준별로 묶임             OK")


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_real_ledger_reasons_are_routed()
    _check_unknown_is_not_invented()
    _check_every_level_has_a_real_owner()
    _check_narrow_rules_win_over_broad()
    _check_route_groups_by_level()
    print("실패 귀속 5개 영역 통과.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_selfcheck())
