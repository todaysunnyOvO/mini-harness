from __future__ import annotations

from copy import deepcopy
from threading import Event
from time import perf_counter
from typing import Any, Callable, Protocol, Sequence

from .api_messages import APIMessageBuilder
from .context_budget import ContextBudgetPolicy, ContextBudgetReport
from .context_compression import CompressionManager, CompressionResult
from .events import EventSink
from .guardrails import RepeatedCallGuardrail
from .provider import ProviderResponse, ToolCall
from .profiles import normalize_profile_id
from .session_store import RecoveryReport, SessionDB
from .session_store import SessionError
from .skills import SkillInvocation
from .tools import ToolExecutionResult, ToolRegistry


ToolObserver = Callable[["ToolCall", ToolExecutionResult], None]


class AgentLoopError(RuntimeError):
    """Raised when the model does not finish within the iteration limit."""


class ContextBudgetExceeded(AgentLoopError):
    """Raised before a Provider request when protected context cannot fit."""

    def __init__(self, report: ContextBudgetReport) -> None:
        self.report = report
        super().__init__(
            "Protected context exceeds the configured input budget "
            f"({report.estimated_tokens_after} > "
            f"{report.available_input_tokens} approximate tokens)"
        )


class Provider(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ProviderResponse: ...


class Agent:
    """Minimal tool-calling agent with in-memory conversation history."""

    def __init__(
        self,
        provider: Provider,
        *,
        system_prompt: str,
        tools: ToolRegistry | None = None,
        max_iterations: int = 8,
        same_call_limit: int = 2,
        max_calls_per_batch: int = 6,
        max_calls_per_turn: int = 16,
        session_db: SessionDB | None = None,
        session_id: str | None = None,
        event_sink: EventSink | None = None,
        tool_observer: ToolObserver | None = None,
        context_budget: ContextBudgetPolicy | None = None,
        compression_manager: CompressionManager | None = None,
        profile_id: str = "default",
    ) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be greater than zero")
        if same_call_limit < 1:
            raise ValueError("same_call_limit must be at least one")
        if max_calls_per_batch < 1:
            raise ValueError("max_calls_per_batch must be at least one")
        if max_calls_per_turn < 1:
            raise ValueError("max_calls_per_turn must be at least one")
        self._profile_id = normalize_profile_id(profile_id)
        self._provider = provider
        self._api_message_builder = APIMessageBuilder(
            context_budget=context_budget
        )
        self._context_checkpoint: frozenset[str] = frozenset()
        self._tools = tools or ToolRegistry()
        self._max_iterations = max_iterations
        self._same_call_limit = same_call_limit
        self._max_calls_per_batch = max_calls_per_batch
        self._max_calls_per_turn = max_calls_per_turn
        self._tool_observer = tool_observer
        self._session_db = session_db
        self._compression_manager = compression_manager
        self._event_sink = event_sink
        self._interrupt_requested = Event()
        self._last_turn_completed = True
        self._last_stop_reason: str | None = None
        self._last_recovery_report: RecoveryReport | None = None
        if session_db is None:
            self._session_id = None
            self._messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt}
            ]
            self._emit_event("session.in_memory")
        else:
            resumed = session_id is not None and session_db.session_exists(session_id)
            if resumed:
                stored_session = session_db.get_session(session_id)
                if stored_session.profile_id != self._profile_id:
                    raise SessionError(
                        "Session profile mismatch: "
                        f"session={stored_session.profile_id!r}, "
                        f"configured={self._profile_id!r}"
                    )
                session, report = session_db.resume_session(session_id)
                self._session_id = session.id
                self._last_recovery_report = report
                self._messages = [
                    {"role": "system", "content": session.system_prompt},
                    *report.messages,
                ]
            else:
                session = session_db.create_session(
                    system_prompt,
                    session_id=session_id,
                    profile_id=self._profile_id,
                )
                self._session_id = session.id
                self._messages = [
                    {"role": "system", "content": session.system_prompt}
                ]
            self._emit_event(
                "session.resumed" if resumed else "session.created",
                {
                    "message_count": len(self._messages) - 1,
                },
            )
            if self._last_recovery_report is not None:
                self._emit_recovery(self._last_recovery_report)

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(message) for message in self._messages)

    @property
    def last_turn_completed(self) -> bool:
        return self._last_turn_completed

    @property
    def last_stop_reason(self) -> str | None:
        return self._last_stop_reason

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def last_recovery_report(self) -> RecoveryReport | None:
        return self._last_recovery_report

    def interrupt(self) -> None:
        """Request a cooperative stop at the next safe boundary."""
        self._interrupt_requested.set()

    def context_status(self) -> dict[str, Any]:
        if self._compression_manager is None or self._session_id is None:
            return {"available": False}
        return {
            "available": True,
            **self._compression_manager.status(self._session_id),
        }

    def compress_context(
        self,
        focus: str = "",
        *,
        force: bool = True,
    ) -> CompressionResult:
        if (
            self._compression_manager is None
            or self._session_db is None
            or self._session_id is None
        ):
            raise ValueError(
                "Persistent context compression is not available"
            )
        result = self._compression_manager.compress(
            self._session_id,
            system_prompt=str(self._messages[0].get("content", "")),
            tools=self._tools.schemas() or None,
            force=force,
            focus=focus,
        )
        if result.status == "committed":
            self._reload_session_messages()
            self._context_checkpoint = frozenset()
        self._emit_event(
            "context.compression",
            {
                "status": result.status,
                "trigger_tokens": result.trigger_tokens,
                "estimated_after_tokens": result.estimated_after_tokens,
                "compacted_message_count": result.compacted_message_count,
                "used_fallback": result.used_fallback,
                "run_id": result.run_id,
                "error_type": result.error_type,
            },
        )
        return result

    def chat_with_skill(self, invocation: SkillInvocation) -> str:
        """Run a turn with one completely loaded Skill as the user message."""

        self._emit_event("skill.loaded", invocation.event_details)
        return self.chat(invocation.message)

    def chat(self, user_message: str) -> str:
        message = user_message.strip()
        if not message:
            raise ValueError("User message cannot be empty")

        self._interrupt_requested.clear()
        self._last_turn_completed = False
        self._last_stop_reason = None
        self._emit_event(
            "turn.started",
            {"user_message_chars": len(message)},
        )
        working_messages = list(self._messages)
        self._record_message(
            working_messages,
            {"role": "user", "content": message},
        )
        if self._session_db is not None and self._session_id is not None:
            session, report = self._session_db.resume_session(self._session_id)
            self._last_recovery_report = report
            working_messages = [
                {"role": "system", "content": session.system_prompt},
                *report.messages,
            ]
            self._messages = list(working_messages)
            self._emit_recovery(report)
        schemas = self._tools.schemas()
        if self._compression_manager is not None:
            compression = self.compress_context(force=False)
            if compression.status == "committed":
                working_messages = list(self._messages)
        repeated_calls = RepeatedCallGuardrail(self._same_call_limit)
        stop_reason: str | None = None
        context_budget_failure: ContextBudgetExceeded | None = None
        requested_tool_calls = 0
        executed_tool_calls = 0
        blocked_tool_calls = 0
        consumed_tool_budget = 0

        for _iteration in range(self._max_iterations):
            if self._interrupt_requested.is_set():
                stop_reason = "interrupted"
                break
            try:
                response = self._complete(
                    working_messages,
                    tools=schemas or None,
                    phase="agent_loop",
                )
            except ContextBudgetExceeded as exc:
                context_budget_failure = exc
                stop_reason = "context_budget"
                break
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }
            if response.tool_calls:
                batch_size = len(response.tool_calls)
                requested_tool_calls += batch_size
                remaining_tool_budget = max(
                    0,
                    self._max_calls_per_turn - consumed_tool_budget,
                )
                assistant_message["tool_calls"] = [
                    call.as_message_dict() for call in response.tool_calls
                ]
                self._emit_event(
                    "tool.batch_received",
                    {
                        "count": batch_size,
                        "tool_names": [call.name for call in response.tool_calls],
                        "requested_tool_calls": requested_tool_calls,
                        "executed_tool_calls": executed_tool_calls,
                        "blocked_tool_calls": blocked_tool_calls,
                        "consumed_tool_budget": consumed_tool_budget,
                        "remaining_tool_budget": remaining_tool_budget,
                    },
                )
                # Persist the Assistant Tool Call and every prepared Journal
                # in one transaction before any Handler can start.
                journal_ids = self._record_assistant_tool_batch(
                    working_messages,
                    assistant_message,
                )
            else:
                journal_ids = ()
                self._record_message(working_messages, assistant_message)

            if not response.tool_calls:
                if response.content is None:
                    raise RuntimeError("Provider returned no final text")
                self._messages = working_messages
                self._last_turn_completed = True
                self._last_stop_reason = None
                if requested_tool_calls:
                    self._emit_tool_turn_budget(
                        requested_tool_calls=requested_tool_calls,
                        executed_tool_calls=executed_tool_calls,
                        blocked_tool_calls=blocked_tool_calls,
                        consumed_tool_budget=consumed_tool_budget,
                    )
                self._emit_event(
                    "turn.completed",
                    {"assistant_message_chars": len(response.content)},
                )
                return response.content

            batch_size = len(response.tool_calls)
            remaining_tool_budget = max(
                0,
                self._max_calls_per_turn - consumed_tool_budget,
            )
            batch_limit_exceeded = batch_size > self._max_calls_per_batch
            turn_limit_exceeded = batch_size > remaining_tool_budget
            if batch_limit_exceeded or turn_limit_exceeded:
                blocked_tool_calls += batch_size
                reason = (
                    "batch_limit"
                    if batch_limit_exceeded
                    else "turn_limit"
                )
                self._emit_event(
                    "tool.batch_rejected",
                    {
                        "reason": reason,
                        "batch_size": batch_size,
                        "max_calls_per_batch": self._max_calls_per_batch,
                        "max_calls_per_turn": self._max_calls_per_turn,
                        "requested_tool_calls": requested_tool_calls,
                        "executed_tool_calls": executed_tool_calls,
                        "blocked_tool_calls": blocked_tool_calls,
                        "consumed_tool_budget": consumed_tool_budget,
                        "remaining_tool_budget": remaining_tool_budget,
                    },
                )
                if batch_limit_exceeded:
                    rejection_message = (
                        "The entire tool batch was rejected before execution "
                        f"because it requested {batch_size} calls, exceeding "
                        f"the per-batch limit of {self._max_calls_per_batch}."
                    )
                else:
                    rejection_message = (
                        "The entire tool batch was rejected before execution "
                        f"because it requested {batch_size} calls with only "
                        f"{remaining_tool_budget} calls remaining in the turn "
                        "budget."
                    )
                for call_index, call in enumerate(response.tool_calls):
                    result = self._tools.error_result(
                        requested_name=call.name,
                        code="tool_call_budget_exceeded",
                        message=rejection_message,
                        effect_disposition="none",
                    )
                    self._commit_tool_result(
                        working_messages,
                        call,
                        result,
                        journal_id=journal_ids[call_index],
                        duration_ms=0.0,
                    )
                stop_reason = "tool_call_budget"
                break

            for call_index, call in enumerate(response.tool_calls):
                tool_started = perf_counter()
                journal_id = journal_ids[call_index]
                decision = repeated_calls.inspect(call.name, call.arguments)
                if self._interrupt_requested.is_set():
                    blocked_tool_calls += 1
                    result = self._tools.error_result(
                        requested_name=call.name,
                        code="interrupted_before_execution",
                        message="Execution was skipped because an interrupt was requested",
                        effect_disposition="none",
                    )
                    stop_reason = "interrupted"
                elif decision.blocked:
                    blocked_tool_calls += 1
                    result = self._tools.error_result(
                        requested_name=call.name,
                        code="repeated_call_blocked",
                        message=(
                            "Identical tool call was blocked after "
                            f"{self._same_call_limit} executions in this turn "
                            f"(signature={decision.signature[:12]})"
                        ),
                        effect_disposition="none",
                    )
                else:
                    consumed_tool_budget += 1
                    on_handler_start = None
                    if (
                        journal_id is not None
                        and self._session_db is not None
                    ):
                        on_handler_start = (
                            lambda backend_name, identifier=journal_id: (
                                self._session_db.mark_tool_running(
                                    identifier,
                                    execution_backend=backend_name,
                                )
                            )
                        )
                    result = self._tools.dispatch(
                        call.name,
                        call.arguments,
                        on_handler_start=on_handler_start,
                    )
                    if result.execution_phase == "rejected_before_execution":
                        blocked_tool_calls += 1
                    else:
                        executed_tool_calls += 1
                duration_ms = round((perf_counter() - tool_started) * 1000, 3)
                self._commit_tool_result(
                    working_messages,
                    call,
                    result,
                    journal_id=journal_id,
                    duration_ms=duration_ms,
                )

            if stop_reason == "interrupted":
                break

        if stop_reason is None:
            stop_reason = "iteration_limit"
        self._emit_event(
            "turn.stopping",
            {
                "reason": stop_reason,
                "requested_tool_calls": requested_tool_calls,
                "executed_tool_calls": executed_tool_calls,
                "blocked_tool_calls": blocked_tool_calls,
                "consumed_tool_budget": consumed_tool_budget,
                "remaining_tool_budget": max(
                    0,
                    self._max_calls_per_turn - consumed_tool_budget,
                ),
            },
        )
        if requested_tool_calls:
            self._emit_tool_turn_budget(
                requested_tool_calls=requested_tool_calls,
                executed_tool_calls=executed_tool_calls,
                blocked_tool_calls=blocked_tool_calls,
                consumed_tool_budget=consumed_tool_budget,
            )
        return self._finalize_without_tools(
            working_messages,
            stop_reason,
            context_budget_failure=context_budget_failure,
        )

    def _finalize_without_tools(
        self,
        working_messages: list[dict[str, Any]],
        stop_reason: str,
        *,
        context_budget_failure: ContextBudgetExceeded | None = None,
    ) -> str:
        if context_budget_failure is None:
            instruction_content = (
                "[Harness control] Tool execution has stopped because "
                f"{stop_reason}. Summarize what was completed, what failed or "
                "remains uncertain, and what should happen next. Do not call tools."
            )
        else:
            report = context_budget_failure.report
            instruction_content = (
                "[Harness control] The prior API request was not sent because "
                "protected conversation context exceeded the configured budget. "
                "State that the task stopped at the context limit and must not be "
                "claimed complete. Do not call tools or recommend automatically "
                "retrying prior tool calls. "
                f"Protected unknown tool outcomes: "
                f"{report.protected_unknown_results}."
            )
        instruction = {"role": "user", "content": instruction_content}
        # Harness control is request-local protocol, not conversation history.
        # Persisting it before the Provider succeeds contaminates Resume state
        # and can merge an obsolete instruction with the next real User turn.
        request_messages = [
            *working_messages,
            deepcopy(instruction),
        ]
        if context_budget_failure is not None:
            stable_system = [
                deepcopy(message)
                for message in working_messages
                if message.get("role") == "system"
            ][:1]
            request_messages = [*stable_system, deepcopy(instruction)]
        self._last_turn_completed = False
        self._last_stop_reason = stop_reason
        try:
            response = self._complete(
                request_messages,
                tools=None,
                phase="no_tools_finalizer",
            )
        except Exception as exc:
            self._emit_event(
                "turn.finalization_failed",
                {
                    "reason": stop_reason,
                    "error_type": type(exc).__name__,
                },
            )
            raise AgentLoopError(
                f"Agent stopped because {stop_reason}, and finalization failed: {exc}"
            ) from exc
        if response.tool_calls or response.content is None:
            raise AgentLoopError(
                f"Agent stopped because {stop_reason}, but the no-tools finalizer "
                "did not return final text"
            )
        self._record_message(
            working_messages,
            {"role": "assistant", "content": response.content},
        )
        self._messages = working_messages
        self._last_turn_completed = False
        self._last_stop_reason = stop_reason
        self._emit_event(
            "turn.partial",
            {
                "reason": stop_reason,
                "assistant_message_chars": len(response.content),
            },
        )
        return response.content

    def _commit_tool_result(
        self,
        working_messages: list[dict[str, Any]],
        call: ToolCall,
        result: ToolExecutionResult,
        *,
        journal_id: int | None,
        duration_ms: float,
    ) -> None:
        if self._tool_observer is not None:
            self._tool_observer(call, result)
        self._emit_event(
            "tool.result",
            {
                "tool_call_id": call.id,
                "requested_name": call.name,
                "executed_name": result.executed_name,
                "ok": result.ok,
                "error_code": result.error_code,
                "effect_disposition": result.effect_disposition,
                "execution_phase": result.execution_phase,
                "execution_backend": result.execution_backend,
                "hard_terminated": result.hard_terminated,
                "name_repaired": result.name_repaired,
                "duration_ms": duration_ms,
            },
        )
        if result.executed_name == "memory_update":
            self._emit_event(
                "memory.update_result",
                {
                    "tool_call_id": call.id,
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "effect_disposition": result.effect_disposition,
                },
            )
        self._record_tool_result(
            working_messages,
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result.content,
            },
            journal_id=journal_id,
            result=result,
        )

    def _emit_tool_turn_budget(
        self,
        *,
        requested_tool_calls: int,
        executed_tool_calls: int,
        blocked_tool_calls: int,
        consumed_tool_budget: int,
    ) -> None:
        self._emit_event(
            "tool.turn_budget",
            {
                "requested_tool_calls": requested_tool_calls,
                "executed_tool_calls": executed_tool_calls,
                "blocked_tool_calls": blocked_tool_calls,
                "consumed_tool_budget": consumed_tool_budget,
                "remaining_tool_budget": max(
                    0,
                    self._max_calls_per_turn - consumed_tool_budget,
                ),
                "max_calls_per_turn": self._max_calls_per_turn,
            },
        )

    def _record_message(
        self,
        working_messages: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        working_messages.append(message)
        if self._session_db is not None and self._session_id is not None:
            self._session_db.append_message(self._session_id, message)
            self._messages = list(working_messages)
        elif message.get("role") == "tool":
            # Without persistence, retain evidence as soon as a side effect may
            # have occurred, even if the next provider request fails.
            self._messages = list(working_messages)

    def _reload_session_messages(self) -> None:
        if self._session_db is None or self._session_id is None:
            return
        session, report = self._session_db.resume_session(self._session_id)
        self._last_recovery_report = report
        self._messages = [
            {"role": "system", "content": session.system_prompt},
            *report.messages,
        ]

    def _record_assistant_tool_batch(
        self,
        working_messages: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> tuple[int | None, ...]:
        tool_calls = message.get("tool_calls") or []
        if self._session_db is None or self._session_id is None:
            working_messages.append(message)
            return tuple(None for _call in tool_calls)

        _message_id, journal_ids = (
            self._session_db.append_assistant_with_prepared_journals(
                self._session_id,
                message,
            )
        )
        working_messages.append(message)
        self._messages = list(working_messages)
        return tuple(journal_ids)

    def _record_tool_result(
        self,
        working_messages: list[dict[str, Any]],
        message: dict[str, Any],
        *,
        journal_id: int | None,
        result: ToolExecutionResult,
    ) -> None:
        if (
            journal_id is None
            or self._session_db is None
            or self._session_id is None
        ):
            self._record_message(working_messages, message)
            return

        self._session_db.append_tool_result_and_finalize_journal(
            self._session_id,
            journal_id,
            message,
            effect_disposition=result.effect_disposition,
            execution_phase=result.execution_phase,
            execution_backend=result.execution_backend,
            hard_terminated=result.hard_terminated,
            error_code=result.error_code,
        )
        working_messages.append(message)
        self._messages = list(working_messages)

    def _complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None,
        phase: str,
    ) -> ProviderResponse:
        started = perf_counter()
        api_batch = self._api_message_builder.build(
            messages,
            tools=tools,
            context_checkpoint=self._context_checkpoint,
        )
        self._context_checkpoint = (
            self._context_checkpoint | api_batch.context_checkpoint
        )
        api_messages = api_batch.messages
        if api_batch.report.changed:
            self._emit_event(
                "api_messages.prepared",
                {
                    "dropped_messages": api_batch.report.dropped_messages,
                    "dropped_fields": api_batch.report.dropped_fields,
                    "inserted_unknown_results": (
                        api_batch.report.inserted_unknown_results
                    ),
                    "dropped_orphan_results": (
                        api_batch.report.dropped_orphan_results
                    ),
                    "repaired_tool_call_ids": (
                        api_batch.report.repaired_tool_call_ids
                    ),
                    "merged_adjacent_users": (
                        api_batch.report.merged_adjacent_users
                    ),
                    "compacted_tool_results": (
                        api_batch.report.context_budget.compacted_tool_results
                        if api_batch.report.context_budget is not None
                        else 0
                    ),
                    "dropped_context_messages": (
                        api_batch.report.context_budget.dropped_messages
                        if api_batch.report.context_budget is not None
                        else 0
                    ),
                },
            )
        budget_report = api_batch.report.context_budget
        if budget_report is not None:
            self._emit_event(
                "context.budget_evaluated",
                {
                    "max_input_tokens": budget_report.max_input_tokens,
                    "available_input_tokens": (
                        budget_report.available_input_tokens
                    ),
                    "compaction_target_tokens": (
                        budget_report.compaction_target_tokens
                    ),
                    "estimated_tokens_before": (
                        budget_report.estimated_tokens_before
                    ),
                    "estimated_tokens_after": (
                        budget_report.estimated_tokens_after
                    ),
                    "tool_schema_tokens": budget_report.tool_schema_tokens,
                    "compacted_tool_results": (
                        budget_report.compacted_tool_results
                    ),
                    "protected_recent_tool_results": (
                        budget_report.protected_recent_tool_results
                    ),
                    "pruned_old_tool_results": (
                        budget_report.pruned_old_tool_results
                    ),
                    "deduplicated_tool_results": (
                        budget_report.deduplicated_tool_results
                    ),
                    "dropped_turns": budget_report.dropped_turns,
                    "dropped_messages": budget_report.dropped_messages,
                    "reused_checkpoint_turns": (
                        budget_report.reused_checkpoint_turns
                    ),
                    "protected_unknown_results": (
                        budget_report.protected_unknown_results
                    ),
                    "budget_exceeded": budget_report.budget_exceeded,
                },
            )
            if budget_report.budget_exceeded:
                self._emit_event(
                    "context.budget_exceeded",
                    {
                        "estimated_tokens_after": (
                            budget_report.estimated_tokens_after
                        ),
                        "available_input_tokens": (
                            budget_report.available_input_tokens
                        ),
                        "protected_unknown_results": (
                            budget_report.protected_unknown_results
                        ),
                    },
                )
                raise ContextBudgetExceeded(budget_report)
        self._emit_event(
            "provider.request_started",
            {
                "phase": phase,
                "message_count": len(api_messages),
                "tools_enabled": bool(tools),
            },
        )
        try:
            response = self._provider.complete(api_messages, tools=tools)
        except Exception as exc:
            self._emit_event(
                "provider.request_failed",
                {
                    "phase": phase,
                    "error_type": type(exc).__name__,
                    "attempt_count": getattr(exc, "attempts", 1),
                    "retryable": getattr(exc, "retryable", False),
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )
            raise
        self._emit_event(
            "provider.request_completed",
            {
                "phase": phase,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "content_present": response.content is not None,
                "tool_call_count": len(response.tool_calls),
                "attempt_count": response.attempt_count,
            },
        )
        return response

    def _emit_recovery(self, report: RecoveryReport) -> None:
        if not report.changed:
            return
        self._emit_event(
            "recovery.applied",
            {
                "inserted_unknown_results": report.inserted_unknown_results,
                "inserted_not_executed_results": (
                    report.inserted_not_executed_results
                ),
                "deactivated_orphan_results": report.deactivated_orphan_results,
                "repaired_tool_call_ids": report.repaired_tool_call_ids,
                "merged_same_role_messages": report.merged_same_role_messages,
            },
        )

    def _emit_event(
        self,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink.emit(
                event,
                session_id=self._session_id,
                details=details,
            )
        except Exception:
            # Diagnostics must never change Agent behavior or tool semantics.
            return
