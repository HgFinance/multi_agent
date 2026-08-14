"""Small, fail-closed Discord delivery adapter for CEO synthesis results."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestration.discord_idempotency import (
    DiscordIdempotencyStore,
    IdempotencyStoreUnavailable,
    canonical_discord_dedup_key,
)

logger = logging.getLogger(__name__)

_CORRELATION_RE = re.compile(
    r"(?m)^(?:discord_)?(?P<key>request_id|message_id|guild_id|channel_id|thread_id|session_id)=(?P<value>\S+)\s*$"
)


@dataclass(frozen=True)
class DiscordCorrelation:
    request_id: str | None = None
    message_id: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None


def _merge(base: dict[str, str], values: Mapping[str, Any]) -> None:
    aliases = {
        "discord_request_id": "request_id",
        "discord_message_id": "message_id",
        "discord_guild_id": "guild_id",
        "discord_channel_id": "channel_id",
        "discord_thread_id": "thread_id",
        "discord_session_id": "session_id",
    }
    for key, value in values.items():
        normalized = aliases.get(str(key), str(key))
        if normalized in {"request_id", "message_id", "guild_id", "channel_id", "thread_id", "session_id"} and value:
            base.setdefault(normalized, str(value))


def _find_in_mapping(value: Any, result: dict[str, str]) -> None:
    if isinstance(value, Mapping):
        _merge(result, value)
        for key in ("body", "comment", "content"):
            if key in value:
                _find_in_mapping(value[key], result)
        for key in (
            "metadata",
            "workflow_metadata",
            "run_metadata",
            "task_run_metadata",
            "task_run",
            "discord_context",
            "correlation",
            "root_task",
            "root_payload",
            "workflow_root",
        ):
            if key in value:
                _find_in_mapping(value[key], result)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _find_in_mapping(item, result)
    elif isinstance(value, str):
        for match in _CORRELATION_RE.finditer(value):
            result.setdefault(match.group("key"), match.group("value"))


def correlation_from_task(task: Mapping[str, Any]) -> DiscordCorrelation:
    """Read explicit correlation only from the completed synthesis task/root."""

    values: dict[str, str] = {}
    _find_in_mapping(task, values)
    body = str(task.get("body") or "")
    for match in _CORRELATION_RE.finditer(body):
        values.setdefault(match.group("key"), match.group("value"))
    comments = task.get("comments")
    _find_in_mapping(comments, values)
    return DiscordCorrelation(**values)


def _message_id_from_request_id(request_id: str | None) -> str | None:
    if not request_id:
        return None
    value = str(request_id)
    if value.startswith("discord:"):
        tail = value.rsplit(":", 1)[-1]
        return tail or None
    return None


def _token_from_env(env: Mapping[str, str], profile: str) -> str | None:
    token = env.get("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()
    home = Path(env.get("HERMES_HOME", "/opt/data"))
    for env_path in (home / ".env", home / "profiles" / profile / ".env"):
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "DISCORD_BOT_TOKEN":
                    return value.strip().strip('"').strip("'") or None
        except OSError:
            continue
    return None


class DiscordFinalDelivery:
    """Publish one final CEO answer through Discord's existing bot identity."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        sender: Callable[[str, str, Mapping[str, str]], Mapping[str, Any]] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.sender = sender or self._send_http
        self.timeout = timeout

    def _send_http(
        self,
        channel_id: str,
        payload: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        request = Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=payload.encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body) if body else {}
        return decoded if isinstance(decoded, Mapping) else {}

    def deliver(
        self,
        *,
        root_task_id: str,
        synthesis_task: Mapping[str, Any],
        content: str,
        store: DiscordIdempotencyStore,
        profile: str = "ceo-agent",
    ) -> str:
        correlation = correlation_from_task(synthesis_task)
        message_id = correlation.message_id or _message_id_from_request_id(correlation.request_id)
        logger.info(
            "discord-correlation root=%s request_id=%s session_id=%s channel_id=%s message_id=%s",
            root_task_id,
            correlation.request_id or "",
            correlation.session_id or "",
            correlation.channel_id or "",
            message_id or "",
        )
        if not message_id:
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"

        inbound_key = store.inbound_key_for_message(message_id, profile)
        context: Mapping[str, str | None] = {}
        if inbound_key:
            context = store.inbound_context(inbound_key, profile)
        guild_id = correlation.guild_id or context.get("guild_id") or "unknown"
        channel_id = correlation.channel_id or context.get("channel_id")
        if not channel_id:
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"
        dedup_key = (
            inbound_key
            if inbound_key
            else canonical_discord_dedup_key(guild_id, channel_id, message_id)
        )
        response_key = f"{dedup_key}:final"
        try:
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=profile,
            )
        except IdempotencyStoreUnavailable:
            logger.error(
                "discord-final-delivery root=%s status=failed error=ledger_unavailable",
                root_task_id,
            )
            return "failed"
        if not claim.admitted:
            logger.info(
                "discord-final-delivery root=%s channel_id=%s message_id=%s status=deduped",
                root_task_id,
                channel_id,
                message_id,
            )
            return "deduped"

        token = _token_from_env(self.environment, profile)
        if not token:
            store.mark_outbound(response_key, "FAILED", profile)
            logger.error(
                "discord-final-delivery root=%s status=failed error=credential_unavailable",
                root_task_id,
            )
            return "failed"

        body: dict[str, Any] = {"content": content}
        body["message_reference"] = {
            "message_id": message_id,
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        }
        try:
            response = self.sender(
                str(channel_id),
                json.dumps(body, ensure_ascii=False),
                {
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                },
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            store.mark_outbound(response_key, "FAILED", profile)
            logger.exception(
                "discord-final-delivery root=%s status=failed",
                root_task_id,
            )
            return "failed"

        response_message_id = str(response.get("id") or "") if isinstance(response, Mapping) else ""
        store.mark_outbound(response_key, "COMPLETED", profile, response_message_id or None)
        logger.info(
            "discord-final-delivery root=%s channel_id=%s message_id=%s status=sent",
            root_task_id,
            channel_id,
            message_id,
        )
        return "sent"


__all__ = ["DiscordCorrelation", "DiscordFinalDelivery", "correlation_from_task"]
