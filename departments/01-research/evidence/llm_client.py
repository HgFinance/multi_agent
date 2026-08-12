#!/usr/bin/env python3
"""분석가 LLM 호출·서술 재시도의 단일 출처.

소유: 재일 (리서치본부)
근거: 2026-08-02 중복 조사. `_ollama_call` 이 7곳(분석가 6인 + scripts.py),
      `narrate` 재시도 루프가 6곳에 글자 그대로 복붙돼 있었다. 그리고
      **이미 갈라져 있었다** - timeout 이 20/30/LLM_TIMEOUT 으로 셋,
      max_tokens 는 scripts.py 에만, temperature 는 0.1/0.2 로 둘.
      복붙은 처음엔 같지만 시간이 지나면 반드시 갈라진다. 갈라진 뒤에는
      "왜 이 분석가만 다르지?"를 아무도 답할 수 없다.

▶ 무엇을 공통화하고 무엇을 남기는가
  공통: HTTP 호출 모양, 응답에서 텍스트 꺼내기, JSON 조각 추출, 재시도 규율.
  남김: model / timeout / temperature - **분석가마다 다를 이유가 실제로 있다**
        (14b 는 느리고 7.8b 는 빠르다). 인자로 받아 호출부가 정한다.
  추상화를 위한 추상화가 아니라, 갈라지면 안 되는 것만 묶는다.

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
import urllib.request

try:
    from .observability import current_metrics
except ImportError:  # direct module execution from the Research profile
    from observability import current_metrics  # type: ignore

MODULE_VERSION = "research-llm-client-v1"

DEFAULT_TEMPERATURE = 0.1
NARRATE_ATTEMPTS = 2       # 처음 + 오류를 알려주고 한 번 더
_ERR_CLIP = 200            # 재시도 프롬프트에 실을 오류 길이


def chat(system: str, user: str, *, base: str, model: str, timeout: float,
         temperature: float = DEFAULT_TEMPERATURE,
         max_tokens: int | None = None,
         json_object: bool = True) -> str:
    """OpenAI 호환 /v1/chat/completions 한 번. 실패는 예외 - 삼키지 않는다.

    qwen3 계열은 <think> 사고 텍스트를 앞에 붙일 수 있다. 그건 여기서 자르지
    않고 extract_json 이 첫 '{'~마지막 '}' 만 취해 견딘다 - 모델별 프리앰블
    형태를 쫓아다니는 것보다 결과만 보는 쪽이 덜 깨진다.
    """
    payload: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if json_object:
        # 소형 모델의 JSON 규율 - 지원 안 하는 서버는 이 키를 무시한다
        payload["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer ollama"},
        data=json.dumps(payload).encode())
    metrics = current_metrics()
    started = time.perf_counter()
    if metrics:
        metrics.record_tool_call()
        if metrics.generation_started_at is None:
            metrics.mark("generation_started_at")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"]
    finally:
        if metrics:
            metrics.llm_duration_ms += max(0, int((time.perf_counter() - started) * 1000))
            metrics.mark("generation_finished_at")


def chat_structured(system: str, user: str, *, base: str, model: str,
                    timeout: float, schema: dict,
                    temperature: float = DEFAULT_TEMPERATURE) -> str:
    """Ollama 네이티브 /api/chat + format=JSON Schema (구조 강제 디코딩).

    /v1 경로와 달리 스키마를 디코딩 단계에서 강제하므로 형식 이탈이 거의
    없다. 대신 Ollama 전용이라 게이트웨이·다른 제공자로 옮길 때 함께 바뀐다.
    """
    req = urllib.request.Request(
        base.rstrip("/") + "/api/chat", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": temperature},
            "format": schema,
        }).encode())
    metrics = current_metrics()
    started = time.perf_counter()
    if metrics:
        metrics.record_tool_call()
        if metrics.generation_started_at is None:
            metrics.mark("generation_started_at")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["message"]["content"]
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
        text = call(system, user)
        try:
            return model_cls.model_validate_json(extract_json(text))
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


def _check_payload_shape():
    """HTTP 를 안 타고 payload 만 확인한다 - 갈라졌던 설정이 인자로 오는가."""
    seen = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data)
        return _FakeResp()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        chat("s", "u", base="http://x:11434/", model="m1", timeout=42,
             temperature=0.3, max_tokens=8192)
    finally:
        urllib.request.urlopen = orig

    assert seen["url"] == "http://x:11434/v1/chat/completions", seen["url"]
    assert seen["timeout"] == 42
    b = seen["body"]
    assert b["model"] == "m1" and b["temperature"] == 0.3
    assert b["max_tokens"] == 8192
    assert b["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in b["messages"]] == ["system", "user"]

    # max_tokens 를 안 주면 키 자체가 없어야 한다 - 0 이나 기본값을 넣으면
    # 서버가 그 값으로 잘라버린다
    urllib.request.urlopen = fake_urlopen
    try:
        chat("s", "u", base="http://x", model="m", timeout=1, json_object=False)
    finally:
        urllib.request.urlopen = orig
    assert "max_tokens" not in seen["body"]
    assert "response_format" not in seen["body"]
    print("  요청 payload 계약        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_extract_json()
    _check_narrate_retry()
    _check_payload_shape()
    print("LLM 클라이언트 3개 영역 통과.")
