from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.cli import _print_sessions
from mini_harness.execution import ThreadExecutionBackend
from mini_harness.provider import ProviderError, ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB
from mini_harness.tools import ToolDefinition, ToolRegistry


class RecordingProvider:
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


class FailingProvider:
    def complete(self, _messages, *, tools=None):
        raise ProviderError("offline")


class SessionStoreTests(unittest.TestCase):
    def test_session_listing_is_scoped_before_limit_and_cli_rendering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session(
                    "alice system",
                    session_id="alice-private-project",
                    profile_id="alice",
                )
                db.append_message(
                    "alice-private-project",
                    {"role": "user", "content": "alice message"},
                )
                db.create_session(
                    "bob system",
                    session_id="bob-client-secret",
                    profile_id="bob",
                )
                db.append_message(
                    "bob-client-secret",
                    {"role": "user", "content": "bob message"},
                )
                db.create_session(
                    "alice second system",
                    session_id="alice-second",
                    profile_id="alice",
                )

                alice = db.list_sessions(profile_id="alice")
                bob = db.list_sessions(profile_id="bob")
                limited_alice = db.list_sessions(
                    profile_id="alice",
                    limit=1,
                )
                output = StringIO()
                with redirect_stdout(output):
                    _print_sessions(db, profile_id="alice")
            finally:
                db.close()

        self.assertEqual(
            {session["id"] for session in alice},
            {"alice-private-project", "alice-second"},
        )
        self.assertTrue(
            all(session["profile_id"] == "alice" for session in alice)
        )
        self.assertEqual(
            [session["id"] for session in bob],
            ["bob-client-secret"],
        )
        self.assertEqual(len(limited_alice), 1)
        self.assertTrue(limited_alice[0]["id"].startswith("alice-"))
        rendered = output.getvalue()
        self.assertIn("alice-private-project", rendered)
        self.assertIn("alice-second", rendered)
        self.assertNotIn("bob-client-secret", rendered)
        self.assertNotIn("profile=bob", rendered)

    def test_messages_and_original_system_prompt_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            first = SessionDB(path)
            session = first.create_session("stable system", session_id="session-a")
            first.append_message(session.id, {"role": "user", "content": "hello"})
            first.append_message(
                session.id,
                {"role": "assistant", "content": "world"},
            )
            first.close()

            reopened = SessionDB(path)
            restored, report = reopened.resume_session("session-a")
            reopened.close()

        self.assertEqual(restored.system_prompt, "stable system")
        self.assertEqual(
            [message["role"] for message in report.messages],
            ["user", "assistant"],
        )
        self.assertFalse(report.changed)

    def test_missing_tool_result_becomes_persistent_unknown_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            db = SessionDB(path)
            session = db.create_session("system", session_id="crashed")
            db.append_message(session.id, {"role": "user", "content": "write it"})
            db.append_message(
                session.id,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "write-1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"x","content":"y"}',
                            },
                        }
                    ],
                },
            )

            _restored, first_report = db.resume_session(session.id)
            _restored, second_report = db.resume_session(session.id)
            audit = db.audit_messages(session.id)
            db.close()

        self.assertEqual(first_report.inserted_unknown_results, 1)
        self.assertEqual(second_report.inserted_unknown_results, 0)
        self.assertEqual(
            [message["role"] for message in first_report.messages],
            ["user", "assistant", "tool"],
        )
        payload = json.loads(first_report.messages[-1]["content"])
        self.assertEqual(
            payload["error"]["code"],
            "result_unavailable_after_resume",
        )
        self.assertEqual(payload["meta"]["effect_disposition"], "unknown")
        self.assertEqual(payload["meta"]["execution_phase"], "result_unavailable")
        self.assertEqual(audit[-1]["source"], "recovery")

    def test_agent_resume_does_not_execute_unknown_prior_tool_call(self) -> None:
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="dangerous",
                description="A non-idempotent operation.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments),
            )
        )
        provider = RecordingProvider(
            [ProviderResponse(content="I see an unknown prior result.", tool_calls=(), raw={})]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system", session_id="resume-no-replay")
            db.append_message(session.id, {"role": "user", "content": "do it"})
            db.append_message(
                session.id,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "danger-1",
                            "type": "function",
                            "function": {
                                "name": "dangerous",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            )

            agent = Agent(
                provider,
                system_prompt="changed system should be ignored",
                tools=registry,
                session_db=db,
                session_id=session.id,
            )
            answer = agent.chat("inspect the prior state")
            db.close()

        self.assertEqual(answer, "I see an unknown prior result.")
        self.assertEqual(handled, [])
        prior_tool_result = provider.calls[0]["messages"][3]
        self.assertEqual(prior_tool_result["role"], "tool")
        self.assertIn("result_unavailable_after_resume", prior_tool_result["content"])
        self.assertEqual(provider.calls[0]["messages"][0]["content"], "system")

    def test_partial_tool_batch_only_synthesizes_the_missing_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system")
            db.append_message(session.id, {"role": "user", "content": "batch"})
            db.append_message(
                session.id,
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
            )
            db.append_message(
                session.id,
                {
                    "role": "tool",
                    "tool_call_id": "done",
                    "name": "first",
                    "content": '{"ok":true}',
                },
            )

            _session, report = db.resume_session(session.id)
            db.close()

        results = [
            message for message in report.messages if message["role"] == "tool"
        ]
        self.assertEqual(report.inserted_unknown_results, 1)
        self.assertEqual(
            [message["tool_call_id"] for message in results],
            ["done", "missing"],
        )
        self.assertIn("result_unavailable_after_resume", results[-1]["content"])

    def test_provider_failure_keeps_user_audit_and_merges_next_user_for_api(self) -> None:
        resumed_provider = RecordingProvider(
            [ProviderResponse(content="recovered", tool_calls=(), raw={})]
        )
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            first_agent = Agent(
                FailingProvider(),
                system_prompt="system",
                session_db=db,
                session_id="provider-failure",
            )
            with self.assertRaises(ProviderError):
                first_agent.chat("first unresolved request")

            audit_after_failure = db.audit_messages("provider-failure")
            resumed_agent = Agent(
                resumed_provider,
                system_prompt="ignored",
                session_db=db,
                session_id="provider-failure",
            )
            self.assertEqual(
                resumed_agent.chat("second request"),
                "recovered",
            )
            merged_report = resumed_agent.last_recovery_report
            full_audit = db.audit_messages("provider-failure")
            db.close()

        self.assertEqual(
            [row["role"] for row in audit_after_failure],
            ["user"],
        )
        self.assertIsNotNone(merged_report)
        self.assertEqual(merged_report.merged_same_role_messages, 1)
        api_messages = resumed_provider.calls[0]["messages"]
        self.assertEqual(
            [message["role"] for message in api_messages],
            ["system", "user"],
        )
        self.assertIn("first unresolved request", api_messages[-1]["content"])
        self.assertIn("second request", api_messages[-1]["content"])
        inactive_user_rows = [
            row
            for row in full_audit
            if row["role"] == "user" and row["active"] == 0
        ]
        self.assertEqual(len(inactive_user_rows), 1)

    def test_orphan_tool_result_is_deactivated_but_retained_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system")
            db.append_message(session.id, {"role": "user", "content": "hello"})
            db.append_message(
                session.id,
                {
                    "role": "tool",
                    "tool_call_id": "never-called",
                    "name": "ghost",
                    "content": "{}",
                },
            )

            _session, report = db.resume_session(session.id)
            audit = db.audit_messages(session.id)
            db.close()

        self.assertEqual(report.deactivated_orphan_results, 1)
        self.assertEqual([message["role"] for message in report.messages], ["user"])
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[-1]["active"], 0)
        self.assertEqual(audit[-1]["original_message"]["tool_call_id"], "never-called")

    def test_duplicate_tool_call_ids_and_results_are_repaired_together(self) -> None:
        def assistant_call(call_id: str) -> dict:
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

        def tool_result(call_id: str) -> dict:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "echo",
                "content": '{"ok":true}',
            }

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system")
            db.append_message(session.id, {"role": "user", "content": "first"})
            db.append_message(session.id, assistant_call("duplicate"))
            db.append_message(session.id, tool_result("duplicate"))
            db.append_message(
                session.id,
                {"role": "assistant", "content": "first done"},
            )
            db.append_message(session.id, {"role": "user", "content": "second"})
            db.append_message(session.id, assistant_call("duplicate"))
            db.append_message(session.id, tool_result("duplicate"))

            _session, report = db.resume_session(session.id)
            audit = db.audit_messages(session.id)
            db.close()

        assistants = [
            message
            for message in report.messages
            if message["role"] == "assistant" and message.get("tool_calls")
        ]
        results = [
            message for message in report.messages if message["role"] == "tool"
        ]
        call_ids = [message["tool_calls"][0]["id"] for message in assistants]
        result_ids = [message["tool_call_id"] for message in results]
        self.assertEqual(call_ids, ["duplicate", "duplicate__resume_2"])
        self.assertEqual(result_ids, call_ids)
        self.assertGreaterEqual(report.repaired_tool_call_ids, 2)
        repaired_assistant = next(
            row
            for row in audit
            if row["message"].get("tool_calls")
            and row["message"]["tool_calls"][0]["id"] == "duplicate__resume_2"
        )
        self.assertEqual(
            repaired_assistant["original_message"]["tool_calls"][0]["id"],
            "duplicate",
        )

    def test_normal_agent_turn_is_incremental_and_resumable(self) -> None:
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
        first_provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="echo-1", name="echo", arguments="{}"),),
                    raw={},
                ),
                ProviderResponse(content="first done", tool_calls=(), raw={}),
            ]
        )
        second_provider = RecordingProvider(
            [ProviderResponse(content="second done", tool_calls=(), raw={})]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            first_agent = Agent(
                first_provider,
                system_prompt="stable",
                tools=registry,
                session_db=db,
                session_id="normal",
            )
            self.assertEqual(first_agent.chat("first"), "first done")
            audit_after_first = db.audit_messages("normal")

            resumed_agent = Agent(
                second_provider,
                system_prompt="different",
                tools=registry,
                session_db=db,
                session_id="normal",
            )
            self.assertEqual(resumed_agent.chat("second"), "second done")
            db.close()

        self.assertEqual(
            [row["role"] for row in audit_after_first],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(len(handled), 1)
        roles = [message["role"] for message in second_provider.calls[0]["messages"]]
        self.assertEqual(
            roles,
            ["system", "user", "assistant", "tool", "assistant", "user"],
        )
        self.assertEqual(second_provider.calls[0]["messages"][0]["content"], "stable")


if __name__ == "__main__":
    unittest.main()
