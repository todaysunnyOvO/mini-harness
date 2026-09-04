from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent, AgentLoopError
from mini_harness.events import MemoryEventSink
from mini_harness.execution import ThreadExecutionBackend
from mini_harness.provider import ProviderError, ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB
from mini_harness.tools import ToolDefinition, ToolRegistry


class FakeProvider:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append([dict(message) for message in messages])
        return ProviderResponse(content=self.answers.pop(0), tool_calls=(), raw={})


class FailingProvider:
    def complete(self, _messages, *, tools=None):
        raise ProviderError("offline")


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        return self.responses.pop(0)


class RepeatingToolProvider:
    def complete(self, _messages, *, tools=None):
        return ProviderResponse(
            content=None,
            tool_calls=(ToolCall(id="repeat", name="echo", arguments='{"text":"x"}'),),
            raw={},
        )


class FinalizerFailingProvider:
    def __init__(self) -> None:
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        if len(self.calls) == 1:
            return ProviderResponse(
                content=None,
                tool_calls=(
                    ToolCall(
                        id="missing-call",
                        name="missing",
                        arguments="{}",
                    ),
                ),
                raw={},
            )
        raise ProviderError("finalization failed")


class AgentTests(unittest.TestCase):
    def test_failed_finalizer_never_persists_harness_control(self) -> None:
        provider = FinalizerFailingProvider()
        events = MemoryEventSink()
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                agent = Agent(
                    provider,
                    system_prompt="system",
                    tools=ToolRegistry(),
                    max_iterations=1,
                    session_db=db,
                    session_id="failed-finalizer",
                    event_sink=events,
                )

                with self.assertRaisesRegex(
                    AgentLoopError,
                    "finalization failed",
                ):
                    agent.chat("start")

                active = db.audit_messages(
                    "failed-finalizer",
                    include_inactive=False,
                )
                _session, report = db.resume_session("failed-finalizer")
            finally:
                db.close()

        finalizer_request = provider.calls[-1]
        self.assertIsNone(finalizer_request["tools"])
        self.assertIn(
            "[Harness control]",
            finalizer_request["messages"][-1]["content"],
        )
        persisted = [row["message"] for row in active]
        self.assertFalse(
            any(
                "[Harness control]" in str(message.get("content", ""))
                for message in persisted
            )
        )
        self.assertFalse(
            any(
                "[Harness control]" in str(message.get("content", ""))
                for message in report.messages
            )
        )
        self.assertEqual(report.messages[-1]["role"], "tool")
        self.assertEqual(agent.last_stop_reason, "iteration_limit")
        self.assertFalse(agent.last_turn_completed)
        self.assertEqual(
            [event.event for event in events.events].count(
                "turn.finalization_failed"
            ),
            1,
        )

    def test_successful_finalizer_persists_only_assistant_result(self) -> None:
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="missing-call",
                            name="missing",
                            arguments="{}",
                        ),
                    ),
                    raw={},
                ),
                ProviderResponse(
                    content="Partial work summarized.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                agent = Agent(
                    provider,
                    system_prompt="system",
                    tools=ToolRegistry(),
                    max_iterations=1,
                    session_db=db,
                    session_id="successful-finalizer",
                )

                answer = agent.chat("start")
                active = db.audit_messages(
                    "successful-finalizer",
                    include_inactive=False,
                )
                _session, report = db.resume_session(
                    "successful-finalizer"
                )
            finally:
                db.close()

        self.assertEqual(answer, "Partial work summarized.")
        self.assertIn(
            "[Harness control]",
            provider.calls[-1]["messages"][-1]["content"],
        )
        persisted = [row["message"] for row in active]
        self.assertFalse(
            any(
                "[Harness control]" in str(message.get("content", ""))
                for message in persisted
            )
        )
        self.assertEqual(persisted[-1]["role"], "assistant")
        self.assertEqual(
            persisted[-1]["content"],
            "Partial work summarized.",
        )
        self.assertEqual(report.messages[-1], persisted[-1])

    def test_oversized_tool_batch_is_atomically_rejected_and_finalized(
        self,
    ) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments) or arguments,
            )
        )
        calls = tuple(
            ToolCall(
                id=f"call-{index}",
                name="echo",
                arguments=(
                    '{"secret":"BUDGET_PRIVATE_ARGUMENT"}'
                    if index == 0
                    else f'{{"n":{index}}}'
                ),
            )
            for index in range(3)
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(content=None, tool_calls=calls, raw={}),
                ProviderResponse(
                    content="The whole oversized batch was rejected.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )

        events = MemoryEventSink()
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                agent = Agent(
                    provider,
                    system_prompt="system",
                    tools=registry,
                    max_calls_per_batch=2,
                    max_calls_per_turn=5,
                    session_db=db,
                    session_id="batch-budget",
                    event_sink=events,
                )
                answer = agent.chat("run too many")
                journals = db.audit_tool_journal("batch-budget")
                audit = db.audit_messages("batch-budget")
            finally:
                db.close()

        self.assertIn("rejected", answer)
        self.assertEqual(handled, [])
        self.assertEqual(len(provider.calls), 2)
        self.assertIsNone(provider.calls[-1]["tools"])
        results = [
            json.loads(message["content"])
            for message in provider.calls[-1]["messages"]
            if message["role"] == "tool"
        ]
        self.assertEqual(len(results), 3)
        self.assertTrue(
            all(
                result["error"]["code"] == "tool_call_budget_exceeded"
                for result in results
            )
        )
        self.assertTrue(
            all(
                result["meta"]["effect_disposition"] == "none"
                and result["meta"]["execution_phase"]
                == "rejected_before_execution"
                for result in results
            )
        )
        self.assertEqual(agent.last_stop_reason, "tool_call_budget")
        self.assertEqual(len(journals), 3)
        self.assertTrue(
            all(
                row["status"] == "completed"
                and row["effect_disposition"] == "none"
                and row["error_code"] == "tool_call_budget_exceeded"
                for row in journals
            )
        )
        self.assertEqual(
            [message["role"] for message in audit].count("tool"),
            3,
        )
        encoded_events = json.dumps(
            [event.as_dict() for event in events.events],
            ensure_ascii=False,
        )
        self.assertNotIn("BUDGET_PRIVATE_ARGUMENT", encoded_events)
        rejected_event = next(
            event
            for event in events.events
            if event.event == "tool.batch_rejected"
        )
        self.assertEqual(rejected_event.details["requested_tool_calls"], 3)
        self.assertEqual(rejected_event.details["executed_tool_calls"], 0)
        self.assertEqual(rejected_event.details["blocked_tool_calls"], 3)
        self.assertEqual(
            [event.event for event in events.events].count(
                "provider.request_started"
            ),
            2,
        )

    def test_turn_budget_rejects_whole_later_batch_without_partial_effects(
        self,
    ) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments["n"]) or arguments,
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="one", name="echo", arguments='{"n":1}'),
                        ToolCall(id="two", name="echo", arguments='{"n":2}'),
                    ),
                    raw={},
                ),
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="three", name="echo", arguments='{"n":3}'),
                        ToolCall(id="four", name="echo", arguments='{"n":4}'),
                    ),
                    raw={},
                ),
                ProviderResponse(
                    content="Two calls completed; the later batch was rejected.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            max_calls_per_batch=3,
            max_calls_per_turn=3,
        )

        answer = agent.chat("run batches")

        self.assertIn("later batch", answer)
        self.assertEqual(handled, [1, 2])
        final_messages = provider.calls[-1]["messages"]
        result_by_id = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in final_messages
            if message["role"] == "tool"
        }
        self.assertEqual(set(result_by_id), {"one", "two", "three", "four"})
        self.assertTrue(result_by_id["one"]["ok"])
        self.assertTrue(result_by_id["two"]["ok"])
        self.assertEqual(
            result_by_id["three"]["error"]["code"],
            "tool_call_budget_exceeded",
        )
        self.assertEqual(
            result_by_id["four"]["error"]["code"],
            "tool_call_budget_exceeded",
        )
        self.assertEqual(agent.last_stop_reason, "tool_call_budget")

    def test_second_turn_contains_first_turn_history(self) -> None:
        provider = FakeProvider(["first answer", "second answer"])
        agent = Agent(provider, system_prompt="system")

        self.assertEqual(agent.chat("first question"), "first answer")
        self.assertEqual(agent.chat("second question"), "second answer")

        second_request = provider.calls[1]
        self.assertEqual(
            [message["role"] for message in second_request],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(second_request[-1]["content"], "second question")

    def test_failed_request_does_not_commit_dangling_user_message(self) -> None:
        agent = Agent(FailingProvider(), system_prompt="system")

        with self.assertRaises(ProviderError):
            agent.chat("will fail")

        self.assertEqual(agent.messages, ({"role": "system", "content": "system"},))

    def test_executes_tool_and_returns_result_to_model(self) -> None:
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                handler=lambda arguments: {"echo": arguments["text"]},
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="call_1", name="echo", arguments='{"text":"hello"}'),
                    ),
                    raw={},
                ),
                ProviderResponse(content="finished", tool_calls=(), raw={}),
            ]
        )
        agent = Agent(provider, system_prompt="system", tools=registry)

        self.assertEqual(agent.chat("use echo"), "finished")

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["tools"][0]["function"]["name"], "echo")
        second_messages = provider.calls[1]["messages"]
        self.assertEqual(
            [message["role"] for message in second_messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(second_messages[-1]["tool_call_id"], "call_1")
        self.assertIn('"echo": "hello"', second_messages[-1]["content"])

        public_snapshot = agent.messages
        public_snapshot[2]["tool_calls"][0]["id"] = "caller-poisoned-id"
        self.assertEqual(agent.messages[2]["tool_calls"][0]["id"], "call_1")

    def test_iteration_limit_stops_repeating_tool_loop(self) -> None:
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo text.",
                parameters={"type": "object"},
                handler=lambda arguments: arguments,
            )
        )
        agent = Agent(
            RepeatingToolProvider(),
            system_prompt="system",
            tools=registry,
            max_iterations=2,
        )

        with self.assertRaisesRegex(AgentLoopError, "no-tools finalizer"):
            agent.chat("loop")

        self.assertIn("tool", [message["role"] for message in agent.messages])

    def test_model_can_correct_invalid_tool_arguments_on_next_iteration(self) -> None:
        calls = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo required text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: calls.append(arguments["text"]) or arguments["text"],
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="bad", name="echo", arguments="{}"),),
                    raw={},
                ),
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="fixed", name="echo", arguments='{"text":"corrected"}'),
                    ),
                    raw={},
                ),
                ProviderResponse(content="done", tool_calls=(), raw={}),
            ]
        )
        agent = Agent(provider, system_prompt="system", tools=registry)

        self.assertEqual(agent.chat("recover from an invalid call"), "done")

        self.assertEqual(calls, ["corrected"])
        first_error = provider.calls[1]["messages"][-1]["content"]
        self.assertIn('"code": "schema_validation"', first_error)
        corrected_result = provider.calls[2]["messages"][-1]["content"]
        self.assertIn('"result": "corrected"', corrected_result)

    def test_repeated_identical_call_is_blocked_after_per_turn_limit(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo text.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments) or arguments,
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="one", name="echo", arguments='{"x":1}'),),
                    raw={},
                ),
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="two", name="echo", arguments='{"x": 1}'),),
                    raw={},
                ),
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="three", name="echo", arguments='{"x":1}'),),
                    raw={},
                ),
                ProviderResponse(content="done", tool_calls=(), raw={}),
            ]
        )
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            same_call_limit=2,
        )

        self.assertEqual(agent.chat("repeat"), "done")
        self.assertEqual(len(handled), 2)
        blocked = provider.calls[3]["messages"][-1]["content"]
        self.assertIn('"code": "repeated_call_blocked"', blocked)
        self.assertIn('"effect_disposition": "none"', blocked)

    def test_iteration_limit_gets_one_no_tools_summary(self) -> None:
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: arguments,
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="one", name="echo", arguments="{}"),),
                    raw={},
                ),
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="two", name="echo", arguments="{}"),),
                    raw={},
                ),
                ProviderResponse(
                    content="I stopped after the limit; two calls completed.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            max_iterations=2,
        )

        answer = agent.chat("loop")

        self.assertIn("stopped", answer)
        self.assertIsNone(provider.calls[-1]["tools"])
        self.assertFalse(agent.last_turn_completed)
        self.assertEqual(agent.last_stop_reason, "iteration_limit")

    def test_interrupt_finishes_batch_protocol_without_executing_later_call(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments["value"]) or arguments,
            )
        )
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="one", name="echo", arguments='{"value":1}'),
                        ToolCall(id="two", name="echo", arguments='{"value":2}'),
                    ),
                    raw={},
                ),
                ProviderResponse(
                    content="The first call completed; the second was skipped.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        holder = {}

        def observe(_call, _result):
            holder["agent"].interrupt()

        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            tool_observer=observe,
        )
        holder["agent"] = agent

        answer = agent.chat("run a batch")

        self.assertIn("first call", answer)
        self.assertEqual(handled, [1])
        finalizer_messages = provider.calls[-1]["messages"]
        tool_results = [
            message["content"]
            for message in finalizer_messages
            if message["role"] == "tool"
        ]
        self.assertEqual(len(tool_results), 2)
        self.assertIn('"code": "interrupted_before_execution"', tool_results[1])
        self.assertEqual(agent.last_stop_reason, "interrupted")


if __name__ == "__main__":
    unittest.main()
