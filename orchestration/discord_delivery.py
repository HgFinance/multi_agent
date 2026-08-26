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
        if (
            normalized
            in {
                "request_id",
                "message_id",
                "guild_id",
                "channel_id",
                "thread_id",
                "session_id",
            }
            and value
        ):
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


def _correlation_from_synthesis(
    synthesis_task: Mapping[str, Any],
    root_task: Mapping[str, Any] | None,
) -> DiscordCorrelation:
    """Prefer synthesis-local fields, then the supervisor's exact root."""

    synthesis = correlation_from_task(synthesis_task)
    if root_task is None:
        return synthesis
    root = correlation_from_task(root_task)
    return DiscordCorrelation(
        request_id=synthesis.request_id or root.request_id,
        message_id=synthesis.message_id or root.message_id,
        guild_id=synthesis.guild_id or root.guild_id,
        channel_id=synthesis.channel_id or root.channel_id,
        thread_id=synthesis.thread_id or root.thread_id,
        session_id=synthesis.session_id or root.session_id,
    )


def _message_id_from_request_id(request_id: str | None) -> str | None:
    if not request_id:
        return None
    value = str(request_id)
    if value.startswith("discord:"):
        tail = value.rsplit(":", 1)[-1]
        return tail or None
    return None


def _token_from_env(env: Mapping[str, str], profile: str) -> str | None:
    """Resolve the Discord identity for the requested Hermes profile.

    A profile-specific token is authoritative. The process-level token is only
    a compatibility fallback for deployments that do not keep per-profile
    Discord credentials.
    """
    home = Path(env.get("HERMES_HOME", "/opt/data"))

    profile_env = home / "profiles" / profile / ".env"
    try:
        for line in profile_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DISCORD_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    except OSError:
        pass

    token = env.get("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()

    global_env = home / ".env"
    try:
        for line in global_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DISCORD_BOT_TOKEN":
                return value.strip().strip('"').strip("'") or None
    except OSError:
        pass

    return None


class DiscordFinalDelivery:
    """Publish one final CEO answer through Discord's existing bot identity."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        sender: Callable[[str, str, Mapping[str, str]], Mapping[str, Any]]
        | None = None,
        editor: Callable[[str, str, str, Mapping[str, str]], Mapping[str, Any]]
        | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.sender = sender or self._send_http
        self.editor = editor or self._edit_http
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

    def _edit_http(
        self,
        channel_id: str,
        message_id: str,
        payload: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        request = Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
            data=payload.encode("utf-8"),
            headers=dict(headers),
            method="PATCH",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body) if body else {}
        return decoded if isinstance(decoded, Mapping) else {}

    @staticmethod
    def _humanize_content(content: str) -> str:
        """Translate common runtime labels in manager/user-facing messages."""

        replacements = (
            ("snapshot_resolvable=false", "현재 투자지침 확인 불가"),
            ("block_reason", "판단 보류 사유"),
            ("Mandate가", "투자지침이"),
            ("Mandate를", "투자지침을"),
            ("Mandate와", "투자지침과"),
            ("Mandate의", "투자지침의"),
            ("Mandate", "투자지침"),
            ("MODERATE", "보통"),
            ("위반 없음(no_breach)", "현재 입력만으로 위반을 확인하지 못함"),
            ("no_breach", "현재 입력만으로 위반을 확인하지 못함"),
            ("확인된 위반 없음", "현재 입력만으로 위반을 확인하지 못함"),
            ("**risk**", "**리스크 부서**"),
            ("Risk 부서", "리스크 부서"),
            ("HIGH 차단으로", "중요 차단 사유로"),
            ("HIGH 차단", "중요 차단 사유"),
            ("DEFER", "판단 보류"),
        )
        rendered = str(content or "")
        for internal, friendly in replacements:
            rendered = rendered.replace(internal, friendly)
        # NAV 만 정규식이다 - str.replace 는 부분 문자열도 바꿔서 UNAVAILABLE 이
        # "U순자산 가치AILABLE" 로 깨졌다(2026-08-26 HR 유휴 리포트 실측). `\b` 는
        # 한글이 \w 라 "NAV가" 를 놓치므로 ASCII 문자만 배제한다.
        rendered = re.sub(r"(?<![A-Za-z])NAV(?![A-Za-z])", "순자산 가치", rendered)
        rendered = re.sub(
            r"(?:PAPER(?: 가상거래)? 기준 |PAPER만으로는 )?"
            r"현재 입력만으로 위반을 확인하지 못함으로 "
            r"(?:보았|회신되었)지만",
            "법률 위반 여부를 확정할 수 없으며",
            rendered,
        )
        return rendered

    @staticmethod
    def _detail_chunks(content: str, limit: int = 1700) -> tuple[str, ...]:
        """Split long department detail safely below Discord's message limit."""
        remaining = str(content or "").strip()
        if not remaining:
            return ()

        chunks: list[str] = []

        while len(remaining) > limit:
            cut = remaining.rfind("\n", 0, limit + 1)
            if cut < max(200, limit // 2):
                cut = remaining.rfind(" ", 0, limit + 1)
            if cut < max(200, limit // 2):
                cut = limit

            chunk = remaining[:cut].strip()
            if chunk:
                chunks.append(chunk)

            remaining = remaining[cut:].lstrip()

        if remaining:
            chunks.append(remaining)

        return tuple(chunks)

    def upsert_thread_card(
        self,
        *,
        root_task_id: str,
        source_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None,
        content: str,
        store: DiscordIdempotencyStore,
        profile: str,
        response_key_suffix: str,
        update_existing: bool = True,
    ) -> str:
        """Create one request-thread card, then optionally edit that same message."""

        correlation = _correlation_from_synthesis(source_task, root_task)

        source_message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )

        thread_id = correlation.thread_id

        if not thread_id:
            context: Mapping[str, str | None] = {}
            inbound_key = None

            if source_message_id:
                inbound_key = store.inbound_key_for_message(
                    str(source_message_id),
                    "ceo-agent",
                )

            if not inbound_key and correlation.session_id:
                inbound_key = store.inbound_key_for_session(
                    str(correlation.session_id),
                    "ceo-agent",
                )

            if inbound_key:
                context = store.inbound_context(
                    inbound_key,
                    "ceo-agent",
                )

            thread_id = context.get("thread_id") or source_message_id

        if not thread_id:
            logger.info(
                "discord-thread-card root=%s profile=%s status=missing_thread",
                root_task_id,
                profile,
            )
            return "missing_thread"

        token = _token_from_env(self.environment, profile)
        if not token:
            logger.error(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=credential_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        # Keep one department card compact enough for a single Discord message.
        rendered = self._humanize_content(content).strip()
        if not rendered:
            return "empty"

        if len(rendered) > 1900:
            rendered = rendered[:1897].rstrip() + "..."

        guild_id = correlation.guild_id or "unknown"
        correlation_message_id = source_message_id or root_task_id

        dedup_key = canonical_discord_dedup_key(
            guild_id,
            str(thread_id),
            str(correlation_message_id),
        )

        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "thread-card"),
        )[:150]

        response_key = f"{dedup_key}:{safe_suffix}"

        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "HgFinance-DiscordDelivery/2.6",
        }

        payload = json.dumps(
            {"content": rendered},
            ensure_ascii=False,
        )

        try:
            existing_message_id = store.outbound_message_id(
                response_key,
                profile,
            )
        except IdempotencyStoreUnavailable:
            logger.exception(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=ledger_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        if existing_message_id:
            if not update_existing:
                logger.info(
                    "discord-thread-card root=%s profile=%s "
                    "thread_id=%s message_id=%s status=unchanged",
                    root_task_id,
                    profile,
                    thread_id,
                    existing_message_id,
                )
                return "unchanged"

            try:
                self.editor(
                    str(thread_id),
                    str(existing_message_id),
                    payload,
                    headers,
                )
            except (HTTPError, URLError, OSError, TimeoutError, ValueError):
                logger.exception(
                    "discord-thread-card root=%s profile=%s "
                    "status=failed operation=patch",
                    root_task_id,
                    profile,
                )
                return "failed"

            logger.info(
                "discord-thread-card root=%s profile=%s "
                "thread_id=%s message_id=%s status=updated",
                root_task_id,
                profile,
                thread_id,
                existing_message_id,
            )
            return "updated"

        try:
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=profile,
            )
        except IdempotencyStoreUnavailable:
            logger.exception(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=ledger_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        if not claim.admitted:
            existing_message_id = store.outbound_message_id(
                response_key,
                profile,
            )

            if existing_message_id:
                try:
                    self.editor(
                        str(thread_id),
                        str(existing_message_id),
                        payload,
                        headers,
                    )
                    return "updated"
                except (HTTPError, URLError, OSError, TimeoutError, ValueError):
                    logger.exception(
                        "discord-thread-card root=%s profile=%s "
                        "status=failed operation=patch-after-dedup",
                        root_task_id,
                        profile,
                    )
                    return "failed"

            return "deduped"

        try:
            response = self.sender(
                str(thread_id),
                payload,
                headers,
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            store.mark_outbound(response_key, "FAILED", profile)
            logger.exception(
                "discord-thread-card root=%s profile=%s status=failed operation=post",
                root_task_id,
                profile,
            )
            return "failed"

        response_message_id = (
            str(response.get("id") or "") if isinstance(response, Mapping) else ""
        )

        store.mark_outbound(
            response_key,
            "COMPLETED",
            profile,
            response_message_id or None,
        )

        logger.info(
            "discord-thread-card root=%s profile=%s "
            "thread_id=%s message_id=%s status=created",
            root_task_id,
            profile,
            thread_id,
            response_message_id,
        )
        return "created"

    def deliver_to_existing_thread(
        self,
        *,
        root_task_id: str,
        source_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None = None,
        content: str,
        title: str,
        store: DiscordIdempotencyStore,
        profile: str,
        response_key_suffix: str,
    ) -> str:
        """Publish full department detail into the request's existing thread."""

        correlation = _correlation_from_synthesis(source_task, root_task)

        message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )

        # Resolve the request's EXISTING Discord thread.
        #
        # Resolution precedence:
        #   1. explicit task/root thread_id
        #   2. CEO inbound ledger thread_id
        #   3. Discord starter message id
        #
        # HgFinance's Discord request threads are public threads created from
        # the originating message. Discord uses that starter message id as the
        # resulting thread/channel id.
        thread_id = correlation.thread_id

        if not thread_id:
            context = {}

            inbound_key = None

            if message_id:
                inbound_key = store.inbound_key_for_message(
                    str(message_id),
                    "ceo-agent",
                )

            if not inbound_key and correlation.session_id:
                inbound_key = store.inbound_key_for_session(
                    str(correlation.session_id),
                    "ceo-agent",
                )

            if inbound_key:
                context = store.inbound_context(
                    inbound_key,
                    "ceo-agent",
                )

            thread_id = context.get("thread_id") or message_id

        if not thread_id:
            logger.info(
                "discord-detail-thread root=%s profile=%s "
                "status=missing_thread message_id=%s session_id=%s",
                root_task_id,
                profile,
                message_id or "",
                correlation.session_id or "",
            )
            return "missing_thread"

        token = _token_from_env(self.environment, profile)
        if not token:
            logger.error(
                "discord-detail-thread root=%s profile=%s "
                "status=failed error=credential_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        chunks = self._detail_chunks(self._humanize_content(content))
        if not chunks:
            return "empty"

        guild_id = correlation.guild_id or "unknown"
        message_id = (
            correlation.message_id
            or _message_id_from_request_id(correlation.request_id)
            or root_task_id
        )

        dedup_key = canonical_discord_dedup_key(
            guild_id,
            str(thread_id),
            str(message_id),
        )

        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "detail"),
        )[:150]

        total = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            response_key = f"{dedup_key}:{safe_suffix}:chunk-{index}-of-{total}"

            try:
                claim = store.claim_outbound(
                    response_key=response_key,
                    dedup_key=dedup_key,
                    profile=profile,
                )
            except IdempotencyStoreUnavailable:
                logger.error(
                    "discord-detail-thread root=%s profile=%s "
                    "status=failed error=ledger_unavailable",
                    root_task_id,
                    profile,
                )
                return "failed"

            if not claim.admitted:
                continue

            if total > 1:
                header = f"**{title} [{index}/{total}]**\n\n"
            else:
                header = f"**{title}**\n\n"

            body = {
                "content": header + chunk,
            }

            try:
                response = self.sender(
                    str(thread_id),
                    json.dumps(body, ensure_ascii=False),
                    {
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                        "User-Agent": "HgFinance-DiscordDelivery/2.5",
                    },
                )
            except (HTTPError, URLError, OSError, TimeoutError, ValueError):
                store.mark_outbound(response_key, "FAILED", profile)
                logger.exception(
                    "discord-detail-thread root=%s profile=%s "
                    "chunk=%d/%d status=failed",
                    root_task_id,
                    profile,
                    index,
                    total,
                )
                return "failed"

            response_message_id = (
                str(response.get("id") or "") if isinstance(response, Mapping) else ""
            )

            store.mark_outbound(
                response_key,
                "COMPLETED",
                profile,
                response_message_id or None,
            )

        logger.info(
            "discord-detail-thread root=%s profile=%s "
            "thread_id=%s chunks=%d status=sent",
            root_task_id,
            profile,
            thread_id,
            total,
        )
        return "sent"

    def deliver(
        self,
        *,
        root_task_id: str,
        synthesis_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None = None,
        content: str,
        store: DiscordIdempotencyStore,
        profile: str = "ceo-agent",
        response_key_suffix: str = "final",
    ) -> str:
        correlation = _correlation_from_synthesis(synthesis_task, root_task)
        explicit_message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )
        message_id = explicit_message_id
        inbound_key: str | None = None
        context: Mapping[str, str | None] = {}
        correlation_source = "explicit" if explicit_message_id else "missing"
        logger.info(
            "discord-correlation root=%s request_id=%s session_id=%s channel_id=%s message_id=%s",
            root_task_id,
            correlation.request_id or "",
            correlation.session_id or "",
            correlation.channel_id or "",
            message_id or "",
        )
        if not message_id and correlation.session_id:
            inbound_key = store.inbound_key_for_session(correlation.session_id, profile)
            if inbound_key:
                context = store.inbound_context(inbound_key, profile)
                message_id = str(context.get("message_id") or "") or None
                correlation_source = "session_ledger"

        if message_id:
            # Explicit correlation wins. The message ledger is only an exact
            # enrichment lookup, never a recent/global-message fallback.
            message_inbound_key = store.inbound_key_for_message(message_id, profile)
            if message_inbound_key:
                inbound_key = message_inbound_key
                context = store.inbound_context(inbound_key, profile)
                if correlation_source == "missing":
                    correlation_source = "message_ledger"

        if not message_id:
            logger.warning(
                "discord-correlation root=%s source=missing session_id=%s",
                root_task_id,
                correlation.session_id or "",
            )
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"
        guild_id = correlation.guild_id or context.get("guild_id") or "unknown"
        channel_id = correlation.channel_id or context.get("channel_id")
        if not channel_id:
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"
        logger.info(
            "discord-correlation root=%s source=%s session_id=%s message_id=%s channel_id=%s",
            root_task_id,
            correlation_source,
            correlation.session_id or context.get("session_id") or "",
            message_id,
            channel_id,
        )
        dedup_key = (
            inbound_key
            if inbound_key
            else canonical_discord_dedup_key(guild_id, channel_id, message_id)
        )
        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "final"),
        )[:180]
        response_key = f"{dedup_key}:{safe_suffix}"
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

        body: dict[str, Any] = {"content": self._humanize_content(content)}
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
                    "User-Agent": "HgFinance-DiscordDelivery/2.4",
                },
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            store.mark_outbound(response_key, "FAILED", profile)
            logger.exception(
                "discord-final-delivery root=%s status=failed",
                root_task_id,
            )
            return "failed"

        response_message_id = (
            str(response.get("id") or "") if isinstance(response, Mapping) else ""
        )
        store.mark_outbound(
            response_key, "COMPLETED", profile, response_message_id or None
        )
        logger.info(
            "discord-final-delivery root=%s channel_id=%s message_id=%s status=sent",
            root_task_id,
            channel_id,
            message_id,
        )
        return "sent"


__all__ = ["DiscordCorrelation", "DiscordFinalDelivery", "correlation_from_task"]
