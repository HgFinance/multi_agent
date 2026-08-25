"""Bounded Discord/Redis mirror retention for HgFinance.

The worker has two deliberately narrow deletion scopes:

* exact gateway-shutdown warning messages in the configured CEO channel;
* unfinished UI mirror claims in Redis, plus empty bot placeholders in their
  already-created Discord threads.

It never deletes a user's root message or a non-empty bot response.  Normal
scheduled runs use the seven-day age gate.  ``--include-current-noise`` and
``--include-current-incomplete`` are explicit one-off maintenance switches
for the operator-approved cleanup of the current incident backlog.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

LOG = logging.getLogger(__name__)
DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1_420_070_400_000
DEFAULT_STREAM = "hf:ui-ceo-mirror:v1"
DEFAULT_NOISE = "⚠️ Gateway shutting down — Your current task will be interrupted."


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _snowflake_time(value: Any) -> datetime | None:
    try:
        snowflake = int(str(value))
    except (TypeError, ValueError):
        return None
    if snowflake <= 0:
        return None
    return datetime.fromtimestamp(
        ((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000,
        tz=timezone.utc,
    )


def _created_at(request_id: str, ingress: Mapping[str, Any]) -> datetime | None:
    value = request_id.rsplit(":", 1)[-1] if request_id.startswith("discord:") else ""
    return _snowflake_time(value) or _snowflake_time(ingress.get("source_message_id"))


@dataclass(frozen=True)
class DiscordRetentionSummary:
    enabled: bool
    available: bool
    warning_deleted: int = 0
    incomplete_deleted: int = 0
    placeholder_deleted: int = 0
    skipped_recent: int = 0
    skipped_malformed: int = 0
    error_code: str | None = None


class DiscordRetentionWorker:
    def __init__(
        self,
        *,
        token: str | None = None,
        redis_url: str | None = None,
        channel_ids: list[str] | None = None,
        retention_days: int | None = None,
        max_messages: int | None = None,
        max_requests: int | None = None,
        enabled: bool | None = None,
        redis_client: Any | None = None,
        opener: Any | None = None,
    ) -> None:
        configured_token = (token or os.getenv("DISCORD_BOT_TOKEN_CEO", "")).strip()
        profile_token = self._profile_token()
        # The live CEO gateway uses the profile credential under HERMES_HOME.
        # Prefer it over a stale compatibility env token when the profile is
        # mounted; an explicit constructor token still wins in tests/tools.
        self.token = (token.strip() if token else profile_token or configured_token)
        self.redis_url = (redis_url or os.getenv("UI_MIRROR_REDIS_URL") or os.getenv("REDIS_URL", "")).strip()
        configured_channels = os.getenv("DISCORD_RETENTION_CHANNEL_IDS", "")
        configured_channels = configured_channels or os.getenv("DISCORD_CEO_CHANNEL_ID", "")
        self.channel_ids = channel_ids or [
            value.strip() for value in configured_channels.split(",") if value.strip()
        ]
        self.retention_days = retention_days or _env_int(
            "DISCORD_RETENTION_DAYS", 7, minimum=1, maximum=3650
        )
        self.max_messages = max_messages or _env_int(
            "DISCORD_RETENTION_MAX_MESSAGES", 500, minimum=1, maximum=10_000
        )
        self.max_requests = max_requests or _env_int(
            "DISCORD_RETENTION_MAX_REQUESTS", 500, minimum=1, maximum=10_000
        )
        self.enabled = (
            _env_bool("DISCORD_RETENTION_ENABLED", True)
            if enabled is None
            else bool(enabled)
        )
        self.redis_client = redis_client
        self.opener = opener or urllib.request.urlopen
        self.request_prefix = "hf:ui-ceo-mirror:request:"
        self.source_prefix = "hf:ui-ceo-mirror:source:"
        self.event_prefix = "hf:ui-ceo-mirror:event:"
        self._profile_tokens_by_bot_id: dict[str, str] = {}
        self._profile_tokens_loaded = False

    @staticmethod
    def _profile_token() -> str:
        home = os.getenv("HERMES_HOME", "/opt/data").strip()
        profile = os.getenv("HERMES_PROFILE", "ceo-agent").strip() or "ceo-agent"
        path = os.path.join(home, "profiles", profile, ".env")
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    key, separator, value = line.partition("=")
                    if separator and key.strip() == "DISCORD_BOT_TOKEN":
                        return value.strip().strip('"').strip("'")
        except OSError:
            pass
        return ""

    @classmethod
    def from_env(cls) -> "DiscordRetentionWorker":
        return cls()

    def _redis(self) -> Any:
        if self.redis_client is not None:
            return self.redis_client
        if not self.redis_url:
            raise RuntimeError("REDIS_URL_MISSING")
        import redis

        self.redis_client = redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            socket_keepalive=True,
        )
        return self.redis_client

    def _headers(self, token: str | None = None) -> dict[str, str]:
        effective_token = token or self.token
        if not effective_token:
            raise RuntimeError("DISCORD_BOT_TOKEN_MISSING")
        # Discord/Cloudflare rejects Python's default ``Python-urllib`` agent
        # on this private egress path even when the bot credential is valid.
        # Identify the integration explicitly, as Discord's API guidance does.
        return {
            "Authorization": f"Bot {effective_token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/discord/discord-api-docs, 10)",
        }

    def _discord_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{DISCORD_API}/{path.lstrip('/')}",
            data=data,
            headers=self._headers(token),
            method=method,
        )
        try:
            with self.opener(request, timeout=10) as response:
                raw = response.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "DELETE":
                return {}
            if exc.code == 429:
                retry_after = 1.0
                try:
                    payload = json.loads(exc.read())
                    retry_after = float(payload.get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(min(10.0, max(0.5, retry_after)))
                with self.opener(request, timeout=10) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            raise

    def _load_profile_tokens(self) -> None:
        if self._profile_tokens_loaded:
            return
        self._profile_tokens_loaded = True
        home = os.getenv("HERMES_HOME", "/opt/data").strip()
        for path in glob.glob(os.path.join(home, "profiles", "*", ".env")):
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        key, separator, value = line.partition("=")
                        if separator and key.strip() == "DISCORD_BOT_TOKEN":
                            token = value.strip().strip('"').strip("'")
                            if token:
                                self._profile_tokens_by_bot_id.setdefault("token:" + token, token)
                            break
            except OSError:
                continue

        # Resolve token -> bot ID once.  Discord permissions are identity
        # scoped: a CEO bot cannot delete a QA bot's own warning in a shared
        # channel unless it has Manage Messages, which is intentionally not
        # granted here.
        unresolved = [
            value for key, value in self._profile_tokens_by_bot_id.items() if key.startswith("token:")
        ]
        for token in unresolved:
            try:
                profile = self._discord_request("GET", "users/@me", token=token)
                bot_id = str(profile.get("id") or "") if isinstance(profile, Mapping) else ""
                if bot_id:
                    self._profile_tokens_by_bot_id[bot_id] = token
            except Exception:
                continue

    def _token_for_author(self, author_id: Any) -> str | None:
        self._load_profile_tokens()
        return self._profile_tokens_by_bot_id.get(str(author_id or "")) or self.token

    @staticmethod
    def _is_empty_bot_placeholder(message: Mapping[str, Any]) -> bool:
        author = message.get("author")
        if not isinstance(author, Mapping) or not bool(author.get("bot")):
            return False
        if str(message.get("content") or "").strip():
            return False
        for key in ("embeds", "components", "attachments", "sticker_items"):
            value = message.get(key)
            if isinstance(value, list) and value:
                return False
        return True

    def _delete_thread_placeholders(self, thread_id: str, *, dry_run: bool) -> int:
        if not thread_id:
            return 0
        try:
            messages = self._discord_request(
                "GET", f"channels/{urllib.parse.quote(thread_id, safe='')}/messages?limit=100"
            )
        except Exception as exc:
            # A stale mirror can point at a channel/thread that this bot no
            # longer sees.  Redis cleanup must still proceed; one inaccessible
            # thread cannot strand the remaining 17 claims.
            LOG.info("discord-retention thread-skip error=%s", type(exc).__name__)
            return 0
        if not isinstance(messages, list):
            return 0
        deleted = 0
        if (
            len(messages) == 1
            and isinstance(messages[0], Mapping)
            and int(messages[0].get("type") or 0) == 21
            and self._is_empty_bot_placeholder(messages[0])
        ):
            # Discord's type-21 thread-starter system message cannot be
            # deleted as an individual message.  The safe equivalent for a
            # thread containing only that placeholder is deleting the thread
            # channel itself; the parent/user root message is untouched.
            if not dry_run:
                try:
                    self._discord_request(
                        "DELETE",
                        f"channels/{urllib.parse.quote(thread_id, safe='')}",
                    )
                except Exception as exc:
                    LOG.info("discord-retention thread-delete-skip error=%s", type(exc).__name__)
                    return 0
            return 1
        for message in messages[: self.max_messages]:
            if not isinstance(message, Mapping) or not self._is_empty_bot_placeholder(message):
                continue
            message_id = str(message.get("id") or "")
            if not message_id:
                continue
            if not dry_run:
                author = message.get("author")
                author_id = author.get("id") if isinstance(author, Mapping) else None
                try:
                    self._discord_request(
                        "DELETE",
                        f"channels/{urllib.parse.quote(thread_id, safe='')}/messages/{urllib.parse.quote(message_id, safe='')}",
                        token=self._token_for_author(author_id),
                    )
                except Exception as exc:
                    LOG.info("discord-retention placeholder-skip error=%s", type(exc).__name__)
                    continue
            deleted += 1
        return deleted

    def _delete_shutdown_warnings(self, *, cutoff: datetime, include_current: bool, dry_run: bool) -> int:
        if not self.token or not self.channel_ids:
            return 0
        deleted = 0
        for channel_id in self.channel_ids:
            before: str | None = None
            scanned = 0
            while scanned < self.max_messages:
                query = "channels/{}/messages?limit=100".format(
                    urllib.parse.quote(channel_id, safe="")
                )
                if before:
                    query += "&before=" + urllib.parse.quote(before, safe="")
                messages = self._discord_request("GET", query)
                if not isinstance(messages, list) or not messages:
                    break
                for message in messages:
                    if not isinstance(message, Mapping):
                        continue
                    scanned += 1
                    if str(message.get("content") or "") != DEFAULT_NOISE:
                        continue
                    created = _parse_discord_timestamp(message.get("timestamp")) or _snowflake_time(message.get("id"))
                    if not include_current and (created is None or created >= cutoff):
                        continue
                    message_id = str(message.get("id") or "")
                    if not message_id:
                        continue
                    if not dry_run:
                        author = message.get("author")
                        author_id = author.get("id") if isinstance(author, Mapping) else None
                        self._discord_request(
                            "DELETE",
                            f"channels/{urllib.parse.quote(channel_id, safe='')}/messages/{urllib.parse.quote(message_id, safe='')}",
                            token=self._token_for_author(author_id),
                        )
                    deleted += 1
                before = str(messages[-1].get("id") or "")
                if not before or len(messages) < 100:
                    break
        return deleted

    def _request_records(self) -> list[tuple[str, str, Mapping[str, Any], datetime | None]]:
        client = self._redis()
        rows: list[tuple[str, str, Mapping[str, Any], datetime | None]] = []
        for key in client.scan_iter(match=f"{self.request_prefix}*", count=200):
            request_id = str(key)[len(self.request_prefix) :]
            raw = client.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping) or payload.get("response") is not None:
                continue
            ingress = payload.get("request")
            if not isinstance(ingress, Mapping):
                continue
            rows.append((str(key), request_id, ingress, _created_at(request_id, ingress)))
            if len(rows) >= self.max_requests:
                break
        return rows

    def _delete_local_record(self, key: str, request_id: str, ingress: Mapping[str, Any], *, dry_run: bool) -> int:
        client = self._redis()
        keys = [key]
        source = str(ingress.get("source") or "")
        source_message_id = str(ingress.get("source_message_id") or "")
        if source and source_message_id:
            keys.append(self.source_prefix + f"{source}:{source_message_id}")
        # Event keys have a TTL, but deleting their exact unfinished records
        # prevents a replay after the request claim is removed.
        for event_key in client.scan_iter(match=f"{self.event_prefix}*", count=200):
            raw_event = client.get(event_key)
            try:
                event = json.loads(raw_event) if raw_event else {}
            except (TypeError, ValueError):
                continue
            if isinstance(event, Mapping) and str(event.get("request_id") or "") == request_id:
                keys.append(str(event_key))
        if not dry_run:
            client.delete(*keys)
        return len(keys)

    def _delete_incomplete(
        self,
        *,
        cutoff: datetime,
        include_current: bool,
        dry_run: bool,
    ) -> tuple[int, int, int, int]:
        deleted = placeholders = recent = malformed = 0
        for key, request_id, ingress, created in self._request_records():
            if created is None:
                if not include_current:
                    malformed += 1
                    continue
            elif not include_current and created >= cutoff:
                recent += 1
                continue
            self._delete_local_record(key, request_id, ingress, dry_run=dry_run)
            deleted += 1
            source = str(ingress.get("source") or "")
            thread_id = str(
                ingress.get("discord_thread_id")
                or ingress.get("thread_id")
                or ingress.get("discord_message_id")
                or (request_id.rsplit(":", 1)[-1] if source == "discord" else "")
            )
            if source == "discord" and thread_id and self.token:
                placeholders += self._delete_thread_placeholders(thread_id, dry_run=dry_run)
        return deleted, placeholders, recent, malformed

    def run_once(
        self,
        *,
        dry_run: bool = False,
        include_current_noise: bool = False,
        include_current_incomplete: bool = False,
        now: datetime | None = None,
    ) -> DiscordRetentionSummary:
        if not self.enabled:
            return DiscordRetentionSummary(enabled=False, available=False, error_code="DISABLED")
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(days=self.retention_days)
        try:
            warning_deleted = self._delete_shutdown_warnings(
                cutoff=cutoff, include_current=include_current_noise, dry_run=dry_run
            )
            incomplete_deleted, placeholders, recent, malformed = self._delete_incomplete(
                cutoff=cutoff, include_current=include_current_incomplete, dry_run=dry_run
            )
            LOG.info(
                "discord-retention enabled=true dry_run=%s warning_deleted=%d "
                "incomplete_deleted=%d placeholder_deleted=%d skipped_recent=%d",
                str(bool(dry_run)).lower(),
                warning_deleted,
                incomplete_deleted,
                placeholders,
                recent,
            )
            return DiscordRetentionSummary(
                enabled=True,
                available=True,
                warning_deleted=warning_deleted,
                incomplete_deleted=incomplete_deleted,
                placeholder_deleted=placeholders,
                skipped_recent=recent,
                skipped_malformed=malformed,
            )
        except Exception as exc:  # maintenance is fail-open for the app plane
            LOG.warning("discord-retention failed error=%s", type(exc).__name__)
            return DiscordRetentionSummary(enabled=True, available=False, error_code=type(exc).__name__)


def _parse_discord_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_worker(worker: DiscordRetentionWorker, *, interval: int, once: bool, dry_run: bool, include_noise: bool, include_incomplete: bool) -> None:
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    import signal

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        worker.run_once(
            dry_run=dry_run,
            include_current_noise=include_noise,
            include_current_incomplete=include_incomplete,
        )
        if once:
            return
        for _ in range(max(1, interval)):
            if stop:
                return
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=_env_int("DISCORD_RETENTION_INTERVAL_SECONDS", 86400))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-current-noise", action="store_true")
    parser.add_argument("--include-current-incomplete", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    _run_worker(
        DiscordRetentionWorker.from_env(),
        interval=max(1, args.interval),
        once=args.once,
        dry_run=args.dry_run,
        include_noise=args.include_current_noise,
        include_incomplete=args.include_current_incomplete,
    )


if __name__ == "__main__":
    main()
