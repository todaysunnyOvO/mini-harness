from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .context_budget import (
    ContextBudgetPolicy,
    ContextBudgetReport,
    apply_context_budget,
)


_MESSAGE_FIELDS = {
    "system": frozenset({"role", "content"}),
    "user": frozenset({"role", "content"}),
    "assistant": frozenset({"role", "content", "tool_calls"}),
    "tool": frozenset({"role", "content", "tool_call_id", "name"}),
}
_TOOL_CALL_FIELDS = frozenset({"id", "type", "function"})
_FUNCTION_FIELDS = frozenset({"name", "arguments"})


@dataclass(frozen=True)
class APIMessageBuildReport:
    dropped_messages: int = 0
    dropped_fields: int = 0
    inserted_unknown_results: int = 0
    dropped_orphan_results: int = 0
    repaired_tool_call_ids: int = 0
    merged_adjacent_users: int = 0
    context_budget: ContextBudgetReport | None = None

    @property
    def changed(self) -> bool:
        return any(
            (
                self.dropped_messages,
                self.dropped_fields,
                self.inserted_unknown_results,
                self.dropped_orphan_results,
                self.repaired_tool_call_ids,
                self.merged_adjacent_users,
                self.context_budget is not None
                and self.context_budget.changed,
            )
        )


@dataclass(frozen=True)
class APIMessageBuildResult:
    messages: tuple[dict[str, Any], ...]
    report: APIMessageBuildReport
    context_checkpoint: frozenset[str] = frozenset()


class APIMessageBuilder:
    """Build a disposable, protocol-safe Provider message sequence."""

    def __init__(
        self,
        *,
        context_budget: ContextBudgetPolicy | None = None,
    ) -> None:
        self._context_budget = context_budget

    def build(
        self,
        conversation_history: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        context_checkpoint: frozenset[str] = frozenset(),
    ) -> APIMessageBuildResult:
        sanitized: list[dict[str, Any]] = []
        dropped_messages = 0
        dropped_fields = 0

        for message_index, raw_message in enumerate(conversation_history):
            message, field_count = _sanitize_message(raw_message, message_index)
            dropped_fields += field_count
            if message is None:
                dropped_messages += 1
                continue
            sanitized.append(message)

        repaired: list[dict[str, Any]] = []
        pending: list[dict[str, str]] = []
        seen_call_ids: set[str] = set()
        inserted_unknown = 0
        dropped_orphans = 0
        repaired_ids = 0
        merged_users = 0

        def flush_pending() -> None:
            nonlocal inserted_unknown
            for call in pending:
                repaired.append(
                    _unknown_result_message(
                        call_id=call["normalized_id"],
                        tool_name=call["name"],
                    )
                )
                inserted_unknown += 1
            pending.clear()

        for message in sanitized:
            role = message["role"]
            if role != "tool":
                flush_pending()

            if role == "assistant" and message.get("tool_calls"):
                normalized_calls: list[dict[str, Any]] = []
                for call in message["tool_calls"]:
                    original_id = call["id"]
                    normalized_id = _unique_call_id(original_id, seen_call_ids)
                    seen_call_ids.add(normalized_id)
                    normalized_call = {
                        **call,
                        "id": normalized_id,
                    }
                    if normalized_id != original_id:
                        repaired_ids += 1
                    normalized_calls.append(normalized_call)
                    pending.append(
                        {
                            "original_id": original_id,
                            "normalized_id": normalized_id,
                            "name": normalized_call["function"]["name"],
                        }
                    )
                repaired.append({**message, "tool_calls": normalized_calls})
                continue

            if role == "tool":
                tool_call_id = message.get("tool_call_id", "")
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
                    dropped_orphans += 1
                    continue
                call = pending.pop(match_index)
                normalized_result = {
                    **message,
                    "tool_call_id": call["normalized_id"],
                    "name": call["name"],
                }
                if tool_call_id != call["normalized_id"]:
                    repaired_ids += 1
                repaired.append(normalized_result)
                continue

            if role == "user" and repaired and repaired[-1]["role"] == "user":
                previous = repaired[-1]
                previous["content"] = _merge_user_content(
                    previous.get("content"),
                    message.get("content"),
                )
                merged_users += 1
                continue

            repaired.append(message)

        flush_pending()
        budget_report = None
        output_messages: tuple[dict[str, Any], ...] = tuple(repaired)
        if self._context_budget is not None:
            budget_result = apply_context_budget(
                repaired,
                tools=tools,
                policy=self._context_budget,
                previously_dropped_turn_fingerprints=(
                    context_checkpoint
                ),
            )
            output_messages = budget_result.messages
            budget_report = budget_result.report
            context_checkpoint = frozenset(
                budget_result.dropped_turn_fingerprints
            )
        return APIMessageBuildResult(
            messages=output_messages,
            report=APIMessageBuildReport(
                dropped_messages=dropped_messages,
                dropped_fields=dropped_fields,
                inserted_unknown_results=inserted_unknown,
                dropped_orphan_results=dropped_orphans,
                repaired_tool_call_ids=repaired_ids,
                merged_adjacent_users=merged_users,
                context_budget=budget_report,
            ),
            context_checkpoint=context_checkpoint,
        )


def _sanitize_message(
    raw_message: Mapping[str, Any],
    message_index: int,
) -> tuple[dict[str, Any] | None, int]:
    if not isinstance(raw_message, Mapping):
        return None, 0
    role = raw_message.get("role")
    if role not in _MESSAGE_FIELDS:
        return None, 0

    allowed = _MESSAGE_FIELDS[role]
    dropped_fields = len(set(raw_message) - allowed)
    message: dict[str, Any] = {
        key: _json_copy(value)
        for key, value in raw_message.items()
        if key in allowed and key != "tool_calls"
    }
    message["role"] = role

    if role == "assistant":
        raw_calls = raw_message.get("tool_calls")
        if raw_calls:
            if not isinstance(raw_calls, list):
                return None, dropped_fields + 1
            calls: list[dict[str, Any]] = []
            for call_index, raw_call in enumerate(raw_calls):
                call, nested_dropped = _sanitize_tool_call(
                    raw_call,
                    message_index=message_index,
                    call_index=call_index,
                )
                dropped_fields += nested_dropped
                if call is not None:
                    calls.append(call)
            if calls:
                message["tool_calls"] = calls
        elif "tool_calls" in raw_message:
            dropped_fields += 1
        if not message.get("tool_calls") and not _has_visible_content(message.get("content")):
            return None, dropped_fields

    if role == "tool":
        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            return None, dropped_fields
        message["tool_call_id"] = call_id.strip()

    return message, dropped_fields


def _sanitize_tool_call(
    raw_call: object,
    *,
    message_index: int,
    call_index: int,
) -> tuple[dict[str, Any] | None, int]:
    if not isinstance(raw_call, Mapping):
        return None, 0
    dropped_fields = len(set(raw_call) - _TOOL_CALL_FIELDS)
    raw_function = raw_call.get("function")
    if not isinstance(raw_function, Mapping):
        raw_function = {}
    dropped_fields += len(set(raw_function) - _FUNCTION_FIELDS)

    raw_id = raw_call.get("id")
    call_id = (
        raw_id.strip()
        if isinstance(raw_id, str) and raw_id.strip()
        else f"api_call_{message_index}_{call_index}"
    )
    raw_name = raw_function.get("name")
    name = (
        raw_name.strip()
        if isinstance(raw_name, str) and raw_name.strip()
        else "invalid_tool_call"
    )
    raw_arguments = raw_function.get("arguments", "{}")
    if isinstance(raw_arguments, str):
        arguments = raw_arguments
    elif isinstance(raw_arguments, Mapping):
        arguments = json.dumps(
            raw_arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        arguments = "{}"

    return (
        {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments,
            },
        },
        dropped_fields,
    )


def _unique_call_id(call_id: str, seen: set[str]) -> str:
    if call_id not in seen:
        return call_id
    suffix = 2
    while f"{call_id}__api_{suffix}" in seen:
        suffix += 1
    return f"{call_id}__api_{suffix}"


def _unknown_result_message(*, call_id: str, tool_name: str) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error": {
            "code": "result_unavailable_for_api",
            "message": (
                "A trustworthy result for this prior tool call is unavailable. "
                "It may have succeeded, failed, or never started. Do not assume "
                "it is safe to retry."
            ),
        },
        "meta": {
            "effect_disposition": "unknown",
            "execution_phase": "result_unavailable",
            "api_copy_repair": True,
        },
    }
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def _merge_user_content(first: object, second: object) -> str:
    return (
        f"{_content_text(first)}\n\n"
        f"[Later user message]\n{_content_text(second)}"
    )


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _has_visible_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _json_copy(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
