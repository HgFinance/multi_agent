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
import ast
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


def _web_mirroring_enabled() -> bool:
    return _truthy("STRATEGY_DISCORD_REPORT_WEB_ENABLED", default=True)


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
    if message_id:
        return (
            {
                "channel_id": channel_id or configured_channel_id,
                "message_id": message_id,
                "thread_id": thread_id,
            },
            recent_messages,
        )

    # Discord-originated requests created by the canonical ingress encode the
    # source message in request_id. Recover that deterministic coordinate even
    # when an older manifest omitted the expanded Discord fields; never fall
    # back to matching human text when this identity is available.
    request_id = str(request.get("request_id") or "").strip()
    if request_id.startswith("discord:"):
        source_message_id = request_id.rsplit(":", 1)[-1].strip()
        if source_message_id:
            return (
                {
                    "channel_id": channel_id or configured_channel_id,
                    "message_id": source_message_id,
                    "thread_id": thread_id,
                },
                recent_messages,
            )

    # Frontend-originated research has no Discord message to reply to.  When
    # mirroring is enabled, publish the exact same report to the configured
    # CEO channel as a standalone message; the frontend receives it through
    # the status API using the same renderer.
    if str(request.get("source") or "").casefold() == "web" and _web_mirroring_enabled():
        return {"channel_id": configured_channel_id, "message_id": "", "thread_id": ""}, recent_messages

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
    if not thread_id and correlation.get("message_id"):
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
    return f"{number * 100:+.2f}%" if number is not None else "—"


def _rate(value: object) -> str:
    number = _number(value)
    return f"{number * 100:.2f}%" if number is not None else "—"


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    """Decode the JSON/repr strings emitted by Hermes plans."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for decoder in (json.loads, ast.literal_eval):
        try:
            decoded = decoder(text)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(decoded, Mapping):
            return decoded
    return None


def _first_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, (list, tuple)):
        for item in value:
            decoded = _as_mapping(item)
            if decoded is not None:
                return decoded
        return None
    return _as_mapping(value)


def _number_text(value: object, *, decimals: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if decimals == 0:
        return f"{number:,.0f}"
    return f"{number:,.{decimals}f}"


def _metric_payload(value: object) -> Mapping[str, Any] | None:
    payload = _as_mapping(value)
    if payload is None:
        return None
    metric_keys = {
        "compound_return",
        "total_return",
        "mean_net_return",
        "median_net_return",
        "hit_rate",
        "max_drawdown",
        "profit_factor",
        "trade_count",
        # Direct Strategy Hermes result schema.
        "compound_net_return",
        "sum_net_return",
        "win_rate",
        "trades",
        "cumulative_compounded",
        "gross_mean_return",
    }
    return payload if any(key in payload for key in metric_keys) else None


def _metric_line(label: str, payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    compound_return = payload.get("compound_return")
    if compound_return is None:
        compound_return = payload.get("compound_net_return")
    if compound_return is None:
        compound_return = payload.get("cumulative_compounded")
    if _number(compound_return) is not None:
        parts.append(f"복리 {_percent(compound_return)}")
    total_return = payload.get("total_return")
    if total_return is None:
        total_return = payload.get("sum_net_return")
    if _number(total_return) is not None:
        parts.append(f"수익 {_percent(total_return)}")
    if _number(payload.get("mean_net_return")) is not None:
        parts.append(f"평균순익 {_percent(payload.get('mean_net_return'))}")
    if _number(payload.get("median_net_return")) is not None:
        parts.append(f"중앙순익 {_percent(payload.get('median_net_return'))}")
    hit_rate = payload.get("hit_rate")
    if hit_rate is None:
        hit_rate = payload.get("win_rate")
    if _number(hit_rate) is not None:
        parts.append(f"승률 {_rate(hit_rate)}")
    if _number(payload.get("max_drawdown")) is not None:
        parts.append(f"MDD {_percent(payload.get('max_drawdown'))}")
    if _number(payload.get("profit_factor")) is not None:
        parts.append(f"PF {_number_text(payload.get('profit_factor'))}")
    trade_count = payload.get("trade_count")
    if trade_count is None:
        trade_count = payload.get("trades")
    if _number(trade_count) is not None:
        parts.append(f"거래 {_number_text(trade_count, decimals=0)}회")
    return f"{label}: " + (" · ".join(parts) if parts else "지표 없음")


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


def _stage_metrics(result: Mapping[str, Any]) -> list[str]:
    """Return compact, schema-tolerant stage metrics for the user report."""

    metrics = _as_mapping(result.get("metrics"))
    if metrics is None:
        return ["핵심지표: 기록 없음"]

    lines: list[str] = []
    primary = _as_mapping(metrics.get("primary"))
    if primary is not None and _as_mapping(primary.get("stages")) is not None:
        stages = _as_mapping(primary.get("stages")) or {}
        for key, display in (("development", "개발"), ("validation", "검증"), ("oos", "OOS"), ("out_of_sample", "OOS")):
            stage = _as_mapping(stages.get(key))
            long_metrics = _metric_payload(stage.get("long")) if stage else None
            short_metrics = _metric_payload(stage.get("short")) if stage else None
            if long_metrics:
                lines.append(_metric_line(f"{display} 롱", long_metrics))
            if short_metrics and key in {"oos", "out_of_sample"}:
                lines.append(_metric_line(f"{display} 숏", short_metrics))
        if lines:
            return lines

    intraday_stages = _as_mapping(metrics.get("intraday_primary_stages"))
    if intraday_stages is not None:
        for key, display in (("development", "개발"), ("validation", "검증"), ("oos", "OOS"), ("out_of_sample", "OOS")):
            stage = _as_mapping(intraday_stages.get(key))
            if stage is None:
                continue
            intraday = _metric_payload(stage.get("intraday"))
            daily_proxy = _metric_payload(stage.get("daily_proxy"))
            if intraday and _number(intraday.get("trade_count")):
                lines.append(_metric_line(f"{display} 장중", intraday))
            elif daily_proxy:
                lines.append(_metric_line(f"{display} 일봉 proxy", daily_proxy) + " · 장중자료 없음")
        if lines:
            return lines

    for group_key, group_label in (
        ("stages_high_range_filter", "고레인지 필터"),
        ("stages", "기본"),
    ):
        stages = _as_mapping(metrics.get(group_key))
        if stages is None:
            continue
        for key, display in (("development", "개발"), ("validation", "검증"), ("oos", "OOS"), ("out_of_sample", "OOS")):
            payload = _metric_payload(stages.get(key))
            if payload:
                lines.append(_metric_line(f"{display} · {group_label}", payload))
        if lines:
            return lines

    for key, display in (
        ("development", "개발"),
        ("validation", "검증"),
        ("out_of_sample", "OOS"),
        ("oos", "OOS"),
        ("full", "전체"),
    ):
        payload = _metric_payload(metrics.get(key))
        if payload:
            lines.append(_metric_line(display, payload))

    aggregate = _metric_payload(metrics.get("aggregate_strategy"))
    if aggregate is None:
        aggregate_group = _as_mapping(metrics.get("aggregate"))
        aggregate = _metric_payload(aggregate_group.get("strategy")) if aggregate_group else None
    if aggregate:
        lines.append(_metric_line("전체", aggregate))

    by_symbol = _as_mapping(metrics.get("by_symbol"))
    if by_symbol:
        for symbol in sorted(by_symbol):
            symbol_metrics = _as_mapping(by_symbol.get(symbol))
            if symbol_metrics is None:
                continue
            oos = _metric_payload(symbol_metrics.get("out_of_sample"))
            if oos:
                lines.append(_metric_line(f"{symbol} OOS", oos))
        if lines:
            return lines

    # Direct Strategy Hermes results use a pooled summary for a bounded
    # available window. Show it explicitly instead of reducing valid metrics
    # such as ``compound_net_return`` to "기록 없음".
    for key, display in (
        ("pooled", "종합"),
        ("pooled_available_window", "가용구간 종합"),
    ):
        payload = _metric_payload(metrics.get(key))
        if payload:
            lines.append(_metric_line(display, payload))
    if lines:
        return lines

    # Hermes' daily breakout result names stages by date and parameter.
    for key, display in (
        ("development_2020_2022", "개발"),
        ("validation_2023_2024", "검증"),
        ("oos_2025_20260826", "OOS"),
        ("primary_alpha_0.5_all", "전체 α=0.50"),
    ):
        payload = _metric_payload(metrics.get(key))
        if payload:
            lines.append(_metric_line(display, payload))

    # Backward-compatible grouped metrics such as the original 5/20 result.
    selected = _primary_metrics(result)
    if selected:
        label, grouped = selected
        for key, display in (
            ("development", "개발"),
            ("validation", "검증"),
            ("out_of_sample", "OOS"),
            ("oos", "OOS"),
            ("full", "전체"),
        ):
            payload = _metric_payload(grouped.get(key))
            if payload:
                lines.append(_metric_line(f"{display} ({label})", payload))

    # Some result contracts expose only scalar stage summaries.
    scalar_stage = {
        "development_selection_mean_net_return": "개발 평균순익",
        "validation_mean_net_return": "검증 평균순익",
        "oos_mean_net_return": "OOS 평균순익",
        "stress_mean_net_return": "스트레스 평균순익",
    }
    scalar_lines = [
        f"{label}: {_percent(metrics.get(key))}"
        for key, label in scalar_stage.items()
        if _number(metrics.get(key)) is not None
    ]
    if scalar_lines:
        lines.extend(scalar_lines)
        oos_extras = {
            "max_drawdown": metrics.get("oos_max_drawdown"),
            "profit_factor": metrics.get("oos_profit_factor"),
            "trade_count": metrics.get("oos_trade_count"),
        }
        if any(value is not None for value in oos_extras.values()):
            lines.append(_metric_line("OOS 상세", oos_extras))

    return lines or ["핵심지표: 기록 없음"]


def _parameter_sensitivity(result: Mapping[str, Any]) -> str | None:
    metrics = _as_mapping(result.get("metrics"))
    if metrics is None:
        return None
    for key, label in (
        ("alpha_grid_mean_net_return", "α 민감도"),
        ("alpha_sensitivity", "α 민감도"),
        ("alpha_grid_high_range", "α 민감도(고레인지)"),
        ("alpha_grid", "α 민감도"),
    ):
        grid = _as_mapping(metrics.get(key))
        if grid is None:
            continue
        values: list[str] = []
        def _alpha_key(item: object) -> tuple[int, float | str]:
            try:
                return (0, float(str(item)))
            except (TypeError, ValueError):
                return (1, str(item))

        for alpha in sorted(grid, key=_alpha_key):
            payload = _metric_payload(grid[alpha])
            scalar_value = _number(grid[alpha])
            if payload is None:
                nested = _as_mapping(grid[alpha])
                payload = _metric_payload(nested.get("long")) if nested else None
            if scalar_value is not None:
                values.append(f"{alpha} {_percent(scalar_value)}")
            elif payload and _number(payload.get("mean_net_return")) is not None:
                values.append(f"{alpha} {_percent(payload.get('mean_net_return'))}")
        if values:
            return f"{label}: " + " · ".join(values)
    return None


def _context(lab_path: Path, result: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Load the immutable plan/hypothesis context for a human report."""

    plan: Mapping[str, Any] = {}
    hypothesis: Mapping[str, Any] = {}
    plan_id = str(result.get("plan_id") or "")
    try:
        plan_path = lab_path / "plans" / f"{plan_id}.json"
        loaded = _read_object(plan_path)
        if loaded:
            plan = loaded
        hypothesis_id = str(plan.get("hypothesis_id") or "")
        if hypothesis_id:
            loaded_hypothesis = _read_object(lab_path / "hypotheses" / f"{hypothesis_id}.json")
            if loaded_hypothesis:
                hypothesis = loaded_hypothesis
    except OSError:
        pass
    return plan, hypothesis


def _context_lines(plan: Mapping[str, Any], hypothesis: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if hypothesis.get("statement"):
        lines.append(f"가설: {_clip(hypothesis['statement'], 180)}")

    method = _as_mapping(plan.get("method"))
    if method:
        if method.get("strategy"):
            lines.append(f"규칙: {_clip(method['strategy'], 220)}")
        alphas = method.get("alphas")
        if isinstance(alphas, (list, tuple)):
            primary_alpha = method.get("primary_alpha")
            primary_text = f" · 기준 α={_number_text(primary_alpha)}" if primary_alpha is not None else ""
            lines.append(f"파라미터: α={','.join(_number_text(item) for item in alphas)}{primary_text}")
        if method.get("delayed_challenge"):
            lines.append(f"지연검증: {_clip(method['delayed_challenge'], 180)}")
    elif plan.get("method"):
        lines.append(f"규칙: {_clip(plan['method'], 220)}")

    signature = _as_mapping(plan.get("signature"))
    if signature and signature.get("signature"):
        lines.append(f"실험 버전: {_clip(signature['signature'], 160)}")
    elif plan.get("signature"):
        lines.append(f"실험 버전: {_clip(plan['signature'], 160)}")

    data = _first_mapping(plan.get("data_requirements"))
    if data:
        nested_scope = "daily" in data or "intraday" in data
        daily = _as_mapping(data.get("daily")) or ({} if nested_scope else data)
        intraday = _as_mapping(data.get("intraday"))
        if daily:
            daily_symbols = daily.get("symbols")
            daily_range = daily.get("range")
            if daily_range is None:
                daily_range = [
                    daily.get("start_date") or daily.get("requested_start"),
                    daily.get("end_date") or daily.get("requested_end"),
                ]
            if isinstance(daily_symbols, (list, tuple)):
                symbols = ",".join(str(item) for item in daily_symbols)
                symbols_text = f"{len(daily_symbols)}종목({symbols})"
            else:
                symbols_text = "—"
            daily_source = daily.get("tr_code") or "일봉"
            timeframe = str(daily.get("timeframe") or "").strip()
            intraday_data = any(token in timeframe.casefold() for token in ("minute", "tick", "분봉", "틱"))
            frequency = timeframe or "분봉" if intraday_data else "조정 일봉"
            daily_dates = _date_range_text(daily_range)
            lines.append(f"데이터: {daily_source} {frequency} · {symbols_text} · {daily_dates}")
        if intraday:
            intraday_symbols = intraday.get("symbols")
            intraday_range = [intraday.get("start_date"), intraday.get("end_date")]
            count = len(intraday_symbols) if isinstance(intraday_symbols, (list, tuple)) else "—"
            lines.append(
                f"장중: {intraday.get('tr_code', '분봉')} {intraday.get('interval', '—')}분봉 · "
                f"{count}종목 · {_date_range_text(intraday_range)}"
            )
    elif plan.get("data_requirements"):
        lines.append(f"데이터: {_clip(plan['data_requirements'], 260)}")

    splits = _first_mapping(plan.get("splits"))
    if splits:
        split_parts: list[str] = []
        for key, display in (
            ("development", "개발"),
            ("validation", "검증"),
            ("out_of_sample", "OOS"),
            ("oos", "OOS"),
        ):
            if key in splits:
                split_parts.append(f"{display} {_date_range_text(splits[key])}")
        if split_parts:
            lines.append("구간: " + " · ".join(split_parts))

    cost = _as_mapping(plan.get("cost_model"))
    if cost:
        primary_cost = cost.get("primary")
        stress_cost = cost.get("stress")
        cost_parts = []
        if primary_cost:
            cost_parts.append(f"기본 {_cost_text(primary_cost)}")
        if stress_cost:
            cost_parts.append(f"스트레스 {_cost_text(stress_cost)}")
        if cost_parts:
            lines.append("비용: " + " · ".join(cost_parts))
    elif plan.get("cost_model"):
        lines.append(f"비용: {_clip(plan['cost_model'], 220)}")
    if plan.get("seed") is not None:
        lines.append(f"seed: {plan['seed']}")
    return lines


def _actual_scope_line(result: Mapping[str, Any]) -> str | None:
    """Render the actual first/last returned bars from a direct Hermes result."""

    metrics = _as_mapping(result.get("metrics"))
    by_symbol = _as_mapping(metrics.get("by_symbol")) if metrics else None
    if not by_symbol:
        return None
    values: list[str] = []
    for symbol in sorted(by_symbol):
        item = _as_mapping(by_symbol.get(symbol))
        if not item or not item.get("first_bar") or not item.get("last_bar"):
            continue
        values.append(f"{symbol} {_clip(item['first_bar'], 24)}~{_clip(item['last_bar'], 24)}")
    return "실제 반환: " + " · ".join(values) if values else None


def _date_range_text(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start, end = value[0], value[1]
        if start and end:
            return f"{_date_text(start)}~{_date_text(end)}"
    if isinstance(value, Mapping):
        start = value.get("start") or value.get("start_date")
        end = value.get("end") or value.get("end_date")
        if start and end:
            return f"{_date_text(start)}~{_date_text(end)}"
    if isinstance(value, str):
        compact = value.strip()
        match = re.fullmatch(r"(\d{8})[-~](\d{8})", compact)
        if match:
            return f"{_date_text(match.group(1))}~{_date_text(match.group(2))}"
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[-~](\d{4}-\d{2}-\d{2})", compact)
        if match:
            return f"{_date_text(match.group(1))}~{_date_text(match.group(2))}"
    return _clip(value, 80) if value else "—"


def _date_text(value: object) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}.{text[4:6]}.{text[6:]}"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", ".")
    return text


def _cost_text(value: object) -> str:
    text = str(value)
    replacements = {
        "round_trip_fee=": "fee ",
        " plus slippage=": "+ slip ",
        " per entry and ": "/편도, ",
        " per exit": "",
        "0.0010": "0.10%",
        "0.0015": "0.15%",
        "0.0020": "0.20%",
        "0.001": "0.10%",
        "0.002": "0.20%",
        "per side": "편도",
        "slippage": "슬리피지",
        "fee": "수수료",
        "plus": "+",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return _clip(text, 150)


def _decision(events: list[dict[str, Any]], plan_id: str) -> str:
    decisions = [
        str((event.get("payload") or {}).get("decision") or "")
        for event in events
        if event.get("event_type") == "DECISION"
        and str((event.get("payload") or {}).get("plan_id") or "") == plan_id
    ]
    return decisions[-1] if decisions else "UNAVAILABLE"


def _decided_plan_ids(events: list[dict[str, Any]]) -> set[str]:
    return {
        str((event.get("payload") or {}).get("plan_id") or "")
        for event in events
        if event.get("event_type") == "DECISION"
        and str((event.get("payload") or {}).get("plan_id") or "").strip()
    }


def _lab_is_final(lab_path: Path, *, events: list[dict[str, Any]], result_ids: set[str]) -> bool:
    """A final report is emitted only after every registered plan is decided."""

    artifact_plans = {
        path.stem
        for path in (lab_path / "plans").glob("*.json")
        if path.is_file()
    }
    registered_plans = {
        str((event.get("payload") or {}).get("plan_id") or "")
        for event in events
        if event.get("event_type") == "PLAN_CREATED"
        and str((event.get("payload") or {}).get("plan_id") or "").strip()
    }
    # A plan file can be left behind by Hermes while a turn is being
    # interrupted. It is not a registered experiment until the supervisor
    # has emitted PLAN_CREATED. Once the event log has registered at least
    # one plan, use that durable set instead of treating an orphan file as an
    # uncompleted experiment. Fixtures/legacy labs without PLAN_CREATED
    # events retain the artifact-based fallback for compatibility.
    plans = registered_plans or artifact_plans
    if plans and not plans.issubset(result_ids & _decided_plan_ids(events)):
        return False
    state = _read_object(lab_path / ".state.json") or {}
    if str(state.get("active_plan_id") or "").strip():
        return False
    # A test fixture or an older lab may not have a state file. If all of its
    # result artifacts have decisions, it is still safe to render a final
    # report because no unmeasured plan can be hidden from the report.
    return bool(result_ids & _decided_plan_ids(events))


def _result_summary_line(result: Mapping[str, Any], events: list[dict[str, Any]]) -> str:
    plan_id = str(result.get("plan_id") or "unknown")
    status = str(result.get("status") or "UNKNOWN").upper()
    decision = _decision(events, plan_id)
    icon = {"COMPLETED": "✅", "CANDIDATE": "🟢", "FAILED": "❌", "BLOCKED": "⛔"}.get(status, "ℹ️")
    metric_lines = _stage_metrics(result)
    metric = next((line for line in metric_lines if line.startswith("OOS")), metric_lines[0])
    return f"{icon} {_clip(plan_id, 42)} · {status}/{decision} · {_clip(metric, 175)}"


def _bounded_report(lines: list[str], *, limit: int = 1950) -> str:
    """Keep the report readable while always retaining the safety footer."""

    footer_prefixes = ("주문 생성:", "추적:")
    footer = [line for line in lines if line.startswith(footer_prefixes)]
    body = [line for line in lines if not line.startswith(footer_prefixes)]
    footer_text = "\n".join(footer)
    budget = max(0, limit - (len(footer_text) + (1 if footer_text else 0)))

    # These details are useful but less important than stage metrics, final
    # decision, and traceability when a platform imposes a hard character cap.
    for prefix in ("seed:", "실험 버전:", "가설:", "비용:", "주의:", "한계:"):
        if len("\n".join(body)) <= budget:
            break
        body = [line for line in body if not line.startswith(prefix)]

    text = "\n".join(body)
    if len(text) > budget:
        for prefix, maximum in (("목표:", 180), ("주요 사유:", 260), ("규칙:", 200)):
            for index, line in enumerate(body):
                if line.startswith(prefix) and len(line) > maximum:
                    body[index] = _clip(line, maximum)
        text = "\n".join(body)
    if len(text) > budget:
        text = _clip(text, budget)
    return text + (f"\n{footer_text}" if footer_text else "")


def _aggregate_report_content(
    request: Mapping[str, Any],
    results: list[Mapping[str, Any]],
    *,
    events: list[dict[str, Any]],
    lab_id: str,
    lab_path: Path,
) -> str:
    """Render one user-facing report after the complete experiment set."""

    decision_order: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.get("event_type") != "DECISION":
            continue
        plan_id = str((event.get("payload") or {}).get("plan_id") or "").strip()
        if plan_id:
            decision_order[plan_id] = index
    ordered_results = sorted(
        results,
        key=lambda item: (
            decision_order.get(str(item.get("plan_id") or ""), -1),
            str(item.get("plan_id") or ""),
        ),
    )
    latest_result = ordered_results[-1] if ordered_results else {}
    latest_status = str(latest_result.get("status") or "UNKNOWN").upper()
    latest_decision = _decision(events, str(latest_result.get("plan_id") or ""))
    decisions = {_decision(events, str(item.get("plan_id") or "")) for item in ordered_results}
    if latest_status == "BLOCKED":
        final_status, final_decision, icon = "BLOCKED", "PAUSE", "⛔"
    elif latest_status == "FAILED":
        final_status, final_decision, icon = "FAILED", "PAUSE", "❌"
    elif latest_status == "CANDIDATE":
        final_status, final_decision, icon = "CANDIDATE", "REVIEW", "🟢"
    else:
        final_status = "COMPLETED"
        final_decision = (
            latest_decision
            if latest_decision != "UNAVAILABLE"
            else "PIVOT" if "PIVOT" in decisions else "PAUSE" if "PAUSE" in decisions else "REVIEW"
        )
        icon = "✅"

    lines = [
        f"{icon} 전략 Hermes 백테스트 완료 · 최종 보고서",
        f"상태: {final_status} · 최종판정: {final_decision} · 실험 {len(ordered_results)}건 완료",
        f"목표: {_clip(request.get('goal'), 280)}",
        "",
        "[실험별 핵심지표]",
    ]
    lines.extend(_result_summary_line(result, events) for result in ordered_results)

    # Show the shared rule/scope once, plus any intraday challenge scope.
    scope_lines: list[str] = []
    if ordered_results:
        plan, hypothesis = _context(lab_path, ordered_results[0])
        for line in _context_lines(plan, hypothesis):
            if line.startswith(("규칙:", "파라미터:", "데이터:", "구간:")) and line not in scope_lines:
                scope_lines.append(line)
    for result in ordered_results:
        plan, hypothesis = _context(lab_path, result)
        for line in _context_lines(plan, hypothesis):
            if line.startswith("장중:") and line not in scope_lines:
                scope_lines.append(line)
        actual_scope = _actual_scope_line(result)
        if actual_scope and actual_scope not in scope_lines:
            scope_lines.append(actual_scope)
    if scope_lines:
        lines.extend(["", "[전략·검증범위]"])
        lines.extend(scope_lines[:6])

    failed = sum(str(item.get("status") or "").upper() == "FAILED" for item in ordered_results)
    blocked = sum(str(item.get("status") or "").upper() == "BLOCKED" for item in ordered_results)
    lines.extend(["", "[종합판정]"])
    if blocked and latest_status != "BLOCKED":
        lines.append(f"이력상 BLOCKED {blocked}건은 후속 결과가 있어 현재 판정에서 제외했습니다.")
    elif blocked:
        lines.append(f"{blocked}건은 필요한 데이터/계약 부족으로 차단됐습니다.")
    if failed and latest_status != "FAILED":
        lines.append(f"이력상 FAILED {failed}건은 후속 결과가 있어 현재 판정에서 제외했습니다.")
    elif failed:
        lines.append(f"{failed}건은 증거 게이트를 통과하지 못했습니다.")
    if latest_status not in {"BLOCKED", "FAILED"}:
        if "PIVOT" in decisions:
            lines.append("결과는 기록됐지만 강건성 검증 조건이 남아 PIVOT 판정입니다. 후보 승격하지 않습니다.")
        else:
            lines.append("전체 실험이 결과 계약을 통과했습니다. 후보 승격은 별도 승인 대상입니다.")
    latest_reason = str(latest_result.get("failure_reason") or "").strip()
    if latest_reason:
        lines.append(f"주요 사유: {_clip(latest_reason, 360)}")
    lines.extend(
        [
            "주문 생성: 없음 · 후보 승격: 없음(연구 결과는 실거래 승인이 아님)",
            f"추적: request={_clip(request.get('request_id'), 100)} · lab={lab_id}",
        ]
    )
    return _bounded_report(lines)


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
    plan, hypothesis = _context(Path(request.get("_lab_path") or ""), result) if request.get("_lab_path") else ({}, {})
    status_icon = {"COMPLETED": "✅", "CANDIDATE": "🟢", "FAILED": "❌", "BLOCKED": "⛔"}.get(status, "ℹ️")
    lines = [
        f"{status_icon} 전략 Hermes 백테스트 완료 · 결과",
        f"상태: {status} · 판정: {decision} · 실험: {plan_id}",
        f"목표: {_clip(request.get('goal'), 300)}",
        "",
        "[핵심 지표]",
    ]
    lines.extend(_stage_metrics(result))
    sensitivity = _parameter_sensitivity(result)
    if sensitivity:
        lines.append(sensitivity)
    lines.extend(["", "[전략·범위]"])
    lines.extend(_context_lines(plan, hypothesis))
    actual_scope = _actual_scope_line(result)
    if actual_scope:
        lines.append(actual_scope)

    robustness = _as_mapping(result.get("robustness"))
    if robustness:
        checks = (
            ("parameter_grid_has_positive_oos", "OOS 양수 파라미터"),
            ("delayed_execution_survives", "지연체결"),
            ("alternate_cost_stress_survives", "비용 스트레스"),
            ("all_asset_slices_positive", "전 종목"),
            ("all_time_slices_positive", "전 기간"),
        )
        check_text = " · ".join(
            f"{label} {'통과' if robustness.get(key) is True else '실패' if robustness.get(key) is False else '미측정'}"
            for key, label in checks
            if key in robustness
        )
        if check_text:
            lines.extend(["", "[강건성]", check_text])

    failure_reason = str(result.get("failure_reason") or "").strip()
    lines.extend(["", "[판정]"])
    if failure_reason:
        lines.append(f"{_clip(failure_reason, 520)}")
    elif status in {"BLOCKED", "FAILED"}:
        lines.append("증거가 불충분해 연구를 보류했습니다.")
    else:
        lines.append("사전 등록된 검증 결과를 확인했습니다.")
    failure_modes = result.get("failure_modes")
    limitations = result.get("limitations")
    if isinstance(failure_modes, (list, tuple)) and failure_modes:
        lines.append(f"주의: {_clip(failure_modes[0], 220)}")
    if isinstance(limitations, (list, tuple)) and limitations:
        lines.append(f"한계: {_clip(limitations[0], 220)}")
    lines.extend(
        [
            "주문 생성: 없음 · 후보 승격: 없음(연구 결과는 실거래 승인이 아님)",
            f"추적: request={_clip(request.get('request_id'), 100)} · lab={lab_id}",
        ]
    )
    return _bounded_report(lines)


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
        try:
            legacy_retry_seconds = float(
                os.getenv("STRATEGY_DISCORD_LEGACY_RESOLUTION_RETRY_SECONDS", "60")
            )
        except ValueError:
            legacy_retry_seconds = 60.0
        self._legacy_resolution_retry_seconds = max(
            30.0, min(legacy_retry_seconds, 900.0)
        )
        self._legacy_resolution_retry_at: dict[str, float] = {}

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
            if not request or str(request.get("source") or "").casefold() not in {"discord", "web"}:
                continue
            # The lab path is an internal, non-user-facing hint used only to
            # join immutable plan/hypothesis files into the Discord report.
            request["_lab_path"] = str(lab_path)
            events = _events(lab_path / "events.jsonl")
            result_ids = {
                path.stem for path in (lab_path / "results").glob("*.json") if path.is_file()
            }
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
            if _lab_is_final(lab_path, events=events, result_ids=result_ids):
                sent.setdefault(f"{lab_path.name}:final", _now())
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
            previous_signature = lab_signatures.get(lab_path.name)
            if previous_signature == signature:
                scanned += int(lab_counts.get(lab_path.name) or 0)
                continue
            if previous_signature is not None and previous_signature != signature:
                self._legacy_resolution_retry_at.pop(lab_path.name, None)
            request = _read_object(lab_path / "request.json")
            if not request or str(request.get("source") or "").casefold() not in {"discord", "web"}:
                lab_signatures[lab_path.name] = signature
                lab_counts[lab_path.name] = 0
                state_dirty = True
                continue
            legacy_resolution_required = (
                str(request.get("source") or "").casefold() == "discord"
                and not str(request.get("discord_message_id") or "").strip()
                and not str(request.get("request_id") or "")
                .strip()
                .startswith("discord:")
            )
            if legacy_resolution_required:
                retry_at = self._legacy_resolution_retry_at.get(lab_path.name, 0.0)
                if time.monotonic() < retry_at:
                    continue
            correlation, recent_messages = _correlation(
                request,
                token=token,
                configured_channel_id=configured_channel_id,
                recent_messages=recent_messages,
            )
            if correlation is None:
                if legacy_resolution_required:
                    self._legacy_resolution_retry_at[lab_path.name] = (
                        time.monotonic() + self._legacy_resolution_retry_seconds
                    )
                    _LOGGER.debug(
                        "strategy-discord-report status=legacy_resolution_deferred "
                        "lab_id=%s retry_seconds=%.1f",
                        lab_path.name,
                        self._legacy_resolution_retry_seconds,
                    )
                continue
            self._legacy_resolution_retry_at.pop(lab_path.name, None)
            events = _events(lab_path / "events.jsonl")
            result_paths = sorted((lab_path / "results").glob("*.json"))
            decision_plan_ids: set[str] = set()
            decided_results: list[Mapping[str, Any]] = []
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
                decided_results.append(result)
                scanned += 1
                lab_scanned += 1
            result_ids = {path.stem for path in result_paths if path.is_file()}
            if _lab_is_final(lab_path, events=events, result_ids=result_ids):
                # A retry appends a new decided result to the same lab. The
                # old stable key suppressed that corrected report forever,
                # even though the lab signature had changed. Keep each exact
                # artifact set idempotent while allowing a new final report
                # for a genuinely new experiment result.
                final_key = f"{lab_path.name}:final:{signature}"
                if final_key not in sent:
                    content = _aggregate_report_content(
                        request,
                        decided_results,
                        events=events,
                        lab_id=lab_path.name,
                        lab_path=lab_path,
                    )
                    if _post_to_discord(token, correlation, content):
                        self._mark_sent(state, final_key)
                        sent[final_key] = state["sent"][final_key]
                        posted += 1
                        _LOGGER.info(
                            "strategy-discord-report status=posted lab_id=%s report=final plans=%s",
                            lab_path.name,
                            len(decided_results),
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
