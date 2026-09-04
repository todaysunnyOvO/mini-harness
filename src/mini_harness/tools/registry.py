from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from ..execution import (
    ExecutionBackend,
    ExecutionStartedCallback,
    SpawnProcessExecutionBackend,
)


class ToolInputError(ValueError):
    """A user/model-correctable tool input error."""


ToolHandler = Callable[[Mapping[str, Any]], Any]
ToolPreflight = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ApprovalCallback = Callable[["ToolDefinition", Mapping[str, Any]], bool]
HandlerStartedCallback = Callable[[str], None]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler
    preflight: ToolPreflight | None = None
    requires_approval: bool = False
    execution_backend: ExecutionBackend | None = None
    failure_effect_disposition: str = "unknown"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolExecutionResult:
    ok: bool
    content: str
    requested_name: str
    executed_name: str | None = None
    name_repaired: bool = False
    effect_disposition: str = "none"
    execution_phase: str = "rejected_before_execution"
    execution_backend: str | None = None
    hard_terminated: bool = False
    error_code: str | None = None


class ToolRegistry:
    def __init__(
        self,
        *,
        auto_repair_names: bool = True,
        name_repair_threshold: float = 0.84,
        name_repair_margin: float = 0.08,
        max_result_chars: int = 12_000,
        timeout_seconds: float = 30.0,
        approval_callback: ApprovalCallback | None = None,
        execution_backend: ExecutionBackend | None = None,
    ) -> None:
        if not 0 < name_repair_threshold <= 1:
            raise ValueError("name_repair_threshold must be greater than 0 and at most 1")
        if max_result_chars < 256:
            raise ValueError("max_result_chars must be at least 256")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._tools: dict[str, ToolDefinition] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._auto_repair_names = auto_repair_names
        self._name_repair_threshold = name_repair_threshold
        self._name_repair_margin = name_repair_margin
        self._max_result_chars = max_result_chars
        self._timeout_seconds = timeout_seconds
        self._approval_callback = approval_callback
        self._execution_backend = (
            execution_backend or SpawnProcessExecutionBackend()
        )

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or definition.name in self._tools:
            raise ValueError(f"Tool name is empty or already registered: {definition.name!r}")
        try:
            Draft202012Validator.check_schema(definition.parameters)
        except SchemaError as exc:
            raise ValueError(f"Invalid JSON Schema for tool {definition.name}: {exc.message}") from exc
        if definition.failure_effect_disposition not in {"none", "unknown"}:
            raise ValueError(
                "failure_effect_disposition must be 'none' or 'unknown'"
            )
        self._tools[definition.name] = definition
        self._validators[definition.name] = Draft202012Validator(definition.parameters)

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.schema() for definition in self._tools.values()]

    def dispatch(
        self,
        name: str,
        arguments_json: str,
        *,
        on_handler_start: HandlerStartedCallback | None = None,
    ) -> ToolExecutionResult:
        requested_name = name
        resolved_name = self._resolve_name(name)
        definition = self._tools.get(resolved_name) if resolved_name else None
        if definition is None:
            available = ", ".join(self._tools) or "none"
            return _error(
                "unknown_tool",
                f"Tool is not registered: {name}. Available tools: {available}",
                requested_name=requested_name,
                max_chars=self._max_result_chars,
            )

        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return _error(
                "invalid_json",
                f"Tool arguments are not valid JSON: {exc.msg}",
                requested_name=requested_name,
                executed_name=resolved_name,
                max_chars=self._max_result_chars,
            )
        if not isinstance(arguments, dict):
            return _error(
                "invalid_arguments",
                "Tool arguments must be a JSON object",
                requested_name=requested_name,
                executed_name=resolved_name,
                max_chars=self._max_result_chars,
            )

        validation_error = next(self._validators[resolved_name].iter_errors(arguments), None)
        if validation_error is not None:
            return _error(
                "schema_validation",
                _format_validation_error(validation_error),
                requested_name=requested_name,
                executed_name=resolved_name,
                max_chars=self._max_result_chars,
            )

        if definition.preflight is not None:
            try:
                preflight_arguments = definition.preflight(dict(arguments))
            except ToolInputError as exc:
                return _error(
                    "tool_input_error",
                    str(exc),
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )
            except Exception as exc:
                return _error(
                    "preflight_error",
                    f"Tool preflight failed closed: {type(exc).__name__}: {exc}",
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )
            if not isinstance(preflight_arguments, Mapping):
                return _error(
                    "preflight_error",
                    "Tool preflight must return a JSON object",
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )
            arguments = dict(preflight_arguments)
            validation_error = next(
                self._validators[resolved_name].iter_errors(arguments),
                None,
            )
            if validation_error is not None:
                return _error(
                    "preflight_schema_validation",
                    _format_validation_error(validation_error),
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )

        if definition.requires_approval:
            if self._approval_callback is None:
                return _error(
                    "approval_required",
                    f"Tool requires approval before execution: {resolved_name}",
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )
            try:
                approved = self._approval_callback(definition, arguments)
            except Exception as exc:
                return _error(
                    "approval_error",
                    f"Approval check failed closed: {type(exc).__name__}: {exc}",
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )
            if not approved:
                return _error(
                    "approval_denied",
                    f"User denied execution of {resolved_name}",
                    requested_name=requested_name,
                    executed_name=resolved_name,
                    max_chars=self._max_result_chars,
                )

        backend = definition.execution_backend or self._execution_backend
        backend_start_callback: ExecutionStartedCallback | None = None
        if on_handler_start is not None:
            backend_start_callback = lambda: on_handler_start(backend.name)
        outcome = backend.execute(
            definition.handler,
            arguments,
            timeout_seconds=self._timeout_seconds,
            on_started=backend_start_callback,
        )
        if outcome.kind == "start_error":
            return _error(
                "execution_start_error",
                (
                    "Tool execution was blocked before Handler entry: "
                    f"{outcome.error_type or 'ExecutionStartError'}: "
                    f"{outcome.error_message or 'unknown start error'}"
                ),
                requested_name=requested_name,
                executed_name=resolved_name,
                execution_backend=outcome.backend_name,
                hard_terminated=outcome.hard_terminated,
                max_chars=self._max_result_chars,
            )
        if outcome.kind == "timeout":
            termination = (
                "the isolated execution process was terminated"
                if outcome.hard_terminated
                else "the Handler may still be running"
            )
            return _error(
                "tool_timeout",
                (
                    f"Tool exceeded timeout of {self._timeout_seconds:g} "
                    f"seconds; {termination}"
                ),
                requested_name=requested_name,
                executed_name=resolved_name,
                effect_disposition=definition.failure_effect_disposition,
                execution_phase="result_unavailable",
                execution_backend=outcome.backend_name,
                hard_terminated=outcome.hard_terminated,
                max_chars=self._max_result_chars,
            )
        if outcome.kind == "tool_input_error":
            return _error(
                "tool_input_error",
                outcome.error_message or "Tool input was rejected by the Handler",
                requested_name=requested_name,
                executed_name=resolved_name,
                effect_disposition=definition.failure_effect_disposition,
                execution_phase="result_unavailable",
                execution_backend=outcome.backend_name,
                hard_terminated=outcome.hard_terminated,
                max_chars=self._max_result_chars,
            )
        if outcome.kind != "success":
            return _error(
                "handler_error",
                (
                    f"{outcome.error_type or 'HandlerError'}: "
                    f"{outcome.error_message or 'unknown Handler failure'}"
                ),
                requested_name=requested_name,
                executed_name=resolved_name,
                effect_disposition=definition.failure_effect_disposition,
                execution_phase="result_unavailable",
                execution_backend=outcome.backend_name,
                hard_terminated=outcome.hard_terminated,
                max_chars=self._max_result_chars,
            )
        value = outcome.value

        repaired = resolved_name != requested_name
        payload: dict[str, Any] = {
            "ok": True,
            "result": value,
            "meta": {
                "effect_disposition": "completed",
                "execution_phase": "handler_completed",
                "requested_tool": requested_name,
                "executed_tool": resolved_name,
                "name_repaired": repaired,
                "execution_backend": outcome.backend_name,
                "hard_terminated": outcome.hard_terminated,
            },
        }
        return ToolExecutionResult(
            ok=True,
            content=_bounded_json(payload, self._max_result_chars),
            requested_name=requested_name,
            executed_name=resolved_name,
            name_repaired=repaired,
            effect_disposition="completed",
            execution_phase="handler_completed",
            execution_backend=outcome.backend_name,
            hard_terminated=outcome.hard_terminated,
        )

    def error_result(
        self,
        *,
        requested_name: str,
        code: str,
        message: str,
        effect_disposition: str = "none",
    ) -> ToolExecutionResult:
        return _error(
            code,
            message,
            requested_name=requested_name,
            effect_disposition=effect_disposition,
            max_chars=self._max_result_chars,
        )

    def _resolve_name(self, requested_name: str) -> str | None:
        if requested_name in self._tools:
            return requested_name
        if not self._auto_repair_names or not self._tools:
            return None

        normalized = requested_name.strip().lower()
        ranked = sorted(
            (
                (SequenceMatcher(None, normalized, candidate.lower()).ratio(), candidate)
                for candidate in self._tools
            ),
            reverse=True,
        )
        best_score, best_name = ranked[0]
        if best_score < self._name_repair_threshold:
            return None
        if len(ranked) > 1 and best_score - ranked[1][0] < self._name_repair_margin:
            return None
        return best_name


def _format_validation_error(error: ValidationError) -> str:
    location = ".".join(str(part) for part in error.absolute_path)
    prefix = f"arguments.{location}" if location else "arguments"
    return f"{prefix}: {error.message}"


def _error(
    code: str,
    message: str,
    *,
    requested_name: str,
    executed_name: str | None = None,
    effect_disposition: str = "none",
    execution_phase: str = "rejected_before_execution",
    execution_backend: str | None = None,
    hard_terminated: bool = False,
    max_chars: int = 12_000,
) -> ToolExecutionResult:
    repaired = executed_name is not None and executed_name != requested_name
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message},
        "meta": {
            "effect_disposition": effect_disposition,
            "execution_phase": execution_phase,
            "requested_tool": requested_name,
            "executed_tool": executed_name,
            "name_repaired": repaired,
            "execution_backend": execution_backend,
            "hard_terminated": hard_terminated,
        },
    }
    return ToolExecutionResult(
        ok=False,
        content=_bounded_json(payload, max_chars),
        requested_name=requested_name,
        executed_name=executed_name,
        name_repaired=repaired,
        effect_disposition=effect_disposition,
        execution_phase=execution_phase,
        execution_backend=execution_backend,
        hard_terminated=hard_terminated,
        error_code=code,
    )


def _bounded_json(payload: dict[str, Any], max_chars: int) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return encoded

    if payload.get("ok"):
        paged = _bounded_text_page(payload, max_chars)
        if paged is None:
            paged = _bounded_list_page(payload, max_chars)
        if paged is not None:
            return paged
        raw_result = json.dumps(payload.get("result"), ensure_ascii=False, default=str)
        preview_size = max(0, max_chars - 240)
        bounded = dict(payload)
        bounded["result"] = {
            "truncated": True,
            "original_chars": len(raw_result),
            "preview": raw_result[:preview_size],
        }
        encoded = json.dumps(bounded, ensure_ascii=False, default=str)
        while len(encoded) > max_chars and preview_size > 0:
            preview_size = max(0, preview_size - (len(encoded) - max_chars) - 8)
            bounded["result"]["preview"] = raw_result[:preview_size]
            encoded = json.dumps(bounded, ensure_ascii=False, default=str)
        if len(encoded) > max_chars:
            bounded["result"].pop("preview", None)
            encoded = json.dumps(bounded, ensure_ascii=False, default=str)
        if len(encoded) <= max_chars:
            return encoded

    fallback = {
        "ok": False,
        "error": {
            "code": "result_too_large",
            "message": "Tool result exceeded the configured output budget",
        },
        "meta": {
            "effect_disposition": payload.get("meta", {}).get(
                "effect_disposition",
                "unknown",
            ),
            "execution_phase": payload.get("meta", {}).get(
                "execution_phase",
                "result_unavailable",
            ),
            "requested_tool": payload.get("meta", {}).get("requested_tool"),
            "executed_tool": payload.get("meta", {}).get("executed_tool"),
            "name_repaired": payload.get("meta", {}).get("name_repaired", False),
            "execution_backend": payload.get("meta", {}).get(
                "execution_backend"
            ),
            "hard_terminated": payload.get("meta", {}).get(
                "hard_terminated",
                False,
            ),
        },
    }
    encoded_fallback = json.dumps(fallback, ensure_ascii=False)
    if len(encoded_fallback) <= max_chars:
        return encoded_fallback
    compact_fallback = {
        "ok": False,
        "error": {
            "code": "result_too_large",
            "message": "Tool result exceeded output budget",
        },
        "meta": {
            "effect_disposition": fallback["meta"]["effect_disposition"],
            "execution_phase": fallback["meta"]["execution_phase"],
        },
    }
    return json.dumps(compact_fallback, ensure_ascii=False)


def _bounded_text_page(
    payload: Mapping[str, Any],
    max_chars: int,
) -> str | None:
    """Shrink a resumable text page without destroying cursor metadata."""

    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    required = {
        "content",
        "offset",
        "returned_chars",
        "next_offset",
        "total_chars",
        "revision",
        "eof",
        "truncated",
    }
    if not required.issubset(result):
        return None
    content = result.get("content")
    offset = result.get("offset")
    total_chars = result.get("total_chars")
    if (
        not isinstance(content, str)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or isinstance(total_chars, bool)
        or not isinstance(total_chars, int)
        or offset < 0
        or total_chars < offset
    ):
        return None

    def encode_prefix(char_count: int) -> str:
        bounded_payload = dict(payload)
        bounded_result = dict(result)
        page = content[:char_count]
        next_offset = offset + len(page)
        eof = next_offset >= total_chars
        bounded_result.update(
            {
                "content": page,
                "returned_chars": len(page),
                "next_offset": None if eof else next_offset,
                "eof": eof,
                "truncated": not eof,
            }
        )
        bounded_payload["result"] = bounded_result
        return json.dumps(
            bounded_payload,
            ensure_ascii=False,
            default=str,
        )

    low = 0
    high = len(content)
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = encode_prefix(middle)
        if len(candidate) <= max_chars:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _bounded_list_page(
    payload: Mapping[str, Any],
    max_chars: int,
) -> str | None:
    """Shrink a directory page while preserving a usable entry cursor."""

    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    required = {
        "entries",
        "offset",
        "returned_entries",
        "next_offset",
        "total_entries",
        "revision",
        "eof",
        "truncated",
    }
    if not required.issubset(result):
        return None
    entries = result.get("entries")
    offset = result.get("offset")
    total_entries = result.get("total_entries")
    if (
        not isinstance(entries, list)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or isinstance(total_entries, bool)
        or not isinstance(total_entries, int)
        or offset < 0
        or total_entries < offset
    ):
        return None

    def encode_prefix(entry_count: int) -> str:
        bounded_payload = dict(payload)
        bounded_result = dict(result)
        page = entries[:entry_count]
        next_offset = offset + len(page)
        eof = next_offset >= total_entries
        bounded_result.update(
            {
                "entries": page,
                "returned_entries": len(page),
                "next_offset": None if eof else next_offset,
                "eof": eof,
                "truncated": not eof,
            }
        )
        bounded_payload["result"] = bounded_result
        return json.dumps(
            bounded_payload,
            ensure_ascii=False,
            default=str,
        )

    low = 0
    high = len(entries)
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = encode_prefix(middle)
        makes_progress = middle > 0 or offset >= total_entries
        if len(candidate) <= max_chars and makes_progress:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best
