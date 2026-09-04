from __future__ import annotations

import json
import math
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolResultPruningReport:
    protected_recent_tool_results: int = 0
    pruned_old_tool_results: int = 0
    deduplicated_tool_results: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.pruned_old_tool_results
            or self.deduplicated_tool_results
        )


def prune_tool_results(
    messages: list[dict[str, Any]],
    *,
    max_tool_result_tokens: int,
    recent_tool_tail_tokens: int,
    approximate_chars_per_token: float,
    min_recent_turns: int,
) -> ToolResultPruningReport:
    """Prune only old Tool Results in a disposable API message copy.

    The newest complete User Turns are protected first. Remaining recent Tool
    Results are admitted from newest to oldest until the recent-tail budget is
    exhausted. Duplicate outputs keep their newest complete copy and replace
    older copies with a structured back-reference.
    """

    tool_indexes = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool"
    ]
    if not tool_indexes:
        return ToolResultPruningReport()

    recent_turn_ids = _recent_turn_ids(messages, min_recent_turns)
    protected: set[int] = {
        index
        for index in tool_indexes
        if _message_turn_id(messages, index) in recent_turn_ids
    }
    recent_tokens_used = sum(
        _content_tokens(
            messages[index].get("content"),
            approximate_chars_per_token,
        )
        for index in protected
    )
    for index in reversed(tool_indexes):
        if index in protected:
            continue
        tokens = _content_tokens(
            messages[index].get("content"),
            approximate_chars_per_token,
        )
        if recent_tokens_used + tokens > recent_tool_tail_tokens:
            continue
        protected.add(index)
        recent_tokens_used += tokens

    protected_recent_count = len(protected)
    seen_content: dict[str, int] = {}
    deduplicated = 0
    deduplicated_indexes: set[int] = set()
    for index in reversed(tool_indexes):
        content = messages[index].get("content")
        if not isinstance(content, str):
            continue
        digest = sha256(content.encode("utf-8")).hexdigest()
        newer_index = seen_content.get(digest)
        if newer_index is None:
            seen_content[digest] = index
            continue
        # The contract for repeated output is explicit: the newest matching
        # output remains complete, even if it sits just outside the recent
        # tail. Only the older copies become back-references.
        protected.add(newer_index)
        effect = tool_effect_disposition(messages[index])
        messages[index]["content"] = _duplicate_back_reference(effect)
        deduplicated_indexes.add(index)
        deduplicated += 1

    pruned = 0
    for index in tool_indexes:
        if index in protected or index in deduplicated_indexes:
            continue
        content = messages[index].get("content")
        tokens = _content_tokens(content, approximate_chars_per_token)
        if tokens <= max_tool_result_tokens:
            continue
        messages[index]["content"] = _informative_tool_summary(
            messages[index]
        )
        pruned += 1

    return ToolResultPruningReport(
        protected_recent_tool_results=protected_recent_count,
        pruned_old_tool_results=pruned,
        deduplicated_tool_results=deduplicated,
    )


def tool_effect_disposition(message: Mapping[str, Any]) -> str:
    payload = _content_payload(message)
    if payload is None:
        return "unknown"
    meta = payload.get("meta")
    if isinstance(meta, Mapping):
        effect = meta.get("effect_disposition")
        if effect in {"none", "completed", "unknown"}:
            return str(effect)
    if payload.get("ok") is True:
        return "completed"
    return "unknown"


def _recent_turn_ids(
    messages: list[dict[str, Any]],
    minimum: int,
) -> frozenset[int]:
    turn_ids: list[int] = []
    current_turn = -1
    for message in messages:
        if message.get("role") == "user":
            current_turn += 1
            turn_ids.append(current_turn)
    return frozenset(turn_ids[-minimum:])


def _message_turn_id(
    messages: list[dict[str, Any]],
    target_index: int,
) -> int:
    current_turn = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            current_turn += 1
        if index == target_index:
            return current_turn
    return -1


def _content_tokens(
    content: object,
    approximate_chars_per_token: float,
) -> int:
    if not isinstance(content, str):
        content = json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return max(
        1,
        math.ceil(len(content) / approximate_chars_per_token),
    )


def _content_payload(
    message: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _duplicate_back_reference(effect_disposition: str) -> str:
    message = "[Duplicate tool output — same content as a newer call]"
    if effect_disposition == "unknown":
        message += (
            " Outcome remains unknown. Do not retry automatically."
        )
    return json.dumps(
        {
            "message": message,
            "meta": {
                "context_deduplicated": True,
                "effect_disposition": effect_disposition,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _informative_tool_summary(message: Mapping[str, Any]) -> str:
    name = _safe_inline(message.get("name") or "unknown_tool", 80)
    content = message.get("content")
    original_chars = len(content) if isinstance(content, str) else 0
    payload = _content_payload(message)
    effect = tool_effect_disposition(message)
    ok = payload.get("ok") if payload is not None else None
    result_state = "completed" if ok is True else "error"
    result = payload.get("result") if payload is not None else None
    path = (
        _safe_inline(result.get("path"), 240)
        if isinstance(result, Mapping) and result.get("path") is not None
        else ""
    )

    if name == "read_file":
        summary = (
            f"[read_file] path={path or '(unknown)'}, "
            f"original_chars={original_chars}, result={result_state}, "
            "old content pruned"
        )
    elif name == "list_files":
        entries = (
            result.get("entries")
            if isinstance(result, Mapping)
            else None
        )
        entry_count = len(entries) if isinstance(entries, list) else "unknown"
        total_entries = (
            result.get("total_entries", "unknown")
            if isinstance(result, Mapping)
            else "unknown"
        )
        offset = (
            result.get("offset", "unknown")
            if isinstance(result, Mapping)
            else "unknown"
        )
        summary = (
            f"[list_files] path={path or '(unknown)'}, "
            f"offset={offset}, entries={entry_count}, "
            f"total_entries={total_entries}, result={result_state}, "
            "old listing pruned"
        )
    elif name == "write_file":
        summary = (
            f"[write_file] path={path or '(unknown)'}, "
            f"result={result_state}, "
            f"effect_disposition={effect}, old result pruned"
        )
    else:
        summary = (
            f"[{name}] result={result_state}, "
            f"original_chars={original_chars}, "
            f"effect_disposition={effect}, old result pruned"
        )
    if effect == "unknown":
        summary += " Outcome remains unknown. Do not retry automatically."
    return json.dumps(
        {
            "message": summary,
            "meta": {
                "context_pruned": True,
                "effect_disposition": effect,
                "original_chars": original_chars,
                "tool_name": name,
                "result": result_state,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _safe_inline(value: object, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
