#!/usr/bin/env python3
"""Deliver completed Strategy Hermes reports to their Discord request thread.

This process is deliberately a delivery adapter, not a research worker.  It
reads immutable request metadata and result artifacts from the autonomous
research volume, posts one bounded sanitized report to Discord, and stores its
own idempotency receipts in a separate state volume.  It never writes to the
research lab, CEO/Kanban, order, broker, or accounting paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MENTION_RE = re.compile(r"<@!?\d+>")
_LOGGER = logging.getLogger("strategy-research-discord-notifier")
_MAX_SENT_ENTRIES = 2048


def _truthy(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().casefold()
    return value in _TRUTHY if value else default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _clip(value: object, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 1)] + "…"


def _normalized_query(value: object) -> str:
    # Legacy manifests created before correlation fields were persisted can be
    # recovered only when exactly one recent Discord message has the same
    # content after removing the gateway's mention token.  Ambiguous matches
    # are rejected; the notifier never guesses a destination.
    text = _MENTION_RE.sub(" ", str(value or ""))
    return " ".join(text.split()).casefold()


def _configured() -> tuple[str, str] | None:
    if not _truthy("STRATEGY_DISCORD_REPORT_ENABLED"):
        return None
    token = os.getenv("DISCORD_BOT_TOKEN_CEO", "").strip()
    channel_id = os.getenv("DISCORD_CEO_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        _LOGGER.warning(
            "strategy-discord-report status=disabled reason=missing_configuration"
        )
        return None
    return token, channel_id


def _discord_messages(token: str, channel_id: str) -> list[dict[str, Any]]:
    try:
        response = httpx.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            params={"limit": 100},
            headers={"Authorization": f"Bot {token}"},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        _LOGGER.warning(
            "strategy-discord-report status=resolve_failed reason=transport exception_type=%s",
            type(exc).__name__,
        )
        return []
    if response.status_code != 200:
        _LOGGER.warning(
            "strategy-discord-report status=resolve_failed reason=http_%s",
            response.status_code,
        )
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _correlation(
    request: Mapping[str, Any],
    *,
    token: str,
    configured_channel_id: str,
    recent_messages: list[dict[str, Any]] | None,
) -> tuple[dict[str, str] | None, list[dict[str, Any]] | None]:
    channel_id = str(request.get("discord_channel_id") or "").strip()
    message_id = str(request.get("discord_message_id") or "").strip()
    thread_id = str(request.get("discord_thread_id") or "").strip()
    if channel_id and message_id:
        return (
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "thread_id": thread_id,
            },
            recent_messages,
        )

    if not _truthy("STRATEGY_DISCORD_RESOLVE_LEGACY", default=True):
        return None, recent_messages
    messages = recent_messages
    if messages is None:
        messages = _discord_messages(token, configured_channel_id)
    target = _normalized_query(request.get("goal"))
    matches = [
        item
        for item in messages
        if target and _normalized_query(item.get("content")) == target
        and str(item.get("id") or "").strip()
    ]
    if len(matches) != 1:
        if len(matches) > 1:
            _LOGGER.warning(
                "strategy-discord-report status=resolve_blocked reason=ambiguous_message_matches"
            )
        return None, messages
    match = matches[0]
    return (
        {
            "channel_id": str(match.get("channel_id") or configured_channel_id),
            "message_id": str(match["id"]),
            "thread_id": str((match.get("thread") or {}).get("id") or match["id"]),
        },
        messages,
    )


def _post_to_discord(token: str, correlation: Mapping[str, str], content: str) -> bool:
    thread_id = str(correlation.get("thread_id") or "").strip()
    channel_id = thread_id or str(correlation.get("channel_id") or "").strip()
    if not channel_id:
        return False
    body: dict[str, Any] = {
        "content": _clip(content, 2000),
        "allowed_mentions": {"parse": []},
    }
    if not thread_id:
        body["message_reference"] = {
            "message_id": str(correlation.get("message_id") or ""),
            "channel_id": str(correlation.get("channel_id") or ""),
            "fail_if_not_exists": False,
        }
    try:
        response = httpx.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json=body,
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        _LOGGER.warning(
            "strategy-discord-report status=failed reason=transport exception_type=%s",
            type(exc).__name__,
        )
        return False
    if response.status_code not in {200, 201}:
        _LOGGER.warning(
            "strategy-discord-report status=failed reason=http_%s",
            response.status_code,
        )
        return False
    return True


def _number(value: object) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _percent(value: object) -> str:
    number = _number(value)
    return f"{number * 100:+.2f}%" if number is not None else "unknown"


def _metric_line(label: str, payload: Mapping[str, Any]) -> str:
    return (
        f"{label}: 수익률 {_percent(payload.get('total_return'))}, "
        f"Sharpe {_number(payload.get('sharpe_0rf')) if _number(payload.get('sharpe_0rf')) is not None else 'unknown'}, "
        f"MDD {_percent(payload.get('max_drawdown'))}, "
        f"거래 {payload.get('trade_count', 'unknown')}회"
    )


def _primary_metrics(result: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    preferred = metrics.get("5/20")
    if isinstance(preferred, Mapping):
        return "5/20", preferred
    candidates = [
        (str(key), value)
        for key, value in metrics.items()
        if isinstance(value, Mapping)
        and any(name in value for name in ("development", "validation", "out_of_sample", "full"))
    ]
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _decision(events: list[dict[str, Any]], plan_id: str) -> str:
    decisions = [
        str((event.get("payload") or {}).get("decision") or "")
        for event in events
        if event.get("event_type") == "DECISION"
        and str((event.get("payload") or {}).get("plan_id") or "") == plan_id
    ]
    return decisions[-1] if decisions else "UNAVAILABLE"


def _report_content(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    events: list[dict[str, Any]],
    lab_id: str,
) -> str:
    plan_id = str(result.get("plan_id") or "unknown")
    decision = _decision(events, plan_id)
    status = str(result.get("status") or "UNKNOWN").upper()
    lines = [
        "📊 전략 Hermes 백테스트 완료",
        f"목표: {_clip(request.get('goal'), 360)}",
        f"상태: {status} / 판정: {decision}",
        f"실험계획: {plan_id}",
    ]
    primary = _primary_metrics(result)
    if primary:
        label, metrics = primary
        lines.append(f"핵심 파라미터: {label}")
        for section, display in (
            ("development", "개발"),
            ("validation", "검증"),
            ("out_of_sample", "OOS"),
            ("full", "전체"),
        ):
            payload = metrics.get(section)
            if isinstance(payload, Mapping):
                lines.append(_metric_line(display, payload))
    failure_reason = str(result.get("failure_reason") or "").strip()
    if failure_reason:
        lines.append(f"결론: {_clip(failure_reason, 620)}")
    elif status in {"BLOCKED", "FAILED"}:
        lines.append("결론: 증거가 불충분해 연구를 보류했습니다.")
    lines.extend(
        [
            "주문 생성: 없음(연구 결과는 실거래 승인이 아님)",
            f"추적 ID: {lab_id}",
        ]
    )
    return _clip("\n".join(lines), 1950)


def _blocked_report_content(
    request: Mapping[str, Any], error: Mapping[str, Any], *, lab_id: str
) -> str:
    phase = str(error.get("phase") or "UNKNOWN")
    reason = _clip(error.get("error") or "검증 오류", 900)
    return _clip(
        "\n".join(
            [
                "⛔ 전략 Hermes 연구 차단",
                f"목표: {_clip(request.get('goal'), 420)}",
                f"상태: BLOCKED / 단계: {phase}",
                f"사유: {reason}",
                "결론: 결과 계약을 통과하지 못해 분석 결과를 후보로 승격하지 않았습니다.",
                "주문 생성: 없음",
                f"추적 ID: {lab_id}",
            ]
        ),
        1950,
    )


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _file_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _lab_signature(lab_path: Path) -> str:
    """Fingerprint artifact metadata without rereading every report body."""

    entries = [
        ("request", _file_signature(lab_path / "request.json")),
        ("events", _file_signature(lab_path / "events.jsonl")),
        ("error", _file_signature(lab_path.parent.parent / "errors" / f"{lab_path.name}.json")),
    ]
    results_dir = lab_path / "results"
    try:
        result_paths = sorted(results_dir.glob("*.json"))
    except OSError:
        result_paths = []
    entries.extend(
        (f"result:{path.name}", _file_signature(path)) for path in result_paths
    )
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _prune_sent(sent: dict[str, Any]) -> bool:
    overflow = len(sent) - _MAX_SENT_ENTRIES
    if overflow > 0:
        for key in list(sent)[:overflow]:
            sent.pop(key, None)
        return True
    return False


class StrategyReportNotifier:
    def __init__(self, lab_root: Path, state_root: Path) -> None:
        self.lab_root = lab_root
        self.state_path = state_root / "sent.json"

    def _state(self) -> dict[str, Any]:
        payload = _read_object(self.state_path)
        return payload if payload is not None else {"sent": {}}

    def _mark_sent(self, state: dict[str, Any], key: str) -> None:
        sent = state.setdefault("sent", {})
        if not isinstance(sent, dict):
            sent = {}
            state["sent"] = sent
        sent[key] = _now()
        _prune_sent(sent)
        _write_object(self.state_path, state)

    def _initialize_baseline(self, state: dict[str, Any]) -> None:
        """Do not replay completed reports that predate this notifier."""

        sent = state.setdefault("sent", {})
        if not isinstance(sent, dict):
            sent = {}
            state["sent"] = sent
        lab_signatures = state.setdefault("lab_signatures", {})
        if not isinstance(lab_signatures, dict):
            lab_signatures = {}
            state["lab_signatures"] = lab_signatures
        lab_counts = state.setdefault("lab_counts", {})
        if not isinstance(lab_counts, dict):
            lab_counts = {}
            state["lab_counts"] = lab_counts
        for lab_path in sorted(
            path for path in (self.lab_root / "labs").iterdir() if path.is_dir()
        ):
            lab_signatures[lab_path.name] = _lab_signature(lab_path)
            lab_counts[lab_path.name] = 0
            request = _read_object(lab_path / "request.json")
            if not request or str(request.get("source") or "") != "discord":
                continue
            events = _events(lab_path / "events.jsonl")
            for result_path in sorted((lab_path / "results").glob("*.json")):
                result = _read_object(result_path)
                if not result:
                    continue
                plan_id = str(result.get("plan_id") or result_path.stem)
                if any(
                    event.get("event_type") == "DECISION"
                    and str((event.get("payload") or {}).get("plan_id") or "") == plan_id
                    for event in events
                ):
                    sent.setdefault(f"{lab_path.name}:result:{plan_id}", _now())
        _prune_sent(sent)
        state["initialized_at"] = _now()
        _write_object(self.state_path, state)
        _LOGGER.info("strategy-discord-report status=baseline_initialized")

    def run_once(self) -> dict[str, int | str]:
        configured = _configured()
        if configured is None:
            return {"status": "DISABLED", "scanned": 0, "posted": 0, "failed": 0}
        token, configured_channel_id = configured
        state = self._state()
        sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
        state["sent"] = sent
        sent_pruned = _prune_sent(sent)
        lab_signatures = state.get("lab_signatures")
        if not isinstance(lab_signatures, dict):
            lab_signatures = {}
            state["lab_signatures"] = lab_signatures
        lab_counts = state.get("lab_counts")
        if not isinstance(lab_counts, dict):
            lab_counts = {}
            state["lab_counts"] = lab_counts
        labs_dir = self.lab_root / "labs"
        if not labs_dir.exists():
            return {"status": "READY", "scanned": 0, "posted": 0, "failed": 0}
        if not str(state.get("initialized_at") or "").strip():
            self._initialize_baseline(state)
            return {"status": "BASELINE_INITIALIZED", "scanned": 0, "posted": 0, "failed": 0}
        scanned = posted = failed = 0
        recent_messages: list[dict[str, Any]] | None = None
        current_lab_names: set[str] = set()
        state_dirty = sent_pruned
        for lab_path in sorted(path for path in labs_dir.iterdir() if path.is_dir()):
            current_lab_names.add(lab_path.name)
            signature = _lab_signature(lab_path)
            if lab_signatures.get(lab_path.name) == signature:
                scanned += int(lab_counts.get(lab_path.name) or 0)
                continue
            request = _read_object(lab_path / "request.json")
            if not request or str(request.get("source") or "") != "discord":
                lab_signatures[lab_path.name] = signature
                lab_counts[lab_path.name] = 0
                state_dirty = True
                continue
            correlation, recent_messages = _correlation(
                request,
                token=token,
                configured_channel_id=configured_channel_id,
                recent_messages=recent_messages,
            )
            if correlation is None:
                continue
            events = _events(lab_path / "events.jsonl")
            result_paths = sorted((lab_path / "results").glob("*.json"))
            decision_plan_ids: set[str] = set()
            lab_scanned = 0
            cycle_complete = True
            for result_path in result_paths:
                result = _read_object(result_path)
                if not result:
                    continue
                plan_id = str(result.get("plan_id") or result_path.stem)
                if not any(
                    event.get("event_type") == "DECISION"
                    and str((event.get("payload") or {}).get("plan_id") or "") == plan_id
                    for event in events
                ):
                    continue
                decision_plan_ids.add(plan_id)
                scanned += 1
                lab_scanned += 1
                key = f"{lab_path.name}:result:{plan_id}"
                if key in sent:
                    continue
                content = _report_content(request, result, events=events, lab_id=lab_path.name)
                if _post_to_discord(token, correlation, content):
                    self._mark_sent(state, key)
                    sent[key] = state["sent"][key]
                    posted += 1
                    _LOGGER.info(
                        "strategy-discord-report status=posted lab_id=%s plan_id=%s",
                        lab_path.name,
                        plan_id,
                    )
                else:
                    cycle_complete = False
                    failed += 1
            error = _read_object(self.lab_root / "errors" / f"{lab_path.name}.json")
            if error and not decision_plan_ids:
                scanned += 1
                error_key = ":".join(
                    (
                        lab_path.name,
                        "error",
                        str(error.get("phase") or "UNKNOWN"),
                        str(error.get("updated_at") or ""),
                    )
                )
                if error_key not in sent:
                    content = _blocked_report_content(request, error, lab_id=lab_path.name)
                    if _post_to_discord(token, correlation, content):
                        self._mark_sent(state, error_key)
                        sent[error_key] = state["sent"][error_key]
                        posted += 1
                        _LOGGER.info(
                            "strategy-discord-report status=posted lab_id=%s phase=%s",
                            lab_path.name,
                            str(error.get("phase") or "UNKNOWN"),
                        )
                    else:
                        cycle_complete = False
                        failed += 1
            if cycle_complete:
                lab_signatures[lab_path.name] = signature
                lab_counts[lab_path.name] = lab_scanned
                state_dirty = True
        stale_labs = set(lab_signatures) - current_lab_names
        if stale_labs:
            for lab_name in stale_labs:
                lab_signatures.pop(lab_name, None)
                lab_counts.pop(lab_name, None)
            state_dirty = True
        if state_dirty:
            _write_object(self.state_path, state)
        return {"status": "READY", "scanned": scanned, "posted": posted, "failed": failed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strategy Hermes Discord result notifier")
    parser.add_argument("--lab-root", type=Path, default=Path(os.getenv("AUTONOMOUS_RESEARCH_LAB_ROOT", "/var/lib/autonomous-research")))
    parser.add_argument("--state-root", type=Path, default=Path(os.getenv("STRATEGY_DISCORD_REPORT_STATE_ROOT", "/var/lib/strategy-discord-notifier")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("STRATEGY_DISCORD_REPORT_INTERVAL_SECONDS", "10")))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.healthcheck:
        if not (args.lab_root / "labs").is_dir():
            return 1
        print("strategy-discord-notifier healthy", flush=True)
        return 0
    notifier = StrategyReportNotifier(args.lab_root, args.state_root)
    while True:
        try:
            print(json.dumps(notifier.run_once(), ensure_ascii=False), flush=True)
        except Exception:
            _LOGGER.exception("strategy-discord-report cycle failed")
        if not args.loop:
            return 0
        time.sleep(max(5.0, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
