from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from mini_harness.agent import Agent
from mini_harness.config import ProviderConfig
from mini_harness.events import MemoryEventSink
from mini_harness.execution import ThreadExecutionBackend
from mini_harness.provider import OpenAICompatibleProvider, ProviderError
from mini_harness.tools import ToolDefinition, ToolRegistry


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeRawResponse(FakeResponse):
    def __init__(self, body: bytes) -> None:
        self._body = body


class _ScriptedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, responses):
        super().__init__(("127.0.0.1", 0), _ScriptedHandler)
        self.responses = list(responses)
        self.requests = []
        self.response_lock = threading.Lock()


class _ScriptedHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(json.loads(body.decode("utf-8")))
        with self.server.response_lock:
            status, payload = self.server.responses.pop(0)
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args) -> None:
        return


@contextmanager
def _serve_provider(responses):
    server = _ScriptedHTTPServer(responses)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ProviderConfig(
            base_url="https://example.test/v1",
            model="test-model",
            api_key="secret",
            timeout_seconds=3,
        )

    def test_sends_chat_completions_request_and_extracts_text(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        result = provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(result.content, "hello")
        self.assertEqual(result.tool_calls, ())
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["timeout"], 3)
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertEqual(captured["payload"]["messages"][0]["content"], "hi")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")

    def test_surfaces_provider_error_message(self) -> None:
        calls = 0

        def opener(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, 3)
            raise HTTPError(
                url="https://example.test/v1/chat/completions",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"bad key"}}'),
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        with self.assertRaisesRegex(ProviderError, "HTTP 401: bad key"):
            provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(calls, 1)

    def test_real_http_transient_failure_recovers_and_is_observable(self) -> None:
        responses = [
            (503, {"error": {"message": "temporarily unavailable"}}),
            (
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "RECOVERED",
                            }
                        }
                    ]
                },
            ),
        ]
        with _serve_provider(responses) as server:
            provider = OpenAICompatibleProvider(
                replace(
                    self.config,
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    max_attempts=2,
                    retry_backoff_seconds=0,
                )
            )
            events = MemoryEventSink()
            answer = Agent(
                provider,
                system_prompt="system",
                event_sink=events,
            ).chat("recover this request")

        self.assertEqual(answer, "RECOVERED")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(server.requests[0], server.requests[1])
        completed = next(
            event
            for event in events.events
            if event.event == "provider.request_completed"
        )
        self.assertEqual(completed.details["attempt_count"], 2)

    def test_retry_does_not_execute_a_returned_tool_call_twice(self) -> None:
        responses = [
            (503, {"error": {"message": "temporary overload"}}),
            (
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "echo-once",
                                        "type": "function",
                                        "function": {
                                            "name": "echo",
                                            "arguments": '{"text":"once"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            (
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "tool completed",
                            }
                        }
                    ]
                },
            ),
        ]
        handled = []
        registry = ToolRegistry(execution_backend=ThreadExecutionBackend())
        registry.register(
            ToolDefinition(
                name="echo",
                description="Record one value.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=lambda arguments: handled.append(arguments["text"])
                or {"echo": arguments["text"]},
            )
        )
        with _serve_provider(responses) as server:
            provider = OpenAICompatibleProvider(
                replace(
                    self.config,
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    max_attempts=2,
                    retry_backoff_seconds=0,
                )
            )
            answer = Agent(
                provider,
                system_prompt="system",
                tools=registry,
            ).chat("use the tool")

        self.assertEqual(answer, "tool completed")
        self.assertEqual(handled, ["once"])
        self.assertEqual(len(server.requests), 3)

    def test_retryable_failure_exhausts_finite_attempts_with_backoff(self) -> None:
        calls = 0
        delays = []

        def opener(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, 3)
            raise HTTPError(
                url="https://example.test/v1/chat/completions",
                code=503,
                msg="Unavailable",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"overloaded"}}'),
            )

        provider = OpenAICompatibleProvider(
            replace(
                self.config,
                max_attempts=3,
                retry_backoff_seconds=0.25,
            ),
            opener=opener,
            sleeper=delays.append,
        )
        with self.assertRaisesRegex(
            ProviderError,
            "HTTP 503: overloaded after 3 attempts",
        ) as raised:
            provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(calls, 3)
        self.assertEqual(delays, [0.25, 0.5])
        self.assertEqual(raised.exception.attempts, 3)
        self.assertTrue(raised.exception.retryable)

    def test_invalid_success_response_is_not_retried(self) -> None:
        calls = 0

        def opener(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, 3)
            return FakeRawResponse(b"not-json")

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        with self.assertRaisesRegex(ProviderError, "invalid JSON") as raised:
            provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertFalse(raised.exception.retryable)

    def test_parses_tool_calls_without_text_content(self) -> None:
        def opener(_request, timeout):
            self.assertEqual(timeout, 3)
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_7",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"README.md"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        result = provider.complete(
            [{"role": "user", "content": "read the readme"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

        self.assertIsNone(result.content)
        self.assertEqual(result.tool_calls[0].id, "call_7")
        self.assertEqual(result.tool_calls[0].name, "read_file")

    def test_summary_request_is_tool_free_and_avoids_hard_output_cap(self) -> None:
        captured = {}

        def opener(request, timeout):
            self.assertEqual(timeout, 3)
            captured["payload"] = json.loads(
                request.data.decode("utf-8")
            )
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "compact history",
                            }
                        }
                    ]
                }
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        result = provider.summarize(
            [{"role": "user", "content": "history"}],
            max_output_tokens=512,
        )

        self.assertEqual(result, "compact history")
        self.assertNotIn("max_tokens", captured["payload"])
        self.assertNotIn("max_completion_tokens", captured["payload"])
        self.assertNotIn("tools", captured["payload"])

    def test_reasoning_provider_gets_room_to_return_visible_summary(self) -> None:
        captured = {}

        def opener(request, timeout):
            self.assertEqual(timeout, 3)
            captured["payload"] = json.loads(
                request.data.decode("utf-8")
            )
            # Reproduce the provider class behind REAL-015: a small hard cap
            # is consumed by hidden reasoning before visible content exists.
            if "max_tokens" in captured["payload"]:
                return FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "reasoning_content": "hidden reasoning",
                                },
                                "finish_reason": "length",
                            }
                        ]
                    }
                )
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "visible compact history",
                                "reasoning_content": "hidden reasoning",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        result = provider.summarize(
            [{"role": "user", "content": "history"}],
            max_output_tokens=57,
        )

        self.assertEqual(result, "visible compact history")
        self.assertNotIn("max_tokens", captured["payload"])

    def test_summary_rejects_unexpected_tool_call(self) -> None:
        def opener(_request, timeout):
            self.assertEqual(timeout, 3)
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-summary",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        provider = OpenAICompatibleProvider(self.config, opener=opener)
        with self.assertRaisesRegex(
            ProviderError,
            "unexpected tool calls",
        ):
            provider.summarize(
                [{"role": "user", "content": "history"}],
                max_output_tokens=512,
            )



if __name__ == "__main__":
    unittest.main()
