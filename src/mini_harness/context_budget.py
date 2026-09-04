from __future__ import annotations

import json
import math
from hashlib import sha256
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .context_pruning import (
    prune_tool_results,
    tool_effect_disposition,
)


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """Deterministic input-budget policy for one disposable API request."""

    max_input_tokens: int
    reserved_output_tokens: int
    approximate_chars_per_token: float = 4.0
    max_tool_result_tokens: int = 2_000
    recent_tool_tail_tokens: int = 12_000
    min_recent_turns: int = 1
    compaction_target_ratio: float = 0.8

    def __post_init__(self) -> None:
        if self.max_input_tokens < 256:
            raise ValueError("max_input_tokens must be at least 256")
        if self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must not be negative")
        if self.reserved_output_tokens >= self.max_input_tokens:
            raise ValueError(
                "reserved_output_tokens must be less than max_input_tokens"
            )
        if self.approximate_chars_per_token <= 0:
            raise ValueError(
                "approximate_chars_per_token must be greater than zero"
            )
        if self.max_tool_result_tokens < 32:
            raise ValueError("max_tool_result_tokens must be at least 32")
        if self.recent_tool_tail_tokens < 32:
            raise ValueError("recent_tool_tail_tokens must be at least 32")
        if self.min_recent_turns < 1:
            raise ValueError("min_recent_turns must be at least one")
        if not 0.5 <= self.compaction_target_ratio <= 1:
            raise ValueError(
                "compaction_target_ratio must be between 0.5 and 1"
            )

    @property
    def available_input_tokens(self) -> int:
        return self.max_input_tokens - self.reserved_output_tokens

    @property
    def compaction_target_tokens(self) -> int:
        return math.floor(
            self.available_input_tokens * self.compaction_target_ratio
        )


@dataclass(frozen=True)
class ContextBudgetReport:
    max_input_tokens: int
    available_input_tokens: int
    compaction_target_tokens: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    tool_schema_tokens: int
    compacted_tool_results: int = 0
    protected_recent_tool_results: int = 0
    pruned_old_tool_results: int = 0
    deduplicated_tool_results: int = 0
    dropped_turns: int = 0
    dropped_messages: int = 0
    reused_checkpoint_turns: int = 0
    protected_unknown_results: int = 0
    budget_exceeded: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.pruned_old_tool_results
            or self.deduplicated_tool_results
            or self.dropped_messages
        )


@dataclass(frozen=True)
class ContextBudgetResult:
    messages: tuple[dict[str, Any], ...]
    report: ContextBudgetReport
    dropped_turn_fingerprints: tuple[str, ...] = ()


def apply_context_budget(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None,
    policy: ContextBudgetPolicy,
    previously_dropped_turn_fingerprints: frozenset[str] = frozenset(),
) -> ContextBudgetResult:
    """Compact a disposable message copy without splitting a conversation turn."""

    working = [_json_copy(message) for message in messages]
    tool_schema_tokens = estimate_tool_schema_tokens(tools, policy)
    estimated_before = (
        estimate_message_tokens(working, policy) + tool_schema_tokens
    )

    protected_unknown_results = sum(
        1
        for message in working
        if message.get("role") == "tool"
        and tool_effect_disposition(message) == "unknown"
    )
    pruning_report = prune_tool_results(
        working,
        max_tool_result_tokens=policy.max_tool_result_tokens,
        recent_tool_tail_tokens=policy.recent_tool_tail_tokens,
        approximate_chars_per_token=policy.approximate_chars_per_token,
        min_recent_turns=policy.min_recent_turns,
    )
    compacted_tool_results = (
        pruning_report.pruned_old_tool_results
        + pruning_report.deduplicated_tool_results
    )

    dropped_turns = 0
    dropped_messages = 0
    reused_checkpoint_turns = 0
    estimated_after = (
        estimate_message_tokens(working, policy) + tool_schema_tokens
    )
    dropped_fingerprints: list[str] = []
    if (
        estimated_after > policy.available_input_tokens
        or previously_dropped_turn_fingerprints
    ):
        prefix, turns = _split_prefix_and_turns(working)
        turn_fingerprints = [_turn_fingerprint(turn) for turn in turns]
        protected_indexes = set(
            range(
                max(0, len(turns) - policy.min_recent_turns),
                len(turns),
            )
        )
        protected_indexes.update(
            index
            for index, turn in enumerate(turns)
            if _turn_has_unknown_result(turn)
        )
        retained = [True] * len(turns)
        for index, turn in enumerate(turns):
            if (
                turn_fingerprints[index]
                in previously_dropped_turn_fingerprints
                and index not in protected_indexes
            ):
                retained[index] = False
                dropped_turns += 1
                dropped_messages += len(turn)
                reused_checkpoint_turns += 1
                dropped_fingerprints.append(turn_fingerprints[index])
        working = [
            *prefix,
            *(
                message
                for turn_index, turn in enumerate(turns)
                if retained[turn_index]
                for message in turn
            ),
        ]
        estimated_after = (
            estimate_message_tokens(working, policy) + tool_schema_tokens
        )
        if estimated_after > policy.available_input_tokens:
            for index, turn in enumerate(turns):
                if estimated_after <= policy.compaction_target_tokens:
                    break
                if index in protected_indexes or not retained[index]:
                    continue
                retained[index] = False
                dropped_turns += 1
                dropped_messages += len(turn)
                dropped_fingerprints.append(turn_fingerprints[index])
                candidate = [
                    *prefix,
                    *(
                        message
                        for turn_index, candidate_turn in enumerate(turns)
                        if retained[turn_index]
                        for message in candidate_turn
                    ),
                ]
                estimated_after = (
                    estimate_message_tokens(candidate, policy)
                    + tool_schema_tokens
                )
        working = [
            *prefix,
            *(
                message
                for turn_index, turn in enumerate(turns)
                if retained[turn_index]
                for message in turn
            ),
        ]
        estimated_after = (
            estimate_message_tokens(working, policy) + tool_schema_tokens
        )

    return ContextBudgetResult(
        messages=tuple(working),
        report=ContextBudgetReport(
            max_input_tokens=policy.max_input_tokens,
            available_input_tokens=policy.available_input_tokens,
            compaction_target_tokens=policy.compaction_target_tokens,
            estimated_tokens_before=estimated_before,
            estimated_tokens_after=estimated_after,
            tool_schema_tokens=tool_schema_tokens,
            compacted_tool_results=compacted_tool_results,
            protected_recent_tool_results=(
                pruning_report.protected_recent_tool_results
            ),
            pruned_old_tool_results=(
                pruning_report.pruned_old_tool_results
            ),
            deduplicated_tool_results=(
                pruning_report.deduplicated_tool_results
            ),
            dropped_turns=dropped_turns,
            dropped_messages=dropped_messages,
            reused_checkpoint_turns=reused_checkpoint_turns,
            protected_unknown_results=protected_unknown_results,
            budget_exceeded=(
                estimated_after > policy.available_input_tokens
            ),
        ),
        dropped_turn_fingerprints=tuple(dropped_fingerprints),
    )


def estimate_message_tokens(
    messages: Sequence[Mapping[str, Any]],
    policy: ContextBudgetPolicy,
) -> int:
    return sum(
        estimate_json_tokens(message, policy) + 4
        for message in messages
    )


def estimate_tool_schema_tokens(
    tools: Sequence[Mapping[str, Any]] | None,
    policy: ContextBudgetPolicy,
) -> int:
    if not tools:
        return 0
    return estimate_json_tokens(tools, policy) + 8 * len(tools)


def estimate_json_tokens(value: object, policy: ContextBudgetPolicy) -> int:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return estimate_text_tokens(encoded, policy)


def estimate_text_tokens(value: object, policy: ContextBudgetPolicy) -> int:
    if not isinstance(value, str):
        value = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return max(
        1,
        math.ceil(len(value) / policy.approximate_chars_per_token),
    )


def _split_prefix_and_turns(
    messages: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    prefix: list[dict[str, Any]] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        prefix.append(messages[index])
        index += 1

    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages[index:]:
        if message.get("role") == "user" and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)
    return prefix, turns


def _turn_has_unknown_result(turn: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        message.get("role") == "tool"
        and tool_effect_disposition(message) == "unknown"
        for message in turn
    )


def _turn_fingerprint(turn: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        turn,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _json_copy(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
