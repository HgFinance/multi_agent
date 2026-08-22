"""Container-side tests for the test-only Hermes provider replay seam.

Run in the Hermes venv because the production ``run_agent`` module lives in
the Hermes image, not in the HgFinance repository::

    docker exec hedgefund-kanban-dispatcher sh -lc \
      'cd /app/repo && PYTHONPATH=/app/repo:/opt/hermes \
       /opt/hermes/.venv/bin/python tests/test_hermes_provider_replay.py'

On the host this module is skipped; it must never make a real provider call.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

try:  # The Hermes image supplies this module; the host test env does not.
    import run_agent  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - host-only collection path
    run_agent = None  # type: ignore[assignment]

from tests.hermes_provider_replay import (
    HistoricalReplayFixture,
    RecordedProvider,
    RecordedProviderResponse,
    RecordedToolCall,
    ReplayFixtureError,
    _sha256_json,
    _sha256_text,
    load_historical_replay_fixture,
)


class ReplayHelperTests(unittest.TestCase):
    def test_fixture_order_mismatch_is_loud(self):
        provider = RecordedProvider(
            (RecordedProviderResponse("one"),),
            expected_tool_history=(("read_file",),),
        )
        with self.assertRaises(ReplayFixtureError):
            provider.create(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "skill_view"}}
                        ],
                    }
                ]
            )


def _skill_call() -> RecordedToolCall:
    return RecordedToolCall(
        "skill-call-1", "skill_view", {"skill": "research-evidence"}
    )


def _fallback_call() -> RecordedToolCall:
    return RecordedToolCall(
        "fallback-call-1", "read_file", {"path": "fixture/evidence.json"}
    )


def _baseline_fixture() -> tuple[tuple[RecordedProviderResponse, ...], tuple[tuple[str, ...], ...]]:
    return (
        (
            RecordedProviderResponse("planning", (_skill_call(),)),
            RecordedProviderResponse("fallback", (_fallback_call(),)),
            RecordedProviderResponse("final answer"),
        ),
        ((), ("skill_view",), ("skill_view", "read_file")),
    )


def _counterfactual_fixture() -> tuple[tuple[RecordedProviderResponse, ...], tuple[tuple[str, ...], ...]]:
    # The initial assistant response is intentionally identical to the
    # baseline.  The test-only precheck below consumes the known-unavailable
    # skill call and executes the already-recorded fallback directly, so the
    # continuation needs only the recorded final response.
    return (
        (
            RecordedProviderResponse("planning", (_skill_call(),)),
            RecordedProviderResponse("final answer"),
        ),
        ((), ("skill_view",)),
    )


@unittest.skipUnless(run_agent is not None, "requires the Hermes runtime image")
class HermesProviderReplayTests(unittest.TestCase):
    def _run_replay(self, *, counterfactual: bool):
        if counterfactual:
            responses, expected_history = _counterfactual_fixture()
        else:
            responses, expected_history = _baseline_fixture()
        provider = RecordedProvider(
            responses, expected_tool_history=expected_history
        )
        executed_tools: list[str] = []
        evidence = {
            "evidence": [{"source": "fixture-source", "claim": "fixture claim"}]
        }

        def fake_tool(name, arguments, task_id, **kwargs):
            executed_tools.append(name)
            if counterfactual and name == "skill_view":
                # Test-only capability precheck simulation.  No production
                # resolver or routing behavior is changed by this callback.
                executed_tools.append("read_file")
                return json.dumps(evidence, sort_keys=True)
            if name == "skill_view":
                return json.dumps(
                    {"error": "deterministic skill resolution failure"},
                    sort_keys=True,
                )
            if name == "read_file":
                return json.dumps(evidence, sort_keys=True)
            raise ReplayFixtureError(f"unexpected tool call: {name}")

        fake_client = provider.client()
        with patch("run_agent.OpenAI", return_value=fake_client), patch(
            "run_agent.handle_function_call", side_effect=fake_tool
        ):
            agent = run_agent.AIAgent(
                base_url="http://fixture.invalid/v1",
                api_key="fixture-only",
                provider="openai",
                model="fixture-model",
                api_mode="openai_chat",
                quiet_mode=True,
                skip_context_files=True,
                load_soul_identity=False,
                skip_memory=True,
                skip_background_review=True,
                max_iterations=6,
            )
            result = agent.run_conversation(
                "fixture question",
                task_id="fixture-task",
            )

        self.assertTrue(result["completed"])
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["final_response"], "final answer")
        self.assertEqual(result["messages"][-1]["content"], "final answer")
        return provider, executed_tools, result, evidence

    def test_baseline_replays_failure_replan_fallback_and_final(self):
        provider, executed_tools, result, _ = self._run_replay(counterfactual=False)

        self.assertEqual(executed_tools, ["skill_view", "read_file"])
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(
            [call["tool_history"] for call in provider.calls],
            [(), ("skill_view",), ("skill_view", "read_file")],
        )
        self.assertEqual(result["messages"][-1]["role"], "assistant")

    @staticmethod
    def _tool_payloads(result):
        return [
            json.loads(message["content"])
            for message in result["messages"]
            if message.get("role") == "tool"
            and isinstance(message.get("content"), str)
        ]

    @staticmethod
    def _assistant_signatures(messages):
        signatures = []
        for message in messages:
            if message.get("role") != "assistant":
                continue
            calls = []
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                calls.append(
                    (
                        function.get("name", ""),
                        _sha256_json(arguments),
                    )
                )
            signatures.append(
                (
                    _sha256_text(message.get("content")),
                    tuple(calls),
                    message.get("finish_reason"),
                )
            )
        return tuple(signatures)

    @staticmethod
    def _tool_signatures(messages):
        return tuple(
            (
                message.get("name") or message.get("tool_name") or "",
                _sha256_text(message.get("content")),
            )
            for message in messages
            if message.get("role") == "tool"
        )

    @classmethod
    def _comparable_tool_signatures(cls, messages):
        """Exclude mutable Kanban housekeeping payloads only.

        ``kanban_show`` contains the current board snapshot and
        ``kanban_complete`` contains run/task identifiers generated by the
        replay environment.  Their presence/order is checked separately;
        evidence and all non-housekeeping tool results remain exact.
        """

        return tuple(
            signature
            for signature in cls._tool_signatures(messages)
            if signature[0] not in {"kanban_show", "kanban_complete"}
        )

    def _run_historical_baseline(self, fixture: HistoricalReplayFixture):
        provider = RecordedProvider(
            fixture.responses,
            expected_tool_history=fixture.expected_tool_history,
        )
        replay_tool_calls: list[tuple[str, str]] = []

        def recorded_special_tool(name, arguments, task_id, **kwargs):
            # Hermes handles housekeeping tools such as kanban_show and
            # kanban_complete outside the ordinary tool batch path. Patch
            # that existing test hook too, so the replay cannot read or write
            # the live board.
            call_id = kwargs.get("tool_call_id")
            if call_id not in fixture.tool_results_by_call_id:
                raise ReplayFixtureError(
                    f"historical replay special-tool id mismatch for {name}"
                )
            replay_tool_calls.append((name, call_id))
            return fixture.tool_results_by_call_id[call_id]

        from hermes_state import SessionDB
        from tools.registry import registry

        with tempfile.TemporaryDirectory(prefix="hgfinance-hermes-replay-") as temp:
            temp_path = Path(temp)
            session_db = SessionDB(temp_path / "state.db")
            fake_client = provider.client()
            original_handlers = {}
            try:
                # Some Hermes housekeeping tools dispatch directly through
                # the global ToolRegistry rather than the re-exported
                # ``run_agent.handle_function_call`` symbol. Swap only those
                # registry handlers for this test scope and restore them
                # unconditionally below.
                for special_name in ("kanban_show", "kanban_complete"):
                    entry = registry.get_entry(special_name)
                    if entry is None:
                        raise ReplayFixtureError(
                            f"missing Hermes registry entry: {special_name}"
                        )
                    original_handlers[special_name] = entry.handler
                    entry.handler = (
                        lambda args, _name=special_name, **kwargs: recorded_special_tool(
                            _name,
                            args,
                            kwargs.get("task_id"),
                            **{
                                key: value
                                for key, value in kwargs.items()
                                if key != "task_id"
                            },
                        )
                    )
                with patch.dict(
                    os.environ,
                    {"HERMES_KANBAN_TASK": f"replay-{fixture.task_id}"},
                    clear=False,
                ), patch("run_agent.OpenAI", return_value=fake_client), patch(
                    "run_agent.handle_function_call", side_effect=recorded_special_tool
                ):
                    agent = run_agent.AIAgent(
                        base_url="http://fixture.invalid/v1",
                        api_key="fixture-only",
                        provider="openai",
                        model="fixture-model",
                        api_mode="openai_chat",
                        quiet_mode=True,
                        skip_context_files=True,
                        load_soul_identity=False,
                        skip_memory=True,
                        skip_background_review=True,
                        max_iterations=max(6, len(fixture.responses) + 2),
                        session_db=session_db,
                    )
                    agent.logs_dir = temp_path / "logs"
                    agent.logs_dir.mkdir(parents=True, exist_ok=True)

                    def replay_tool_calls_into_transcript(
                        assistant_message,
                        messages,
                        effective_task_id,
                        api_call_count=0,
                    ):
                        # This replaces only the tool-execution boundary for
                        # the fixture. It prevents kanban_show, skill_view,
                        # MCP, and terminal tools from touching live state.
                        for tool_call in assistant_message.tool_calls:
                            call_id = getattr(tool_call, "id", "") or ""
                            function = getattr(tool_call, "function", None)
                            name = getattr(function, "name", "") or ""
                            if call_id not in fixture.tool_results_by_call_id:
                                raise ReplayFixtureError(
                                    f"historical replay tool id mismatch for {name}"
                                )
                            replay_tool_calls.append((name, call_id))
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    # Preserve the canonical result name from
                                    # the historical transcript.  Hermes may
                                    # emit a tool-search bridge name in the
                                    # assistant call while persisting the
                                    # resolved MCP tool name on the result.
                                    "name": fixture.tool_names_by_call_id[call_id],
                                    "content": fixture.tool_results_by_call_id[call_id],
                                }
                            )

                    # Instance-local monkeypatch: the real conversation loop
                    # remains active, but every historical tool result is
                    # replayed from the read-only transcript fixture.
                    agent._execute_tool_calls = replay_tool_calls_into_transcript
                    result = agent.run_conversation(
                        "historical replay fixture",
                        task_id=f"replay-{fixture.task_id}",
                    )
            finally:
                for special_name, handler in original_handlers.items():
                    entry = registry.get_entry(special_name)
                    if entry is not None:
                        entry.handler = handler
                session_db.close()

        return provider, replay_tool_calls, result

    def _assert_historical_baseline(self, *, task_id, state_db):
        fixture = load_historical_replay_fixture(
            task_id=task_id,
            kanban_db="/opt/data/shared-kanban/kanban.db",
            state_db=state_db,
        )
        provider, replay_tool_calls, result = self._run_historical_baseline(fixture)

        self.assertTrue(result["completed"])
        self.assertIsNone(result.get("error"))
        self.assertEqual(len(provider.calls), len(fixture.responses))
        self.assertEqual(
            self._assistant_signatures(result["messages"]),
            fixture.assistant_signatures,
        )
        self.assertEqual(
            self._comparable_tool_signatures(result["messages"]),
            tuple(
                signature
                for signature in fixture.tool_signatures
                if signature[0] not in {"kanban_show", "kanban_complete"}
            ),
        )
        self.assertEqual(
            _sha256_text(result["final_response"]),
            fixture.final_response_hash,
        )
        housekeeping_count = sum(
            1
            for name, _ in fixture.tool_signatures
            if name in {"kanban_show", "kanban_complete"}
        )
        self.assertEqual(
            len(replay_tool_calls), len(fixture.tool_signatures) - housekeeping_count
        )
        self.assertEqual(
            sum(1 for name, _ in replay_tool_calls if name == "kanban_complete"),
            0,
        )
        self.assertEqual(
            sum(
                1
                for name, _ in self._tool_signatures(result["messages"])
                if name == "kanban_complete"
            ),
            fixture.historical_terminal_tool_count,
        )

    @staticmethod
    def _drop_historical_response(
        fixture: HistoricalReplayFixture, response_index: int
    ) -> tuple[HistoricalReplayFixture, set[str]]:
        """Build a test-only counterfactual with one known failure turn removed."""

        dropped_call_ids = {
            call.call_id for call in fixture.responses[response_index].tool_calls
        }
        responses = (
            fixture.responses[:response_index]
            + fixture.responses[response_index + 1 :]
        )
        expected_history: list[tuple[str, ...]] = []
        seen_tools: list[str] = []
        for response in responses:
            expected_history.append(tuple(seen_tools))
            seen_tools.extend(call.name for call in response.tool_calls)

        assistant_signatures = (
            fixture.assistant_signatures[:response_index]
            + fixture.assistant_signatures[response_index + 1 :]
        )
        call_ids = tuple(fixture.tool_results_by_call_id)
        tool_signatures = tuple(
            signature
            for call_id, signature in zip(call_ids, fixture.tool_signatures)
            if call_id not in dropped_call_ids
        )
        counterfactual = replace(
            fixture,
            responses=responses,
            expected_tool_history=tuple(expected_history),
            assistant_signatures=assistant_signatures,
            tool_signatures=tool_signatures,
        )
        return counterfactual, dropped_call_ids

    @staticmethod
    def _remove_historical_call_and_response(
        fixture: HistoricalReplayFixture,
        *,
        call_response_index: int,
        call_id: str,
        response_index: int,
    ) -> tuple[HistoricalReplayFixture, set[str]]:
        """Remove one failed parallel call and its immediate re-plan turn."""

        responses = list(fixture.responses)
        original = responses[call_response_index]
        removed_call_ids = {call_id}
        responses[call_response_index] = replace(
            original,
            tool_calls=tuple(
                call for call in original.tool_calls if call.call_id != call_id
            ),
        )
        removed_call_ids.update(
            call.call_id for call in responses[response_index].tool_calls
        )
        del responses[response_index]

        expected_history: list[tuple[str, ...]] = []
        seen_tools: list[str] = []
        assistant_signatures: list[tuple[object, ...]] = []
        for response in responses:
            expected_history.append(tuple(seen_tools))
            seen_tools.extend(call.name for call in response.tool_calls)
            assistant_signatures.append(
                (
                    _sha256_text(response.content),
                    tuple(
                        (call.name, _sha256_json(dict(call.arguments)))
                        for call in response.tool_calls
                    ),
                    response.finish_reason,
                )
            )

        call_ids = tuple(fixture.tool_results_by_call_id)
        tool_signatures = tuple(
            signature
            for candidate_id, signature in zip(call_ids, fixture.tool_signatures)
            if candidate_id not in removed_call_ids
        )
        return replace(
            fixture,
            responses=tuple(responses),
            expected_tool_history=tuple(expected_history),
            assistant_signatures=tuple(assistant_signatures),
            tool_signatures=tool_signatures,
        ), removed_call_ids

    def test_historical_quant_counterfactual_skips_known_skill_failure(self):
        fixture = load_historical_replay_fixture(
            task_id="t_10e395c0",
            kanban_db="/opt/data/shared-kanban/kanban.db",
            state_db="/opt/data/profiles/quant-backtest-department/state.db",
        )
        baseline_provider, _, baseline = self._run_historical_baseline(fixture)

        # Historical turn 4 is the deterministic
        # ``quant/equity-quant-assessment`` skill_view failure; turn 5 is the
        # recorded read_file fallback.  The replay removes only that provider
        # response and reuses the existing historical fallback continuation.
        counterfactual_fixture, dropped_call_ids = self._drop_historical_response(
            fixture, response_index=3
        )
        counter_provider, _, counterfactual = self._run_historical_baseline(
            counterfactual_fixture
        )

        self.assertEqual(
            self._assistant_signatures(counterfactual["messages"]),
            counterfactual_fixture.assistant_signatures,
        )
        self.assertEqual(
            self._comparable_tool_signatures(counterfactual["messages"]),
            tuple(
                signature
                for signature in counterfactual_fixture.tool_signatures
                if signature[0] not in {"kanban_show", "kanban_complete"}
            ),
        )
        self.assertEqual(
            _sha256_text(counterfactual["final_response"]),
            _sha256_text(baseline["final_response"]),
        )
        self.assertTrue(counterfactual["completed"])
        self.assertIsNone(counterfactual.get("error"))
        self.assertEqual(len(counter_provider.calls), len(baseline_provider.calls) - 1)
        self.assertEqual(
            len(self._tool_signatures(counterfactual["messages"])),
            len(self._tool_signatures(baseline["messages"])) - len(dropped_call_ids),
        )
        self.assertEqual(
            sum(
                1
                for name, _ in self._tool_signatures(counterfactual["messages"])
                if name == "kanban_complete"
            ),
            fixture.historical_terminal_tool_count,
        )
        replayed_call_ids = {
            message.get("tool_call_id")
            for message in counterfactual["messages"]
            if message.get("role") == "tool"
        }
        self.assertTrue(dropped_call_ids.isdisjoint(replayed_call_ids))

    def test_historical_research_counterfactual_skips_categorized_duplicate(self):
        fixture = load_historical_replay_fixture(
            task_id="t_f6a20be2",
            kanban_db="/opt/data/shared-kanban/kanban.db",
            state_db="/opt/data/profiles/research-department/state.db",
        )
        baseline_provider, _, baseline = self._run_historical_baseline(fixture)
        counterfactual_fixture, dropped_call_ids = self._drop_historical_response(
            fixture, response_index=2
        )
        counter_provider, _, counterfactual = self._run_historical_baseline(
            counterfactual_fixture
        )

        self.assertTrue(counterfactual["completed"])
        self.assertIsNone(counterfactual.get("error"))
        self.assertEqual(len(counter_provider.calls), len(baseline_provider.calls) - 1)
        self.assertEqual(
            self._assistant_signatures(counterfactual["messages"]),
            counterfactual_fixture.assistant_signatures,
        )
        self.assertEqual(
            self._comparable_tool_signatures(counterfactual["messages"]),
            tuple(
                signature
                for signature in counterfactual_fixture.tool_signatures
                if signature[0] not in {"kanban_show", "kanban_complete"}
            ),
        )
        self.assertEqual(
            _sha256_text(counterfactual["final_response"]),
            _sha256_text(baseline["final_response"]),
        )
        self.assertEqual(
            sum(
                1
                for name, _ in self._tool_signatures(counterfactual["messages"])
                if name == "kanban_complete"
            ),
            fixture.historical_terminal_tool_count,
        )
        replayed_call_ids = {
            message.get("tool_call_id")
            for message in counterfactual["messages"]
            if message.get("role") == "tool"
        }
        self.assertTrue(dropped_call_ids.isdisjoint(replayed_call_ids))

    def test_historical_research_counterfactual_skips_qualified_duplicate(self):
        fixture = load_historical_replay_fixture(
            task_id="t_f6a20be2",
            kanban_db="/opt/data/shared-kanban/kanban.db",
            state_db="/opt/data/profiles/research-department/state.db",
        )
        baseline_provider, _, baseline = self._run_historical_baseline(fixture)
        failed_call_id = fixture.responses[1].tool_calls[0].call_id
        counterfactual_fixture, dropped_call_ids = (
            self._remove_historical_call_and_response(
                fixture,
                call_response_index=1,
                call_id=failed_call_id,
                response_index=2,
            )
        )
        counter_provider, _, counterfactual = self._run_historical_baseline(
            counterfactual_fixture
        )

        self.assertTrue(counterfactual["completed"])
        self.assertIsNone(counterfactual.get("error"))
        self.assertEqual(len(counter_provider.calls), len(baseline_provider.calls) - 1)
        self.assertEqual(
            self._assistant_signatures(counterfactual["messages"]),
            counterfactual_fixture.assistant_signatures,
        )
        self.assertEqual(
            self._comparable_tool_signatures(counterfactual["messages"]),
            tuple(
                signature
                for signature in counterfactual_fixture.tool_signatures
                if signature[0] not in {"kanban_show", "kanban_complete"}
            ),
        )
        self.assertEqual(
            _sha256_text(counterfactual["final_response"]),
            _sha256_text(baseline["final_response"]),
        )
        self.assertEqual(
            sum(
                1
                for name, _ in self._tool_signatures(counterfactual["messages"])
                if name == "kanban_complete"
            ),
            fixture.historical_terminal_tool_count,
        )
        replayed_call_ids = {
            message.get("tool_call_id")
            for message in counterfactual["messages"]
            if message.get("role") == "tool"
        }
        self.assertTrue(dropped_call_ids.isdisjoint(replayed_call_ids))

    def test_historical_research_baseline_replay(self):
        self._assert_historical_baseline(
            task_id="t_f6a20be2",
            state_db="/opt/data/profiles/research-department/state.db",
        )

    def test_historical_quant_baseline_replay(self):
        self._assert_historical_baseline(
            task_id="t_10e395c0",
            state_db="/opt/data/profiles/quant-backtest-department/state.db",
        )

    def test_counterfactual_reuses_fallback_and_preserves_result(self):
        baseline_provider, baseline_tools, baseline, evidence = self._run_replay(
            counterfactual=False
        )
        counter_provider, counter_tools, counter, counter_evidence = self._run_replay(
            counterfactual=True
        )

        self.assertEqual(counter["final_response"], baseline["final_response"])
        self.assertIn(evidence, self._tool_payloads(baseline))
        self.assertIn(counter_evidence, self._tool_payloads(counter))
        self.assertEqual(
            [payload for payload in self._tool_payloads(baseline) if "evidence" in payload],
            [payload for payload in self._tool_payloads(counter) if "evidence" in payload],
        )
        self.assertEqual(counter["messages"][-1]["content"], baseline["messages"][-1]["content"])
        self.assertEqual(baseline_tools, ["skill_view", "read_file"])
        self.assertEqual(counter_tools, ["skill_view", "read_file"])
        self.assertEqual(len(baseline_provider.calls), 3)
        self.assertEqual(len(counter_provider.calls), 2)
        self.assertEqual(counter["completed"], baseline["completed"])

    def test_fixture_exhaustion_is_loud(self):
        provider = RecordedProvider((RecordedProviderResponse("one"),))
        provider.create(messages=[])
        with self.assertRaises(ReplayFixtureError):
            provider.create(messages=[])


if __name__ == "__main__":  # pragma: no cover - container-side entry point
    unittest.main()
