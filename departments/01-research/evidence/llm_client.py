#!/usr/bin/env python3
"""분석가 LLM 호출·서술 재시도의 단일 출처.

소유: 재일 (리서치본부)
근거: 2026-08-02 중복 조사. 분석가별 생성 호출과 `narrate` 재시도 루프가
      복제돼 모델·timeout·형식 정책이 갈라졌다. 이 모듈은 재시도 규율을
      유지하면서 생성 요청을 공용 Gateway 한 곳으로 고정한다.

▶ 모델 경계
  모든 생성 호출은 공용 Worker Model Gateway로만 나간다. 따라서 운영에서는
  Qwen2.5-14B-AWQ + Hybrid Upgrade 정책, 구조 출력 검증과 계측이 한 경로에서
  적용된다. 호출부는 더 이상 Ollama/EXAONE/외부 모델이나 timeout을 고르지 않는다.

▶ 재시도 규율 (지어내지 않는다)
  Schema 를 어기면 오류 문구를 붙여 한 번 더 부른다. 두 번 다 어기면 예외다.
  **빈 값이나 기본값으로 채우지 않는다** - 호출부가 LLM_UNAVAILABLE 로
  처리하고, 결정론 판정은 그대로 남는다(개발원칙 9).

실행: python evidence/llm_client.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import time
import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from .observability import current_metrics, redacted_span, update_span_metadata
except ImportError:  # direct module execution from the Research profile
    from observability import current_metrics, redacted_span, update_span_metadata  # type: ignore

MODULE_VERSION = "research-llm-client-v2-gateway-enforced"

DEFAULT_TEMPERATURE = 0.1
NARRATE_ATTEMPTS = 2       # 처음 + 오류를 알려주고 한 번 더
_ERR_CLIP = 200            # 재시도 프롬프트에 실을 오류 길이


_JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


def _gateway_llm(worker_id: str):
    """Resolve the sole production LLM boundary without a local fallback."""

    try:
        from departments.worker_model_gateway import llm_for_worker
    except ModuleNotFoundError as exc:
        if exc.name != "departments":
            raise
        repo_root = Path(__file__).resolve().parents[3]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from departments.worker_model_gateway import llm_for_worker
    return llm_for_worker(worker_id)


def _call_gateway(
    system: str,
    user: str,
    *,
    worker_id: str,
    json_schema: dict[str, Any] | None,
) -> tuple[str, str]:
    worker_llm, binding = _gateway_llm(worker_id)
    if json_schema is not None and getattr(worker_llm, "_json_schema_capable", False):
        return worker_llm(system, user, json_schema=json_schema), binding.model
    return worker_llm(system, user), binding.model


def chat(
    system: str,
    user: str,
    *,
    worker_id: str = "research-document-synthesis-worker",
    base: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int | None = None,
    json_object: bool = True,
) -> str:
    """Generate through the canonical Qwen AWQ+Hybrid Gateway.

    Legacy keyword arguments remain temporarily source-compatible for callers,
    but cannot select an endpoint, model, timeout, or temperature.
    """
    del base, model, timeout, temperature, max_tokens
    metrics = current_metrics()
    started = time.perf_counter()
    if metrics:
        metrics.record_tool_call()
        if metrics.generation_started_at is None:
            metrics.mark("generation_started_at")
    with redacted_span(
        "research.llm.call",
        run_type="llm",
        metadata={"worker_id": worker_id, "retry_count": 0, "error": False, "status": "started"},
        tags=("llm", "chat_completions"),
    ) as span:
        try:
            result, resolved_model = _call_gateway(
                system,
                user,
                worker_id=worker_id,
                json_schema=_JSON_OBJECT_SCHEMA if json_object else None,
            )
            if span is not None:
                update_span_metadata(span, {"model": resolved_model})
            return result
        except Exception:
            if span is not None:
                update_span_metadata(span, {"status": "error"})
            raise
        finally:
            if metrics:
                metrics.llm_duration_ms += max(0, int((time.perf_counter() - started) * 1000))
                metrics.mark("generation_finished_at")


def chat_structured(system: str, user: str, *, schema: dict,
                    worker_id: str = "research-document-synthesis-worker",
                    base: str | None = None, model: str | None = None,
                    timeout: float | None = None,
                    temperature: float = DEFAULT_TEMPERATURE) -> str:
    """Generate schema-constrained output through the canonical Gateway."""
    del base, model, timeout, temperature
    metrics = current_metrics()
    started = time.perf_counter()
    if metrics:
        metrics.record_tool_call()
        if metrics.generation_started_at is None:
            metrics.mark("generation_started_at")
    with redacted_span(
        "research.llm.call",
        run_type="llm",
        metadata={"worker_id": worker_id, "retry_count": 0, "error": False, "status": "started"},
        tags=("llm", "structured"),
    ) as span:
        try:
            result, resolved_model = _call_gateway(
                system, user, worker_id=worker_id, json_schema=schema
            )
            if span is not None:
                update_span_metadata(span, {"model": resolved_model})
            return result
        except Exception:
            if span is not None:
                update_span_metadata(span, {"status": "error"})
            raise
        finally:
            if metrics:
                metrics.llm_duration_ms += max(0, int((time.perf_counter() - started) * 1000))
                metrics.mark("generation_finished_at")


def extract_json(text: str) -> str:
    """LLM 출력에서 JSON 조각만. 첫 '{' ~ 마지막 '}'.

    프리앰블(<think>...)과 후행 설명을 같이 견딘다. 괄호가 없으면 원문을
    그대로 돌려준다 - 여기서 예외를 내면 재시도 루프가 오류 문구를 못 만든다.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return text
    return text[start:end + 1]


def narrate(system: str, prompt: str, model_cls, call,
            *, attempts: int = NARRATE_ATTEMPTS):
    """LLM 서술 -> Pydantic 검증. Schema 를 어기면 오류를 알려주고 재시도.

    분석가 6인이 글자 그대로 같은 루프를 갖고 있었다 - 달라지는 건 model_cls
    하나뿐이다. call 은 (system, user) -> str 인 호출 가능 객체이며, 자체
    점검에서는 가짜 함수를 넣는다.

    **두 번 다 실패하면 예외다.** 빈 노트를 만들어 돌려주면 호출부가 서술이
    있는 줄 알고 Packet 에 싣는다 - 그게 가장 위험한 실패다.
    """
    from pydantic import ValidationError

    last_err = None
    for attempt in range(attempts):
        if attempt:
            metrics = current_metrics()
            if metrics:
                metrics.record_retry()
        user = prompt if attempt == 0 else (
            prompt + f"\n\nYour previous output failed validation: {last_err}. "
                     f"Return ONLY valid JSON for the schema.")
        try:
            with redacted_span(
                "research.llm.narrate",
                run_type="llm",
                metadata={
                    "model": os.getenv("WORKER_MODEL_NAME", "qwen2.5-14b-instruct-awq"),
                    "retry_count": attempt,
                    "error": False,
                    "status": "started",
                },
                tags=("llm", "narrate"),
            ) as span:
                try:
                    text = call(system, user)
                    return model_cls.model_validate_json(extract_json(text))
                except Exception:
                    if span is not None:
                        update_span_metadata(span, {"status": "error"})
                    raise
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:_ERR_CLIP]
    raise RuntimeError(
        f"LLM 서술이 Schema 를 {attempts}번 어겼다: {last_err}")


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_extract_json():
    assert extract_json('{"a":1}') == '{"a":1}'
    # qwen3 프리앰블·후행 설명을 견딘다
    assert extract_json('<think>음...</think>\n{"a":1}\n설명') == '{"a":1}'
    # 중첩 - 마지막 '}' 까지 취한다
    assert extract_json('x {"a":{"b":2}} y') == '{"a":{"b":2}}'
    # 괄호가 없으면 원문 - 여기서 예외를 내면 재시도 문구를 못 만든다
    assert extract_json("no json here") == "no json here"
    assert extract_json("} {") == "} {"
    print("  JSON 조각 추출           OK")


def _check_narrate_retry():
    from pydantic import BaseModel

    class Note(BaseModel):
        verdict: str

    calls = {"n": 0, "prompts": []}

    def flaky(system, user):
        calls["n"] += 1
        calls["prompts"].append(user)
        return "죄송합니다 JSON 이 아닙니다" if calls["n"] == 1 else '{"verdict":"OK"}'

    note = narrate("sys", "prompt", Note, flaky)
    assert note.verdict == "OK" and calls["n"] == 2
    # 재시도 프롬프트에 **무엇이 틀렸는지**가 실려야 모델이 고칠 수 있다
    assert "failed validation" in calls["prompts"][1]
    assert calls["prompts"][0] == "prompt", "첫 호출은 원문 그대로여야 한다"

    # 두 번 다 어기면 예외 - 빈 노트로 위장하지 않는다
    try:
        narrate("sys", "prompt", Note, lambda s, u: "never json")
        raise AssertionError("Schema 를 두 번 어겼는데 통과했다")
    except RuntimeError as e:
        assert "2번 어겼다" in str(e), e
    print("  서술 재시도 규율         OK")


def _check_gateway_contract():
    """No-network check: legacy arguments cannot escape the Gateway."""
    seen = {}

    def fake_call(system, user, *, worker_id, json_schema):
        seen.update(
            system=system,
            user=user,
            worker_id=worker_id,
            json_schema=json_schema,
        )
        return "{}", "qwen2.5-14b-instruct-awq"

    original = _call_gateway
    globals()["_call_gateway"] = fake_call
    try:
        chat("s", "u", base="http://x:11434/", model="m1", timeout=42,
             temperature=0.3, max_tokens=8192)
    finally:
        globals()["_call_gateway"] = original

    assert seen["worker_id"] == "research-document-synthesis-worker"
    assert seen["json_schema"] == _JSON_OBJECT_SCHEMA
    print("  Gateway 강제 계약        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_extract_json()
    _check_narrate_retry()
    _check_gateway_contract()
    print("LLM 클라이언트 3개 영역 통과.")
