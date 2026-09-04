from __future__ import annotations

import copy
import json
import unittest

from mini_harness.agent import Agent
from mini_harness.api_messages import APIMessageBuilder
from mini_harness.provider import ProviderResponse


class MutatingProvider:
    def __init__(self) -> None:
        self.received = []

    def complete(self, messages, *, tools=None):
        self.received.append(copy.deepcopy(messages))
        messages[0]["content"] = "provider-poisoned-system"
        if len(messages) > 1:
            messages[1]["content"] = "provider-poisoned-user"
        return ProviderResponse(content="done", tool_calls=(), raw={})


class APIMessageBuilderTests(unittest.TestCase):
    def test_build_is_stable_and_does_not_mutate_conversation_history(self) -> None:
        history = [
            {
                "role": "system",
                "content": "stable system",
                "_internal_cache_key": "secret",
            },
            {"role": "user", "content": "hello", "_db_id": 7},
            {
                "role": "assistant",
                "content": None,
                "reasoning": "internal reasoning",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "response_item_id": "internal-item",
                        "function": {
                            "name": "echo",
                            "arguments": {"value": 1},
                            "internal_signature": "secret",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "echo",
                "content": '{"ok":true}',
                "_execution_record_id": 99,
            },
        ]
        original = copy.deepcopy(history)
        builder = APIMessageBuilder()

        first = builder.build(history)
        second = builder.build(history)

        self.assertEqual(history, original)
        self.assertEqual(first.messages, second.messages)
        self.assertGreaterEqual(first.report.dropped_fields, 6)
        self.assertNotIn("_internal_cache_key", first.messages[0])
        self.assertNotIn("_db_id", first.messages[1])
        assistant = first.messages[2]
        self.assertNotIn("reasoning", assistant)
        self.assertNotIn("finish_reason", assistant)
        self.assertNotIn("response_item_id", assistant["tool_calls"][0])
        self.assertEqual(
            assistant["tool_calls"][0]["function"]["arguments"],
            '{"value":1}',
        )

    def test_protocol_repair_is_applied_only_to_api_copy(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "run both"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "done",
                        "type": "function",
                        "function": {"name": "first", "arguments": "{}"},
                    },
                    {
                        "id": "missing",
                        "type": "function",
                        "function": {"name": "second", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "done",
                "name": "first",
                "content": '{"ok":true}',
            },
            {
                "role": "tool",
                "tool_call_id": "orphan",
                "name": "ghost",
                "content": '{"ok":true}',
            },
        ]
        original = copy.deepcopy(history)

        result = APIMessageBuilder().build(history)

        self.assertEqual(history, original)
        tool_messages = [
            message for message in result.messages if message["role"] == "tool"
        ]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["done", "missing"],
        )
        missing_payload = json.loads(tool_messages[-1]["content"])
        self.assertEqual(
            missing_payload["error"]["code"],
            "result_unavailable_for_api",
        )
        self.assertEqual(
            missing_payload["meta"]["effect_disposition"],
            "unknown",
        )
        self.assertEqual(result.report.inserted_unknown_results, 1)
        self.assertEqual(result.report.dropped_orphan_results, 1)

    def test_duplicate_call_ids_are_repaired_with_matching_results_on_copy(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
            _assistant_call("duplicate"),
            _tool_result("duplicate"),
            {"role": "assistant", "content": "first done"},
            {"role": "user", "content": "second"},
            _assistant_call("duplicate"),
            _tool_result("duplicate"),
        ]

        result = APIMessageBuilder().build(history)

        call_ids = [
            message["tool_calls"][0]["id"]
            for message in result.messages
            if message["role"] == "assistant" and message.get("tool_calls")
        ]
        result_ids = [
            message["tool_call_id"]
            for message in result.messages
            if message["role"] == "tool"
        ]
        self.assertEqual(call_ids, ["duplicate", "duplicate__api_2"])
        self.assertEqual(result_ids, call_ids)
        self.assertEqual(history[-2]["tool_calls"][0]["id"], "duplicate")
        self.assertEqual(result.report.repaired_tool_call_ids, 2)

    def test_adjacent_user_messages_merge_only_in_api_copy(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first unresolved request"},
            {"role": "user", "content": "second request"},
        ]

        result = APIMessageBuilder().build(history)

        self.assertEqual([message["role"] for message in result.messages], ["system", "user"])
        self.assertIn("first unresolved request", result.messages[-1]["content"])
        self.assertIn("second request", result.messages[-1]["content"])
        self.assertEqual(history[-1]["content"], "second request")
        self.assertEqual(result.report.merged_adjacent_users, 1)

    def test_agent_gives_provider_a_disposable_message_copy(self) -> None:
        provider = MutatingProvider()
        agent = Agent(provider, system_prompt="stable system")

        self.assertEqual(agent.chat("original user"), "done")

        self.assertEqual(agent.messages[0]["content"], "stable system")
        self.assertEqual(agent.messages[1]["content"], "original user")
        self.assertEqual(provider.received[0][0]["content"], "stable system")


def _assistant_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }
        ],
    }


def _tool_result(call_id: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "echo",
        "content": '{"ok":true}',
    }


if __name__ == "__main__":
    unittest.main()
