"""Runtime shim installed into the Hermes Discord gateway image.

The shim is intentionally limited to message admission and final Discord
publication.  It does not change Discord permissions, mention rules,
history backfill policy, or the Hermes session/worker implementation.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestration.discord_idempotency import (
    ClaimResult,
    DiscordIdempotencyStore,
    IdempotencyStoreUnavailable,
    canonical_discord_dedup_key,
    safe_json_log_fields,
)
from orchestration.qa_discord_feedback import (
    QA_FEEDBACK_MARKER,
    QaFeedbackCommand,
    artifact_id_from_text,
    parse_qa_feedback_command,
    qa_feedback_channel_id,
    submit_qa_feedback_decision,
)

logger = logging.getLogger(__name__)
_INSTALL_MARKER = "_hgfinance_discord_idempotency_installed"
_PREFILTER_DROP_REASONS = frozenset(
    {
        "BOT_AUTHOR",
        "SELF_MESSAGE",
        "WEBHOOK",
        "UNSUPPORTED_MESSAGE_TYPE",
        "CHANNEL_POLICY",
        "THREAD_POLICY",
        "MENTION_POLICY",
        "EMPTY_OR_UNSUPPORTED",
        "DEDUP",
        "OTHER",
    }
)


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


def _safe_message_type(message: Any) -> str:
    message_type = getattr(message, "type", None)
    name = getattr(message_type, "name", None)
    if name:
        return str(name)
    value = getattr(message_type, "value", None)
    if value is not None:
        return str(value)
    return "unknown"


def _safe_is_webhook(message: Any) -> bool:
    try:
        return bool(getattr(message, "webhook_id", None))
    except Exception:  # pragma: no cover - defensive against Discord objects
        return False


def _safe_has_bot_mention(adapter: Any, message: Any) -> bool:
    checker = getattr(adapter, "_self_is_raw_mentioned", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(message))
    except Exception:  # pragma: no cover - telemetry must never affect ingress
        return False


def _raw_message_fields(adapter: Any, message: Any) -> dict[str, Any]:
    context = _message_context(message, adapter)
    channel = getattr(message, "channel", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        parent = getattr(channel, "parent", None)
        parent_id = getattr(parent, "id", None)
    author = getattr(message, "author", None)
    is_webhook = _safe_is_webhook(message)
    is_bot = bool(getattr(author, "bot", False)) if author is not None else False
    if is_webhook:
        author_kind = "webhook"
    elif author is None:
        author_kind = "unknown"
    elif is_bot:
        author_kind = "bot"
    else:
        author_kind = "human"
    return {
        "message_id": str(getattr(message, "id", "") or "unknown"),
        "guild_id": context.get("guild_id"),
        "channel_id": context.get("channel_id"),
        # For a thread this is its parent; for a parent/DM it is the current
        # channel.  The current channel remains in channel_id above.
        "thread_or_parent_id": str(parent_id or context.get("channel_id") or "unknown"),
        "author_kind": author_kind,
        "message_type": _safe_message_type(message),
        "is_bot": is_bot,
        "is_webhook": is_webhook,
        "has_bot_mention": _safe_has_bot_mention(adapter, message),
    }


def _log_raw_message(adapter: Any, message: Any) -> None:
    try:
        logger.info(
            "discord-raw-message %s",
            safe_json_log_fields(**_raw_message_fields(adapter, message)),
        )
    except Exception:  # pragma: no cover - logging must not affect ingress
        logger.debug("discord raw-message telemetry failed", exc_info=True)


def _log_pre_filter_drop(
    adapter: Any,
    message: Any,
    *,
    stage: str,
    reason: str,
) -> None:
    try:
        if reason not in _PREFILTER_DROP_REASONS:
            reason = "OTHER"
        context = _message_context(message, adapter)
        logger.info(
            "discord-pre-filter-drop %s",
            safe_json_log_fields(
                message_id=str(getattr(message, "id", "") or "unknown"),
                stage=stage,
                reason=reason,
                guild_id=context.get("guild_id"),
                channel_id=context.get("channel_id"),
            ),
        )
    except Exception:  # pragma: no cover - logging must not affect ingress
        logger.debug("discord pre-filter telemetry failed", exc_info=True)


def _admission_drop_reason(adapter: Any, message: Any, *, claim: bool) -> str:
    """Best-effort reason label for a false admission result.

    Hermes remains the sole owner of admission semantics.  This function only
    inspects the same stable metadata/configuration after Hermes has decided to
    reject the message; it never makes the admission decision itself.
    """

    message_id = str(getattr(message, "id", "") or "")
    dedup = getattr(adapter, "_dedup", None)
    contains = getattr(dedup, "contains", None)
    if message_id and callable(contains):
        try:
            if bool(contains(message_id)):
                return "DEDUP"
        except Exception:
            pass

    client = getattr(adapter, "_client", None)
    author = getattr(message, "author", None)
    client_user = getattr(client, "user", None)
    if author is not None and client_user is not None and author == client_user:
        return "SELF_MESSAGE"

    message_type = _safe_message_type(message).casefold()
    if message_type not in {"unknown", "default", "reply"}:
        return "UNSUPPORTED_MESSAGE_TYPE"

    if _safe_is_webhook(message):
        return "WEBHOOK"

    if bool(getattr(author, "bot", False)):
        try:
            allow_bots = str(adapter._get_allow_bots()).strip().lower()
        except Exception:
            allow_bots = "none"
        if allow_bots == "none":
            return "BOT_AUTHOR"
        try:
            explicit_mention = bool(adapter._self_is_explicitly_mentioned(message))
        except Exception:
            explicit_mention = False
        if allow_bots == "mentions" and not explicit_mention:
            return "MENTION_POLICY"
        try:
            if adapter._discord_bots_require_inline_mention() and not _safe_has_bot_mention(
                adapter, message
            ):
                return "MENTION_POLICY"
        except Exception:
            pass
        return "OTHER"

    # Avoid re-running the potentially role-aware user check on the hot path.
    # When no user/role allowlist or open-mode flag is present, a false human
    # admission is the channel-scoped policy branch in Hermes.  Other policy
    # failures remain deliberately generic rather than guessing.
    try:
        has_user_or_role_policy = bool(
            getattr(adapter, "_allowed_user_ids", set())
            or getattr(adapter, "_allowed_role_ids", set())
        )
        open_mode = bool(
            adapter._discord_allow_all_users() or adapter._gateway_allow_all_users()
        )
        if not has_user_or_role_policy and not open_mode:
            return "CHANNEL_POLICY"
    except Exception:
        pass

    raw_self_mention = False
    try:
        raw_self_mention = bool(adapter._self_is_explicitly_mentioned(message))
    except Exception:
        pass
    mentions = getattr(message, "mentions", None) or ()
    if raw_self_mention or mentions:
        return "MENTION_POLICY"
    try:
        ignore_no_mention = os.getenv("DISCORD_IGNORE_NO_MENTION", "true").lower() in {
            "true",
            "1",
            "yes",
        }
        if ignore_no_mention and not _safe_has_bot_mention(adapter, message):
            free_channels = adapter._discord_free_response_channels()
            channel_keys = adapter._discord_channel_keys(message)
            if "*" not in free_channels and not (channel_keys & free_channels):
                return "MENTION_POLICY"
    except Exception:
        pass
    return "OTHER"


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


def _qa_channel_matches(message: Any) -> bool:
    expected = qa_feedback_channel_id()
    if not expected:
        return False
    channel = getattr(message, "channel", None)
    current = str(getattr(channel, "id", "") or "")
    parent = str(getattr(channel, "parent_id", "") or "")
    return expected in {current, parent}


def _csv_ids(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {
        value
        for value in (part.strip() for part in raw.replace(",", " ").split())
        if value.isdigit()
    }


def _qa_approver_authorized(message: Any) -> bool:
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", "") or "")
    guild_owner_id = str(
        getattr(getattr(message, "guild", None), "owner_id", "") or ""
    )
    if author_id and author_id == guild_owner_id:
        return True
    allowed_users = _csv_ids("QA_DISCORD_APPROVER_USER_IDS")
    allowed_roles = _csv_ids("QA_DISCORD_APPROVER_ROLE_IDS")
    if not allowed_users and not allowed_roles:
        return False
    if author_id in allowed_users:
        return True
    role_ids = {
        str(getattr(role, "id", "") or "")
        for role in (getattr(author, "roles", None) or ())
    }
    return bool(role_ids.intersection(allowed_roles))


async def _qa_reply(message: Any, content: str) -> None:
    reply = getattr(message, "reply", None)
    if callable(reply):
        await reply(str(content)[:1900], mention_author=False)
        return
    send = getattr(getattr(message, "channel", None), "send", None)
    if callable(send):
        await send(str(content)[:1900])


async def _artifact_from_reply(message: Any) -> str | None:
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    artifact_id = artifact_id_from_text(getattr(resolved, "content", ""))
    if artifact_id:
        return artifact_id
    referenced_id = str(getattr(reference, "message_id", "") or "")
    fetch_message = getattr(getattr(message, "channel", None), "fetch_message", None)
    if referenced_id and callable(fetch_message):
        try:
            referenced = await fetch_message(int(referenced_id))
            return artifact_id_from_text(getattr(referenced, "content", ""))
        except Exception:
            logger.info(
                "qa-discord-feedback status=reference_unavailable message_id=%s",
                str(getattr(message, "id", "") or "unknown"),
            )
    return None


async def _maybe_handle_qa_feedback_message(adapter: Any, message: Any) -> bool | None:
    """Own QA review cards and approval commands before normal chat admission."""

    if _profile_name() != "qa-department" or not _qa_channel_matches(message):
        return None
    content = str(getattr(message, "content", "") or "")
    author = getattr(message, "author", None)
    client_user = getattr(getattr(adapter, "_client", None), "user", None)
    message_id = str(getattr(message, "id", "") or "")

    # The background evaluator publishes through this same existing QA bot.
    # The exact marker and self identity are both required before bypassing the
    # upstream self-message rejection and invoking the QA Hermes Agent once.
    if author is not None and client_user is not None and author == client_user:
        if QA_FEEDBACK_MARKER not in content:
            return None
        try:
            dedup_key, _context, claim = _claim_inbound(
                adapter,
                message,
                handler="qa_feedback_agent",
            )
        except IdempotencyStoreUnavailable:
            logger.error("qa-discord-feedback status=failed_closed reason=idempotency_unavailable")
            return True
        if not claim.admitted:
            return True
        _store(adapter).mark_inbound(dedup_key, "PROCESSING", _profile_name())
        try:
            result = await adapter._handle_message(message)
            if not result:
                _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
            return bool(result)
        except Exception:
            _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
            raise

    if bool(getattr(author, "bot", True)):
        return None
    command = parse_qa_feedback_command(content)
    if command is None:
        return None
    try:
        dedup_key, _context, claim = _claim_inbound(
            adapter,
            message,
            handler="qa_feedback_decision",
        )
    except IdempotencyStoreUnavailable:
        await _qa_reply(message, "QA 승인 원장을 사용할 수 없어 요청을 처리하지 않았습니다.")
        return True
    if not claim.admitted:
        return True
    _store(adapter).mark_inbound(dedup_key, "PROCESSING", _profile_name())
    if not _qa_approver_authorized(message):
        _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
        await _qa_reply(message, "이 채널의 QA 승인 권한이 없습니다.")
        return True
    artifact_id = command.artifact_id or await _artifact_from_reply(message)
    if not artifact_id:
        _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
        await _qa_reply(
            message,
            "artifact를 찾지 못했습니다. QA 응답에 Reply하거나 `승인 feedback-... 사유` 형식으로 입력해 주세요.",
        )
        return True
    resolved = QaFeedbackCommand(
        decision=command.decision,
        artifact_id=artifact_id,
        reason=command.reason,
    )
    try:
        status, _body = await asyncio.to_thread(
            submit_qa_feedback_decision,
            resolved,
            actor_id=str(getattr(author, "id", "") or "unknown"),
            message_id=message_id,
        )
    except Exception as exc:
        _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
        logger.warning(
            "qa-discord-feedback status=failed_closed error_type=%s message_id=%s",
            type(exc).__name__,
            message_id,
        )
        await _qa_reply(message, "QA 원장 연결에 실패해 결정은 적용되지 않았습니다.")
        return True
    if status in {200, 201, 202}:
        _store(adapter).mark_inbound(dedup_key, "COMPLETED", _profile_name())
        gate = "offline benchmark PENDING" if resolved.decision == "APPROVED" else "반려 완료"
        await _qa_reply(message, f"{artifact_id}: {resolved.decision} 기록 완료 · {gate}")
    elif status == 409:
        _store(adapter).mark_inbound(dedup_key, "COMPLETED", _profile_name())
        await _qa_reply(message, f"{artifact_id}: 이미 결정된 artifact라 중복 적용하지 않았습니다.")
    else:
        _store(adapter).mark_inbound(dedup_key, "FAILED", _profile_name())
        await _qa_reply(message, f"{artifact_id}: QA 원장이 HTTP {status}로 거부해 적용되지 않았습니다.")
    return True


def _wrap_dispatch(cls: type[Any]) -> None:
    """Log the live Discord callback before any admission policy runs."""

    if not hasattr(cls, "_dispatch_discord_message"):
        return
    original = cls._dispatch_discord_message

    @functools.wraps(original)
    async def wrapped(self: Any, message: Any, *args: Any, **kwargs: Any) -> Any:
        _log_raw_message(self, message)
        handled = await _maybe_handle_qa_feedback_message(self, message)
        if handled is not None:
            return handled
        return await original(self, message, *args, **kwargs)

    cls._dispatch_discord_message = wrapped


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
            _log_pre_filter_drop(
                self,
                message,
                stage="admission",
                reason="DEDUP",
            )
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
            _log_pre_filter_drop(
                self,
                message,
                stage="admission",
                reason=_admission_drop_reason(
                    self,
                    message,
                    claim=bool(kwargs.get("claim", False)),
                ),
            )
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
            _log_pre_filter_drop(
                self,
                message,
                stage="idempotency",
                reason="OTHER",
            )
            logger.error("discord_gateway_event idempotency_unavailable error_type=%s", type(exc).__name__)
            return False, False

        if not result.admitted:
            _log_pre_filter_drop(
                self,
                message,
                stage="idempotency",
                reason="DEDUP",
            )
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
_INGRESS_RETRY_DELAYS_SECONDS = (0.25, 0.75, 1.5)


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


def _mark_ingress_forwarded(adapter: Any, message_id: str) -> None:
    """Close the gateway-side inbound lease after BFF accepted ownership.

    The canonical BFF path is asynchronous: the gateway is finished as soon
    as `/ui/ceo/ingress` accepts (or deduplicates) the message, while Kanban
    and final Discord delivery continue in separate services. Leaving the
    gateway row in PROCESSING makes a healthy handoff look permanently stuck
    and eventually re-admits the same Discord message after the active lease.
    """

    if adapter is None:
        return
    try:
        store = _store(adapter)
        dedup_key = store.inbound_key_for_message(message_id, _profile_name())
        if dedup_key:
            store.mark_inbound(dedup_key, "COMPLETED", _profile_name())
    except IdempotencyStoreUnavailable:
        # The BFF may already have committed the request. Never replay through
        # direct Hermes merely because the local acknowledgement could not be
        # written; mirror dedup remains the authoritative execution boundary.
        logger.error(
            "discord-ingress ledger_ack=failed message_id=%s", message_id
        )


def _mark_ingress_failed(adapter: Any, message_id: str) -> None:
    """Close an unsuccessful handoff honestly so it is not stuck as active."""

    if adapter is None:
        return
    try:
        store = _store(adapter)
        dedup_key = store.inbound_key_for_message(message_id, _profile_name())
        if dedup_key:
            store.mark_inbound(dedup_key, "FAILED", _profile_name())
    except IdempotencyStoreUnavailable:
        logger.error("discord-ingress ledger_fail_ack=failed message_id=%s", message_id)


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
        logger.error(
            "discord-ingress status=failed_closed reason=credential_unavailable"
        )
        return True
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return False
    if _author_is_bot(message):
        logger.info(
            "discord-ingress status=skipped reason=bot_author message_id=%s", message_id
        )
        return False

    context = _message_context(message, adapter)

    # hgfinance-bff-parent-thread-correlation-v1
    #
    # Request-thread routing changes message.channel to the newly created
    # Discord thread.  BFF still needs both coordinates separately:
    #   channel_id -> original parent channel
    #   thread_id  -> request thread
    channel = getattr(message, "channel", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:
        context = dict(context)
        context["channel_id"] = str(parent_id)
        context["thread_id"] = str(
            getattr(channel, "id", "") or context.get("thread_id") or ""
        )

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
        "discord_thread_id": str(context.get("thread_id") or "") or None,
    }
    if not payload["query"]:
        return False

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
    timeout = float(os.getenv(INGRESS_TIMEOUT_ENV, "30"))
    retryable_http = frozenset({429, 500, 502, 503, 504})
    for attempt in range(len(_INGRESS_RETRY_DELAYS_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
        except urllib.error.HTTPError as exc:
            # The BFF binds the exact Discord message ID before execution, so
            # a retry after an ambiguous transport outcome cannot create a
            # second workflow.  409 proves an earlier attempt already won.
            if exc.code == 409:
                logger.info(
                    "discord-ingress status=duplicate message_id=%s", message_id
                )
                _mark_ingress_forwarded(adapter, message_id)
                return True
            if (
                exc.code in retryable_http
                and attempt < len(_INGRESS_RETRY_DELAYS_SECONDS)
            ):
                time.sleep(_INGRESS_RETRY_DELAYS_SECONDS[attempt])
                continue
            logger.warning(
                "discord-ingress status=failed_closed reason=http_%s message_id=%s",
                exc.code,
                message_id,
            )
            _mark_ingress_failed(adapter, message_id)
            return True
        except Exception as exc:  # noqa: BLE001 - retried with one stable request ID.
            if attempt < len(_INGRESS_RETRY_DELAYS_SECONDS):
                time.sleep(_INGRESS_RETRY_DELAYS_SECONDS[attempt])
                continue
            logger.warning(
                "discord-ingress status=failed_closed reason=transport "
                "exception_type=%s message_id=%s",
                type(exc).__name__,
                message_id,
            )
            _mark_ingress_failed(adapter, message_id)
            return True

        if status in (200, 202):
            break
        if status in retryable_http and attempt < len(_INGRESS_RETRY_DELAYS_SECONDS):
            time.sleep(_INGRESS_RETRY_DELAYS_SECONDS[attempt])
            continue
        logger.warning(
            "discord-ingress status=failed_closed reason=http_%s message_id=%s",
            status,
            message_id,
        )
        _mark_ingress_failed(adapter, message_id)
        return True
    _mark_ingress_forwarded(adapter, message_id)
    logger.info("discord-ingress status=forwarded message_id=%s", message_id)
    return True


async def _forward_to_ingress_async(message: Any, adapter: Any) -> bool:
    """Forward without blocking Discord's heartbeat/event-loop thread."""

    return await asyncio.to_thread(_forward_to_ingress, message, adapter)


async def _ensure_request_thread(adapter: Any, message: Any) -> Any:
    """Create exactly one HgFinance request thread for a CEO Discord request.

    hgfinance-direct-request-thread-v1

    Hermes upstream auto-threading is disabled for HgFinance.  This replacement
    performs one direct Discord thread-create request only: no seed-message
    fallback and no retry storm.  Failure is fail-open and the request continues
    in the parent channel.
    """

    if _profile_name() != "ceo-agent":
        return message

    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return message

    if _author_is_bot(message):
        return message

    context = _message_context(message, adapter)

    # Already inside a Discord thread.
    if context.get("thread_id"):
        return message

    # DMs cannot host Discord threads.
    if not context.get("guild_id"):
        return message

    message_type = getattr(message, "type", None)
    message_type_name = str(getattr(message_type, "name", "") or "").casefold()

    # Preserve Hermes' existing rule: parent-channel replies are not
    # auto-threaded into a second conversation.
    if message_type_name == "reply":
        return message

    create_thread = getattr(message, "create_thread", None)
    derive_name = getattr(adapter, "_derive_auto_thread_name", None)

    if not callable(create_thread) or not callable(derive_name):
        logger.warning(
            "hgfinance-request-thread status=skipped "
            "reason=thread_api_unavailable message_id=%s",
            message_id,
        )
        return message

    thread_name = derive_name(str(getattr(message, "content", "") or ""))

    try:
        thread = await create_thread(
            name=thread_name,
            auto_archive_duration=1440,
        )
    except Exception as exc:
        # Thread UX is supplementary.  A Discord 429 or other thread failure
        # must never cancel the CEO request.
        logger.warning(
            "hgfinance-request-thread status=failed "
            "message_id=%s error_type=%s",
            message_id,
            type(exc).__name__,
        )
        return message

    thread_id = str(getattr(thread, "id", "") or "")
    if not thread_id:
        logger.warning(
            "hgfinance-request-thread status=failed "
            "reason=missing_thread_id message_id=%s",
            message_id,
        )
        return message

    # Persist the exact request -> thread correlation used later by the
    # supervisor's department cards and detached CEO synthesis.
    try:
        _store(adapter).bind_inbound_thread(
            message_id,
            thread_id,
            _profile_name(),
        )
    except Exception as exc:
        logger.warning(
            "hgfinance-request-thread-ledger status=failed "
            "message_id=%s thread_id=%s error_type=%s",
            message_id,
            thread_id,
            type(exc).__name__,
        )

    # Match Hermes' own successful auto-thread bookkeeping so the starter
    # MESSAGE_CREATE event cannot trigger another agent execution.
    try:
        threads = getattr(adapter, "_threads", None)
        if threads is not None:
            threads.mark(thread_id)
    except Exception:
        logger.debug(
            "hgfinance-request-thread thread-cache mark failed",
            exc_info=True,
        )

    try:
        dedup = getattr(adapter, "_dedup", None)
        if dedup is not None:
            dedup.is_duplicate(thread_id)
    except Exception:
        logger.debug(
            "hgfinance-request-thread starter dedup mark failed",
            exc_info=True,
        )

    try:
        routed = copy.copy(message)
        routed.channel = thread
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "hgfinance-request-thread status=route_copy_failed "
            "message_id=%s thread_id=%s",
            message_id,
            thread_id,
        )
        return message

    logger.info(
        "hgfinance-request-thread status=created "
        "message_id=%s thread_id=%s",
        message_id,
        thread_id,
    )

    return routed


def _wrap_handle_message(cls: type[Any]) -> None:
    if not hasattr(cls, "_handle_message"):
        return
    original = cls._handle_message

    def with_routing_context(message: Any, adapter: Any) -> Any:
        """Expose explicit Discord correlation to the CEO planner.

        The fallback Hermes path needs the same routing identifiers as BFF ingress.  A private-looking
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

        # hgfinance-parent-thread-correlation-v1
        #
        # The CEO runs inside the request thread, but detached supervisor
        # responses need two separate coordinates:
        #   discord_channel_id -> original parent channel
        #   discord_thread_id  -> request thread
        #
        # `_message_context()` intentionally reports the current channel, so
        # normalize only the routing block here when the current channel is a
        # Discord thread.
        channel = getattr(message, "channel", None)
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            context = dict(context)
            context["channel_id"] = str(parent_id)
            context["thread_id"] = str(
                getattr(channel, "id", "") or context.get("thread_id") or ""
            )

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
        # hgfinance-bff-request-thread-v1
        #
        # HgFinance owns the request thread before choosing BFF ingress versus
        # direct Hermes fallback.  Both paths therefore share one correlation.
        # Upstream Hermes auto-threading remains disabled.
        routed_message = await _ensure_request_thread(self, message)

        # BFF ingress is the canonical path when configured. Forward the
        # routed message so the BFF receives both the parent channel and the
        # actual request-thread id. Once selected, this boundary owns the
        # message even when the outcome is ambiguous; replaying through direct
        # Hermes could create a second workflow after the BFF committed.
        if await _forward_to_ingress_async(routed_message, self):
            return True

        try:
            result = await original(
                self,
                with_routing_context(routed_message, self),
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
    _wrap_dispatch(cls)
    _wrap_admission(cls)
    _wrap_handle_message(cls)
    _wrap_processing_complete(cls)
    _wrap_send(cls)
    setattr(cls, _INSTALL_MARKER, True)
    logger.info("HgFinance Discord durable idempotency enabled")
