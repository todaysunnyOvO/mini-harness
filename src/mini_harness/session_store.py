from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence

from .guardrails import tool_arguments_hash, tool_call_signature
from .profiles import normalize_profile_id

MESSAGE_STRIDE = 1_000_000
CURRENT_SCHEMA_VERSION = 5

_CORE_TABLES = frozenset({"sessions", "messages", "recovery_events"})
_BASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        system_prompt TEXT NOT NULL,
        profile_id TEXT NOT NULL DEFAULT 'default',
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ),
        updated_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_session_position
        ON messages(session_id, active, position)
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    )
    """,
)
_JOURNAL_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tool_execution_journal (
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
        execution_backend TEXT,
        hard_terminated INTEGER NOT NULL DEFAULT 0 CHECK (
            hard_terminated IN (0, 1)
        ),
        error_code TEXT,
        result_message_id INTEGER REFERENCES messages(id),
        prepared_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ),
        started_at TEXT,
        finished_at TEXT,
        UNIQUE(assistant_message_id, tool_call_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_journal_session
        ON tool_execution_journal(session_id, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_journal_incomplete
        ON tool_execution_journal(session_id, status)
        WHERE status IN ('prepared', 'running')
    """,
)
_COMPRESSION_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS compression_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ),
        trigger_tokens INTEGER NOT NULL,
        original_active_message_count INTEGER NOT NULL,
        compacted_message_count INTEGER NOT NULL,
        summary_revision TEXT NOT NULL,
        used_fallback INTEGER NOT NULL DEFAULT 0 CHECK (
            used_fallback IN (0, 1)
        ),
        status TEXT NOT NULL CHECK (
            status IN ('committed', 'failed')
        ),
        saved_tokens INTEGER NOT NULL DEFAULT 0,
        failure_type TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_compression_runs_session
        ON compression_runs(session_id, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS compression_state (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        lock_owner TEXT,
        lock_expires_at REAL NOT NULL DEFAULT 0,
        cooldown_until REAL NOT NULL DEFAULT 0,
        consecutive_low_savings INTEGER NOT NULL DEFAULT 0,
        consecutive_fallbacks INTEGER NOT NULL DEFAULT 0,
        auto_paused INTEGER NOT NULL DEFAULT 0 CHECK (
            auto_paused IN (0, 1)
        ),
        last_failure_type TEXT,
        updated_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    )
    """,
)


class SessionError(RuntimeError):
    """Raised when persistent session state is missing or invalid."""


class CompressionLeaseError(SessionError):
    """The compressor no longer owns a live lease for the Session."""


@dataclass(frozen=True)
class SessionRecord:
    id: str
    system_prompt: str
    profile_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RecoveryReport:
    messages: tuple[dict[str, Any], ...]
    inserted_unknown_results: int = 0
    inserted_not_executed_results: int = 0
    deactivated_orphan_results: int = 0
    repaired_tool_call_ids: int = 0
    merged_same_role_messages: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.inserted_unknown_results,
                self.inserted_not_executed_results,
                self.deactivated_orphan_results,
                self.repaired_tool_call_ids,
                self.merged_same_role_messages,
            )
        )


class SessionDB:
    """Small append-oriented SQLite store with protocol-safe recovery."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # The compression watchdog renews its lease from a background thread.
        # All access to this connection remains serialized by ``self._lock``.
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        try:
            self._migrate_schema()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _migrate_schema(self) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute("PRAGMA user_version").fetchone()
                version = int(row[0])
                if version > CURRENT_SCHEMA_VERSION:
                    raise SessionError(
                        "Database schema is newer than this Mini Harness: "
                        f"{version} > {CURRENT_SCHEMA_VERSION}"
                    )

                tables = {
                    item["name"]
                    for item in self._connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                }
                if version == 0 and tables:
                    if not _CORE_TABLES.issubset(tables):
                        missing = ", ".join(sorted(_CORE_TABLES - tables))
                        raise SessionError(
                            "Legacy database has an incomplete core schema; "
                            f"missing: {missing}"
                        )
                    version = 1

                for statement in _BASE_SCHEMA:
                    self._connection.execute(statement)
                session_columns = {
                    item["name"]
                    for item in self._connection.execute(
                        'PRAGMA table_info("sessions")'
                    ).fetchall()
                }
                if "profile_id" not in session_columns:
                    self._connection.execute(
                        """
                        ALTER TABLE sessions
                        ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default'
                        """
                    )
                for statement in _JOURNAL_SCHEMA:
                    self._connection.execute(statement)
                for statement in _COMPRESSION_SCHEMA:
                    self._connection.execute(statement)
                journal_columns = {
                    item["name"]
                    for item in self._connection.execute(
                        'PRAGMA table_info("tool_execution_journal")'
                    ).fetchall()
                }
                if "execution_backend" not in journal_columns:
                    self._connection.execute(
                        """
                        ALTER TABLE tool_execution_journal
                        ADD COLUMN execution_backend TEXT
                        """
                    )
                if "hard_terminated" not in journal_columns:
                    self._connection.execute(
                        """
                        ALTER TABLE tool_execution_journal
                        ADD COLUMN hard_terminated INTEGER NOT NULL DEFAULT 0
                        CHECK (hard_terminated IN (0, 1))
                        """
                    )

                self._connection.execute(
                    f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}"
                )
                self._connection.commit()
            except SessionError:
                self._connection.rollback()
                raise
            except Exception as exc:
                self._connection.rollback()
                raise SessionError(
                    f"Database schema migration failed: {exc}"
                ) from exc

    def create_session(
        self,
        system_prompt: str,
        *,
        session_id: str | None = None,
        profile_id: str = "default",
    ) -> SessionRecord:
        identifier = _validate_session_id(session_id or uuid.uuid4().hex)
        try:
            normalized_profile_id = normalize_profile_id(profile_id)
        except ValueError as exc:
            raise SessionError(str(exc)) from exc
        if not system_prompt.strip():
            raise SessionError("system_prompt must not be empty")
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO sessions(id, system_prompt, profile_id)
                    VALUES (?, ?, ?)
                    """,
                    (identifier, system_prompt, normalized_profile_id),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionError(f"Session already exists: {identifier}") from exc
        return self.get_session(identifier)

    def session_exists(self, session_id: str) -> bool:
        identifier = _validate_session_id(session_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (identifier,),
            ).fetchone()
        return row is not None

    def get_session(self, session_id: str) -> SessionRecord:
        identifier = _validate_session_id(session_id)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, system_prompt, profile_id, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
        if row is None:
            raise SessionError(f"Session not found: {identifier}")
        return SessionRecord(
            id=row["id"],
            system_prompt=row["system_prompt"],
            profile_id=row["profile_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_sessions(
        self,
        *,
        profile_id: str,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        try:
            normalized_profile_id = normalize_profile_id(profile_id)
        except ValueError as exc:
            raise SessionError(str(exc)) from exc
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be at least one")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.id, s.profile_id, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions AS s
                LEFT JOIN messages AS m
                  ON m.session_id = s.id AND m.active = 1
                WHERE s.profile_id = ?
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (normalized_profile_id, limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def append_message(
        self,
        session_id: str,
        message: Mapping[str, Any],
        *,
        source: str = "runtime",
    ) -> int:
        identifier = _validate_session_id(session_id)
        normalized = _normalize_message(message)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            if not self.session_exists(identifier):
                raise SessionError(f"Session not found: {identifier}")
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position), 0) AS maximum FROM messages WHERE session_id = ?",
                (identifier,),
            ).fetchone()
            position = int(row["maximum"]) + MESSAGE_STRIDE
            cursor = self._connection.execute(
                """
                INSERT INTO messages(
                    session_id, position, role, message_json, original_json, source
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    position,
                    normalized["role"],
                    encoded,
                    encoded,
                    source,
                ),
            )
            self._touch_session(identifier)
            return int(cursor.lastrowid)

    def append_assistant_with_prepared_journals(
        self,
        session_id: str,
        message: Mapping[str, Any],
    ) -> tuple[int, tuple[int, ...]]:
        """Atomically persist one Assistant Tool Call batch and its journals."""
        identifier = _validate_session_id(session_id)
        normalized = _normalize_message(message)
        tool_calls = normalized.get("tool_calls")
        if normalized["role"] != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
            raise SessionError(
                "prepared journals require an assistant message with tool_calls"
            )
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            message_id = self._insert_message_unlocked(
                identifier,
                normalized["role"],
                encoded,
                encoded,
                source="runtime",
            )
            journal_ids: list[int] = []
            for call_index, raw_call in enumerate(tool_calls):
                call = dict(raw_call) if isinstance(raw_call, Mapping) else {}
                function_raw = call.get("function")
                function = (
                    dict(function_raw)
                    if isinstance(function_raw, Mapping)
                    else {}
                )
                call_id = str(call.get("id") or f"call_{message_id}_{call_index}")
                tool_name = str(function.get("name") or "unknown_tool")
                arguments_raw = function.get("arguments", "{}")
                if isinstance(arguments_raw, str):
                    arguments = arguments_raw
                elif isinstance(arguments_raw, Mapping):
                    arguments = json.dumps(
                        arguments_raw,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                else:
                    arguments = "{}"
                cursor = self._connection.execute(
                    """
                    INSERT INTO tool_execution_journal(
                        session_id, assistant_message_id,
                        tool_call_id, tool_call_index, tool_name,
                        arguments_hash, call_signature,
                        idempotency_key, status,
                        effect_disposition, execution_phase
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'prepared', 'none', 'prepared')
                    """,
                    (
                        identifier,
                        message_id,
                        call_id,
                        call_index,
                        tool_name,
                        tool_arguments_hash(arguments),
                        tool_call_signature(tool_name, arguments),
                    ),
                )
                journal_ids.append(int(cursor.lastrowid))
            self._touch_session(identifier)
            return message_id, tuple(journal_ids)

    def mark_tool_running(
        self,
        journal_id: int,
        *,
        execution_backend: str | None = None,
    ) -> None:
        if not isinstance(journal_id, int) or journal_id < 1:
            raise SessionError("journal_id must be a positive integer")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE tool_execution_journal
                SET status = 'running',
                    execution_phase = 'handler_started',
                    execution_backend = ?,
                    started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = 'prepared'
                """,
                (execution_backend, journal_id),
            )
            if cursor.rowcount != 1:
                raise SessionError(
                    f"Journal is missing or not prepared: {journal_id}"
                )

    def append_tool_result_and_finalize_journal(
        self,
        session_id: str,
        journal_id: int,
        message: Mapping[str, Any],
        *,
        effect_disposition: str,
        execution_phase: str,
        execution_backend: str | None,
        hard_terminated: bool,
        error_code: str | None,
    ) -> int:
        """Atomically persist a Tool Result and make its journal terminal."""
        identifier = _validate_session_id(session_id)
        normalized = _normalize_message(message)
        if normalized["role"] != "tool":
            raise SessionError("journal finalization requires a tool message")
        if effect_disposition not in {"none", "completed", "unknown"}:
            raise SessionError(
                f"unsupported effect_disposition: {effect_disposition!r}"
            )
        terminal_status = (
            "unknown" if effect_disposition == "unknown" else "completed"
        )
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            journal = self._connection.execute(
                """
                SELECT session_id, tool_call_id, status
                FROM tool_execution_journal
                WHERE id = ?
                """,
                (journal_id,),
            ).fetchone()
            if journal is None or journal["session_id"] != identifier:
                raise SessionError(f"Journal not found in session: {journal_id}")
            if journal["status"] not in {"prepared", "running"}:
                raise SessionError(
                    f"Journal is already terminal: {journal_id}"
                )
            if normalized.get("tool_call_id") != journal["tool_call_id"]:
                raise SessionError(
                    "Tool Result call ID does not match its execution journal"
                )

            result_message_id = self._insert_message_unlocked(
                identifier,
                normalized["role"],
                encoded,
                encoded,
                source="runtime",
            )
            cursor = self._connection.execute(
                """
                UPDATE tool_execution_journal
                SET status = ?,
                    effect_disposition = ?,
                    execution_phase = ?,
                    execution_backend = COALESCE(?, execution_backend),
                    hard_terminated = ?,
                    error_code = ?,
                    result_message_id = ?,
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status IN ('prepared', 'running')
                """,
                (
                    terminal_status,
                    effect_disposition,
                    execution_phase,
                    execution_backend,
                    int(hard_terminated),
                    error_code,
                    result_message_id,
                    journal_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SessionError(
                    f"Journal transition failed: {journal_id}"
                )
            self._touch_session(identifier)
            return result_message_id

    def audit_tool_journal(
        self,
        session_id: str,
    ) -> tuple[dict[str, Any], ...]:
        identifier = _validate_session_id(session_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM tool_execution_journal
                WHERE session_id = ?
                ORDER BY id
                """,
                (identifier,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def resume_session(self, session_id: str) -> tuple[SessionRecord, RecoveryReport]:
        session = self.get_session(session_id)
        with self._lock, self._connection:
            report = self._repair_messages(session.id)
            if report.changed:
                self._touch_session(session.id)
        return self.get_session(session.id), report

    def audit_messages(
        self,
        session_id: str,
        *,
        include_inactive: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        identifier = _validate_session_id(session_id)
        where = "session_id = ?" if include_inactive else "session_id = ? AND active = 1"
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, position, role, message_json, original_json,
                       active, source, created_at
                FROM messages
                WHERE {where}
                ORDER BY position, id
                """,
                (identifier,),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "message": json.loads(row["message_json"]),
                "original_message": json.loads(row["original_json"]),
            }
            for row in rows
        )

    def active_message_records(
        self,
        session_id: str,
    ) -> tuple[dict[str, Any], ...]:
        return self.audit_messages(session_id, include_inactive=False)

    def audit_compression_runs(
        self,
        session_id: str,
    ) -> tuple[dict[str, Any], ...]:
        identifier = _validate_session_id(session_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM compression_runs
                WHERE session_id = ?
                ORDER BY id
                """,
                (identifier,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def get_compression_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        identifier = _validate_session_id(session_id)
        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            self._ensure_compression_state_unlocked(identifier)
            row = self._connection.execute(
                """
                SELECT *
                FROM compression_state
                WHERE session_id = ?
                """,
                (identifier,),
            ).fetchone()
        return dict(row)

    def try_acquire_compression_lock(
        self,
        session_id: str,
        *,
        owner: str,
        ttl_seconds: float,
    ) -> bool:
        identifier = _validate_session_id(session_id)
        if not isinstance(owner, str) or not owner.strip():
            raise SessionError("compression lock owner must not be empty")
        if ttl_seconds <= 0:
            raise SessionError("compression lock TTL must be positive")
        now = time.time()
        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            self._ensure_compression_state_unlocked(identifier)
            cursor = self._connection.execute(
                """
                UPDATE compression_state
                SET lock_owner = ?,
                    lock_expires_at = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE session_id = ?
                  AND (
                    lock_owner IS NULL
                    OR lock_expires_at <= ?
                    OR lock_owner = ?
                  )
                """,
                (
                    owner.strip(),
                    now + ttl_seconds,
                    identifier,
                    now,
                    owner.strip(),
                ),
            )
        return cursor.rowcount == 1

    def refresh_compression_lock(
        self,
        session_id: str,
        *,
        owner: str,
        ttl_seconds: float,
    ) -> bool:
        identifier = _validate_session_id(session_id)
        if not isinstance(owner, str) or not owner.strip():
            raise SessionError("compression lock owner must not be empty")
        if ttl_seconds <= 0:
            raise SessionError("compression lock TTL must be positive")
        now = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE compression_state
                SET lock_expires_at = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE session_id = ?
                  AND lock_owner = ?
                  AND lock_expires_at > ?
                """,
                (now + ttl_seconds, identifier, owner.strip(), now),
            )
        return cursor.rowcount == 1

    def release_compression_lock(
        self,
        session_id: str,
        *,
        owner: str,
    ) -> None:
        identifier = _validate_session_id(session_id)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE compression_state
                SET lock_owner = NULL,
                    lock_expires_at = 0,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE session_id = ? AND lock_owner = ?
                """,
                (identifier, owner),
            )

    def record_compression_failure(
        self,
        session_id: str,
        *,
        trigger_tokens: int,
        original_active_message_count: int,
        failure_type: str,
        cooldown_seconds: float,
    ) -> None:
        identifier = _validate_session_id(session_id)
        safe_failure = str(failure_type or "CompressionError")[:120]
        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            self._ensure_compression_state_unlocked(identifier)
            self._connection.execute(
                """
                INSERT INTO compression_runs(
                    session_id, trigger_tokens,
                    original_active_message_count,
                    compacted_message_count, summary_revision,
                    used_fallback, status, saved_tokens, failure_type
                )
                VALUES (?, ?, ?, 0, '', 0, 'failed', 0, ?)
                """,
                (
                    identifier,
                    max(0, int(trigger_tokens)),
                    max(0, int(original_active_message_count)),
                    safe_failure,
                ),
            )
            self._connection.execute(
                """
                UPDATE compression_state
                SET cooldown_until = ?,
                    last_failure_type = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE session_id = ?
                """,
                (
                    time.time() + max(0.0, cooldown_seconds),
                    safe_failure,
                    identifier,
                ),
            )

    def record_compression_rejection(
        self,
        session_id: str,
        *,
        trigger_tokens: int,
        original_active_message_count: int,
        failure_type: str,
        cooldown_seconds: float,
        anti_thrashing_limit: int,
    ) -> None:
        """Audit a non-destructive low-yield candidate and back it off."""

        identifier = _validate_session_id(session_id)
        safe_failure = str(failure_type or "CompressionRejected")[:120]
        with self._lock, self._connection:
            self._require_session_unlocked(identifier)
            self._ensure_compression_state_unlocked(identifier)
            state = self._connection.execute(
                """
                SELECT consecutive_low_savings, auto_paused
                FROM compression_state
                WHERE session_id = ?
                """,
                (identifier,),
            ).fetchone()
            low_count = int(state["consecutive_low_savings"]) + 1
            paused = int(
                bool(state["auto_paused"])
                or low_count >= max(1, int(anti_thrashing_limit))
            )
            self._connection.execute(
                """
                INSERT INTO compression_runs(
                    session_id, trigger_tokens,
                    original_active_message_count,
                    compacted_message_count, summary_revision,
                    used_fallback, status, saved_tokens, failure_type
                )
                VALUES (?, ?, ?, 0, '', 0, 'failed', 0, ?)
                """,
                (
                    identifier,
                    max(0, int(trigger_tokens)),
                    max(0, int(original_active_message_count)),
                    safe_failure,
                ),
            )
            self._connection.execute(
                """
                UPDATE compression_state
                SET cooldown_until = ?,
                    consecutive_low_savings = ?,
                    auto_paused = ?,
                    last_failure_type = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE session_id = ?
                """,
                (
                    time.time() + max(0.0, cooldown_seconds),
                    low_count,
                    paused,
                    safe_failure,
                    identifier,
                ),
            )

    def archive_and_compact(
        self,
        session_id: str,
        *,
        owner: str,
        expected_active_ids: Sequence[int],
        head_messages: Sequence[Mapping[str, Any]],
        summary_message: Mapping[str, Any],
        bridge_message: Mapping[str, Any],
        tail_messages: Sequence[Mapping[str, Any]],
        trigger_tokens: int,
        estimated_after_tokens: int,
        compacted_message_count: int,
        summary_revision: str,
        used_fallback: bool,
        minimum_saved_tokens: int,
        anti_thrashing_limit: int,
    ) -> int:
        """Atomically soft-archive active history and commit one boundary."""

        identifier = _validate_session_id(session_id)
        expected = tuple(int(value) for value in expected_active_ids)
        if not expected:
            raise SessionError("compression requires active messages")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._require_session_unlocked(identifier)
                lock = self._connection.execute(
                    """
                    SELECT lock_owner, lock_expires_at
                    FROM compression_state
                    WHERE session_id = ?
                    """,
                    (identifier,),
                ).fetchone()
                if (
                    lock is None
                    or lock["lock_owner"] != owner
                    or float(lock["lock_expires_at"]) <= time.time()
                ):
                    raise CompressionLeaseError(
                        "compression lock ownership was lost or expired"
                    )
                current_ids = tuple(
                    int(row["id"])
                    for row in self._connection.execute(
                        """
                        SELECT id
                        FROM messages
                        WHERE session_id = ? AND active = 1
                        ORDER BY position, id
                        """,
                        (identifier,),
                    ).fetchall()
                )
                if current_ids != expected:
                    raise SessionError(
                        "active Session history changed during compression"
                    )
                incomplete = self._connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM tool_execution_journal
                    WHERE session_id = ?
                      AND status IN ('prepared', 'running')
                    """,
                    (identifier,),
                ).fetchone()
                if int(incomplete["count"]) > 0:
                    raise SessionError(
                        "cannot compress while Tool executions are incomplete"
                    )

                saved_tokens = (
                    int(trigger_tokens) - int(estimated_after_tokens)
                )
                if saved_tokens < max(1, int(minimum_saved_tokens)):
                    raise SessionError(
                        "compression candidate does not meet the minimum "
                        "token savings contract"
                    )

                self._connection.execute(
                    """
                    UPDATE messages
                    SET active = 0
                    WHERE session_id = ? AND active = 1
                    """,
                    (identifier,),
                )
                for message in head_messages:
                    self._insert_compression_message_unlocked(
                        identifier,
                        message,
                        source="compression_head",
                    )
                self._insert_compression_message_unlocked(
                    identifier,
                    summary_message,
                    source="compression_summary",
                )
                self._insert_compression_message_unlocked(
                    identifier,
                    bridge_message,
                    source="compression_bridge",
                )
                for message in tail_messages:
                    self._insert_compression_message_unlocked(
                        identifier,
                        message,
                        source="compression_tail",
                    )

                cursor = self._connection.execute(
                    """
                    INSERT INTO compression_runs(
                        session_id, trigger_tokens,
                        original_active_message_count,
                        compacted_message_count, summary_revision,
                        used_fallback, status, saved_tokens, failure_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'committed', ?, NULL)
                    """,
                    (
                        identifier,
                        max(0, int(trigger_tokens)),
                        len(expected),
                        max(0, int(compacted_message_count)),
                        str(summary_revision),
                        int(bool(used_fallback)),
                        saved_tokens,
                    ),
                )
                state = self._connection.execute(
                    """
                    SELECT consecutive_low_savings,
                           consecutive_fallbacks
                    FROM compression_state
                    WHERE session_id = ?
                    """,
                    (identifier,),
                ).fetchone()
                low_count = 0
                fallback_count = (
                    int(state["consecutive_fallbacks"]) + 1
                    if used_fallback
                    else 0
                )
                paused = int(
                    low_count >= anti_thrashing_limit
                    or fallback_count >= anti_thrashing_limit
                )
                self._connection.execute(
                    """
                    UPDATE compression_state
                    SET cooldown_until = 0,
                        consecutive_low_savings = ?,
                        consecutive_fallbacks = ?,
                        auto_paused = ?,
                        last_failure_type = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE session_id = ?
                    """,
                    (low_count, fallback_count, paused, identifier),
                )
                self._touch_session(identifier)
                self._connection.commit()
                return int(cursor.lastrowid)
            except Exception as exc:
                self._connection.rollback()
                if isinstance(exc, SessionError):
                    raise
                raise SessionError(
                    f"Compression transaction failed: {exc}"
                ) from exc

    def _ensure_compression_state_unlocked(self, session_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO compression_state(session_id)
            VALUES (?)
            """,
            (session_id,),
        )

    def _insert_compression_message_unlocked(
        self,
        session_id: str,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> int:
        normalized = _normalize_message(message)
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._insert_message_unlocked(
            session_id,
            normalized["role"],
            encoded,
            encoded,
            source=source,
        )

    def recovery_events(self, session_id: str) -> tuple[dict[str, Any], ...]:
        identifier = _validate_session_id(session_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_type, details_json, created_at
                FROM recovery_events
                WHERE session_id = ?
                ORDER BY id
                """,
                (identifier,),
            ).fetchall()
        return tuple(
            {
                "event_type": row["event_type"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        )

    def _repair_messages(self, session_id: str) -> RecoveryReport:
        rows = self._connection.execute(
            """
            SELECT id, position, role, message_json
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY position, id
            """,
            (session_id,),
        ).fetchall()
        output: list[tuple[int, int, dict[str, Any]]] = []
        pending: list[dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        repaired_ids = 0
        inserted_unknown = 0
        inserted_not_executed = 0
        deactivated_orphans = 0
        merged_same_role = 0

        def flush_pending(before_position: int | None) -> None:
            nonlocal inserted_unknown, inserted_not_executed
            if not pending:
                return
            previous_position = output[-1][1] if output else 0
            if before_position is not None and before_position - previous_position <= len(pending):
                raise SessionError("No position space remains for recovery messages")
            for offset, call in enumerate(tuple(pending), start=1):
                journal = self._connection.execute(
                    """
                    SELECT id, status
                    FROM tool_execution_journal
                    WHERE assistant_message_id = ? AND tool_call_index = ?
                    """,
                    (call["assistant_message_id"], call["call_index"]),
                ).fetchone()
                if journal is not None and journal["status"] == "prepared":
                    effect_disposition = "none"
                    terminal_status = "completed"
                    execution_phase = "rejected_before_execution"
                    error_code = "not_executed_after_resume"
                    error_message = (
                        "The prior process ended while this tool call was "
                        "prepared, before its Handler was allowed to start. "
                        "The tool was not executed."
                    )
                    inserted_not_executed += 1
                else:
                    effect_disposition = "unknown"
                    terminal_status = "unknown"
                    execution_phase = "result_unavailable"
                    error_code = "result_unavailable_after_resume"
                    error_message = (
                        "The prior process ended before a trustworthy tool "
                        "result was persisted. The tool may have succeeded, "
                        "failed, or never started. It was not retried "
                        "automatically."
                    )
                    inserted_unknown += 1
                tool_message = _recovery_result_message(
                    call_id=call["normalized_id"],
                    tool_name=call["name"],
                    error_code=error_code,
                    error_message=error_message,
                    effect_disposition=effect_disposition,
                    execution_phase=execution_phase,
                )
                position = previous_position + offset
                row_id = self._insert_recovery_message(
                    session_id,
                    position,
                    tool_message,
                )
                self._record_event(
                    session_id,
                    "missing_tool_result",
                    {
                        "tool_call_id": call["normalized_id"],
                        "tool_name": call["name"],
                        "effect_disposition": effect_disposition,
                    },
                )
                if journal is not None:
                    self._connection.execute(
                        """
                        UPDATE tool_execution_journal
                        SET status = ?,
                            effect_disposition = ?,
                            execution_phase = ?,
                            error_code = ?,
                            result_message_id = ?,
                            finished_at = strftime(
                                '%Y-%m-%dT%H:%M:%fZ', 'now'
                            )
                        WHERE id = ?
                        """,
                        (
                            terminal_status,
                            effect_disposition,
                            execution_phase,
                            error_code,
                            row_id,
                            journal["id"],
                        ),
                    )
                output.append((row_id, position, tool_message))
            pending.clear()

        for row in rows:
            try:
                message = _normalize_message(json.loads(row["message_json"]))
            except (json.JSONDecodeError, SessionError, TypeError, ValueError) as exc:
                self._deactivate_message(row["id"])
                self._record_event(
                    session_id,
                    "invalid_message",
                    {"message_id": row["id"], "error": str(exc)},
                )
                continue

            role = message["role"]
            if role != "tool":
                flush_pending(row["position"])

            if role == "assistant":
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    changed = False
                    normalized_calls: list[dict[str, Any]] = []
                    for index, raw_call in enumerate(tool_calls):
                        call = dict(raw_call) if isinstance(raw_call, Mapping) else {}
                        function_raw = call.get("function")
                        function = (
                            dict(function_raw)
                            if isinstance(function_raw, Mapping)
                            else {}
                        )
                        original_id = str(call.get("id") or f"resume_call_{row['id']}_{index}")
                        normalized_id = _unique_call_id(original_id, seen_call_ids)
                        seen_call_ids.add(normalized_id)
                        if normalized_id != call.get("id"):
                            changed = True
                            repaired_ids += 1
                        call.update(
                            {
                                "id": normalized_id,
                                "type": "function",
                                "function": function,
                            }
                        )
                        normalized_calls.append(call)
                        self._connection.execute(
                            """
                            UPDATE tool_execution_journal
                            SET tool_call_id = ?, tool_name = ?
                            WHERE assistant_message_id = ?
                              AND tool_call_index = ?
                            """,
                            (
                                normalized_id,
                                str(function.get("name") or "unknown_tool"),
                                row["id"],
                                index,
                            ),
                        )
                        pending.append(
                            {
                                "original_id": original_id,
                                "normalized_id": normalized_id,
                                "name": str(function.get("name") or "unknown_tool"),
                                "assistant_message_id": row["id"],
                                "call_index": index,
                            }
                        )
                    if changed:
                        message["tool_calls"] = normalized_calls
                        self._update_message(row["id"], message)
                        self._record_event(
                            session_id,
                            "tool_call_id_repaired",
                            {"message_id": row["id"]},
                        )
                output.append((row["id"], row["position"], message))
                continue

            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "")
                match_index = next(
                    (
                        index
                        for index, call in enumerate(pending)
                        if tool_call_id
                        in {call["original_id"], call["normalized_id"]}
                    ),
                    None,
                )
                if match_index is None:
                    self._deactivate_message(row["id"])
                    self._record_event(
                        session_id,
                        "orphan_tool_result",
                        {
                            "message_id": row["id"],
                            "tool_call_id": tool_call_id,
                        },
                    )
                    deactivated_orphans += 1
                    continue
                call = pending.pop(match_index)
                if tool_call_id != call["normalized_id"]:
                    message["tool_call_id"] = call["normalized_id"]
                    self._update_message(row["id"], message)
                    repaired_ids += 1
                    self._record_event(
                        session_id,
                        "tool_result_id_repaired",
                        {
                            "message_id": row["id"],
                            "tool_call_id": call["normalized_id"],
                        },
                    )
                output.append((row["id"], row["position"], message))
                continue

            if (
                role == "user"
                and output
                and output[-1][2].get("role") == "user"
            ):
                previous_id, previous_position, previous = output[-1]
                previous_content = str(previous.get("content") or "")
                current_content = str(message.get("content") or "")
                previous["content"] = (
                    f"{previous_content}\n\n[Later user message]\n{current_content}"
                )
                self._update_message(previous_id, previous)
                self._deactivate_message(row["id"])
                self._record_event(
                    session_id,
                    "same_role_messages_merged",
                    {
                        "kept_message_id": previous_id,
                        "deactivated_message_id": row["id"],
                        "role": "user",
                    },
                )
                output[-1] = (previous_id, previous_position, previous)
                merged_same_role += 1
                continue

            output.append((row["id"], row["position"], message))

        flush_pending(None)
        return RecoveryReport(
            messages=tuple(message for _row_id, _position, message in output),
            inserted_unknown_results=inserted_unknown,
            inserted_not_executed_results=inserted_not_executed,
            deactivated_orphan_results=deactivated_orphans,
            repaired_tool_call_ids=repaired_ids,
            merged_same_role_messages=merged_same_role,
        )

    def _insert_recovery_message(
        self,
        session_id: str,
        position: int,
        message: Mapping[str, Any],
    ) -> int:
        normalized = _normalize_message(message)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        cursor = self._connection.execute(
            """
            INSERT INTO messages(
                session_id, position, role, message_json, original_json, source
            )
            VALUES (?, ?, ?, ?, ?, 'recovery')
            """,
            (
                session_id,
                position,
                normalized["role"],
                encoded,
                encoded,
            ),
        )
        return int(cursor.lastrowid)

    def _update_message(self, message_id: int, message: Mapping[str, Any]) -> None:
        normalized = _normalize_message(message)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        self._connection.execute(
            "UPDATE messages SET role = ?, message_json = ? WHERE id = ?",
            (normalized["role"], encoded, message_id),
        )

    def _deactivate_message(self, message_id: int) -> None:
        self._connection.execute(
            "UPDATE messages SET active = 0 WHERE id = ?",
            (message_id,),
        )

    def _record_event(
        self,
        session_id: str,
        event_type: str,
        details: Mapping[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO recovery_events(session_id, event_type, details_json)
            VALUES (?, ?, ?)
            """,
            (
                session_id,
                event_type,
                json.dumps(details, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def _require_session_unlocked(self, session_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionError(f"Session not found: {session_id}")

    def _insert_message_unlocked(
        self,
        session_id: str,
        role: str,
        message_json: str,
        original_json: str,
        *,
        source: str,
    ) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(position), 0) AS maximum
            FROM messages
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        position = int(row["maximum"]) + MESSAGE_STRIDE
        cursor = self._connection.execute(
            """
            INSERT INTO messages(
                session_id, position, role,
                message_json, original_json, source
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                position,
                role,
                message_json,
                original_json,
                source,
            ),
        )
        return int(cursor.lastrowid)

    def _touch_session(self, session_id: str) -> None:
        self._connection.execute(
            """
            UPDATE sessions
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (session_id,),
        )


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise SessionError("session_id must be a non-empty string")
    identifier = session_id.strip()
    if len(identifier) > 128:
        raise SessionError("session_id must be at most 128 characters")
    return identifier


def _normalize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise SessionError("message must be a mapping")
    normalized = dict(message)
    role = normalized.get("role")
    if role not in {"user", "assistant", "tool"}:
        raise SessionError(f"unsupported persisted role: {role!r}")
    if role == "tool":
        call_id = normalized.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise SessionError("tool message requires tool_call_id")
    json.dumps(normalized, ensure_ascii=False)
    return normalized


def _unique_call_id(call_id: str, seen: set[str]) -> str:
    if call_id not in seen:
        return call_id
    suffix = 2
    while f"{call_id}__resume_{suffix}" in seen:
        suffix += 1
    return f"{call_id}__resume_{suffix}"


def _recovery_result_message(
    *,
    call_id: str,
    tool_name: str,
    error_code: str,
    error_message: str,
    effect_disposition: str,
    execution_phase: str,
) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error": {
            "code": error_code,
            "message": error_message,
        },
        "meta": {
            "effect_disposition": effect_disposition,
            "execution_phase": execution_phase,
            "requested_tool": tool_name,
            "executed_tool": None,
            "name_repaired": False,
            "recovered": True,
        },
    }
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": json.dumps(payload, ensure_ascii=False),
    }
