from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.api_messages import APIMessageBuilder
from mini_harness.context_budget import ContextBudgetPolicy
from mini_harness.events import MemoryEventSink
from mini_harness.provider import ProviderResponse
from mini_harness.session_store import SessionDB
from mini_harness.skills import (
    SkillCatalog,
    SkillNotFoundError,
    SkillTooLargeError,
    render_skills_index,
)


class RecordingProvider:
    def __init__(self) -> None:
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        return ProviderResponse(content="done", tool_calls=(), raw={})


class SkillCatalogTests(unittest.TestCase):
    def test_index_contains_metadata_but_not_skill_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body_marker = "BODY-MUST-NOT-BE-IN-THE-INDEX"
            _write_skill(
                root / "bundled" / "review",
                name="review",
                description="Review a harness carefully.",
                body=body_marker + "\n" + ("details\n" * 2_000),
            )

            catalog = _catalog(root)
            prompt = render_skills_index("base-system", catalog.entries)

            self.assertEqual([entry.name for entry in catalog.entries], ["review"])
            self.assertIn("Review a harness carefully.", prompt)
            self.assertIn("source=bundled", prompt)
            self.assertNotIn(body_marker, prompt)
            self.assertFalse(hasattr(catalog.entries[0], "content"))

    def test_source_precedence_is_external_then_bundled_then_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "profiles" / "alice" / "skills" / "same",
                name="same",
                description="external wins",
                body="external body",
            )
            _write_skill(
                root / "bundled" / "same",
                name="same",
                description="bundled loses",
                body="bundled body",
            )
            _write_skill(
                root / "optional" / "same",
                name="same",
                description="optional loses",
                body="optional body",
            )

            catalog = _catalog(root, enabled_optional=("same",))

            self.assertEqual(catalog.get("same").source, "external")
            self.assertIn("external body", catalog.load("same").content)
            self.assertEqual(len(catalog.conflicts), 2)
            self.assertEqual(
                [conflict.shadowed_source for conflict in catalog.conflicts],
                ["bundled", "optional"],
            )

    def test_optional_skills_are_inactive_until_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "optional" / "heavy",
                name="heavy",
                description="A heavy optional skill.",
                body="optional body",
            )

            disabled = _catalog(root)
            enabled = _catalog(root, enabled_optional=("heavy",))

            self.assertEqual(disabled.entries, ())
            self.assertEqual(enabled.get("heavy").source, "optional")

    def test_load_reads_the_complete_current_skill_without_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "bundled" / "review"
            _write_skill(
                skill_dir,
                name="review",
                description="Initial description.",
                body="first body",
            )
            catalog = _catalog(root)
            frozen_index = render_skills_index("system", catalog.entries)
            updated_body = "line one\nline two\nFINAL-INSTRUCTION"
            _write_skill(
                skill_dir,
                name="review",
                description="Changed after startup.",
                body=updated_body,
            )

            loaded = catalog.load("review")
            invocation = catalog.build_invocation("review", "Inspect this project")

            self.assertIn(updated_body, loaded.content)
            self.assertIn(updated_body, invocation.message)
            self.assertIn("Inspect this project", invocation.message)
            self.assertIn("skill://bundled/review/", invocation.message)
            self.assertNotIn(str(skill_dir.resolve()), invocation.message)
            self.assertFalse(hasattr(invocation, "skill_dir"))
            self.assertNotIn("Changed after startup.", frozen_index)

    def test_all_skill_sources_use_logical_references_without_host_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "configured-external" / "external-skill",
                name="external-skill",
                description="External Skill.",
                body="external body",
            )
            _write_skill(
                root / "bundled" / "bundled-skill",
                name="bundled-skill",
                description="Bundled Skill.",
                body="bundled body",
            )
            _write_skill(
                root / "optional" / "optional-skill",
                name="optional-skill",
                description="Optional Skill.",
                body="optional body",
            )
            catalog = _catalog(
                root,
                enabled_optional=("optional-skill",),
                external_dirs=(root / "configured-external",),
            )

            invocations = {
                name: catalog.build_invocation(name)
                for name in (
                    "external-skill",
                    "bundled-skill",
                    "optional-skill",
                )
            }

        expected = {
            "external-skill": "skill://external/external-skill/",
            "bundled-skill": "skill://bundled/bundled-skill/",
            "optional-skill": "skill://optional/optional-skill/",
        }
        for name, invocation in invocations.items():
            self.assertEqual(invocation.reference, expected[name])
            self.assertIn(expected[name], invocation.message)
            self.assertNotIn(str(root.resolve()), invocation.message)
            self.assertNotIn("Skill directory:", invocation.message)

    def test_missing_and_path_escape_names_fail_before_file_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = _catalog(Path(directory))

            with self.assertRaisesRegex(SkillNotFoundError, "missing"):
                catalog.load("missing")
            with self.assertRaisesRegex(ValueError, "skill name"):
                catalog.load("../outside")

    def test_nested_support_skill_is_not_a_second_active_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "bundled" / "parent",
                name="parent",
                description="Parent skill.",
                body="parent",
            )
            _write_skill(
                root / "bundled" / "parent" / "references" / "archived",
                name="archived",
                description="Must stay support data.",
                body="archived",
            )
            _write_skill(
                root / "bundled" / "invalid",
                name="../invalid",
                description="Must be skipped safely.",
                body="invalid",
            )

            catalog = _catalog(root)

            self.assertEqual([entry.name for entry in catalog.entries], ["parent"])
            self.assertEqual(len(catalog.diagnostics), 1)

    def test_oversized_skill_is_rejected_instead_of_partially_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "bundled" / "large",
                name="large",
                description="Large skill.",
                body="x" * 300,
            )
            catalog = _catalog(root, max_skill_chars=256)

            with self.assertRaisesRegex(SkillTooLargeError, "complete"):
                catalog.load("large")

    def test_skill_invocation_is_a_user_message_and_event_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret_marker = "INSTRUCTION-CONTENT-NOT-FOR-EVENT"
            _write_skill(
                root / "bundled" / "review",
                name="review",
                description="Review.",
                body=secret_marker,
            )
            invocation = _catalog(root).build_invocation(
                "review",
                "Do the review",
            )
            provider = RecordingProvider()
            events = MemoryEventSink()
            agent = Agent(
                provider,
                system_prompt="stable-system",
                event_sink=events,
            )

            self.assertEqual(agent.chat_with_skill(invocation), "done")

            request = provider.calls[0]
            self.assertEqual(
                [message["role"] for message in request["messages"]],
                ["system", "user"],
            )
            self.assertEqual(request["messages"][0]["content"], "stable-system")
            self.assertIn(secret_marker, request["messages"][1]["content"])
            self.assertIn(
                "skill://bundled/review/",
                request["messages"][1]["content"],
            )
            self.assertNotIn(str(root.resolve()), request["messages"][1]["content"])
            self.assertIsNone(request["tools"])
            loaded_event = next(
                event for event in events.events if event.event == "skill.loaded"
            )
            encoded_details = json.dumps(
                loaded_event.details,
                ensure_ascii=False,
            )
            self.assertNotIn(secret_marker, encoded_details)
            self.assertEqual(loaded_event.details["name"], "review")
            self.assertEqual(loaded_event.details["source"], "bundled")
            self.assertEqual(
                loaded_event.details["reference"],
                "skill://bundled/review/",
            )

    def test_index_cost_is_counted_by_the_existing_context_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_skill(
                root / "bundled" / "review",
                name="review",
                description="d" * 180,
                body="body",
            )
            indexed_system = render_skills_index(
                "system",
                _catalog(root).entries,
            )
            policy = ContextBudgetPolicy(
                max_input_tokens=2_000,
                reserved_output_tokens=200,
                approximate_chars_per_token=4,
                max_tool_result_tokens=200,
            )

            builder = APIMessageBuilder(context_budget=policy)
            base = builder.build(
                [{"role": "system", "content": "system"}]
            ).report.context_budget
            indexed = builder.build(
                [{"role": "system", "content": indexed_system}]
            ).report.context_budget

            self.assertGreater(
                indexed.estimated_tokens_before,
                base.estimated_tokens_before,
            )

    def test_skill_update_does_not_rewrite_an_existing_session_system_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "bundled" / "review"
            _write_skill(
                skill_dir,
                name="review",
                description="Version one.",
                body="body one",
            )
            first_prompt = render_skills_index(
                "base-system",
                _catalog(root).entries,
            )
            database = SessionDB(root / "sessions.db")
            try:
                created_provider = RecordingProvider()
                created = Agent(
                    created_provider,
                    system_prompt=first_prompt,
                    session_db=database,
                    session_id="stable-session",
                    profile_id="alice",
                )
                created.chat("first turn")

                _write_skill(
                    skill_dir,
                    name="review",
                    description="Version two.",
                    body="body two",
                )
                rebuilt_prompt = render_skills_index(
                    "base-system",
                    _catalog(root).entries,
                )
                resumed_provider = RecordingProvider()
                resumed = Agent(
                    resumed_provider,
                    system_prompt=rebuilt_prompt,
                    session_db=database,
                    session_id="stable-session",
                    profile_id="alice",
                )
                resumed.chat("second turn")
            finally:
                database.close()

            resumed_system = resumed_provider.calls[0]["messages"][0]["content"]
            self.assertEqual(resumed_system, first_prompt)
            self.assertIn("Version one.", resumed_system)
            self.assertNotIn("Version two.", resumed_system)


def _catalog(
    root: Path,
    *,
    enabled_optional: tuple[str, ...] = (),
    external_dirs: tuple[Path, ...] = (),
    max_skill_chars: int = 40_000,
) -> SkillCatalog:
    return SkillCatalog(
        profile_root=root / "profiles",
        profile_id="alice",
        external_dirs=external_dirs,
        bundled_dir=root / "bundled",
        optional_dir=root / "optional",
        enabled_optional=enabled_optional,
        max_skill_chars=max_skill_chars,
    )


def _write_skill(
    directory: Path,
    *,
    name: str,
    description: str,
    body: str,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
