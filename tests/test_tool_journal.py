from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.execution import ThreadExecutionBackend
from mini_harness.provider import ProviderResponse, ToolCall
from mini_harness.session_store import (
    CURRENT_SCHEMA_VERSION,
    SessionDB,
    SessionError,
)
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


class ToolJournalTests(unittest.TestCase):
    def test_legacy_database_migrates_once_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            _create_legacy_database(path)

            first = SessionDB(path)
            first_audit = first.audit_messages("legacy")
            self.assertEqual(first.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(first.audit_tool_journal("legacy"), ())
            first.close()

            second = SessionDB(path)
            second_audit = second.audit_messages("legacy")
            journal_columns = _table_columns(path, "tool_execution_journal")
            second.close()

        self.assertEqual(first_audit, second_audit)
        self.assertEqual(first_audit[0]["message"]["content"], "preserved")
        self.assertIn("arguments_hash", journal_columns)
        self.assertIn("result_message_id", journal_columns)

    def test_v2_database_adds_execution_metadata_without_losing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-v2.db"
            _create_v2_database(path)

            db = SessionDB(path)
            journal = db.audit_tool_journal("legacy")
            version = db.schema_version
            db.close()
            columns = _table_columns(path, "tool_execution_journal")

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["tool_name"], "legacy_tool")
        self.assertIsNone(journal[0]["execution_backend"])
        self.assertEqual(journal[0]["hard_terminated"], 0)
        self.assertIn("execution_backend", columns)
        self.assertIn("hard_terminated", columns)

    def test_v3_database_adds_default_profile_without_losing_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-v3.db"
            _create_v3_database(path)

            db = SessionDB(path)
            session = db.get_session("legacy")
            sessions = db.list_sessions(profile_id="default")
            version = db.schema_version
            db.close()
            columns = _table_columns(path, "sessions")

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(session.profile_id, "default")
        self.assertEqual(sessions[0]["profile_id"], "default")
        self.assertIn("profile_id", columns)

    def test_failed_migration_rolls_back_and_leaves_legacy_data_openable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken-migration.db"
            _create_legacy_database(path)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE tool_execution_journal (broken TEXT)"
            )
            connection.commit()
            connection.close()

            with self.assertRaises((SessionError, sqlite3.OperationalError)):
                SessionDB(path)

            reopened = sqlite3.connect(path)
            version = reopened.execute("PRAGMA user_version").fetchone()[0]
            preserved = reopened.execute(
                "SELECT message_json FROM messages"
            ).fetchone()[0]
            integrity = reopened.execute("PRAGMA integrity_check").fetchone()[0]
            broken_columns = {
                row[1]
                for row in reopened.execute(
                    'PRAGMA table_info("tool_execution_journal")'
                ).fetchall()
            }
            reopened.close()

        self.assertEqual(version, 0)
        self.assertIn("preserved", preserved)
        self.assertEqual(integrity, "ok")
        self.assertEqual(broken_columns, {"broken"})

    def test_successful_execution_links_call_journal_and_result(self) -> None:
        secret_argument = "JOURNAL_ARGUMENT_MUST_NOT_BE_STORED"
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: arguments,
            )
        )
        provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="echo-1",
                            name="echo",
                            arguments=json.dumps({"value": secret_argument}),
                        ),
                    ),
                    raw={},
                ),
                ProviderResponse(content="done", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                session_db=db,
                session_id="journal-success",
            )
            self.assertEqual(agent.chat("run echo"), "done")
            journal = db.audit_tool_journal("journal-success")
            audit = db.audit_messages("journal-success")
            db.close()

        self.assertEqual(len(journal), 1)
        row = journal[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["effect_disposition"], "completed")
        self.assertEqual(row["execution_phase"], "handler_completed")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(len(row["arguments_hash"]), 64)
        self.assertEqual(len(row["call_signature"]), 64)
        self.assertNotIn(secret_argument, json.dumps(row))
        result_row = next(item for item in audit if item["id"] == row["result_message_id"])
        self.assertEqual(result_row["role"], "tool")

    def test_preflight_or_approval_rejection_never_marks_handler_running(self) -> None:
        registry = ToolRegistry(
            approval_callback=lambda _definition, _arguments: False,
            execution_backend=ThreadExecutionBackend(),
        )
        registry.register(
            ToolDefinition(
                name="dangerous",
                description="Needs approval.",
                parameters={"type": "object"},
                handler=lambda _arguments: self.fail("handler must not run"),
                requires_approval=True,
            )
        )
        provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="denied-1", name="dangerous", arguments="{}"),
                    ),
                    raw={},
                ),
                ProviderResponse(content="denied", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                session_db=db,
                session_id="journal-denied",
            )
            self.assertEqual(agent.chat("try"), "denied")
            row = db.audit_tool_journal("journal-denied")[0]
            db.close()

        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["effect_disposition"], "none")
        self.assertEqual(row["execution_phase"], "rejected_before_execution")
        self.assertIsNone(row["started_at"])

    def test_handler_failure_is_persisted_as_unknown(self) -> None:
        def fail(_arguments):
            raise RuntimeError("boom")

        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="fail",
                description="Fail after start.",
                parameters={"type": "object"},
                handler=fail,
            )
        )
        provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall(id="fail-1", name="fail", arguments="{}"),),
                    raw={},
                ),
                ProviderResponse(content="observed", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                session_db=db,
                session_id="journal-failure",
            )
            self.assertEqual(agent.chat("run"), "observed")
            row = db.audit_tool_journal("journal-failure")[0]
            db.close()

        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["effect_disposition"], "unknown")
        self.assertEqual(row["execution_phase"], "result_unavailable")
        self.assertEqual(row["error_code"], "handler_error")
        self.assertIsNotNone(row["started_at"])

    def test_resume_repairs_duplicate_ids_in_call_result_and_journal_together(self) -> None:
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda arguments: arguments,
            )
        )
        provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(id="duplicate", name="echo", arguments='{"n":1}'),
                        ToolCall(id="duplicate", name="echo", arguments='{"n":2}'),
                    ),
                    raw={},
                ),
                ProviderResponse(content="done", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                session_db=db,
                session_id="journal-duplicates",
            )
            self.assertEqual(agent.chat("run both"), "done")
            _session, report = db.resume_session("journal-duplicates")
            journal = db.audit_tool_journal("journal-duplicates")
            audit = db.audit_messages("journal-duplicates")
            db.close()

        journal_ids = [row["tool_call_id"] for row in journal]
        result_ids = [
            row["message"]["tool_call_id"]
            for row in audit
            if row["role"] == "tool" and row["active"] == 1
        ]
        self.assertEqual(journal_ids, ["duplicate", "duplicate__resume_2"])
        self.assertEqual(result_ids, journal_ids)
        self.assertGreaterEqual(report.repaired_tool_call_ids, 2)

    def test_resume_of_prepared_call_proves_handler_never_started(self) -> None:
        report, journal, provider_messages = _resume_crashed_call(mark_running=False)

        self.assertEqual(report.inserted_not_executed_results, 1)
        self.assertEqual(report.inserted_unknown_results, 0)
        self.assertEqual(journal["status"], "completed")
        self.assertEqual(journal["effect_disposition"], "none")
        self.assertIsNone(journal["started_at"])
        prior_result = next(
            message for message in provider_messages if message["role"] == "tool"
        )
        payload = json.loads(prior_result["content"])
        self.assertEqual(payload["error"]["code"], "not_executed_after_resume")

    def test_resume_of_running_call_is_unknown_and_never_replayed(self) -> None:
        report, journal, provider_messages = _resume_crashed_call(mark_running=True)

        self.assertEqual(report.inserted_unknown_results, 1)
        self.assertEqual(journal["status"], "unknown")
        self.assertEqual(journal["effect_disposition"], "unknown")
        self.assertIsNotNone(journal["started_at"])
        prior_result = next(
            message for message in provider_messages if message["role"] == "tool"
        )
        payload = json.loads(prior_result["content"])
        self.assertEqual(
            payload["error"]["code"],
            "result_unavailable_after_resume",
        )

    def test_partial_batch_recovery_uses_each_journals_own_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            session = db.create_session("system", session_id="partial-batch")
            db.append_message(session.id, {"role": "user", "content": "batch"})
            _message_id, journal_ids = db.append_assistant_with_prepared_journals(
                session.id,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "running",
                            "type": "function",
                            "function": {"name": "first", "arguments": "{}"},
                        },
                        {
                            "id": "prepared",
                            "type": "function",
                            "function": {"name": "second", "arguments": "{}"},
                        },
                    ],
                },
            )
            db.mark_tool_running(journal_ids[0])

            _session, report = db.resume_session(session.id)
            journal = db.audit_tool_journal(session.id)
            tool_messages = [
                row["message"]
                for row in db.audit_messages(session.id)
                if row["role"] == "tool" and row["active"] == 1
            ]
            db.close()

        self.assertEqual(report.inserted_unknown_results, 1)
        self.assertEqual(report.inserted_not_executed_results, 1)
        self.assertEqual(
            [row["status"] for row in journal],
            ["unknown", "completed"],
        )
        self.assertEqual(
            [row["effect_disposition"] for row in journal],
            ["unknown", "none"],
        )
        payloads = [json.loads(message["content"]) for message in tool_messages]
        self.assertEqual(
            [payload["error"]["code"] for payload in payloads],
            [
                "result_unavailable_after_resume",
                "not_executed_after_resume",
            ],
        )


def _resume_crashed_call(*, mark_running: bool):
    handled = []
    registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
    registry.register(
        ToolDefinition(
            name="charge",
            description="Non-idempotent.",
            parameters={"type": "object"},
            handler=lambda arguments: handled.append(arguments),
        )
    )
    provider = RecordingProvider(
        [ProviderResponse(content="recovered", tool_calls=(), raw={})]
    )

    with tempfile.TemporaryDirectory() as directory:
        db = SessionDB(Path(directory) / "sessions.db")
        session = db.create_session("system", session_id="crashed-journal")
        db.append_message(session.id, {"role": "user", "content": "charge"})
        _message_id, journal_ids = db.append_assistant_with_prepared_journals(
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
                            "arguments": '{"amount":10}',
                        },
                    }
                ],
            },
        )
        if mark_running:
            db.mark_tool_running(journal_ids[0])

        agent = Agent(
            provider,
            system_prompt="ignored",
            tools=registry,
            session_db=db,
            session_id=session.id,
        )
        report = agent.last_recovery_report
        agent.chat("inspect only")
        journal = db.audit_tool_journal(session.id)[0]
        provider_messages = provider.calls[0]["messages"]
        db.close()

    if handled:
        raise AssertionError("crashed tool was replayed")
    return report, journal, provider_messages


def _create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            system_prompt TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            updated_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            role TEXT NOT NULL,
            message_json TEXT NOT NULL,
            original_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'runtime',
            created_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            UNIQUE(session_id, position)
        );
        CREATE INDEX idx_messages_session_position
            ON messages(session_id, active, position);
        CREATE TABLE recovery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        );
        """
    )
    connection.execute(
        "INSERT INTO sessions(id, system_prompt) VALUES (?, ?)",
        ("legacy", "stable"),
    )
    encoded = json.dumps({"role": "user", "content": "preserved"})
    connection.execute(
        """
        INSERT INTO messages(
            session_id, position, role, message_json, original_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("legacy", 1_000_000, "user", encoded, encoded),
    )
    connection.commit()
    connection.close()


def _create_v2_database(path: Path) -> None:
    _create_legacy_database(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tool_execution_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            assistant_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            tool_call_id TEXT NOT NULL,
            tool_call_index INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_hash TEXT NOT NULL,
            call_signature TEXT NOT NULL,
            idempotency_key TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('prepared', 'running', 'completed', 'unknown')
            ),
            effect_disposition TEXT NOT NULL CHECK (
                effect_disposition IN ('none', 'completed', 'unknown')
            ),
            execution_phase TEXT NOT NULL CHECK (
                execution_phase IN (
                    'prepared',
                    'rejected_before_execution',
                    'handler_started',
                    'handler_completed',
                    'result_unavailable'
                )
            ),
            error_code TEXT,
            result_message_id INTEGER REFERENCES messages(id),
            prepared_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(assistant_message_id, tool_call_index)
        );
        CREATE INDEX idx_tool_journal_session
            ON tool_execution_journal(session_id, id);
        CREATE INDEX idx_tool_journal_incomplete
            ON tool_execution_journal(session_id, status)
            WHERE status IN ('prepared', 'running');
        PRAGMA user_version = 2;
        """
    )
    connection.execute(
        """
        INSERT INTO tool_execution_journal(
            session_id,
            assistant_message_id,
            tool_call_id,
            tool_call_index,
            tool_name,
            arguments_hash,
            call_signature,
            status,
            effect_disposition,
            execution_phase
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy",
            1,
            "legacy-call",
            0,
            "legacy_tool",
            "a" * 64,
            "b" * 64,
            "prepared",
            "none",
            "prepared",
        ),
    )
    connection.commit()
    connection.close()


def _create_v3_database(path: Path) -> None:
    _create_v2_database(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        ALTER TABLE tool_execution_journal
            ADD COLUMN execution_backend TEXT;
        ALTER TABLE tool_execution_journal
            ADD COLUMN hard_terminated INTEGER NOT NULL DEFAULT 0
            CHECK (hard_terminated IN (0, 1));
        PRAGMA user_version = 3;
        """
    )
    connection.commit()
    connection.close()


def _table_columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
