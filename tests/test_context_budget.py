from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.api_messages import APIMessageBuilder
from mini_harness.context_budget import ContextBudgetPolicy
from mini_harness.events import JsonlEventSink, MemoryEventSink
from mini_harness.provider import ProviderResponse
from mini_harness.session_store import SessionDB


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
            }
        )
        return self.responses.pop(0)


class ContextBudgetTests(unittest.TestCase):
    def test_fresh_large_tool_result_is_delivered_complete_once(self) -> None:
        payload = {
            "ok": True,
            "result": {
                "path": "src/mini_harness/agent.py",
                "content": "FRESH_EVIDENCE_" + "x" * 12_000,
            },
            "meta": {"effect_disposition": "completed"},
        }
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "inspect agent"},
            _assistant_calls(("fresh",), tool_name="read_file"),
            _tool_result("fresh", payload, name="read_file"),
        ]
        original = copy.deepcopy(history)

        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=10_000,
                reserved_output_tokens=500,
                approximate_chars_per_token=4,
                max_tool_result_tokens=2_000,
                recent_tool_tail_tokens=12_000,
            )
        ).build(history)

        delivered = next(
            message["content"]
            for message in result.messages
            if message["role"] == "tool"
        )
        self.assertEqual(delivered, history[-1]["content"])
        self.assertEqual(history, original)
        report = result.report.context_budget
        self.assertEqual(report.protected_recent_tool_results, 1)
        self.assertEqual(report.pruned_old_tool_results, 0)
        self.assertEqual(report.compacted_tool_results, 0)

    def test_old_file_and_listing_results_get_informative_summaries(self) -> None:
        old_read = {
            "ok": True,
            "result": {
                "path": "src/mini_harness/agent.py",
                "content": "r" * 2_000,
            },
            "meta": {"effect_disposition": "completed"},
        }
        old_list = {
            "ok": True,
            "result": {
                "path": "src",
                "entries": [{"path": f"file-{index}"} for index in range(80)],
            },
            "meta": {"effect_disposition": "completed"},
        }
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old inspection"},
            _assistant_calls(
                ("old-read", "old-list"),
                tool_name=("read_file", "list_files"),
            ),
            _tool_result("old-read", old_read, name="read_file"),
            _tool_result("old-list", old_list, name="list_files"),
            {"role": "user", "content": "new request"},
        ]

        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=3_000,
                reserved_output_tokens=300,
                approximate_chars_per_token=4,
                max_tool_result_tokens=70,
                recent_tool_tail_tokens=32,
            )
        ).build(history)

        summaries = {
            message["name"]: json.loads(message["content"])
            for message in result.messages
            if message["role"] == "tool"
        }
        self.assertIn("path=src/mini_harness/agent.py", summaries["read_file"]["message"])
        self.assertIn("original_chars=", summaries["read_file"]["message"])
        self.assertIn("old content pruned", summaries["read_file"]["message"])
        self.assertIn("path=src", summaries["list_files"]["message"])
        self.assertIn("entries=80", summaries["list_files"]["message"])
        self.assertIn("old listing pruned", summaries["list_files"]["message"])
        self.assertEqual(result.report.context_budget.pruned_old_tool_results, 2)

    def test_duplicate_tool_results_keep_latest_complete_and_back_reference_old(
        self,
    ) -> None:
        duplicate_payload = {
            "ok": True,
            "result": "SAME_OUTPUT_" + "d" * 1_000,
            "meta": {"effect_disposition": "completed"},
        }
        duplicate_content = json.dumps(duplicate_payload)
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old"},
            _assistant_calls(("old",)),
            _tool_result("old", duplicate_payload),
            {"role": "user", "content": "new"},
            _assistant_calls(("new",)),
            _tool_result("new", duplicate_payload),
        ]

        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=2_000,
                reserved_output_tokens=200,
                approximate_chars_per_token=4,
                max_tool_result_tokens=70,
                recent_tool_tail_tokens=500,
            )
        ).build(history)

        outputs = {
            message["tool_call_id"]: message["content"]
            for message in result.messages
            if message["role"] == "tool"
        }
        self.assertEqual(outputs["new"], duplicate_content)
        old = json.loads(outputs["old"])
        self.assertIn("same content as a newer call", old["message"])
        self.assertTrue(old["meta"]["context_deduplicated"])
        self.assertEqual(
            result.report.context_budget.deduplicated_tool_results,
            1,
        )

    def test_old_unknown_summary_preserves_no_retry_semantics(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old"},
            _assistant_calls(("unknown",), tool_name="write_file"),
            _tool_result(
                "unknown",
                {
                    "ok": False,
                    "error": {
                        "code": "result_unavailable",
                        "message": "diagnostic-" + "u" * 2_000,
                    },
                    "meta": {"effect_disposition": "unknown"},
                },
                name="write_file",
            ),
            {"role": "user", "content": "new"},
        ]

        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=2_000,
                reserved_output_tokens=200,
                approximate_chars_per_token=4,
                max_tool_result_tokens=70,
                recent_tool_tail_tokens=32,
            )
        ).build(history)

        summary = json.loads(
            next(
                message["content"]
                for message in result.messages
                if message["role"] == "tool"
            )
        )
        self.assertEqual(summary["meta"]["effect_disposition"], "unknown")
        self.assertIn("Outcome remains unknown", summary["message"])
        self.assertIn("Do not retry automatically", summary["message"])
        self.assertEqual(
            result.report.context_budget.protected_unknown_results,
            1,
        )

    def test_budget_measurement_is_stable_and_counts_tool_schemas(self) -> None:
        history = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "hello"},
        ]
        policy = ContextBudgetPolicy(
            max_input_tokens=2_000,
            reserved_output_tokens=200,
            approximate_chars_per_token=4,
            max_tool_result_tokens=200,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "large_schema",
                    "description": "x" * 400,
                    "parameters": {"type": "object"},
                },
            }
        ]
        original = copy.deepcopy(history)
        builder = APIMessageBuilder(context_budget=policy)

        first = builder.build(history, tools=tools)
        second = builder.build(history, tools=tools)
        without_tools = builder.build(history, tools=None)

        self.assertEqual(history, original)
        self.assertEqual(first, second)
        self.assertIsNotNone(first.report.context_budget)
        report = first.report.context_budget
        self.assertGreater(report.tool_schema_tokens, 0)
        self.assertGreater(
            report.estimated_tokens_before,
            without_tools.report.context_budget.estimated_tokens_before,
        )
        self.assertFalse(report.budget_exceeded)

    def test_oldest_complete_turn_is_dropped_without_splitting_latest_turn(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old-user-" + "x" * 600},
            {"role": "assistant", "content": "old-answer-" + "y" * 600},
            {"role": "user", "content": "latest-user"},
            {"role": "assistant", "content": "latest-answer"},
        ]
        original = copy.deepcopy(history)
        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=300,
                reserved_output_tokens=60,
                approximate_chars_per_token=4,
                max_tool_result_tokens=80,
            )
        ).build(history)

        contents = [message.get("content") for message in result.messages]
        report = result.report.context_budget
        self.assertEqual(history, original)
        self.assertNotIn(history[1]["content"], contents)
        self.assertNotIn(history[2]["content"], contents)
        self.assertIn("latest-user", contents)
        self.assertIn("latest-answer", contents)
        self.assertEqual(report.dropped_turns, 1)
        self.assertEqual(report.dropped_messages, 2)
        self.assertLessEqual(
            report.estimated_tokens_after,
            report.compaction_target_tokens,
        )
        self.assertFalse(report.budget_exceeded)

    def test_compaction_headroom_keeps_the_same_prefix_boundary(self) -> None:
        history = [{"role": "system", "content": "system"}]
        for index in range(5):
            history.extend(
                (
                    {
                        "role": "user",
                        "content": f"user-{index}-" + "u" * 250,
                    },
                    {
                        "role": "assistant",
                        "content": f"answer-{index}-" + "a" * 250,
                    },
                )
            )
        builder = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=500,
                reserved_output_tokens=100,
                approximate_chars_per_token=4,
                max_tool_result_tokens=80,
                compaction_target_ratio=0.8,
            )
        )

        first = builder.build(history)
        grown = [
            *history,
            {"role": "user", "content": "small follow-up"},
            {"role": "assistant", "content": "small answer"},
        ]
        second = builder.build(
            grown,
            context_checkpoint=first.context_checkpoint,
        )

        first_boundary = next(
            message["content"]
            for message in first.messages
            if message["role"] == "user"
        )
        second_boundary = next(
            message["content"]
            for message in second.messages
            if message["role"] == "user"
        )
        self.assertEqual(first_boundary, second_boundary)
        self.assertEqual(
            first.report.context_budget.dropped_turns,
            second.report.context_budget.dropped_turns,
        )
        self.assertEqual(
            second.report.context_budget.reused_checkpoint_turns,
            first.report.context_budget.dropped_turns,
        )

    def test_tool_batch_remains_protocol_complete_when_its_turn_is_retained(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old-" + "x" * 700},
            {"role": "assistant", "content": "old-answer-" + "y" * 700},
            {"role": "user", "content": "run both"},
            _assistant_calls(("call-a", "call-b")),
            _tool_result("call-a", {"ok": True, "result": "a"}),
            _tool_result("call-b", {"ok": True, "result": "b"}),
        ]
        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=320,
                reserved_output_tokens=50,
                approximate_chars_per_token=4,
                max_tool_result_tokens=80,
            )
        ).build(history)

        calls = [
            call["id"]
            for message in result.messages
            for call in message.get("tool_calls", [])
        ]
        results = [
            message["tool_call_id"]
            for message in result.messages
            if message["role"] == "tool"
        ]
        self.assertEqual(calls, ["call-a", "call-b"])
        self.assertEqual(results, calls)
        self.assertFalse(result.report.context_budget.budget_exceeded)

    def test_old_large_tool_results_are_pruned_but_unknown_fact_is_preserved(self) -> None:
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old run"},
            _assistant_calls(("done", "unknown")),
            _tool_result(
                "done",
                {
                    "ok": True,
                    "result": "x" * 2_000,
                    "meta": {"effect_disposition": "completed"},
                },
            ),
            _tool_result(
                "unknown",
                {
                    "ok": False,
                    "error": {
                        "code": "result_unavailable",
                        "message": "y" * 2_000,
                    },
                    "meta": {"effect_disposition": "unknown"},
                },
            ),
            {"role": "user", "content": "current request"},
        ]
        result = APIMessageBuilder(
            context_budget=ContextBudgetPolicy(
                max_input_tokens=700,
                reserved_output_tokens=100,
                approximate_chars_per_token=4,
                max_tool_result_tokens=70,
                recent_tool_tail_tokens=32,
            )
        ).build(history)

        tool_payloads = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in result.messages
            if message["role"] == "tool"
        }
        self.assertTrue(tool_payloads["done"]["meta"]["context_pruned"])
        self.assertEqual(
            tool_payloads["done"]["meta"]["effect_disposition"],
            "completed",
        )
        self.assertTrue(tool_payloads["unknown"]["meta"]["context_pruned"])
        self.assertEqual(
            tool_payloads["unknown"]["meta"]["effect_disposition"],
            "unknown",
        )
        self.assertIn(
            "Do not retry automatically",
            tool_payloads["unknown"]["message"],
        )
        report = result.report.context_budget
        self.assertEqual(report.compacted_tool_results, 2)
        self.assertEqual(report.pruned_old_tool_results, 2)
        self.assertEqual(report.protected_unknown_results, 1)

    def test_agent_uses_one_minimal_no_tools_finalizer_after_budget_failure(self) -> None:
        provider = RecordingProvider(
            [ProviderResponse(content="Context limit reached safely.", tool_calls=(), raw={})]
        )
        events = MemoryEventSink()
        agent = Agent(
            provider,
            system_prompt="stable system",
            context_budget=ContextBudgetPolicy(
                max_input_tokens=500,
                reserved_output_tokens=100,
                approximate_chars_per_token=2,
                max_tool_result_tokens=80,
            ),
            event_sink=events,
        )
        oversized = "PRIVATE_CONTEXT_" + "z" * 5_000

        answer = agent.chat(oversized)

        self.assertEqual(answer, "Context limit reached safely.")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0]["tools"])
        self.assertNotIn(oversized, json.dumps(provider.calls[0]["messages"]))
        self.assertEqual(agent.messages[1]["content"], oversized)
        self.assertFalse(agent.last_turn_completed)
        self.assertEqual(agent.last_stop_reason, "context_budget")
        event_names = [event.event for event in events.events]
        self.assertEqual(event_names.count("context.budget_exceeded"), 1)
        self.assertIn("turn.partial", event_names)

    def test_agent_trims_api_copy_but_preserves_sqlite_audit_history(self) -> None:
        secret = "OLD_AUDIT_CONTEXT_" + "q" * 5_000
        provider = RecordingProvider(
            [ProviderResponse(content="latest handled", tool_calls=(), raw={})]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = SessionDB(root / "sessions.db")
            session = db.create_session("stable system", session_id="context-audit")
            db.append_message(
                session.id,
                {"role": "user", "content": secret},
            )
            db.append_message(
                session.id,
                {"role": "assistant", "content": "old answer"},
            )
            event_path = root / "events.jsonl"
            agent = Agent(
                provider,
                system_prompt="ignored",
                session_db=db,
                session_id=session.id,
                context_budget=ContextBudgetPolicy(
                    max_input_tokens=500,
                    reserved_output_tokens=100,
                    approximate_chars_per_token=4,
                    max_tool_result_tokens=80,
                ),
                event_sink=JsonlEventSink(event_path),
            )

            self.assertEqual(agent.chat("latest request"), "latest handled")
            audit = db.audit_messages(session.id)
            event_raw = event_path.read_text(encoding="utf-8")
            db.close()

        provider_raw = json.dumps(provider.calls[0]["messages"])
        audit_raw = json.dumps(audit)
        self.assertNotIn(secret, provider_raw)
        self.assertIn(secret, audit_raw)
        self.assertNotIn(secret, event_raw)
        events = [json.loads(line) for line in event_raw.splitlines()]
        budget_event = next(
            event
            for event in events
            if event["event"] == "context.budget_evaluated"
        )
        self.assertEqual(budget_event["details"]["dropped_turns"], 1)
        self.assertFalse(budget_event["details"]["budget_exceeded"])

    def test_old_tool_pruning_preserves_agent_and_sqlite_audit_content(
        self,
    ) -> None:
        secret = "TOOL_AUDIT_SECRET_" + "s" * 2_000
        tool_payload = {
            "ok": True,
            "result": {
                "path": "private/audit-file.txt",
                "content": secret,
            },
            "meta": {"effect_disposition": "completed"},
        }
        provider = RecordingProvider(
            [ProviderResponse(content="new request handled", tool_calls=(), raw={})]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = SessionDB(root / "sessions.db")
            session = db.create_session(
                "stable system",
                session_id="tool-pruning-audit",
            )
            db.append_message(
                session.id,
                {"role": "user", "content": "old inspection"},
            )
            db.append_message(
                session.id,
                _assistant_calls(("old-read",), tool_name="read_file"),
            )
            db.append_message(
                session.id,
                _tool_result(
                    "old-read",
                    tool_payload,
                    name="read_file",
                ),
            )
            db.append_message(
                session.id,
                {"role": "assistant", "content": "old answer"},
            )
            event_path = root / "events.jsonl"
            agent = Agent(
                provider,
                system_prompt="ignored",
                session_db=db,
                session_id=session.id,
                context_budget=ContextBudgetPolicy(
                    max_input_tokens=2_000,
                    reserved_output_tokens=200,
                    approximate_chars_per_token=4,
                    max_tool_result_tokens=70,
                    recent_tool_tail_tokens=32,
                ),
                event_sink=JsonlEventSink(event_path),
            )

            self.assertEqual(
                agent.chat("current request"),
                "new request handled",
            )
            audit = db.audit_messages(session.id)
            agent_snapshot = agent.messages
            event_raw = event_path.read_text(encoding="utf-8")
            db.close()

        provider_raw = json.dumps(
            provider.calls[0]["messages"],
            ensure_ascii=False,
        )
        self.assertNotIn(secret, provider_raw)
        self.assertIn("path=private/audit-file.txt", provider_raw)
        self.assertIn(secret, json.dumps(audit, ensure_ascii=False))
        self.assertIn(secret, json.dumps(agent_snapshot, ensure_ascii=False))
        self.assertNotIn(secret, event_raw)
        self.assertNotIn("private/audit-file.txt", event_raw)
        budget_event = next(
            json.loads(line)
            for line in event_raw.splitlines()
            if json.loads(line)["event"] == "context.budget_evaluated"
        )
        self.assertEqual(
            budget_event["details"]["pruned_old_tool_results"],
            1,
        )
        self.assertEqual(
            budget_event["details"]["protected_recent_tool_results"],
            0,
        )


def _assistant_calls(
    call_ids: tuple[str, ...],
    *,
    tool_name: str | tuple[str, ...] = "echo",
) -> dict:
    names = (
        tool_name
        if isinstance(tool_name, tuple)
        else tuple(tool_name for _ in call_ids)
    )
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for call_id, name in zip(call_ids, names)
        ],
    }


def _tool_result(
    call_id: str,
    payload: dict,
    *,
    name: str = "echo",
) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(payload),
    }


if __name__ == "__main__":
    unittest.main()
