from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_harness.agent import Agent
from mini_harness.events import MemoryEventSink
from mini_harness.memory import (
    MemoryInputError,
    MemoryStore,
    MemoryStoreError,
    render_memory_snapshot,
)
from mini_harness.provider import ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB, SessionError
from mini_harness.tools import ToolRegistry, register_memory_tools


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "tools": json.loads(json.dumps(tools)) if tools else None,
            }
        )
        return self.responses.pop(0)


def _run_slow_memory_update(root, started, release, results) -> None:
    class SlowMemoryStore(MemoryStore):
        def _atomic_write(self, path, content):
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("slow Memory update was not released")
            super()._atomic_write(path, content)

    try:
        update = SlowMemoryStore(root, profile_id="alice").update(
            operation="replace",
            document="memory",
            content="slow update committed",
        )
        results.put(("committed", update.content))
    except BaseException as exc:
        results.put(("failed", type(exc).__name__, str(exc)))


def _crash_while_holding_memory_lock(root, acquired) -> None:
    store = MemoryStore(root, profile_id="alice")
    with store._exclusive_lock():
        acquired.set()
        os._exit(17)


class MemoryTests(unittest.TestCase):
    def test_profile_scoped_crud_is_atomic_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice = MemoryStore(root, profile_id="alice")
            bob = MemoryStore(root, profile_id="bob")

            first = alice.update(
                operation="replace",
                document="user",
                content="Prefers concise answers.",
            )
            second = alice.update(
                operation="append",
                document="user",
                content="Uses PowerShell.",
            )
            alice_snapshot = alice.snapshot()
            bob_snapshot = bob.snapshot()
            temporary_files = list(
                alice.memory_directory.glob(".memory-tmp-*")
            )
            lock_exists = (alice.memory_directory / ".memory.lock").exists()
            deleted = alice.update(
                operation="delete",
                document="user",
            )

        self.assertNotEqual(first.revision, second.revision)
        self.assertEqual(
            alice_snapshot.user,
            "Prefers concise answers.\nUses PowerShell.",
        )
        self.assertTrue(bob_snapshot.empty)
        self.assertEqual(temporary_files, [])
        # The inode is intentionally stable; only the kernel lock is released.
        self.assertTrue(lock_exists)
        self.assertEqual(deleted.content, "")

    def test_oversized_update_preserves_previous_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(
                Path(directory),
                profile_id="alice",
                max_user_chars=256,
            )
            store.update(
                operation="replace",
                document="user",
                content="preserved",
            )

            with self.assertRaises(MemoryInputError):
                store.update(
                    operation="replace",
                    document="user",
                    content="x" * 257,
                )
            content = store.snapshot().user

        self.assertEqual(content, "preserved")

    def test_profile_directory_link_is_rejected_before_memory_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice = MemoryStore(root, profile_id="alice")

            with patch(
                "mini_harness.memory._is_link_or_reparse_point",
                return_value=True,
            ):
                with self.assertRaisesRegex(
                    MemoryStoreError,
                    "must not be symlinks",
                ):
                    alice.snapshot()

    def test_abandoned_lock_file_is_reused_but_live_kernel_lock_times_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(
                Path(directory),
                profile_id="alice",
                lock_timeout_seconds=0.1,
            )
            store.memory_directory.mkdir(parents=True)
            lock = store.memory_directory / ".memory.lock"
            lock.write_text("crashed-owner", encoding="ascii")
            orphan = store.memory_directory / ".memory-tmp-orphan"
            orphan.write_text("partial", encoding="utf-8")
            old = time.time() - 1
            os.utime(lock, (old, old))

            store.update(
                operation="replace",
                document="memory",
                content="recovered",
            )
            self.assertTrue(lock.exists())
            self.assertFalse(orphan.exists())

            with store._exclusive_lock():
                with self.assertRaisesRegex(
                    MemoryStoreError,
                    "Timed out waiting",
                ):
                    store.update(
                        operation="replace",
                        document="memory",
                        content="must-not-overwrite",
                    )
            current = store.snapshot().memory

        self.assertEqual(current, "recovered")

    def test_slow_cross_process_update_cannot_be_mistaken_for_stale_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            started = context.Event()
            release = context.Event()
            results = context.Queue()
            process = context.Process(
                target=_run_slow_memory_update,
                args=(root, started, release, results),
            )
            process.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                # Deliberately keep the first real update alive longer than
                # the old test's 200 ms stale threshold.
                time.sleep(0.30)
                contender = MemoryStore(
                    root,
                    profile_id="alice",
                    lock_timeout_seconds=0.15,
                )
                with self.assertRaisesRegex(
                    MemoryStoreError,
                    "Timed out waiting",
                ):
                    contender.update(
                        operation="replace",
                        document="memory",
                        content="competing update",
                    )
                self.assertTrue(process.is_alive())
            finally:
                release.set()
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

            self.assertEqual(process.exitcode, 0)
            self.assertEqual(
                results.get(timeout=2),
                ("committed", "slow update committed"),
            )
            self.assertEqual(
                MemoryStore(root, profile_id="alice").snapshot().memory,
                "slow update committed",
            )

    def test_process_crash_releases_memory_lock_without_stale_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            acquired = context.Event()
            process = context.Process(
                target=_crash_while_holding_memory_lock,
                args=(root, acquired),
            )
            process.start()
            self.assertTrue(acquired.wait(timeout=5))
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 17)

            update = MemoryStore(
                root,
                profile_id="alice",
                lock_timeout_seconds=0.2,
            ).update(
                operation="replace",
                document="memory",
                content="recovered immediately",
            )

            self.assertEqual(update.content, "recovered immediately")

    def test_memory_tools_require_approval_and_return_updated_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory), profile_id="alice")
            denied_registry = ToolRegistry(
                approval_callback=lambda _definition, _arguments: False
            )
            register_memory_tools(denied_registry, store)
            denied = denied_registry.dispatch(
                "memory_update",
                json.dumps(
                    {
                        "operation": "replace",
                        "document": "memory",
                        "content": "denied",
                    }
                ),
            )

            approved_registry = ToolRegistry(
                approval_callback=lambda _definition, _arguments: True
            )
            register_memory_tools(approved_registry, store)
            approved = approved_registry.dispatch(
                "memory_update",
                json.dumps(
                    {
                        "operation": "replace",
                        "document": "memory",
                        "content": "approved fact",
                    }
                ),
            )
            read_result = approved_registry.dispatch(
                "memory_read",
                '{"document":"memory"}',
            )
            content = store.snapshot().memory

        self.assertFalse(denied.ok)
        self.assertEqual(denied.error_code, "approval_denied")
        self.assertEqual(denied.effect_disposition, "none")
        self.assertTrue(approved.ok)
        self.assertEqual(approved.execution_backend, "spawn_process")
        approved_payload = json.loads(approved.content)["result"]
        self.assertEqual(approved_payload["content"], "approved fact")
        self.assertTrue(read_result.ok)
        self.assertEqual(content, "approved fact")

    def test_memory_snapshot_is_frozen_per_session_and_visible_to_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "profiles", profile_id="alice")
            store.update(
                operation="replace",
                document="user",
                content="old preference",
            )
            db = SessionDB(root / "sessions.db")
            first_prompt = render_memory_snapshot("system", store.snapshot())
            first = Agent(
                RecordingProvider([]),
                system_prompt=first_prompt,
                session_db=db,
                session_id="first",
                profile_id="alice",
            )

            store.update(
                operation="replace",
                document="user",
                content="new preference",
            )
            resumed = Agent(
                RecordingProvider([]),
                system_prompt="ignored",
                session_db=db,
                session_id="first",
                profile_id="alice",
            )
            second_prompt = render_memory_snapshot("system", store.snapshot())
            second = Agent(
                RecordingProvider([]),
                system_prompt=second_prompt,
                session_db=db,
                session_id="second",
                profile_id="alice",
            )
            db.close()

        self.assertIn("old preference", first.messages[0]["content"])
        self.assertIn("old preference", resumed.messages[0]["content"])
        self.assertNotIn("new preference", resumed.messages[0]["content"])
        self.assertIn("new preference", second.messages[0]["content"])

    def test_session_cannot_resume_under_a_different_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            Agent(
                RecordingProvider([]),
                system_prompt="system",
                session_db=db,
                session_id="profile-bound",
                profile_id="alice",
            )
            db.append_message(
                "profile-bound",
                {"role": "user", "content": "unresolved"},
            )
            db.append_assistant_with_prepared_journals(
                "profile-bound",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "must-not-recover",
                            "type": "function",
                            "function": {
                                "name": "memory_update",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            )
            before = db.audit_messages("profile-bound")

            with self.assertRaisesRegex(
                SessionError,
                "Session profile mismatch",
            ):
                Agent(
                    RecordingProvider([]),
                    system_prompt="ignored",
                    session_db=db,
                    session_id="profile-bound",
                    profile_id="bob",
                )
            after = db.audit_messages("profile-bound")
            journal = db.audit_tool_journal("profile-bound")
            db.close()

        self.assertEqual(before, after)
        self.assertEqual(journal[0]["status"], "prepared")
    def test_agent_memory_update_is_journaled_and_events_are_redacted(self) -> None:
        secret = "PRIVATE_LONG_TERM_FACT"
        provider = RecordingProvider(
            [
                ProviderResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="memory-1",
                            name="memory_update",
                            arguments=json.dumps(
                                {
                                    "operation": "replace",
                                    "document": "memory",
                                    "content": secret,
                                }
                            ),
                        ),
                    ),
                    raw={},
                ),
                ProviderResponse(content="saved", tool_calls=(), raw={}),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "profiles", profile_id="alice")
            registry = ToolRegistry(
                approval_callback=lambda _definition, _arguments: True
            )
            register_memory_tools(registry, store)
            db = SessionDB(root / "sessions.db")
            events = MemoryEventSink()
            agent = Agent(
                provider,
                system_prompt="stable system",
                tools=registry,
                session_db=db,
                session_id="memory-audit",
                profile_id="alice",
                event_sink=events,
            )

            self.assertEqual(agent.chat("remember this"), "saved")
            journal = db.audit_tool_journal("memory-audit")
            audit = db.audit_messages("memory-audit")
            event_json = json.dumps(
                [event.as_dict() for event in events.events]
            )
            persisted_memory = store.snapshot().memory
            db.close()

        self.assertEqual(persisted_memory, secret)
        self.assertEqual(journal[0]["tool_name"], "memory_update")
        self.assertEqual(journal[0]["status"], "completed")
        self.assertIn(secret, json.dumps(audit))
        self.assertNotIn(secret, event_json)
        self.assertIn(
            "memory.update_result",
            [event.event for event in events.events],
        )
        second_request = provider.calls[1]["messages"]
        tool_message = next(
            message
            for message in second_request
            if message["role"] == "tool"
        )
        self.assertIn(secret, tool_message["content"])
        self.assertEqual(agent.messages[0]["content"], "stable system")


if __name__ == "__main__":
    unittest.main()
