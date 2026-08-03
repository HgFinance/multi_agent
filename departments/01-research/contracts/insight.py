"""Insight - 해석을 1급 객체로 분리한다.

담당: 재일 (리서치본부 RES)
근거: RESEARCH_QUANT_AGENTIC_FRAMEWORK.md, HEDGE_FUND_MASTER_PLAN.md 5.5절

▶ 왜 필요한가 - 인사이트가 나올 자리가 없었다
  분석가는 compute(결정론) -> narrate(LLM) -> verify(결정론) 로 돈다. 라벨을
  코드가 정하고 LLM 은 그 라벨을 문장으로 옮긴다. 그래서 리포트는 이렇게 된다:

    RES-04  ADX 28.3  -> 코드가 "추세 형성" -> LLM 이 그 라벨을 서술
    RES-07  폭 1.42   -> 코드가 "위험선호" -> LLM 이 그 라벨을 서술
    RES-03  스프 0.41%-> 코드가 "정상"     -> LLM 이 그 라벨을 서술

  **셋을 가로질러 "그래서 무슨 뜻인가" 를 말하는 자리가 없다.** 총괄이 thesis 를
  쓰지만 모든 문장이 확정치 대조를 받으므로 결국 숫자 나열로 수렴한다.

▶ 규칙 판정과 해석은 다른 것이다
  마스터 플랜이 결정론에 묶은 것은 **규칙 판정**이다 - PIT 필터, 인용 검증,
  한도 검사. 틀리면 컴플라이언스 사고이므로 기계가 해야 맞다.

  그런데 "이 세 신호가 같이 나타난다는 게 무슨 뜻인가" 는 규칙 판정이 아니다.
  둘을 같은 것으로 묶어 해석까지 결정론에 넘긴 것이 지금의 한계였다.
  Insight 는 **가설**이지 규칙 판정이 아니므로 그 원칙과 충돌하지 않는다.

▶ 검증 방식이 Fact 와 다르다 - 이것이 설계의 핵심이다
  Fact    수치·사실  -> 확정치 풀 대조 + evidence_ids 필수
  Insight 해석·가설  -> **수치 대조를 하지 않는다.** 대신 셋을 요구한다

    ① 서로 다른 분석가의 Fact 를 2개 이상 참조
       하나만 참조하면 그건 해석이 아니라 그 Fact 의 재진술이다.
    ② 반증 조건 필수
       "무엇이 관측되면 이 해석이 틀린가" 를 못 쓰는 문장은 틀릴 수 없는
       문장이고, 틀릴 수 없는 문장은 분석이 아니다.
    ③ 판정 지평
       언제 맞았는지 볼 수 있어야 채점이 된다.

  ②가 자유와 규율을 동시에 준다. LLM 은 마음껏 해석하되 반증 조건을 못 쓰면
  통과하지 못한다 - 헛소리는 반증 조건을 쓸 수 없다.

▶ 그리고 채점된다
  반증 조건과 지평이 있으므로 Insight 는 packet_claims 로 발행되고 지평이
  지나면 대조된다. 지금은 지표만 성적이 매겨지는데, 이걸로 **교차 해석이
  실제로 맞는지**까지 재게 된다 - 자가 발전의 대상이 지표에서 판단으로 넓어진다.

자체 점검: python departments/01-research/contracts/insight.py
"""

from __future__ import annotations

import re
import sys
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

CONTRACT_VERSION = "research-insight-v1"

# 해석의 종류. 자유 문자열로 두면 집계가 안 되고, 집계가 안 되면 성적도 못 낸다.
InsightKind = Literal[
    "CROSS_SIGNAL",      # 서로 다른 분석 축이 같은/반대 방향을 가리킨다
    "CAUSAL_HYPOTHESIS",  # A 때문에 B 가 일어났을 것이다
    "REGIME_READ",       # 지금 국면이 무엇이고 무엇이 바뀌면 전환인가
    "RISK_ASYMMETRY",    # 상방과 하방의 크기가 다르다
    "DIVERGENCE",        # 신호끼리 어긋난다 - 어느 쪽이 먼저 꺾이나
]

CONF = Literal["LOW", "MEDIUM", "HIGH"]

# 교차 참조 최소치. 1 이면 Fact 재진술이라 해석이 아니다.
MIN_SUPPORTING = 2
# 서로 다른 분석가 최소치. 같은 분석가 Fact 두 개는 한 관점이다.
MIN_DISTINCT_NODES = 2

# 반증 조건이 실제 조건인지 보는 최소 길이·형태.
MIN_FALSIFIER_CHARS = 12

# "틀릴 수 없는" 반증 조건을 걸러낸다. 이런 문장은 조건이 아니라 회피다.
_VACUOUS_FALSIFIER = re.compile(
    r"(없다|없음|해당\s*없|불가능|모른다|알\s*수\s*없|N/?A|없\s*음)\s*$")


class Insight(BaseModel):
    """해석 하나. **수치 대조를 받지 않는 대신 반증 가능해야 한다.**"""

    model_config = {"extra": "forbid"}

    kind: InsightKind
    claim: str = Field(min_length=10, max_length=400)
    # 어느 Fact 들을 가로질렀는가. Packet 의 fact id 를 가리킨다.
    supporting_fact_ids: list[str] = Field(min_length=MIN_SUPPORTING)
    # 그 Fact 들이 어느 분석가에게서 왔는가. 한 관점만이면 교차가 아니다.
    source_nodes: list[str] = Field(min_length=MIN_DISTINCT_NODES)
    # ▶ 이 한 줄이 자유와 규율을 동시에 준다
    falsifier: str = Field(min_length=MIN_FALSIFIER_CHARS, max_length=300)
    horizon_days: int = Field(ge=1, le=120)
    confidence: CONF = "MEDIUM"
    # 해석이 틀렸을 때 무엇을 바꿔야 하는가(선택). 있으면 학습이 빨라진다.
    revision_hint: Optional[str] = Field(default=None, max_length=200)

    @field_validator("supporting_fact_ids")
    @classmethod
    def _distinct_facts(cls, v: list[str]) -> list[str]:
        cleaned = [x.strip() for x in v if x and x.strip()]
        if len(set(cleaned)) < MIN_SUPPORTING:
            raise ValueError(
                f"서로 다른 Fact 를 {MIN_SUPPORTING}개 이상 참조해야 한다 - "
                f"하나만 가리키면 해석이 아니라 그 Fact 의 재진술이다")
        return cleaned

    @field_validator("source_nodes")
    @classmethod
    def _distinct_nodes(cls, v: list[str]) -> list[str]:
        cleaned = [x.strip() for x in v if x and x.strip()]
        if len(set(cleaned)) < MIN_DISTINCT_NODES:
            raise ValueError(
                f"서로 다른 분석가 {MIN_DISTINCT_NODES}인 이상의 근거를 가로질러야 "
                f"한다 - 같은 분석가의 Fact 둘은 한 관점이다")
        return cleaned

    @field_validator("falsifier")
    @classmethod
    def _falsifier_is_a_condition(cls, v: str) -> str:
        s = v.strip()
        if _VACUOUS_FALSIFIER.search(s):
            raise ValueError(
                "반증 조건이 회피 문장이다 - 무엇이 관측되면 이 해석이 틀리는지 "
                "구체적으로 써야 한다. 틀릴 수 없는 문장은 분석이 아니다")
        return s


def validate_insights(raw: list[dict], *, fact_ids: set[str],
                      known_nodes: set[str]) -> tuple[list[Insight], list[dict]]:
    """LLM 이 낸 해석 목록 -> (통과, 거부+사유).

    **참조 무결성을 여기서 본다.** 존재하지 않는 Fact 를 가리키는 해석은
    근거가 없는 것이고, 그건 수치 창작과 같은 종류의 사고다. 다만 사유를
    남긴다 - 조용히 버리면 왜 인사이트가 안 나오는지 아무도 모른다.
    """
    ok: list[Insight] = []
    rejected: list[dict] = []
    for item in (raw or []):
        try:
            ins = Insight(**item)
        except ValidationError as e:
            rejected.append({"raw": item,
                             "reason": e.errors()[0].get("msg", "계약 위반")[:160]})
            continue
        missing = [f for f in ins.supporting_fact_ids if f not in fact_ids]
        if missing:
            rejected.append({"raw": item,
                             "reason": f"없는 Fact 를 참조한다: {missing[:3]}"})
            continue
        unknown = [n for n in ins.source_nodes if n not in known_nodes]
        if unknown:
            rejected.append({"raw": item,
                             "reason": f"없는 분석가를 가리킨다: {unknown[:3]}"})
            continue
        ok.append(ins)
    return ok, rejected


def to_claims(insights: list[Insight], *, symbol: str) -> list[dict]:
    """Insight -> packet_claims 행. **해석도 채점 대상이 된다.**

    지금은 지표만 성적이 매겨진다. 해석에 반증 조건과 지평이 있으므로 같은
    경로로 발행하면 "이 분석가의 교차 해석이 실제로 맞나" 까지 재게 된다.
    """
    out = []
    for i, ins in enumerate(insights):
        out.append({
            "kind": "INSIGHT_" + ins.kind,
            "metric": "insight",
            "op": "==",
            "threshold_text": ins.claim[:200],
            "horizon_days": ins.horizon_days,
            "source_node": ",".join(sorted(set(ins.source_nodes)))[:60],
            "method_key": "insight_" + ins.kind.lower(),
            "falsification_note": ins.falsifier[:300],
            # 확률은 신뢰도에서 옮기지 않는다 - LOW/MEDIUM/HIGH 를 0.3/0.5/0.7
            # 로 바꾸면 있지도 않은 정밀도를 만들어낸다. Brier 는 실제 확률을
            # 낸 주장에만 쓴다.
            "probability": None,
            "probability_method": None,
            "symbol": symbol,
            "seq": i,
        })
    return out


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _base(**kw) -> dict:
    d = {"kind": "CROSS_SIGNAL",
         "claim": "추세는 형성됐는데 폭이 안 따라와 상승의 저변이 좁다",
         "supporting_fact_ids": ["f1", "f2"],
         "source_nodes": ["technical", "regime"],
         "falsifier": "5거래일 내 상승종목비율이 55% 를 넘으면 이 해석은 틀렸다",
         "horizon_days": 5}
    d.update(kw)
    return d


def _check_single_reference_is_not_insight():
    """Fact 하나만 가리키면 해석이 아니라 재진술이다."""
    for bad in (["f1"], ["f1", "f1"]):
        try:
            Insight(**_base(supporting_fact_ids=bad))
        except ValidationError:
            continue
        raise AssertionError(f"단일 참조가 통과했다: {bad}")


def _check_single_node_is_not_crossing():
    """같은 분석가 Fact 둘은 한 관점이다 - 가로지른 것이 아니다."""
    try:
        Insight(**_base(source_nodes=["technical", "technical"]))
    except ValidationError:
        return
    raise AssertionError("한 분석가만으로 교차 해석이 통과했다")


def _check_vacuous_falsifier_rejected():
    """틀릴 수 없는 문장은 분석이 아니다."""
    for bad in ("해당 없음", "없다", "알 수 없음", "N/A"):
        try:
            Insight(**_base(falsifier=bad))
        except ValidationError:
            continue
        raise AssertionError(f"회피 반증조건이 통과했다: {bad!r}")
    # 길이만 채운 회피도 잡아야 한다
    try:
        Insight(**_base(falsifier="이 해석을 반증할 방법은 없다"))
    except ValidationError:
        pass
    else:
        raise AssertionError("길이만 채운 회피가 통과했다")


def _check_good_insight_passes():
    """정상 해석은 통과해야 한다 - 가드가 정상까지 막으면 아무도 안 쓴다."""
    ins = Insight(**_base())
    assert ins.confidence == "MEDIUM"
    assert ins.horizon_days == 5


def _check_reference_integrity():
    ok, rej = validate_insights(
        [_base(), _base(supporting_fact_ids=["f1", "없는거"]),
         _base(source_nodes=["technical", "없는분석가"])],
        fact_ids={"f1", "f2"},
        known_nodes={"technical", "regime", "fundamental"})
    assert len(ok) == 1, ok
    assert len(rej) == 2, rej
    # ▶ 조용히 버리지 않는다 - 왜 인사이트가 안 나오는지 보여야 한다
    assert any("없는 Fact" in r["reason"] for r in rej), rej
    assert any("없는 분석가" in r["reason"] for r in rej), rej


def _check_contract_violation_keeps_reason():
    ok, rej = validate_insights([{"kind": "CROSS_SIGNAL"}],
                                fact_ids=set(), known_nodes=set())
    assert not ok and rej and rej[0]["reason"], rej


def _check_claims_do_not_invent_probability():
    """LOW/MEDIUM/HIGH 를 확률로 바꾸지 않는다 - 없는 정밀도를 만드는 짓이다."""
    claims = to_claims([Insight(**_base(confidence="HIGH"))], symbol="005380")
    assert len(claims) == 1
    c = claims[0]
    assert c["probability"] is None and c["probability_method"] is None, c
    assert c["kind"] == "INSIGHT_CROSS_SIGNAL"
    assert c["falsification_note"], "반증 조건이 채점 행에 안 실렸다"
    assert c["method_key"] == "insight_cross_signal"
    assert c["horizon_days"] == 5


def _check_extra_field_forbidden():
    try:
        Insight(**_base(뭔가="추가"))
    except ValidationError:
        return
    raise AssertionError("모르는 필드가 통과했다")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{CONTRACT_VERSION} 자체 점검 (LLM·API 없음)")
    _check_single_reference_is_not_insight(); print("  단일 참조 거부          OK")
    _check_single_node_is_not_crossing();     print("  한 관점 거부            OK")
    _check_vacuous_falsifier_rejected();      print("  회피 반증조건 거부      OK")
    _check_good_insight_passes();             print("  정상 해석 통과          OK")
    _check_reference_integrity();             print("  참조 무결성+사유        OK")
    _check_contract_violation_keeps_reason(); print("  거부 사유 보존          OK")
    _check_claims_do_not_invent_probability();print("  확률 날조 안 함         OK")
    _check_extra_field_forbidden();           print("  미지 필드 거부          OK")
    print("Insight 계약 8개 영역 통과.")
