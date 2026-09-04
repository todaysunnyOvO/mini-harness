from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ProviderConfig


class ProviderError(RuntimeError):
    """A safe, user-facing provider failure."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.attempts = max(1, int(attempts))
        self.retryable = bool(retryable)


class _RetryableProviderError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


@dataclass(frozen=True)
class ProviderResponse:
    content: str | None
    tool_calls: tuple["ToolCall", ...]
    raw: Mapping[str, Any]
    attempt_count: int = 1


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


class OpenAICompatibleProvider:
    """Minimal non-streaming Chat Completions transport."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._opener = opener
        self._sleeper = sleeper

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ProviderResponse:
        payload = {
            "model": self._config.model,
            "messages": list(messages),
        }
        if tools:
            payload["tools"] = list(tools)
        data, attempt_count = self._request(payload)
        content, tool_calls = _extract_assistant_message(data)
        return ProviderResponse(
            content=content,
            tool_calls=tool_calls,
            raw=data,
            attempt_count=attempt_count,
        )

    def summarize(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_output_tokens: int,
    ) -> str:
        """Run a tool-free summary request through the configured model.

        ``max_output_tokens`` is advisory and is carried by the compression
        prompt. It must not become a wire-level generation cap: reasoning
        models count hidden thinking against ``max_tokens`` and can exhaust a
        small cap before producing any visible summary text.
        """

        payload = {
            "model": self._config.model,
            "messages": list(messages),
        }
        data, _attempt_count = self._request(payload)
        content, tool_calls = _extract_assistant_message(data)
        if tool_calls:
            raise ProviderError("Summary provider returned unexpected tool calls")
        if content is None:
            raise ProviderError("Summary provider returned no text")
        return content

    def _request(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], int]:
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return self._request_once(payload), attempt
            except _RetryableProviderError as exc:
                if attempt >= self._config.max_attempts:
                    raise ProviderError(
                        f"{exc} after {attempt} attempts",
                        attempts=attempt,
                        retryable=True,
                    ) from exc
                delay = self._config.retry_backoff_seconds * (2 ** (attempt - 1))
                if delay > 0:
                    self._sleeper(delay)
        raise AssertionError("Provider retry loop exited unexpectedly")

    def _request_once(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._config.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with self._opener(request, timeout=self._config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = _read_http_error(exc)
            message = f"Provider returned HTTP {exc.code}: {detail}"
            if exc.code in {408, 425, 429} or 500 <= exc.code <= 599:
                raise _RetryableProviderError(message) from exc
            raise ProviderError(message) from exc
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ProviderError(
                    f"Provider TLS certificate verification failed: {exc.reason}"
                ) from exc
            raise _RetryableProviderError(
                f"Could not reach provider: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise _RetryableProviderError("Provider request timed out") from exc
        except OSError as exc:
            raise _RetryableProviderError(
                f"Provider transport failed: {exc}"
            ) from exc

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise ProviderError("Provider response must be a JSON object")
        return data


def _read_http_error(error: HTTPError) -> str:
    try:
        raw = error.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return error.reason or "request failed"
    if not raw:
        return error.reason or "request failed"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    if isinstance(data, Mapping):
        error_obj = data.get("error")
        if isinstance(error_obj, Mapping) and error_obj.get("message"):
            return str(error_obj["message"])[:500]
        if data.get("message"):
            return str(data["message"])[:500]
    return raw[:500]


def _extract_assistant_message(
    data: object,
) -> tuple[str | None, tuple[ToolCall, ...]]:
    if not isinstance(data, Mapping):
        raise ProviderError("Provider response must be a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("Provider response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ProviderError("Provider response choice is invalid")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ProviderError("Provider response choice has no message")
    content_raw = message.get("content")
    content = content_raw if isinstance(content_raw, str) and content_raw.strip() else None

    tool_calls_raw = message.get("tool_calls") or []
    if not isinstance(tool_calls_raw, list):
        raise ProviderError("Provider response tool_calls must be a list")
    tool_calls: list[ToolCall] = []
    for index, raw_call in enumerate(tool_calls_raw):
        if not isinstance(raw_call, Mapping):
            raise ProviderError(f"Provider tool call {index} is invalid")
        call_id = raw_call.get("id")
        function = raw_call.get("function")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ProviderError(f"Provider tool call {index} has no id")
        if not isinstance(function, Mapping):
            raise ProviderError(f"Provider tool call {index} has no function")
        name = function.get("name")
        arguments_raw = function.get("arguments", "{}")
        if not isinstance(name, str) or not name.strip():
            raise ProviderError(f"Provider tool call {index} has no function name")
        if isinstance(arguments_raw, Mapping):
            arguments = json.dumps(arguments_raw, ensure_ascii=False)
        elif isinstance(arguments_raw, str):
            arguments = arguments_raw
        else:
            raise ProviderError(f"Provider tool call {index} has invalid arguments")
        tool_calls.append(
            ToolCall(id=call_id.strip(), name=name.strip(), arguments=arguments)
        )

    if content is None and not tool_calls:
        raise ProviderError("Provider response contains neither text nor tool calls")
    return content, tuple(tool_calls)
