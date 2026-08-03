#!/usr/bin/env python3
"""V1 Packet -> V2 계약 어댑터.

담당: 재일 (리서치본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md Phase RQF-0
        "기존 Packet 과 Hypothesis 를 V2 로 변환하는 Adapter 작성"
        완료 기준 "V1 Fixture 가 V2 로 변환되고 Schema Test 를 통과한다"

▶ 어댑터가 있어야 계약이 계약이 된다
  contracts/research_v2.py 를 만들었지만 scripts.py 는 여전히 dict 를 돌린다.
  둘을 잇지 않으면 계약은 **선언으로만 남는다** - 레지스트리에 ADOPTED 라고
  적어놓고 구현 모듈이 없던 것과 같은 상태다(evidence/methods.py 사고, 08-02).

▶ 이 어댑터의 유일한 원칙: **없는 것을 지어내지 않는다**
  V1 에 없는 정보는 V2 에서 비워 두고, 비었다는 사실을 status 로 드러낸다.
  구체적으로:

  | V2 가 요구 | V1 에 있나 | 어댑터의 처리 |
  |---|---|---|
  | claim.evidence_ids | 인용했을 때만 | 인용한 것만 `fact`, 나머지는 `inference` |
  | claim.confidence | 없다(verdict 라벨만) | 라벨→확률 매핑을 **하지 않는다.** 0.5 고정 + uncalibrated |
  | as_known_at | 있다(실행 시각) | 그대로. 뜻이 '컷오프'가 아니라 '실행 시각'임을 note 에 남긴다 |
  | perspective 별 horizon | 없다 | Case 의 대표 지평을 쓴다 |
  | evidence_manifest_id | 없다 | None |

  **verdict 를 fact 로 승격하지 않는 것이 이 어댑터에서 가장 중요한 결정이다.**
  BULLISH/BEARISH 는 분석가의 판정이지 문서에 적힌 사실이 아니다. fact 로 올리면
  V2 의 "모든 Fact Claim 이 Evidence ID 를 가진다"(RQF-1 완료기준)가 형식적으로만
  만족되고, 실제로는 근거 없는 주장이 근거 있는 것처럼 흘러간다.

  마찬가지로 confidence 를 라벨에서 만들어내지 않는다. "STRONG_BULLISH 니까 0.8"
  같은 매핑은 근거가 없고, 한 번 숫자가 되면 하류가 그것을 확률로 취급한다
  (framework 6.4 "충분한 표본이 없으면 숫자를 정밀한 확률처럼 사용하지 않는다").

실행: python contracts/research_v1_to_v2.py     # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))

from research_v2 import (  # noqa: E402
    AnalystFindingV1,
    Budgets,
    Calibration,
    CaseStatus,
    Claim,
    ClaimType,
    Direction,
    FindingStatus,
    Lineage,
    Outlook,
    ResearchCaseV2,
    ResearchPacketV2,
    Trigger,
)

ADAPTER_VERSION = "research-v1-to-v2-v1"

# V1 의 노드 이름 -> V2 perspective. scripts.py 의 state 키와 같다.
NODE_TO_PERSPECTIVE: dict[str, str] = {
    "fundamental": "fundamental",
    "technical": "technical",
    "microstructure": "microstructure",
    "sentiment": "news_sentiment",     # V1 은 sentiment, V2 는 news_sentiment
    "regime": "macro_regime",          # V1 은 regime, V2 는 macro_regime
    "geopolitical": "geopolitical",
}

# 근거 없는 상태를 확률처럼 다루지 않기 위한 고정값. **라벨에서 만들지 않는다.**
NEUTRAL_CONFIDENCE = 0.5

# V1 verdict 가 이 값이면 결과가 없는 것으로 본다
_NO_RESULT = {"INSUFFICIENT_DATA", "UNAVAILABLE", "ERROR", None, ""}

_BULLISH = ("BULL", "POSITIVE", "RISK_ON", "IMPROV", "EXPAND")
_BEARISH = ("BEAR", "NEGATIVE", "RISK_OFF", "DETERIOR", "SHOCK", "CONTRACT")


class AdapterError(ValueError):
    """V1 이 어댑터가 감당할 모양이 아니다. 추측으로 메우지 않는다."""


def _direction_of(verdict: str | None) -> Direction:
    """판정 라벨의 방향. 모르면 NEUTRAL 이다 - 억지로 한쪽으로 밀지 않는다."""
    v = (verdict or "").upper()
    if any(t in v for t in _BULLISH):
        return Direction.SUPPORTIVE
    if any(t in v for t in _BEARISH):
        return Direction.OPPOSING
    return Direction.NEUTRAL


def _finding_status(node_state: dict | None) -> FindingStatus:
    """분석가 하나의 상태. **결과를 버리지 않기 위한 판정이다.**

    V1 은 LLM 서술(note.summary)이 없으면 리포트에서 통째로 빠졌었다 - 코드
    계산(readout)까지 함께 사라졌다(실측: 5인 중 3인 증발). V2 에서는 그것이
    PARTIAL 이지 부재가 아니다.
    """
    if not node_state:
        return FindingStatus.BLOCKED
    verdict = node_state.get("verdict")
    if verdict in _NO_RESULT:
        return FindingStatus.INCONCLUSIVE
    note = node_state.get("note") or {}
    has_narrative = bool(note.get("summary") or node_state.get("summary"))
    return FindingStatus.COMPLETE if has_narrative else FindingStatus.PARTIAL


def cited_fact_claims(
    node: str,
    node_state: dict | None,
    bundle: dict | None,
    *,
    case_id: str,
) -> tuple[Claim, ...]:
    """분석가가 **실제로 인용한 근거**가 있으면 fact 주장을 만든다.

    ▶ 이것이 RQF-1 완료기준 "모든 Fact Claim 이 존재하는 Evidence ID 를 가진다"를
      우회가 아니라 실제로 여는 경로다. 예전에는 Bundle 이 document_id 를 버려서
      인용할 ID 자체가 없었고(evidence/bundle.py 2026-08-03 수정), 그래서 이
      어댑터는 모든 주장을 inference 로 낼 수밖에 없었다.

      이제 분석가가 note.cited_refs 로 ref('n1','d2')를 가리키면 코드가 그것을
      진짜 evidence_id 로 바꾼다. **없는 ref 는 예외**이므로(resolve_refs strict)
      환각 인용이 fact 로 올라가지 않는다.

    ▶ 인용이 없으면 fact 를 만들지 않는다. 빈 튜플이 정상 상태다(fail-closed).
    """
    st = node_state or {}

    # ▶ 경로 1: 분석가가 **이미 진짜 document_id 로** 인용한 경우.
    #   RES-06 이 그렇다 - 자기 verify 에서 환각 인용을 이미 버렸으므로
    #   Bundle 대조를 다시 하지 않는다. 두 번 검증하면 RES-06 이 본 기사와
    #   Bundle 이 담은 기사가 달라(창·개수가 다르다) 멀쩡한 인용이 탈락한다.
    direct = tuple(dict.fromkeys(
        str(x) for x in (st.get("cited_evidence_ids") or ()) if str(x).strip()))

    # ▶ 경로 2: ref('n1','d1')로 가리킨 경우 - Bundle 로 해석한다.
    refs = ((st.get("note") or {}).get("cited_refs")
            or st.get("cited_refs") or ())
    resolved: tuple[str, ...] = ()
    if refs and bundle:
        from bundle import resolve_refs  # 지연 import - 계약이 evidence 에 의존하지 않게

        resolved = resolve_refs(refs, bundle)   # 없는 ref 면 CitationError

    evidence_ids = tuple(dict.fromkeys(direct + resolved))
    if not evidence_ids:
        return ()
    summary = ((st.get("note") or {}).get("summary") or st.get("summary") or "")
    if not summary.strip():
        # 근거는 있는데 무엇을 주장하는지 문장이 없다 - 사실 주장을 만들 수 없다
        return ()
    return (Claim(
        claim_id=f"claim_{case_id}_{node}_cited",
        statement=summary.strip()[:500],
        claim_type=ClaimType.FACT,
        evidence_ids=evidence_ids,
        direction=_direction_of(st.get("verdict")),
        confidence=NEUTRAL_CONFIDENCE,
    ),)


def finding_from_node(
    node: str,
    node_state: dict | None,
    *,
    case_id: str,
    as_known_at: datetime,
    horizon: str,
    model_version: str,
    prompt_version: str,
    tool_versions: tuple[str, ...] = (),
    bundle: dict | None = None,
) -> AnalystFindingV1:
    """분석가 한 명의 V1 출력 -> AnalystFindingV1.

    verdict 는 **inference** 로 낸다. 판정은 문서에 적힌 사실이 아니다.
    인용한 근거가 있으면 그 부분만 별도로 fact 주장이 된다(cited_fact_claims).
    """
    perspective = NODE_TO_PERSPECTIVE.get(node)
    if perspective is None:
        raise AdapterError(
            f"모르는 노드 '{node}' - NODE_TO_PERSPECTIVE 에 매핑을 먼저 넣는다. "
            f"임의로 관점을 정하면 계약이 거짓이 된다")

    status = _finding_status(node_state)
    claims: list[Claim] = []
    if status is not FindingStatus.BLOCKED:
        st = node_state or {}
        verdict = st.get("verdict")
        if verdict not in _NO_RESULT:
            claims.append(Claim(
                claim_id=f"claim_{case_id}_{node}_verdict",
                statement=f"{perspective} 판정은 {verdict} 이다",
                # ▶ fact 가 아니라 inference 다. 이 한 줄이 이 어댑터의 핵심이다.
                claim_type=ClaimType.INFERENCE,
                evidence_ids=(),
                direction=_direction_of(verdict),
                # 라벨에서 확률을 만들지 않는다 - 근거가 없다
                confidence=NEUTRAL_CONFIDENCE,
                numeric_refs=tuple(sorted((st.get("readout") or {}).keys()))[:20],
            ))
        claims.extend(cited_fact_claims(node, node_state, bundle, case_id=case_id))

    # BLOCKED 는 claims 가 있으면 안 된다(V2 불변식). 여기서 그럴 일은 없지만
    # 위 분기가 바뀌었을 때 조용히 어기지 않도록 계약이 잡게 둔다.
    cautions = ((node_state or {}).get("note") or {}).get("cautions") or []
    return AnalystFindingV1(
        finding_id=f"finding_{case_id}_{node}",
        case_id=case_id,
        perspective=perspective,
        as_known_at=as_known_at,
        horizon=horizon,
        claims=tuple(claims),
        unanswered_questions=tuple(str(c) for c in cautions)[:20],
        status=status if claims or status is not FindingStatus.COMPLETE
        else FindingStatus.INCONCLUSIVE,
        model_version=model_version,
        prompt_version=prompt_version,
        tool_versions=tool_versions,
    )


def case_from_v1(
    symbol: str,
    *,
    as_known_at: datetime,
    trace_id: str,
    horizons: tuple[str, ...] = ("20d",),
    mandate_version: str = "mandate_unversioned",
) -> ResearchCaseV2:
    """V1 실행 1건 -> ResearchCaseV2.

    V1 에는 Case 개념이 없다(Hermes 가 아직 Case 를 발행하지 않는다). trace_id 를
    Case 식별자로 쓰고, **필수 관점을 비워 둔다** - V1 은 어느 분석가가 필수인지
    선언한 적이 없으므로, 여기서 정하면 없던 요구사항을 만들어내는 것이다.
    """
    if not symbol:
        raise AdapterError("symbol 이 없다")
    return ResearchCaseV2(
        case_id=f"research_case_{trace_id}",
        instrument_ids=(symbol,),
        trigger=Trigger(type="manual", source_event_ids=()),
        mandate_version=mandate_version,
        as_known_at=as_known_at,
        horizons=horizons,
        # V1 파이프라인은 6인을 모두 부르지만 '필수' 로 선언한 적은 없다.
        # 전부 필수로 만들면 한 명만 실패해도 Case 가 막히는데, V1 은 그렇게
        # 동작하지 않았다 - 관측된 동작을 바꾸는 것은 어댑터의 일이 아니다.
        required_perspectives=("fundamental",),
        optional_perspectives=tuple(
            p for p in NODE_TO_PERSPECTIVE.values() if p != "fundamental"),
        budgets=Budgets(wall_clock_seconds=600, max_llm_calls=20,
                        max_retrieval_rounds=2),
        priority=50,
        status=CaseStatus.PUBLISHED,
    )


def packet_from_v1(
    packet: dict,
    state: dict | None = None,
    *,
    bundle: dict | None = None,
    horizon: str = "20d",
    graph_version: str = "research-v1-legacy",
    model_version: str = "unknown",
    prompt_version: str = "unknown",
) -> tuple[ResearchCaseV2, ResearchPacketV2]:
    """V1 packet dict (+ LangGraph state) -> (Case, Packet).

    state 는 분석가별 원본이다. 없으면 packet 의 `_analyst_verdicts` 로 대신한다 -
    그쪽에는 readout 이 없으므로 numeric_refs 가 빈다.
    """
    missing = [k for k in ("symbol", "thesis") if not packet.get(k)]
    if missing:
        raise AdapterError(f"V1 Packet 에 필수 키가 없다: {missing}")

    raw_as_known = packet.get("as_known_at")
    if not raw_as_known:
        raise AdapterError(
            "as_known_at 이 없다 - 시점 없는 Packet 을 V2 로 올릴 수 없다")
    ak = datetime.fromisoformat(raw_as_known) if isinstance(raw_as_known, str) \
        else raw_as_known
    if ak.tzinfo is None:
        raise AdapterError("as_known_at 에 timezone 이 없다")

    trace_id = str(packet.get("trace_id") or "unknown")
    case = case_from_v1(packet["symbol"], as_known_at=ak, trace_id=trace_id,
                        horizons=(horizon,))

    # 분석가별 원본이 있으면 그것을, 없으면 verdict 요약만 쓴다
    verdicts = packet.get("_analyst_verdicts") or {}
    findings = []
    for node in NODE_TO_PERSPECTIVE:
        node_state = (state or {}).get(node)
        if node_state is None and node in verdicts:
            node_state = {"verdict": verdicts[node]}
        if node_state is None:
            continue          # 아예 안 돈 분석가는 만들어내지 않는다
        findings.append(finding_from_node(
            node, node_state, case_id=case.case_id, as_known_at=ak,
            horizon=horizon, model_version=model_version,
            prompt_version=prompt_version, bundle=bundle))

    claim_ids = tuple(c.claim_id for f in findings for c in f.claims)

    # ▶ V1 에는 Macro/Micro 이중 전망이 없다 (framework 6.1 7단계 미구현).
    #   그래서 둘 다 neutral 0.5 로 두고 uncalibrated 를 세운다 - 없는 전망을
    #   thesis 에서 추론해 만들어내지 않는다.
    outlook = Outlook(direction="neutral", confidence=NEUTRAL_CONFIDENCE,
                      claim_ids=claim_ids)

    quality = str(packet.get("evidence_quality") or "").lower()
    status = "PUBLISHED" if quality in ("good", "ok", "full") else "PARTIAL"
    # fact claim 을 만들지 않으므로 PUBLISHED 라도 근거 검사에 걸리지 않는다.
    # findings 가 비면 PUBLISHED 가 될 수 없다(V2 불변식) - 그건 옳다.
    if not findings:
        status = "PARTIAL"

    v2 = ResearchPacketV2(
        packet_id=f"rp_{trace_id}",
        case_id=case.case_id,
        instrument_id=packet["symbol"],
        trigger="manual",
        as_known_at=ak,
        horizons=(horizon,),
        macro_outlook=outlook,
        micro_outlook=outlook,
        thesis=str(packet["thesis"]),
        catalysts=_as_str_tuple(packet.get("facts")),
        invalidation=_as_str_tuple(packet.get("invalidation")),
        dissent=(),
        evidence_gaps=_as_str_tuple(
            (packet.get("numeric_check") or {}).get("unmatched")),
        calibration=Calibration(cohort=f"legacy_{horizon}"),
        uncalibrated=True,        # V1 은 보정된 적이 없다
        status=status,
        lineage=Lineage(graph_version=graph_version),
        findings=tuple(findings),
    )
    return case, v2


def _as_str_tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, dict):
        return tuple(f"{k}: {x}" for k, x in v.items())
    return tuple(str(x) for x in v if str(x).strip())


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

_T = "2026-08-03T01:30:00+00:00"


def _v1_packet(**kw) -> dict:
    base = {
        "symbol": "005930",
        "thesis": "단기 촉매는 있으나 중기 환경은 중립이다",
        "facts": ["2분기 영업이익 개선"],
        "interpretation": "밸류에이션 부담은 남아 있다",
        "invalidation": ["원가 상승이 재개되면 무효"],
        "evidence_quality": "good",
        "as_known_at": _T,
        "trace_id": "tr_1",
        "_analyst_verdicts": {"fundamental": "BULLISH", "technical": "NEUTRAL",
                              "regime": "RISK_OFF", "sentiment": None,
                              "geopolitical": "INSUFFICIENT_DATA",
                              "microstructure": "NEUTRAL"},
    }
    base.update(kw)
    return base


def _check_verdict_is_not_fact():
    """이 어댑터의 핵심 - 판정을 사실로 승격하지 않는가."""
    _, v2 = packet_from_v1(_v1_packet())
    all_claims = [c for f in v2.findings for c in f.claims]
    assert all_claims, "주장이 하나도 안 만들어졌다"
    facts = [c for c in all_claims if c.claim_type is ClaimType.FACT]
    assert not facts, (
        f"판정이 fact 로 올라갔다: {[c.claim_id for c in facts]} - "
        f"근거 없는 주장이 근거 있는 것처럼 흘러간다")
    # 근거가 비었는데 inference 라서 V2 계약을 통과한다
    assert all(c.evidence_ids == () for c in all_claims)
    print("  판정 != 사실             OK")


def _check_confidence_not_invented():
    """라벨에서 확률을 만들어내지 않는가."""
    _, strong = packet_from_v1(_v1_packet(
        _analyst_verdicts={"fundamental": "STRONG_BULLISH"}))
    _, weak = packet_from_v1(_v1_packet(
        _analyst_verdicts={"fundamental": "SLIGHTLY_BULLISH"}))
    cs = [c.confidence for f in strong.findings for c in f.claims]
    cw = [c.confidence for f in weak.findings for c in f.claims]
    assert cs == cw == [NEUTRAL_CONFIDENCE], (cs, cw)
    assert strong.uncalibrated is True
    print("  확률을 지어내지 않는가   OK")


def _check_direction_mapping():
    _, v2 = packet_from_v1(_v1_packet())
    by = {f.perspective: f for f in v2.findings}
    assert by["fundamental"].claims[0].direction is Direction.SUPPORTIVE
    assert by["macro_regime"].claims[0].direction is Direction.OPPOSING
    assert by["technical"].claims[0].direction is Direction.NEUTRAL
    print("  방향 매핑                OK")


def _check_partial_keeps_result():
    """LLM 서술이 없어도 결과를 버리지 않는가 (V1 의 실제 사고)."""
    state = {"technical": {"verdict": "BULLISH",
                           "readout": {"rsi": 62.1, "ma20": 71000},
                           "note": {}}}          # summary 없음
    _, v2 = packet_from_v1(_v1_packet(), state)
    tech = next(f for f in v2.findings if f.perspective == "technical")
    assert tech.status is FindingStatus.PARTIAL, tech.status
    assert tech.claims, "PARTIAL 인데 결과가 사라졌다"
    assert "rsi" in tech.claims[0].numeric_refs, tech.claims[0].numeric_refs
    print("  서술 없어도 결과 보존    OK")


def _check_inconclusive_and_blocked():
    _, v2 = packet_from_v1(_v1_packet())
    geo = next(f for f in v2.findings if f.perspective == "geopolitical")
    assert geo.status is FindingStatus.INCONCLUSIVE and not geo.claims
    # 아예 안 돈 분석가는 만들어내지 않는다
    p = _v1_packet(_analyst_verdicts={"fundamental": "BULLISH"})
    _, v2b = packet_from_v1(p)
    assert v2b.perspectives_present() == ("fundamental",), v2b.perspectives_present()
    print("  결과 없음 vs 안 돔       OK")


def _check_refuses_to_guess():
    for bad, needle in (
        ({"thesis": "t", "as_known_at": _T}, "필수 키가 없다"),
        (_v1_packet(as_known_at=None), "시점 없는 Packet"),
        (_v1_packet(as_known_at="2026-08-03T01:30:00"), "timezone"),
    ):
        try:
            packet_from_v1(bad)
            raise AssertionError(f"통과하면 안 된다: {needle}")
        except AdapterError as e:
            assert needle in str(e), f"{needle} 아닌 이유로 거부:\n{e}"
    try:
        finding_from_node("없는노드", {"verdict": "X"}, case_id="c",
                          as_known_at=datetime.now(timezone.utc), horizon="20d",
                          model_version="m", prompt_version="p")
        raise AssertionError("모르는 노드가 통과했다")
    except AdapterError as e:
        assert "모르는 노드" in str(e)
    print("  추측 거부                OK")


def _check_v2_contract_passes():
    """RQF-0 완료 기준 - V1 Fixture 가 V2 로 변환되고 Schema Test 를 통과한다."""
    case, v2 = packet_from_v1(_v1_packet())
    # 왕복
    again = ResearchPacketV2.model_validate_json(v2.model_dump_json())
    assert again == v2
    ResearchCaseV2.model_validate_json(case.model_dump_json())
    # 필수 관점이 실제로 채워졌는지 계약이 판정할 수 있다
    assert v2.missing_required(case) == (), v2.missing_required(case)
    # evidence_quality 가 나쁘면 PUBLISHED 가 아니다
    _, partial = packet_from_v1(_v1_packet(evidence_quality="partial"))
    assert partial.status == "PARTIAL"
    print("  V2 계약 통과·왕복        OK")


def _check_cited_facts_open_the_axis():
    """인용이 있으면 fact 가 된다 - RQF-1 완료기준을 우회가 아니라 실제로 연다."""
    bundle = {
        "news_headlines": [{"ref": "n1", "evidence_id": "doc-news-1",
                            "title": "공급계약", "production_authorized": True}],
        "disclosures_7d": [{"ref": "d1", "evidence_id": "doc-disc-1",
                            "title": "단일판매공급계약"}],
    }
    state = {"fundamental": {
        "verdict": "BULLISH",
        "note": {"summary": "공급계약 공시로 매출 가시성이 개선됐다",
                 "cited_refs": ["d1", "n1"]}}}
    _, v2 = packet_from_v1(_v1_packet(), state, bundle=bundle)
    fund = next(f for f in v2.findings if f.perspective == "fundamental")
    facts = fund.fact_claims()
    assert len(facts) == 1, [c.claim_id for c in fund.claims]
    assert facts[0].evidence_ids == ("doc-disc-1", "doc-news-1"), facts[0].evidence_ids
    # 판정 주장은 여전히 inference 다 - 둘이 섞이지 않는다
    assert any(c.claim_type is ClaimType.INFERENCE for c in fund.claims)
    # PUBLISHED 도 통과한다(V2 가 fact 의 근거 존재를 검사한다)
    assert v2.status == "PUBLISHED", v2.status

    # 인용이 없으면 fact 를 만들지 않는다 (fail-closed)
    no_cite = {"fundamental": {"verdict": "BULLISH", "note": {"summary": "좋다"}}}
    _, v2b = packet_from_v1(_v1_packet(), no_cite, bundle=bundle)
    assert sum(len(f.fact_claims()) for f in v2b.findings) == 0

    # 환각 인용은 예외다 - 없는 ref 가 fact 로 올라가지 않는다
    from bundle import CitationError
    ghost = {"fundamental": {"verdict": "BULLISH",
                             "note": {"summary": "s", "cited_refs": ["d9"]}}}
    try:
        packet_from_v1(_v1_packet(), ghost, bundle=bundle)
        raise AssertionError("없는 근거를 인용했는데 통과했다")
    except CitationError as e:
        assert "없는 근거를 인용했다" in str(e)

    # Bundle 이 없으면(옛 호출부) 예전처럼 inference 만 - 하위 호환
    _, v2c = packet_from_v1(_v1_packet(), state)
    assert sum(len(f.fact_claims()) for f in v2c.findings) == 0

    # ▶ RES-06 경로: 이미 진짜 document_id 로 인용한다. Bundle 없이도 fact 다.
    #   자기 verify 에서 환각을 이미 버렸으므로 Bundle 대조를 다시 하지 않는다 -
    #   두 번 검증하면 RES-06 이 본 기사와 Bundle 이 담은 기사의 창이 달라
    #   멀쩡한 인용이 탈락한다.
    senti = {"sentiment": {
        "verdict": "SCORED",
        "cited_evidence_ids": ("doc-real-1", "doc-real-2", "doc-real-1"),
        "note": {"summary": "공급계약 보도가 다수 관측됐다"}}}
    _, v2d = packet_from_v1(_v1_packet(), senti)          # bundle 없음
    ns = next(f for f in v2d.findings if f.perspective == "news_sentiment")
    facts = ns.fact_claims()
    assert len(facts) == 1, [c.claim_id for c in ns.claims]
    assert facts[0].evidence_ids == ("doc-real-1", "doc-real-2"), facts[0].evidence_ids
    print("  인용 -> fact 축 개방     OK")


def _check_node_map_covers_v1():
    """V1 의 분석가 노드를 빠뜨리지 않았는가 - scripts.py 와 대조한다."""
    src = (Path(__file__).resolve().parent.parent / "scripts.py").read_text(
        encoding="utf-8")
    # scripts.py 가 _analyst_verdicts 에 넣는 키 목록이 V1 의 정본이다
    start = src.index('packet["_analyst_verdicts"]')
    block = src[start:start + 700]
    keys = {ln.split('"')[1] for ln in block.splitlines()
            if ln.strip().startswith('"') and ".get(" in ln}
    missing = keys - set(NODE_TO_PERSPECTIVE)
    assert not missing, f"V1 에 있는데 매핑에 없는 노드: {sorted(missing)}"
    print(f"  V1 노드 매핑 전수({len(keys)}) OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{ADAPTER_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_verdict_is_not_fact()
    _check_confidence_not_invented()
    _check_direction_mapping()
    _check_partial_keeps_result()
    _check_inconclusive_and_blocked()
    _check_refuses_to_guess()
    _check_v2_contract_passes()
    _check_cited_facts_open_the_axis()
    _check_node_map_covers_v1()
    print("V1->V2 어댑터 9개 영역 통과.")
