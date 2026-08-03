#!/usr/bin/env python3
"""Skeptic - 분석가끼리 실제로 대화시키는 유일한 자리.

담당: 재일 (리서치본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md
        6.1절 9단계(Challenge) "대안 설명, 반대 근거, 과신과 누락 질문 제시.
        치명적 반증이면 INSUFFICIENT", 6.4절(dissent 필드)
      docs/HEDGE_FUND_MASTER_PLAN.md "충돌하는 견해를 Dossier 에 통합하되
        **반대 의견을 삭제하지 않는다**"
      재일님 지시 2026-08-03 "에이전트 대화도 구현해"

▶ 지금까지 대화가 없었다
  분석가 6인이 **병렬로 각자 답하고 총괄이 fan-in** 할 뿐이었다. 서로의 판정을
  보지 않고, 반박하지 않고, 모순이 있어도 총괄이 매끄럽게 뭉갠다.
  그래서 리포트가 "여섯 개의 독백" 이 된다 - 그게 분석이 얕게 느껴지는 이유다.

▶ 대화의 절반은 코드가 한다
  "누가 누구와 어긋나는가" 는 **물어볼 필요가 없다.** 판정 라벨의 방향을 코드가
  비교하면 나온다. LLM 에게 물으면 없는 갈등을 지어내거나 있는 갈등을 놓친다.
  코드가 갈등을 찾고, LLM 은 **찾아진 갈등에 대해서만** 대안 설명을 쓴다.

  이 분리가 이 모듈의 설계 전부다:
    detect_disagreements()  결정론. 무엇이 어긋났는가
    challenge()             LLM. 그 어긋남이 무엇을 뜻하는가
    verify_challenge()      결정론. 반박이 확정치 안에서 말하는가

▶ 반박은 사라지지 않는다
  Skeptic 이 낸 것은 packet["dissent"] 로 **그대로 남는다.** 총괄이 동의하든
  안 하든 지우지 못한다. 마스터플랜이 "반대 의견을 삭제하지 않는다" 고 못박은
  이유는, 지워진 반박은 나중에 같은 자리에서 다시 넘어지기 때문이다.

▶ 치명적 반증은 등급을 낮춘다
  6.1절 9단계가 "치명적 반증이면 INSUFFICIENT" 라고 정했다. 표시만 하는 반박은
  하류에서 안 읽힌다 - 등급을 낮춰야 계약이 된다(낮추기만 하고 올리지 않는다).

실행: python agents/skeptic.py     # 자체 점검 (LLM·네트워크 없음)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "evidence"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SKEPTIC_VERSION = "research-skeptic-v1"

# 판정 라벨 -> 방향. 라벨 집합이 분석가마다 다르므로 부분 문자열로 본다.
# 모르는 라벨은 **중립이 아니라 미지(None)** 다 - 모르는 것을 안다고 하지 않는다.
_UP = ("BULL", "POSITIVE", "RISK_ON", "IMPROV", "EXPAND", "THRUST", "ORDERLY")
_DOWN = ("BEAR", "NEGATIVE", "RISK_OFF", "DETERIOR", "SHOCK", "CONTRACT",
         "STRESSED", "ELEVATED")
_FLAT = ("NEUTRAL", "MIXED", "NOTED", "SCORED", "RANGE")

# 이 값들은 '결과 없음' 이지 판정이 아니다 - 갈등 판정에서 제외한다
_NO_RESULT = {"INSUFFICIENT_DATA", "UNAVAILABLE", "ERROR", "NOT_RUN", None, ""}


def direction_of(verdict: str | None) -> str | None:
    """판정 라벨의 방향. 모르면 None - 억지로 한쪽으로 밀지 않는다."""
    if verdict in _NO_RESULT:
        return None
    v = str(verdict).upper()
    if any(t in v for t in _UP):
        return "UP"
    if any(t in v for t in _DOWN):
        return "DOWN"
    if any(t in v for t in _FLAT):
        return "FLAT"
    return None


@dataclass(frozen=True)
class Disagreement:
    """두 분석가가 어긋난 지점. **코드가 찾은 것이므로 재현된다.**"""

    left: str
    left_verdict: str
    right: str
    right_verdict: str
    kind: str          # 'OPPOSITE' | 'SIGNAL_VS_CONTEXT'
    note: str

    def line(self) -> str:
        return (f"{self.left}={self.left_verdict} ↔ "
                f"{self.right}={self.right_verdict} ({self.note})")


# 개별 종목 신호 vs 시장 맥락. 둘이 어긋나는 것은 오류가 아니라 **정보**다 -
# "종목은 좋은데 시장이 나쁘다" 는 판단을 바꿔야 하는 상황이다.
_SIGNAL = ("technical", "fundamental", "microstructure")
_CONTEXT = ("regime", "geopolitical")


def detect_disagreements(analysts: dict) -> list[Disagreement]:
    """분석가 판정 사이의 갈등을 **결정론으로** 찾는다.

    analysts: {노드이름: {"verdict": ...}} - scripts.py 의 state 모양.

    LLM 에게 "누가 어긋나나" 를 묻지 않는다. 물으면 없는 갈등을 지어내거나
    있는 갈등을 놓친다. 여기서 찾아진 것만 반박 대상이 된다.
    """
    dirs: dict[str, tuple[str, str]] = {}
    for node, st in (analysts or {}).items():
        v = (st or {}).get("verdict") if isinstance(st, dict) else None
        d = direction_of(v)
        if d is not None:
            dirs[node] = (d, str(v))

    out: list[Disagreement] = []
    names = sorted(dirs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            da, va = dirs[a]
            db, vb = dirs[b]
            if da == db:
                continue
            if {da, db} == {"UP", "DOWN"}:
                out.append(Disagreement(
                    a, va, b, vb, "OPPOSITE",
                    "방향이 정반대다 - 한쪽은 틀렸거나 서로 다른 지평을 본다"))
            elif "FLAT" in (da, db):
                # FLAT vs 방향은 약한 갈등이다. 신호 vs 맥락일 때만 의미가 있다.
                sig = a if a in _SIGNAL else (b if b in _SIGNAL else None)
                ctx = a if a in _CONTEXT else (b if b in _CONTEXT else None)
                if sig and ctx:
                    out.append(Disagreement(
                        sig, dirs[sig][1], ctx, dirs[ctx][1], "SIGNAL_VS_CONTEXT",
                        "종목 신호와 시장 맥락이 어긋난다 - 어느 쪽이 지배하는지가 판단이다"))
    return out


def summarize(disagreements: list[Disagreement]) -> dict:
    """갈등 요약. **0건도 결과다** - 갈등이 없으면 없다고 말한다."""
    return {
        "count": len(disagreements),
        "opposite": sum(1 for d in disagreements if d.kind == "OPPOSITE"),
        "signal_vs_context": sum(1 for d in disagreements
                                 if d.kind == "SIGNAL_VS_CONTEXT"),
        "lines": [d.line() for d in disagreements],
        # 갈등이 없다는 것이 곧 합의는 아니다 - 판정이 없어서일 수도 있다.
        "note": ("판정 간 갈등 없음" if not disagreements
                 else f"{len(disagreements)}건의 갈등이 있다 - 반박 대상"),
    }


CHALLENGE_SYSTEM = (
    "You are the Skeptic (RES-00 challenge stage) of a Korean equity research "
    "department. You are given the draft thesis, the analysts' verdicts, and a list "
    "of DISAGREEMENTS that were computed by code - not by you. "
    "Your job is to argue against the draft, not to improve it. "
    "For each disagreement, give the alternative explanation the draft ignored. "
    "Then name the single weakest claim in the draft and say what evidence would "
    "overturn it. "
    "HARD RULES: (1) Write in Korean. (2) Every number you use must already appear "
    "in the confirmed figures given to you - you may not introduce any new number. "
    "(3) Do not invent events, dates or sources. (4) If the disagreements are not "
    "material, say so plainly rather than manufacturing doubt. "
    "(5) You do not decide anything - you only surface what a careful reader would ask."
)


def build_challenge_prompt(*, thesis: str, analysts: dict,
                           disagreements: list[Disagreement],
                           confirmed: dict) -> str:
    """반박 프롬프트. **코드가 찾은 갈등만** 준다."""
    import json

    verdicts = {k: (v or {}).get("verdict") for k, v in (analysts or {}).items()
                if isinstance(v, dict)}
    return json.dumps({
        "draft_thesis": thesis,
        "analyst_verdicts": verdicts,
        "disagreements_found_by_code": [d.line() for d in disagreements],
        "confirmed_figures": confirmed,
        "answer_schema": {
            "alternative_explanations": ["문장"],
            "weakest_claim": "문장",
            "what_would_overturn_it": "문장",
            "material": True,
        },
    }, ensure_ascii=False, default=str)


def deep_pool(confirmed: dict, *, extra: dict | None = None) -> list[float]:
    """확정치 전체에서 인용 가능한 수치를 모은다.

    number_guard.numeric_pool 은 **최상위만** 본다(그 자리에서는 그게 맞다 -
    분석가 readout 은 평평하다). 그런데 Skeptic 이 보는 confirmed 는
    {"price": {...}, "analysts": {...}} 처럼 중첩이라, 최상위만 보면 풀이 0 이
    되고 **모든 반박이 창작으로 몰린다**(자체점검이 실제로 잡았다).

    extra 는 도구로 정당하게 가져온 수치다(analyst_toolbox.retrieved_numbers).
    가져온 것이 풀에 들어가야 자율성과 검증이 같이 큰다.
    """
    pool: list[float] = []

    def walk(v, depth=0):
        if depth > 4:            # 무한 중첩 방지. 4단이면 충분하다
            return
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            pool.append(float(v))
        elif isinstance(v, dict):
            for k, iv in v.items():
                # 개수·길이는 풀에 넣지 않는다 - 넣으면 LLM 이 그 숫자를 단위와
                # 함께 지어내도 통과한다(number_guard 주석과 같은 이유).
                if k in ("bars_used", "days_used", "n", "count", "limit"):
                    continue
                walk(iv, depth + 1)
        elif isinstance(v, (list, tuple)):
            for iv in v:
                walk(iv, depth + 1)

    walk(confirmed)
    walk(extra or {})
    return pool


def verify_challenge(text: str, confirmed: dict, *,
                     retrieved: dict | None = None) -> dict:
    """반박도 검증한다. **반박이라고 창작이 허용되지는 않는다.**

    retrieved 를 주면 도구로 가져온 수치까지 풀에 들어간다.
    """
    from number_guard import flag_unmatched

    pool = deep_pool(confirmed, extra=retrieved)
    unmatched = flag_unmatched(text or "", pool)
    return {"ok": not unmatched, "unmatched": unmatched,
            "pool_size": len(pool)}


def _as_sentence(item) -> str:
    """반박 한 줄을 사람이 읽는 문장으로. dict 로 와도 흡수한다.

    실측 2026-08-03: 총괄이 alternative_explanations 를 문자열 목록이 아니라
    [{"disagreement": ..., "explanation": ...}] 로 냈고, str() 한 결과가
    리포트에 파이썬 리터럴 그대로 찍혔다. **읽을 수 없는 반박은 반박이 아니다.**
    """
    if isinstance(item, dict):
        head = str(item.get("disagreement") or item.get("claim") or "").strip()
        body = str(item.get("explanation") or item.get("alternative")
                   or item.get("reason") or "").strip()
        if head and body:
            return f"{head} — {body}"
        return head or body or ""
    return str(item).strip()


def apply_challenge(packet: dict, *, disagreements: list[Disagreement],
                    challenge: dict | None, verification: dict | None) -> dict:
    """반박을 Packet 에 **남긴다**. 총괄이 지우지 못한다.

    치명적 반증(material=True 이고 OPPOSITE 갈등이 있음)이면 등급을 낮춘다 -
    6.1절 9단계. 낮추기만 하고 올리지 않는다.
    """
    p = dict(packet or {})
    summary = summarize(disagreements)
    p["disagreements"] = summary

    existing = list(p.get("dissent") or [])
    added: list[str] = []
    if challenge:
        for line in (challenge.get("alternative_explanations") or []):
            # ▶ LLM 이 문자열 대신 {disagreement, explanation} dict 를 내는 일이
            #   잦다(실측 2026-08-03). dict 를 str() 하면 리포트에 파이썬 리터럴이
            #   그대로 찍혀 사람이 못 읽는다 - 모양을 여기서 흡수한다.
            s = _as_sentence(line)
            if s and s not in existing:
                added.append(s)
        weak = str(challenge.get("weakest_claim") or "").strip()
        if weak:
            over = str(challenge.get("what_would_overturn_it") or "").strip()
            added.append(f"가장 약한 주장: {weak}"
                         + (f" / 뒤집는 근거: {over}" if over else ""))
    # 검증에 실패한 반박은 **버리지 않고 표시한다** - 반박을 지우는 것이 더 나쁘다
    if verification and not verification.get("ok"):
        added.append(f"⚠ 반박 서술에 확정치 밖 수치가 있다: "
                     f"{verification.get('unmatched')}")
    p["dissent"] = existing + added
    p["_challenge_verification"] = verification or {"ok": None,
                                                    "reason": "반박 미실행"}

    material = bool((challenge or {}).get("material"))
    if material and summary["opposite"] > 0:
        order = ("insufficient_evidence", "partial", "sufficient")
        cur = str(p.get("evidence_quality", "sufficient"))
        if cur in order and order.index(cur) > order.index("partial"):
            p["evidence_quality"] = "partial"
            p["_downgraded_by_challenge"] = cur
    return p


# ---------------------------------------------------------------------------
# 자체 점검 - LLM·네트워크 없음
# ---------------------------------------------------------------------------

def _a(**kw) -> dict:
    return {k: {"verdict": v} for k, v in kw.items()}


def _check_direction():
    assert direction_of("BULLISH") == "UP"
    assert direction_of("RISK_OFF") == "DOWN"
    assert direction_of("NEUTRAL") == "FLAT"
    # 모르는 라벨은 중립이 아니라 미지다
    assert direction_of("WEIRD_LABEL") is None
    # 결과 없음은 판정이 아니다
    for v in ("INSUFFICIENT_DATA", "NOT_RUN", None, ""):
        assert direction_of(v) is None, v
    print("  방향 판정(모르면 미지)   OK")


def _check_detect_is_deterministic():
    st = _a(technical="BULLISH", regime="RISK_OFF", fundamental="NOTED")
    d1 = detect_disagreements(st)
    d2 = detect_disagreements(st)
    assert [x.line() for x in d1] == [x.line() for x in d2], "같은 입력에 다른 결과"
    kinds = {x.kind for x in d1}
    assert "OPPOSITE" in kinds, [x.line() for x in d1]
    # 판정 없는 분석가는 갈등에 안 들어간다
    st2 = _a(technical="BULLISH", regime="INSUFFICIENT_DATA")
    assert detect_disagreements(st2) == []
    print("  갈등 탐지(결정론)        OK")


def _check_signal_vs_context():
    """종목 신호와 시장 맥락의 어긋남은 오류가 아니라 정보다."""
    st = _a(fundamental="NOTED", geopolitical="ELEVATED")
    ds = detect_disagreements(st)
    assert any(d.kind == "SIGNAL_VS_CONTEXT" for d in ds), [d.line() for d in ds]
    # 같은 층끼리의 FLAT 차이는 갈등으로 세지 않는다 - 잡음이 된다
    st2 = _a(technical="NEUTRAL", fundamental="NOTED")
    assert detect_disagreements(st2) == []
    print("  신호 vs 맥락 구분        OK")


def _check_zero_is_a_result():
    """갈등 0건도 결과다 - 침묵하지 않는다."""
    s = summarize([])
    assert s["count"] == 0 and "없음" in s["note"], s
    # 다만 '갈등 없음' 이 곧 합의는 아니다(판정이 없어서일 수 있다)
    assert "합의" not in s["note"]
    print("  0건도 결과다             OK")


def _check_dissent_is_never_deleted():
    """**이 모듈의 존재 이유** - 반박이 살아남는가."""
    packet = {"evidence_quality": "sufficient", "dissent": ["기존 반대 의견"]}
    ds = detect_disagreements(_a(technical="BULLISH", regime="RISK_OFF"))
    ch = {"alternative_explanations": ["시장 전체 반등일 뿐 종목 고유 요인이 아니다"],
          "weakest_claim": "실적 개선이 주가를 끌었다",
          "what_would_overturn_it": "동종업계가 같이 올랐다면 종목 요인이 아니다",
          "material": True}
    out = apply_challenge(packet, disagreements=ds, challenge=ch,
                          verification={"ok": True, "unmatched": []})
    assert "기존 반대 의견" in out["dissent"], "기존 반박이 지워졌다"
    assert any("시장 전체 반등" in x for x in out["dissent"])
    assert any("가장 약한 주장" in x for x in out["dissent"])
    # 치명적 반증이면 등급이 내려간다
    assert out["evidence_quality"] == "partial", out["evidence_quality"]
    assert out["_downgraded_by_challenge"] == "sufficient"
    print("  반박 보존·등급 강등      OK")


def _check_downgrade_only_when_material():
    packet = {"evidence_quality": "sufficient"}
    ds = detect_disagreements(_a(technical="BULLISH", regime="RISK_OFF"))
    # material=False 면 낮추지 않는다 - 없는 의심을 만들지 않는다
    out = apply_challenge(packet, disagreements=ds,
                          challenge={"material": False}, verification={"ok": True})
    assert out["evidence_quality"] == "sufficient"
    # OPPOSITE 갈등이 없으면 material 이어도 낮추지 않는다
    ds2 = detect_disagreements(_a(fundamental="NOTED", geopolitical="ELEVATED"))
    out2 = apply_challenge(packet, disagreements=ds2,
                           challenge={"material": True}, verification={"ok": True})
    assert out2["evidence_quality"] == "sufficient", out2
    # 올리지는 않는다
    low = apply_challenge({"evidence_quality": "insufficient_evidence"},
                          disagreements=ds, challenge={"material": True},
                          verification={"ok": True})
    assert low["evidence_quality"] == "insufficient_evidence"
    print("  강등 조건(올리지 않음)   OK")


def _check_deep_pool():
    """중첩된 확정치를 못 보면 **모든 반박이 창작으로 몰린다**(실제로 그랬다)."""
    confirmed = {"price": {"change_1d_pct": 2.5},
                 "analysts": {"technical": {"readout": {"rsi": 62.1}}}}
    pool = deep_pool(confirmed)
    assert 2.5 in pool and 62.1 in pool, pool
    # 개수는 풀에 안 들어간다 - 들어가면 검증이 헐거워진다
    assert deep_pool({"x": {"bars_used": 21}}) == []
    # 도구로 가져온 수치는 들어간다 - 자율성과 검증이 같이 큰다
    assert 3.0 in deep_pool({}, extra={"story_cluster.독립출처수": 3.0})
    print("  중첩 확정치 풀          OK")


def _check_challenge_is_verified():
    """반박이라고 창작이 허용되지 않는다."""
    confirmed = {"price": {"change_1d_pct": 2.5}}
    good = verify_challenge("1일 등락률 2.5%는 시장 전체 흐름과 같다", confirmed)
    assert good["ok"] is True, good
    bad = verify_challenge("영업이익이 47.3% 늘었다", confirmed)
    assert bad["ok"] is False and bad["unmatched"], bad
    # 검증 실패한 반박도 **버리지 않고 표시한다**
    out = apply_challenge({"evidence_quality": "sufficient"}, disagreements=[],
                          challenge={"alternative_explanations": ["47.3% 증가"]},
                          verification=bad)
    assert any("확정치 밖 수치" in x for x in out["dissent"]), out["dissent"]
    # LLM 이 dict 로 내도 사람이 읽는 문장이 된다 (실측 2026-08-03)
    d = _as_sentence({"disagreement": "레짐이 강세로 해석됐다",
                      "explanation": "단기 모멘텀일 수 있다"})
    assert d == "레짐이 강세로 해석됐다 — 단기 모멘텀일 수 있다", d
    out2 = apply_challenge({"evidence_quality": "sufficient"}, disagreements=[],
                           challenge={"alternative_explanations": [
                               {"disagreement": "a", "explanation": "b"}]},
                           verification={"ok": True})
    assert "a — b" in out2["dissent"], out2["dissent"]
    assert not any("{" in x for x in out2["dissent"]), "파이썬 리터럴이 찍혔다"
    print("  반박도 검증한다          OK")


def _check_prompt_gives_only_found_conflicts():
    """LLM 에게 코드가 찾은 갈등만 준다 - 없는 갈등을 지어내지 않게."""
    ds = detect_disagreements(_a(technical="BULLISH", regime="RISK_OFF"))
    p = build_challenge_prompt(thesis="t", analysts=_a(technical="BULLISH"),
                               disagreements=ds, confirmed={"price": {"x": 1}})
    assert "disagreements_found_by_code" in p
    assert "technical=BULLISH" in p
    print("  프롬프트 계약            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{SKEPTIC_VERSION} 자체 점검 (LLM·네트워크 없음)")
    _check_direction()
    _check_detect_is_deterministic()
    _check_signal_vs_context()
    _check_zero_is_a_result()
    _check_dissent_is_never_deleted()
    _check_downgrade_only_when_material()
    _check_deep_pool()
    _check_challenge_is_verified()
    _check_prompt_gives_only_found_conflicts()
    print("Skeptic 9개 영역 통과.")
