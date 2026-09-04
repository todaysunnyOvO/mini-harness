from __future__ import annotations

from typing import Any, Mapping

from ..execution import SpawnProcessExecutionBackend
from ..memory import MemoryInputError, MemoryStore, MemoryUpdate
from .registry import ToolDefinition, ToolInputError, ToolRegistry


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
) -> None:
    """Register profile-scoped Memory tools only when Memory is configured."""

    backend = SpawnProcessExecutionBackend()
    registry.register(
        ToolDefinition(
            name="memory_read",
            description=(
                "Read the current profile's persistent USER.md preferences or "
                "MEMORY.md long-term facts. Memory is profile-scoped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document": {
                        "type": "string",
                        "enum": ["user", "memory"],
                        "description": (
                            "Use user for USER.md preferences or memory for "
                            "MEMORY.md facts."
                        ),
                    }
                },
                "required": ["document"],
                "additionalProperties": False,
            },
            preflight=lambda arguments: _preflight_read(store, arguments),
            handler=_MemoryReadHandler(store),
            execution_backend=backend,
            failure_effect_disposition="none",
        )
    )
    registry.register(
        ToolDefinition(
            name="memory_update",
            description=(
                "Append, replace, or delete one persistent profile Memory "
                "document. This changes future Sessions and requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["append", "replace", "delete"],
                    },
                    "document": {
                        "type": "string",
                        "enum": ["user", "memory"],
                    },
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["operation", "document"],
                "additionalProperties": False,
                "oneOf": [
                    {
                        "properties": {
                            "operation": {
                                "enum": ["append", "replace"],
                            }
                        },
                        "required": ["content"],
                    },
                    {
                        "properties": {
                            "operation": {"const": "delete"},
                        },
                        "not": {"required": ["content"]},
                    },
                ],
            },
            preflight=lambda arguments: _preflight_update(store, arguments),
            handler=_MemoryUpdateHandler(store),
            requires_approval=True,
            execution_backend=backend,
            failure_effect_disposition="unknown",
        )
    )


class _MemoryReadHandler:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def __call__(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        try:
            return self.store.read(str(arguments["document"]))
        except MemoryInputError as exc:
            raise ToolInputError(str(exc)) from exc


class _MemoryUpdateHandler:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def __call__(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        try:
            update = self.store.update(
                operation=str(arguments["operation"]),
                document=str(arguments["document"]),
                content=arguments.get("content"),
            )
        except MemoryInputError as exc:
            raise ToolInputError(str(exc)) from exc
        return _update_payload(update)


def _preflight_read(
    store: MemoryStore,
    arguments: Mapping[str, Any],
) -> dict[str, str]:
    document = str(arguments["document"]).strip().lower()
    try:
        store.read(document)
    except MemoryInputError as exc:
        raise ToolInputError(str(exc)) from exc
    return {"document": document}


def _preflight_update(
    store: MemoryStore,
    arguments: Mapping[str, Any],
) -> dict[str, str]:
    try:
        return store.preview_update(
            operation=str(arguments["operation"]),
            document=str(arguments["document"]),
            content=arguments.get("content"),
        )
    except MemoryInputError as exc:
        raise ToolInputError(str(exc)) from exc


def _update_payload(update: MemoryUpdate) -> dict[str, object]:
    return {
        "profile_id": update.profile_id,
        "operation": update.operation,
        "document": (
            "USER.md" if update.document == "user" else "MEMORY.md"
        ),
        "content": update.content,
        "characters": len(update.content),
        "revision": update.revision,
    }
