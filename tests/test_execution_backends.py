from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.execution import (
    SpawnProcessExecutionBackend,
    ThreadExecutionBackend,
)
from mini_harness.provider import ProviderResponse, ToolCall
from mini_harness.session_store import SessionDB
from mini_harness.tools import ToolDefinition, ToolRegistry


def _return_from_process(arguments):
    return {"received": arguments["value"]}


def _write_after_delay(arguments):
    started = Path(arguments["started"])
    late = Path(arguments["late"])
    started.write_text("started", encoding="utf-8")
    time.sleep(float(arguments["delay"]))
    late.write_text("late-effect", encoding="utf-8")
    return {"finished": True}


def _sleep_then_return(arguments):
    time.sleep(float(arguments["delay"]))
    return {"finished": True}


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, _messages, *, tools=None):
        return self.responses.pop(0)


class ExecutionBackendTests(unittest.TestCase):
    def test_spawn_backend_returns_picklable_handler_result(self) -> None:
        backend = SpawnProcessExecutionBackend()
        started_calls = []

        outcome = backend.execute(
            _return_from_process,
            {"value": 7},
            timeout_seconds=2,
            on_started=lambda: started_calls.append("running"),
        )

        self.assertEqual(started_calls, ["running"])
        self.assertEqual(outcome.kind, "success")
        self.assertEqual(outcome.value, {"received": 7})
        self.assertTrue(outcome.handler_started)
        self.assertFalse(outcome.hard_terminated)

    def test_spawn_timeout_hard_terminates_before_late_effect(self) -> None:
        backend = SpawnProcessExecutionBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started.txt"
            late = root / "late.txt"

            outcome = backend.execute(
                _write_after_delay,
                {
                    "started": str(started),
                    "late": str(late),
                    "delay": 0.5,
                },
                timeout_seconds=0.1,
                on_started=lambda: None,
            )
            time.sleep(0.55)

            self.assertTrue(started.exists())
            self.assertFalse(late.exists())

        self.assertEqual(outcome.kind, "timeout")
        self.assertTrue(outcome.handler_started)
        self.assertTrue(outcome.hard_terminated)

    def test_spawn_start_gate_failure_prevents_handler_entry(self) -> None:
        backend = SpawnProcessExecutionBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started.txt"
            late = root / "late.txt"

            def fail_running_transition():
                raise RuntimeError("journal unavailable")

            outcome = backend.execute(
                _write_after_delay,
                {
                    "started": str(started),
                    "late": str(late),
                    "delay": 0,
                },
                timeout_seconds=1,
                on_started=fail_running_transition,
            )
            time.sleep(0.1)

            self.assertFalse(started.exists())
            self.assertFalse(late.exists())

        self.assertEqual(outcome.kind, "start_error")
        self.assertFalse(outcome.handler_started)
        self.assertTrue(outcome.hard_terminated)

    def test_registry_reports_hard_timeout_without_claiming_no_side_effect(self) -> None:
        registry = ToolRegistry(timeout_seconds=0.1)
        registry.register(
            ToolDefinition(
                name="delayed_effect",
                description="Start, then write later.",
                parameters={
                    "type": "object",
                    "properties": {
                        "started": {"type": "string"},
                        "late": {"type": "string"},
                        "delay": {"type": "number"},
                    },
                    "required": ["started", "late", "delay"],
                },
                handler=_write_after_delay,
                execution_backend=SpawnProcessExecutionBackend(),
                failure_effect_disposition="unknown",
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = registry.dispatch(
                "delayed_effect",
                json.dumps(
                    {
                        "started": str(root / "started.txt"),
                        "late": str(root / "late.txt"),
                        "delay": 0.5,
                    }
                ),
            )
            payload = json.loads(result.content)
            time.sleep(0.55)

            self.assertTrue((root / "started.txt").exists())
            self.assertFalse((root / "late.txt").exists())

        self.assertEqual(payload["error"]["code"], "tool_timeout")
        self.assertEqual(payload["meta"]["effect_disposition"], "unknown")
        self.assertTrue(payload["meta"]["hard_terminated"])
        self.assertTrue(result.hard_terminated)

    def test_default_registry_hard_terminates_before_late_effect(self) -> None:
        registry = ToolRegistry(timeout_seconds=0.1)
        registry.register(
            ToolDefinition(
                name="default_delayed_effect",
                description="Start, then write later.",
                parameters={
                    "type": "object",
                    "properties": {
                        "started": {"type": "string"},
                        "late": {"type": "string"},
                        "delay": {"type": "number"},
                    },
                    "required": ["started", "late", "delay"],
                },
                handler=_write_after_delay,
                failure_effect_disposition="unknown",
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = registry.dispatch(
                "default_delayed_effect",
                json.dumps(
                    {
                        "started": str(root / "started.txt"),
                        "late": str(root / "late.txt"),
                        "delay": 0.5,
                    }
                ),
            )
            payload = json.loads(result.content)
            exists_at_return = (root / "late.txt").exists()
            time.sleep(0.55)
            exists_later = (root / "late.txt").exists()

        self.assertEqual(payload["error"]["code"], "tool_timeout")
        self.assertEqual(payload["meta"]["execution_backend"], "spawn_process")
        self.assertTrue(payload["meta"]["hard_terminated"])
        self.assertFalse(exists_at_return)
        self.assertFalse(exists_later)

    def test_default_registry_rejects_unserializable_handler_before_entry(
        self,
    ) -> None:
        entered = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="closure",
                description="Unserializable closure.",
                parameters={"type": "object"},
                handler=lambda _arguments: entered.append(True),
            )
        )

        result = registry.dispatch("closure", "{}")
        payload = json.loads(result.content)

        self.assertEqual(payload["error"]["code"], "execution_start_error")
        self.assertIn("HandlerNotSerializable", payload["error"]["message"])
        self.assertEqual(entered, [])
        self.assertFalse(result.hard_terminated)

    def test_hard_timeout_can_prove_no_effect_for_declared_pure_handler(self) -> None:
        registry = ToolRegistry(timeout_seconds=0.1)
        registry.register(
            ToolDefinition(
                name="slow_pure",
                description="Pure computation.",
                parameters={
                    "type": "object",
                    "properties": {"delay": {"type": "number"}},
                    "required": ["delay"],
                },
                handler=_sleep_then_return,
                execution_backend=SpawnProcessExecutionBackend(),
                failure_effect_disposition="none",
            )
        )

        result = registry.dispatch("slow_pure", '{"delay":0.5}')
        payload = json.loads(result.content)

        self.assertEqual(payload["error"]["code"], "tool_timeout")
        self.assertEqual(payload["meta"]["effect_disposition"], "none")
        self.assertTrue(payload["meta"]["hard_terminated"])

    def test_hard_timeout_metadata_reaches_execution_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = RecordingProvider(
                [
                    ProviderResponse(
                        content=None,
                        tool_calls=(
                            ToolCall(
                                id="hard-timeout",
                                name="delayed_effect",
                                arguments=json.dumps(
                                    {
                                        "started": str(root / "started.txt"),
                                        "late": str(root / "late.txt"),
                                        "delay": 0.5,
                                    }
                                ),
                            ),
                        ),
                        raw={},
                    ),
                    ProviderResponse(
                        content="timeout observed",
                        tool_calls=(),
                        raw={},
                    ),
                ]
            )
            registry = ToolRegistry(timeout_seconds=0.1)
            registry.register(
                ToolDefinition(
                    name="delayed_effect",
                    description="Write after delay.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "started": {"type": "string"},
                            "late": {"type": "string"},
                            "delay": {"type": "number"},
                        },
                        "required": ["started", "late", "delay"],
                    },
                    handler=_write_after_delay,
                    failure_effect_disposition="unknown",
                )
            )
            db = SessionDB(root / "sessions.db")
            agent = Agent(
                provider,
                system_prompt="system",
                tools=registry,
                session_db=db,
                session_id="hard-timeout-journal",
            )

            self.assertEqual(agent.chat("run"), "timeout observed")
            row = db.audit_tool_journal("hard-timeout-journal")[0]
            time.sleep(0.55)
            late_exists = (root / "late.txt").exists()
            db.close()

        self.assertFalse(late_exists)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["execution_backend"], "spawn_process")
        self.assertEqual(row["hard_terminated"], 1)
        self.assertEqual(row["error_code"], "tool_timeout")

    def test_thread_backend_remains_available_for_unpicklable_handlers(self) -> None:
        backend = ThreadExecutionBackend()
        prefix = "closure"

        outcome = backend.execute(
            lambda arguments: f"{prefix}:{arguments['value']}",
            {"value": 3},
            timeout_seconds=1,
            on_started=lambda: None,
        )

        self.assertEqual(outcome.kind, "success")
        self.assertEqual(outcome.value, "closure:3")
        self.assertFalse(outcome.hard_terminated)

    def test_thread_backend_waits_for_completion_instead_of_false_timeout(
        self,
    ) -> None:
        backend = ThreadExecutionBackend()
        effects = []

        started = time.monotonic()

        def complete_late(_arguments):
            time.sleep(0.15)
            effects.append("completed")
            return {"finished": True}

        outcome = backend.execute(
            complete_late,
            {},
            timeout_seconds=0.01,
            on_started=lambda: None,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(outcome.kind, "success")
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertEqual(effects, ["completed"])


if __name__ == "__main__":
    unittest.main()
