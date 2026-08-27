#!/usr/bin/env python3
"""SDK 없이 Langfuse 에 span 을 보내는 전송 계층. 표준 라이브러리만 쓴다.

## 왜 SDK 를 안 쓰나 (2026-08-27 AWS 실측)

부서장 턴은 Hermes 컨테이너/dispatcher 안에서 끝난다. 그쪽에 langfuse SDK 를
넣으면 에이전트 런타임 표면이 넓어지고(의존성 26개), 이미 같은 이유로 LangSmith
쪽은 SDK 대신 HTTP 를 직접 쓰고 있다(scripts/hermes_worker_observability.py 머리말).

실측으로 확인한 것 - 운영 Langfuse 로 프로브 1건을 쏴서 UI 까지 확인했다:

    POST {host}/api/public/otel/v1/traces
    Authorization: Basic base64(public_key:secret_key)
    Content-Type: application/json
    -> HTTP 200, otel-ingestion-job 큐잉, authCheck.validKey=true

    UI 결과: w0.head.turn -> w0.generation 2단 트리, Session 배지,
             1,234 prompt -> 567 completion (Σ1,801), $0.0042

즉 **OTLP/JSON 을 그대로 받는다.** protobuf 도, SDK 도 필요 없다.

## 두 가지가 이 모듈의 존재 이유다

1. `cost_details` 를 직접 실어 보내면 Langfuse 모델 단가표를 거치지 않는다.
   부서장 모델(gpt-5.6-luna)은 Codex 구독 백엔드라 **토큰당 청구서가 없어서**
   단가표로 표현할 수 없다 - 비용은 "청구서 ÷ 그 창의 관측 토큰"으로 상각해
   우리가 계산한다. 실측에서 우리가 준 0.0042 가 trace 총액까지 그대로 합산됐다.

2. 프로세스가 갈려도 같은 trace 에 붙는다. id 는 trace_identity 가 만든다.

## 이 모듈이 하지 않는 것

프롬프트·응답·도구 인자·도구 결과를 **인자로 받지 않는다.** 옵션이 아니라 아예
받을 자리가 없다 - `.env.example` 3-2절이 지적한 대로 compliance 계열 Trace 에는
Mandate·제한종목 내용이 그대로 실릴 수 있고, "이번만" 켜는 스위치를 두면 언젠가
켜진다. e2e 그림은 span 이름·라벨·수치만으로 그린다.

자체 점검: python orchestration/langfuse_otlp.py
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

# 두 가지 경로로 뜬다 - 저장소 루트가 sys.path 에 있는 프로세스(BFF·supervisor)와,
# /app/repo/scripts 에서 형제 모듈만 보이는 dispatcher wrapper. `python
# orchestration/langfuse_otlp.py` 로 직접 돌릴 때도 후자와 같은 모양이 된다
# (sys.path[0] 이 orchestration/ 이라 패키지 이름이 안 잡힌다).
try:
    from orchestration.trace_identity import (
        is_span_id,
        is_trace_id,
        span_id_for,
        trace_id_for,
    )
except ImportError:  # pragma: no cover - 실행 위치에 따라 갈리는 배선
    from trace_identity import (  # type: ignore[no-redef]
        is_span_id,
        is_trace_id,
        span_id_for,
        trace_id_for,
    )

OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
DEFAULT_HOST = "https://cloud.langfuse.com"
DEFAULT_TIMEOUT_SECONDS = 3.0

# Langfuse 가 OTel span attribute 에서 읽어가는 키. langfuse 4.14.5
# `_client/attributes.py` 에서 그대로 옮겼다 - 이름을 지어내면 값이 조용히
# metadata 로만 떨어지고 UI 의 토큰·비용 집계에는 안 잡힌다(그게 제일 나쁜 실패다:
# 보내긴 보냈는데 아무 데도 안 쓰임).
ATTR_OBSERVATION_TYPE = "langfuse.observation.type"
ATTR_OBSERVATION_LEVEL = "langfuse.observation.level"
ATTR_STATUS_MESSAGE = "langfuse.observation.status_message"
ATTR_MODEL_NAME = "langfuse.observation.model.name"
ATTR_USAGE_DETAILS = "langfuse.observation.usage_details"
ATTR_COST_DETAILS = "langfuse.observation.cost_details"
ATTR_OBSERVATION_METADATA = "langfuse.observation.metadata"
ATTR_TRACE_NAME = "langfuse.trace.name"
ATTR_TRACE_METADATA = "langfuse.trace.metadata"
ATTR_SESSION_ID = "session.id"
ATTR_ENVIRONMENT = "langfuse.environment"

OBSERVATION_TYPES = frozenset({"span", "generation", "event", "tool", "agent"})
LEVELS = frozenset({"DEBUG", "DEFAULT", "WARNING", "ERROR"})

# 메타데이터 허용 목록. llm_observability._metric_metadata 와 같은 계약이고,
# **여기 없는 키는 조용히 버린다.** 화이트리스트가 아니라 블랙리스트로 두면
# 새 필드가 생길 때마다 원문 유출 여부를 사람이 판단해야 한다.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "observability_schema",
        "trace_kind",
        "observation_unit",
        "source",
        "department",
        "stage",
        "profile",
        "head_persona",
        "worker_id",
        "role",
        "status",
        "error_code",
        "provider",
        "model_name",
        "authority",
        "llm_involved",
        "request_id",
        "root_id",
        "task_id",
        "run_id",
        "attempt",
        "attempts",
        "retries",
        "session_id",
        "hermes_session_id",
        "tool_name",
        "tool_call_index",
        "tool_error",
        "tool_timing_source",
        "latency_scope",
        "latency_available",
        "token_source",
        "cost_basis",
        "cost_provisional",
        "usage_complete",
        "raw_payloads_sent",
    }
)

# 원문이 실릴 수 있는 자리. 실수로 넘어오면 버리는 게 아니라 **터뜨린다** -
# 조용히 버리면 "보냈는데 왜 안 보이지"로 며칠을 태우고, 무엇보다 다음 사람이
# 같은 실수를 반복한다.
FORBIDDEN_METADATA_KEYS = frozenset(
    {"input", "output", "prompt", "completion", "query", "answer", "body",
     "task_body", "text", "content", "messages", "tool_input", "tool_result",
     "arguments", "result"}
)


class RedactionError(ValueError):
    """원문이 실릴 수 있는 필드가 계측 payload 에 들어왔다."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """llm_observability.langfuse_enabled() 와 **같은 스위치**를 본다."""

    source = env if env is not None else os.environ
    tracing = str(source.get("LANGFUSE_TRACING", "") or "")
    return (
        tracing.casefold() in {"1", "true", "yes", "on"}
        and bool(str(source.get("LANGFUSE_PUBLIC_KEY", "") or "").strip())
        and bool(str(source.get("LANGFUSE_SECRET_KEY", "") or "").strip())
    )


def _attribute(key: str, value: Any) -> dict[str, Any] | None:
    """OTLP KeyValue 1개. 타입을 proto3 JSON 매핑대로 싣는다.

    int64 는 JSON 에서 **문자열**이다(proto3 JSON mapping). 숫자로 실으면 큰 값에서
    수신 측 파싱이 갈릴 수 있다.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)[:4000]}}


def _checked_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    leaked = FORBIDDEN_METADATA_KEYS.intersection(
        str(key).casefold() for key in metadata
    )
    if leaked:
        raise RedactionError(f"원문 가능성 필드가 계측에 들어옴: {sorted(leaked)}")
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key) in ALLOWED_METADATA_KEYS and value not in (None, "")
    }


def build_span(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    start_ms: int,
    end_ms: int,
    parent_span_id: str = "",
    observation_type: str = "span",
    level: str = "DEFAULT",
    status_message: str = "",
    model_name: str = "",
    usage_details: Mapping[str, int] | None = None,
    cost_details: Mapping[str, float] | None = None,
    session_id: str = "",
    trace_name: str = "",
    environment: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """OTLP span 1개. 원문을 받을 자리가 없다(input/output 인자 자체가 없다).

    `usage_details`/`cost_details` 는 Langfuse 가 **JSON 문자열**로 기대한다
    (실측 확인: {"input":1234,"output":567,"total":1801} 가 UI 집계에 그대로 잡힘).
    """

    if not is_trace_id(trace_id):
        raise ValueError(f"trace_id 형식 오류: {trace_id!r}")
    if not is_span_id(span_id):
        raise ValueError(f"span_id 형식 오류: {span_id!r}")
    if observation_type not in OBSERVATION_TYPES:
        raise ValueError(f"모르는 observation type: {observation_type!r}")
    if level not in LEVELS:
        raise ValueError(f"모르는 level: {level!r}")
    if end_ms < start_ms:
        # 음수 지연은 대시보드에서 조용히 0 으로 접히거나 정렬을 깨뜨린다.
        raise ValueError(f"end_ms < start_ms ({end_ms} < {start_ms})")

    safe_metadata = _checked_metadata(metadata)
    attributes: list[dict[str, Any]] = []
    for key, value in (
        (ATTR_OBSERVATION_TYPE, observation_type),
        (ATTR_OBSERVATION_LEVEL, level),
        (ATTR_STATUS_MESSAGE, status_message),
        (ATTR_MODEL_NAME, model_name),
        (ATTR_SESSION_ID, session_id),
        (ATTR_TRACE_NAME, trace_name),
        (ATTR_ENVIRONMENT, environment),
        (
            ATTR_USAGE_DETAILS,
            json.dumps(dict(usage_details), separators=(",", ":"))
            if usage_details
            else "",
        ),
        (
            ATTR_COST_DETAILS,
            json.dumps(dict(cost_details), separators=(",", ":"))
            if cost_details
            else "",
        ),
        (
            ATTR_OBSERVATION_METADATA,
            json.dumps(safe_metadata, separators=(",", ":"), ensure_ascii=False)
            if safe_metadata
            else "",
        ),
    ):
        attribute = _attribute(key, value)
        if attribute is not None:
            attributes.append(attribute)

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": str(name)[:200],
        # 1 = SPAN_KIND_INTERNAL. 우리 span 은 전부 내부 작업이다.
        "kind": 1,
        "startTimeUnixNano": str(int(start_ms) * 1_000_000),
        "endTimeUnixNano": str(int(end_ms) * 1_000_000),
        "attributes": attributes,
    }
    if is_span_id(parent_span_id):
        span["parentSpanId"] = parent_span_id
    return span


def build_payload(
    spans: Sequence[Mapping[str, Any]], *, service_name: str = "hgfinance"
) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_attribute("service.name", service_name)]
                },
                "scopeSpans": [
                    {"scope": {"name": "hgfinance"}, "spans": list(spans)}
                ],
            }
        ]
    }


def publish(
    spans: Sequence[Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None = None,
    service_name: str = "hgfinance",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """span 여러 개를 **한 번의 왕복**으로 보낸다. 실패는 삼킨다(fail-open).

    한 번인 이유: 부서장 턴 하나가 도구 span 을 여러 개 만드는데 span 마다 왕복하면
    계측이 실행 시간을 바꾼다. llm_observability 가 매 호출 flush 를 걷어낸 것과
    같은 이유다(실측: flush 포함 중앙값 85.8ms).

    반환값은 "서버가 200 을 줬다"는 뜻이다 - langfuse SDK 경로(큐잉만 하고 True)와
    달리 여기서는 동기 왕복을 확인한다. 다만 200 은 **ingestion 큐에 들어갔다**는
    뜻이지 trace 가 조회 가능해졌다는 뜻은 아니다(실측 응답: otel-ingestion-job).
    """

    source = dict(env) if env is not None else dict(os.environ)
    if not spans or not enabled(source):
        return False
    host = (str(source.get("LANGFUSE_HOST", "") or "").strip() or DEFAULT_HOST).rstrip("/")
    auth = base64.b64encode(
        f"{source.get('LANGFUSE_PUBLIC_KEY', '')}:{source.get('LANGFUSE_SECRET_KEY', '')}".encode()
    ).decode("ascii")
    body = json.dumps(
        build_payload(spans, service_name=service_name), separators=(",", ":")
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{host}{OTLP_TRACES_PATH}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        # 관측은 절대 업무 결과를 바꾸지 않는다.
        return False


if __name__ == "__main__":
    trace = trace_id_for("t_selfcheck")
    parent = span_id_for("t_selfcheck", "research", "head", 1)
    child = span_id_for("t_selfcheck", "research", "tool", 1, 0)

    # 1. 부모/자식 span 이 조립된다.
    head = build_span(
        trace_id=trace, span_id=parent, name="head.turn",
        start_ms=1_700_000_000_000, end_ms=1_700_000_002_000,
        session_id="t_selfcheck", trace_name="ceo.query",
        metadata={"department": "research", "status": "COMPLETED", "raw_payloads_sent": False},
    )
    assert head["traceId"] == trace and "parentSpanId" not in head
    assert head["startTimeUnixNano"] == "1700000000000000000"

    tool = build_span(
        trace_id=trace, span_id=child, parent_span_id=parent,
        name="tool.research.evidence.search", observation_type="tool",
        start_ms=1_700_000_000_500, end_ms=1_700_000_001_200,
        metadata={"tool_name": "research.evidence.search", "tool_timing_source": "hermes-log-duration"},
    )
    assert tool["parentSpanId"] == parent

    # 2. usage/cost 는 JSON 문자열로 실린다(실측 형식).
    generation = build_span(
        trace_id=trace, span_id=span_id_for("t_selfcheck", "research", "gen", 1),
        parent_span_id=parent, name="codex.chat", observation_type="generation",
        start_ms=1_700_000_000_000, end_ms=1_700_000_002_000,
        model_name="gpt-5.6-luna",
        usage_details={"input": 1234, "output": 567, "total": 1801},
        cost_details={"total": 0.0042},
        metadata={"cost_basis": "amortized_subscription", "cost_provisional": True},
    )
    packed = {a["key"]: a["value"] for a in generation["attributes"]}
    assert json.loads(packed[ATTR_USAGE_DETAILS]["stringValue"])["total"] == 1801
    assert json.loads(packed[ATTR_COST_DETAILS]["stringValue"])["total"] == 0.0042
    assert packed[ATTR_MODEL_NAME]["stringValue"] == "gpt-5.6-luna"

    # 3. 원문이 실릴 수 있는 키는 조용히 버리지 않고 터뜨린다.
    for leak in ("input", "prompt", "task_body", "tool_result"):
        try:
            build_span(trace_id=trace, span_id=parent, name="x",
                       start_ms=0, end_ms=1, metadata={leak: "비밀"})
            raise AssertionError(f"{leak} 가 통과함")
        except RedactionError:
            pass

    # 4. 허용 목록 밖의 무해한 키는 조용히 빠진다(터뜨릴 일은 아니다).
    quiet = build_span(trace_id=trace, span_id=parent, name="x", start_ms=0, end_ms=1,
                       metadata={"department": "research", "made_up_key": "v"})
    meta = json.loads(
        {a["key"]: a["value"] for a in quiet["attributes"]}[ATTR_OBSERVATION_METADATA]["stringValue"]
    )
    assert meta == {"department": "research"}

    # 5. 깨진 id·역전된 시각은 발행 전에 막는다.
    for bad in ({"trace_id": "zz"}, {"span_id": "zz"}):
        try:
            build_span(trace_id=bad.get("trace_id", trace), span_id=bad.get("span_id", parent),
                       name="x", start_ms=0, end_ms=1)
            raise AssertionError(f"{bad} 가 통과함")
        except ValueError:
            pass
    try:
        build_span(trace_id=trace, span_id=parent, name="x", start_ms=10, end_ms=1)
        raise AssertionError("음수 지연이 통과함")
    except ValueError:
        pass

    # 6. payload 모양이 실측 프로브와 같다.
    payload = build_payload([head, tool, generation])
    assert len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 3

    # 7. 스위치가 꺼져 있으면 아무것도 안 보내고 False.
    assert publish([head], env={}) is False
    assert enabled({"LANGFUSE_TRACING": "true"}) is False  # 키가 없다

    print("ok - Langfuse OTLP 전송 계층 점검 통과")
