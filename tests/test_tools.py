from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from mini_harness.execution import ThreadExecutionBackend
from mini_harness.tools import (
    ToolDefinition,
    ToolInputError,
    ToolRegistry,
    build_file_registry,
    build_readonly_file_registry,
)


def _sleep_for_delay(arguments):
    time.sleep(float(arguments["delay"]))


class ToolRegistryTests(unittest.TestCase):
    def test_unknown_tool_and_invalid_json_become_structured_errors(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo arguments.",
                parameters={"type": "object"},
                handler=lambda arguments: arguments,
            )
        )

        unknown = json.loads(registry.dispatch("missing", "{}").content)
        invalid = json.loads(registry.dispatch("echo", "{").content)

        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "unknown_tool")
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "invalid_json")

    def test_readonly_file_tools_list_and_read_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("hello harness", encoding="utf-8")
            (root / "docs").mkdir()
            registry = build_readonly_file_registry(root)

            listed = json.loads(
                registry.dispatch("list_files", '{"path":".","recursive":false}').content
            )
            read = json.loads(
                registry.dispatch("read_file", '{"path":"notes.txt"}').content
            )

        self.assertTrue(listed["ok"])
        self.assertEqual(
            [entry["path"] for entry in listed["result"]["entries"]],
            ["docs", "notes.txt"],
        )
        self.assertTrue(read["ok"])
        self.assertEqual(read["result"]["content"], "hello harness")

    def test_list_files_pages_reconstruct_large_directory_without_gaps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = [f"file-{index:03}.txt" for index in range(250)]
            for name in expected:
                (root / name).touch()
            registry = build_readonly_file_registry(
                root,
                max_result_chars=2_500,
            )
            entries = []
            offset = 0
            revision = None
            page_count = 0

            while True:
                arguments = {
                    "path": ".",
                    "recursive": False,
                    "offset": offset,
                    "limit": 200,
                }
                if revision is not None:
                    arguments["expected_revision"] = revision
                execution = registry.dispatch(
                    "list_files",
                    json.dumps(arguments),
                )
                payload = json.loads(execution.content)

                self.assertTrue(payload["ok"])
                self.assertLessEqual(len(execution.content), 2_500)
                result = payload["result"]
                self.assertIn("entries", result)
                self.assertNotIn("preview", result)
                self.assertEqual(result["offset"], offset)
                self.assertEqual(
                    result["returned_entries"],
                    len(result["entries"]),
                )
                self.assertEqual(result["total_entries"], 250)
                revision = revision or result["revision"]
                self.assertEqual(result["revision"], revision)
                entries.extend(entry["path"] for entry in result["entries"])
                page_count += 1
                if result["eof"]:
                    self.assertIsNone(result["next_offset"])
                    self.assertFalse(result["truncated"])
                    break
                self.assertTrue(result["truncated"])
                self.assertGreater(result["next_offset"], offset)
                offset = result["next_offset"]
                self.assertLess(page_count, 20)

        self.assertGreater(page_count, 1)
        self.assertEqual(entries, expected)
        self.assertEqual(len(entries), len(set(entries)))
        self.assertEqual(entries[-1], "file-249.txt")

    def test_list_files_revision_prevents_mixing_changed_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(205):
                (root / f"file-{index:03}.txt").touch()
            registry = build_readonly_file_registry(root)
            first = json.loads(
                registry.dispatch(
                    "list_files",
                    '{"path":".","offset":0,"limit":100}',
                ).content
            )["result"]
            missing_revision = json.loads(
                registry.dispatch(
                    "list_files",
                    '{"path":".","offset":100,"limit":100}',
                ).content
            )
            (root / "file-050-new.txt").touch()

            continued = json.loads(
                registry.dispatch(
                    "list_files",
                    json.dumps(
                        {
                            "path": ".",
                            "offset": first["next_offset"],
                            "limit": 100,
                            "expected_revision": first["revision"],
                        }
                    ),
                ).content
            )

        self.assertFalse(continued["ok"])
        self.assertEqual(continued["error"]["code"], "tool_input_error")
        self.assertIn("directory changed", continued["error"]["message"])
        self.assertFalse(missing_revision["ok"])
        self.assertEqual(
            missing_revision["error"]["code"],
            "tool_input_error",
        )
        self.assertIn(
            "expected_revision is required",
            missing_revision["error"]["message"],
        )

    def test_read_file_rejects_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = build_readonly_file_registry(Path(directory))

            result = json.loads(
                registry.dispatch("read_file", '{"path":"../outside.txt"}').content
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "tool_input_error")
        self.assertIn("escapes", result["error"]["message"])

    def test_read_file_pages_reconstruct_large_file_without_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "REAL004_TAIL_MARKER"
            original = ("A\"\\\n\t汉字" * 4_000) + marker
            (root / "large.txt").write_text(original, encoding="utf-8")
            registry = build_readonly_file_registry(
                root,
                max_result_chars=12_000,
            )
            pages = []
            offset = 0
            revision = None

            while True:
                arguments = {"path": "large.txt", "offset": offset}
                if revision is not None:
                    arguments["expected_revision"] = revision
                execution = registry.dispatch(
                    "read_file",
                    json.dumps(arguments),
                )
                payload = json.loads(execution.content)

                self.assertTrue(payload["ok"])
                self.assertLessEqual(len(execution.content), 12_000)
                result = payload["result"]
                self.assertIn("content", result)
                self.assertNotIn("preview", result)
                self.assertEqual(result["offset"], offset)
                self.assertEqual(
                    result["returned_chars"],
                    len(result["content"]),
                )
                revision = revision or result["revision"]
                self.assertEqual(result["revision"], revision)
                pages.append(result["content"])
                if result["eof"]:
                    self.assertIsNone(result["next_offset"])
                    self.assertFalse(result["truncated"])
                    break
                self.assertTrue(result["truncated"])
                self.assertGreater(result["next_offset"], offset)
                offset = result["next_offset"]
                self.assertLess(len(pages), 20)

        reconstructed = "".join(pages)
        self.assertEqual(reconstructed, original)
        self.assertTrue(reconstructed.endswith(marker))

    def test_read_file_revision_prevents_mixing_changed_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "changing.txt"
            path.write_text("abcdefghij", encoding="utf-8")
            registry = build_readonly_file_registry(root)
            first = json.loads(
                registry.dispatch(
                    "read_file",
                    '{"path":"changing.txt","limit":5}',
                ).content
            )["result"]
            path.write_text("abcde-CHANGED", encoding="utf-8")

            continued = json.loads(
                registry.dispatch(
                    "read_file",
                    json.dumps(
                        {
                            "path": "changing.txt",
                            "offset": first["next_offset"],
                            "expected_revision": first["revision"],
                        }
                    ),
                ).content
            )

        self.assertFalse(continued["ok"])
        self.assertEqual(continued["error"]["code"], "tool_input_error")
        self.assertIn("file changed", continued["error"]["message"])

    def test_read_file_validates_page_bounds_and_exact_eof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "short.txt").write_text("hello", encoding="utf-8")
            registry = build_readonly_file_registry(root)

            negative = json.loads(
                registry.dispatch(
                    "read_file",
                    '{"path":"short.txt","offset":-1}',
                ).content
            )
            too_far = json.loads(
                registry.dispatch(
                    "read_file",
                    '{"path":"short.txt","offset":6}',
                ).content
            )
            eof = json.loads(
                registry.dispatch(
                    "read_file",
                    '{"path":"short.txt","offset":5}',
                ).content
            )["result"]

        self.assertEqual(negative["error"]["code"], "schema_validation")
        self.assertEqual(too_far["error"]["code"], "tool_input_error")
        self.assertEqual(eof["content"], "")
        self.assertEqual(eof["returned_chars"], 0)
        self.assertIsNone(eof["next_offset"])
        self.assertTrue(eof["eof"])

    def test_schema_validation_blocks_handler_execution(self) -> None:
        handled = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="count",
                description="Accept one integer.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(arguments["value"]),
            )
        )

        wrong_type = json.loads(
            registry.dispatch("count", '{"value":"not-an-integer"}').content
        )
        extra_field = json.loads(
            registry.dispatch("count", '{"value":1,"unexpected":true}').content
        )

        self.assertEqual(handled, [])
        self.assertEqual(wrong_type["error"]["code"], "schema_validation")
        self.assertIn("arguments.value", wrong_type["error"]["message"])
        self.assertEqual(extra_field["error"]["code"], "schema_validation")

    def test_preflight_blocks_before_approval_and_handler(self) -> None:
        approved = []
        handled = []
        registry = ToolRegistry(
            approval_callback=lambda _definition, arguments: approved.append(arguments) or True
        )
        registry.register(
            ToolDefinition(
                name="guarded",
                description="Reject unsafe arguments before approval.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(arguments),
                preflight=lambda _arguments: (_ for _ in ()).throw(
                    ToolInputError("unsafe path")
                ),
                requires_approval=True,
            )
        )

        result = registry.dispatch("guarded", '{"path":"../outside.txt"}')
        payload = json.loads(result.content)

        self.assertEqual(approved, [])
        self.assertEqual(handled, [])
        self.assertEqual(payload["error"]["code"], "tool_input_error")
        self.assertEqual(result.effect_disposition, "none")
        self.assertEqual(result.execution_phase, "rejected_before_execution")

    def test_preflight_normalizes_arguments_before_approval_and_handler(self) -> None:
        approved = []
        handled = []
        registry = ToolRegistry(
            approval_callback=lambda _definition, arguments: approved.append(dict(arguments))
            or True,
            execution_backend=ThreadExecutionBackend(),
        )
        registry.register(
            ToolDefinition(
                name="normalized",
                description="Normalize arguments before execution.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(dict(arguments)) or arguments,
                preflight=lambda arguments: {"path": arguments["path"].replace("nested/../", "")},
                requires_approval=True,
            )
        )

        result = registry.dispatch("normalized", '{"path":"nested/../note.txt"}')

        self.assertTrue(result.ok)
        self.assertEqual(approved, [{"path": "note.txt"}])
        self.assertEqual(handled, [{"path": "note.txt"}])
        self.assertEqual(result.execution_phase, "handler_completed")

    def test_invalid_preflight_output_fails_closed(self) -> None:
        handled = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="broken_preflight",
                description="Return an invalid preflight value.",
                parameters={"type": "object"},
                handler=lambda arguments: handled.append(arguments),
                preflight=lambda _arguments: "not-an-object",
            )
        )

        result = registry.dispatch("broken_preflight", "{}")
        payload = json.loads(result.content)

        self.assertEqual(handled, [])
        self.assertEqual(payload["error"]["code"], "preflight_error")
        self.assertEqual(result.effect_disposition, "none")

    def test_preflight_output_is_revalidated_against_tool_schema(self) -> None:
        handled = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="schema_preserved",
                description="Preflight cannot bypass the public schema.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(arguments),
                preflight=lambda _arguments: {},
            )
        )

        result = registry.dispatch("schema_preserved", '{"path":"note.txt"}')
        payload = json.loads(result.content)

        self.assertEqual(handled, [])
        self.assertEqual(payload["error"]["code"], "preflight_schema_validation")
        self.assertEqual(result.execution_phase, "rejected_before_execution")

    def test_conservative_name_repair_uses_only_registered_tools(self) -> None:
        registry = ToolRegistry(
            name_repair_threshold=0.84,
            execution_backend=ThreadExecutionBackend(),
        )
        registry.register(
            ToolDefinition(
                name="read_file",
                description="Read a file.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: arguments["path"],
            )
        )

        repaired = registry.dispatch("read_flie", '{"path":"README.md"}')
        unknown = json.loads(registry.dispatch("delete_everything", "{}").content)
        repaired_payload = json.loads(repaired.content)

        self.assertTrue(repaired.ok)
        self.assertTrue(repaired.name_repaired)
        self.assertEqual(repaired.executed_name, "read_file")
        self.assertEqual(repaired_payload["meta"]["requested_tool"], "read_flie")
        self.assertEqual(repaired_payload["meta"]["executed_tool"], "read_file")
        self.assertEqual(unknown["error"]["code"], "unknown_tool")

    def test_invalid_tool_schema_is_rejected_at_registration(self) -> None:
        registry = ToolRegistry()

        with self.assertRaisesRegex(ValueError, "Invalid JSON Schema"):
            registry.register(
                ToolDefinition(
                    name="broken",
                    description="Broken schema.",
                    parameters={"type": "definitely-not-a-json-schema-type"},
                    handler=lambda arguments: arguments,
                )
            )

    def test_name_repair_refuses_ambiguous_candidates(self) -> None:
        registry = ToolRegistry(name_repair_threshold=0.84, name_repair_margin=0.08)
        for name in ("read_file", "read_files"):
            registry.register(
                ToolDefinition(
                    name=name,
                    description="Test ambiguity.",
                    parameters={"type": "object"},
                    handler=lambda arguments: arguments,
                )
            )

        result = json.loads(registry.dispatch("read_fil", "{}").content)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_tool")

    def test_write_file_requires_approval_and_denial_has_no_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_file_registry(
                root,
                enable_write_file=True,
                approval_callback=lambda _definition, _arguments: False,
            )

            result = registry.dispatch(
                "write_file",
                '{"path":"denied.txt","content":"must not exist"}',
            )
            payload = json.loads(result.content)

            self.assertFalse((root / "denied.txt").exists())
            self.assertEqual(payload["error"]["code"], "approval_denied")
            self.assertEqual(result.effect_disposition, "none")

    def test_write_scope_rejection_happens_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approval_calls = []
            registry = build_file_registry(
                root,
                enable_write_file=True,
                approval_callback=lambda _definition, arguments: approval_calls.append(arguments)
                or True,
            )

            result = registry.dispatch(
                "write_file",
                '{"path":"../outside.txt","content":"must not exist"}',
            )
            payload = json.loads(result.content)

            self.assertEqual(approval_calls, [])
            self.assertFalse((root.parent / "outside.txt").exists())
            self.assertEqual(payload["error"]["code"], "tool_input_error")
            self.assertEqual(result.effect_disposition, "none")
            self.assertEqual(result.execution_phase, "rejected_before_execution")

    def test_write_preflight_normalizes_path_shown_to_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved_arguments = []
            registry = build_file_registry(
                root,
                enable_write_file=True,
                approval_callback=lambda _definition, arguments: approved_arguments.append(
                    dict(arguments)
                )
                or True,
            )

            result = registry.dispatch(
                "write_file",
                '{"path":"nested/../note.txt","content":"complete"}',
            )

            self.assertTrue(result.ok)
            self.assertEqual(
                approved_arguments,
                [{"path": "note.txt", "content": "complete"}],
            )
            self.assertEqual(
                (root / "note.txt").read_text(encoding="utf-8"),
                "complete",
            )

    def test_approved_write_atomically_creates_and_replaces_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_file_registry(
                root,
                enable_write_file=True,
                approval_callback=lambda _definition, _arguments: True,
            )

            created = registry.dispatch(
                "write_file",
                '{"path":"nested/note.txt","content":"first complete value"}',
            )
            replaced = registry.dispatch(
                "write_file",
                '{"path":"nested/note.txt","content":"second complete value"}',
            )

            self.assertTrue(created.ok)
            self.assertTrue(replaced.ok)
            self.assertEqual(
                (root / "nested" / "note.txt").read_text(encoding="utf-8"),
                "second complete value",
            )
            self.assertEqual(
                list((root / "nested").glob(".mini-harness-tmp-*")),
                [],
            )

    def test_approval_callback_failure_is_closed(self) -> None:
        def fail_approval(_definition, _arguments):
            raise RuntimeError("approval UI unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_file_registry(
                root,
                enable_write_file=True,
                approval_callback=fail_approval,
            )
            result = registry.dispatch(
                "write_file",
                '{"path":"note.txt","content":"blocked"}',
            )
            payload = json.loads(result.content)

            self.assertFalse((root / "note.txt").exists())
            self.assertEqual(payload["error"]["code"], "approval_error")
            self.assertEqual(result.effect_disposition, "none")

    def test_large_result_is_bounded_but_remains_valid_json(self) -> None:
        registry = ToolRegistry(
            max_result_chars=300,
            execution_backend=ThreadExecutionBackend(),
        )
        registry.register(
            ToolDefinition(
                name="large",
                description="Return a large value.",
                parameters={"type": "object"},
                handler=lambda _arguments: "x" * 5_000,
            )
        )

        result = registry.dispatch("large", "{}")
        payload = json.loads(result.content)

        self.assertLessEqual(len(result.content), 300)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["truncated"])
        self.assertEqual(payload["meta"]["effect_disposition"], "completed")
        self.assertEqual(payload["meta"]["execution_phase"], "handler_completed")
        self.assertEqual(payload["meta"]["requested_tool"], "large")
        self.assertEqual(payload["meta"]["executed_tool"], "large")
        self.assertFalse(payload["meta"]["name_repaired"])

    def test_timeout_reports_unknown_effect_instead_of_retrying(self) -> None:
        registry = ToolRegistry(timeout_seconds=0.01)
        registry.register(
            ToolDefinition(
                name="slow",
                description="Sleep longer than the timeout.",
                parameters={
                    "type": "object",
                    "properties": {"delay": {"type": "number"}},
                    "required": ["delay"],
                },
                handler=_sleep_for_delay,
            )
        )

        result = registry.dispatch("slow", '{"delay":0.05}')
        payload = json.loads(result.content)

        self.assertFalse(result.ok)
        self.assertEqual(payload["error"]["code"], "tool_timeout")
        self.assertEqual(result.effect_disposition, "unknown")
        self.assertEqual(payload["meta"]["execution_phase"], "result_unavailable")


if __name__ == "__main__":
    unittest.main()
