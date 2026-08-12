#!/usr/bin/env python3
"""Mandate 온보딩 챗봇 제안 - USER_INPUT_API_SPEC.md 2.4.

담당: 영주 (CEO Office)
근거: docs/02-engineering/USER_INPUT_API_SPEC.md 2.4(챗봇 제안 API),
      docs/01-product/USER_INPUT_SPEC.md 4.1(LLM 허용/금지 경계),
      departments/01-research/agents/news_sentiment_analyst.py(재시도·Schema 검증 패턴)

이 모듈은 자연어 대화에서 구조화 값 "제안"만 만든다. **어떤 상태도 저장하거나
바꾸지 않는다**(Stateless) - 확정은 화면이 사용자 확인을 받은 뒤
POST .../mandates/{mandate_id}/change-requests(§2.2) 또는
POST /portfolio/v1/investor-profiles(§2.3) 경로로 한다.

## LLM 경계 (USER_INPUT_SPEC.md 4.1)

- 허용: 자연어 -> 구조화 값 "제안"(투자 기간, 유동성 긴급도, 목표 문장 정리).
- 금지: mindset/experience 추론, 한도 값의 적정성 "판정", 사용자 확인 없는 확정.

`suitability.py`가 "LLM 이 성향을 추론하지 않는다"를 이미 명시적으로 요구한다 -
이 모듈이 그 경계를 다시 어기지 않도록 ALLOWED_SUGGESTION_FIELDS 로 강제한다.
LLM 이 목록 밖 필드(예: mindset)를 내놓으면 조용히 버리지 않고 `dropped_fields`
에 남긴다 - 호출부가 감사·디버깅에 쓸 수 있게.

## 왜 업종·종목 필드가 여기 없는가

`allowed_assets`/`forbidden_assets`/`preferred_sectors`/`excluded_sectors` 는 KRX
업종 코드 사전(재일, USER_INPUT_API_SPEC.md 2.5 `sectors/resolve`)과 종목 검색이
선행돼야 자연어를 실제 코드값으로 바꿀 수 있다. 그 전까지 이 필드들을
ALLOWED_SUGGESTION_FIELDS 에 넣지 않는다 - LLM 이 존재하지 않는 코드를 지어내지
않게 막는 것이다(개발 원칙 9: 위험한 기능은 실패 시 확대가 아니라 차단).

## 왜 Schema 위반 시 예외를 던지는가 (2회 재시도 후)

`news_sentiment_analyst.py` 와 같은 원칙이다 - "판정을 지어내지 않는다." 다만 이
모듈을 호출하는 FastAPI endpoint(app.py)는 이 예외를 그대로 사용자에게 500 으로
보여주지 않고, 빈 제안 + 실패 안내 reply 로 감싼다(USER_INPUT_API_SPEC.md 2.4의
"LLM 실패·저신뢰는 제안을 비우고 끝낸다") - 채팅 UI가 한 번의 LLM 장애로 멈추면
안 되기 때문이다. 이 모듈 자체는 계속 "지어내지 않는다"를 지킨다.

자체 점검(python mandate_assistant.py):
  - allow-list 강제, Schema 위반 재시도/실패, 빈 messages 거절은 가짜 llm 함수로
    항상 검증한다(네트워크 없이 결정론적).
  - ANTHROPIC_API_KEY 가 있으면 실제 Claude 호출도 한 번 확인한다(없으면 SKIP).
"""
from __future__ import annotations

import json
import os
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 이 5개만 LLM 제안 대상이다. 확장하려면 §2.5(업종·종목 조회)가 먼저 있어야 한다.
ALLOWED_SUGGESTION_FIELDS = frozenset({
    "investment_horizon_years",
    "liquidity_need",
    "objective_text",
})

MODEL = os.environ.get("MANDATE_ASSISTANT_MODEL", "claude-sonnet-5")

_SYSTEM_PROMPT = """You are a structured-data extraction assistant for a Korean \
hedge-fund Mandate onboarding chat. The user describes their investing situation \
in free text (Korean or English).

Extract ONLY the following fields, and ONLY when the user has clearly stated them:
- investment_horizon_years: integer number of years they plan to stay invested.
- liquidity_need: one of HIGH, MEDIUM, LOW - how urgently they might need to \
withdraw cash (HIGH = within days, MEDIUM = within weeks, LOW = not for a long time).
- objective_text: one short sentence (in the user's language) summarizing their \
stated investment goal, using their own words as closely as possible.

Do NOT infer risk tolerance or investing experience level - those are asked \
elsewhere and you must never guess them. Do NOT invent a value for a field the \
user did not actually state. If nothing extractable was said, return an empty \
suggestions list.

Return ONLY valid JSON matching this schema, nothing else:
{"reply": "<one short sentence acknowledging what you understood, in the user's \
language>", "suggestions": [{"field": "<field name above>", "value": <string or \
integer>, "label": "<short human-readable label>", "confidence": "HIGH|MEDIUM|LOW"}]}"""


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class Suggestion(BaseModel):
    """확정된 제안 하나. `source`는 항상 llm_extraction - 결정론 제안은 아직 없다."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str | int
    label: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    source: Literal["llm_extraction"] = "llm_extraction"


class _LLMSuggestion(BaseModel):
    """LLM 원시 출력. allow-list 검증 전 단계라 field 값에 제약을 걸지 않는다."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str | int
    label: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class _LLMBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=1000)
    suggestions: list[_LLMSuggestion] = Field(default_factory=list)


class SuggestResult(BaseModel):
    """Stateless 응답. requires_user_confirmation 은 항상 True다 - 이 결과만으로
    저장되는 값은 없다(USER_INPUT_API_SPEC.md 2.4 계약 불변식 1)."""

    model_config = ConfigDict(extra="forbid")

    reply: str
    suggestions: list[Suggestion]
    requires_user_confirmation: Literal[True] = True
    dropped_fields: list[str] = Field(
        default_factory=list, description="allow-list 밖이라 버려진 LLM 출력 필드"
    )


def _anthropic_call(system: str, user: str) -> str:
    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY가 없다 (CEO Office 배정 키)")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=MODEL, max_tokens=1000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def suggest(
    *,
    messages: list[AssistantMessage],
    current_draft: dict | None = None,
    llm: Callable[[str, str], str] | None = None,
) -> SuggestResult:
    """대화 이력에서 구조화 제안을 만든다.

    Schema 불합격이면 한 번 고쳐 부르고, 또 실패하면 예외다 - 판정을 지어내지
    않는다. 이 함수를 직접 호출하는 쪽(예: FastAPI endpoint)이 예외를 사용자
    응답으로 어떻게 감쌀지 결정한다(모듈 docstring 참고).
    """
    if not messages:
        raise ValueError("messages가 비어 있다")

    call = llm or _anthropic_call
    conversation = "\n".join(f"{m.role}: {m.content}" for m in messages)
    draft_note = (
        f"\n\n(Current draft state, for context only - do not restate these as "
        f"new suggestions: {json.dumps(current_draft or {}, ensure_ascii=False)})"
    )

    last_err: str | None = None
    for attempt in range(2):
        prompt = conversation + draft_note
        if attempt == 1:
            prompt += (
                f"\n\nYour previous output failed validation: {last_err}. "
                f"Return ONLY valid JSON for the schema."
            )
        text = call(_SYSTEM_PROMPT, prompt)
        try:
            start, end = text.find("{"), text.rfind("}")
            batch = _LLMBatch.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as exc:
            last_err = str(exc)[:200]
            continue

        accepted: list[Suggestion] = []
        dropped: list[str] = []
        for raw in batch.suggestions:
            if raw.field not in ALLOWED_SUGGESTION_FIELDS:
                dropped.append(raw.field)  # allow-list 밖 - 조용히 버리지 않는다.
                continue
            accepted.append(Suggestion(
                field=raw.field, value=raw.value, label=raw.label,
                confidence=raw.confidence,
            ))
        return SuggestResult(reply=batch.reply, suggestions=accepted, dropped_fields=dropped)

    raise RuntimeError(f"LLM 제안이 Schema를 두 번 어겼다: {last_err}")


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/mandate_assistant.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # 1) allow-list 강제 - 금지 필드(mindset)는 버려지고 dropped_fields에 남는다.
    def _fake_llm_with_forbidden(system: str, user: str) -> str:
        return json.dumps({
            "reply": "확인했습니다.",
            "suggestions": [
                {"field": "investment_horizon_years", "value": 10, "label": "10년",
                 "confidence": "HIGH"},
                {"field": "mindset", "value": "RISK_SEEKING", "label": "공격적",
                 "confidence": "HIGH"},
            ],
        })

    result = suggest(
        messages=[AssistantMessage(role="user", content="10년쯤 투자할 생각이에요")],
        llm=_fake_llm_with_forbidden,
    )
    assert result.requires_user_confirmation is True
    assert len(result.suggestions) == 1
    assert result.suggestions[0].field == "investment_horizon_years"
    assert result.suggestions[0].value == 10
    assert result.suggestions[0].source == "llm_extraction"
    assert result.dropped_fields == ["mindset"], result.dropped_fields
    print("ok - allow-list 강제 (금지 필드 mindset 버려짐, dropped_fields에 기록)")

    # 2) Schema 위반 응답은 재시도 후에도 실패하면 예외 (지어내지 않는다).
    def _fake_llm_broken(system: str, user: str) -> str:
        return "not even json"

    try:
        suggest(messages=[AssistantMessage(role="user", content="x")], llm=_fake_llm_broken)
        raise AssertionError("Schema 위반인데 통과함")
    except RuntimeError as exc:
        assert "두 번" in str(exc)
    print("ok - Schema 위반 2회 실패 시 예외 (제안을 지어내지 않음)")

    # 3) 1차 실패 -> 2차 성공 재시도 경로.
    calls = {"n": 0}

    def _fake_llm_retry(system: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "garbage output, not json"
        return json.dumps({"reply": "확인했습니다.", "suggestions": []})

    r3 = suggest(messages=[AssistantMessage(role="user", content="x")], llm=_fake_llm_retry)
    assert calls["n"] == 2 and r3.suggestions == [] and r3.dropped_fields == []
    print("ok - 1차 Schema 위반 -> 2차 성공 재시도 경로")

    # 4) 빈 messages는 거절.
    try:
        suggest(messages=[], llm=_fake_llm_retry)
        raise AssertionError("빈 messages인데 통과함")
    except ValueError:
        pass
    print("ok - 빈 messages 거절")

    # 5) liquidity_need/objective_text 정상 추출 경로 + current_draft가 프롬프트에
    #    들어가는지(재확인 방지 맥락용).
    seen_prompts: list[str] = []

    def _fake_llm_full(system: str, user: str) -> str:
        seen_prompts.append(user)
        return json.dumps({
            "reply": "장기 투자와 낮은 유동성 필요를 확인했습니다.",
            "suggestions": [
                {"field": "liquidity_need", "value": "LOW", "label": "낮음",
                 "confidence": "HIGH"},
                {"field": "objective_text", "value": "안정적인 노후 자금 마련",
                 "label": "안정적인 노후 자금 마련", "confidence": "MEDIUM"},
            ],
        })

    r5 = suggest(
        messages=[AssistantMessage(role="user", content="급하게 쓸 돈은 아니에요")],
        current_draft={"objective_text": "이전 초안"},
        llm=_fake_llm_full,
    )
    assert {s.field for s in r5.suggestions} == {"liquidity_need", "objective_text"}
    assert "이전 초안" in seen_prompts[0], "current_draft가 프롬프트 맥락에 안 들어감"
    print("ok - liquidity_need/objective_text 추출 + current_draft 맥락 전달 확인")

    # 6) 실 Anthropic 호출 (ANTHROPIC_API_KEY 있을 때만, 없으면 SKIP).
    if os.environ.get("ANTHROPIC_API_KEY"):
        real = suggest(messages=[
            AssistantMessage(
                role="user",
                content=(
                    "저는 10년 정도 장기 투자할 생각이고, 급하게 현금화할 일은 "
                    "없어요. 목표는 안정적인 노후 자금 마련입니다."
                ),
            ),
        ])
        assert real.requires_user_confirmation is True
        fields = {s.field for s in real.suggestions}
        assert fields <= ALLOWED_SUGGESTION_FIELDS
        print(f"ok - 실 Anthropic 호출 통과 - 추출 필드: {fields}")
    else:
        print("SKIP - ANTHROPIC_API_KEY 없음, 실 LLM 호출은 건너뜀")

    print("mandate_assistant.py 자체 점검 통과")
