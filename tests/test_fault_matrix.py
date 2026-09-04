from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.events import MemoryEventSink
from mini_harness.execution import ThreadExecutionBackend
from mini_harness.provider import ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB
from mini_harness.tools import ToolDefinition, ToolRegistry


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


def tool_response(call_id: str, name: str, arguments: str) -> ProviderResponse:
    return ProviderResponse(
        content=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        raw={},
    )


class FourLayerFaultMatrixTests(unittest.TestCase):
    def test_prevention_denied_approval_never_reaches_handler(self) -> None:
        handled = []
        registry = ToolRegistry(
            approval_callback=lambda _definition, _arguments: False,
            execution_backend=ThreadExecutionBackend(),
        )
        registry.register(
            ToolDefinition(
                name="dangerous",
                description="Dangerous mutation.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments),
                requires_approval=True,
            )
        )
        provider = ScriptedProvider(
            [
                tool_response("danger", "dangerous", "{}"),
                ProviderResponse(
                    content="The action was not executed.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        events = MemoryEventSink()
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            event_sink=events,
        )

        agent.chat("perform the action")

        self.assertEqual(handled, [])
        result_event = next(
            event for event in events.events if event.event == "tool.result"
        )
        self.assertEqual(result_event.details["error_code"], "approval_denied")
        self.assertEqual(result_event.details["effect_disposition"], "none")
        self.assertEqual(
            result_event.details["execution_phase"],
            "rejected_before_execution",
        )

    def test_correction_model_repairs_schema_error_on_next_iteration(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="set_count",
                description="Set an integer count.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(arguments["value"])
                or arguments,
            )
        )
        provider = ScriptedProvider(
            [
                tool_response("bad", "set_count", '{"value":"seven"}'),
                tool_response("fixed", "set_count", '{"value":7}'),
                ProviderResponse(content="corrected", tool_calls=(), raw={}),
            ]
        )
        events = MemoryEventSink()
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            event_sink=events,
        )

        self.assertEqual(agent.chat("set the count"), "corrected")

        self.assertEqual(handled, [7])
        tool_events = [
            event for event in events.events if event.event == "tool.result"
        ]
        self.assertEqual(
            [event.details["error_code"] for event in tool_events],
            ["schema_validation", None],
        )
        self.assertEqual(
            [event.details["execution_phase"] for event in tool_events],
            ["rejected_before_execution", "handler_completed"],
        )

    def test_stop_loss_blocks_repeated_call_without_undoing_first_effect(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="send",
                description="Send once.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments) or arguments,
            )
        )
        provider = ScriptedProvider(
            [
                tool_response("first", "send", '{"destination":"a"}'),
                tool_response("repeat", "send", '{"destination":"a"}'),
                ProviderResponse(
                    content="The duplicate was blocked.",
                    tool_calls=(),
                    raw={},
                ),
            ]
        )
        events = MemoryEventSink()
        agent = Agent(
            provider,
            system_prompt="system",
            tools=registry,
            same_call_limit=1,
            event_sink=events,
        )

        agent.chat("send")

        self.assertEqual(handled, [{"destination": "a"}])
        blocked = [
            event
            for event in events.events
            if event.event == "tool.result"
            and event.details["error_code"] == "repeated_call_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].details["effect_disposition"], "none")

    def test_recovery_injects_unknown_and_never_replays_handler(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="charge",
                description="Non-idempotent charge.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments),
            )
        )
        events = MemoryEventSink()
        provider = ScriptedProvider(
            [
                ProviderResponse(
                    content="The prior charge result is unknown.",
                    tool_calls=(),
                    raw={},
                )
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system", session_id="fault-recovery")
            db.append_message(session.id, {"role": "user", "content": "charge"})
            db.append_message(
                session.id,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "charge-1",
                            "type": "function",
                            "function": {
                                "name": "charge",
                                "arguments": '{"amount":100}',
                            },
                        }
                    ],
                },
            )

            agent = Agent(
                provider,
                system_prompt="ignored",
                tools=registry,
                session_db=db,
                session_id=session.id,
                event_sink=events,
            )
            agent.chat("inspect only")
            db.close()

        self.assertEqual(handled, [])
        recovery_event = next(
            event for event in events.events if event.event == "recovery.applied"
        )
        self.assertEqual(
            recovery_event.details["inserted_unknown_results"],
            1,
        )
        self.assertIn(
            "result_unavailable_after_resume",
            provider.calls[0]["messages"][3]["content"],
        )


if __name__ == "__main__":
    unittest.main()
