from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardrailDecision:
    blocked: bool
    occurrence: int
    signature: str


class RepeatedCallGuardrail:
    """Blocks one exact tool request after a small per-turn allowance."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least one")
        self._limit = limit
        self._counts: dict[str, int] = {}

    def inspect(self, name: str, arguments_json: str) -> GuardrailDecision:
        signature = tool_call_signature(name, arguments_json)
        occurrence = self._counts.get(signature, 0) + 1
        self._counts[signature] = occurrence
        return GuardrailDecision(
            blocked=occurrence > self._limit,
            occurrence=occurrence,
            signature=signature,
        )


def canonicalize_tool_arguments(arguments_json: str) -> str:
    try:
        value: Any = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return arguments_json
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def tool_arguments_hash(arguments_json: str) -> str:
    canonical = canonicalize_tool_arguments(arguments_json)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tool_call_signature(name: str, arguments_json: str) -> str:
    canonical_arguments = canonicalize_tool_arguments(arguments_json)
    material = f"{name}\0{canonical_arguments}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
