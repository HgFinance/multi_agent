"""Test-only recorded provider seam for Hermes conversation replay.

This module deliberately has no production imports or environment hooks.  The
Hermes container test patches ``run_agent.OpenAI`` for one test scope and uses
the client below as an OpenAI chat-completions-compatible response source.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping


class ReplayFixtureError(AssertionError):
    """Raised when a replay consumes a response sequence unexpectedly."""


class _MappingNamespace(dict):
    """Small OpenAI-response-shaped object supporting attr and key access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - mirrors normal attr access
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class RecordedToolCall:
    """Minimal non-sensitive tool-call data retained in a fixture."""

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def as_response_value(self) -> _MappingNamespace:
        return _MappingNamespace(
            id=self.call_id,
            type="function",
            function=_MappingNamespace(
                name=self.name,
                arguments=json.dumps(
                    dict(self.arguments), sort_keys=True, separators=(",", ":")
                ),
            ),
        )


@dataclass(frozen=True)
class RecordedProviderResponse:
    """Normalized response fields needed by the chat-completions loop."""

    content: str | None = None
    tool_calls: tuple[RecordedToolCall, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int = 1
    completion_tokens: int = 1

    def as_openai_response(self) -> _MappingNamespace:
        finish_reason = self.finish_reason or (
            "tool_calls" if self.tool_calls else "stop"
        )
        message = _MappingNamespace(
            role="assistant",
            content=self.content,
            tool_calls=[call.as_response_value() for call in self.tool_calls]
            or None,
        )
        usage = _MappingNamespace(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
        )
        return _MappingNamespace(
            choices=[_MappingNamespace(message=message, finish_reason=finish_reason)],
            usage=usage,
        )


def _tool_history(messages: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Return tool names already present in the request, in transcript order."""

    names: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or ():
            function = tool_call.get("function") or {}
            name = function.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


class RecordedProvider:
    """OpenAI-compatible, ordered response source used only by tests.

    ``expected_tool_history`` is optional, but when supplied it makes a
    continuation mismatch fail at the provider boundary instead of silently
    returning a response for the wrong conversation state.
    """

    def __init__(
        self,
        responses: Iterable[RecordedProviderResponse],
        *,
        expected_tool_history: Iterable[Iterable[str]] | None = None,
    ) -> None:
        self._responses = tuple(responses)
        self._expected_tool_history = (
            tuple(tuple(names) for names in expected_tool_history)
            if expected_tool_history is not None
            else None
        )
        self.calls: list[dict[str, Any]] = []

    def create(self, **request: Any) -> _MappingNamespace:
        index = len(self.calls)
        if index >= len(self._responses):
            raise ReplayFixtureError(
                f"recorded provider fixture exhausted at call {index + 1}"
            )

        messages = request.get("messages") or []
        observed_history = _tool_history(messages)
        if self._expected_tool_history is not None:
            if index >= len(self._expected_tool_history):
                raise ReplayFixtureError(
                    f"unexpected provider call {index + 1}; fixture has no "
                    "expected request shape"
                )
            expected_history = self._expected_tool_history[index]
            if observed_history != expected_history:
                raise ReplayFixtureError(
                    "provider continuation mismatch at call "
                    f"{index + 1}: expected tool history {expected_history!r}, "
                    f"observed {observed_history!r}"
                )

        self.calls.append(
            {
                "call_index": index + 1,
                "message_count": len(messages),
                "tool_history": observed_history,
                "model": request.get("model"),
                "response_tool_names": tuple(
                    call.name for call in self._responses[index].tool_calls
                ),
            }
        )
        return self._responses[index].as_openai_response()

    def client(self) -> Any:
        """Return a fake client recognized as non-streaming by Hermes tests."""

        # Hermes deliberately avoids its streaming path for unittest.mock.Mock
        # clients.  This keeps the fixture focused on ordered response
        # injection while exercising the real conversation loop and tool
        # result insertion.
        from unittest.mock import Mock

        client = Mock(name="recorded_provider_client")
        client.chat = SimpleNamespace(completions=self)
        client.last_aggregator_slot = None
        client.close = lambda: None
        return client


@dataclass(frozen=True)
class HistoricalReplayFixture:
    """In-memory normalized transcript extracted from a historical session."""

    task_id: str
    session_id: str
    responses: tuple[RecordedProviderResponse, ...]
    expected_tool_history: tuple[tuple[str, ...], ...]
    tool_results_by_call_id: Mapping[str, str]
    tool_names_by_call_id: Mapping[str, str]
    assistant_signatures: tuple[tuple[Any, ...], ...]
    tool_signatures: tuple[tuple[str, str], ...]
    final_response_hash: str
    historical_terminal_tool_count: int


def _sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(encoded)


def _decode_recorded_tool_calls(raw: Any) -> tuple[RecordedToolCall, ...]:
    if not raw:
        return ()
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, list):
        raise ReplayFixtureError("historical tool_calls payload is not a list")

    calls: list[RecordedToolCall] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ReplayFixtureError("historical tool call is not an object")
        function = item.get("function") or {}
        if not isinstance(function, Mapping):
            raise ReplayFixtureError("historical tool function is not an object")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ReplayFixtureError("historical tool call has no name")
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, Mapping):
            raise ReplayFixtureError(
                f"historical arguments for {name} are not an object"
            )
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ReplayFixtureError(f"historical {name} call has no id")
        calls.append(
            RecordedToolCall(call_id, name, dict(arguments))
        )
    return tuple(calls)


def load_historical_replay_fixture(
    *,
    task_id: str,
    kanban_db: str | Path,
    state_db: str | Path,
) -> HistoricalReplayFixture:
    """Extract a replay fixture into memory without writing production data.

    Only the normalized transcript needed by the test is returned.  Callers
    should report hashes/counts, never the returned content or arguments.
    """

    kanban = sqlite3.connect(f"file:{Path(kanban_db)}?mode=ro", uri=True)
    run_row = kanban.execute(
        "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY rowid LIMIT 1",
        (task_id,),
    ).fetchone()
    if not run_row or not run_row[0]:
        raise ReplayFixtureError(f"no task run metadata for {task_id}")
    run_metadata = json.loads(run_row[0])
    session_id = run_metadata.get("worker_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ReplayFixtureError(f"task {task_id} has no worker session id")

    state = sqlite3.connect(f"file:{Path(state_db)}?mode=ro", uri=True)
    rows = state.execute(
        """
        SELECT role, content, tool_call_id, tool_calls, tool_name, finish_reason
        FROM messages WHERE session_id=? ORDER BY id
        """,
        (session_id,),
    ).fetchall()
    state.close()
    kanban.close()
    if not rows:
        raise ReplayFixtureError(f"no messages for historical session {session_id}")

    responses: list[RecordedProviderResponse] = []
    expected_history: list[tuple[str, ...]] = []
    assistant_signatures: list[tuple[Any, ...]] = []
    tool_results_by_call_id: dict[str, str] = {}
    tool_names_by_call_id: dict[str, str] = {}
    tool_signatures: list[tuple[str, str]] = []
    seen_tool_names: list[str] = []

    for role, content, tool_call_id, raw_tool_calls, tool_name, finish_reason in rows:
        if role == "tool":
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ReplayFixtureError("historical tool row has no call id")
            if tool_call_id in tool_results_by_call_id:
                raise ReplayFixtureError(f"duplicate historical tool call id: {tool_call_id}")
            tool_content = content or ""
            tool_results_by_call_id[tool_call_id] = tool_content
            canonical_tool_name = tool_name or ""
            tool_names_by_call_id[tool_call_id] = canonical_tool_name
            tool_signatures.append((canonical_tool_name, _sha256_text(tool_content)))
            continue
        if role != "assistant":
            continue

        calls = _decode_recorded_tool_calls(raw_tool_calls)
        responses.append(
            RecordedProviderResponse(
                content=content,
                tool_calls=calls,
                finish_reason=finish_reason,
            )
        )
        names = tuple(call.name for call in calls)
        expected_history.append(tuple(seen_tool_names))
        seen_tool_names.extend(names)
        assistant_signatures.append(
            (
                _sha256_text(content),
                tuple((call.name, _sha256_json(dict(call.arguments))) for call in calls),
                finish_reason,
            )
        )

    if not responses:
        raise ReplayFixtureError(f"historical session {session_id} has no assistant responses")
    if len(expected_history) != len(responses):
        raise ReplayFixtureError("historical response/history length mismatch")

    terminal_count = sum(1 for name, _ in tool_signatures if name == "kanban_complete")
    return HistoricalReplayFixture(
        task_id=task_id,
        session_id=session_id,
        responses=tuple(responses),
        expected_tool_history=tuple(expected_history),
        tool_results_by_call_id=tool_results_by_call_id,
        tool_names_by_call_id=tool_names_by_call_id,
        assistant_signatures=tuple(assistant_signatures),
        tool_signatures=tuple(tool_signatures),
        final_response_hash=assistant_signatures[-1][0],
        historical_terminal_tool_count=terminal_count,
    )
