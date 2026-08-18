"""Runtime shim installed into the Hermes Discord gateway image.

The shim is intentionally limited to message admission and final Discord
publication.  It does not change Discord permissions, mention rules,
history backfill policy, or the Hermes session/worker implementation.
"""

from __future__ import annotations

import functools
import logging
import os
import copy
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from orchestration.discord_idempotency import (
    ClaimResult,
    DiscordIdempotencyStore,
    IdempotencyStoreUnavailable,
    canonical_discord_dedup_key,
    safe_json_log_fields,
)

logger = logging.getLogger(__name__)
_INSTALL_MARKER = "_hgfinance_discord_idempotency_installed"


def _profile_name() -> str:
    return (
        os.getenv("HERMES_PROFILE")
        or os.getenv("HERMES_PROFILE_NAME")
        or os.getenv("HERMES_ACTIVE_PROFILE")
        or "unknown"
    )


def _session_id(adapter: Any, message: Any) -> str | None:
    """Read an explicitly exposed Hermes session identifier, if available."""

    owners = (
        message,
        getattr(message, "metadata", None),
        getattr(message, "context", None),
        adapter,
        getattr(adapter, "session", None),
    )
    for owner in owners:
        for key in ("session_id", "sessionId"):
            value = (
                owner.get(key)
                if isinstance(owner, Mapping)
                else getattr(owner, key, None)
            )
            if value:
                return str(value)
    return None


def _message_context(
    message: Any, adapter: Any | None = None
) -> dict[str, str | None]:
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    channel_id = str(getattr(channel, "id", "") or "unknown")
    parent_id = getattr(channel, "parent_id", None)
    thread_id = channel_id if parent_id else None
    return {
        "guild_id": str(getattr(guild, "id", "") or "dm"),
        "channel_id": channel_id,
        "thread_id": thread_id,
        "session_id": _session_id(adapter, message),
    }


def _log_event(
    adapter: Any,
    *,
    message_id: str,
    context: dict[str, str | None],
    dedup_key: str,
    handler: str,
    dedup_hit: bool = False,
    hermes_invocation_started: bool = False,
    discord_publish_started: bool = False,
    discord_publish_completed: bool = False,
) -> None:
    fields = {
        "producer": "hermes-discord-gateway",
        "profile": _profile_name(),
        "pid": os.getpid(),
        "discord_message_id": message_id,
        "guild_id": context.get("guild_id"),
        "channel_id": context.get("channel_id"),
        "thread_id": context.get("thread_id"),
        "session_id": context.get("session_id"),
        "request_id": f"discord:{message_id}",
        "dedup_key": dedup_key,
        "handler": handler,
        "dedup_hit": dedup_hit,
        "hermes_invocation_started": hermes_invocation_started,
        "discord_publish_started": discord_publish_started,
        "discord_publish_completed": discord_publish_completed,
    }
    logger.info("discord_gateway_event %s", safe_json_log_fields(**fields))


def _store(adapter: Any) -> DiscordIdempotencyStore:
    store = getattr(adapter, "_hgfinance_idempotency", None)
    if store is None:
        store = DiscordIdempotencyStore(Path(os.getenv("HERMES_HOME", "/opt/data")))
        adapter._hgfinance_idempotency = store
    return store


def _claim_inbound(adapter: Any, message: Any, *, handler: str) -> tuple[str, dict[str, str | None], ClaimResult]:
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        raise IdempotencyStoreUnavailable("Discord message has no message_id")
    context = _message_context(message, adapter)
    dedup_key = canonical_discord_dedup_key(
        context["guild_id"], context["channel_id"], message_id
    )
    result = _store(adapter).claim_inbound(
        dedup_key=dedup_key,
        message_id=message_id,
        guild_id=str(context["guild_id"]),
        channel_id=str(context["channel_id"]),
        thread_id=context["thread_id"],
        profile=_profile_name(),
        handler=handler,
        session_id=context["session_id"],
    )
    return dedup_key, context, result


def _wrap_init(cls: type[Any]) -> None:
    original = cls.__init__

    @functools.wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        _store(self)

    cls.__init__ = wrapped


def _wrap_admission(cls: type[Any]) -> None:
    original = cls._discord_message_admission

    @functools.wraps(original)
    def wrapped(self: Any, message: Any, *args: Any, **kwargs: Any) -> tuple[bool, bool]:
        message_id = str(getattr(message, "id", "") or "")
        context = _message_context(message, self)
        dedup_key = canonical_discord_dedup_key(
            context["guild_id"], context["channel_id"], message_id
        )
        if bool(kwargs.get("claim", False)) and message_id and self._dedup.contains(message_id):
            _log_event(
                self,
                message_id=message_id,
                context=context,
                dedup_key=dedup_key,
                handler="live",
                dedup_hit=True,
            )
            return False, False
        admitted, role_authorized = original(self, message, *args, **kwargs)
        if not admitted:
            return admitted, role_authorized

        claim = bool(kwargs.get("claim", False))
        handler = "live" if claim else "history_backfill"
        try:
            dedup_key, context, result = _claim_inbound(self, message, handler=handler)
        except IdempotencyStoreUnavailable as exc:
            # A failed closed ledger is safer than invoking Hermes without an
            # idempotency claim.  Do not change permission/mention policy.
            if message_id:
                self._dedup.discard(message_id)
            logger.error("discord_gateway_event idempotency_unavailable error_type=%s", type(exc).__name__)
            return False, False

        if not result.admitted:
            _log_event(
                self,
                message_id=message_id,
                context=context,
                dedup_key=dedup_key,
                handler=handler,
                dedup_hit=True,
            )
            return False, False

        _store(self).mark_inbound(dedup_key, "PROCESSING", _profile_name())
        _log_event(
            self,
            message_id=message_id,
            context=context,
            dedup_key=dedup_key,
            handler=handler,
            hermes_invocation_started=True,
        )
        return admitted, role_authorized

    cls._discord_message_admission = wrapped


def _event_key(adapter: Any, event: Any) -> str | None:
    message = getattr(event, "raw_message", None)
    message_id = str(getattr(message, "id", "") or getattr(event, "message_id", "") or "")
    if not message_id:
        return None
    return _store(adapter).inbound_key_for_message(message_id, _profile_name())


# BFF canonical ingress 전달(2026-08-18). 비어 있으면 **기능이 꺼진다** -
# 그때 동작은 이 코드가 없던 때와 정확히 같다(Hermes가 직접 처리).
#
# ## 왜 BFF를 거치게 하나
#
# Hermes가 직접 처리하면 root Kanban 카드를 CEO Agent가 만든다. 그 경로는
# `orchestration/ceo_workflow_scope.build_root_body()`를 지나지 않으므로
# **Mandate 스냅샷도 `requested_by=`도 붙지 않는다** - 웹에서 물으면 붙고
# Discord에서 물으면 안 붙는 상태였다. BFF를 거치면 두 경로가 같은 카드를 만든다.
#
# ## 전달에 성공하면 Hermes는 이 메시지를 처리하지 않는다
#
# 둘 다 처리하면 한 질문에 워크플로가 두 개 생긴다. 그래서 전달이 성공한
# 경우에만 원래 핸들러를 건너뛴다. **실패하면 반드시 원래 경로로 흘린다** -
# 조용히 버리면 사용자는 봇이 죽은 것으로 본다.
INGRESS_URL_ENV = "HGFINANCE_DISCORD_INGRESS_URL"
INGRESS_TIMEOUT_ENV = "HGFINANCE_DISCORD_INGRESS_TIMEOUT_SECONDS"
INGRESS_SECRET_ENV = "CEO_DISCORD_INGRESS_API_KEY"
INGRESS_PROFILES = frozenset({"ceo-agent", "trading-department"})


def _ingress_url() -> str:
    return os.getenv(INGRESS_URL_ENV, "").strip()


def _ingress_secret() -> str | None:
    value = os.getenv(INGRESS_SECRET_ENV, "").strip()
    if (
        len(value.encode("utf-8")) < 32
        or len(set(value)) <= 1
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        return None
    return value


def _author_id(message: Any) -> str:
    author = getattr(message, "author", None)
    return str(getattr(author, "id", "") or "")


def _author_is_bot(message: Any) -> bool:
    """봇이 쓴 글인가. 판단할 수 없으면 **봇으로 본다**.

    미러 게시물(`[web-mirror]`)은 봇이 쓴다. 그걸 사람 발화로 오인해 BFF로
    보내면 웹 질문 하나가 워크플로를 다시 만들고, 그 답변이 또 채널에 올라가
    순환한다. 확신이 없을 때 사람으로 취급하는 쪽이 그 순환을 만든다.
    """

    author = getattr(message, "author", None)
    if author is None:
        return True
    value = getattr(author, "bot", None)
    return True if value is None else bool(value)


def _forward_to_ingress(message: Any, adapter: Any) -> bool:
    """사람 메시지를 `/ui/ceo/ingress`로 넘긴다. 넘겼으면 True.

    False면 호출자가 원래 Hermes 경로로 흘린다 - 설정이 없을 때, 봇 메시지일 때,
    전달이 실패했을 때 전부 False다.
    """

    url = _ingress_url()
    # Both human-facing PAPER order channels terminate at the same canonical
    # BFF boundary.  A message written to Trading still creates the governed
    # CEO root + Trading child workflow; it must never bypass Kanban or call
    # the OMS directly.  Every other department keeps its normal Hermes path.
    if not url or _profile_name() not in INGRESS_PROFILES:
        return False
    ingress_secret = _ingress_secret()
    if ingress_secret is None:
        logger.error("discord-ingress status=failed reason=credential_unavailable")
        return False
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return False
    if _author_is_bot(message):
        logger.info(
            "discord-ingress status=skipped reason=bot_author message_id=%s", message_id
        )
        return False

    context = _message_context(message, adapter)
    payload = {
        "query": str(getattr(message, "content", "") or "").strip(),
        # `discord:<message_id>` 형식을 지킨다 - `discord_delivery`의
        # `_message_id_from_request_id()`가 이 접두어를 보고 뒤를 잘라 쓴다.
        "request_id": f"discord:{message_id}",
        "source": "discord",
        "source_message_id": message_id,
        "actor_id": _author_id(message),
        "actor_type": "user",
        "mirrored": False,
        # 발송 좌표. 이 값이 root body에 적혀야 부서 진행·최종 답변이
        # **사용자가 쓴 그 메시지**에 붙는다. BFF는 이 출처를 보고 미러 재게시를
        # 건너뛴다(`apps/api/ceo_mirror_api._ceo_query`).
        "discord_channel_id": str(context["channel_id"]),
        "discord_message_id": message_id,
        "discord_guild_id": str(context["guild_id"]),
    }
    if not payload["query"]:
        return False

    try:
        import json as _json
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=_json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ingress_secret}",
            },
            method="POST",
        )
        timeout = float(os.getenv(INGRESS_TIMEOUT_ENV, "10"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        # 409는 **같은 메시지를 이미 받았다**는 뜻이다(mirror dedup). 다시
        # Hermes로 흘리면 중복 실행이 되므로 "넘겼다"로 친다.
        if exc.code == 409:
            logger.info(
                "discord-ingress status=duplicate message_id=%s", message_id
            )
            return True
        logger.warning(
            "discord-ingress status=failed reason=http_%s message_id=%s",
            exc.code,
            message_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - 전달 실패는 기존 경로로 되돌린다.
        logger.warning(
            "discord-ingress status=failed reason=transport exception_type=%s message_id=%s",
            type(exc).__name__,
            message_id,
        )
        return False

    if status not in (200, 202):
        logger.warning(
            "discord-ingress status=failed reason=http_%s message_id=%s",
            status,
            message_id,
        )
        return False
    logger.info("discord-ingress status=forwarded message_id=%s", message_id)
    return True


def _wrap_handle_message(cls: type[Any]) -> None:
    if not hasattr(cls, "_handle_message"):
        return
    original = cls._handle_message

    def with_routing_context(message: Any, adapter: Any) -> Any:
        """Expose explicit correlation to the direct CEO planner.

        The direct Discord session does not call the BFF.  A private-looking
        routing block gives the CEO tool flow the same stable identifiers the
        BFF path already carries, without changing mention/permission policy.
        Non-CEO profiles and history/context messages are left untouched.
        """

        if _profile_name() != "ceo-agent":
            return message
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return message
        context = _message_context(message, adapter)
        content = str(getattr(message, "content", "") or "")
        marker = "[hgfinance discord routing context]"
        if marker in content:
            return message
        try:
            enriched = copy.copy(message)
            enriched.content = (
                f"{content}\n\n{marker}\n"
                f"discord_request_id=discord:{message_id}\n"
                f"discord_message_id={message_id}\n"
                f"discord_guild_id={context['guild_id']}\n"
                f"discord_channel_id={context['channel_id']}\n"
                f"discord_thread_id={context['thread_id'] or ''}\n"
                f"discord_session_id={context['session_id'] or ''}\n"
                "Do not quote this routing block in the user-facing response."
            )
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "discord-correlation root=unavailable status=context_injection_skipped"
            )
            return message
        return enriched

    @functools.wraps(original)
    async def wrapped(self: Any, message: Any, *args: Any, **kwargs: Any) -> bool:
        # BFF ingress로 넘겼으면 Hermes는 이 메시지를 처리하지 않는다 - 둘 다
        # 처리하면 한 질문에 워크플로가 두 개 생긴다. 넘기지 못했으면(설정 없음·
        # 봇 메시지·전달 실패) 아래 기존 경로가 그대로 돈다.
        if _forward_to_ingress(message, self):
            return True
        try:
            result = await original(
                self,
                with_routing_context(message, self),
                *args,
                **kwargs,
            )
            resolved_session_id = _session_id(self, result) or _session_id(
                self, message
            )
            if resolved_session_id:
                _store(self).bind_inbound_session(
                    str(getattr(message, "id", "") or ""),
                    resolved_session_id,
                    _profile_name(),
                )
            return result
        except Exception:
            message_id = str(getattr(message, "id", "") or "")
            key = _store(self).inbound_key_for_message(message_id, _profile_name()) if message_id else None
            if key:
                _store(self).mark_inbound(key, "FAILED", _profile_name())
            raise
        if not result:
            message_id = str(getattr(message, "id", "") or "")
            key = _store(self).inbound_key_for_message(message_id, _profile_name()) if message_id else None
            if key:
                _store(self).mark_inbound(key, "FAILED", _profile_name())
        return result

    cls._handle_message = wrapped


def _wrap_processing_complete(cls: type[Any]) -> None:
    if not hasattr(cls, "on_processing_complete"):
        return
    original = cls.on_processing_complete

    @functools.wraps(original)
    async def wrapped(self: Any, event: Any, outcome: Any) -> None:
        key = _event_key(self, event)
        try:
            await original(self, event, outcome)
        finally:
            if key:
                state = "COMPLETED" if getattr(outcome, "name", str(outcome)) == "SUCCESS" else "FAILED"
                _store(self).mark_inbound(key, state, _profile_name())

    cls.on_processing_complete = wrapped


def _response_anchor(metadata: Any, reply_to: Any) -> str | None:
    if reply_to:
        return str(reply_to)
    if isinstance(metadata, dict):
        for field in ("discord_message_id", "request_id"):
            value = metadata.get(field)
            if value:
                return str(value)
    return None


def _send_result(success: bool, *, message_id: str | None = None, error: str | None = None) -> Any:
    from gateway.platforms.base import SendResult

    return SendResult(
        success=success,
        message_id=message_id,
        error=error,
        raw_response={"dedup_hit": True} if message_id or error is None else None,
    )


def _wrap_send(cls: type[Any]) -> None:
    original = cls.send

    @functools.wraps(original)
    async def wrapped(
        self: Any,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        final_delivery = bool(isinstance(metadata, dict) and metadata.get("notify"))
        anchor = _response_anchor(metadata, reply_to) if final_delivery else None
        if not anchor:
            return await original(self, chat_id, content, reply_to, metadata)

        store = _store(self)
        inbound_key = store.inbound_key_for_message(anchor, _profile_name())
        dedup_key = inbound_key or canonical_discord_dedup_key("unknown", chat_id, anchor)
        response_key = f"{dedup_key}:gateway"
        context = store.inbound_context(inbound_key, _profile_name()) if inbound_key else {
            "guild_id": "unknown",
            "channel_id": str(chat_id),
            "thread_id": str(metadata.get("thread_id")) if isinstance(metadata, dict) and metadata.get("thread_id") else None,
        }
        try:
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=_profile_name(),
            )
        except IdempotencyStoreUnavailable as exc:
            logger.error("discord_gateway_event idempotency_unavailable error_type=%s", type(exc).__name__)
            return _send_result(False, error="Discord outbound idempotency ledger unavailable")

        if not claim.admitted:
            _log_event(
                self,
                message_id=anchor,
                context=context,
                dedup_key=dedup_key,
                handler="outbound",
                dedup_hit=True,
            )
            return _send_result(True, message_id=claim.response_message_id)

        _log_event(
            self,
            message_id=anchor,
            context=context,
            dedup_key=dedup_key,
            handler="outbound",
            discord_publish_started=True,
        )
        try:
            result = await original(self, chat_id, content, reply_to, metadata)
            store.mark_outbound(
                response_key,
                "COMPLETED" if bool(getattr(result, "success", False)) else "FAILED",
                _profile_name(),
                str(getattr(result, "message_id", "") or "") or None,
            )
            _log_event(
                self,
                message_id=anchor,
                context=context,
                dedup_key=dedup_key,
                handler="outbound",
                discord_publish_completed=bool(getattr(result, "success", False)),
            )
            return result
        except Exception:
            store.mark_outbound(response_key, "FAILED", _profile_name())
            raise

    cls.send = wrapped


def install(cls: type[Any]) -> None:
    """Install the shim exactly once when Hermes imports DiscordAdapter."""

    if getattr(cls, _INSTALL_MARKER, False):
        return
    _wrap_init(cls)
    _wrap_admission(cls)
    _wrap_handle_message(cls)
    _wrap_processing_complete(cls)
    _wrap_send(cls)
    setattr(cls, _INSTALL_MARKER, True)
    logger.info("HgFinance Discord durable idempotency enabled")
