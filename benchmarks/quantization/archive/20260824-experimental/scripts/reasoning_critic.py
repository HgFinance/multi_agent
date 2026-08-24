#!/usr/bin/env python3
"""OpenAI-compatible critic/rewrite boundary for AWQ draft responses."""
from __future__ import annotations

import json
import time
from typing import Any
from urllib import request


def critic_prompt(question: str, draft: str) -> str:
    return (
        "Review the AWQ draft for correctness against the supplied question. "
        "Return only the corrected final answer, without commentary.\n\n"
        f"QUESTION:\n{question}\n\nAWQ DRAFT:\n{draft}"
    )


def _estimate_cost(usage: dict[str, Any], input_cost: float, output_cost: float) -> float:
    return (usage.get("prompt_tokens", 0) / 1_000_000) * input_cost + (
        usage.get("completion_tokens", 0) / 1_000_000
    ) * output_cost


def rewrite(
    *, url: str, api_key: str, model: str, question: str, draft: str,
    timeout: float = 60.0, max_tokens: int = 256, max_retries: int = 2,
    budget_usd: float | None = None, input_cost_per_million: float = 0.0,
    output_cost_per_million: float = 0.0,
) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict finance answer critic."},
            {"role": "user", "content": critic_prompt(question, draft)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    result: dict[str, object] = {
        "status": "error", "model": model, "draft": draft,
        "rewritten": None, "retry_count": 0, "usage": {}, "estimated_cost_usd": 0.0,
    }
    for attempt in range(max_retries + 1):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), 4))
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"].get("content") or ""
            usage = body.get("usage", {})
            cost = _estimate_cost(usage, input_cost_per_million, output_cost_per_million)
            result.update({"status": "ok", "rewritten": content, "usage": usage,
                           "estimated_cost_usd": cost, "retry_count": attempt})
            if budget_usd is not None and cost > budget_usd:
                result.update({"status": "error", "rewritten": None, "primary_result": "HOLD",
                               "error": "budget exceeded"})
            return result
        except Exception as exc:
            result.update({"retry_count": attempt, "error": f"{type(exc).__name__}: {exc}"})
    result["primary_result"] = "HOLD"
    result["error"] = "retry budget exhausted"
    return result
