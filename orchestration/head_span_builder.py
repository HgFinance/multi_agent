#!/usr/bin/env python3
"""카드 1장(부서장 턴) -> Langfuse span 트리. 순수 조립부라 네트워크가 없다.

## 계측 지점이 왜 여기 하나인가 (2026-08-27 실측으로 좁혀짐)

사용자 질의는 `/ui/ceo/ask` 하나로만 들어오고, 그 경로는 **CEO 루트 카드만 만든다**
(`apps/api/ceo.py` 머리말: "creates only the CEO root task. The CEO Supervisor owns
planning, department-task creation, QA, and final synthesis"). 그래서 CEO 턴도,
부서장 턴도, QA 턴도, 종합 턴도 전부 Kanban 카드로 dispatcher 를 지나간다.

한때 BFF 가 CLI 로 부서장을 부른다고 보고 공유 기록부·수집기를 설계했는데, 그
전제가 틀렸다 - BFF 컨테이너에는 `profiles/` 자체가 없고(실측), 부서 직접 질의
엔드포인트는 실사용 경로가 아니다. 전제가 깨지자 기록부·수집기·watermark·권한
문제가 전부 사라졌다. **지금 필요한 것은 dispatcher 안에서 자기 카드 하나를
span 으로 바꾸는 조립부뿐이다.**

## span 모양

    head.<부서>                     (span, 카드 전체 구간)
      ├─ codex.session              (generation, 토큰·비용)
      ├─ model.generate             (span)   ┐ messages.timestamp 로 그린
      ├─ tool.<이름>                (span)   │ 실측 waterfall
      └─ ...                                 ┘

토큰을 구간마다 쪼개지 않는다. `state.db` 는 세션 단위로만 재고(messages.token_count
는 전부 NULL 이었다), 없는 분해를 지어내면 그 숫자를 인용한 판단이 조용히 틀린다.
그래서 generation 1개가 세션 총량을 지고 `usage_scope: session` 을 단다.

## 부모-자식은 계산으로 붙는다

카드마다 wrapper 프로세스가 따로 돌고 서로를 모른다. 그래도 트리가 되는 이유는
span id 가 (root, 종류, task) 에서 파생되기 때문이다 - 부서 카드는 자기 부모의
span id 를 **루트 카드 id 만으로** 계산해 낸다. 전파가 필요 없다.

자체 점검: python orchestration/head_span_builder.py
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

try:
    from orchestration.hermes_session_usage import TurnUsage
    from orchestration.langfuse_otlp import build_span
    from orchestration.model_cost import UNPRICED, UnitRate, cost_details, cost_metadata
    from orchestration.trace_identity import span_id_for, trace_id_for
except ImportError:  # pragma: no cover - dispatcher wrapper 는 형제 모듈만 본다
    from hermes_session_usage import TurnUsage  # type: ignore[no-redef]
    from langfuse_otlp import build_span  # type: ignore[no-redef]
    from model_cost import (  # type: ignore[no-redef]
        UNPRICED,
        UnitRate,
        cost_details,
        cost_metadata,
    )
    from trace_identity import span_id_for, trace_id_for  # type: ignore[no-redef]

# 세션 매칭 여유. 자식이 뜨고 Hermes 가 세션 행을 쓰기까지의 간격만 흡수한다.
#
# ▶ 왜 '실행 구간 겹침'이 아니라 '시작 근접'인가 (2026-08-27 실측)
#   같은 부서 kanban 세션 433개 중 실행 **구간**이 겹치는 쌍이 34개였다(15.7%).
#   50초 도는 카드 중간에 시작한 다른 카드가 앞 카드 구간에 통째로 들어가기
#   때문이다. 반면 **시작 시각**이 1.5초 안에 붙은 쌍은 2개뿐이었다(0.46%).
#   자식은 자기 세션을 뜬 직후에 만드니, 봐야 할 것은 시작 시각이다.
MATCH_GRACE_MS = 1_500


def match_session_id(
    *,
    source: str,
    started_ms: int,
    candidates: Sequence[Mapping[str, Any]],
    grace_ms: int = MATCH_GRACE_MS,
) -> tuple[str, str]:
    """(session_id, confidence). confidence 는 window/ambiguous/missing.

    모호하면 **고르지 않는다.** 겹친 창에서 아무거나 집으면 한 카드의 토큰이 다른
    카드에 붙고, 그 오류는 나중에 찾아낼 방법이 없다. 빈 값이 낫다.
    """

    low, high = started_ms - grace_ms, started_ms + grace_ms
    hits = [
        candidate
        for candidate in candidates
        if str(candidate.get("source") or "") == source
        and low <= int(candidate.get("started_ms") or 0) <= high
    ]
    if not hits:
        return "", "missing"
    if len(hits) > 1:
        return "", "ambiguous"
    return str(hits[0].get("id") or ""), "window"


def card_span_id(*, root_id: str, task_id: str) -> str:
    """카드 1장의 span id. 루트 카드는 root_id == task_id 라 자기 자신이 된다."""

    return span_id_for(root_id, "card", task_id)


def build_card_spans(
    *,
    root_id: str,
    task_id: str,
    department: str,
    head_persona: str,
    profile: str,
    status: str,
    started_ms: int,
    ended_ms: int,
    usage: TurnUsage | None = None,
    session_confidence: str = "missing",
    rate: UnitRate = UNPRICED,
    attempts: int = 1,
    run_id: str = "",
    request_id: str = "",
    environment: str = "",
) -> list[dict[str, Any]]:
    """카드 1장을 span 목록으로. 발행은 호출자(langfuse_otlp.publish)가 한다."""

    trace_id = trace_id_for(root_id)
    if not trace_id:
        # 루트를 모르면 계측을 포기한다 - 빈 seed 로 만든 trace 에 모든 카드가
        # 쏟아지면 그건 관측이 아니라 오염이다.
        return []

    head_span_id = card_span_id(root_id=root_id, task_id=task_id)
    is_root_card = task_id == root_id
    parent_id = "" if is_root_card else card_span_id(root_id=root_id, task_id=root_id)

    shared = {
        "department": department,
        "profile": profile,
        "head_persona": head_persona,
        "role": "department_head",
        "root_id": root_id,
        "task_id": task_id,
        "request_id": request_id,
        "run_id": run_id,
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "raw_payloads_sent": False,
    }
    level = "DEFAULT" if status == "COMPLETED" else "ERROR"

    spans: list[dict[str, Any]] = [
        build_span(
            trace_id=trace_id,
            span_id=head_span_id,
            parent_span_id=parent_id,
            name=f"head.{department}",
            start_ms=started_ms,
            end_ms=ended_ms,
            observation_type="span",
            level=level,
            status_message=status,
            session_id=root_id,
            # 루트 카드가 trace 이름을 정한다. 부서 카드가 각자 이름을 쓰면
            # 같은 trace 이름이 카드 순서에 따라 바뀐다.
            trace_name=f"ceo.workflow.{root_id}" if is_root_card else "",
            environment=environment,
            metadata={
                **shared,
                "trace_kind": "department_head",
                "observation_unit": "card",
                "status": status,
                "latency_scope": "card_execution",
                "latency_available": True,
                "session_id": (usage.session_id if usage else ""),
                "hermes_session_id": (usage.session_id if usage else ""),
                # 세션을 어떻게 찾았는지를 남긴다 - 운영에서 손실률이 그대로 측정된다.
                "token_source": (
                    f"state.db:{session_confidence}" if usage else session_confidence
                ),
            },
        )
    ]

    if usage is not None:
        total_tokens = usage.total_tokens
        usage_details = {
            key: value
            for key, value in (
                ("input", usage.input_tokens),
                ("output", usage.output_tokens),
                ("cache_read", usage.cache_read_tokens),
                ("cache_write", usage.cache_write_tokens),
                ("reasoning", usage.reasoning_tokens),
                ("total", total_tokens),
            )
            if value is not None
        }
        spans.append(
            build_span(
                trace_id=trace_id,
                span_id=span_id_for(root_id, "generation", task_id, usage.session_id),
                parent_span_id=head_span_id,
                name="codex.session",
                start_ms=usage.started_ms or started_ms,
                end_ms=usage.ended_ms or ended_ms,
                observation_type="generation",
                model_name=usage.model_name,
                usage_details=usage_details or None,
                cost_details=cost_details(total_tokens=total_tokens, rate=rate),
                environment=environment,
                metadata={
                    **shared,
                    **cost_metadata(rate),
                    "observation_unit": "session",
                    "provider": usage.billing_provider,
                    "model_name": usage.model_name,
                    "hermes_session_id": usage.session_id,
                    # 토큰이 카드 단위가 아니라 **세션 단위**라는 사실을 숫자 옆에
                    # 붙여 둔다(messages.token_count 는 전부 NULL 이었다).
                    "usage_complete": usage.usage_complete,
                    "status": status,
                },
            )
        )

        for segment in usage.segments:
            spans.append(
                build_span(
                    trace_id=trace_id,
                    span_id=span_id_for(
                        root_id, "segment", task_id, usage.session_id, segment.index
                    ),
                    parent_span_id=head_span_id,
                    name=segment.name,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    observation_type="tool" if segment.kind == "tool" else "span",
                    environment=environment,
                    metadata={
                        **shared,
                        "observation_unit": segment.kind,
                        "tool_name": (
                            segment.name.split("tool.", 1)[-1]
                            if segment.kind == "tool"
                            else ""
                        ),
                        "tool_call_index": segment.index,
                        "tool_timing_source": "hermes-session-messages",
                        "latency_scope": "segment",
                    },
                )
            )
    return spans


if __name__ == "__main__":
    from decimal import Decimal

    try:
        from orchestration.hermes_session_usage import TurnSegment
        from orchestration.langfuse_otlp import (
            ATTR_COST_DETAILS,
            ATTR_USAGE_DETAILS,
        )
        from orchestration.model_cost import amortized_rate
    except ImportError:  # pragma: no cover
        from hermes_session_usage import TurnSegment  # type: ignore[no-redef]
        from langfuse_otlp import (  # type: ignore[no-redef]
            ATTR_COST_DETAILS,
            ATTR_USAGE_DETAILS,
        )
        from model_cost import amortized_rate  # type: ignore[no-redef]

    import json

    # 실측 카드 1장(research, 51초, 11 도구호출)을 본떠 조립한다.
    segments = (
        TurnSegment("model", "model.generate", 1_787_795_186_400, 1_787_795_190_000, 0),
        TurnSegment("tool", "tool.research.evidence.search", 1_787_795_190_000, 1_787_795_191_200, 1),
        TurnSegment("model", "model.generate", 1_787_795_191_200, 1_787_795_237_000, 2),
    )
    usage = TurnUsage(
        session_id="20260827_014614_a2ee73", source="kanban", model_name="gpt-5.6-luna",
        started_ms=1_787_795_186_365, ended_ms=1_787_795_237_554, end_reason="",
        message_count=21, tool_call_count=11, api_call_count=12,
        input_tokens=64449, output_tokens=1590, cache_read_tokens=180_000,
        cache_write_tokens=0, reasoning_tokens=900,
        billing_provider="openai-codex", billing_mode="subscription_included",
        cost_status="included", cost_source="none", system_prompt_hash="3919c914",
        parent_session_id="", segments=segments,
    )
    rate = amortized_rate(invoice_usd="200", observed_tokens=376_485_465, window_label="2026-08")

    spans = build_card_spans(
        root_id="t_aed63d47", task_id="t_6a665d49", department="research",
        head_persona="autonomous-quant-researcher", profile="research-department",
        status="COMPLETED", started_ms=1_787_795_186_000, ended_ms=1_787_795_237_600,
        usage=usage, session_confidence="window", rate=rate, attempts=1, run_id="7",
        request_id="req-1",
    )

    # 1. head + generation + 구간 3개.
    assert len(spans) == 5, len(spans)
    head, generation, *segment_spans = spans
    assert head["name"] == "head.research"
    assert generation["name"] == "codex.session"
    assert [s["name"] for s in segment_spans] == [
        "model.generate", "tool.research.evidence.search", "model.generate"
    ]

    # 2. 부서 카드는 루트 카드 span 의 자식이고, 그 부모 id 는 **루트 id 만으로**
    #    계산된다 - 다른 wrapper 프로세스와 아무것도 주고받지 않는다.
    assert head["parentSpanId"] == card_span_id(root_id="t_aed63d47", task_id="t_aed63d47")
    for child in spans[1:]:
        assert child["parentSpanId"] == head["spanId"]
        assert child["traceId"] == head["traceId"]

    # 3. 루트 카드 자신은 부모가 없고 trace 이름을 정한다.
    root_spans = build_card_spans(
        root_id="t_aed63d47", task_id="t_aed63d47", department="ceo",
        head_persona="executive-orchestrator", profile="ceo-agent",
        status="COMPLETED", started_ms=1, ended_ms=2,
    )
    assert "parentSpanId" not in root_spans[0]
    assert len(root_spans) == 1  # usage 가 없으면 head span 하나뿐이다

    # 4. 토큰은 세션 총량으로 한 번만 실린다(구간에 쪼개지 않는다).
    packed = {a["key"]: a["value"] for a in generation["attributes"]}
    usage_sent = json.loads(packed[ATTR_USAGE_DETAILS]["stringValue"])
    assert usage_sent["input"] == 64449 and usage_sent["output"] == 1590
    assert usage_sent["total"] == 64449 + 1590 + 180_000 + 0 + 900
    for span in segment_spans:
        keys = {a["key"] for a in span["attributes"]}
        assert ATTR_USAGE_DETAILS not in keys and ATTR_COST_DETAILS not in keys

    # 5. 비용은 상각치이고 라벨이 함께 나간다.
    cost_sent = json.loads(packed[ATTR_COST_DETAILS]["stringValue"])
    assert cost_sent["total"] > 0
    assert '"cost_basis":"amortized_subscription"' in json.dumps(
        packed["langfuse.observation.metadata"]["stringValue"], ensure_ascii=False
    ).replace("\\", "")

    # 6. 단가가 없으면 비용 자체가 안 나간다(0 을 만들지 않는다).
    unpriced = build_card_spans(
        root_id="t_aed63d47", task_id="t_6a665d49", department="research",
        head_persona="x", profile="research-department", status="COMPLETED",
        started_ms=1, ended_ms=2, usage=usage, session_confidence="window",
    )
    assert ATTR_COST_DETAILS not in {a["key"] for a in unpriced[1]["attributes"]}

    # 7. 루트를 모르면 아무것도 만들지 않는다.
    assert build_card_spans(
        root_id="", task_id="t_1", department="research", head_persona="x",
        profile="p", status="COMPLETED", started_ms=1, ended_ms=2,
    ) == []

    # 8. 세션 매칭: 시작 근접만 본다.
    candidates = [
        {"id": "s-a", "source": "kanban", "started_ms": 1_787_795_186_365},
        {"id": "s-b", "source": "kanban", "started_ms": 1_787_795_206_000},  # +20s
        {"id": "s-c", "source": "cli", "started_ms": 1_787_795_186_400},
    ]
    assert match_session_id(
        source="kanban", started_ms=1_787_795_186_000, candidates=candidates
    ) == ("s-a", "window")
    assert match_session_id(
        source="kanban", started_ms=1_787_795_186_000,
        candidates=candidates + [{"id": "s-d", "source": "kanban", "started_ms": 1_787_795_186_900}],
    ) == ("", "ambiguous")
    assert match_session_id(
        source="kanban", started_ms=1_700_000_000_000, candidates=candidates
    ) == ("", "missing")

    # 9. 매칭 실패도 계측을 멈추지 않는다 - head span 은 나가고 토큰만 빠진다.
    lost = build_card_spans(
        root_id="t_aed63d47", task_id="t_6a665d49", department="research",
        head_persona="x", profile="research-department", status="COMPLETED",
        started_ms=1, ended_ms=2, usage=None, session_confidence="ambiguous",
    )
    assert len(lost) == 1
    meta = json.loads(
        {a["key"]: a["value"] for a in lost[0]["attributes"]}[
            "langfuse.observation.metadata"
        ]["stringValue"]
    )
    assert meta["token_source"] == "ambiguous"

    print("ok - 부서장 카드 span 조립 점검 통과")
