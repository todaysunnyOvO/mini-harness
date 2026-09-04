from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mini_harness.agent import Agent
from mini_harness.context_compression import (
    CompressionManager,
    CompressionPolicy,
    CompressionResult,
)
from mini_harness.cli import _print_compression_result
from mini_harness.config import load_config
from mini_harness.context_budget import (
    ContextBudgetPolicy,
    estimate_message_tokens,
)
from mini_harness.provider import ProviderResponse
from mini_harness.session_store import (
    CompressionLeaseError,
    SessionDB,
    SessionError,
)


class FakeSummaryProvider:
    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def summarize(self, messages, *, max_output_tokens):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "max_output_tokens": max_output_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class FakeAgentProvider:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages, *, tools=None):
        self.calls.append([dict(message) for message in messages])
        return ProviderResponse(
            content=self.answers.pop(0),
            tool_calls=(),
            raw={},
        )


class RecordingSummaryProvider:
    def __init__(self) -> None:
        self.calls = []

    def summarize(self, messages, *, max_output_tokens):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "max_output_tokens": max_output_tokens,
            }
        )
        return f"summary-call-{len(self.calls)}"


class UnboundedBudgetCompressionManager(CompressionManager):
    """Test double proving the commit gate is independent of generation."""

    def _summary_token_budget(self, *args, **kwargs):
        return self.policy.max_output_tokens


class ContextCompressionTests(unittest.TestCase):
    def test_explicitly_disabled_auto_compression_reports_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="disabled")
                _append_turns(db, "disabled", count=8)
                provider = RecordingSummaryProvider()
                result = CompressionManager(
                    db,
                    provider,
                    policy=_policy(enabled=False),
                ).compress(
                    "disabled",
                    system_prompt="system",
                    tools=None,
                    force=False,
                )
            finally:
                db.close()

        self.assertEqual(result.status, "disabled")
        self.assertEqual(provider.calls, [])

    def test_repository_default_config_auto_compresses_real_long_session(
        self,
    ) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        config = load_config(
            config_path,
            environ={"MINI_HARNESS_API_KEY": "test-only"},
        )
        self.assertTrue(config.compression.enabled)

        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session(
                    config.agent.system_prompt,
                    session_id="default-auto-compression",
                    profile_id=config.profile.id,
                )
                for index in range(120):
                    db.append_message(
                        "default-auto-compression",
                        {
                            "role": "user",
                            "content": f"REAL013_USER_{index}-" + "u" * 1_000,
                        },
                    )
                    db.append_message(
                        "default-auto-compression",
                        {
                            "role": "assistant",
                            "content": (
                                f"REAL013_ASSISTANT_{index}-" + "a" * 1_000
                            ),
                        },
                    )
                active_before = len(
                    db.active_message_records("default-auto-compression")
                )
                summary_provider = RecordingSummaryProvider()
                manager = CompressionManager(
                    db,
                    summary_provider,
                    policy=_policy_from_config(config),
                )
                primary = FakeAgentProvider(["continued after default compression"])
                agent = Agent(
                    primary,
                    system_prompt="ignored on resume",
                    session_db=db,
                    session_id="default-auto-compression",
                    profile_id=config.profile.id,
                    compression_manager=manager,
                )

                answer = agent.chat("latest real request")

                active_after = db.active_message_records(
                    "default-auto-compression"
                )
                runs = db.audit_compression_runs(
                    "default-auto-compression"
                )
            finally:
                db.close()

        self.assertEqual(answer, "continued after default compression")
        self.assertGreater(len(summary_provider.calls), 0)
        self.assertEqual([run["status"] for run in runs], ["committed"])
        self.assertLess(len(active_after), active_before)
        self.assertEqual(
            sum(
                row["source"] == "compression_summary"
                for row in active_after
            ),
            1,
        )
        self.assertTrue(
            any(
                "[CONTEXT COMPACTION" in str(message.get("content"))
                for message in primary.calls[0]
            )
        )
        self.assertEqual(
            primary.calls[0][-1]["content"],
            "latest real request",
        )

    def test_rejected_compression_prints_truthful_negative_savings(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            _print_compression_result(
                CompressionResult(
                    "rejected",
                    trigger_tokens=338,
                    estimated_after_tokens=1_621,
                    error_type="InsufficientCompressionSavings",
                )
            )

        rendered = output.getvalue()
        self.assertIn("status=rejected", rendered)
        self.assertIn("saved≈-1283", rendered)
        self.assertIn("error=InsufficientCompressionSavings", rendered)

    def test_v4_database_migrates_to_v5_without_losing_session_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            initial = SessionDB(path)
            initial.create_session("system", session_id="legacy-v4")
            initial.append_message(
                "legacy-v4",
                {"role": "user", "content": "preserve me"},
            )
            initial.close()
            raw = sqlite3.connect(path)
            raw.execute("DROP TABLE compression_state")
            raw.execute("DROP TABLE compression_runs")
            raw.execute("PRAGMA user_version = 4")
            raw.commit()
            raw.close()

            migrated = SessionDB(path)
            try:
                self.assertEqual(migrated.schema_version, 5)
                self.assertTrue(migrated.session_exists("legacy-v4"))
                self.assertIn(
                    "preserve me",
                    json.dumps(migrated.audit_messages("legacy-v4")),
                )
                self.assertEqual(
                    migrated.audit_compression_runs("legacy-v4"),
                    (),
                )
            finally:
                migrated.close()

    def test_compression_soft_archives_middle_and_resume_reuses_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("stable system", session_id="compress")
                _append_turns(db, "compress", count=6)
                original = db.audit_messages("compress")
                provider = FakeSummaryProvider(
                    [
                        "## Historical Goals\nReview the harness.\n"
                        "## Completed Actions and Evidence\nRead files 1-4."
                    ]
                )
                manager = CompressionManager(
                    db,
                    provider,
                    policy=_policy(),
                )

                result = manager.compress(
                    "compress",
                    system_prompt="stable system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(result.status, "committed")
                self.assertEqual(len(provider.calls), 1)
                active = db.audit_messages(
                    "compress",
                    include_inactive=False,
                )
                all_rows = db.audit_messages("compress")
                self.assertTrue(all(row["active"] == 0 for row in all_rows[:12]))
                self.assertGreater(len(all_rows), len(original))
                roles = [row["message"]["role"] for row in active]
                self.assertEqual(
                    roles,
                    ["user", "assistant", "user", "assistant", "user", "assistant"],
                )
                summary = next(
                    row["message"]["content"]
                    for row in active
                    if row["source"] == "compression_summary"
                )
                self.assertIn(
                    "[CONTEXT COMPACTION — REFERENCE ONLY]",
                    summary,
                )
                self.assertIn("--- END OF CONTEXT SUMMARY —", summary)
                self.assertEqual(
                    active[-2]["message"]["content"],
                    "user-5-" + "u" * 200,
                )
                runs = db.audit_compression_runs("compress")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["status"], "committed")

                _session, resumed = db.resume_session("compress")
                self.assertFalse(resumed.changed)
                resumed_summary = next(
                    message["content"]
                    for message in resumed.messages
                    if "[CONTEXT COMPACTION" in str(message.get("content"))
                )
                self.assertEqual(resumed_summary, summary)
                self.assertEqual(len(provider.calls), 1)
            finally:
                db.close()

    def test_summary_failure_keeps_active_history_and_enters_cooldown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="failure")
                _append_turns(db, "failure", count=5)
                before = db.audit_messages("failure", include_inactive=False)
                provider = FakeSummaryProvider(error=RuntimeError("offline"))
                manager = CompressionManager(
                    db,
                    provider,
                    policy=_policy(abort_on_summary_failure=True),
                )

                failed = manager.compress(
                    "failure",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )
                auto_retry = manager.compress(
                    "failure",
                    system_prompt="system",
                    tools=None,
                    force=False,
                )

                self.assertEqual(failed.status, "failed")
                self.assertEqual(auto_retry.status, "cooldown")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    db.audit_messages("failure", include_inactive=False),
                    before,
                )
                runs = db.audit_compression_runs("failure")
                self.assertEqual(runs[-1]["status"], "failed")
                state = db.get_compression_state("failure")
                self.assertGreater(state["cooldown_until"], 0)
            finally:
                db.close()

    def test_compression_transaction_failure_rolls_back_all_active_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            db = SessionDB(path)
            try:
                db.create_session("system", session_id="rollback")
                _append_turns(db, "rollback", count=5)
                before = db.audit_messages("rollback", include_inactive=False)
                db.close()
                raw = sqlite3.connect(path)
                raw.execute(
                    """
                    CREATE TRIGGER fail_committed_compression
                    BEFORE INSERT ON compression_runs
                    WHEN NEW.status = 'committed'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected compression failure');
                    END
                    """
                )
                raw.commit()
                raw.close()
                db = SessionDB(path)
                manager = CompressionManager(
                    db,
                    FakeSummaryProvider(["valid summary"]),
                    policy=_policy(),
                )

                result = manager.compress(
                    "rollback",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    db.audit_messages("rollback", include_inactive=False),
                    before,
                )
                self.assertTrue(
                    all(row["active"] == 1 for row in before)
                )
                self.assertEqual(
                    db.audit_compression_runs("rollback")[-1]["status"],
                    "failed",
                )
                self.assertGreater(
                    db.get_compression_state("rollback")["cooldown_until"],
                    0,
                )
            finally:
                db.close()

    def test_summary_input_and_output_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="redaction")
                _append_turns(db, "redaction", count=5)
                db.append_message(
                    "redaction",
                    {
                        "role": "user",
                        "content": (
                            "api_key=TOP_SECRET_INPUT "
                            "sk-proj-UNLABELED_SECRET_ABC123"
                        ),
                    },
                )
                db.append_message(
                    "redaction",
                    {
                        "role": "assistant",
                        "content": "noted",
                        "reasoning": "secret=HIDDEN_REASONING",
                    },
                )
                _append_turns(db, "redaction", count=3, start=6)
                provider = FakeSummaryProvider(
                    [
                        "## Critical Context\n"
                        "password=OUTPUT_SECRET\n"
                        "github_pat_OUTPUT_SECRET_ABC123"
                    ]
                )
                manager = CompressionManager(
                    db,
                    provider,
                    policy=_policy(tail_tokens=40),
                )

                result = manager.compress(
                    "redaction",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(result.status, "committed")
                provider_input = json.dumps(
                    provider.calls,
                    ensure_ascii=False,
                )
                active = json.dumps(
                    db.audit_messages(
                        "redaction",
                        include_inactive=False,
                    ),
                    ensure_ascii=False,
                )
                self.assertNotIn("TOP_SECRET_INPUT", provider_input)
                self.assertNotIn(
                    "sk-proj-UNLABELED_SECRET_ABC123",
                    provider_input,
                )
                self.assertNotIn("HIDDEN_REASONING", provider_input)
                self.assertNotIn('"reasoning"', provider_input)
                self.assertNotIn("OUTPUT_SECRET", active)
                self.assertNotIn("github_pat_OUTPUT_SECRET_ABC123", active)
                self.assertIn("[REDACTED]", active)
            finally:
                db.close()

    def test_verbose_summary_is_rejected_instead_of_silently_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="bounded-yield")
                for index in range(5):
                    db.append_message(
                        "bounded-yield",
                        {
                            "role": "user",
                            "content": f"user-{index}-" + "u" * 80,
                        },
                    )
                    db.append_message(
                        "bounded-yield",
                        {
                            "role": "assistant",
                            "content": f"assistant-{index}-" + "a" * 80,
                        },
                    )
                provider = FakeSummaryProvider(["X" * 6_000])
                before = db.audit_messages(
                    "bounded-yield",
                    include_inactive=False,
                )
                manager = CompressionManager(
                    db,
                    provider,
                    policy=_policy(
                        tail_tokens=1,
                        max_input_tokens=10_000,
                        reserved_output_tokens=2_000,
                        max_output_tokens=2_000,
                    ),
                )

                result = manager.compress(
                    "bounded-yield",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(result.status, "rejected")
                self.assertEqual(
                    result.error_type,
                    "InsufficientCompressionSavings",
                )
                self.assertEqual(
                    db.audit_messages(
                        "bounded-yield",
                        include_inactive=False,
                    ),
                    before,
                )
                self.assertLess(
                    provider.calls[0]["max_output_tokens"],
                    1_024,
                )
                request_text = json.dumps(
                    provider.calls[0]["messages"],
                    ensure_ascii=False,
                )
                self.assertIn(
                    "Keep the final visible summary within approximately",
                    request_text,
                )
                self.assertNotIn("[TRUNCATED MIDDLE]", request_text)
            finally:
                db.close()

    def test_negative_savings_candidate_is_rejected_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="negative-yield")
                for index in range(5):
                    db.append_message(
                        "negative-yield",
                        {
                            "role": "user",
                            "content": f"user-{index}-" + "u" * 80,
                        },
                    )
                    db.append_message(
                        "negative-yield",
                        {
                            "role": "assistant",
                            "content": f"assistant-{index}-" + "a" * 80,
                        },
                    )
                before = db.audit_messages(
                    "negative-yield",
                    include_inactive=False,
                )
                provider = FakeSummaryProvider(["X" * 6_000, "Y" * 6_000])
                manager = UnboundedBudgetCompressionManager(
                    db,
                    provider,
                    policy=_policy(
                        tail_tokens=1,
                        max_input_tokens=10_000,
                        reserved_output_tokens=2_000,
                        max_output_tokens=2_000,
                        anti_thrashing_limit=2,
                    ),
                )

                first = manager.compress(
                    "negative-yield",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(first.status, "rejected")
                self.assertEqual(
                    first.error_type,
                    "InsufficientCompressionSavings",
                )
                self.assertGreater(
                    first.estimated_after_tokens,
                    first.trigger_tokens,
                )
                self.assertEqual(
                    db.audit_messages(
                        "negative-yield",
                        include_inactive=False,
                    ),
                    before,
                )
                run = db.audit_compression_runs("negative-yield")[-1]
                self.assertEqual(run["status"], "failed")
                self.assertEqual(
                    run["failure_type"],
                    "InsufficientCompressionSavings",
                )
                self.assertEqual(
                    db.get_compression_state("negative-yield")[
                        "consecutive_low_savings"
                    ],
                    1,
                )

                second = manager.compress(
                    "negative-yield",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(second.status, "rejected")
                self.assertEqual(
                    db.audit_messages(
                        "negative-yield",
                        include_inactive=False,
                    ),
                    before,
                )
                self.assertEqual(
                    db.get_compression_state("negative-yield")["auto_paused"],
                    1,
                )
            finally:
                db.close()

    def test_storage_rejects_candidate_that_bypasses_savings_guard(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="storage-guard")
                db.append_message(
                    "storage-guard",
                    {"role": "user", "content": "original"},
                )
                before = db.audit_messages(
                    "storage-guard",
                    include_inactive=False,
                )
                owner = "storage-test"
                self.assertTrue(
                    db.try_acquire_compression_lock(
                        "storage-guard",
                        owner=owner,
                        ttl_seconds=60,
                    )
                )

                with self.assertRaisesRegex(
                    SessionError,
                    "minimum token savings contract",
                ):
                    db.archive_and_compact(
                        "storage-guard",
                        owner=owner,
                        expected_active_ids=tuple(
                            row["id"]
                            for row in db.active_message_records(
                                "storage-guard"
                            )
                        ),
                        head_messages=(),
                        summary_message={
                            "role": "user",
                            "content": "larger candidate",
                        },
                        bridge_message={
                            "role": "assistant",
                            "content": "bridge",
                        },
                        tail_messages=(),
                        trigger_tokens=100,
                        estimated_after_tokens=101,
                        compacted_message_count=1,
                        summary_revision="revision",
                        used_fallback=False,
                        minimum_saved_tokens=1,
                        anti_thrashing_limit=2,
                    )

                self.assertEqual(
                    db.audit_messages(
                        "storage-guard",
                        include_inactive=False,
                    ),
                    before,
                )
                self.assertEqual(
                    db.audit_compression_runs("storage-guard"),
                    (),
                )
            finally:
                db.close()

    def test_oversized_history_uses_bounded_hierarchical_summary_calls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="hierarchical")
                for index in range(30):
                    db.append_message(
                        "hierarchical",
                        {
                            "role": "user",
                            "content": (
                                f"USER_MARK_{index} " + "u" * 2_500
                            ),
                        },
                    )
                    db.append_message(
                        "hierarchical",
                        {
                            "role": "assistant",
                            "content": (
                                f"ASSISTANT_MARK_{index} " + "a" * 2_500
                            ),
                        },
                    )
                provider = RecordingSummaryProvider()
                policy = _policy(
                    max_input_tokens=2_000,
                    reserved_output_tokens=400,
                    max_output_tokens=200,
                    tail_tokens=100,
                )
                manager = CompressionManager(
                    db,
                    provider,
                    policy=policy,
                )

                result = manager.compress(
                    "hierarchical",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(result.status, "committed")
                self.assertGreater(len(provider.calls), 2)
                estimate_policy = ContextBudgetPolicy(
                    max_input_tokens=policy.max_input_tokens,
                    reserved_output_tokens=(
                        policy.reserved_output_tokens
                    ),
                    approximate_chars_per_token=(
                        policy.approximate_chars_per_token
                    ),
                )
                input_limit = (
                    policy.max_input_tokens
                    - policy.reserved_output_tokens
                )
                for call in provider.calls:
                    self.assertLessEqual(
                        estimate_message_tokens(
                            call["messages"],
                            estimate_policy,
                        ),
                        input_limit,
                    )
                all_provider_input = json.dumps(
                    provider.calls,
                    ensure_ascii=False,
                )
                for index in range(1, 29):
                    self.assertIn(
                        f"USER_MARK_{index}",
                        all_provider_input,
                    )
                    self.assertIn(
                        f"ASSISTANT_MARK_{index}",
                        all_provider_input,
                    )
                active = db.active_message_records("hierarchical")
                self.assertEqual(
                    sum(
                        row["source"] == "compression_summary"
                        for row in active
                    ),
                    1,
                )
            finally:
                db.close()

    def test_session_lock_excludes_second_compressor_and_can_be_released(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            first = SessionDB(path)
            second = SessionDB(path)
            try:
                first.create_session("system", session_id="locked")
                self.assertTrue(
                    first.try_acquire_compression_lock(
                        "locked",
                        owner="worker-a",
                        ttl_seconds=60,
                    )
                )
                self.assertFalse(
                    second.try_acquire_compression_lock(
                        "locked",
                        owner="worker-b",
                        ttl_seconds=60,
                    )
                )
                first.release_compression_lock(
                    "locked",
                    owner="worker-a",
                )
                self.assertTrue(
                    second.try_acquire_compression_lock(
                        "locked",
                        owner="worker-b",
                        ttl_seconds=60,
                    )
                )
            finally:
                second.close()
                first.close()

    def test_slow_summary_renews_lease_and_prevents_duplicate_provider_call(
        self,
    ) -> None:
        class BlockingProvider:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()
                self.calls = 0

            def summarize(self, messages, *, max_output_tokens):
                self.calls += 1
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("test did not release Summary Provider")
                return "durable concise summary"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            seed = SessionDB(path)
            seed.create_session("system", session_id="slow-summary")
            _append_turns(seed, "slow-summary", count=8)
            seed.close()

            blocking = BlockingProvider()
            first_result: dict[str, CompressionResult] = {}

            def run_first() -> None:
                first_db = SessionDB(path)
                try:
                    first_result["value"] = CompressionManager(
                        first_db,
                        blocking,
                        policy=_policy(lock_ttl_seconds=0.15),
                    ).compress(
                        "slow-summary",
                        system_prompt="system",
                        tools=None,
                        force=True,
                    )
                finally:
                    first_db.close()

            worker = threading.Thread(target=run_first)
            worker.start()
            self.assertTrue(blocking.started.wait(timeout=3))
            time.sleep(0.30)

            second_db = SessionDB(path)
            second_provider = RecordingSummaryProvider()
            try:
                second_result = CompressionManager(
                    second_db,
                    second_provider,
                    policy=_policy(lock_ttl_seconds=0.15),
                ).compress(
                    "slow-summary",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )
            finally:
                blocking.release.set()
                worker.join(timeout=5)

            try:
                self.assertFalse(worker.is_alive())
                self.assertEqual(second_result.status, "locked")
                self.assertEqual(second_provider.calls, [])
                self.assertEqual(first_result["value"].status, "committed")
                self.assertEqual(blocking.calls, 1)
                runs = second_db.audit_compression_runs("slow-summary")
                self.assertEqual([run["status"] for run in runs], ["committed"])
                integrity = second_db._connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                self.assertEqual(integrity, "ok")
            finally:
                second_db.close()

    def test_expired_compression_lease_cannot_be_resurrected_or_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="expired")
                _append_turns(db, "expired", count=2)
                self.assertTrue(
                    db.try_acquire_compression_lock(
                        "expired", owner="old", ttl_seconds=0.05
                    )
                )
                time.sleep(0.10)
                self.assertFalse(
                    db.refresh_compression_lock(
                        "expired", owner="old", ttl_seconds=60
                    )
                )
                records = db.active_message_records("expired")
                with self.assertRaises(CompressionLeaseError):
                    db.archive_and_compact(
                        "expired",
                        owner="old",
                        expected_active_ids=[row["id"] for row in records],
                        head_messages=(),
                        summary_message={"role": "user", "content": "summary"},
                        bridge_message={"role": "assistant", "content": "bridge"},
                        tail_messages=(),
                        trigger_tokens=100,
                        estimated_after_tokens=10,
                        compacted_message_count=len(records),
                        summary_revision="revision",
                        used_fallback=False,
                        minimum_saved_tokens=1,
                        anti_thrashing_limit=2,
                    )
                self.assertEqual(
                    [row["id"] for row in db.active_message_records("expired")],
                    [row["id"] for row in records],
                )
            finally:
                db.close()

    def test_rolling_fallback_keeps_one_summary_and_trips_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="rolling")
                _append_turns(db, "rolling", count=6)
                provider = FakeSummaryProvider(error=RuntimeError("offline"))
                manager = CompressionManager(
                    db,
                    provider,
                    policy=_policy(
                        abort_on_summary_failure=False,
                        anti_thrashing_limit=2,
                        min_savings_ratio=0.05,
                    ),
                )

                first = manager.compress(
                    "rolling",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )
                _append_turns(db, "rolling", count=4, start=6)
                second = manager.compress(
                    "rolling",
                    system_prompt="system",
                    tools=None,
                    force=True,
                )

                self.assertEqual(first.status, "committed")
                self.assertTrue(first.used_fallback)
                self.assertEqual(second.status, "committed")
                self.assertTrue(second.used_fallback)
                active = db.audit_messages(
                    "rolling",
                    include_inactive=False,
                )
                self.assertEqual(
                    sum(
                        row["source"] == "compression_summary"
                        for row in active
                    ),
                    1,
                )
                state = db.get_compression_state("rolling")
                self.assertEqual(state["consecutive_fallbacks"], 2)
                self.assertEqual(state["auto_paused"], 1)
                auto = manager.compress(
                    "rolling",
                    system_prompt="system",
                    tools=None,
                    force=False,
                )
                self.assertEqual(auto.status, "paused")
            finally:
                db.close()

    def test_agent_manual_compression_reloads_committed_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="agent-manual")
                _append_turns(db, "agent-manual", count=6)
                manager = CompressionManager(
                    db,
                    FakeSummaryProvider(["durable summary"]),
                    policy=_policy(),
                )
                agent = Agent(
                    FakeAgentProvider([]),
                    system_prompt="ignored for resume",
                    session_db=db,
                    session_id="agent-manual",
                    compression_manager=manager,
                )

                result = agent.compress_context(force=True)

                self.assertEqual(result.status, "committed")
                self.assertTrue(
                    any(
                        "[CONTEXT COMPACTION" in str(message.get("content"))
                        for message in agent.messages
                    )
                )
                self.assertEqual(
                    tuple(
                        row["message"]
                        for row in db.active_message_records("agent-manual")
                    ),
                    agent.messages[1:],
                )
            finally:
                db.close()

    def test_agent_auto_compresses_before_primary_provider_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = SessionDB(Path(directory) / "sessions.db")
            try:
                db.create_session("system", session_id="agent-auto")
                _append_turns(db, "agent-auto", count=6)
                summary_provider = FakeSummaryProvider(["automatic summary"])
                manager = CompressionManager(
                    db,
                    summary_provider,
                    policy=_policy(
                        max_input_tokens=1_000,
                        reserved_output_tokens=100,
                    ),
                )
                primary = FakeAgentProvider(["continued"])
                agent = Agent(
                    primary,
                    system_prompt="ignored for resume",
                    session_db=db,
                    session_id="agent-auto",
                    compression_manager=manager,
                )

                answer = agent.chat("latest real request")

                self.assertEqual(answer, "continued")
                self.assertEqual(len(summary_provider.calls), 1)
                self.assertTrue(
                    any(
                        "[CONTEXT COMPACTION" in str(message.get("content"))
                        for message in primary.calls[0]
                    )
                )
                self.assertEqual(
                    primary.calls[0][-1]["content"],
                    "latest real request",
                )
            finally:
                db.close()


def _append_turns(
    db: SessionDB,
    session_id: str,
    *,
    count: int,
    start: int = 0,
) -> None:
    for index in range(start, start + count):
        db.append_message(
            session_id,
            {
                "role": "user",
                "content": f"user-{index}-" + "u" * 200,
            },
        )
        db.append_message(
            session_id,
            {
                "role": "assistant",
                "content": f"assistant-{index}-" + "a" * 200,
            },
        )


def _policy(**overrides) -> CompressionPolicy:
    values = {
        "enabled": True,
        "threshold_ratio": 0.5,
        "target_ratio": 0.2,
        "protect_first_turns": 1,
        "tail_tokens": 120,
        "max_output_tokens": 800,
        "max_input_tokens": 4_000,
        "reserved_output_tokens": 400,
        "approximate_chars_per_token": 4,
        "abort_on_summary_failure": True,
        "cooldown_seconds": 60,
        "lock_ttl_seconds": 60,
        "min_savings_ratio": 0.1,
        "anti_thrashing_limit": 2,
    }
    values.update(overrides)
    return CompressionPolicy(**values)


def _policy_from_config(config) -> CompressionPolicy:
    return CompressionPolicy(
        enabled=config.compression.enabled,
        threshold_ratio=config.compression.threshold_ratio,
        target_ratio=config.compression.target_ratio,
        protect_first_turns=config.compression.protect_first_turns,
        tail_tokens=config.compression.tail_tokens,
        max_output_tokens=config.compression.max_output_tokens,
        max_input_tokens=config.context.max_input_tokens,
        reserved_output_tokens=config.context.reserved_output_tokens,
        approximate_chars_per_token=config.context.approximate_chars_per_token,
        abort_on_summary_failure=config.compression.abort_on_summary_failure,
        cooldown_seconds=config.compression.cooldown_seconds,
        lock_ttl_seconds=config.compression.lock_ttl_seconds,
        min_savings_ratio=config.compression.min_savings_ratio,
        anti_thrashing_limit=config.compression.anti_thrashing_limit,
        in_place=config.compression.in_place,
    )


if __name__ == "__main__":
    unittest.main()
