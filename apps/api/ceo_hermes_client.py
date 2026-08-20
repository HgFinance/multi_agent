"""Authenticated client for the existing CEO Hermes API server.

The portfolio BFF does not start a Hermes gateway.  ``ceo-hermes`` owns the
CEO profile, credentials, tool allowlist, and agent process; this module only
uses its documented OpenAI-compatible API surface.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import HTTPException


def _enabled() -> bool:
    return os.getenv("ENABLE_AGENT_ASK", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _chat_endpoint() -> str:
    base_url = os.getenv("HERMES_CEO_API_URL", "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail="CEO Hermes API 연결이 설정되지 않았습니다.",
        )
    if base_url.endswith("/v1/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _response_error(response: httpx.Response) -> str:
    """Return a bounded upstream error without leaking credentials."""

    try:
        payload = response.json()
    except ValueError:
        return f"CEO Hermes API HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:500]
        if isinstance(payload.get("detail"), str):
            return payload["detail"][:500]
    return f"CEO Hermes API HTTP {response.status_code}"



def _publish_ceo_turn(*, started: float, status: str) -> None:
    """CEO 부서장 턴 1건을 HR 관측으로 내보낸다(2026-08-20). 실패는 삼킨다.

    CEO 만 다른 경로다 - 다른 부서장은 hermes_boundary.ask() 가 CLI 를 띄우지만
    CEO 는 ceo-hermes 게이트웨이에 HTTP 로 묻는다. 계측 계약은 같게 맞춘다.
    신원(executive-orchestrator)은 CEO Profile 의 agent.head_persona 에서 읽는다 -
    여기 문자열로 박으면 Profile 이 바뀌어도 아무도 모른다.
    """

    try:
        try:
            from hermes_boundary import head_persona_of
        except ModuleNotFoundError:
            from apps.api.hermes_boundary import head_persona_of
        from orchestration.llm_observability import publish_head_activity

        persona = head_persona_of("departments/00-ceo-office/hermes/config.yaml")
        if not persona:
            return
        publish_head_activity(
            stage="ceo",
            head_persona=persona,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_count=0 if status == "COMPLETED" else 1,
        )
    except Exception:  # noqa: BLE001 - 계측이 CEO 질의를 죽이지 못한다
        return


def ask_ceo(*, query: str, timeout: float) -> dict[str, Any]:
    """Run one CEO turn through the existing ``ceo-hermes`` gateway.

    ``HERMES_CEO_API_URL`` and ``HERMES_CEO_API_KEY`` are deployment
    configuration, not request data.  A configured remote endpoint never
    falls back to a second local CEO runtime: a failed gateway call is
    reported to the BFF caller instead.
    """

    if not _enabled():
        raise HTTPException(
            status_code=503,
            detail="Agent 인증·Tool Allowlist 연결 전까지 기본 비활성화 상태입니다.",
        )

    api_key = os.getenv("HERMES_CEO_API_KEY", "").strip()
    if len(api_key) < 16:
        raise HTTPException(
            status_code=503,
            detail="CEO Hermes API 인증키가 설정되지 않았습니다.",
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("HERMES_CEO_API_MODEL", "hermes-agent"),
        "messages": [{"role": "user", "content": query}],
        "stream": False,
    }

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(_chat_endpoint(), headers=headers, json=payload)
    except httpx.RequestError as exc:
        _publish_ceo_turn(started=started, status="DEGRADED")
        raise HTTPException(
            status_code=503,
            detail="CEO Hermes API에 연결할 수 없습니다.",
        ) from exc

    if response.status_code >= 400:
        _publish_ceo_turn(started=started, status="DEGRADED")
        raise HTTPException(status_code=502, detail=_response_error(response))

    _publish_ceo_turn(started=started, status="COMPLETED")

    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="CEO Hermes API가 잘못된 JSON을 반환했습니다.",
        ) from exc

    choices = body.get("choices") if isinstance(body, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else None
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    answer = message.get("content") if isinstance(message, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise HTTPException(
            status_code=502,
            detail="CEO Hermes API 응답에 답변이 없습니다.",
        )

    return {
        "department": "ceo-agent",
        "answer": answer.strip(),
        "session_id": response.headers.get("X-Hermes-Session-Id"),
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


__all__ = ["ask_ceo"]
