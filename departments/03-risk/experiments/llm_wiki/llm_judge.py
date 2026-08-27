"""LLM-as-a-Judge 2차 평가 — token-F1이 놓치는 패러프레이즈를 잡는다.

1차(eval_metrics.py의 token F1/EM)는 어휘 일치만 본다. 판정문(rationale)은 gold와
같은 법적 결론이라도 문장을 다르게 쓰면 F1이 낮게 나온다(실측: 3개 Arm 모두 F1
0.03~0.06). verdict(no_breach/breach/ambiguous) 자체는 이미 run_experiment.py에서
문자열로 정확히 비교하므로(verdict_match), 여기서는 rationale이 gold_answer의 핵심
법적 근거(조항 번호, 요건, 결론)를 실제로 담고 있는지를 GPT에게 채점시킨다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_RISK_ROOT = Path(__file__).resolve().parents[2]  # departments/03-risk
_AGENTIC_RAG_ROOT = Path(__file__).resolve().parents[4] / "skills" / "agentic-rag"
for path in (_RISK_ROOT, _AGENTIC_RAG_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.resilience import CircuitBreaker, RedisJsonCache, emit_metric

MODEL = os.environ.get("LLM_WIKI_JUDGE_MODEL", "gpt-4o-mini")
_BREAKER = CircuitBreaker("llm-wiki-judge", failure_threshold=3, recovery_timeout_seconds=30)
_CACHE = RedisJsonCache("risk-qa:llm-wiki:judge", ttl_seconds=7 * 24 * 3600)

_JUDGE_SYSTEM_PROMPT = (
    "You are grading a compliance-QA system's answer against a gold reference answer. "
    "Score how well the PREDICTED answer semantically covers the key legal claims in the "
    "GOLD answer, ignoring wording differences.\n"
    'Return JSON exactly as: {"semantic_f1": <float 0-1>, "correct": <bool>, "reasoning": '
    '"<short string>"}.\n'
    "semantic_f1 reflects precision and recall of the gold answer's key legal points "
    "(which clause/article, what it requires or prohibits, the practical consequence). "
    "correct=true only if a compliance officer reading PREDICTED would reach the same "
    "practical conclusion as GOLD. Do not reward content not grounded in PREDICTED, and "
    "do not penalize phrasing differences alone."
)


def judge(query: str, gold_answer: str, predicted_answer: str) -> dict[str, Any]:
    """실패/빈 예측은 fail-closed(semantic_f1=0.0, correct=False) — 판정 불능을 숨기지 않는다."""

    if not predicted_answer.strip():
        return {"semantic_f1": 0.0, "correct": False, "reasoning": "empty prediction"}

    user = (
        f"Question: {query}\n\nGOLD answer:\n{gold_answer}\n\n"
        f"PREDICTED answer:\n{predicted_answer}"
    )
    fingerprint = _CACHE.fingerprint(MODEL, _JUDGE_SYSTEM_PROMPT, user)
    cached = _CACHE.get(fingerprint)
    if isinstance(cached, dict):
        emit_metric("llm_wiki_judge_cache_hit")
        return cached

    try:
        from openai import OpenAI

        response = _BREAKER.call(
            lambda: OpenAI().chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
            )
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        emit_metric("llm_wiki_judge_failure", error=type(exc).__name__)
        return {
            "semantic_f1": 0.0,
            "correct": False,
            "reasoning": f"judge unavailable: {type(exc).__name__}",
        }

    result.setdefault("semantic_f1", 0.0)
    result.setdefault("correct", False)
    result.setdefault("reasoning", "")
    _CACHE.set(fingerprint, result)
    return result


if __name__ == "__main__":
    empty = judge("아무 질문", "정답", "")
    assert empty == {"semantic_f1": 0.0, "correct": False, "reasoning": "empty prediction"}

    print("llm_judge self-check OK (실제 채점은 run_experiment.py --judge에서 확인)")
