from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.events import JsonlEventSink
from mini_harness.provider import ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB
from mini_harness.tools import ToolDefinition, ToolRegistry


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, _messages, *, tools=None):
        return self.responses.pop(0)


class EventLogTests(unittest.TestCase):
    def test_jsonl_events_are_parseable_and_do_not_contain_message_or_arguments(self) -> None:
        secret = "SUPER_SECRET_EVENT_PAYLOAD"
        registry = ToolRegistry()
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
                    tool_calls=(
                        ToolCall(
                            id="echo-1",
                            name="echo",
                            arguments=f'{{"secret":"{secret}"}}',
                        ),
                    ),
                    raw={},
                ),
                ProviderResponse(content="done", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                event_sink=JsonlEventSink(path),
            )
            agent.chat(f"never log {secret}")
            raw = path.read_text(encoding="utf-8")

        records = [json.loads(line) for line in raw.splitlines()]
        self.assertNotIn(secret, raw)
        self.assertIn("turn.started", [record["event"] for record in records])
        self.assertIn("tool.result", [record["event"] for record in records])
        self.assertIn("turn.completed", [record["event"] for record in records])
        self.assertEqual(
            next(
                record
                for record in records
                if record["event"] == "turn.started"
            )["details"]["user_message_chars"],
            len(f"never log {secret}"),
        )

    def test_api_message_repairs_log_counts_without_internal_content(self) -> None:
        secret = "INTERNAL_API_MESSAGE_SECRET"
        provider = ScriptedProvider(
            [ProviderResponse(content="done", tool_calls=(), raw={})]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = SessionDB(root / "sessions.db")
            session = db.create_session("system", session_id="api-event")
            db.append_message(
                session.id,
                {
                    "role": "user",
                    "content": "first",
                    "_internal_provider_field": secret,
                },
            )
            event_path = root / "events.jsonl"
            agent = Agent(
                provider,
                system_prompt="ignored",
                session_db=db,
                session_id=session.id,
                event_sink=JsonlEventSink(event_path),
            )

            self.assertEqual(agent.chat("second"), "done")
            raw = event_path.read_text(encoding="utf-8")
            db.close()

        records = [json.loads(line) for line in raw.splitlines()]
        prepared = next(
            record
            for record in records
            if record["event"] == "api_messages.prepared"
        )
        self.assertNotIn(secret, raw)
        self.assertEqual(prepared["details"]["dropped_fields"], 1)
        self.assertEqual(prepared["details"]["merged_adjacent_users"], 0)


if __name__ == "__main__":
    unittest.main()
