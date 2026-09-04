from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_harness.config import ConfigError, load_config


VALID_CONFIG = """\
provider:
  base_url: https://example.test/v1/
  model: test-model
  api_key_env: TEST_API_KEY
  timeout_seconds: 5
agent:
  system_prompt: Be helpful.
"""


class ConfigTests(unittest.TestCase):
    def test_loads_behavior_from_yaml_and_secret_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(VALID_CONFIG, encoding="utf-8")

            config = load_config(path, environ={"TEST_API_KEY": "secret"})

        self.assertEqual(config.provider.base_url, "https://example.test/v1")
        self.assertEqual(config.provider.model, "test-model")
        self.assertEqual(config.provider.api_key, "secret")
        self.assertEqual(config.provider.timeout_seconds, 5)
        self.assertEqual(config.provider.max_attempts, 3)
        self.assertEqual(config.provider.retry_backoff_seconds, 0.5)
        self.assertEqual(config.agent.max_iterations, 8)
        self.assertEqual(config.agent.workspace_root, Path(directory).resolve())
        self.assertEqual(config.profile.id, "default")
        self.assertTrue(config.context.enabled)
        self.assertEqual(config.context.max_input_tokens, 64_000)
        self.assertEqual(config.context.reserved_output_tokens, 4_000)
        self.assertEqual(config.context.approximate_chars_per_token, 4.0)
        self.assertEqual(config.context.max_tool_result_tokens, 2_000)
        self.assertEqual(config.context.recent_tool_tail_tokens, 12_000)
        self.assertEqual(config.context.min_recent_turns, 1)
        self.assertEqual(config.context.compaction_target_ratio, 0.8)
        self.assertTrue(config.memory.enabled)
        self.assertEqual(
            config.memory.root_path,
            (Path(directory) / ".mini-harness" / "profiles").resolve(),
        )
        self.assertEqual(config.memory.max_user_chars, 4_000)
        self.assertEqual(config.memory.max_memory_chars, 8_000)
        self.assertEqual(config.memory.lock_timeout_seconds, 5)
        self.assertTrue(config.skills.enabled)
        self.assertEqual(
            config.skills.profile_root_path,
            (Path(directory) / ".mini-harness" / "profiles").resolve(),
        )
        self.assertEqual(config.skills.external_dirs, ())
        self.assertEqual(
            config.skills.bundled_dir,
            (Path(directory) / "skills").resolve(),
        )
        self.assertEqual(
            config.skills.optional_dir,
            (Path(directory) / "optional-skills").resolve(),
        )
        self.assertEqual(config.skills.enabled_optional, ())
        self.assertEqual(config.skills.max_skill_chars, 40_000)
        self.assertEqual(config.skills.max_index_description_chars, 240)
        self.assertTrue(config.compression.enabled)
        self.assertEqual(config.compression.threshold_ratio, 0.75)
        self.assertEqual(config.compression.target_ratio, 0.20)
        self.assertEqual(config.compression.protect_first_turns, 1)
        self.assertEqual(config.compression.tail_tokens, 12_000)
        self.assertEqual(config.compression.summary_model, "")
        self.assertTrue(config.compression.abort_on_summary_failure)
        self.assertEqual(config.compression.cooldown_seconds, 600)
        self.assertEqual(config.compression.lock_ttl_seconds, 120)
        self.assertEqual(config.compression.min_savings_ratio, 0.10)
        self.assertEqual(config.compression.anti_thrashing_limit, 2)
        self.assertTrue(config.compression.in_place)
        self.assertTrue(config.tools.auto_repair_names)
        self.assertFalse(config.tools.enable_write_file)
        self.assertEqual(config.tools.max_result_chars, 12_000)
        self.assertEqual(config.tools.name_repair_threshold, 0.84)
        self.assertEqual(config.tools.same_call_limit, 2)
        self.assertEqual(config.tools.max_calls_per_batch, 6)
        self.assertEqual(config.tools.max_calls_per_turn, 16)
        self.assertEqual(config.tools.timeout_seconds, 30)
        self.assertTrue(config.storage.enabled)
        self.assertEqual(
            config.storage.database_path,
            (Path(directory) / ".mini-harness" / "sessions.db").resolve(),
        )
        self.assertTrue(config.observability.enabled)
        self.assertEqual(
            config.observability.event_log_path,
            (Path(directory) / ".mini-harness" / "events.jsonl").resolve(),
        )

    def test_rejects_missing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(VALID_CONFIG, encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Missing API key"):
                load_config(path, environ={})

    def test_compression_can_be_explicitly_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                VALID_CONFIG
                + """
compression:
  enabled: false
""",
                encoding="utf-8",
            )

            config = load_config(path, environ={"TEST_API_KEY": "secret"})

        self.assertFalse(config.compression.enabled)

    def test_rejects_output_reserve_that_consumes_entire_context(self) -> None:
        invalid = (
            VALID_CONFIG
            + """
context:
  max_input_tokens: 1000
  reserved_output_tokens: 1000
"""
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigError,
                "reserved_output_tokens must be less",
            ):
                load_config(path, environ={"TEST_API_KEY": "secret"})

    def test_rejects_profile_id_that_could_escape_its_directory(self) -> None:
        invalid = (
            VALID_CONFIG
            + """
profile:
  id: ../other-user
"""
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "profile_id must be"):
                load_config(path, environ={"TEST_API_KEY": "secret"})


if __name__ == "__main__":
    unittest.main()
