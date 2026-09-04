from __future__ import annotations

from functools import partial
from hashlib import sha256
from pathlib import Path
import json
import os
import tempfile
from typing import Any, Mapping

from ..execution import SpawnProcessExecutionBackend
from .registry import ApprovalCallback, ToolDefinition, ToolInputError, ToolRegistry


MAX_LIST_ENTRIES = 200
MAX_READ_CHARS = 20_000


def build_file_registry(
    workspace_root: Path,
    *,
    auto_repair_names: bool = True,
    enable_write_file: bool = False,
    max_result_chars: int = 12_000,
    name_repair_threshold: float = 0.84,
    timeout_seconds: float = 30.0,
    approval_callback: ApprovalCallback | None = None,
) -> ToolRegistry:
    root = workspace_root.resolve()
    process_backend = SpawnProcessExecutionBackend()
    registry = ToolRegistry(
        auto_repair_names=auto_repair_names,
        max_result_chars=max_result_chars,
        name_repair_threshold=name_repair_threshold,
        timeout_seconds=timeout_seconds,
        approval_callback=approval_callback,
    )
    registry.register(
        ToolDefinition(
            name="list_files",
            description=(
                "List files and directories inside the configured workspace. "
                "Use relative paths only. Large directories are returned in "
                "pages; continue with offset=next_offset and the same "
                "expected_revision until eof is true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path. Defaults to '.'.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to list descendants recursively. Defaults to false.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based entry offset. Defaults to 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_ENTRIES,
                        "description": "Maximum entries requested for this page.",
                    },
                    "expected_revision": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                        "description": (
                            "Revision returned by the first page; detects "
                            "directory changes between continuation reads."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            preflight=lambda arguments: _preflight_list_files(root, arguments),
            handler=partial(_list_files, root),
            execution_backend=process_backend,
            failure_effect_disposition="none",
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description=(
                "Read a UTF-8 text file inside the configured workspace. "
                "Use a workspace-relative path. Large files are returned in "
                "pages; continue with offset=next_offset and the same "
                "expected_revision until eof is true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based character offset. Defaults to 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_CHARS,
                        "description": "Maximum characters requested for this page.",
                    },
                    "expected_revision": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                        "description": (
                            "Revision returned by the first page; detects "
                            "file changes between continuation reads."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            preflight=lambda arguments: _preflight_read_file(root, arguments),
            handler=partial(_read_file, root),
            execution_backend=process_backend,
            failure_effect_disposition="none",
        )
    )
    if enable_write_file:
        registry.register(
            ToolDefinition(
                name="write_file",
                description=(
                    "Atomically write a UTF-8 text file inside the configured workspace. "
                    "This tool always requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative destination path.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Complete UTF-8 text content to write.",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                preflight=lambda arguments: _preflight_write_file(root, arguments),
                handler=partial(_write_file, root),
                requires_approval=True,
                execution_backend=process_backend,
                failure_effect_disposition="unknown",
            )
        )
    return registry


def build_readonly_file_registry(
    workspace_root: Path,
    *,
    auto_repair_names: bool = True,
    max_result_chars: int = 12_000,
    name_repair_threshold: float = 0.84,
    timeout_seconds: float = 30.0,
) -> ToolRegistry:
    return build_file_registry(
        workspace_root,
        auto_repair_names=auto_repair_names,
        enable_write_file=False,
        max_result_chars=max_result_chars,
        name_repair_threshold=name_repair_threshold,
        timeout_seconds=timeout_seconds,
    )


def _resolve_inside(root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolInputError("path must be a non-empty string")
    requested = Path(raw_path)
    if requested.is_absolute():
        raise ToolInputError("absolute paths are not allowed")
    resolved = (root / requested).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolInputError("path escapes the configured workspace") from exc
    return resolved


def _relative_display(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    return "." if str(relative) == "." else relative.as_posix()


def _preflight_list_files(
    root: Path,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    directory = _resolve_inside(root, arguments.get("path", "."))
    if not directory.exists():
        raise ToolInputError(f"path does not exist: {_relative_display(root, directory)}")
    if not directory.is_dir():
        raise ToolInputError(f"path is not a directory: {_relative_display(root, directory)}")
    return {
        **arguments,
        "path": _relative_display(root, directory),
    }


def _preflight_read_file(
    root: Path,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    path = _resolve_inside(root, arguments.get("path"))
    if not path.exists():
        raise ToolInputError(f"file does not exist: {_relative_display(root, path)}")
    if not path.is_file():
        raise ToolInputError(f"path is not a file: {_relative_display(root, path)}")
    return {
        **arguments,
        "path": _relative_display(root, path),
    }


def _preflight_write_file(
    root: Path,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    path = _resolve_inside(root, arguments.get("path"))
    if path.exists() and not path.is_file():
        raise ToolInputError(f"path is not a file: {_relative_display(root, path)}")
    return {
        **arguments,
        "path": _relative_display(root, path),
    }


def _list_files(root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    directory = _resolve_inside(root, arguments.get("path", "."))
    if not directory.exists():
        raise ToolInputError(f"path does not exist: {_relative_display(root, directory)}")
    if not directory.is_dir():
        raise ToolInputError(f"path is not a directory: {_relative_display(root, directory)}")

    recursive = arguments.get("recursive", False)
    if not isinstance(recursive, bool):
        raise ToolInputError("recursive must be a boolean")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", MAX_LIST_ENTRIES)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolInputError("offset must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIST_ENTRIES
    ):
        raise ToolInputError(
            f"limit must be an integer between 1 and {MAX_LIST_ENTRIES}"
        )
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    paths = sorted(
        iterator,
        key=lambda item: (item.as_posix().casefold(), item.as_posix()),
    )
    all_entries = [
        {
            "path": _relative_display(root, path),
            "type": "directory" if path.is_dir() else "file",
        }
        for path in paths
    ]
    display_path = _relative_display(root, directory)
    revision = _listing_revision(
        display_path,
        recursive=recursive,
        entries=all_entries,
    )
    expected_revision = arguments.get("expected_revision")
    if offset > 0 and expected_revision is None:
        raise ToolInputError(
            "expected_revision is required when offset is greater than 0"
        )
    if expected_revision is not None and expected_revision != revision:
        raise ToolInputError(
            "directory changed since the previous page; restart at offset 0"
        )
    total_entries = len(all_entries)
    if offset > total_entries:
        raise ToolInputError(
            f"offset {offset} exceeds directory entry count {total_entries}"
        )
    end = min(total_entries, offset + limit)
    entries = all_entries[offset:end]
    eof = end >= total_entries
    return {
        "path": display_path,
        "entries": entries,
        "offset": offset,
        "returned_entries": len(entries),
        "next_offset": None if eof else end,
        "total_entries": total_entries,
        "revision": revision,
        "eof": eof,
        "truncated": not eof,
    }


def _listing_revision(
    path: str,
    *,
    recursive: bool,
    entries: list[dict[str, str]],
) -> str:
    encoded = json.dumps(
        {
            "path": path,
            "recursive": recursive,
            "entries": entries,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _read_file(root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _resolve_inside(root, arguments.get("path"))
    if not path.exists():
        raise ToolInputError(f"file does not exist: {_relative_display(root, path)}")
    if not path.is_file():
        raise ToolInputError(f"path is not a file: {_relative_display(root, path)}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolInputError("file is not valid UTF-8 text") from exc
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", MAX_READ_CHARS)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolInputError("offset must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_READ_CHARS
    ):
        raise ToolInputError(
            f"limit must be an integer between 1 and {MAX_READ_CHARS}"
        )
    total_chars = len(content)
    if offset > total_chars:
        raise ToolInputError(
            f"offset {offset} exceeds file length {total_chars}"
        )
    revision = sha256(content.encode("utf-8")).hexdigest()
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and expected_revision != revision:
        raise ToolInputError(
            "file changed since the previous page; restart at offset 0"
        )
    end = min(total_chars, offset + limit)
    page = content[offset:end]
    eof = end >= total_chars
    return {
        "path": _relative_display(root, path),
        "content": page,
        "offset": offset,
        "returned_chars": len(page),
        "next_offset": None if eof else end,
        "total_chars": total_chars,
        "revision": revision,
        "eof": eof,
        "truncated": not eof,
    }


def _write_file(root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _resolve_inside(root, arguments.get("path"))
    content = arguments.get("content")
    if not isinstance(content, str):
        raise ToolInputError("content must be a string")
    if path.exists() and not path.is_file():
        raise ToolInputError(f"path is not a file: {_relative_display(root, path)}")

    existed = path.exists()
    previous_mode = path.stat().st_mode if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".mini-harness-tmp-",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temporary_path, previous_mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "path": _relative_display(root, path),
        "characters": len(content),
        "operation": "overwritten" if existed else "created",
        "atomic_visibility": True,
        "crash_durability": "best_effort_without_directory_fsync",
    }
