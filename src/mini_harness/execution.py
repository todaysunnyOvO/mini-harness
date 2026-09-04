from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from typing import Any, Callable, Mapping, Protocol


ExecutionHandler = Callable[[Mapping[str, Any]], Any]
ExecutionStartedCallback = Callable[[], None]


@dataclass(frozen=True)
class ExecutionOutcome:
    kind: str
    value: Any = None
    error_type: str | None = None
    error_message: str | None = None
    handler_started: bool = False
    hard_terminated: bool = False
    backend_name: str = "unknown"


class ExecutionBackend(Protocol):
    name: str

    def execute(
        self,
        handler: ExecutionHandler,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float,
        on_started: ExecutionStartedCallback | None,
    ) -> ExecutionOutcome: ...


class ThreadExecutionBackend:
    """In-process compatibility backend with no enforceable hard timeout.

    Python cannot safely terminate a running thread. This backend therefore
    waits for the Handler to finish instead of returning a false timeout while
    work continues in the background. Use the default spawn backend whenever a
    hard deadline is required.
    """

    name = "thread"

    def execute(
        self,
        handler: ExecutionHandler,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float,
        on_started: ExecutionStartedCallback | None,
    ) -> ExecutionOutcome:
        try:
            if on_started is not None:
                on_started()
        except Exception as exc:
            return ExecutionOutcome(
                kind="start_error",
                error_type=type(exc).__name__,
                error_message=str(exc),
                backend_name=self.name,
            )

        try:
            value = handler(dict(arguments))
        except Exception as exc:
            return ExecutionOutcome(
                kind=(
                    "tool_input_error"
                    if type(exc).__name__ == "ToolInputError"
                    else "handler_error"
                ),
                error_type=type(exc).__name__,
                error_message=str(exc),
                handler_started=True,
                backend_name=self.name,
            )

        return ExecutionOutcome(
            kind="success",
            value=value,
            handler_started=True,
            backend_name=self.name,
        )


class SpawnProcessExecutionBackend:
    """Spawn a killable Handler process with a parent-controlled start gate."""

    name = "spawn_process"

    def execute(
        self,
        handler: ExecutionHandler,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float,
        on_started: ExecutionStartedCallback | None,
    ) -> ExecutionOutcome:
        try:
            ForkingPickler.dumps(handler)
        except Exception as exc:
            return ExecutionOutcome(
                kind="start_error",
                error_type="HandlerNotSerializable",
                error_message=(
                    "Handler cannot cross the spawn-process boundary: "
                    f"{type(exc).__name__}: {exc}. Use a module-level "
                    "function or explicitly choose ThreadExecutionBackend "
                    "when waiting past the deadline is acceptable."
                ),
                backend_name=self.name,
            )
        context = get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        start_gate = context.Event()
        process = context.Process(
            target=_process_worker,
            args=(
                child_connection,
                start_gate,
                handler,
                dict(arguments),
                max(5.0, min(timeout_seconds + 5.0, 60.0)),
            ),
            name="mini-tool-process",
            daemon=True,
        )
        try:
            try:
                process.start()
            except Exception as exc:
                return ExecutionOutcome(
                    kind="start_error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    backend_name=self.name,
                )
            finally:
                child_connection.close()

            try:
                if on_started is not None:
                    on_started()
            except Exception as exc:
                _terminate_process(process)
                return ExecutionOutcome(
                    kind="start_error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    hard_terminated=True,
                    backend_name=self.name,
                )

            start_gate.set()
            startup_timeout = max(5.0, min(timeout_seconds + 5.0, 30.0))
            if not parent_connection.poll(startup_timeout):
                _terminate_process(process)
                return ExecutionOutcome(
                    kind="start_error",
                    error_type="ProcessStartTimeout",
                    error_message=(
                        "Handler process did not acknowledge the start gate"
                    ),
                    hard_terminated=True,
                    backend_name=self.name,
                )
            try:
                started_payload = parent_connection.recv()
            except EOFError:
                started_payload = {}
            if started_payload.get("kind") != "started":
                _terminate_process(process)
                return ExecutionOutcome(
                    kind="start_error",
                    error_type="ProcessStartError",
                    error_message=(
                        "Handler process exited before entering the Handler"
                    ),
                    hard_terminated=True,
                    backend_name=self.name,
                )

            if parent_connection.poll(timeout_seconds):
                try:
                    payload = parent_connection.recv()
                except EOFError:
                    payload = {
                        "kind": "handler_error",
                        "error_type": "ProcessExitError",
                        "error_message": (
                            "Handler process exited without returning a result"
                        ),
                    }
                process.join(timeout=1.0)
                if process.is_alive():
                    _terminate_process(process)
                return ExecutionOutcome(
                    kind=str(payload.get("kind") or "handler_error"),
                    value=payload.get("value"),
                    error_type=payload.get("error_type"),
                    error_message=payload.get("error_message"),
                    handler_started=True,
                    backend_name=self.name,
                )

            _terminate_process(process)
            return ExecutionOutcome(
                kind="timeout",
                handler_started=True,
                hard_terminated=True,
                backend_name=self.name,
            )
        finally:
            parent_connection.close()
            if process.pid is not None:
                try:
                    process.close()
                except ValueError:
                    pass


def _process_worker(
    connection: Connection,
    start_gate,
    handler: ExecutionHandler,
    arguments: Mapping[str, Any],
    gate_timeout_seconds: float,
) -> None:
    try:
        if not start_gate.wait(gate_timeout_seconds):
            return
        connection.send({"kind": "started"})
        try:
            value = handler(arguments)
            payload = {"kind": "success", "value": value}
        except Exception as exc:
            payload = {
                "kind": (
                    "tool_input_error"
                    if type(exc).__name__ == "ToolInputError"
                    else "handler_error"
                ),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        try:
            connection.send(payload)
        except Exception as exc:
            try:
                connection.send(
                    {
                        "kind": "handler_error",
                        "error_type": type(exc).__name__,
                        "error_message": (
                            "Handler result could not cross the process boundary: "
                            f"{exc}"
                        ),
                    }
                )
            except Exception:
                return
    finally:
        connection.close()


def _terminate_process(process) -> None:
    if not process.is_alive():
        process.join(timeout=0.1)
        return
    process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
